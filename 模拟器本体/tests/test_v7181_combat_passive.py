"""v7.18.1: 推荐层感知战斗内常驻被动双暴（combat_passive_stats）。

背景（项目主报障）: 长夜月行迹「天黑黑，月寂寂」+35% 暴击率由角色模块在
rem_init 相位直加 base_stats, compute_combat_stats/推荐层不可见 → 满爆预算按
5%+18.7% 面板行迹计算, 修复前推荐 15 条暴击率, 实际总暴击 108.2% 溢出。

修复: 角色数据 JSON 新增可选字段 combat_passive_stats（无条件常驻加成, 百分数
原始口径）; relic_optimizer 三处并入（推荐基态/主词条满爆判断/旧路径边际基态）。
"""
import json
from pathlib import Path

from engine.constants import SUB_STAT_VALUES, SUBSTAT_ROLL_FACTOR
from engine.models.character import load_character
from engine.core.relic_optimizer import (
    recommend_substats_full, optimize_relics, _combat_passive_crit,
)
from engine.core.attributes import compute_combat_stats

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "characters"


class TestDataContract:
    def test_changyeyue_json_declares_passive_crit(self):
        raw = json.loads((DATA_DIR / "changyeyue.json").read_text(encoding="utf-8"))
        assert raw["combat_passive_stats"] == {"CRIT_RATE": 35.0}

    def test_model_loads_field_and_defaults_empty(self):
        assert load_character("changyeyue").combat_passive_stats == {"CRIT_RATE": 35.0}
        # 无字段角色默认空表 → 机制对其不激活（遐蝶同为记忆C, 无此类被动）
        assert load_character("xiadie").combat_passive_stats == {}
        assert _combat_passive_crit(load_character("xiadie")) == (0.0, 0.0)

    def test_helper_decimal_conversion(self):
        assert _combat_passive_crit(load_character("changyeyue")) == (0.35, 0.0)


class TestRecommendBudget:
    """满爆预算必须计入面板外被动, 暴击率条数不得溢出 100%。"""

    def _total_crit_rate(self, rolls_cr: int) -> float:
        char = load_character("changyeyue")
        base = compute_combat_stats(char, None, [], {})
        mid = SUB_STAT_VALUES["CRIT_RATE"][1] * SUBSTAT_ROLL_FACTOR
        return base.CRIT_RATE + 0.35 + rolls_cr * mid / 100.0

    def test_no_overflow_with_passive(self):
        rolls = recommend_substats_full(load_character("changyeyue"))["rolls"]
        assert self._total_crit_rate(rolls["CRIT_RATE"]) <= 1.0 + 1e-6

    def test_pinned_rebalanced_distribution(self):
        """修复前口径: CR=15/CD=12/HP=3（总暴击 108.2%）→ 修复后溢出条转投暴伤。"""
        rolls = recommend_substats_full(load_character("changyeyue"))["rolls"]
        assert rolls["CRIT_RATE"] == 12
        assert rolls["CRIT_DMG"] == 18
        assert rolls["CRIT_RATE"] < 15  # 修复回归线: 回到 15 条即机制失效

    def test_weights_unchanged(self):
        """被动并入只影响预算基态, 不动权重信号链（dps 双暴 1.0/HP 0.8）。"""
        full = recommend_substats_full(load_character("changyeyue"))
        assert full["weights"]["CRIT_RATE"] == 1.0
        assert full["weights"]["HP_percent"] == 0.8
        assert full["graduation"]["effective_used"] == 30


class TestMainStatPick:
    """optimize_relics 暴击衣判断同样计入被动（测试专用路径, Web 推荐不走此入口）。"""

    def _body_main(self, char) -> str:
        build = optimize_relics(char, ["CRIT_RATE", "CRIT_DMG", "HP_percent"],
                                crit_target=1.0)
        return build.pieces["body"].main_stat_type

    def test_body_crit_without_passive(self):
        char = load_character("changyeyue")
        char.combat_passive_stats = {}
        # 非sub暴击 0.237 + sub上限 ~0.198 < 1.0 → 需要暴击衣
        assert self._body_main(char) == "CRIT_RATE"

    def test_body_cd_when_passive_covers_budget(self):
        char = load_character("changyeyue")
        char.combat_passive_stats = {"CRIT_RATE": 60.0}
        # 非sub暴击 0.837 + sub上限 ~0.198 ≥ 1.0 → 暴伤衣可达满爆
        assert self._body_main(char) == "CRIT_DMG"
