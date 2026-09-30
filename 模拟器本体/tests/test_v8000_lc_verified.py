"""v8.0.0 开版轮: 光锥甲组三把项目主核实订正（2026-09-30）。

1. 欢愉满溢祝福(黑塔商店, 非专属): ATK20-40 五档 + 我方单体战技/终结技→目标
   欢愉度12-24% 2回合（此前 state 门控行未接线, 效果从未生效）
2. 制胜的瞬间: DEF/EHR/受击DEF 三行 24-40 五档（杰帕德专属, 结构确认无误）
3. 欢迎来到银河城(银狼): SPD18-30 + 欢愉伤害无视防20-36(elation专属) +
   对自身终结技→+20-40笑点(1次, 3普攻重置; 裁决15: 笑点=银狼隐藏分等价,
   净扣 60-20行迹-20光锥=20)
连带修复: 银狼 silver_ult 直调路径此前不发光锥 on_ult 事件(终结技光锥对其失效)。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import (  # noqa: E402
    SimState, _lc_galaxy_count_basic, _lc_galaxy_ult_laugh,
    _lc_rank_value)
from engine.models.equipment import load_lightcone  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402

GALAXY = 'welcome_to_galaxy_city'
EOB = 'elation_overflow_blessing'
MOV = 'moment_of_victory'


def _state(*units):
    st = SimState(enemies=[_enemy()], units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


class TestData:
    def test_three_lcs_have_five_tiers(self):
        for lc_id, codes in ((GALAXY, ['event_ult_after', 'state_galaxy_elation_defpen']),
                             (EOB, ['event_ally_targeted']),
                             (MOV, [])):
            d = load_lightcone(lc_id)
            scaled = [e for e in d.effects if e.values]
            assert scaled, lc_id
            for e in scaled:
                assert len(e.values) == 5, (lc_id, e.condition_code)

    def test_moment_of_victory_rows(self):
        d = load_lightcone(MOV)
        rows = {(e.condition_code or 'passive', tuple(sorted((e.attributes or {}).keys()))): e.values
                for e in d.effects if e.values}
        assert rows[('passive', ('DEF_percent', 'EFFECT_HIT_RATE'))] == [24, 28, 32, 36, 40]
        assert rows[('event_hit_taken', ('DEF_percent',))] == [24, 28, 32, 36, 40]


class TestGalaxyCity:
    def test_ult_grants_laugh_and_hidden_equivalence(self):
        """裁决15: 终结技→+20笑点, 池+20 且银狼隐藏分实时+20(等价性)。"""
        u = _unit('yinlang', lc_id=GALAXY)
        st = _state(u)
        hs0, lp0 = u.hidden_score, st.laugh_points
        _lc_galaxy_ult_laugh(st, u)
        assert st.laugh_points == pytest.approx(lp0 + 20.0)
        assert u.hidden_score == pytest.approx(hs0 + 20.0)  # 实时叠加等价
        assert u.extra['galaxy_laugh_armed'] is False

    def test_once_lock_and_three_basics_rearm(self):
        u = _unit('yinlang', lc_id=GALAXY)
        st = _state(u)
        st.laugh_points = 0.0
        _lc_galaxy_ult_laugh(st, u)
        lp = st.laugh_points
        _lc_galaxy_ult_laugh(st, u)  # 已触发, 不再给
        assert st.laugh_points == pytest.approx(lp)
        _lc_galaxy_count_basic(st, u)
        _lc_galaxy_count_basic(st, u)
        assert u.extra.get('galaxy_laugh_armed') is False
        _lc_galaxy_count_basic(st, u)  # 第3次普攻→重臂
        assert u.extra['galaxy_laugh_armed'] is True
        _lc_galaxy_ult_laugh(st, u)
        assert st.laugh_points == pytest.approx(lp + 20.0)

    def test_ult_net_cost_20(self):
        """银狼开大净扣20: 60 - 行迹20 - 光锥20(经笑点实时同步回隐藏分)。"""
        from engine.characters.yinlang import silver_ult
        u = _unit('yinlang', lc_id=GALAXY)
        st = _state(u)
        u.hidden_score = 120.0
        silver_ult(u, st)
        assert u.hidden_score == pytest.approx(120.0 - 60.0 + 20.0 + 20.0)
        assert st.laugh_points == pytest.approx(20.0)  # 全队池+20

    def test_elation_defpen_scoped(self):
        u = _unit('yinlang', lc_id=GALAXY)
        u.lightcone.rank = 5
        st = _state(u)
        s = st.extra['_elation'].eff_stats(u, state=st)
        assert s.DEF_PEN_BY_TYPE.get('elation') == pytest.approx(0.36)
        assert s.DEF_PEN == pytest.approx(0.0)  # 不漏到全伤害类型


class TestElationOverflow:
    def test_ally_targeted_buff_fires(self):
        u = _unit('trailblazer_elation', lc_id=EOB)
        u.lightcone.rank = 5
        mate = _unit('sparxie', position=2)
        st = _state(u, mate)
        u.extra['lc_last_skill_target'] = mate
        from engine.core.combat_engine import _lc_elation_overflow_buff
        _lc_elation_overflow_buff(st, u)
        buffs = [b for b in mate.buffs
                 if getattr(b, 'param_id', '') == 'lc_eob_elation']
        assert buffs and buffs[0].attributes['ELATION_LEVEL'] == 24.0
        assert buffs[0].remaining_turns == 2

    def test_no_target_no_fire(self):
        u = _unit('trailblazer_elation', lc_id=EOB)
        mate = _unit('sparxie', position=2)
        st = _state(u, mate)
        from engine.core.combat_engine import _lc_elation_overflow_buff
        _lc_elation_overflow_buff(st, u)  # 无 lc_last_skill_target
        assert not [b for b in mate.buffs
                    if getattr(b, 'param_id', '') == 'lc_eob_elation']

    def test_wrong_wearer_no_fire(self):
        u = _unit('seele', lc_id=EOB)  # 命途不匹配
        mate = _unit('sparxie', position=2)
        st = _state(u, mate)
        u.extra['lc_last_skill_target'] = mate
        from engine.core.combat_engine import _lc_elation_overflow_buff
        _lc_elation_overflow_buff(st, u)
        assert not [b for b in mate.buffs
                    if getattr(b, 'param_id', '') == 'lc_eob_elation']
