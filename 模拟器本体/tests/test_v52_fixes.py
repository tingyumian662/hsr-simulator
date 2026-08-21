"""v5.2: CODEX 审查 7 问题修复回归测试（S1-S2 组）"""
import copy
import random
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, RelicPiece, RelicSet, RelicSetEffect
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, simulate, _apply_team_static_relics, _apply_hit,
)


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


class TestFuxuanE6Broadcast:
    """问题2: 符玄E6 队友掉血广播路径必须累计（原 guard 误判恒失效）"""

    def test_ally_hp_loss_accumulates(self):
        """队友受击 → 符玄E6 累计"""
        from engine.core.effect_resolver import _eid_fuxuan_e6_loss
        fu = _unit('fu_xuan', position=1)
        fu.eidolon_rank = 6
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[fu, ally])
        state.extra['fuxuan_field_turns'] = 3
        # 模拟 trigger_all 广播: u=事件主体(队友)
        _eid_fuxuan_e6_loss(u=ally, state=state, total_lost=500,
                            affected=[(ally, 500)])
        assert state.extra.get('fuxuan_lost_hp_total', 0.0) == pytest.approx(500.0, abs=1e-6)

    def test_ally_hit_through_apply_hit(self):
        """黑盒: 队友受击（on_hp_loss 广播, u=队友）→ 符玄E6 累计"""
        from engine.core.effect_resolver import _eid_fuxuan_e6_loss
        fu = _unit('fu_xuan', position=1)
        fu.eidolon_rank = 6
        ally = _unit('seele', position=2)
        ally.current_hp = 1000
        state = SimState(enemies=[_enemy()], units=[fu, ally])
        state.extra['fuxuan_field_turns'] = 3
        state.hooks.register('fu_xuan', 'on_hp_loss', _eid_fuxuan_e6_loss)
        _apply_hit(state, ally, 300, state.enemies[0])
        assert state.extra.get('fuxuan_lost_hp_total', 0.0) == pytest.approx(300.0, abs=1e-6)


class TestOptimizerSpdSign:
    """问题5: 速度边际收益符号修正（原式恒负, 速度词条永不推荐）"""

    def test_spd_marginal_positive(self):
        from engine.core.relic_optimizer import _marginal_benefit, CharProfile
        c = load_character('seele', 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        # v5.5 离散行动次数模型: 132→135 跨 134 边界（150×spd/10000 取整 +1 行动）
        stats.SPD = 132.0
        profile = CharProfile(role='dps', primary_stat='ATK',
                              scaling_stats={'ATK', 'HP', 'DEF'})
        gain = _marginal_benefit(stats, c, profile, 'SPD_percent', 3.0)  # 内部分配 key 小写p
        assert gain > 0  # 跨边界: 加 3 点速度多一次行动, 正收益


class TestTeamStaticRelics:
    """问题3b/3d: 仙舟/龙骨全队传播 + 坠星同阵营CD"""

    def _unit_with_relics(self, cid, conds, position=1):
        u = _unit(cid, position=position)
        u._active_relic_conditions = set(conds)
        return u

    def test_xianzhou_team_atk(self):
        """仙舟2pc: SPD≥120 → 全队ATK+8%（排除佩戴者自身, 静态层已给）"""
        holder = self._unit_with_relics('seele', ['spd_threshold_120_team_atk'])
        holder.base_stats.SPD = 130.0
        ally = _unit('seele', position=2)
        atk0 = ally.base_stats.ATK
        state = SimState(enemies=[_enemy()], units=[holder, ally])
        _apply_team_static_relics(state)
        assert ally.base_stats.ATK == pytest.approx(
            atk0 + ally.base_stats._base_ATK * 0.08, abs=1e-6)

    def test_longgu_team_cd(self):
        """龙骨2pc: EFFECT_RES≥30% → 全队CD+10%"""
        holder = self._unit_with_relics('seele', ['effect_res_30_team_cd'])
        holder.base_stats.EFFECT_RES = 0.40
        ally = _unit('seele', position=2)
        cd0 = ally.base_stats.CRIT_DMG
        state = SimState(enemies=[_enemy()], units=[holder, ally])
        _apply_team_static_relics(state)
        assert ally.base_stats.CRIT_DMG == pytest.approx(cd0 + 0.10, abs=1e-9)

    def test_zhuixing_team_cd(self):
        """坠星2pc: 有队友 → 佩戴者CD+32%"""
        holder = self._unit_with_relics('seele', ['enter_combat_faction_cd'])
        ally = _unit('seele', position=2)
        cd0 = holder.base_stats.CRIT_DMG
        state = SimState(enemies=[_enemy()], units=[holder, ally])
        _apply_team_static_relics(state)
        assert holder.base_stats.CRIT_DMG == pytest.approx(cd0 + 0.32, abs=1e-9)


class TestNoConfigPollution:
    """问题1: 模拟不污染输入配置（v5.2 重建: deepcopy + runtime 字段）"""

    def test_memsprite_config_unchanged_after_sim(self):
        """遐蝶模拟后 memsprite 配置不变（CODEX 复现: base_SPD 0→35.7）"""
        char = load_character('xiadie', 'data/characters')
        before = copy.deepcopy(char.memsprite)
        chars = [{'char': char, 'position': 1}]
        simulate(chars, _enemy(), max_av=200)
        assert char.memsprite.base_SPD == before.base_SPD
        assert char.memsprite.is_backup == before.is_backup

    def test_aglaea_config_unchanged_after_sim(self):
        """阿格莱雅模拟后衣匠速度配置不变（v5.1 曾写 data.base_SPD）"""
        char = load_character('aglaea', 'data/characters')
        before = copy.deepcopy(char.memsprite)
        chars = [{'char': char, 'position': 1, 'initial_energy_pct': 100}]
        simulate(chars, _enemy(), max_av=300)
        assert char.memsprite.base_SPD == before.base_SPD

    def test_sequential_sim_consistent(self):
        """同一配置连续两次模拟结果一致"""
        chars = [{'char': load_character('aglaea', 'data/characters'),
                  'position': 1, 'initial_energy_pct': 100}]
        s1 = simulate(copy.deepcopy(chars), _enemy(), max_av=300)
        s2 = simulate(copy.deepcopy(chars), _enemy(), max_av=300)
        assert len(s1.log) == len(s2.log)


class TestDiffMachine:
    """问题3c: 星体差分机首击暴击, 首次攻击后移除"""

    def test_first_attack_consumes_bonus(self):
        from engine.core.combat_sim import _use_skill
        u = _unit('seele')
        u._active_relic_conditions = {'cd_threshold_first_atk_cr'}
        u.base_stats.CRIT_DMG = 1.30
        # 静态层已施加 CR+60%（0.30→0.90 上限截断前）; 初始化记录实际加成量
        u.base_stats.CRIT_RATE = 0.90
        u.extra['diff_machine_cr'] = min(1.0, 0.90) - 0.30
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'basic_attack')
        # 首次攻击后加成被消耗, CR 回到基础
        assert '首击暴击加成已消耗' in '\n'.join(state.log)
        assert u.base_stats.CRIT_RATE == pytest.approx(0.30, abs=1e-9)
        assert 'diff_machine_cr' not in u.extra


class TestFanxingQuantum:
    """问题3e: 繁星4pc 量子弱点目标额外10%无视防御"""

    def test_quantum_weakness_extra_defpen(self):
        from engine.core.combat_sim import _apply_target_relic_modifiers
        u = _unit('seele')
        u._active_relic_conditions = {'defpen_vs_quantum'}
        q_weak = _enemy()
        q_weak.element_res = {**q_weak.element_res, '量子': 0.0}  # 量子弱点
        q_res = _enemy()
        q_res.element_res = {**q_res.element_res, '量子': 0.20}  # 无量子弱点
        s1 = _apply_target_relic_modifiers(u.base_stats, u, q_weak)
        s2 = _apply_target_relic_modifiers(u.base_stats, u, q_res)
        assert s1.DEF_PEN == pytest.approx(u.base_stats.DEF_PEN + 0.10, abs=1e-9)
        assert s2.DEF_PEN == pytest.approx(u.base_stats.DEF_PEN, abs=1e-9)


class TestRelicEventSemantics:
    """问题3a: 遗器事件语义——普通技能不得触发, 正确事件才触发"""

    def test_big_duke_basic_attack_no_stack(self):
        """大公4pc: 普通普攻不得叠层（原 bug: on_after_skill 误触发）"""
        from engine.core.combat_sim import _use_skill
        u = _unit('seele')
        u._active_relic_conditions = {'stack_atk_on_fua'}
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'basic_attack')
        assert (u.relic_stacks or {}).get('大公', 0) == 0

    def test_big_duke_followup_stacks(self):
        """大公4pc: 追加攻击(FUA)叠层"""
        from engine.core.relic_conditions import _stack_atk_on_fua_hit
        u = _unit('seele')
        u._active_relic_conditions = {'stack_atk_on_fua'}
        state = SimState(enemies=[_enemy()], units=[u])
        _stack_atk_on_fua_hit(u, state)
        assert (u.relic_stacks or {}).get('大公', 0) == 1

    def test_qianxing_followup_buff(self):
        """千星2pc: 追加攻击后 ATK+24% buff"""
        from engine.core.relic_conditions import _on_fua_atk_buff
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        _on_fua_atk_buff(u, state)
        assert any(getattr(b, 'source_name', '') == '千星2pc' for b in u.buffs)

    def test_yinghao_memsprite_cd(self):
        """英豪4pc: 忆灵攻击后 CD+30% buff（on_memsprite_attack 触发）"""
        from engine.core.relic_conditions import _on_memosprite_atk_cd
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        _on_memosprite_atk_cd(u, state)
        assert any(getattr(b, 'source_name', '') == '英豪4pc' for b in u.buffs)

    def test_duolan_merit_holder_locates_by_char_id(self):
        """都蓝王朝: 广播事件中按 char_id 定位持有者叠层（u=执行者）"""
        from engine.core.relic_conditions import _stack_merit_on_fua
        holder = _unit('seele', position=1)
        executor = _unit('bronya', position=2)  # 执行追加的队友
        state = SimState(enemies=[_enemy()], units=[holder, executor])
        _stack_merit_on_fua(u=executor, state=state, char_id='seele')
        assert (holder.relic_stacks or {}).get('Merit', 0) == 1


class TestExpectedCrit:
    """问题4: 期望暴击模式（1+CR×CD, 消除 50% 阈值断层）"""

    def test_expected_crit_formula(self):
        """期望模式: 伤害 = 白值段 × (1+min(CR,1)×CD)"""
        from engine.core.damage import calculate_damage
        from engine.core.attributes import CombatStats
        stats = CombatStats(ATK=1000.0, CRIT_RATE=0.30, CRIT_DMG=1.50)
        t = _enemy()
        d = calculate_damage(stats, t, 1000.0, 100.0, "direct", "物理", 80,
                             is_crit=False, crit_mode="expected")
        assert d.crit_mult == pytest.approx(1.0 + 0.30 * 1.50, rel=1e-9)
        # 无断层: CR 0.49 与 0.51 的期望伤害连续
        s1 = CombatStats(ATK=1000.0, CRIT_RATE=0.49, CRIT_DMG=1.50)
        s2 = CombatStats(ATK=1000.0, CRIT_RATE=0.51, CRIT_DMG=1.50)
        d1 = calculate_damage(s1, t, 1000.0, 100.0, "direct", "物理", 80,
                              is_crit=False, crit_mode="expected")
        d2 = calculate_damage(s2, t, 1000.0, 100.0, "direct", "物理", 80,
                              is_crit=False, crit_mode="expected")
        assert d2.final_damage / d1.final_damage == pytest.approx(
            (1.0 + 0.51 * 1.50) / (1.0 + 0.49 * 1.50), rel=1e-9)

    def test_seele_zhanjin_expected(self):
        """希儿斩尽: HP≤80% 目标 CR+15% 进入期望公式（不再是阈值跨越）"""
        from engine.core.combat_sim import _use_skill
        u = _unit('seele')
        u.base_stats.CRIT_RATE = 0.35
        state = SimState(enemies=[_enemy()], units=[u])
        t = state.enemies[0]
        t.HP = 1  # HP≤80% → 斩尽生效
        _use_skill(u, state, 'basic_attack')
        # 伤害>0 且斩尽日志出现（CR+15% 进入期望公式）
        assert any('斩尽' in l for l in state.log) or u.total_damage_dealt > 0

    def test_regular_skill_uses_expected_crit(self):
        """常规技能伤害随暴击率连续变化，不能保留 50% 二元阈值。"""
        from engine.core.combat_sim import _use_skill

        def basic_damage(crit_rate):
            unit = _unit('seele')
            unit.base_stats.CRIT_RATE = crit_rate
            unit.base_stats.CRIT_DMG = 1.50
            enemy = _enemy()
            state = SimState(enemies=[enemy], units=[unit])
            state.current_av = 0.0
            _use_skill(unit, state, 'basic_attack')
            return 500000.0 - enemy.HP

        low = basic_damage(0.49)
        high = basic_damage(0.51)
        assert high / low == pytest.approx(
            (1.0 + 0.51 * 1.50) / (1.0 + 0.49 * 1.50), rel=1e-9,
        )

    def test_bounce_skill_uses_expected_crit(self):
        """弹射伤害也使用期望暴击，不得走旧二元判定。"""
        from engine.core.combat_sim import _use_skill

        def bounce_damage(crit_rate):
            unit = _unit('trailblazer_harmony')
            unit.base_stats.CRIT_RATE = crit_rate
            unit.base_stats.CRIT_DMG = 1.50
            enemy = _enemy()
            state = SimState(enemies=[enemy], units=[unit])
            state.current_av = 0.0
            _use_skill(unit, state, 'skill')
            return 500000.0 - enemy.HP

        low = bounce_damage(0.49)
        high = bounce_damage(0.51)
        assert high / low == pytest.approx(
            (1.0 + 0.51 * 1.50) / (1.0 + 0.49 * 1.50), rel=1e-9,
        )

    def test_silver_enhanced_basic_uses_expected_crit(self):
        """银狼强化普攻所有伤害段均按期望暴击连续变化。"""
        from engine.systems.elation import ElationSystem

        def enhanced_basic_damage(crit_rate):
            unit = _unit('yinlang')
            unit.base_stats.CRIT_RATE = crit_rate
            unit.base_stats.CRIT_DMG = 1.50
            state = SimState(enemies=[_enemy()], units=[unit])
            state.elation_state.grant_good_show(unit.char.id, 10)
            rng_state = random.getstate()
            random.seed(7)
            try:
                ElationSystem().silver_enhanced_basic(unit, state)
            finally:
                random.setstate(rng_state)
            return unit.total_damage_dealt

        low = enhanced_basic_damage(0.49)
        high = enhanced_basic_damage(0.51)
        assert high / low == pytest.approx(
            (1.0 + 0.51 * 1.50) / (1.0 + 0.49 * 1.50), rel=1e-9,
        )
