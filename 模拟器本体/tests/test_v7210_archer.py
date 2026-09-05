"""v7.21.0 批3: Archer 录入测试（Fate 联动·巡猎·量子, archer.txt）。

机制面: 充能资源（≤4: 终结技+2/行迹2+1/秘技+1, 心眼消耗）/ 回路连接（战技后 X 轴连动、
战技伤害叠层 2→E6 3、5次/SP不足/波次更替退出）/ 心眼追击（队友攻击后 200% +1SP）/
行迹1 SP上限+2 / 行迹3 守护者（SP获得后≥4 暴伤+120% 1回合, sp_change 观察者）/
E1 单回合3战技+2SP / E2 量子抗性-20%+弱点 / E4 终结技+150% / E6 回合+1SP·无视防20%。
"""
import pytest

from engine.core.combat_engine import simulate
from engine.models.enemy import Enemy
from engine.models.character import load_character
from engine.characters.archer import (
    _archer_charge_gain, _archer_circuit_stacks_cap, _archer_exit_circuit,
)
from tests.helpers import _unit as make_unit


def _archer(eidolon=0):
    return make_unit('archer', eidolon=eidolon)


def _enemy_obj():
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80, element_res={'量子': 0})


def _sim(eidolon=0, max_av=2000, mate=None):
    team = [{"char": load_character('archer', 'data/characters'),
             "position": 1, "eidolon": eidolon}]
    if mate:
        team.append({"char": load_character(mate, 'data/characters'),
                     "position": 2, "eidolon": 0})
    return simulate(team, _enemy_obj(), max_av=max_av)


def _log(s):
    return '\n'.join(s.log)


class TestCharge:
    def test_charge_cap(self):
        u = _archer()
        _archer_charge_gain(u, 3)
        _archer_charge_gain(u, 3)
        assert u.extra['archer_charge'] == 4  # ≤4 封顶

    def test_stacks_cap_e6(self):
        assert _archer_circuit_stacks_cap(_archer()) == 2
        assert _archer_circuit_stacks_cap(_archer(eidolon=6)) == 3


class TestCircuit:
    def test_skill_enters_circuit_and_queues_extra(self):
        from engine.characters.archer import _archer_skill_cast
        from engine.runtime import SimState
        u = _archer()
        state = SimState(enemies=[_enemy_obj()], units=[u])
        state.skill_points = 5
        _archer_skill_cast(state, u)
        assert u.extra.get('archer_circuit') is True
        assert u.extra['archer_circuit_stacks'] == 1
        assert any(x is u for x, k in state.extra.get('extra_turns', []))  # X轴连动
        assert '回路连接' in _log(state)

    def test_exit_at_5_casts(self):
        from engine.characters.archer import _archer_skill_cast
        from engine.runtime import SimState
        u = _archer()
        state = SimState(enemies=[_enemy_obj()], units=[u])
        state.skill_points = 10
        state.extra['extra_turns'] = []  # 手动清队列观察计数
        for i in range(4):
            state.extra['extra_turns'] = []
            _archer_skill_cast(state, u)
            assert u.extra.get('archer_circuit'), i
        state.extra['extra_turns'] = []
        _archer_skill_cast(state, u)  # 第5次→退出
        assert not u.extra.get('archer_circuit')
        assert 'archer_circuit_stacks' not in u.extra

    def test_exit_clears_all_state(self):
        from engine.runtime import SimState
        u = _archer()
        u.extra.update(archer_circuit=True, archer_circuit_stacks=2,
                       archer_circuit_casts=3, archer_turn_skill_count=1)
        _archer_exit_circuit(u)
        for k in ('archer_circuit', 'archer_circuit_stacks',
                  'archer_circuit_casts', 'archer_turn_skill_count'):
            assert k not in u.extra


class TestMindEye:
    def test_ally_attack_triggers_followup(self):
        from engine.characters.archer import _archer_mind_eye
        from engine.runtime import SimState
        u = _archer()
        mate = make_unit('huohuo')
        _archer_charge_gain(u, 2)
        state = SimState(enemies=[_enemy_obj()], units=[u, mate])
        state.skill_points = 3
        _archer_mind_eye(state, mate, 'skill')
        assert u.extra['archer_charge'] == 1  # 消耗1
        assert state.skill_points == 4  # +1SP
        assert '心眼（真）' in _log(state)
        # 充能耗尽不再追击
        _archer_mind_eye(state, mate, 'skill')
        _archer_mind_eye(state, mate, 'skill')
        assert '心眼' not in _log(state).split('心眼（真）')[-1]

    def test_self_attack_does_not_trigger(self):
        from engine.characters.archer import _archer_mind_eye
        from engine.runtime import SimState
        u = _archer()
        _archer_charge_gain(u, 2)
        state = SimState(enemies=[_enemy_obj()], units=[u])
        _archer_mind_eye(state, u, 'skill')
        assert u.extra['archer_charge'] == 2


class TestSpChangeObserver:
    def test_guardian_cd_buff(self):
        from engine.core.combat_engine import (_ensure_phase_tables, _obs_phase,
                                               _gain_skill_points)
        from engine.runtime import SimState
        u = _archer()
        state = SimState(enemies=[], units=[u])
        _ensure_phase_tables(state)
        state.max_sp = 7  # 行迹1 场景
        state.skill_points = 2
        _gain_skill_points(state, 1)
        assert not any(b.param_id == 'archer_guardian_cd' for b in u.buffs)  # 3<4
        _gain_skill_points(state, 2)
        assert any(b.param_id == 'archer_guardian_cd' for b in u.buffs)  # ≥4
        assert next(b for b in u.buffs
                    if b.param_id == 'archer_guardian_cd').attributes['CRIT_DMG'] == 120.0


class TestInitAndEidolons:
    def test_init_sp_cap_and_charge(self):
        from engine.core.combat_engine import _setup_battle
        state, _ = _setup_battle(
            [{"char": load_character('archer', 'data/characters'),
              "position": 1, "eidolon": 0}], _enemy_obj(), 1000, 1, None)
        u = next(x for x in state.units if x.char.id == 'archer')
        assert state.max_sp == 7  # 行迹1 +2
        assert u.extra.get('archer_charge') == 1  # 行迹2

    def test_e2_ult_applies_quantum_weakness(self):
        from engine.characters.archer import _archer_ult_cast
        from engine.runtime import SimState
        u = _archer(eidolon=2)
        e = _enemy_obj()
        state = SimState(enemies=[e], units=[u])
        _archer_ult_cast(state, u)
        assert any(s.attributes.get('weakness_element') == '量子' for s in e.statuses)
        assert e.element_res['量子'] == pytest.approx(-0.20)


class TestSimulation:
    def test_solo_battle_circuit_and_mind_eye(self):
        s = _sim(max_av=2000, mate='huohuo')
        log = _log(s)
        assert '回路连接' in log
        assert '心眼（真）' in log
        assert s.units[0].total_damage_dealt > 0

    def test_sp_cap_visible_in_battle(self):
        s = _sim(max_av=1200, mate='huohuo')
        assert s.max_sp == 7
