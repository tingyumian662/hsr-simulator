"""v7.19.0: 组队条件型常驻被动 + 生命阈值→暴击率换算（项目主 2026-09-04 两项裁决）。

- 万敌: combat_passive_stats 按满额 48% 入预算（血祥罩衫, 静态口径）; hp_crit_convert
  让生命条按"每100生命附带1.2%暴击率"加权, 三重边界=阈值4000以下不计/超出4000点不计/
  暴击率满100%无增量; 阈值达标走 atk_threshold 同款 mandatory 硬优先
  （裸装缺口吃满有效词条, 与刻律德菈 ATK=30 先例一致）。
- 那刻夏: team_path_passives——推荐按队伍命途计数应用（智识<2 → 自身CD+140%）;
  无队伍上下文不应用; /api/recommend 传入完整队伍（含自身）。
"""
import json
from pathlib import Path

import pytest

from engine.constants import SUB_STAT_VALUES, SUBSTAT_ROLL_FACTOR
from engine.models.character import load_character
from engine.models.equipment import RelicPiece
from engine.core.attributes import CombatStats, compute_combat_stats
from engine.core.relic_optimizer import (
    recommend_substats_full, _marginal_benefit, _analyze_character,
    _expected_output,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "characters"
MID_CR = SUB_STAT_VALUES["CRIT_RATE"][1] * SUBSTAT_ROLL_FACTOR
MID_HP = SUB_STAT_VALUES["HP_percent"][1] * SUBSTAT_ROLL_FACTOR


def _mydei_pieces():
    """标准 HP 缩放配装（头/手 + HP 绳球）——面板 HP 约 3439。"""
    mk = lambda slot, st, v: RelicPiece(
        slot=slot, set_name="", rarity=5, level=15, main_stat_type=st, main_stat_value=v)
    return [mk("head", "HP_FLAT", 705), mk("hands", "ATK_FLAT", 352),
            mk("link_rope", "HP_percent", 51.8), mk("planar_sphere", "HP_percent", 51.8)]


class TestDataContract:
    def test_mydei_declares_budget_and_convert(self):
        raw = json.loads((DATA_DIR / "mydei.json").read_text(encoding="utf-8"))
        assert raw["combat_passive_stats"] == {"CRIT_RATE": 48.0}
        assert raw["hp_crit_convert"] == {
            "threshold": 4000, "per_points": 100,
            "crit_rate_pct": 1.2, "max_points": 4000,
            "_note": "血祥罩衫按满额48%入推荐预算（项目主 2026-09-04 裁决）; "
                     "生命条按每100生命附带1.2%暴击率加权（窗口4000~8000）"}

    def test_anaxa_declares_team_passive(self):
        raw = json.loads((DATA_DIR / "anaxa.json").read_text(encoding="utf-8"))
        rule = raw["team_path_passives"][0]
        assert (rule["path"], rule["count_lt"]) == ("智识", 2)
        assert rule["stats"] == {"CRIT_DMG": 140.0}

    def test_roster_exclusive_holders(self):
        """全库仅万敌/那刻夏持有新字段（消费面数据契约）。"""
        holders = {"hp_crit_convert": [], "team_path_passives": []}
        for f in DATA_DIR.glob("*.json"):
            raw = json.loads(f.read_text(encoding="utf-8"))
            if raw.get("id") != f.stem:
                continue
            for k in holders:
                if raw.get(k):
                    holders[k].append(raw["id"])
        assert holders == {"hp_crit_convert": ["mydei"],
                           "team_path_passives": ["anaxa"]}


class TestMydeiBudget:
    def test_bare_load_mandatory_hp(self):
        """裸装面板 1831 → HP≥4000 缺口吃满有效词条（atk_threshold 同款先例）。"""
        rolls = recommend_substats_full(load_character("mydei"))["rolls"]
        assert rolls["HP_percent"] == 30
        assert rolls["CRIT_RATE"] == 0

    def test_equipped_rebalance_no_overflow(self):
        char = load_character("mydei")
        r = recommend_substats_full(char, pieces=_mydei_pieces())["rolls"]
        assert (r["CRIT_RATE"], r["CRIT_DMG"], r["HP_percent"]) == (7, 9, 14)
        base = compute_combat_stats(char, None, _mydei_pieces(), {})
        total = base.CRIT_RATE + 0.48 + r["CRIT_RATE"] * MID_CR / 100.0
        assert total <= 1.0 + 1e-6

    def test_hp_constraint_visible(self):
        full = recommend_substats_full(load_character("mydei"), pieces=_mydei_pieces())
        hp_cons = [c for c in full["constraints"] if c["stat"] == "HP_percent"]
        assert len(hp_cons) == 1
        assert hp_cons[0]["threshold"] == 4000.0
        assert hp_cons[0]["name"] == "HP≥4000"
        # 面板 3439 → 缺口 561 点; HP% 条 = 2.75%×白值1552 ≈ 42.7 点 → 14 条
        assert hp_cons[0]["suggest_rolls"] == 14


class TestConvertBoundaries:
    """hp_crit_convert 三重边界的逐条钉扎（直接调 _marginal_benefit, 剥离权重）。"""

    def _state(self, hp, cr=0.5, cd=1.0):
        char = load_character("mydei")
        profile = _analyze_character(char)
        s = CombatStats()
        s.HP, s.ATK = hp, 1000.0
        s._base_HP = 2000.0
        s.CRIT_RATE, s.CRIT_DMG = cr, cd
        return char, profile, s

    def _gains(self, hp, cr=0.5, cd=1.0):
        char, profile, s = self._state(hp, cr, cd)
        on = _marginal_benefit(s, char, profile, "HP_percent", MID_HP)
        char.hp_crit_convert = {}
        off = _marginal_benefit(s, char, profile, "HP_percent", MID_HP)
        return char, profile, s, on, off

    def test_below_threshold_no_side_value(self):
        _, _, _, on, off = self._gains(3000.0)
        assert on == pytest.approx(off)

    def test_within_window_side_value_exact(self):
        char, profile, s, on, off = self._gains(5000.0)
        delta_hp = 2000.0 * MID_HP / 100.0
        d_cr = delta_hp / 100.0 * 0.012
        e_old = _expected_output(s, char, profile)
        assert on - off == pytest.approx(e_old * s.CRIT_DMG * d_cr)

    def test_above_cap_no_side_value(self):
        _, _, _, on, off = self._gains(8100.0)
        assert on == pytest.approx(off)

    def test_full_crit_headroom_clamps_side(self):
        char, profile, s, on, off = self._gains(5000.0, cr=0.995)
        e_old = _expected_output(s, char, profile)
        assert on - off == pytest.approx(e_old * s.CRIT_DMG * 0.005)


class TestAnaxaTeamPassive:
    def _rolls(self, team=None):
        return recommend_substats_full(load_character("anaxa"), team_paths=team)["rolls"]

    def test_solo_knowledge_team_gets_cd_passive(self):
        r = self._rolls(["智识", "毁灭", "丰饶", "巡猎"])
        assert (r["CRIT_RATE"], r["CRIT_DMG"]) == (18, 4)

    def test_two_knowledge_or_no_context_unchanged(self):
        base = self._rolls()
        assert self._rolls(["智识", "智识", "丰饶", "巡猎"]) == base
        assert (base["CRIT_RATE"], base["CRIT_DMG"]) == (7, 5)


class TestApiTeamContext:
    def test_recommend_endpoint_uses_team_paths(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.api import router
        app = FastAPI()
        app.include_router(router, prefix="/api")

        second_knowledge = next(
            f.stem for f in sorted(DATA_DIR.glob("*.json"))
            if json.loads(f.read_text(encoding="utf-8")).get("path") == "智识"
            and f.stem != "anaxa")

        def _post(mates):
            resp = TestClient(app).post("/api/recommend", json={
                "team": [{"char_id": "anaxa"}] + [{"char_id": m} for m in mates]})
            assert resp.status_code == 200
            return resp.json()["recommendations"][0]["rolls"]

        solo = _post(["firefly", "huohuo", "fengjin"])
        duo = _post([second_knowledge, "huohuo", "fengjin"])
        # API 构建头/手主词条装备上下文: 智识×1 → CD+140 生效(CR 追高); ×2 → 不生效
        assert solo["CRIT_RATE"] == 18
        assert duo["CRIT_RATE"] == 16
