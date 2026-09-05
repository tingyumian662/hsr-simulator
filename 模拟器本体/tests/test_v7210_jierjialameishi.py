"""v7.21.0 批2: 吉尔伽美什录入测试（jierjialameishi, 拼音 id 替换英文壳 gilgamesh）。

机制面: 兴致资源（SPD+10%/点增量与清空回落、行迹2暴伤层≤6、首次≥10 来兴致了）/
来兴致了状态（仅战技+免SP+战技后清空）/ 王之财宝（王来承认 buff、E1 全队化+ATK60%+40能、
E2 倍率）/ 天地乖离（黄金律耗尽暴伤、E6 弹射180%、E2 终结技+5兴致）/ 天赋（自动普攻、
队友终结技王来背负+2兴致+回能30%）/ 行迹3光环（ATK/CD 20%+能量上限加成）/ E6 全队抗穿 /
连携合击（Gil/Saber 计数8 → 双人全体合击 + saber 收益）/ 秘技。
"""
import pytest

from engine.core.combat_engine import simulate
from engine.models.enemy import Enemy
from engine.models.character import load_character
from engine.characters.jierjialameishi import (
    _gil_xingzhi_gain, _gil_xingzhi_clear, _gil_joint_attack,
)
from tests.helpers import _unit as make_unit


def _gil(eidolon=0):
    return make_unit('jierjialameishi', eidolon=eidolon)


def _enemy_obj():
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80, element_res={'雷': 0})


def _sim(eidolon=0, max_av=2500, mate=None, mate_eid=0):
    team = [{"char": load_character('jierjialameishi', 'data/characters'),
             "position": 1, "eidolon": eidolon}]
    if mate:
        team.append({"char": load_character(mate, 'data/characters'),
                     "position": 2, "eidolon": mate_eid})
    return simulate(team, _enemy_obj(), max_av=max_av, num_enemies=2)


def _log(s):
    return '\n'.join(s.log)


class TestXingzhi:
    def test_gain_speed_and_high_spirit(self):
        from engine.runtime import SimState
        u = _gil()
        state = SimState(enemies=[], units=[u])
        base_spd = u.base_stats.SPD
        _gil_xingzhi_gain(u, 4, state)
        assert u.extra['gil_xingzhi'] == 4
        assert u.base_stats.SPD == pytest.approx(base_spd + u.base_stats._base_SPD * 0.4)
        assert not u.extra.get('gil_high_spirit')
        _gil_xingzhi_gain(u, 6, state)
        assert u.extra['gil_high_spirit']  # 首次达10
        _gil_xingzhi_clear(u, state)
        assert u.base_stats.SPD == pytest.approx(base_spd)
        assert 'gil_xingzhi' not in u.extra
        # 清空后来兴致了保持（整场）; 再次达10不重复置位
        _gil_xingzhi_gain(u, 10, state)
        assert u.extra['gil_high_spirit']

    def test_trace2_cd_stacks_cap6(self):
        from engine.runtime import SimState
        u = _gil()
        state = SimState(enemies=[], units=[u])
        base_cd = u.base_stats.CRIT_DMG
        _gil_xingzhi_gain(u, 8, state)
        _gil_xingzhi_clear(u, state)
        _gil_xingzhi_gain(u, 8, state)  # 累计获得16 → 6层封顶
        assert u.extra['gil_t2_cd_stacks'] == 6
        assert u.base_stats.CRIT_DMG == pytest.approx(base_cd + 0.25 * 6)

    def test_sp_cost_override_in_high_spirit(self):
        from engine.characters.jierjialameishi import _gil_sp_cost_override
        from engine.runtime import SimState
        u = _gil()
        state = SimState(enemies=[], units=[u])
        assert _gil_sp_cost_override(u, state, sp_cost=1, skill_key='skill') is None
        u.extra['gil_high_spirit'] = True
        assert _gil_sp_cost_override(u, state, sp_cost=1, skill_key='skill') == 0


class TestSkillAndUlt:
    def test_skill_grants_acknowledged_e1_teamwide(self):
        from engine.characters.jierjialameishi import _gil_skill_cast
        from engine.runtime import SimState
        u = _gil(eidolon=1)
        mate = make_unit('huohuo')
        state = SimState(enemies=[_enemy_obj()], units=[u, mate])
        _gil_skill_cast(state, u)
        assert any(b.param_id == 'gil_acknowledged' for b in u.buffs)
        assert any(b.param_id == 'gil_acknowledged' for b in mate.buffs)  # E1 全队
        assert '王之财宝' in _log(state)

    def test_ult_golden_law(self):
        from engine.characters.jierjialameishi import _gil_ult_cast
        from engine.runtime import SimState
        u = _gil(eidolon=6)
        u.extra['gil_golden_law'] = 3
        state = SimState(enemies=[_enemy_obj()], units=[u])
        _gil_ult_cast(state, u)
        assert 'gil_golden_law' not in u.extra  # 耗尽
        assert '黄金律' in _log(state)
        # E2 终结技+5 / 行迹1 +2
        assert u.extra.get('gil_xingzhi') == 7

    def test_high_spirit_skill_clears_xingzhi(self):
        from engine.characters.jierjialameishi import _gil_skill_cast
        from engine.runtime import SimState
        u = _gil()
        u.extra['gil_high_spirit'] = True
        u.extra['gil_xingzhi'] = 10
        state = SimState(enemies=[_enemy_obj()], units=[u])
        _gil_skill_cast(state, u)
        assert 'gil_xingzhi' not in u.extra


class TestAllyUltSettle:
    def test_ally_ult_grants_burden_and_energy(self):
        from engine.characters.jierjialameishi import _gil_settle_ally_ult
        from engine.runtime import SimState
        u = _gil()
        mate = make_unit('huohuo')
        e_before = u.current_energy
        state = SimState(enemies=[], units=[u, mate])
        _gil_settle_ally_ult(mate, state, None, 'ultimate', 0)
        assert any(b.param_id == 'gil_burden' for b in u.buffs)
        assert u.extra['gil_xingzhi'] == 2
        assert u.current_energy == pytest.approx(
            e_before + (mate.char.max_energy or 0) * 0.30)
        _gil_settle_ally_ult(mate, state, None, 'skill', 0)
        assert u.extra['gil_xingzhi'] == 2  # 非终结技不触发

    def test_e6_golden_law_accumulates(self):
        from engine.characters.jierjialameishi import _gil_settle_ally_ult
        from engine.runtime import SimState
        u = _gil(eidolon=6)
        mate = make_unit('huohuo')
        state = SimState(enemies=[], units=[u, mate])
        for _ in range(5):
            _gil_settle_ally_ult(mate, state, None, 'ultimate', 0)
        assert u.extra['gil_golden_law'] == 3  # ≤3


class TestJointAttack:
    def test_joint_attack_with_saber(self):
        from engine.runtime import SimState
        from engine.characters.saber import _saber_joint_reward
        gil = _gil()
        saber = make_unit('saber', eidolon=6)
        e_before = saber.current_energy
        state = SimState(enemies=[_enemy_obj()], units=[gil, saber])
        _gil_joint_attack(state)
        assert gil.extra['gil_xingzhi'] == 3
        assert saber.current_energy > e_before  # +120 能量
        assert saber.extra.get('saber_next_ult_boost') == 1.20
        assert '本王允许你进攻' in _log(state)

    def test_settle_counter_triggers_at_8(self):
        from engine.characters.jierjialameishi import _gil_settle_self
        from engine.runtime import SimState
        gil = _gil()
        state = SimState(enemies=[_enemy_obj()], units=[gil])
        for _ in range(7):
            _gil_settle_self(gil, state, None, 'skill', 0)
        assert gil.extra['gil_joint_count'] == 7
        _gil_settle_self(gil, state, None, 'skill', 0)  # 第8次→合击+清零
        assert gil.extra['gil_joint_count'] == 0
        assert '本王允许你进攻' in _log(state)


class TestInitAura:
    def test_trace3_aura_scales_with_energy_cap(self):
        from engine.core.combat_engine import _setup_battle
        state, _ = _setup_battle(
            [{"char": load_character('jierjialameishi', 'data/characters'),
              "position": 1, "eidolon": 0},
             {"char": load_character('huohuo', 'data/characters'),
              "position": 2, "eidolon": 0}],
            _enemy_obj(), 1000, 1, None)
        gil = next(x for x in state.units if x.char.id == 'jierjialameishi')
        huohuo = next(x for x in state.units if x.char.id == 'huohuo')
        gil_aura = next(b for b in gil.buffs if getattr(b, 'param_id', '') == 'gil_trace3_aura')
        hh_aura = next(b for b in huohuo.buffs if getattr(b, 'param_id', '') == 'gil_trace3_aura')
        # 360上限: 20+min(100, 360-140)=20+100 → 120; 藿藿140(不超过阈值): 20
        assert gil_aura.attributes['ATK_PERCENT'] == 120.0
        assert hh_aura.attributes['CRIT_DMG'] == 20.0


class TestSimulation:
    def test_saber_pair_joint_attack(self):
        s = _sim(max_av=2500, mate='saber')
        log = _log(s)
        assert '王之财宝' in log
        assert '自动普攻' in log
        assert '本王允许你进攻' in log  # 连携合击
        assert s.units[0].total_damage_dealt > 0

    def test_huohuo_pair_burden(self):
        s = _sim(max_av=2500, mate='huohuo')
        assert '王来背负' in _log(s)  # 队友(藿藿)终结技联动

    def test_high_spirit_reached_in_long_battle(self):
        s = _sim(max_av=3500, mate='huohuo')
        assert '来兴致了' in _log(s)
