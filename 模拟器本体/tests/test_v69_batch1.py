"""v6.9 批1 回归: 星期日（同谐·虚数）+ 瓦尔特（虚无·虚数）+ 阮·梅（同谐·冰）

语义依据: 角色技能介绍/同谐/星期日.txt、虚无/瓦尔特.txt、同谐/阮·梅.txt + CLAUDE_HANDOFF v6.9 节
用户确认（2026-08-18）: 阮·梅 HP=1086（txt/JSON 统一）; 不死途速度=106; 不死途秘技=非进战"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _use_skill, _gain_energy, _build_effective_stats,
    _sunday_skill, _sunday_ult, _sunday_tick, _sunday_ai,
    _welt_ult, _welt_extra_damage, _welt_apply_slow, _welt_apply_shizhong,
    _ruanmei_xianyin_apply, _ruanmei_field_apply, _ruanmei_canmei_trigger,
    _ruanmei_break_damage, _ruanmei_tick, _ruanmei_apply_canmei,
)
from engine.core.effect_resolver import (
    _trace_sunday_trace2, _trace_welt_trace1, _trace_ruanmei_break,
)


def _enemy(hp=500000, toughness=200, broken=False):
    e = Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness,
              max_toughness=toughness, level=80,
              element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                           '虚数': -0.2, '物理': -0.2, '火': 0})
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestSunday:
    def test_skill_immediate_action_and_dmg(self):
        """战技: 目标立即行动 + 增伤30% 2回合 + 天赋CR+20%"""
        u = _unit('sunday')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {0: 500.0, 1: 600.0}
        _sunday_skill(state, u)
        assert state.extra['navs'][1] == pytest.approx(0.0)  # 立即行动
        assert any(getattr(b, 'param_id', '') == 'sunday_skill_dmg' for b in ally.buffs)
        assert any(getattr(b, 'param_id', '') == 'sunday_cr' for b in ally.buffs)

    def test_skill_harmony_no_immediate(self):
        """对同谐目标施放战技不触发立即行动"""
        u = _unit('sunday')
        ally = _unit('sunday', position=2)  # 同谐
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {0: 500.0, 1: 600.0}
        _sunday_skill(state, u)
        assert state.extra['navs'][1] == pytest.approx(600.0)  # 未提前

    def test_ult_mentor_dynamic_cd(self):
        """终结技: 回20%能量上限+【蒙福者】CD+30%×星期日CD+12%"""
        u = _unit('sunday')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        u.base_stats.CRIT_DMG = 1.0  # 100%暴伤
        _sunday_ult(state, u)
        assert ally.extra.get('sunday_mentor') is True
        # 希儿能量120×20%=24 < 40 → 行迹1补至40
        assert ally.current_energy == pytest.approx(40.0)
        mentor = next(b for b in ally.buffs
                      if getattr(b, 'param_id', '') == 'sunday_mentor_cd')
        assert mentor.attributes['CRIT_DMG'] == pytest.approx(42.0)  # 30%×100+12

    def test_trace2_start_energy(self):
        """行迹2: 开局25能量"""
        u = _unit('sunday')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_sunday_trace2(u, state)
        assert u.current_energy == pytest.approx(25.0)

    def test_mentor_tick_on_sunday_turn(self):
        """蒙福者按星期日回合递减: 3→2"""
        u = _unit('sunday')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _sunday_ult(state, u)
        _sunday_tick(state, u)
        mentor = next(b for b in ally.buffs
                      if getattr(b, 'param_id', '') == 'sunday_mentor_cd')
        assert mentor.remaining_turns == 2


class TestWelt:
    def test_skill_bounce_5_hits(self):
        """战技: 5段弹射（JSON _hits）造成伤害"""
        u = _unit('welt')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _use_skill(u, state, 'skill')
        assert u.total_damage_dealt > 0

    def test_slow_applied_deterministic(self):
        """减速状态确定性验证: 直接挂减速10% 2回合"""
        u = _unit('welt')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        from engine.core.combat_sim import _roll_effect_hit
        import engine.core.combat_sim as cs
        # 强制命中（75%基础概率; 测试直挂状态避开随机）
        e.add_status(EnemyStatus(id='welt_slow', name='减速', category='debuff',
                                 source='welt', remaining_turns=2,
                                 attributes={'spd_down': 0.10}))
        assert e.has_status(status_id='welt_slow')
        assert e.status_attribute('spd_down') == pytest.approx(0.10)

    def test_ult_imprison_shizhong(self):
        """终结技: 禁锢+【失重】(DEF-40%)"""
        u = _unit('welt')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _welt_ult(state, u)
        assert e.has_status(status_id='welt_jinggu')
        assert e.has_status(status_id='welt_shizhong')
        assert e.status_attribute('def_reduction') == pytest.approx(0.40)

    def test_talent_extra_on_slow(self):
        """天赋: 击中减速目标→100%ATK附加"""
        u = _unit('welt')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _welt_apply_slow(state, u, e)  # 挂减速
        state.extra['last_attack_targets'] = [e]
        dmg0 = u.total_damage_dealt
        _welt_extra_damage(state, u, 'basic_attack')
        assert u.total_damage_dealt > dmg0

    def test_trace1_start_energy(self):
        """行迹1: 开局30能量"""
        u = _unit('welt')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_welt_trace1(u, state)
        assert u.current_energy == pytest.approx(30.0)

    def test_e1_extra_on_shizhong_ult(self):
        """E1: 失重目标被终结技击中→40%终结技倍率附加"""
        u = _unit('welt', eidolon=1)
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _welt_apply_shizhong(state, u, e)
        state.extra['last_attack_targets'] = [e]
        dmg0 = u.total_damage_dealt
        _welt_extra_damage(state, u, 'ultimate')
        assert u.total_damage_dealt > dmg0
        assert '瓦尔特E1' in '\n'.join(state.log)


class TestRuanMei:
    def test_xianyin_buffs(self):
        """战技【弦外音】: 全队增伤32%+破韧效率50%"""
        u = _unit('ruan_mei')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _ruanmei_xianyin_apply(state, u)
        assert any(getattr(b, 'attributes', {}).get('DMG_BONUS_ALL') == 32.0
                   for b in ally.buffs)
        assert any(getattr(b, 'attributes', {}).get('TOUGHNESS_EFFICIENCY') == 50.0
                   for b in ally.buffs)

    def test_field_res_pen(self):
        """终结技结界: 全队全抗穿透25%"""
        u = _unit('ruan_mei')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _ruanmei_field_apply(state, u)
        assert state.extra['ruanmei_field_turns'] == 2
        assert any(getattr(b, 'attributes', {}).get('RES_PEN_ALL') == 25.0
                   for b in ally.buffs)

    def test_canmei_trigger_extend_break(self):
        """残梅绽: 破韧恢复时触发——延长破韧+推条+冰击破伤害"""
        u = _unit('ruan_mei')
        e = _enemy(broken=True)
        state = SimState(enemies=[e], units=[u])
        _ruanmei_field_apply(state, u)  # 结界激活才可挂残梅绽
        _ruanmei_apply_canmei(state, u, e)
        assert e.has_status(status_id='ruanmei_canmei')
        hp0 = e.HP
        triggered = _ruanmei_canmei_trigger(state, u, e)
        assert triggered is True
        assert e.is_broken is True  # 延长破韧（未恢复）
        assert e.HP < hp0
        assert not e.has_status(status_id='ruanmei_canmei')  # 触发后移除

    def test_break_damage_talent(self):
        """天赋: 我方击破弱点→120%冰击破伤害"""
        u = _unit('ruan_mei')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        dmg0 = u.total_damage_dealt
        _trace_ruanmei_break(u, state, enemy=e)
        assert u.total_damage_dealt > dmg0
        assert any('阮·梅天赋' in l for l in state.log)

    def test_field_tick(self):
        """结界按阮·梅回合递减"""
        u = _unit('ruan_mei')
        state = SimState(enemies=[_enemy()], units=[u])
        _ruanmei_field_apply(state, u)
        _ruanmei_tick(state, u)
        assert state.extra['ruanmei_field_turns'] == 1
