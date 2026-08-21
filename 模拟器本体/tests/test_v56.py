"""v5.6 近似项收窄测试：
S1 叠层独立计时（lc_stacks 分层 + 圣咏每层 TimedBuff）
S2/S3 攻击类别·技能类型作用域（火舞FUA限定 / 流光终结技限定 / 都蓝每层FUA+5%）
S4 船长 Help 事件驱动（被队友选中叠层, 终结技消费）
S5 EHR 检定（enemy.effect_res, 统一 _roll_effect_hit）
S6 语义确认（昔涟进战斗判定一次 / 乌黯死龙在场自动强化战技）
"""
import pytest
import types
from pathlib import Path
from engine.models.character import Character, Skill, SkillEffect, load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, LightConeEffect
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, _build_effective_stats, _lc_tick_stacks, _tick_buffs,
    _process_lc_effects, _use_skill, _apply_skill_effects,
    _pick_single_ally_target, _roll_effect_hit, _lc_grounded_ascent_counter,
    _apply_hit, TimedBuff,
)
from engine.core.damage import calculate_damage, _calc_def_mult
from engine.core.relic_conditions import (
    _stack_merit_on_fua, register_dynamic_relic_effects,
)
from engine.systems.remembrance import RemembranceSystem


def _enemy(effect_res=0.0):
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 effect_res=effect_res,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, lc=None, eidolon_rank=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, lc, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position, eidolon_rank=eidolon_rank)
    u.max_hp = u.current_hp = stats.HP
    u.lightcone = lc
    u.extra.update(extra)
    return u


def _lc(effects, path='巡猎'):
    return LightCone(id='test_lc', name='测试光锥', path=path, effects=effects)


def _gain_effect(event='on_skill', max_n=3, dur=0, op='', target='self'):
    return LightConeEffect(type='trigger_effect', condition='测试触发', attributes={},
                           condition_code=f'stack_gain:{event}:test:{max_n}:{target}:{dur}:{op}')


class TestIndependentLayerTiming:
    """S1: 每层独立计时（用户确认圣咏/龙吟/火舞实机均每层独立）"""

    def test_layers_expire_oldest_first(self):
        """cap=2 dur=2 错开施放: 第一层先到期, 第二层剩余独立保留"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 2, dur=2)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')   # 层1 [2]
        _lc_tick_stacks(state, u)                    # 层1→[1]
        _process_lc_effects(u, state, 'on_skill')   # 层2 → [1,2]
        assert u.lc_stacks['test_lc::test'] == 2
        _lc_tick_stacks(state, u)                    # 层1到期消失, 层2剩1
        assert u.lc_stacks['test_lc::test'] == 1    # 扁平刷新式此处仍为2
        assert u.lc_stack_turns['test_lc::test'] == [1]

    def test_full_stack_replaces_oldest(self):
        """cap=1 满层后再触发 → 替换最旧层（新倒计时）"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 1, dur=2)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        _lc_tick_stacks(state, u)
        assert u.lc_stack_turns['test_lc::test'] == [1]
        _process_lc_effects(u, state, 'on_skill')
        assert u.lc_stack_turns['test_lc::test'] == [2]

    def test_hymn_layers_independent(self):
        """圣咏: 每层独立 TimedBuff, 一层到期其余保留"""
        u = _unit('bronya', lc=_lc([]))
        target = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, target])
        u.extra['lc_last_skill_target_type'] = 'single_ally'
        u.extra['lc_last_skill_target'] = target
        for _ in range(3):
            _lc_grounded_ascent_counter(state, u)
        hymns = [b for b in target.buffs
                 if getattr(b, 'param_id', '') == 'grounded_ascent_hymn']
        assert len(hymns) == 3
        assert _build_effective_stats(target, state).DMG_BONUS_ALL == pytest.approx(0.45)
        # 第1层到期
        hymns[0].remaining_turns = 1
        _tick_buffs(target)
        assert _build_effective_stats(target, state).DMG_BONUS_ALL == pytest.approx(0.30)

    def test_hymn_full_replaces_oldest(self):
        """圣咏满3层后再获得 → 替换最旧层"""
        u = _unit('bronya', lc=_lc([]))
        target = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, target])
        u.extra['lc_last_skill_target_type'] = 'single_ally'
        u.extra['lc_last_skill_target'] = target
        for _ in range(4):
            _lc_grounded_ascent_counter(state, u)
        hymns = [b for b in target.buffs
                 if getattr(b, 'param_id', '') == 'grounded_ascent_hymn']
        assert len(hymns) == 3
        assert all(b.remaining_turns == 3 for b in hymns)  # 最旧层被新层替换


class TestAttackTypeScope:
    """S2/S3: 火舞仅追加攻击增伤 / 流光仅终结技无视防御"""

    def _fire_dance_lc(self):
        return LightCone(id='dance_at_sunset', name='落日时起舞', path='毁灭',
                         effects=[
                             LightConeEffect(type='trigger_effect', condition='',
                                             attributes={},
                                             condition_code='stack_gain:on_ult:huowu:2:self:2:'),
                             LightConeEffect(type='conditional_buff', condition='',
                                             attributes={'DMG_BONUS_ATK_FOLLOW_UP': 72.0},
                                             condition_code='stack:huowu:2'),
                         ])

    def test_fire_dance_fua_only(self):
        """火舞满层: 仅追加攻击+72%, 全局增伤为0; 普攻不吃/FUA吃
        （落日时起舞为毁灭命途光锥, 用毁灭角色流萤使特效生效）"""
        u = _unit('firefly', lc=self._fire_dance_lc())
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_ult')
        _process_lc_effects(u, state, 'on_ult')
        s = _build_effective_stats(u, state)
        assert s.DMG_BONUS_ALL == 0.0
        assert s.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.72, rel=1e-9)
        e = _enemy()
        d_basic = calculate_damage(s, e, s.ATK, 100.0, "direct", "火", 80,
                                   False, crit_mode="expected")
        d_fua = calculate_damage(s, e, s.ATK, 100.0, "direct", "火", 80,
                                 False, crit_mode="expected", attack_type="follow_up")
        assert d_fua.dmg_bonus_mult == pytest.approx(d_basic.dmg_bonus_mult + 0.72, rel=1e-9)

    def _liuguang_lc(self):
        return LightCone(id='i_venture_forth_to_hunt', name='我将，巡征追猎', path='巡猎',
                         effects=[
                             LightConeEffect(type='trigger_effect', condition='',
                                             attributes={},
                                             condition_code='stack_gain:on_followup:liuguang:2:self:0:turn_end:-1'),
                             LightConeEffect(type='conditional_buff', condition='',
                                             attributes={'DEF_PEN_SKILL_ULTIMATE': 54.0},
                                             condition_code='stack:liuguang:2'),
                         ])

    def test_liuguang_ult_only_def_pen(self):
        """流光满层: 仅终结技无视54%防御, 战技/普攻不生效"""
        u = _unit('seele', lc=self._liuguang_lc())
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_followup')
        _process_lc_effects(u, state, 'on_followup')
        s = _build_effective_stats(u, state)
        assert s.DEF_PEN == 0.0
        assert s.DEF_PEN_BY_SKILL_TYPE['ultimate'] == pytest.approx(0.54, rel=1e-9)
        assert s.get_total_def_reduction(skill_type='ultimate') == pytest.approx(0.54, rel=1e-9)
        assert s.get_total_def_reduction(skill_type='skill') == 0.0
        e = _enemy()
        m_ult = _calc_def_mult(e, s, 80, damage_type='direct', skill_type='ultimate')
        m_skill = _calc_def_mult(e, s, 80, damage_type='direct', skill_type='skill')
        # def_mult 越接近1 = 无视防御越多 → 终结技更高（54%无视生效）
        assert m_ult > m_skill


class TestDuranMerit:
    """S3: 都蓝王朝每层FUA+5%, 5层FUA暴伤+25%（FUA限定, 不掉层时对称保留）"""

    def test_merit_per_layer_scales(self):
        holder = _unit('seele', position=2)
        bronya = _unit('bronya', position=1)
        state = SimState(enemies=[_enemy()], units=[bronya, holder])
        _stack_merit_on_fua(bronya, state, char_id='seele')
        assert holder.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.05)
        _stack_merit_on_fua(bronya, state, char_id='seele')
        assert holder.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.10)

    def test_merit_full_cap_fua_cd_only(self):
        holder = _unit('seele', position=2)
        bronya = _unit('bronya', position=1)
        state = SimState(enemies=[_enemy()], units=[bronya, holder])
        base_cd = holder.base_stats.CRIT_DMG
        for _ in range(5):
            _stack_merit_on_fua(bronya, state, char_id='seele')
        assert holder.relic_stacks['Merit'] == 5
        assert holder.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.25)
        assert holder.base_stats.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.25)
        assert holder.base_stats.CRIT_DMG == base_cd  # 全局暴伤未被污染（旧实现会+0.25）
        # 满层后再触发: 层数不变, 加成幂等
        _stack_merit_on_fua(bronya, state, char_id='seele')
        assert holder.base_stats.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.25)

    def test_holder_own_fua_counts(self):
        """实机文本'我方角色'(ally) 含装备者自身 → 自身FUA也叠层"""
        holder = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[holder])
        _stack_merit_on_fua(holder, state, char_id='seele')
        assert holder.relic_stacks['Merit'] == 1

    def test_e4_followup_broadcast_triggers_duran(self):
        """布洛妮娅E4追加攻击广播 on_followup → 都蓝持有者叠层（FUA调用点接线）"""
        holder = _unit('bronya', position=1)
        seele = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[seele, holder])
        register_dynamic_relic_effects(state.hooks, 'bronya', 'stack_merit_on_fua')
        from engine.core.effect_resolver import _eid_bronya_e4
        _eid_bronya_e4(seele, state, target=state.enemies[0], skill_key='basic_attack')
        assert holder.relic_stacks.get('Merit', 0) == 1
        assert holder.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.05)


class TestCaptainHelp:
    """S4: 船长·Help 事件驱动（被队友单体技能选中叠层, 终结技消费）"""

    def _captain_state(self):
        captain = _unit('seele', position=2)
        captain.extra['single_ally_priority'] = 1
        caster = _unit('bronya', position=1)
        state = SimState(enemies=[_enemy()], units=[caster, captain])
        register_dynamic_relic_effects(state.hooks, 'seele',
                                       'help_stack_gain+help_stack_consume')
        return captain, caster, state

    def test_help_gain_when_targeted_by_ally_skill(self):
        captain, caster, state = self._captain_state()
        _apply_skill_effects(caster, state, caster.char.skills['skill'], 'skill')
        assert captain.relic_stacks.get('Help', 0) == 1

    def test_help_gain_when_targeted_by_single_target_heal(self):
        """单体治疗也属于“被队友选中的技能”，不依赖技能带 buff 效果。"""
        captain = _unit('seele', position=2)
        captain.extra['single_ally_priority'] = 1
        caster = _unit('huohuo', position=1)
        state = SimState(enemies=[_enemy()], units=[caster, captain])
        register_dynamic_relic_effects(state.hooks, 'seele',
                                       'help_stack_gain+help_stack_consume')

        _apply_skill_effects(caster, state, caster.char.skills['skill'], 'skill')

        assert captain.relic_stacks.get('Help', 0) == 1

    def test_help_no_gain_when_caster_targets_self(self):
        caster = _unit('bronya', position=1)
        caster.extra['single_ally_priority'] = 1  # 施放者自己带船长
        state = SimState(enemies=[_enemy()], units=[caster])
        register_dynamic_relic_effects(state.hooks, 'bronya',
                                       'help_stack_gain+help_stack_consume')
        _apply_skill_effects(caster, state, caster.char.skills['skill'], 'skill')
        assert caster.relic_stacks.get('Help', 0) == 0  # "another ally" 语义

    def test_help_consume_on_ult(self):
        captain, caster, state = self._captain_state()
        captain.relic_stacks['Help'] = 2
        _use_skill(captain, state, 'ultimate')
        assert captain.relic_stacks.get('Help', 0) == 0
        assert any(b.source_name == '船长·Help消费' for b in captain.buffs)

    def test_pick_single_ally_target_prefers_captain(self):
        caster = _unit('bronya', position=1)
        captain = _unit('seele', position=2)
        captain.extra['single_ally_priority'] = 1
        other = _unit('xiadie', position=3)
        state = SimState(enemies=[_enemy()], units=[caster, captain, other])
        assert _pick_single_ally_target(state, caster) is captain


class TestEffectHitRoll:
    """S5: 玩家→敌方 debuff 命中检定（enemy.effect_res 默认0=必中, 数据驱动）"""

    def test_effect_res_default_zero_no_block(self):
        """effect_res=0（现有数据）: 100%基础概率效果必中, 不依赖随机"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        assert _roll_effect_hit(u, state, state.enemies[0], '测试') is True

    def test_resistance_reduces_chance(self, monkeypatch):
        u = _unit('seele')
        e = _enemy(effect_res=0.5)
        state = SimState(enemies=[e], units=[u])
        rolls = iter([0.8, 0.2])
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: next(rolls))
        assert _roll_effect_hit(u, state, e, '测试') is False   # 命中线0.5, roll 0.8 → 抵抗
        assert _roll_effect_hit(u, state, e, '测试') is True    # roll 0.2 → 命中

    def test_skill_debuff_blocked_by_enemy_res(self, monkeypatch):
        char = Character(id='caster', name='Caster', element='虚数', path='虚无')
        unit = SimUnit(char=char, base_stats=compute_combat_stats(char), position=1)
        e = _enemy(effect_res=0.5)  # 50%抵抗 → 命中线0.5
        state = SimState(enemies=[e], units=[unit])
        skill = Skill(name='Debuff', type='skill', target='all_enemies', effects=[
            SkillEffect(type='debuff', target='all_enemies', value=16, param_id='凶星低语')])
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.99)
        _apply_skill_effects(unit, state, skill, 'skill')
        assert not e.has_status(status_id='凶星低语')
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.01)
        _apply_skill_effects(unit, state, skill, 'skill')
        assert e.has_status(status_id='凶星低语')

    def test_all_resisted_debuff_does_not_trigger_success_hooks(self, monkeypatch):
        """全部目标抵抗时不得伪装成成功施加减益。"""
        char = Character(id='caster', name='Caster', element='虚数', path='虚无')
        unit = SimUnit(char=char, base_stats=compute_combat_stats(char), position=1)
        unit._active_relic_conditions = {'cd_per_debuff_count'}
        enemy = _enemy(effect_res=0.5)
        state = SimState(enemies=[enemy], units=[unit])
        skill = Skill(name='Debuff', type='skill', target='all_enemies', effects=[
            SkillEffect(type='debuff', target='all_enemies', value=16, param_id='凶星低语')])
        events = []
        monkeypatch.setattr('engine.core.combat_sim._process_lc_effects',
                            lambda *args: events.append(args[2]))
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: 0.99)

        _apply_skill_effects(unit, state, skill, 'skill')

        assert not enemy.has_status(status_id='凶星低语')
        assert 'pioneer_double_pending' not in unit.extra
        assert 'on_debuff_apply' not in events

    def test_partial_debuff_hit_triggers_success_hook_once(self, monkeypatch):
        """部分目标命中时只按实际命中目标触发一次成功副作用。"""
        char = Character(id='caster', name='Caster', element='虚数', path='虚无')
        unit = SimUnit(char=char, base_stats=compute_combat_stats(char), position=1)
        unit._active_relic_conditions = {'cd_per_debuff_count'}
        enemies = [_enemy(effect_res=0.5), _enemy(effect_res=0.5)]
        state = SimState(enemies=enemies, units=[unit])
        skill = Skill(name='Debuff', type='skill', target='all_enemies', effects=[
            SkillEffect(type='debuff', target='all_enemies', value=16, param_id='凶星低语')])
        rolls = iter([0.01, 0.99])
        events = []
        monkeypatch.setattr('engine.core.combat_sim._process_lc_effects',
                            lambda *args: events.append(args[2]))
        monkeypatch.setattr('engine.core.combat_sim.random.random', lambda: next(rolls))

        _apply_skill_effects(unit, state, skill, 'skill')

        assert enemies[0].has_status(status_id='凶星低语')
        assert not enemies[1].has_status(status_id='凶星低语')
        assert unit.extra['pioneer_double_pending'] is True
        assert events.count('on_debuff_apply') == 1


def test_frontend_sends_effect_resistance_as_fraction():
    """敌方效果抵抗输入应以百分比展示并转换为 API 所需的小数（v6.5: 逐只敌人卡片, id 带索引）。"""
    html = Path('web/templates/index.html').read_text(encoding='utf-8')
    assert 'value="${(defaults.effect_res??0)*100}"' in html  # 百分比展示
    assert "effect_res: (parseFloat(document.getElementById(`enemy-res-${idx}`).value) || 0) / 100" in html


class TestSemanticConfirm:
    """S6: 实机语义确认（用户确认）"""

    def test_xilian_trace1_speed_threshold(self):
        """昔涟: 进战斗面板SPD≥180→全队+20%+冰穿透（进战斗判定一次, 面板含遗器）"""
        from engine.core.effect_resolver import _xilian_trace1_speed_pen
        u = _unit('xilian')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        u.base_stats.SPD = 179.0
        _xilian_trace1_speed_pen(u, state)
        assert ally.base_stats.DMG_BONUS_ALL == 0.0
        u.base_stats.SPD = 190.0
        _xilian_trace1_speed_pen(u, state)
        assert ally.base_stats.DMG_BONUS_ALL == pytest.approx(0.20)
        assert u.base_stats.RES_PEN['冰'] == pytest.approx(0.20)  # 超10点→20%穿透

    def test_xiadie_dragon_form_uses_enhanced_skill(self, monkeypatch):
        """乌黯: 死龙在场→AI自动施放强化战技（实机战技自动替换, AI层已接线）"""
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        u.memsprite_unit = types.SimpleNamespace(is_alive=True)
        from engine.core import combat_sim
        used = []
        monkeypatch.setattr(combat_sim, '_use_skill',
                            lambda *a, **kw: used.append(a[2]))
        RemembranceSystem().xiadie_ai(u, state)
        assert used == ['skill_dragon']
        u.memsprite_unit = None
        RemembranceSystem().xiadie_ai(u, state)
        assert used == ['skill_dragon', 'skill']


class TestV561RemembranceRegressions:
    """v5.6.1: 忆灵行动、长夜月天赋和风堇天赋的回归覆盖。"""

    def _summon(self, state, unit):
        return RemembranceSystem().summon_memsprite(state, unit, unit.char.memsprite)

    def test_memsprite_buffs_expire_on_its_regular_turns(self):
        owner = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[owner])
        ms = self._summon(state, owner)
        ms.buffs.append(TimedBuff('test', {'DMG_BONUS_ALL': 80.0}, 2,
                                  param_id='test_memsprite_duration'))

        rem = RemembranceSystem()
        rem.handle_memsprite_action(state, ms)
        assert [b.remaining_turns for b in ms.buffs
                if b.param_id == 'test_memsprite_duration'] == [1]
        assert any(b.param_id == 'changyeyue_night_abyss' and b.remaining_turns == -1
                   for b in ms.buffs)
        rem.handle_memsprite_action(state, ms)
        assert not any(b.param_id == 'test_memsprite_duration' for b in ms.buffs)
        assert any(b.param_id == 'changyeyue_night_abyss' and b.remaining_turns == -1
                   for b in ms.buffs)

    def test_enemy_hit_on_changyeyue_memsprite_dispatches_talent(self):
        owner = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[owner])
        ms = self._summon(state, owner)
        yizhi_before = owner.yizhi

        _apply_hit(state, ms, 100.0, state.enemies[0])

        assert owner.yizhi == yizhi_before + 2
        assert any(b.param_id == 'changyeyue_talent_cd' for b in owner.buffs)
        assert any(b.param_id == 'changyeyue_talent_cd' for b in ms.buffs)

    def test_fully_absorbed_hit_does_not_dispatch_changyeyue_talent(self):
        owner = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[owner])
        ms = self._summon(state, owner)
        ms.shield = 100.0
        yizhi_before = owner.yizhi

        _apply_hit(state, ms, 100.0, state.enemies[0])

        assert owner.yizhi == yizhi_before
        # v5.7: 召唤即挂「孤独浮游漆黑」永久buff, 断言改为"无天赋暴伤buff"
        assert not any(getattr(b, 'param_id', '') == 'changyeyue_talent_cd'
                       for b in owner.buffs)
        assert not any(getattr(b, 'param_id', '') == 'changyeyue_talent_cd'
                       for b in ms.buffs)

    def test_changyeyue_e2_applies_to_any_memsprite_skill_yizhi(self):
        changyeyue = _unit('changyeyue', eidolon_rank=2)
        fengjin = _unit('fengjin', position=2)
        state = SimState(enemies=[_enemy()], units=[changyeyue, fengjin])
        ms = self._summon(state, fengjin)
        yizhi_before = changyeyue.yizhi

        RemembranceSystem()._use_memsprite_skill(state, fengjin, ms, 'memsprite_basic')

        assert changyeyue.yizhi == yizhi_before + 3

    def test_memsprite_def_pen_preserves_timed_effective_stats(self, monkeypatch):
        owner = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[owner])
        ms = self._summon(state, owner)
        ms.buffs.append(TimedBuff('test', {'DMG_BONUS_ALL': 80.0}, 2,
                                  param_id='test_memsprite_damage'))
        seen_stats = []

        def capture_damage(stats, *_args, **_kwargs):
            seen_stats.append(stats)
            return types.SimpleNamespace(final_damage=1.0)

        monkeypatch.setattr('engine.systems.remembrance.calculate_damage', capture_damage)
        monkeypatch.setattr('engine.systems.remembrance._team_memsprite_def_pen',
                            lambda _state: 0.20)
        RemembranceSystem()._use_memsprite_skill(state, owner, ms, 'memsprite_basic')

        assert seen_stats[0].DMG_BONUS_ALL == pytest.approx(0.80 + 0.50)  # v5.7: 含孤独浮游+50%
        assert seen_stats[0].DEF_PEN == pytest.approx(0.20)

    def test_fengjin_regular_turn_talent_heal_adds_memsprite_damage_layer(self):
        # v6.2.1: 入队不再治疗（双份治疗修复）——X轴执行时治疗并叠疗愈层
        fengjin = _unit('fengjin')
        fengjin.extra['clear_sky_turns'] = 1
        state = SimState(enemies=[_enemy()], units=[fengjin])
        ms = self._summon(state, fengjin)
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem

        rem._fengjin_extra_turn(state, fengjin)
        # 入队阶段: 只排队不治疗
        assert len([b for b in ms.buffs if b.param_id == 'fengjin_talent_dmg']) == 0
        assert any(x is ms for x, k in state.extra.get('extra_turns', []))

        # X轴执行: 治疗一次→疗愈层+1
        from engine.core.combat_sim import _fengjin_talent_heal_buff
        _fengjin_talent_heal_buff(state, fengjin)
        layers = [b for b in ms.buffs if b.param_id == 'fengjin_talent_dmg']
        assert len(layers) == 1
        assert layers[0].remaining_turns == 2
