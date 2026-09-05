"""v7.20.0: 光锥叠影选择——档位审计 + 前后端映射通路。

项目主三项裁决（2026-09-06）: ①五档数据等 txt 补录（不重抓不臆造）; ②无五档数据
光锥可选+标注"单档·按S{N}计算"; ③后续 txt 录入光锥数据时须重新核对硬编码 handler 数值。

档位审计结论（批0, 证据=JSON 顶层 rank 字段/_note/git 提交信息）:
- values 五档 7 把 + 决心如汗珠般闪耀（rank 算术 handler）→ rank_scaled, 默认按稀有度;
- 单档: JSON rank 字段即录入校准档（批量 4★=65 把 S5 / 抽卡 5★=66 把 S1 / 商店赠送
  5★=7 把 S5 / landaus_choice 4★ S1 / 欢愉满溢祝福 5★ S5）, 数值不随叠影缩放。
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web" / "static" / "app.js"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _preview(lc_id=None, lc_rank=None, char_id="robin"):
    body = {"team": [{"char_id": char_id}], "enemy": {"hp": 1}, "max_av": 1}
    if lc_id is not None:
        body["team"][0]["lc_id"] = lc_id
    if lc_rank is not None:
        body["team"][0]["lc_rank"] = lc_rank
    resp = _client().post("/api/preview", json=body)
    assert resp.status_code == 200
    return resp.json()["previews"][0]


class TestRankInfoListing:
    """/api/list 光锥条目的档位数据面（前端默认档与徽标的数据源）。"""

    def _lcs(self):
        resp = _client().get("/api/list")
        assert resp.status_code == 200
        return {l["id"]: l for l in resp.json()["light_cones"]}

    def test_fields_present_and_pinned_samples(self):
        lcs = self._lcs()
        # 缩放光锥: values 五档（4★→默认5 / 5★→默认1）+ rank 算术 handler
        assert lcs["swordplay"]["rank_scaled"] is True
        assert lcs["swordplay"]["default_rank"] == 5
        assert lcs["rise_and_sing"]["rank_scaled"] is True
        assert lcs["rise_and_sing"]["default_rank"] == 1
        assert lcs["resolution_shines_as_pearls_of_sweat"]["rank_scaled"] is True
        # 单档光锥: 默认=录入校准档（JSON rank 字段）
        assert lcs["dance_dance_dance"] == {**lcs["dance_dance_dance"],
                                            "rank_scaled": False, "default_rank": 5}
        assert lcs["in_the_night"]["rank_scaled"] is False
        assert lcs["in_the_night"]["default_rank"] == 1
        assert lcs["gugu_gaga_adventure"]["default_rank"] == 5  # 录入=叠五(0e38553)
        assert lcs["landaus_choice"]["default_rank"] == 1       # 4★ 例外: S1 校准
        assert lcs["elation_overflow_blessing"]["default_rank"] == 5  # 商店/赠送类 5★

    def test_tier_distribution(self):
        lcs = self._lcs()
        scaled = [l for l in lcs.values() if l["rank_scaled"]]
        assert len(scaled) == 8  # 7 values + 决心如汗珠般闪耀
        singles = [l for l in lcs.values() if not l["rank_scaled"]]
        assert len(singles) == 135  # 用户可见 143 把（_template 不入列表）
        assert all(1 <= l["default_rank"] <= 5 for l in lcs.values())


class TestRankEndToEnd:
    def _preview_hp(self, rank):
        # 起身歌唱=记忆光锥 → 用记忆角色晴歌（Step5 效果需命途匹配）
        p = _preview(lc_id="rise_and_sing", lc_rank=rank, char_id="robin_summeretto")
        return p["HP"]

    def test_rank_scales_values_lightcone(self):
        """起身歌唱 HP% 走 Step5 values 五档: S1 vs S5 面板 HP 必须不同。"""
        hp1, hp5 = self._preview_hp(1), self._preview_hp(5)
        assert hp5 > hp1

    def test_cache_isolation_between_ranks(self):
        """lru 缓存对象按 rank 隔离: 同 id 交替 rank 不串档。"""
        a = self._preview_hp(1)
        b = self._preview_hp(5)
        c = self._preview_hp(1)
        assert a == c and b > a

    def test_invalid_rank_rejected(self):
        resp = _client().post("/api/preview", json={
            "team": [{"char_id": "robin_summeretto", "lc_id": "rise_and_sing", "lc_rank": 6}],
            "enemy": {"hp": 1}, "max_av": 1})
        assert resp.status_code == 422

    def test_rank_without_lightcone_ignored(self):
        assert _preview(lc_rank=5)["HP"] > 0

    def test_omitted_rank_zero_drift(self):
        """不传 lc_rank = JSON 默认档（旧口径逐位一致, smoke 零漂移的 API 面）。"""
        omitted = _preview(lc_id="in_the_night", char_id="seele")
        explicit = _preview(lc_id="in_the_night", lc_rank=1, char_id="seele")
        assert omitted == explicit
        raw = json.loads((ROOT / "data" / "light_cones" / "in_the_night.json")
                         .read_text(encoding="utf-8"))
        assert raw["rank"] == 1  # JSON 默认档=1（该光锥 S1 校准）


class TestFrontendPins:
    """app.js 结构钉扎（v7.17.0 口径: 派生用法与挂接点存在性）。"""

    def test_rank_control_and_payload(self):
        src = APP_JS.read_text(encoding="utf-8")
        assert 'id="rank${i}"' in src                      # 叠影下拉控件
        assert "[1,2,3,4,5].map(n=>`<option>${n}</option>`)" in src
        assert 'onchange="syncRankDefault(${i})"' in src   # 光锥变化触发
        assert "function syncRankDefault(i)" in src        # 默认档函数
        assert "lc.default_rank" in src                    # /api/list 档位消费
        assert "单档·按S" in src                            # 徽标文案
        assert "lc_rank: parseInt(document.getElementById(`rank${i}`).value)||null" in src

    def test_sync_default_reset_points(self):
        src = APP_JS.read_text(encoding="utf-8")
        # onCharChange 清空光锥后重置 + 推荐套用后设默认（两处调用）+ 模板 onchange 挂接
        assert src.count("syncRankDefault(i)") >= 3
