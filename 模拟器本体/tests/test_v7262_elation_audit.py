"""v7.26.2: 欢愉审计轮——裁决7（欢愉技回能5通用）+ 审计确认项 + 丙(b)消除。

甲·项目主裁决 2026-09-30（欢愉机制口径.md 第八节·7/8）:
- 裁决7: 欢愉技回能 5 通用（常规能量角色; 银狼终结技为隐藏分特殊能量不吃常规回能）
- 裁决8（审计批确认）: 开局播种/阿哈速度公式/好活到期点/单人成队 四项乙级口径确认无误
- 丙(b)消除: 真珠阵亡后行迹2/E6 两个对称开关经每回合同步点回收
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState, _use_skill  # noqa: E402
from engine.runtime import ENERGY_GAIN  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402


def _state(*units):
    st = SimState(enemies=[_enemy()], units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


class TestElationSkillEnergyUniversal:
    def test_energy_gain_table(self):
        assert ENERGY_GAIN["elation_skill"] == 5

    def test_yaoguang_elation_cast_gains_5(self):
        """爻光欢愉技: 原稿无行, 通用默认补 5。"""
        u = _unit("yaoguang")
        st = _state(u)
        e0 = u.current_energy
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        assert u.current_energy == pytest.approx(e0 + 5.0)

    def test_aventurine_elation_cast_gains_5(self):
        """砂金欢愉技: JSON 注'回能5'但旧引擎给 0——修复后到 5。"""
        u = _unit("aventurine_waveflair")
        st = _state(u)
        e0 = u.current_energy
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        assert u.current_energy == pytest.approx(e0 + 5.0)

    def test_trailblazer_elation_cast_gains_5(self):
        u = _unit("trailblazer_elation")
        st = _state(u)
        e0 = u.current_energy
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        assert u.current_energy == pytest.approx(e0 + 5.0)

    def test_special_energy_yinlang_noop(self):
        """银狼隐藏分特殊能量: 常规回能天然 no-op（裁决7排除条款）。"""
        u = _unit("yinlang")
        st = _state(u)
        hs0 = u.hidden_score
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        # 能量条不走(特殊能量); 欢愉技本身+15隐藏分照常(JSON效果)
        assert u.current_energy == pytest.approx(0.0)
        assert u.hidden_score == pytest.approx(hs0 + 15.0)


class TestAuditConfirmations:
    """裁决8: 四项乙级口径经项目主确认(2026-09-30), 行为钉扎防回归。"""

    def test_battle_start_seeding(self):
        from engine.models.character import load_character
        from engine.core.combat_engine import simulate
        random.seed(1)
        st = simulate([{"char": load_character("zhenzhu")},
                       {"char": load_character("sparxie"), "position": 2}],
                      _enemy(), max_av=50)
        # 开局: 池=欢愉角色数; 每名欢愉角色 20 层好活(2回合)
        assert st.laugh_points == pytest.approx(2.0)
        for cid in ("zhenzhu", "sparxie"):
            assert st.elation_state.get_good_show_total(cid) == pytest.approx(20.0)

    def test_aha_speed_formula(self):
        from engine.models.elation import calc_aha_speed
        assert calc_aha_speed([100.0]) == pytest.approx(80 + 100 / 5)
        assert calc_aha_speed([100.0, 90.0]) == pytest.approx(80 + 100 / 5 + 90 / 10)
        assert calc_aha_speed([100, 90, 80, 70]) == pytest.approx(
            80 + 100 / 5 + 90 / 10 + 80 / 20 + 70 / 50)

    def test_single_elation_char_full_system(self):
        from engine.models.character import load_character
        from engine.core.combat_engine import simulate
        random.seed(1)
        st = simulate([{"char": load_character("yaoguang")}],
                      _enemy(), max_av=200)
        assert st.laugh_points >= 0
        assert st.elation_state.get_good_show_total("yaoguang") > 0
        assert any("[Aha]" in line for line in st.log) or st.aha_next_av > 0


class TestZhenzhuDeathCleanup:
    def test_toggles_reclaimed_on_death(self):
        """丙(b)消除: 真珠阵亡后, 行迹2 RES 开关与 E6 抗穿开关经同步点回收。"""
        from engine.characters import zhenzhu as zz
        u = _unit("zhenzhu", eidolon=6)
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        zz._zz_gain(u, st, 10, cause="t")            # 行迹2: 持好活→全队RES+50
        u.extra["zz_dl"] = True                      # E6: 全队抗穿+20
        zz._zz_sync_e6(st)
        res_on = mate.base_stats.EFFECT_RES
        pen_on = mate.base_stats.RES_PEN_ALL
        u.is_alive = False                            # 阵亡
        zz._zz_ally_turn_start(mate, st)              # 下一我方回合开始→同步点
        assert mate.base_stats.EFFECT_RES == pytest.approx(res_on - 0.50)
        assert mate.base_stats.RES_PEN_ALL == pytest.approx(pen_on - 0.20)
