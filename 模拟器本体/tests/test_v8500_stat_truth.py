"""v8.5.0: 面板口径修复钉扎（三项项目主裁决 2026-10-01）。

①副词条单roll表=实机口径; ②smoke 构造器主词条键形双形态（HP_percent 不再归零）;
③哀歌覆国的诗人 -8%速度属 4pc（2pc 仅量子增伤）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_baseline as sb  # noqa: E402

from engine.constants import (  # noqa: E402
    SUB_STAT_VALUES, SUBSTAT_ROLL_FACTOR, StatType,
)


class TestSubstatTruthTable:
    """副词条实机表锚（项目主裁决: 以游戏内显示为准, 有出入报数即改）。"""

    def test_percentage_tiers(self):
        for st in (StatType.HP_PERCENT, StatType.ATK_PERCENT,
                   StatType.EFFECT_HIT_RATE, StatType.EFFECT_RES):
            assert SUB_STAT_VALUES[st] == pytest.approx((3.46, 4.32, 5.18, 25.9)), st
        assert SUB_STAT_VALUES[StatType.DEF_PERCENT] == pytest.approx((4.32, 5.4, 6.48, 32.4))

    def test_crit_and_flats(self):
        assert SUB_STAT_VALUES[StatType.CRIT_RATE] == pytest.approx((2.59, 3.24, 3.89, 19.45))
        assert SUB_STAT_VALUES[StatType.CRIT_DMG] == pytest.approx((5.18, 6.48, 7.78, 38.9))
        assert SUB_STAT_VALUES[StatType.HP_FLAT] == pytest.approx((42.8, 53.4, 64.1, 320.5))
        assert SUB_STAT_VALUES[StatType.ATK_FLAT] == pytest.approx((14.3, 17.8, 22.4, 112.0))
        assert SUB_STAT_VALUES[StatType.SPD_PERCENT] == pytest.approx((2.0, 2.6, 3.0, 15.0))

    def test_roll_factor_kept(self):
        # v7.5.0 项目主指示的小毕业档保留
        assert SUBSTAT_ROLL_FACTOR == pytest.approx(1.1)


class TestSmokeMainStatKeyForms:
    """smoke 构造器主词条键形双形态回归（此前 HP_percent 恒取 0, 五队18槽中招）。"""

    def test_value_form_resolves(self):
        assert sb.MAIN_VALUES["HP_percent"] == pytest.approx(43.2)
        assert sb.MAIN_VALUES["ATK_percent"] == pytest.approx(43.2)
        assert sb.MAIN_VALUES["HP_PERCENT"] == pytest.approx(43.2)  # name 形仍在
        assert sb._main_stat_of("HP_percent") is StatType.HP_PERCENT
        assert sb._main_stat_of("SPD_PERCENT") is StatType.SPD_PERCENT

    def test_member_build_hp_mains_live(self):
        recs = json.loads(Path("data/recommendations.json").read_text(encoding="utf-8"))
        sets = sb._load_relic_sets()
        cfg = sb._build_member("xiadie", 0, 1, recs, sets)
        feet = next(p for p in cfg["relics"] if p.slot == "feet")
        rope = next(p for p in cfg["relics"] if p.slot == "link_rope")
        assert feet.main_stat_value == pytest.approx(43.2)  # 遐蝶鞋=HP%
        assert rope.main_stat_value == pytest.approx(43.2)  # 遐蝶绳=HP%


class TestPoetSetPlacement:
    """哀歌覆国的诗人: -8%速度是 4pc 效果（游戏口径）; 2pc 仅量子增伤。"""

    def test_two_pc_pure(self):
        eff = sb._load_relic_sets()["哀歌覆国的诗人"].effects
        two = next(e for e in eff if e.pieces_required == 2)
        assert two.attributes == {"DMG_BONUS_QUANTUM": 10.0}

    def test_four_pc_carries_spd_down(self):
        eff = sb._load_relic_sets()["哀歌覆国的诗人"].effects
        four = next(e for e in eff if e.pieces_required == 4)
        assert four.attributes == {"SPD_percent": -8.0}
        assert four.condition == "low_spd_cr_boost"
