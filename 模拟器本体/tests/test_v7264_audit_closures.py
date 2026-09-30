"""v7.26.4: 审计收尾——裁决9（好活上限获得瞬间截断）+ 裁决10（藿藿门控移除）。

甲·项目主裁决 2026-09-30（欢愉机制口径.md 第八节·9/10）:
- 裁决9: 好活当赏持有上限在"获得瞬间"截断, 超出部分直接不获得（全路径含阿哈转化）
- 裁决10: 藿藿（标准丰饶治疗, cast_number=0 无欢愉技）本命AI不依赖队友命途,
  移除 ELATION_GATED; 连带修复其终结技自身回能 JSON 值(1.0→5.0, 原稿"消耗能量：
  140能量恢复：5"——此前打完大只回1点)
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState, _use_skill  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402


def _state(*units, laugh=0.0):
    st = SimState(enemies=[_enemy()], units=list(units))
    st.laugh_points = laugh
    st.extra["_elation"] = ElationSystem()
    return st


class TestGoodshowCapAtGrant:
    def test_own_gain_clamped_instantly(self):
        from engine.characters import zhenzhu as zz
        u = _unit("zhenzhu")
        st = _state(u)
        zz._zz_gain(u, st, 60, cause="t")
        assert st.elation_state.get_good_show_total("zhenzhu") == 50.0

    def test_aha_conversion_clamped_instantly(self):
        """裁决9核心场景: 池40 + 阿哈转化N=30 → 50(不是70再裁)。"""
        u = _unit("zhenzhu")
        st = _state(u, laugh=30.0)
        st.elation_state.grant_good_show("zhenzhu", 40.0, duration=-1, source="t")
        random.seed(1)
        st.extra["_elation"].execute_aha(st)
        assert st.elation_state.get_good_show_total("zhenzhu") == 50.0

    def test_full_pool_grant_returns_none_noop(self):
        u = _unit("zhenzhu")
        st = _state(u)
        st.elation_state.grant_good_show("zhenzhu", 50.0, duration=-1, source="t")
        r = st.extra["_elation"].grant_good_show(st, "zhenzhu", 15, duration=-1,
                                                 source="t2")
        assert r is None
        assert st.elation_state.get_good_show_total("zhenzhu") == 50.0

    def test_other_chars_uncapped(self):
        u = _unit("sparxie")
        st = _state(u)
        st.extra["_elation"].grant_good_show(st, "sparxie", 80, duration=2, source="t")
        assert st.elation_state.get_good_show_total("sparxie") == 80.0


class TestHuohuoGateRemoved:
    def test_custom_ai_installed_in_non_elation_team(self):
        """裁决10: 非欢愉队也装她的本命AI（不再回落默认AI）。"""
        from engine.characters import activate
        hh = _unit("huohuo")
        sl = _unit("seele", position=2)
        st = _state(hh, sl)
        ai = activate(st, ["huohuo", "seele"], elation_active=False)
        assert "huohuo" in ai

    def test_ult_self_regen_5(self):
        """原稿"消耗能量140·能量恢复5"——JSON 行修正后打完大自回5。"""
        hh = _unit("huohuo")
        st = _state(hh)
        hh.current_energy = hh.char.max_energy
        random.seed(1)
        _use_skill(hh, st, "ultimate")
        assert hh.current_energy == pytest.approx(5.0)

    def test_ult_teammate_regen_20pct_any_team(self):
        """终结技队友回能20%在非欢愉队同样生效且单次——本命AI触发,
        回能/ATK buff 全由 post_effects 相位单源结算(此前AI内联+相位双算=40%)。"""
        hh = _unit("huohuo")
        sl = _unit("seele", position=2)
        st = _state(hh, sl)
        from engine.characters.huohuo import _hh_ai
        hh.current_energy = hh.char.max_energy
        e0 = sl.current_energy
        random.seed(1)
        _hh_ai(hh, st, elation=st.extra["_elation"])
        assert sl.current_energy == pytest.approx(
            min(sl.char.max_energy, e0 + sl.char.max_energy * 0.20))
        atk = [b for b in sl.buffs if getattr(b, "param_id", "") == "huohuo_ult_atk"]
        assert len(atk) == 1  # 单源不叠
        expect_atk = 64.0 if (sl.char.max_energy or 0) >= 160 else 40.0
        assert atk[0].attributes["ATK_PERCENT"] == expect_atk

    def test_team_sim_non_elation_runs(self):
        from engine.core.combat_engine import simulate
        random.seed(1)
        st = simulate([{"char": load_character("huohuo")},
                       {"char": load_character("seele"), "position": 2}],
                      _enemy(), max_av=400)
        assert all(x.is_alive for x in st.units)
        assert any("藿藿终结技" in line for line in st.log)
