"""v7.21.0 批1: Saber 录入测试（Fate 联动·毁灭·风, saber.txt）。

机制面: 炉心共鸣资源（获得联动行迹3暴伤层/E2无视防层）/ 战技条件分支（耗尽回满→倍率
提升+耗尽回能, 否则+3）/ 湖之祝福溢出银行（120→E6 200, 终结技清空回能, 开战能量≥60%）/
终结技后强化普攻 key_rewrite 一次性替换（敌数2/1 额外段）/ 行迹1 魔力放出（+1SP+立即
行动）/ 天赋任意我方终结技联动（含自身）/ E1-E6 / 秘技。
"""
import pytest

from engine.core.combat_engine import simulate
from engine.models.enemy import Enemy
from engine.models.character import load_character
from engine.characters.saber import _saber_res_gain, _saber_bank_cap
from tests.helpers import _unit as make_unit


def _saber(eidolon=0):
    return make_unit('saber', eidolon=eidolon)


def _enemy_obj(count=1):
    e = Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
              toughness=30, max_toughness=30, level=80, element_res={'风': 0})
    return e


def _sim(eidolon=0, max_av=2500, mate=None):
    team = [{"char": load_character('saber', 'data/characters'),
             "position": 1, "eidolon": eidolon}]
    if mate:
        team.append({"char": load_character(mate, 'data/characters'),
                     "position": 2, "eidolon": 0})
    return simulate(team, _enemy_obj(), max_av=max_av, num_enemies=2)


def _log(s):
    return '\n'.join(s.log)


class TestResonance:
    def test_gain_accumulates_with_trace3_cd_stacks(self):
        from engine.runtime import SimState
        u = _saber()
        state = SimState(enemies=[], units=[u])
        for _ in range(5):
            _saber_res_gain(u, 2, state)
        assert u.extra['saber_resonance'] == 10
        assert u.extra['saber_t3_cd_stacks'] == 8  # min(8, 10)
        assert u.base_stats.CRIT_DMG == pytest.approx(0.5 + 0.04 * 8)  # 面板0.5+行迹3八层

    def test_e2_defpen_stacks_cap_15(self):
        from engine.runtime import SimState
        u = _saber(eidolon=2)
        state = SimState(enemies=[], units=[u])
        for _ in range(10):
            _saber_res_gain(u, 2, state)
        assert u.extra['saber_e2_defpen_stacks'] == 15
        assert u.base_stats.DEF_PEN == pytest.approx(0.15)


class TestBank:
    def test_bank_caps(self):
        assert _saber_bank_cap(_saber()) == 120.0
        assert _saber_bank_cap(_saber(eidolon=6)) == 200.0

    def test_overflow_banks_via_observer(self):
        from engine.runtime import SimState
        from engine.core.combat_engine import _obs_phase, _ensure_phase_tables
        u = _saber()
        state = SimState(enemies=[], units=[u])
        _ensure_phase_tables(state)
        _obs_phase(state, 'energy_overflow_bank', u, overflow=50.0)
        _obs_phase(state, 'energy_overflow_bank', u, overflow=100.0)
        assert u.extra['saber_bank'] == 120.0  # 50+100 截断于 120

    def test_init_energy_floor_60pct(self):
        from engine.core.combat_engine import _setup_battle
        state, _ = _setup_battle(
            [{"char": load_character('saber', 'data/characters'),
              "position": 1, "eidolon": 0}], _enemy_obj(), 1000, 1, None)
        saber = next(x for x in state.units if x.char.id == 'saber')
        assert saber.current_energy == 360 * 0.6  # 50% 开局 → 行迹2 抬到 60%
        assert saber.extra.get('saber_resonance') == 1  # 天赋
        assert saber.extra.get('saber_magic_release')  # 行迹1


class TestSkillBranch:
    def _state_with(self, res, energy):
        from engine.runtime import SimState
        u = _saber()
        u.extra['saber_resonance'] = res
        u.current_energy = energy
        u.char.max_energy = 360
        return SimState(enemies=[], units=[u]), u

    def test_boost_condition_and_consume(self):
        from engine.characters.saber import _saber_skill_cast
        from tests.helpers import ZERO_RES
        state, u = self._state_with(res=20, energy=220)
        e1, e2 = _enemy_obj(), _enemy_obj()
        e2.id, e2.name = 'y', 'Y'
        state.enemies = [e1, e2]
        _saber_skill_cast(state, u)
        # 20 点×8 + 220 ≥ 360 → 耗尽分支: 共振清零, 能量回满
        assert u.extra.get('saber_resonance', 0) == 0
        assert u.current_energy == 360.0
        assert '风王铁槌' in _log(state)

    def test_else_branch_gains_3(self):
        from engine.characters.saber import _saber_skill_cast
        state, u = self._state_with(res=2, energy=100)
        state.enemies = [_enemy_obj()]
        _saber_skill_cast(state, u)
        assert u.extra['saber_resonance'] == 5  # 2+3（2×8+100=116 <360 不触发）
        assert u.current_energy == 100  # 无能量变动（引擎 S2 回能在接管路径外, 单测口径）

    def test_magic_release_grants_sp_and_advance(self):
        from engine.characters.saber import _saber_skill_cast
        state, u = self._state_with(res=20, energy=220)
        state.enemies = [_enemy_obj()]
        state.skill_points = 3
        u.extra['saber_magic_release'] = True
        _saber_skill_cast(state, u)
        assert state.skill_points == 4
        assert '魔力放出' in _log(state)


class TestUltAndEnhancedBasic:
    def test_ult_sets_enhanced_basic_and_bank_clear(self):
        from engine.characters.saber import _saber_ult_cast
        from engine.runtime import SimState
        u = _saber()
        u.current_energy = 0  # 直调口径: 引擎 S2 在钩子前已清零终结技能量
        u.extra['saber_bank'] = 80.0
        state = SimState(enemies=[_enemy_obj()], units=[u])
        _saber_ult_cast(state, u)
        assert u.extra.get('saber_enhanced_basic_ready') is True
        assert u.extra.get('saber_bank', 0.0) == 0.0  # 湖之祝福清空
        assert u.current_energy == pytest.approx(80.0)  # 直调口径无 S2 回能; 仅湖之祝福银行80回充

    def test_key_rewrite_one_shot(self):
        from engine.characters.saber import _saber_key_rewrite
        from engine.runtime import SimState
        u = _saber()
        state = SimState(enemies=[], units=[u])
        assert _saber_key_rewrite(u, state, 'skill') is None
        u.extra['saber_enhanced_basic_ready'] = True
        assert _saber_key_rewrite(u, state, 'basic_attack') == 'basic_attack_enhanced'
        assert _saber_key_rewrite(u, state, 'basic_attack') is None  # 一次性

    def test_e6_refund_cadence(self):
        from engine.characters.saber import _saber_ult_cast
        from engine.runtime import SimState
        u = _saber(eidolon=6)
        for expect in (1, 0, 0, 1, 0, 0, 1):  # 第1/4/7次触发
            u.current_energy = 0
            state = SimState(enemies=[_enemy_obj()], units=[u])
            _saber_ult_cast(state, u)
            triggered = u.current_energy >= 300
            assert triggered == bool(expect), u.extra.get('saber_ult_count')
            u.current_energy = 0

    def test_e4_ult_stacks(self):
        from engine.characters.saber import _saber_ult_cast
        from engine.runtime import SimState
        u = _saber(eidolon=4)
        for i in range(1, 5):
            state = SimState(enemies=[_enemy_obj()], units=[u])
            _saber_ult_cast(state, u)
        assert u.extra['saber_e4_ult_stacks'] == 3  # 4 次封顶 3 层
        assert u.base_stats.RES_PEN['风'] == pytest.approx(0.04 * 3)  # 直调口径 INIT 未跑, 仅层叠加


class TestTalentSettle:
    def test_ally_ult_grants_buff_and_resonance(self):
        from engine.characters.saber import _saber_settle_ally_ult
        from engine.runtime import SimState
        u = _saber()
        mate = make_unit('huohuo')
        state = SimState(enemies=[], units=[u, mate])
        _saber_settle_ally_ult(mate, state, None, 'ultimate', 0)
        assert u.extra['saber_resonance'] == 3  # 直调口径无 INIT 初始值
        assert any(b.param_id == 'saber_talent_dmg' for b in u.buffs)
        _saber_settle_ally_ult(mate, state, None, 'skill', 0)
        assert u.extra['saber_resonance'] == 3  # 非终结技不触发


class TestSimulation:
    def test_solo_battle_mechanics_light_up(self):
        s = _sim(max_av=2500)
        log = _log(s)
        assert '炉心共鸣' in log
        assert '风王铁槌' in log
        assert '誓约胜利之剑' in log  # 2500AV 内至少一次终结技
        assert s.units[0].total_damage_dealt > 0

    def test_e6_long_window_full_chain(self):
        s = _sim(eidolon=6, max_av=2500)
        log = _log(s)
        assert '解放的金色王权' in log   # 终结技→强化普攻链路
        assert '湖之祝福' in log or '守护命运长夜' in log
