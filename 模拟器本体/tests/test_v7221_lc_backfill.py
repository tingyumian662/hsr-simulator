"""v7.22.1: 光锥数据回填——时节不居全量 + 滞后五把 values 固化 + 落选清除。

项目主指令（2026-09-07）: ①时节不居原稿全量录入（双常驻行五档 + 比率档位化）;
②落选同名 JSON 清除（landau_s_choice/memory_s_curtain_never_falls/to_evernight_s_stars）;
③txt 已有内容回填 JSON（所见即我/星火悄然闪耀/没有回报的加冕/游戏尘寰/理想燃烧的
地狱 + 朗道注记五档固化）。
"""
import json
from pathlib import Path

import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import SimState, _process_lc_effects
from engine.models.character import load_character
from engine.models.equipment import load_lightcone
from helpers import _enemy

ROOT = Path(__file__).resolve().parent.parent
LC_DIR = ROOT / "data" / "light_cones"

BACKFILLED = ["time_waits_for_no_one", "i_am_as_you_behold", "flickering_stars",
              "a_thankless_coronation", "earthly_escapade",
              "the_hell_where_ideals_burn", "landaus_choice"]
DOOMED = ["landau_s_choice", "memory_s_curtain_never_falls", "to_evernight_s_stars"]


def _stats(lc_id, rank, cid):
    c = load_character(cid)
    lc = load_lightcone(lc_id)
    lc.rank = rank
    return compute_combat_stats(c, lc, None, None)


class TestTimeWaitsForNoOne:
    def test_permanent_rows_scale(self):
        """双常驻行五档: S1→S5 生命差 = 白值×12%、治疗差 = 8%（原 JSON 缺这两条）。"""
        s0 = compute_combat_stats(load_character("bailu"), None, None, None)
        s1, s5 = (_stats("time_waits_for_no_one", r, "bailu") for r in (1, 5))
        assert s5.HP - s1.HP == pytest.approx(s1._base_HP * 0.12)
        assert s5.HEAL_BONUS - s1.HEAL_BONUS == pytest.approx(0.08)
        assert s1.HEAL_BONUS > s0.HEAL_BONUS  # 常驻行此前完全缺失, 现已生效

    def test_ratio_rank_aware(self):
        """附加伤害比率按档取值: S3=48%（原硬编码 36%=S1）。"""
        c = load_character("lingsha")
        lc = load_lightcone("time_waits_for_no_one")
        lc.rank = 3
        stats = compute_combat_stats(c, lc, None, None)
        from engine.runtime import SimUnit
        u = SimUnit(char=c, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        u.lightcone = lc
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra["lc_last_heal_amt"] = 1000.0
        _process_lc_effects(u, state, "on_heal")
        hp0 = e.HP
        _process_lc_effects(u, state, "on_self_attack")
        assert hp0 - e.HP == pytest.approx(480.0, abs=1e-6)  # 1000×48%


class TestBackfilledValues:
    def test_earthly_escapade_cd_tiers(self):
        """游戏尘寰 S1/S3/S5: 常驻 32/46/60 + 既有【假面】常驻近似 28（不随档）。"""
        cd = [_stats("earthly_escapade", r, "sparkle").CRIT_DMG for r in (1, 3, 5)]
        base = compute_combat_stats(load_character("sparkle"), None, None, None).CRIT_DMG
        assert cd[0] == pytest.approx(base + 0.32 + 0.28)
        assert cd[1] == pytest.approx(base + 0.46 + 0.28)
        assert cd[2] == pytest.approx(base + 0.60 + 0.28)

    def test_flickering_stars_rows(self):
        """星火悄然闪耀: 暴击五档 + 新增无视防御/战技增伤两行（常驻近似, 均随档缩放）。"""
        s0 = compute_combat_stats(load_character("yuanbanlin"), None, None, None)
        s1, s5 = (_stats("flickering_stars", r, "yuanbanlin") for r in (1, 5))
        assert s1.CRIT_RATE == pytest.approx(s0.CRIT_RATE + 0.18)
        assert s5.CRIT_RATE == pytest.approx(s0.CRIT_RATE + 0.30)
        assert s1.DEF_PEN == pytest.approx(s0.DEF_PEN + 0.20)
        assert s5.DEF_PEN == pytest.approx(s0.DEF_PEN + 0.36)
        assert s1.DMG_BONUS_BY_SKILL_TYPE.get("skill", 0.0) == pytest.approx(0.72)
        assert s5.DMG_BONUS_BY_SKILL_TYPE.get("skill", 0.0) == pytest.approx(1.20)

    def test_i_am_as_you_behold_split_rows(self):
        """所见即我: ATK/ER 拆双常驻行各自带档（S3 vs S1 = 白值×6% / ER+5%）。"""
        s0 = compute_combat_stats(load_character("jierjialameishi"), None, None, None)
        s1 = _stats("i_am_as_you_behold", 1, "jierjialameishi")
        s3 = _stats("i_am_as_you_behold", 3, "jierjialameishi")
        assert s3.ATK - s1.ATK == pytest.approx(s1._base_ATK * 0.06)
        assert s3.ENERGY_REGEN - s1.ENERGY_REGEN == pytest.approx(0.05)
        assert s1.ENERGY_REGEN > s0.ENERGY_REGEN

    def test_landaus_choice_tiers(self):
        """朗道的选择: 注记五档固化, S1=16% / S5=24%。"""
        s0 = compute_combat_stats(load_character("gepard"), None, None, None)
        s1, s5 = (_stats("landaus_choice", r, "gepard") for r in (1, 5))
        assert s1.DMG_REDUCTION == pytest.approx(s0.DMG_REDUCTION + 0.16)
        assert s5.DMG_REDUCTION == pytest.approx(s0.DMG_REDUCTION + 0.24)


class TestBackfillAudit:
    def test_permanent_values_match_default_rank(self):
        """审计不变量: 每条带 values 的常驻行, values[rank-1] == attributes 值
        ——默认档零漂移（smoke 重置仅源于数据补全而非口径变化）。"""
        for lc_id in BACKFILLED:
            d = json.loads((LC_DIR / f"{lc_id}.json").read_text(encoding="utf-8"))
            rank = int(d.get("rank", 1))
            for e in d["effects"]:
                if e["type"] == "permanent_buff" and e.get("values"):
                    for v in e["attributes"].values():
                        assert v == pytest.approx(e["values"][rank - 1]), lc_id

    def test_doomed_files_gone(self):
        for lc_id in DOOMED:
            assert not (LC_DIR / f"{lc_id}.json").exists()
            with pytest.raises(FileNotFoundError):
                load_lightcone(lc_id)

    def test_changyeyue_reco_points_to_winner(self):
        recs = json.loads((ROOT / "data" / "recommendations.json")
                          .read_text(encoding="utf-8"))
        assert recs["changyeyue"]["light_cone"] == "starlight_to_the_long_night"

    def test_text_conditions_present(self):
        """回填后不允许再出现空文案常驻行（提取器合成口径的数据面）。"""
        for lc_id in BACKFILLED:
            d = json.loads((LC_DIR / f"{lc_id}.json").read_text(encoding="utf-8"))
            for e in d["effects"]:
                assert e.get("condition", "").strip() or e.get("condition_code"), lc_id
