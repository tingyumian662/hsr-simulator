"""v5.3: 行动条标记系统（浮元）测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import SimUnit, SimState, _check_fatal, MARKER_ACTIONS
from engine.systems.timeline_marker import TimelineMarkerSystem


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


def _state(e, *units):
    s = SimState(enemies=[e], units=list(units))
    s.current_av = 0.0
    return s


def _mk_sys(state):
    sys = TimelineMarkerSystem()
    sys.action_handlers.update(MARKER_ACTIONS)
    state.extra['_marker_sys'] = sys
    return sys


class TestFuyuanSpawn:
    def test_spawn_creates_marker(self):
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        m = sys.spawn(state, lingsha, 'lingsha_fuyuan')
        assert m is not None
        assert m.action_spd == 90.0
        assert m.extra['charges'] == 3
        assert lingsha.marker is m
        assert m in state.markers
        assert m.extra['next_av'] > 0  # 90速: 10000/90 ≈ 111

    def test_resummon_adds_charges_capped(self):
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        sys.spawn(state, lingsha, 'lingsha_fuyuan')  # charges=3
        sys.spawn(state, lingsha, 'lingsha_fuyuan')  # +3 → 6 → cap 5
        assert lingsha.marker.extra['charges'] == 5

    def test_advance_pulls_next_av(self):
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        m = sys.spawn(state, lingsha, 'lingsha_fuyuan')
        av0 = m.extra['next_av']
        sys.advance(state, lingsha, 0.20)  # 战技: 浮元行动提前20%
        assert m.extra['next_av'] == pytest.approx(av0 - 10000.0 / 90.0 * 0.20, abs=1e-6)

    def test_advance_full_av(self):
        """终结技提前100%: next_av 归 0（立即行动）"""
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        m = sys.spawn(state, lingsha, 'lingsha_fuyuan')
        sys.advance(state, lingsha, 1.0)
        assert m.extra['next_av'] == pytest.approx(0.0, abs=1e-9)


class TestFuyuanAction:
    def test_action_deals_damage_heals_cleanses(self):
        lingsha = _unit('lingsha')
        e = _enemy(toughness=200)
        state = _state(e, lingsha)
        sys = _mk_sys(state)
        m = sys.spawn(state, lingsha, 'lingsha_fuyuan')
        hp0 = e.HP
        # 先扣血供治疗验证（灵砂初始满血会被治疗上限截断）
        lingsha.current_hp -= 500
        hp_u0 = lingsha.current_hp
        # 挂1个负面供净化
        lingsha.statuses.append(type('S', (), {'category': 'debuff', 'name': 'x'})())
        sys.handle_action(state, m)
        # 伤害（全体段75%ATK + 单体段75%ATK）+ 削韧（全体每目标10 + 单体10）
        assert e.HP < hp0
        assert e.toughness < 200
        # 治疗（12%ATK+360, ATK基数）
        assert lingsha.current_hp > hp_u0
        # 净化
        assert lingsha.statuses == []
        # 行动次数 -1
        assert m.extra['charges'] == 2
        assert '浮元行动' in '\n'.join(state.log)

    def test_charges_deplete_despawn(self):
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        m = sys.spawn(state, lingsha, 'lingsha_fuyuan')
        for _ in range(3):
            sys.handle_action(state, m)
            if not m.is_alive:
                break
        assert not m.is_alive
        assert lingsha.marker is None
        assert '浮元消失' in '\n'.join(state.log)

    def test_summoner_death_despawns_marker(self):
        lingsha = _unit('lingsha')
        state = _state(_enemy(), lingsha)
        sys = _mk_sys(state)
        sys.spawn(state, lingsha, 'lingsha_fuyuan')
        lingsha.current_hp = 0
        _check_fatal(state, lingsha)
        assert lingsha.marker is None
