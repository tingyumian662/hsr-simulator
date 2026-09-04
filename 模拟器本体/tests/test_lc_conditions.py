"""P3: 光锥 condition 动态化测试（事件缓冲器 + 状态求值 + 负向修正）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import load_lightcone, LightCone, LightConeEffect
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _build_effective_stats, _apply_lc_condition_corrections, _lc_target_correct, _lc_apply_event_effect, _process_lc_effects, simulate
from engine.runtime import SimUnit, SimState, TimedBuff


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


def _lc(effects):
    return LightCone(id='test_lc', name='测试光锥', path='巡猎', effects=effects)


def _mk_effect(etype, code, attrs, condition='', target='self'):
    return LightConeEffect(type=etype, condition=condition,
                           attributes=attrs, condition_code=code, target=target)


class TestStateGated:
    def test_event_effect_negated(self):
        """事件型条件码: 属性被负向抵消（base_stats 已含静态施加, 抵消后恢复白值）"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'event_skill_after', {'ATK_PERCENT': 40.0},
            '施放战技后攻击+40%')]))
        state = SimState(enemies=[_enemy()], units=[u])
        s = _build_effective_stats(u, state)
        assert s.ATK == pytest.approx(
            u.base_stats.ATK - u.base_stats._base_ATK * 0.40, rel=1e-9)  # 已抵消

    def test_enemies_ge_3(self):
        """state_enemies_ge_3: 3敌保留 / 1敌抵消"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'state_enemies_ge_3', {'CRIT_RATE': 16.0},
            '场上存在>=3名敌方目标')]))
        state = SimState(enemies=[_enemy(), _enemy(), _enemy()], units=[u])
        s3 = _build_effective_stats(u, state)
        assert s3.CRIT_RATE == pytest.approx(u.base_stats.CRIT_RATE, rel=1e-9)  # 保留
        state1 = SimState(enemies=[_enemy()] * 1, units=[u])
        s1 = _build_effective_stats(u, state1)
        assert s1.CRIT_RATE == pytest.approx(u.base_stats.CRIT_RATE - 0.16, rel=1e-9)

    def test_unsupported_keeps_and_warns(self):
        """unsupported: 保持常驻近似 + WARN 一次（防刷屏）"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'unsupported', {'CRIT_DMG': 24.0},
            '每层【标记】使暴伤+24%')]))
        state = SimState(enemies=[_enemy()], units=[u])
        s = _build_effective_stats(u, state)
        assert s.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG, rel=1e-9)  # 保留
        assert any('光锥[test_lc]条件未建模' in l for l in state.log)
        n_warn = sum(1 for l in state.log if '条件未建模' in l)
        _build_effective_stats(u, state)
        assert sum(1 for l in state.log if '条件未建模' in l) == n_warn  # 不重复

    def test_typed_permanent_kept(self):
        """typed_permanent（限定属性型）: 常驻不修正"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'typed_permanent', {'DMG_BONUS_ATK_FOLLOW_UP': 30.0},
            '追加攻击伤害+30%')]))
        state = SimState(enemies=[_enemy()], units=[u])
        s = _build_effective_stats(u, state)
        # 限定属性存于 DMG_BONUS_BY_ATTACK_TYPE['follow_up']
        assert s.DMG_BONUS_BY_ATTACK_TYPE.get('follow_up', 0.0) == pytest.approx(0.30, rel=1e-9)
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE.get('follow_up', 0.0) == pytest.approx(
            0.30, rel=1e-9)  # 保留（静态施加未抵消）


class TestTargetGated:
    def test_hp_below_50_target(self):
        """对HP≤50%目标增伤: 低血目标保留 / 高血目标抵消"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'state_hp_below_50_target', {'CRIT_RATE': 16.0},
            '对生命值<=50%的敌方目标暴击率+16%')]))
        low = _enemy(hp=500000)
        low.HP = 100000  # 20%
        high = _enemy(hp=500000)  # 100%
        state = SimState(enemies=[low, high], units=[u])
        s_low = _lc_target_correct(u.base_stats, u, state, low)
        s_high = _lc_target_correct(u.base_stats, u, state, high)
        assert s_low.CRIT_RATE == pytest.approx(u.base_stats.CRIT_RATE, rel=1e-9)  # 保留
        assert s_high.CRIT_RATE == pytest.approx(
            u.base_stats.CRIT_RATE - 0.16, rel=1e-9)  # 高血抵消


class TestEventBuffer:
    def test_battle_start_buff(self):
        """事件缓冲器: battle_start 挂 TimedBuff 恢复被抵消属性"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'event_battle_start', {'DMG_BONUS_ALL': 24.0},
            '进入战斗后全队伤害+24%持续3回合', )]))
        state = SimState(enemies=[_enemy()], units=[u])
        # 无 buff 时面板已抵消（静态施加被负向抵消）
        s0 = _build_effective_stats(u, state)
        assert s0.DMG_BONUS_ALL == pytest.approx(
            u.base_stats.DMG_BONUS_ALL - 0.24, rel=1e-9)
        # 事件触发后挂 buff → 面板恢复静态值
        _process_lc_effects(u, state, "on_battle_start")
        assert any(b.source_id == 'test_lc' for b in u.buffs)
        assert u.buffs[-1].remaining_turns == 3
        s1 = _build_effective_stats(u, state)
        assert s1.DMG_BONUS_ALL == pytest.approx(u.base_stats.DMG_BONUS_ALL, rel=1e-9)

    def test_all_allies_target(self):
        """target=all_allies: buff 挂全队"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'event_skill_after', {'DMG_BONUS_ALL': 16.0},
            '施放战技后我方全体伤害+16%', target='all_allies')]))
        ally = _unit('xilian', position=2)
        ally.lightcone = None
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, "on_skill")
        assert any(b.source_id == 'test_lc' for b in ally.buffs)

    def test_kill_event(self):
        """on_kill 事件触发"""
        u = _unit('seele', lc=_lc([_mk_effect(
            'conditional_buff', 'event_kill', {'ATK_PERCENT': 40.0},
            '消灭敌方目标后攻击+40%持续2回合')]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, "on_kill")
        assert any(b.source_id == 'test_lc' for b in u.buffs)


class TestRealLightCone:
    def test_stellar_sea_blackbox(self):
        """星海巡航黑盒: 低血敌人被击杀 → 击杀事件挂ATK40% buff"""
        from engine.core.combat_engine import simulate
        lc = load_lightcone('cruising_in_the_stellar_sea')
        chars = [{'char': load_character('seele', 'data/characters'),
                  'position': 1, 'lightcone': lc}]
        s = simulate(chars, _enemy(hp=8000), max_av=600)
        log = '\n'.join(s.log)
        assert '光锥[cruising_in_the_stellar_sea] on_kill' in log
        assert "['ATK_percent']" in log  # 击杀 buff 挂载

    def test_model_parses_condition_code(self):
        """模型解析: condition_code/target 字段读入"""
        lc = load_lightcone('cruising_in_the_stellar_sea')
        codes = {e.condition_code for e in lc.effects}
        assert 'state_hp_below_50_target' in codes
        assert 'event_kill' in codes


class TestMultihitPath:
    def test_bounce_applies_target_condition(self):
        """弹射路径同样应用目标相关光锥条件（星海巡航 HP≤50% CR+16%）"""
        import copy
        from engine.core.combat_engine import _multihit_damage
        lc = load_lightcone('cruising_in_the_stellar_sea')
        u = _unit('seele', lc=lc)
        # 面板 CR 调至 0.50（含静态 0.32）: 低血保留→暴击; 高血抵消 0.16→0.34 不暴
        stats = copy.deepcopy(u.base_stats)
        stats.CRIT_RATE = 0.50
        low = _enemy(hp=500000)
        low.HP = 100000  # 20% 低血
        high = _enemy(hp=500000)  # 100%
        state = SimState(enemies=[low, high], units=[u])
        d_low = _multihit_damage(stats, [low], stats.ATK, 100.0,
                                 'direct', '量子', True, hits=1,
                                 u=u, state=state)
        d_high = _multihit_damage(stats, [high], stats.ATK, 100.0,
                                  'direct', '量子', True, hits=1,
                                  u=u, state=state)
        # 低血目标暴击伤害 > 高血目标（CR 16% 跨阈值 → 暴击乘区生效）
        assert d_low > d_high


class TestEventActions:
    """v5.1: 无属性事件型效果（回能/回血/回SP）动作处理"""

    def test_epoch_sp_recovery(self):
        """时代铭记: 终结技后回1SP"""
        from engine.core.combat_engine import _process_lc_effects
        lc = load_lightcone('epoch_etched_in_golden_blood')
        u = _unit('bronya', lc=lc)
        state = SimState(enemies=[_enemy()], units=[u])
        state.skill_points = 4
        _process_lc_effects(u, state, 'on_ult')
        assert state.skill_points == 5

    def test_when_she_decided_wave_energy(self):
        """当她决定看见: 波次回15能量（吃 ENERGY_REGEN 倍率）"""
        from engine.core.combat_engine import _process_lc_effects
        lc = load_lightcone('当她决定看见')
        u = _unit('yinlang', lc=lc)
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_wave_start')
        er = _build_effective_stats(u, state).ENERGY_REGEN
        # v6.10: 特殊能量角色(银狼Lv.999)无能量系统, 光锥回能 no-op
        assert u.current_energy == pytest.approx(0.0, abs=1e-6)

    def test_past_self_mirror_team_energy(self):
        """镜中故我: 波次全队回10能量"""
        from engine.core.combat_engine import _process_lc_effects
        lc = load_lightcone('past_self_in_mirror')
        u = _unit('bronya', lc=lc)
        ally = _unit('xilian', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _process_lc_effects(u, state, 'on_wave_start')
        assert u.current_energy == pytest.approx(10.0, abs=1e-6)
        # v6.10: 特殊能量角色(昔涟)无能量系统, 光锥回能 no-op
        assert ally.current_energy == pytest.approx(0.0, abs=1e-6)

    def test_solitary_healing_kill_energy(self):
        """孤独的疗愈: 击杀回6能量"""
        from engine.core.combat_engine import _process_lc_effects
        lc = load_lightcone('solitary_healing')
        u = _unit('pela', lc=lc)
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_kill')
        assert u.current_energy == pytest.approx(6.0, abs=1e-6)

    def test_something_irreplaceable_heal(self):
        """无可取代的东西: 击杀/受击回8%攻击生命"""
        from engine.core.combat_engine import _process_lc_effects, _build_effective_stats
        lc = load_lightcone('something_irreplaceable')
        u = _unit('mydei', lc=lc)
        u.current_hp = u.max_hp * 0.5
        state = SimState(enemies=[_enemy()], units=[u])
        atk = _build_effective_stats(u, state).ATK
        hp0 = u.current_hp
        _process_lc_effects(u, state, 'on_kill')
        assert u.current_hp == pytest.approx(hp0 + atk * 0.08, abs=1e-6)
        hp1 = u.current_hp
        _process_lc_effects(u, state, 'on_hit_taken')
        assert u.current_hp == pytest.approx(hp1 + atk * 0.08, abs=1e-6)

    def test_unmapped_empty_attrs_still_warns(self):
        """未映射的空attrs事件效果仍 WARN（不回退为静默）"""
        from engine.core.combat_engine import _process_lc_effects
        u = _unit('seele', lc=_lc([_mk_effect(
            'trigger_effect', 'event_kill', {}, '击杀后回能(未映射)')]))
        state = SimState(enemies=[_enemy()], units=[u])
        _process_lc_effects(u, state, 'on_kill')
        assert any('未建模' in l for l in state.log)

    def test_mismatched_path_does_not_trigger_event_action(self):
        """命途不匹配时，光锥动态效果必须与静态效果一样失效。"""
        lc = load_lightcone('epoch_etched_in_golden_blood')
        u = _unit('seele', lc=lc)
        state = SimState(enemies=[_enemy()], units=[u], skill_points=4)
        _process_lc_effects(u, state, 'on_ult')
        assert state.skill_points == 4
        assert _build_effective_stats(u, state).DMG_BONUS_ALL == pytest.approx(
            u.base_stats.DMG_BONUS_ALL)

    def test_past_self_ultimate_team_buff_and_sp(self):
        """镜中故我: 终结技后全队增伤3回合，击破特攻达标时回1SP。"""
        lc = load_lightcone('past_self_in_mirror')
        u = _unit('bronya', lc=lc)
        u.base_stats.BREAK_EFFECT = 1.5
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally], skill_points=4)
        _process_lc_effects(u, state, 'on_ult')
        assert state.skill_points == 5
        assert any(b.source_id == lc.id for b in u.buffs)
        assert any(b.source_id == lc.id for b in ally.buffs)

    def test_initial_wave_triggers_wave_start_effects(self):
        """首波也应结算“每波次开始”的光锥效果。"""
        lc = load_lightcone('past_self_in_mirror')
        bronya = load_character('bronya', 'data/characters')
        seele = load_character('seele', 'data/characters')
        state = simulate([
            {'char': bronya, 'position': 1, 'lightcone': lc},
            {'char': seele, 'position': 2},
        ], _enemy(), max_av=0)
        assert state.units[0].current_energy == pytest.approx(70.0)  # v6.10: 50%开局(120×0.5=60)+光锥10
        assert state.units[1].current_energy == pytest.approx(70.0)  # v6.10: 50%开局60+队伍光锥10
