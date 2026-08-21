"""v5.4: 无用效果重审修复——光锥效果建模测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.models.equipment import load_lightcone
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, TimedBuff, _use_skill, _process_lc_effects, _apply_lc_condition_corrections,
    _build_effective_stats, _apply_target_relic_modifiers, _apply_toughness_damage,
    _tick_buffs, _respawn_wave,
)


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, lc_id=None, **extra):
    c = load_character(cid, 'data/characters')
    lc = load_lightcone(lc_id) if lc_id else None
    stats = compute_combat_stats(c, lc, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.lightcone = lc
    u.extra.update(extra)
    return u


class TestEventActions:
    def test_quid_pro_quo(self):
        """等价交换: 回合开始随机为能量<50%的队友回16能量"""
        u = _unit('lingsha', lc_id='quid_pro_quo')
        ally = _unit('seele', position=2)
        ally.current_energy = 10.0
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, "on_self_turn_start")
        assert ally.current_energy == pytest.approx(26.0, abs=1e-6)

    def test_starlight_memsprite_despawn(self):
        """致长夜的星光: 忆灵消失回8能量"""
        u = _unit('trailblazer_remembrance', lc_id='starlight_to_the_long_night')
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_memsprite_despawn")
        assert u.current_energy == pytest.approx(8.0, abs=1e-6)

    def test_starlight_reacts_to_an_ally_memsprite_despawn(self):
        """致长夜的星光持有者应监听队友忆灵消失，而非只监听自身忆灵。"""
        from engine.systems.remembrance import RemembranceSystem

        holder = _unit('trailblazer_remembrance', lc_id='starlight_to_the_long_night')
        summoner = _unit('aglaea', position=2)
        state = SimState(enemies=[_enemy()], units=[holder, summoner])
        system = RemembranceSystem()
        memsprite = system.summon_memsprite(state, summoner, summoner.char.memsprite)

        system.despawn_memsprite(state, summoner, memsprite)

        assert holder.current_energy == pytest.approx(8.0, abs=1e-9)

    def test_grounded_ascent_sp_recovery(self):
        """回到大地的飞行: 2次战技/终结技后回1战技点"""
        u = _unit('trailblazer_harmony', lc_id='a_grounded_ascent')
        u.extra['lc_last_skill_target_type'] = 'single_ally'
        state = SimState(enemies=[_enemy()], units=[u])
        state.skill_points = 0
        _process_lc_effects(u, state, "on_skill")
        assert state.skill_points == 0
        _process_lc_effects(u, state, "on_skill")
        assert state.skill_points == 1

    def test_grounded_ascent_recovers_energy_and_grants_hymn(self):
        """每次单体辅助技都应回6能量，并给技能目标可叠层的3回合圣咏。"""
        owner = _unit('bronya', lc_id='a_grounded_ascent')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[owner, ally])
        owner.extra['lc_last_skill_target_type'] = 'single_ally'

        _process_lc_effects(owner, state, 'on_skill')

        assert owner.current_energy == pytest.approx(6.0, abs=1e-9)
        hymn = next(b for b in ally.buffs if b.param_id == 'grounded_ascent_hymn')
        assert hymn.attributes['DMG_BONUS_ALL'] == pytest.approx(15.0, abs=1e-9)
        assert hymn.remaining_turns == 3

    def test_grounded_ascent_uses_the_resolved_skill_target(self):
        """圣咏应给实际单体目标，不能依赖队内是否存在希儿。"""
        owner = _unit('bronya', lc_id='a_grounded_ascent')
        target = _unit('aglaea', position=2)
        state = SimState(enemies=[_enemy()], units=[owner, target])
        owner.extra['lc_last_skill_target_type'] = 'single_ally'
        owner.extra['lc_last_skill_target'] = target

        _process_lc_effects(owner, state, 'on_skill')

        assert any(b.param_id == 'grounded_ascent_hymn' for b in target.buffs)
        assert not any(b.param_id == 'grounded_ascent_hymn' for b in owner.buffs)

    def test_epoch_sp_recovery_counts_for_earthly_escapade(self):
        """所有回SP来源都应经过统一入口，触发彩焰计数。"""
        earthly_owner = _unit('bronya', lc_id='earthly_escapade')
        epoch_owner = _unit('trailblazer_harmony', position=2,
                            lc_id='epoch_etched_in_golden_blood')
        state = SimState(enemies=[_enemy()], units=[earthly_owner, epoch_owner])
        state.skill_points = 0

        _process_lc_effects(epoch_owner, state, 'on_ult')

        assert state.skill_points == 1
        assert earthly_owner.extra.get('masquerade_caiyan') == 1

    def test_today_peaceful_day_dmg_bonus(self):
        """今日亦是和平的一日: 增伤 = min(能量上限,160)×0.4%"""
        u = _unit('the_herta', lc_id='today_is_another_peaceful_day')  # 140能量
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_battle_start")
        assert any(getattr(b, 'attributes', {}).get('DMG_BONUS_ALL') == pytest.approx(64.0)
                   for b in u.buffs)  # the_herta 220能量 → 160封顶 ×0.4


class TestEnemyMarkers:
    def test_life_flames_restores_energy_on_turn_start(self):
        """生命当付之一炬: 装备者回合开始时恢复10点能量。"""
        u = _unit('the_herta', lc_id='life_should_be_cast_to_flames')
        state = SimState(enemies=[_enemy()], units=[u])

        _process_lc_effects(u, state, 'on_self_turn_start')

        assert u.current_energy == pytest.approx(10.0, abs=1e-9)

    def test_life_flames_def_down(self):
        """生命当付之一炬: 攻击使目标DEF-12% 2回合"""
        u = _unit('the_herta', lc_id='life_should_be_cast_to_flames')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _process_lc_effects(u, state, "on_self_attack")
        assert e.status_attribute('def_reduction') == pytest.approx(0.12, abs=1e-9)

    def test_gongxian_probability_hit(self, monkeypatch):
        """决心如汗珠般闪耀精5（用户确认）: 100%基础概率攻陷 DEF-16%"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.10)
        u = _unit('fugue', lc_id='resolution_shines_as_pearls_of_sweat')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _process_lc_effects(u, state, "on_self_attack")
        assert e.status_attribute('def_reduction') == pytest.approx(0.16, abs=1e-9)

    def test_gongxian_rank1_hit(self, monkeypatch):
        """精1: 60%基础概率命中 → DEF-12%"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.10)
        u = _unit('fugue', lc_id='resolution_shines_as_pearls_of_sweat')
        u.lightcone.rank = 1
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _process_lc_effects(u, state, "on_self_attack")
        assert e.status_attribute('def_reduction') == pytest.approx(0.12, abs=1e-9)

    def test_gongxian_probability_miss(self, monkeypatch):
        """精1(60%基础概率): 未命中 → 无攻陷"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.90)
        u = _unit('fugue', lc_id='resolution_shines_as_pearls_of_sweat')
        u.lightcone.rank = 1
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _process_lc_effects(u, state, "on_self_attack")
        assert e.status_attribute('def_reduction') == pytest.approx(0.0, abs=1e-9)

    def test_kubai_break_dmg_bonus(self):
        """梦应归于何处: 溃败状态下装备者击破伤害+24%"""
        u = _unit('firefly', lc_id='whereabouts_should_dreams_rest')
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        _use_skill(u, state, 'basic_attack')  # 击破 → 溃败
        assert e.has_status(status_id='kubai')
        assert e.status_attribute('spd_down') == pytest.approx(0.20, abs=1e-9)
        # 再次击破（韧性已恢复）验证 break_mult×1.24
        from engine.core.combat_sim import _begin_enemy_turn
        _begin_enemy_turn(state, e)  # 韧性恢复（溃败2回合→1）
        e.is_broken = False
        e.toughness = 10.0
        hp0 = e.HP
        _use_skill(u, state, 'basic_attack')
        assert e.HP < hp0  # 击破伤害含+24%

    def test_wenshun_cd_bonus(self):
        """烦恼着，幸福着: 温驯2层 → 命中者CD+24%"""
        u = _unit('seele', lc_id='worrisome_blissful')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _process_lc_effects(u, state, "on_followup")  # 温驯1层
        _process_lc_effects(u, state, "on_followup")  # 温驯2层
        stats = _build_effective_stats(u, state)
        t_stats = _apply_target_relic_modifiers(stats, u, e)
        assert t_stats.CRIT_DMG == pytest.approx(stats.CRIT_DMG + 0.24, abs=1e-9)


class TestHealRecord:
    def test_time_waits_record_is_consumed_by_ally_attack(self):
        """时节不居: 任意我方角色攻击都应消费持有者的治疗记录。"""
        holder = _unit('lingsha', lc_id='时节不居')
        ally = _unit('seele', position=2)
        enemy = _enemy()
        state = SimState(enemies=[enemy], units=[holder, ally])
        state.extra['lc_last_heal_amt'] = 1000.0
        _process_lc_effects(holder, state, 'on_heal')

        _use_skill(ally, state, 'basic_attack')

        assert holder.extra.get('lc_heal_record') == pytest.approx(0.0, abs=1e-9)
        assert holder.extra.get('lc_heal_record_used_this_turn') is True

    def test_time_waits_records_marker_healing(self):
        """光锥持有者通过行动条标记治疗时也应产生治疗记录。"""
        from engine.core.combat_sim import _marker_heal_allies

        holder = _unit('lingsha', lc_id='时节不居')
        holder.current_hp -= 100.0
        state = SimState(enemies=[_enemy()], units=[holder])

        _marker_heal_allies(state, holder, 'lingsha_fuyuan_heal')

        assert holder.extra.get('lc_heal_record', 0.0) > 0.0

    def test_time_waits_for_no_one_extra_damage(self):
        """时节不居: 治疗记录36% → 攻击后附加伤害"""
        u = _unit('lingsha', lc_id='时节不居')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['lc_last_heal_amt'] = 1000.0
        _process_lc_effects(u, state, "on_heal")
        hp0 = e.HP
        _process_lc_effects(u, state, "on_self_attack")
        assert hp0 - e.HP == pytest.approx(360.0, abs=1e-6)  # 1000×36%
        # 每回合最多1次
        _process_lc_effects(u, state, "on_self_attack")
        assert hp0 - e.HP == pytest.approx(360.0, abs=1e-6)


class TestFlamesAfar:
    def test_flames_afar_triggers_at_exactly_one_quarter_hp_loss(self):
        """在火的远处的“至少25%”包含刚好损失25%最大生命。"""
        u = _unit('firefly', lc_id='flames_afar')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['lc_last_hp_loss'] = u.max_hp * 0.25

        _process_lc_effects(u, state, 'on_hp_loss')

        assert u.extra.get('flames_afar_cd') == 3
        assert any(b.source_id == 'flames_afar' for b in u.buffs)


class TestSleepLikeDead:
    def test_miss_crit_triggers_cr_buff(self, monkeypatch):
        """如泥酣眠: 未暴击概率触发 CR+36%（期望模式 1-CR 判定）"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.10)
        u = _unit('seele', lc_id='sleep_like_the_dead')
        u.base_stats.CRIT_RATE = 0.20
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['lc_last_skill_key'] = 'basic_attack'
        _process_lc_effects(u, state, "on_self_attack")
        assert any(getattr(b, 'attributes', {}).get('CRIT_RATE') == pytest.approx(36.0)
                   for b in u.buffs)


class TestFlowersWorld:
    def test_sp_spent_defpen(self):
        """花花世界迷人眼: 每消耗1SP无视5%防御（4层封顶）"""
        u = _unit('trailblazer_elation', lc_id='花花世界迷人眼')
        state = SimState(enemies=[_enemy()], units=[u])
        # _build_effective_stats 内置条件修正 → 动态按 SP 消耗比例
        s0 = _build_effective_stats(u, state)
        assert s0.DEF_PEN == pytest.approx(0.0, abs=1e-9)  # 未耗SP → 0
        state.extra['lc_sp_spent'] = 2
        s2 = _build_effective_stats(u, state)
        assert s2.DEF_PEN == pytest.approx(0.10, abs=1e-9)  # 2/4×20%
        state.extra['lc_sp_spent'] = 4
        s4 = _build_effective_stats(u, state)
        assert s4.DEF_PEN == pytest.approx(0.20, abs=1e-9)  # 满层


class TestBatch2:
    def test_taunt_mult_dance_at_sunset(self):
        """落日时起舞: 受击概率×6"""
        u = _unit('firefly', lc_id='dance_at_sunset')
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_battle_start")
        assert u.extra.get('taunt_mult') == pytest.approx(6.0, abs=1e-9)

    def test_taunt_mult_moment_of_victory(self):
        """制胜的瞬间: 受击概率×3"""
        u = _unit('gepard', lc_id='moment_of_victory')
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_battle_start")
        assert u.extra.get('taunt_mult') == pytest.approx(3.0, abs=1e-9)

    def test_planetary_rendezvous_element_bonus(self):
        """与行星相会: 全队同属性增伤12%"""
        u = _unit('trailblazer_harmony', lc_id='planetary_rendezvous')  # 虚数
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, "on_battle_start")
        from engine.core.combat_sim import _build_effective_stats
        s = _build_effective_stats(ally, state)
        assert s.DMG_BONUS['虚数'] == pytest.approx(0.12, abs=1e-9)

    def test_we_will_meet_again_extra_damage(self):
        """后会有期: 普攻/战技后48%ATK附加伤害"""
        u = _unit('fugue', lc_id='we_will_meet_again')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        hp0 = e.HP
        _process_lc_effects(u, state, "on_skill")
        assert hp0 - e.HP == pytest.approx(u.base_stats.ATK * 0.48, abs=1e-6)

    def test_flames_afar_threshold(self):
        """在火的远处: 单次损失≥25%生命上限→回15%HP+增伤25%（3回合CD）"""
        u = _unit('firefly', lc_id='flames_afar')
        state = SimState(enemies=[_enemy()], units=[u])
        u.current_hp -= 1000  # 先扣血供回血验证
        hp0 = u.current_hp
        state.extra['lc_last_hp_loss'] = u.max_hp * 0.30
        _process_lc_effects(u, state, "on_hp_loss")
        assert u.current_hp == pytest.approx(hp0 + u.max_hp * 0.15, abs=1e-6)
        assert any(getattr(b, 'attributes', {}).get('DMG_BONUS_ALL') == pytest.approx(25.0)
                   for b in u.buffs)
        # 3回合CD内不重复触发
        hp1 = u.current_hp
        state.extra['lc_last_hp_loss'] = u.max_hp * 0.30
        _process_lc_effects(u, state, "on_hp_loss")
        assert u.current_hp == hp1

    def test_flames_afar_exactly_25_percent_triggers(self):
        """文本为“至少25%”，恰好25%应触发。"""
        u = _unit('firefly', lc_id='flames_afar')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['lc_last_hp_loss'] = u.max_hp * 0.25

        _process_lc_effects(u, state, 'on_hp_loss')

        assert any(b.source_id == 'flames_afar' for b in u.buffs)

    def test_childlike_mark(self):
        """美梦小镇大冒险: 最新技能类型童心→全队对应类型增伤12%"""
        u = _unit('trailblazer_harmony', lc_id='dreamville_adventure')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, "on_skill")
        assert any(getattr(b, 'param_id', '') == 'childlike' for b in ally.buffs)
        assert u.extra.get('childlike_type') == 'skill'

    def test_swordplay_stack_and_consume(self):
        """论剑: 同目标叠层(S1=8%/层, v5.7 用户实机确认分档8/10/12/14/16), 换目标重置"""
        u = _unit('seele', lc_id='swordplay')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['lc_attack_first_target_id'] = e.id
        _process_lc_effects(u, state, "on_self_attack")  # 层1
        _process_lc_effects(u, state, "on_self_attack")  # 层2
        assert u.extra.get('swordplay_layers') == 2
        from engine.core.combat_sim import _lc_target_correct, _build_effective_stats
        s = _build_effective_stats(u, state)
        t = _lc_target_correct(s, u, state, e)
        assert t.DMG_BONUS_ALL == pytest.approx(s.DMG_BONUS_ALL + 0.08 * 2, abs=1e-9)
        # 换目标重置
        state.extra['lc_attack_first_target_id'] = 'other'
        _process_lc_effects(u, state, "on_self_attack")
        assert u.extra.get('swordplay_layers') == 1

    def test_masquerade_caiyan_full(self):
        """游戏尘寰: 彩焰4层→假面4回合（全队jiamian叠层刷新）"""
        from engine.core.combat_sim import _lc_masquerade_caiyan
        u = _unit('bronya', lc_id='earthly_escapade')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        for _ in range(4):
            _lc_masquerade_caiyan(state, u)
        assert ally.lc_stacks.get('earthly_escapade::jiamian') == 1
        assert ally.lc_stack_turns.get('earthly_escapade::jiamian') == [4]  # v5.6: 分层容器

    def test_masquerade_counts_teammate_sp_recovery_and_overflow(self):
        """任意队友恢复SP都应生成彩焰，满SP时的溢出也必须计数。"""
        owner = _unit('bronya', lc_id='earthly_escapade')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[owner, ally])
        state.skill_points = state.max_sp

        for _ in range(4):
            _use_skill(ally, state, 'basic_attack')

        assert ally.lc_stacks.get('earthly_escapade::jiamian') == 1
        assert ally.lc_stack_turns.get('earthly_escapade::jiamian') == [4]  # v5.6: 分层容器

    def test_love_forever_blank_and_poem(self):
        """爱如此刻永恒: 忆灵技后空白(敌方易伤)/诗行(全队CD), 当局永久"""
        u = _unit('trailblazer_remembrance', lc_id='this_love_forever')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['lc_last_memsprite_target'] = 'single_enemy'
        _process_lc_effects(u, state, "on_memsprite_attack")  # 诗行
        assert u.extra.get('love_poem')
        assert any(getattr(b, 'param_id', '') == 'love_poem' for b in u.buffs)
        state.extra['lc_last_memsprite_target'] = 'single_ally'
        _process_lc_effects(u, state, "on_memsprite_attack")  # 空白（双持→×1.6）
        assert u.extra.get('love_blank')
        assert e.extra.get('love_blank_vuln') == pytest.approx(0.16, abs=1e-9)  # 10%×1.6

    def test_life_flames_weakness_dmg_bonus(self):
        """生命当付之一炬: 目标拥有弱点植入→装备者伤害+60%"""
        from engine.core.combat_sim import _lc_target_correct, _build_effective_stats
        u = _unit('the_herta', lc_id='life_should_be_cast_to_flames')
        e = _enemy()
        e.add_status(EnemyStatus(id='firefly_fire_weakness', name='火弱点', category='debuff',
                                 source=u.char.id,
                                 remaining_turns=2, attributes={'weakness_element': '火',
                                                                 'weakness_old_res': 0.0}))
        state = SimState(enemies=[e], units=[u])
        s = _build_effective_stats(u, state)
        t = _lc_target_correct(s, u, state, e)
        assert t.DMG_BONUS_ALL == pytest.approx(s.DMG_BONUS_ALL + 0.60, abs=1e-9)

    def test_life_flames_ignores_weakness_added_by_ally(self):
        """生命当付之一炬只识别装备者自己添加的弱点。"""
        from engine.core.combat_sim import _lc_target_correct

        u = _unit('the_herta', lc_id='life_should_be_cast_to_flames')
        enemy = _enemy()
        enemy.add_status(EnemyStatus(
            id='ally_weakness', name='队友弱点', category='debuff', source='firefly',
            remaining_turns=2,
            attributes={'weakness_element': '火', 'weakness_old_res': 0.0},
        ))
        state = SimState(enemies=[enemy], units=[u])
        stats = _build_effective_stats(u, state)

        corrected = _lc_target_correct(stats, u, state, enemy)

        assert corrected.DMG_BONUS_ALL == pytest.approx(stats.DMG_BONUS_ALL, abs=1e-9)


class TestBaseSpd:
    def test_base_spd_plus_12(self):
        """黎明恰如此燃烧: 基础速度+12（白值加算, 吃百分比加成）"""
        from engine.core.attributes import compute_combat_stats
        c = load_character('firefly', 'data/characters')
        lc = load_lightcone('thus_burns_the_dawn')
        stats = compute_combat_stats(c, lc, None, None)
        # v6.6c P3: 行迹固定速度（+5）按实机计入白值: 白值 = base + 行迹5 + 光锥12
        assert stats._base_SPD == pytest.approx(c.base_SPD + 5.0 + 12.0, abs=1e-9)
        assert stats.SPD == pytest.approx(c.base_SPD + 5.0 + 12.0, abs=1e-9)

    def test_today_peaceful_day_energy_bonus(self):
        """今日亦是和平的一日: 增伤=min(能量,160)×0.4%（the_herta 220→160→64%）"""
        u = _unit('the_herta', lc_id='today_is_another_peaceful_day')
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_battle_start")
        assert any(getattr(b, 'attributes', {}).get('DMG_BONUS_ALL') == pytest.approx(64.0)
                   for b in u.buffs)


class TestLandausChoice:
    def test_taunt_mult_landaus(self):
        """朗道的选择: 受击概率×3 + 常驻减伤16%"""
        u = _unit('gepard', lc_id='landaus_choice')
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_battle_start")
        assert u.extra.get('taunt_mult') == pytest.approx(3.0, abs=1e-9)
        from engine.core.combat_sim import _build_effective_stats
        s = _build_effective_stats(u, state)
        assert s.DMG_REDUCTION == pytest.approx(0.16, abs=1e-9)


class TestV54ReviewRegressions:
    def test_event_action_runs_once_when_multiple_effect_rows_share_event(self):
        """同一光锥的多个描述行不能让单次事件重复执行同一个动作。"""
        u = _unit('trailblazer_remembrance', lc_id='this_love_forever')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['lc_last_memsprite_target'] = 'single_enemy'

        _process_lc_effects(u, state, 'on_memsprite_attack')

        logs = [line for line in state.log if '光锥[this_love_forever]' in line]
        assert len(logs) == 1

    def test_memsprite_attack_dispatches_lightcone_event_once(self, monkeypatch):
        """一次真实忆灵攻击只能派发一次光锥忆灵攻击事件。"""
        from engine.core import combat_sim
        from engine.systems.remembrance import RemembranceSystem

        u = _unit('trailblazer_remembrance', lc_id='this_love_forever')
        state = SimState(enemies=[_enemy()], units=[u])
        system = RemembranceSystem()
        ms = system.summon_memsprite(state, u, u.char.memsprite)
        calls = []
        original = combat_sim._process_lc_effects

        def record(unit, sim_state, event_type):
            if event_type == 'on_memsprite_attack':
                calls.append(event_type)
            return original(unit, sim_state, event_type)

        monkeypatch.setattr(combat_sim, '_process_lc_effects', record)
        system._use_memsprite_skill(state, u, ms, 'memsprite_basic')

        assert calls == ['on_memsprite_attack']

    def test_memsprite_support_skill_triggers_blank_mark(self):
        """无伤害的单体忆灵辅助技也必须触发【空白】与忆灵施技事件。"""
        from engine.systems.remembrance import RemembranceSystem

        u = _unit('trailblazer_remembrance', lc_id='this_love_forever')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        system = RemembranceSystem()
        ms = system.summon_memsprite(state, u, u.char.memsprite)

        system._use_memsprite_skill(state, u, ms, 'memsprite_support')

        assert u.extra.get('love_blank') is True
        assert not u.extra.get('love_poem')
        assert state.enemies[0].extra.get('love_blank_vuln') == pytest.approx(0.10, abs=1e-9)

    def test_love_blank_persists_across_waves(self):
        """【空白】是当局永久标记，新波次敌人仍应获得易伤。"""
        u = _unit('trailblazer_remembrance', lc_id='this_love_forever')
        first = _enemy()
        state = SimState(enemies=[first], units=[u])
        state.extra['enemy_blueprint'] = _enemy()
        state.extra['num_enemies'] = 2
        state.extra['lc_last_memsprite_target'] = 'single_ally'
        _process_lc_effects(u, state, 'on_memsprite_attack')

        _respawn_wave(state)

        assert len(state.enemies) == 2
        assert all(enemy.extra.get('love_blank_vuln') == pytest.approx(0.10, abs=1e-9)
                   for enemy in state.enemies)

    def test_starlight_def_pen_requires_yese(self):
        """致长夜的星光的忆灵无视防御必须由【夜色】激活。"""
        u = _unit('trailblazer_remembrance', lc_id='starlight_to_the_long_night')
        state = SimState(enemies=[_enemy()], units=[u])

        assert _build_effective_stats(u, state).DEF_PEN_MEMSPRITE == pytest.approx(0.0, abs=1e-9)
        _process_lc_effects(u, state, 'on_memsprite_skill')
        assert _build_effective_stats(u, state).DEF_PEN_MEMSPRITE == pytest.approx(0.20, abs=1e-9)

    def test_starlight_yese_buffs_allied_memsprite_damage(self):
        """持有【夜色】时，另一名我方角色的忆灵也应获得20%无视防御。"""
        from engine.systems.remembrance import RemembranceSystem

        owner = _unit('trailblazer_remembrance', lc_id='starlight_to_the_long_night')
        ally = _unit('aglaea', position=2)
        enemy = _enemy(hp=1000000, toughness=0)
        enemy.DEF = 800
        state = SimState(enemies=[enemy], units=[owner, ally])
        system = RemembranceSystem()
        system.summon_memsprite(state, owner, owner.char.memsprite)
        ally_ms = system.summon_memsprite(state, ally, ally.char.memsprite)

        hp0 = enemy.HP
        system._use_memsprite_skill(state, ally, ally_ms, 'memsprite_basic')
        before = hp0 - enemy.HP

        enemy.HP = hp0
        _process_lc_effects(owner, state, 'on_memsprite_skill')
        system._use_memsprite_skill(state, ally, ally_ms, 'memsprite_basic')
        after = hp0 - enemy.HP

        no_pen = 1000.0 / (1000.0 + 800.0)
        pen_20 = 1000.0 / (1000.0 + 800.0 * 0.80)
        assert after == pytest.approx(before * pen_20 / no_pen, rel=1e-9)

    def test_childlike_only_buffs_latest_skill_type(self):
        """童心应进入技能类型乘区，不能变成全类型增伤。"""
        u = _unit('trailblazer_harmony', lc_id='dreamville_adventure')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        before = _build_effective_stats(ally, state)

        _process_lc_effects(u, state, 'on_skill')
        after = _build_effective_stats(ally, state)

        assert after.DMG_BONUS_ALL == pytest.approx(before.DMG_BONUS_ALL, abs=1e-9)
        assert after.DMG_BONUS_BY_SKILL_TYPE['skill'] == pytest.approx(0.12, abs=1e-9)
        assert after.DMG_BONUS_BY_SKILL_TYPE.get('basic', 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_grounded_ascent_counter_is_per_holder(self):
        """两名持有者各触发一次时，不能共享计数并提前回SP。"""
        u1 = _unit('trailblazer_harmony', lc_id='a_grounded_ascent')
        u2 = _unit('trailblazer_harmony', position=2, lc_id='a_grounded_ascent')
        u1.extra['lc_last_skill_target_type'] = 'single_ally'
        u2.extra['lc_last_skill_target_type'] = 'single_ally'
        state = SimState(enemies=[_enemy()], units=[u1, u2])
        state.skill_points = 0

        _process_lc_effects(u1, state, 'on_skill')
        _process_lc_effects(u2, state, 'on_skill')

        assert state.skill_points == 0
        assert u1.extra.get('lc_grounded_count') == 1
        assert u2.extra.get('lc_grounded_count') == 1

    def test_grounded_ascent_ignores_enemy_targeting_skill(self):
        """攻击敌人的战技不满足“对我方单体施放”的触发条件。"""
        u = _unit('trailblazer_harmony', lc_id='a_grounded_ascent')
        u.extra['lc_last_skill_target_type'] = 'single_ally'
        state = SimState(enemies=[_enemy()], units=[u])

        _use_skill(u, state, 'skill')

        assert u.extra.get('lc_grounded_count', 0) == 0

    def test_sleep_like_dead_uses_current_attack_and_survives_action_end(self, monkeypatch):
        """未暴击判定读取本次技能，触发的一回合Buff不能在当前行动末立即消失。"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.0)
        u = _unit('seele', lc_id='sleep_like_the_dead')
        u.base_stats.CRIT_RATE = 0.20
        state = SimState(enemies=[_enemy()], units=[u])

        _use_skill(u, state, 'basic_attack')
        assert any(b.source_id == 'sleep_like_the_dead' for b in u.buffs)

        _tick_buffs(u)
        assert any(b.source_id == 'sleep_like_the_dead' for b in u.buffs)

    def test_sleep_like_dead_cooldown_is_per_holder(self, monkeypatch):
        """如泥酣眠的冷却不能由另一名持有者共享或阻断。"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.0)
        u1 = _unit('seele', lc_id='sleep_like_the_dead')
        u2 = _unit('seele', position=2, lc_id='sleep_like_the_dead')
        u1.base_stats.CRIT_RATE = u2.base_stats.CRIT_RATE = 0.20
        u1.extra['lc_last_skill_key'] = u2.extra['lc_last_skill_key'] = 'basic_attack'
        state = SimState(enemies=[_enemy()], units=[u1, u2])
        state.extra['lc_last_skill_key'] = 'basic_attack'

        _process_lc_effects(u1, state, 'on_self_attack')
        _process_lc_effects(u2, state, 'on_self_attack')

        assert u1.extra.get('lc_sleep_cd') == 3
        assert u2.extra.get('lc_sleep_cd') == 3
        assert any(b.source_id == 'sleep_like_the_dead' for b in u1.buffs)
        assert any(b.source_id == 'sleep_like_the_dead' for b in u2.buffs)

    def test_sleep_like_dead_extra_turn_does_not_gain_an_extra_duration(self, monkeypatch):
        """X轴不tick，额外回合触发的一回合Buff应从1开始计时。"""
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.0)
        u = _unit('seele', lc_id='sleep_like_the_dead')
        u.base_stats.CRIT_RATE = 0.20
        u.extra['lc_last_skill_key'] = 'basic_attack'
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['action_ctx'] = 'extra'

        _process_lc_effects(u, state, 'on_self_attack')

        buff = next(b for b in u.buffs if b.source_id == 'sleep_like_the_dead')
        assert buff.remaining_turns == 1

    def test_flames_afar_cooldown_ticks_on_owner_turn_not_hp_loss(self):
        """冷却按持有者回合递减，额外受伤不能加速冷却。"""
        u = _unit('firefly', lc_id='flames_afar')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['lc_last_hp_loss'] = u.max_hp * 0.30

        _process_lc_effects(u, state, 'on_hp_loss')
        for _ in range(4):
            u.current_hp = max(1.0, u.current_hp - u.max_hp * 0.30)
            _process_lc_effects(u, state, 'on_hp_loss')
        assert sum(b.source_id == 'flames_afar' for b in u.buffs) == 1
        assert u.extra.get('flames_afar_cd') == 3

        for _ in range(3):
            _process_lc_effects(u, state, 'on_self_turn_start')
        assert u.extra.get('flames_afar_cd') == 0

    def test_time_waits_heal_record_is_per_holder(self):
        """多个时节不居持有者必须各自记录治疗与每回合触发次数。"""
        u1 = _unit('lingsha', lc_id='时节不居')
        u2 = _unit('lingsha', position=2, lc_id='时节不居')
        enemy = _enemy()
        state = SimState(enemies=[enemy], units=[u1, u2])

        state.extra['lc_last_heal_amt'] = 100.0
        _process_lc_effects(u1, state, 'on_heal')
        state.extra['lc_last_heal_amt'] = 200.0
        _process_lc_effects(u2, state, 'on_heal')

        hp0 = enemy.HP
        _process_lc_effects(u1, state, 'on_self_attack')
        assert hp0 - enemy.HP == pytest.approx(36.0, abs=1e-9)
        _process_lc_effects(u2, state, 'on_self_attack')
        assert hp0 - enemy.HP == pytest.approx(108.0, abs=1e-9)

    def test_attack_mark_applies_to_every_enemy_hit(self):
        """群攻施加的敌方标记必须落在本次所有命中目标上。"""
        u = _unit('fugue', lc_id='life_should_be_cast_to_flames')
        u.char.path = u.lightcone.path  # 保留忘归人全体终结技，仅用于命途匹配
        enemies = [_enemy() for _ in range(3)]
        for i, enemy in enumerate(enemies):
            enemy.id = f'e{i}'
        state = SimState(enemies=enemies, units=[u])

        _use_skill(u, state, 'ultimate')

        assert all(enemy.has_status(status_id='life_flames_def_down') for enemy in enemies)

    def test_we_will_meet_again_only_hits_an_attacked_enemy(self, monkeypatch):
        """后会有期的随机目标范围是本次受击敌人，而不是任意存活敌人。"""
        monkeypatch.setattr('engine.core.combat_sim.random.choice', lambda seq: seq[-1])
        u = _unit('fugue', lc_id='we_will_meet_again')
        enemies = [_enemy(), _enemy()]
        enemies[0].id, enemies[1].id = 'hit', 'not_hit'
        state = SimState(enemies=enemies, units=[u])
        untouched_hp = enemies[1].HP

        _use_skill(u, state, 'basic_attack')

        assert enemies[1].HP == pytest.approx(untouched_hp, abs=1e-9)

    def test_we_will_meet_again_uses_current_attack(self):
        """附加伤害的攻击力基数应包含战斗中的攻击力增益。"""
        u = _unit('fugue', lc_id='we_will_meet_again')
        u.buffs.append(TimedBuff(source_id='atk_buff', attributes={'ATK_percent': 50.0},
                                 remaining_turns=2))
        enemy = _enemy()
        state = SimState(enemies=[enemy], units=[u])
        state.extra['lc_attack_target_refs'] = [enemy]
        expected = _build_effective_stats(u, state).ATK * 0.48
        hp0 = enemy.HP

        _process_lc_effects(u, state, 'on_basic_attack')

        assert hp0 - enemy.HP == pytest.approx(expected, abs=1e-9)

    def test_we_will_meet_again_does_not_jump_from_dead_target(self):
        """本次唯一受击目标已死亡时，附加伤害不能跳到未受击的存活敌人。"""
        u = _unit('fugue', lc_id='we_will_meet_again')
        dead = _enemy(hp=0)
        untouched = _enemy()
        dead.id, untouched.id = 'dead', 'untouched'
        state = SimState(enemies=[dead, untouched], units=[u])
        state.extra['lc_attack_target_refs'] = [dead]
        hp0 = untouched.HP

        _process_lc_effects(u, state, 'on_basic_attack')

        assert untouched.HP == pytest.approx(hp0, abs=1e-9)
