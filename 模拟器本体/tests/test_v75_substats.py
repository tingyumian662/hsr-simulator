"""v7.5.0 副词条分布现实化回归（项目主指示）。

> 按你建议处理。另外，每一个词条的数值可以适当调高一些（比如1.1/1.2）

三项口径:
1. 单词条 ≤ 有效词条数×60%（SUBSTAT_SINGLE_SHARE）——"30 条全速度"不真实, 强制混合
2. SPD 跨档收益第二档起 ×0.5（抑制追档链条）
3. 词条数值 ×1.1（SUBSTAT_ROLL_FACTOR, 小毕业平均档; 推荐器与预览/模拟同口径）
"""
import pytest

from engine.constants import SUBSTAT_ROLL_FACTOR, SUB_STAT_VALUES, StatType
from engine.core.attributes import compute_combat_stats
from engine.core.relic_optimizer import (
    _analyze_character, _marginal_benefit, _mid, recommend_substats,
    recommend_substats_full,
)
from engine.models.character import load_character
from engine.models.equipment import RelicPiece

DATA = "data/characters"


def _load(cid):
    return load_character(cid, DATA)


class TestSingleShareCap:
    """v7.5: 单词条 ≤60% 有效词条（mandatory 约束达标不受限）"""

    @pytest.mark.parametrize("cid", ["yinlang", "yaoguang", "seele", "huohuo", "dan_heng_permansor_terrae"])
    def test_no_monostat_stacking(self, cid):
        r = recommend_substats(_load(cid))  # 有效 30 → 单词条 ≤18
        for k, v in r.items():
            if k in ("SPD_PERCENT", "HP_percent", "ATK_percent", "CRIT_RATE", "CRIT_DMG"):
                assert v <= 18, (cid, k, v)

    def test_yinlang_mixed_build(self):
        # 报障主例: 30 有效词条不再全速度——速度 ≤18 且双暴有实质条数
        r = recommend_substats(_load("yinlang"))
        assert r["SPD_PERCENT"] <= 18
        assert r["CRIT_RATE"] + r["CRIT_DMG"] >= 5

    def test_cap_50_budget(self):
        # 有效 50 → 单词条 ≤30（全覆盖时不再额外收紧）
        r = recommend_substats(_load("seele"), effective_rolls=50)
        assert max(r.values()) <= 30

    def test_mandatory_exempts_cap(self):
        # 火花 ATK>2000 阈值是配装需求: mandatory 不受 60% 上限（面板 640 无主词条时需全预算）
        r = recommend_substats(_load("sparxie"))
        assert r["ATK_percent"] > 18


class TestTierDiscount:
    """v7.5: SPD 跨档收益第二档起 ×0.5"""

    def test_second_tier_half(self):
        ch = _load("seele")
        p = _analyze_character(ch)
        s1 = compute_combat_stats(ch)
        s2 = compute_combat_stats(ch)
        s1.SPD = 131.0   # 第一档内（1→2 动）: 全额
        s2.SPD = 198.0   # 第二档内（2→3 动）: 减半
        g1 = _marginal_benefit(s1, ch, p, "SPD_percent", 3.0)
        g2 = _marginal_benefit(s2, ch, p, "SPD_percent", 3.0)
        # 同为"距断点1条"时, 第二档收益≈第一档的一半（e_old 不同于精确 0.5, 用区间断言）
        assert g1 > 0 and g2 > 0
        ratio = g2 / g1
        assert 0.35 < ratio < 0.75, ratio


class TestRollFactor:
    """v7.5: 词条数值 ×1.1（推荐器与预览/模拟同口径）"""

    def test_mid_value_scaled(self):
        assert _mid("CRIT_RATE") == pytest.approx(3.0 * 1.1)
        assert _mid("SPD_percent") == pytest.approx(3.0 * 1.1)

    def test_factor_value(self):
        assert SUBSTAT_ROLL_FACTOR == pytest.approx(1.1)

    def test_preview_uses_same_factor(self):
        # 预览/模拟路径: 10 条命中 → +10×2.5×1.1 = 27.5%（黑天鹅行迹 10 + 27.5 = 37.5）
        from engine.models.equipment import RelicPiece
        ch = _load("black_swan")
        sub = {'EFFECT_HIT_RATE': 10 * 2.5 * SUBSTAT_ROLL_FACTOR / 6.0}
        pieces = [RelicPiece(slot=s, set_name='', main_stat_type='', main_stat_value=0,
                             sub_stats=sub)
                  for s in ['head', 'hands', 'body', 'feet', 'planar_sphere', 'link_rope']]
        stats = compute_combat_stats(ch, None, pieces, {})
        assert stats.EFFECT_HIT_RATE == pytest.approx(0.10 + 10 * 2.5 * 1.1 / 100.0)


class TestRealisticShapes:
    """v7.5: 关键角色的混合形态快照"""

    def test_seele_crit_mix(self):
        r = recommend_substats(_load("seele"))
        assert r["CRIT_RATE"] == 18 and r["CRIT_DMG"] == 12  # 单属性 cap 后 1:2 自然混合

    def test_boothill_spd_be_mix(self):
        r = recommend_substats(_load("boothill"))
        assert r["SPD_PERCENT"] >= 8 and r["BREAK_EFFECT"] >= 10
        assert r["SPD_PERCENT"] <= 18


class TestManualInputUncapped:
    """v7.5.1（项目主裁决）: 单词条 60% 规定仅作用于推荐算法——用户手动词条不受限"""

    def test_rule_only_in_recommender(self):
        # 常量只被 recommend 的 stat_caps 引用（推荐算法内部）
        import engine.core.relic_optimizer as ro
        import inspect
        src = inspect.getsource(ro)
        assert src.count("SUBSTAT_SINGLE_SHARE") == 2  # import + stat_caps 单处使用

    def test_manual_rolls_flow_uncapped(self):
        # 手动 40 条暴击（超过 18/30 推荐上限）在预览/模拟路径无任何钳制
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from web.api import router
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)
        body = {"team": [{"char_id": "seele", "lc_id": None, "relics": {},
                          "substats": {"SPD_PERCENT": 40}}],
                "enemy": {"hp": 1, "def": 1, "toughness": 1, "weakness": [], "count": 1},
                "max_av": 1}
        pv = client.post("/api/preview", json=body).json()["previews"][0]
        import engine.constants as C
        from engine.models.character import load_character
        base_spd = load_character("seele", "data/characters").base_SPD
        expected = base_spd + 40 * 3.0 * C.SUBSTAT_ROLL_FACTOR  # 速度无上限, 40 条全额生效
        assert pv["SPD"] == pytest.approx(expected, abs=0.2)

    def test_frontend_input_max_50(self):
        from pathlib import Path
        # v7.17.0: 前端脚本迁 web/static/app.js, 断言改读该文件
        js = (Path(__file__).resolve().parents[1]
              / "web" / "static" / "app.js").read_text(encoding="utf-8")
        # 词条输入上限=总词条50（不受单词条推荐上限约束）
        assert 'min="0" max="50"' in js
        assert "手动调整不受单词条上限约束" in js
