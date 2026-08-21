"""v5.0.1: 光锥叠层/标记系统测试（叠层叠加/上限/衰减/标记开关/计数型）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, LightConeEffect
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, _build_effective_stats, _lc_maybe_gain_stack,
    _lc_tick_stacks, _process_lc_effects, _use_skill, _lc_target_correct,
)


def _enemy(hp=500000, count=1):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, lc=None, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, lc, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.lightcone = lc
    u.extra.update(extra)
    return u


def _lc(effects, path='巡猎'):
    return LightCone(id='test_lc', name='测试光锥', path=path, effects=effects)


def _stack_panel(max_n, attrs):
    return LightConeEffect(type='conditional_buff', condition='测试叠层',
                           attributes=attrs, condition_code=f'stack:test:{max_n}')


def _gain_effect(event='on_skill', max_n=3, dur=0, op='', target='self'):
    return LightConeEffect(type='trigger_effect', condition='测试触发',
                           attributes={},
                           condition_code=f'stack_gain:{event}:test:{max_n}:{target}:{dur}:{op}')


class TestStackScaling:
    def _panel(self, count, max_n=3, attrs=None):
        u = _unit('seele', lc=_lc([_stack_panel(max_n, attrs or {'CRIT_DMG': 60.0})]))
        if count:
            u.lc_stacks[f'test_lc::test'] = count
        state = SimState(enemies=[_enemy()], units=[u])
        return _build_effective_stats(u, state), u

    def test_zero_stack_full_negated(self):
        """零层: 满层总量 60 全量抵消（面板=白值）"""
        s, u = self._panel(0)
        assert s.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG - 0.60, rel=1e-9)

    def test_partial_stack_scaled(self):
        """1 层/3: 抵消 2/3; 2 层/3: 抵消 1/3"""
        s1, u1 = self._panel(1)
        assert s1.CRIT_DMG == pytest.approx(u1.base_stats.CRIT_DMG - 0.40, rel=1e-9)
        s2, u2 = self._panel(2)
        assert s2.CRIT_DMG == pytest.approx(u2.base_stats.CRIT_DMG - 0.20, rel=1e-9)

    def test_full_stack_kept(self):
        """满层: 保留全部"""
        s, u = self._panel(3)
        assert s.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG, rel=1e-9)


class TestGain:
    def test_gain_and_cap(self):
        """事件触发 +1 层, 上限截断"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 3)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        _process_lc_effects(u, state, 'on_skill')
        _process_lc_effects(u, state, 'on_skill')
        _process_lc_effects(u, state, 'on_skill')  # 超过上限
        assert u.lc_stacks.get('test_lc::test') == 3

    def test_event_mismatch_no_gain(self):
        """事件不匹配不叠层"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 3)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_ult')
        assert 'test_lc::test' not in u.lc_stacks

    def test_duration_expires(self):
        """duration=1: 回合结束 tick 后清零"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 3, dur=1)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        assert u.lc_stacks.get('test_lc::test') == 1
        _lc_tick_stacks(state, u)
        assert 'test_lc::test' not in u.lc_stacks

    def test_max_stack_refreshes_duration(self):
        """v5.6 分层计时: cap=1 时满层再次触发 → 替换最旧层（新倒计时）"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 1, dur=2)]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        _lc_tick_stacks(state, u)
        assert u.lc_stack_turns['test_lc::test'] == [1]
        _process_lc_effects(u, state, 'on_skill')
        assert u.lc_stack_turns['test_lc::test'] == [2]
        _lc_tick_stacks(state, u)
        assert u.lc_stacks['test_lc::test'] == 1

    def test_turn_end_decay(self):
        """turn_end:-1: 回合结束掉 1 层"""
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 3, op='turn_end:-1')]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        _process_lc_effects(u, state, 'on_skill')
        assert u.lc_stacks.get('test_lc::test') == 2
        _lc_tick_stacks(state, u)
        assert u.lc_stacks.get('test_lc::test') == 1

    def test_clear_on_attack(self):
        """clear_on:attack: 攻击后清零再叠（per_target）"""
        u = _unit('seele', lc=_lc([_gain_effect('on_self_attack', 5,
                                                op='per_target:clear_on:attack')]))
        state = SimState(enemies=[_enemy(), _enemy()], units=[u])
        state.extra['lc_attack_targets'] = 2
        _process_lc_effects(u, state, 'on_self_attack')
        assert u.lc_stacks.get('test_lc::test') == 2
        state.extra['lc_attack_targets'] = 1
        _process_lc_effects(u, state, 'on_self_attack')  # 先清再叠
        assert u.lc_stacks.get('test_lc::test') == 1


class TestMark:
    def test_mark_toggle(self):
        """标记(max=1): 无标记全抵, 获得后保留"""
        u = _unit('seele', lc=_lc([_gain_effect('on_ult', 1),
                                   _stack_panel(1, {'CRIT_DMG': 30.0})]))
        state = SimState(enemies=[_enemy()], units=[u])
        s0 = _build_effective_stats(u, state)
        assert s0.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG - 0.30, rel=1e-9)
        _process_lc_effects(u, state, 'on_ult')
        s1 = _build_effective_stats(u, state)
        assert s1.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG, rel=1e-9)

    def test_mark_clear_on_turn_start(self):
        """clear_on:self_turn_start: 自身回合开始移除标记"""
        u = _unit('seele', lc=_lc([_gain_effect('on_ult', 1, op='clear_on:self_turn_start')]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_ult')
        assert 'test_lc::test' in u.lc_stacks
        _process_lc_effects(u, state, 'on_self_turn_start')
        assert 'test_lc::test' not in u.lc_stacks

    def test_remove_code(self):
        """stack_remove: 事件移除标记"""
        remove = LightConeEffect(type='trigger_effect', condition='移除',
                                 attributes={}, condition_code='stack_remove:on_ult:test:self')
        u = _unit('seele', lc=_lc([_gain_effect('on_skill', 1), remove]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_skill')
        _process_lc_effects(u, state, 'on_ult')
        assert 'test_lc::test' not in u.lc_stacks


class TestCount:
    def test_enemies_alive(self):
        """计数: 场上敌人数"""
        cnt = LightConeEffect(type='conditional_buff', condition='敌人数',
                              attributes={'ATK_PERCENT': 45.0},
                              condition_code='count:enemies_alive:5')
        u = _unit('seele', lc=_lc([cnt]))
        state1 = SimState(enemies=[_enemy()], units=[u])
        s1 = _build_effective_stats(u, state1)
        assert s1.ATK == pytest.approx(u.base_stats.ATK - u.base_stats._base_ATK * 0.36, rel=1e-9)  # 1/5 保留
        state5 = SimState(enemies=[_enemy()] * 5, units=[u])
        s5 = _build_effective_stats(u, state5)
        assert s5.ATK == pytest.approx(u.base_stats.ATK, rel=1e-9)  # 5/5 满

    def test_target_debuffs(self):
        """计数: 目标负面数（逐目标重评）"""
        cnt = LightConeEffect(type='conditional_buff', condition='负面数',
                              attributes={'CRIT_DMG': 24.0},
                              condition_code='count:target_debuffs:3')
        u = _unit('seele', lc=_lc([cnt]))
        from engine.models.enemy import EnemyStatus
        clean = _enemy()
        debuffed = _enemy()
        debuffed.add_status(EnemyStatus(id='d1', name='易伤', category='debuff',
                                        remaining_turns=2))
        debuffed.add_status(EnemyStatus(id='d2', name='减速', category='debuff',
                                        remaining_turns=2))
        state = SimState(enemies=[clean, debuffed], units=[u])
        s_clean = _lc_target_correct(u.base_stats, u, state, clean)
        s_debuffed = _lc_target_correct(u.base_stats, u, state, debuffed)
        assert s_clean.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG - 0.24, rel=1e-9)
        assert s_debuffed.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG - 0.08, rel=1e-9)  # 2/3

    def test_target_debuffs_are_corrected_once_in_damage_path(self):
        """目标负面数在无目标面板阶段不可预先抵消。"""
        cnt = LightConeEffect(type='conditional_buff', condition='负面数',
                              attributes={'CRIT_DMG': 24.0},
                              condition_code='count:target_debuffs:3')
        u = _unit('seele', lc=_lc([cnt]))
        from engine.models.enemy import EnemyStatus
        target = _enemy()
        target.add_status(EnemyStatus(id='d1', name='易伤', category='debuff',
                                      remaining_turns=2))
        target.add_status(EnemyStatus(id='d2', name='减速', category='debuff',
                                      remaining_turns=2))
        state = SimState(enemies=[target], units=[u])
        base = _build_effective_stats(u, state)
        corrected = _lc_target_correct(base, u, state, target)
        assert corrected.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG - 0.08, rel=1e-9)


class TestTeamScope:
    def test_all_allies_stack_buff_reaches_teammates(self):
        """叠层型全队光锥加成不能只停留在持有者的静态面板。"""
        gain = _gain_effect('on_ult', 1, target='all_allies')
        panel = LightConeEffect(type='conditional_buff', condition='全队增益',
                                attributes={'CRIT_DMG': 30.0},
                                condition_code='stack:test:1', target='all_allies')
        source = _unit('seele', lc=_lc([gain, panel]))
        ally = _unit('xilian', position=2)
        state = SimState(enemies=[_enemy()], units=[source, ally])
        _process_lc_effects(source, state, 'on_ult')
        assert _build_effective_stats(ally, state).CRIT_DMG == pytest.approx(
            ally.base_stats.CRIT_DMG + 0.30, rel=1e-9)

    def test_ally_main_stack_buff_skips_holder(self):
        """单体目标叠层应加给被选中的队友，不能错误留在持有者面板。"""
        gain = _gain_effect('on_skill', 1, target='ally_main')
        panel = LightConeEffect(type='conditional_buff', condition='单体增益',
                                attributes={'DMG_BONUS_ALL': 30.0},
                                condition_code='stack:test:1', target='ally_main')
        source = _unit('seele', lc=_lc([gain, panel]))
        ally = _unit('xilian', position=2)
        state = SimState(enemies=[_enemy()], units=[source, ally])
        _process_lc_effects(source, state, 'on_skill')
        assert _build_effective_stats(source, state).DMG_BONUS_ALL == pytest.approx(
            source.base_stats.DMG_BONUS_ALL - 0.30, rel=1e-9)
        assert _build_effective_stats(ally, state).DMG_BONUS_ALL == pytest.approx(
            ally.base_stats.DMG_BONUS_ALL + 0.30, rel=1e-9)

    def test_real_time_flower_buffs_party(self):
        """如果时间是一朵花: 【谕示】应把暴伤实际授予全队。"""
        from engine.models.equipment import load_lightcone
        source = _unit('bronya', lc=load_lightcone('if_time_were_a_flower'))
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[source, ally])
        _process_lc_effects(source, state, 'on_battle_start')
        assert _build_effective_stats(ally, state).CRIT_DMG == pytest.approx(
            ally.base_stats.CRIT_DMG + 0.48, rel=1e-9)

    def test_real_earthly_escapade_excludes_holder(self):
        """游戏尘寰: 【假面】加成只给队友，不能额外给持有者。"""
        from engine.models.equipment import load_lightcone
        source = _unit('bronya', lc=load_lightcone('earthly_escapade'))
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[source, ally])
        _process_lc_effects(source, state, 'on_battle_start')
        assert _build_effective_stats(source, state).CRIT_RATE == pytest.approx(
            source.base_stats.CRIT_RATE - 0.10, rel=1e-9)
        assert _build_effective_stats(ally, state).CRIT_RATE == pytest.approx(
            ally.base_stats.CRIT_RATE + 0.10, rel=1e-9)


class TestHpLossEvents:
    def test_self_hp_cost_triggers_lightcone_stack(self):
        """自身扣血也必须触发光锥 on_hp_loss，不应只覆盖敌方受击。"""
        u = _unit('mydei', lc=_lc([_gain_effect('on_hp_loss', 1)], path='毁灭'))
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        assert u.lc_stacks.get('test_lc::test') == 1


class TestRealLightCones:
    def test_brighter_than_the_sun_progression(self):
        """比阳光更明亮: 普攻叠层 → 面板 ATK 递增"""
        from engine.models.equipment import load_lightcone
        lc = load_lightcone('brighter_than_the_sun')
        u = _unit('mydei', lc=lc)
        state = SimState(enemies=[_enemy()], units=[u])
        s0 = _build_effective_stats(u, state)
        atk0 = s0.ATK
        _process_lc_effects(u, state, 'on_basic_attack')
        s1 = _build_effective_stats(u, state)
        assert s1.ATK > atk0
        _process_lc_effects(u, state, 'on_basic_attack')
        s2 = _build_effective_stats(u, state)
        assert s2.ATK > s1.ATK  # 2 层叠满

    def test_flowing_nightglow_ally_attack(self):
        """流光溢彩: 队友攻击 → 持有者歌咏叠层（广播 on_attack）"""
        from engine.models.equipment import load_lightcone
        lc = load_lightcone('flowing_nightglow')
        u = _unit('bronya', lc=lc)
        ally = _unit('xilian', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, 'on_attack')  # 队友攻击广播
        assert u.lc_stacks.get('flowing_nightglow::geyong') == 1
