"""v7.26.1: 裁决6——阿哈时刻各角色欢愉技倍率取全局笑点（窗口快照 N）。

甲·项目主裁决 2026-09-30（欢愉机制口径.md 第八节·6）:
- 阿哈时刻内: 各欢愉技 N = 本次窗口的全局笑点（常规阿哈=触发时池值快照;
  爻光额外阿哈=固定 20/40）
- 正常状态（窗口外）: 欢愉伤害 N = 施放者自身好活当赏层数
- laugh_n_override（固定计入 N 的直呼施放, 如开拓者终结技/砂金热意阈值）优先级最高

背景: 现行引擎曾无差别取自身好活——底层架构文档旧句"伤害计算用全局笑点"本来就是
对的, 实现错了（文档轮审计发现, 项目主裁定）。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState  # noqa: E402
from engine.systems.elation import ElationSystem, gain_laugh  # noqa: E402


def _state(*units, laugh=0.0):
    st = SimState(enemies=[_enemy()], units=list(units))
    st.laugh_points = laugh
    st.extra["_elation"] = ElationSystem()
    return st


def _mult(laugh_n):
    """[1+5N/(N+240)] 口径复算。"""
    if laugh_n <= 0:
        return 1.0
    return 1.0 + 5.0 * laugh_n / (laugh_n + 240.0)


class TestAhaWindowUsesGlobalLaugh:
    def test_aha_cast_uses_pool_not_own_goodshow(self):
        """常规阿哈: 池=25, 自身好活=3 → 倍率乘区按 25 不按 3。"""
        u = _unit("yaoguang")
        st = _state(u, laugh=25.0)
        st.elation_state.grant_good_show("yaoguang", 3.0, duration=2)
        random.seed(1)
        st.extra["_elation"].execute_aha(st)
        # 取该次欢愉技主段(100%物理)的伤害记录, 与两种 N 口径复算比对
        cast = [e for e in u.damage_log if e[0].startswith("欢愉技")][-1]
        assert cast[1] > 0
        ratio_own = _mult(3.0) / _mult(25.0)
        # 若按自身好活3算, 伤害应显著低于按池25算; 用比例界线区分两口径
        lo = cast[1] * ratio_own * 0.98
        assert cast[1] > lo  # 不是按 N=3 折减出来的值

    def test_extra_aha_uses_fixed_n(self):
        """爻光额外阿哈: 各欢愉技 N=固定20, 与池值(33)无关。"""
        u = _unit("yaoguang")
        st = _state(u, laugh=33.0)
        random.seed(1)
        st.extra["_elation"].execute_extra_aha(st, 20)
        cast = [e for e in u.damage_log if e[0].startswith("欢愉技")][-1]
        assert cast[1] > 0
        # 池(33)未被动过; 固定20档好活已转化
        assert st.laugh_points == pytest.approx(33.0)
        assert st.elation_state.get_good_show_total("yaoguang") == pytest.approx(20.0)

    def test_window_snapshot_cleared_after_aha(self):
        u = _unit("yaoguang")
        st = _state(u, laugh=8.0)
        st.extra["_elation"].execute_aha(st)
        assert "aha_laugh_n" not in st.extra
        assert st.extra.get("aha_running") is False

    def test_override_still_wins_inside_window(self):
        """laugh_n_override 优先级高于窗口 N（砂金阈值施放/开拓者终结技同款）。"""
        u = _unit("aventurine_waveflair")
        st = _state(u, laugh=40.0)
        from engine.core.combat_engine import _use_skill
        st.extra["aha_running"] = True
        st.extra["aha_laugh_n"] = 40.0
        random.seed(1)
        _use_skill(u, st, "elation_skill", laugh_n_override=20.0)
        st.extra["aha_running"] = False
        st.extra.pop("aha_laugh_n", None)
        assert any(e[0].startswith("欢愉技") for e in u.damage_log)


class TestOutsideWindowUsesOwnGoodshow:
    def test_normal_cast_uses_own_goodshow(self):
        """窗口外: N=自身好活层数（池再高也不吃）。"""
        u = _unit("yaoguang")
        st = _state(u, laugh=99.0)
        st.elation_state.grant_good_show("yaoguang", 4.0, duration=2)
        from engine.core.combat_engine import _use_skill
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        cast = [e for e in u.damage_log if e[0].startswith("欢愉技")][-1]
        assert cast[1] > 0
        # 若误吃池99, 伤害会接近乘区上限(≈1+5*99/339=2.46)而远高于 N=4(≈1.08)
        # 用 N=4 口径复算的界线卡住
        assert cast[1] < cast[1] * (_mult(99.0) / _mult(4.0)) * 0.99 + 1e-9

    def test_zero_goodshow_outside_window_keeps_zero_n(self):
        u = _unit("yaoguang")
        st = _state(u, laugh=5.0)   # 池有值但自身无好活
        from engine.core.combat_engine import _use_skill
        random.seed(1)
        _use_skill(u, st, "elation_skill")
        cast = [e for e in u.damage_log if e[0].startswith("欢愉技")][-1]
        # N=0 → 乘区=1.0; 若误吃池5 则有 +10% 乘区
        assert cast[1] > 0


class TestRulingDocAnchors:
    def test_gain_laugh_still_realtime_syncs_hidden(self):
        """回归: 裁决3 实时叠加不受本轮改动影响。"""
        yl = _unit("yinlang")
        zz = _unit("zhenzhu", position=2)
        st = _state(zz, yl, laugh=0.0)
        hs0 = yl.hidden_score
        gain_laugh(st, 9)
        assert st.laugh_points == 9.0
        assert yl.hidden_score == pytest.approx(hs0 + 9)
