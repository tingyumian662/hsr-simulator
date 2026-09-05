"""v7.19.1 修复批（项目主 2026-09-05 批准的四项缺陷修复）。

- 批1 光锥文件名对齐 id: 7 个中文文件名 JSON 重命名为英文 id——engine 侧
  load_lightcone 硬约定 文件名==id, 此前 smoke t3 三角色（银狼Lv.999/绯英/爻光）
  推荐光锥静默裸装（lc missing 警告）; stem==id 全量钉扎防再错位。
- 批2 P3 三连修: hp_crit_convert per_points 0/None 兜底 100（防除零）;
  /api/recommend 角色单轮加载; team_path_passives 未知 stats 键 fail-loud。
- 批3 API 层 JSON 缓存: (path, mtime_ns, size) 键控 dict 缓存 + 目录签名 lru
  派生表（光锥索引/套装表/角色对象）——四端点零重复读盘。
"""
import json
from pathlib import Path

import pytest

from engine.models.equipment import load_lightcone
from engine.core.attributes import CombatStats
from engine.core.relic_optimizer import (
    _marginal_benefit, _analyze_character, recommend_substats_full,
)
from engine.constants import SUB_STAT_VALUES, SUBSTAT_ROLL_FACTOR

LC_DIR = Path(__file__).resolve().parent.parent / "data" / "light_cones"
MID_HP = SUB_STAT_VALUES["HP_percent"][1] * SUBSTAT_ROLL_FACTOR

RENAMED_IDS = ["when_she_decided_to_see", "time_waits_for_no_one",
               "elation_overflow_blessing", "welcome_to_galaxy_city",
               "dazzling_world", "gugu_gaga_adventure", "encounter_next_bloom"]


class TestLightconeFileAlignment:
    def test_all_filenames_equal_ids(self):
        """全量钉扎: 文件名 stem == 内部 id（_template 豁免）——防未来再错位。"""
        bad = []
        for f in LC_DIR.glob("*.json"):
            if f.stem.startswith("_"):
                continue
            if json.loads(f.read_text(encoding="utf-8")).get("id") != f.stem:
                bad.append(f.name)
        assert not bad, f"filename != id: {bad}"

    def test_renamed_ids_load_via_engine(self):
        for lc_id in RENAMED_IDS:
            lc = load_lightcone(lc_id)
            assert lc.id == lc_id

    def test_lightcone_characters_map_uses_display_names(self):
        """映射文件键 = 光锥显示名（name 字段, 非文件 id）——全量钉扎口径。"""
        raw = json.loads((LC_DIR.parent / "light_cone_characters.json")
                         .read_text(encoding="utf-8"))
        names = {json.loads(f.read_text(encoding="utf-8")).get("name")
                 for f in LC_DIR.glob("*.json") if not f.stem.startswith("_")}
        unknown = [k for k in raw if k not in names]
        assert not unknown, f"map keys not in lightcone name pool: {unknown}"


class TestHpConvertGuard:
    def _side_value(self, per_points):
        from engine.models.character import load_character
        char = load_character("mydei")
        char.hp_crit_convert = {"threshold": 4000, "per_points": per_points,
                                "crit_rate_pct": 1.2, "max_points": 4000}
        profile = _analyze_character(char)
        s = CombatStats()
        s.HP, s.ATK = 5000.0, 1000.0
        s._base_HP = 2000.0
        s.CRIT_RATE, s.CRIT_DMG = 0.5, 1.0
        return _marginal_benefit(s, char, profile, "HP_percent", MID_HP)

    def test_zero_per_points_falls_back_to_hundred(self):
        """per_points=0/None 数据误写不除零, 数值等效 100（审核积压 P3-1）。"""
        assert self._side_value(0) == pytest.approx(self._side_value(100))


class TestTeamPassiveFailLoud:
    def test_unknown_stat_key_raises(self):
        from engine.models.character import load_character
        char = load_character("anaxa")
        char.team_path_passives = [{"path": "智识", "count_lt": 2,
                                    "stats": {"DMG_BONUS_ALL": 50.0}}]
        with pytest.raises(ValueError, match="unsupported stat key"):
            recommend_substats_full(char, team_paths=["智识", "丰饶"])

    def test_known_keys_still_work(self):
        from engine.models.character import load_character
        char = load_character("anaxa")
        r = recommend_substats_full(char, team_paths=["智识", "丰饶"])["rolls"]
        assert r["CRIT_RATE"] == 18  # CD+140 生效口径与 v7.19.0 一致


@pytest.fixture(autouse=True)
def _clear_caches():
    import web.api as api
    caches = (api._JSON_CACHE, api._lightcone_index, api._lightcone_obj,
              api._character_obj, api._relic_sets)
    for c in caches:
        if hasattr(c, "cache_clear"):
            c.cache_clear()
        else:
            c.clear()
    yield
    for c in caches:
        if hasattr(c, "cache_clear"):
            c.cache_clear()
        else:
            c.clear()


class TestApiJsonCache:
    def test_second_read_hits_cache(self, monkeypatch):
        """同文件二次调用零读盘（json.load 仅一次）。"""
        import web.api as api
        calls = []
        orig = json.load
        monkeypatch.setattr(api.json, "load",
                            lambda fh: (calls.append(1), orig(fh))[1])
        fp = api.DATA_DIR / "recommendations.json"
        a = api._load_json_cached(fp)
        b = api._load_json_cached(fp)
        assert a is b
        assert len(calls) == 1

    def test_mtime_bump_invalidates(self, monkeypatch):
        """签名变化（mtime_ns 漂移）触发重读。"""
        import web.api as api
        calls = []
        orig = json.load
        monkeypatch.setattr(api.json, "load",
                            lambda fh: (calls.append(1), orig(fh))[1])
        fp = api.DATA_DIR / "recommendations.json"
        real_stat = api.os.stat
        fake_ns = [1000]

        class _St:
            st_mtime_ns = 1000
            st_size = 0

        def stat(p, *a, **kw):
            if str(p).endswith("recommendations.json"):
                _St.st_mtime_ns = fake_ns[0]
                return _St
            return real_stat(p, *a, **kw)

        monkeypatch.setattr(api.os, "stat", stat)
        api._load_json_cached(fp)
        fake_ns[0] = 2000
        api._load_json_cached(fp)
        assert len(calls) == 2

    def test_dir_sig_changes_on_file_set(self):
        import web.api as api
        sig = api._dir_sig("relics")
        assert sig and all(isinstance(m, int) for _, m in sig)

    def test_derived_tables_cached_by_identity(self):
        import web.api as api
        assert api._relic_sets(api._dir_sig("relics")) is \
               api._relic_sets(api._dir_sig("relics"))
        assert api._lightcone_index(api._dir_sig("light_cones")) is \
               api._lightcone_index(api._dir_sig("light_cones"))
        assert api._character_obj("anaxa", api._dir_sig("characters")) is \
               api._character_obj("anaxa", api._dir_sig("characters"))

    def test_endpoint_stack_end_to_end(self):
        """四端点全链路（缓存/单轮加载/改名光锥）端到端可用。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.api import router
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)

        listing = client.get("/api/list").json()
        assert listing["recommendations"]["yinlang"]["light_cone"] == \
            "welcome_to_galaxy_city"
        for _ in range(2):  # 二连发验证缓存命中路径不破坏响应
            assert client.get("/api/list").status_code == 200

        resp = client.post("/api/recommend", json={
            "team": [{"char_id": "anaxa", "lc_id": "time_waits_for_no_one"}]})
        assert resp.status_code == 200
        assert resp.json()["recommendations"][0]["rolls"]["CRIT_RATE"] == 18
