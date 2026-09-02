"""遗器副词条推荐逻辑测试（技能结构驱动）"""
import pytest
from engine.core.relic_optimizer import (
    recommend_substats, _analyze_character, _extract_spd_constraints,
    _extract_trace_spd_constraints, FRONTEND_ROLL_KEYS, _roll_cap,
    _marginal_benefit, _expected_output, _with_roll,
)
from engine.models.character import Character, load_character
from engine.models.equipment import LightCone, LightConeEffect, RelicPiece, RelicSet
from engine.core.attributes import compute_combat_stats
from engine.constants import StatType
import os

DATA = "data/characters"


def _load(cid):
    return load_character(cid, DATA)


def _relic_sets():
    sets = {}
    for f in os.listdir("data/relics"):
        try:
            rs = RelicSet.from_json("data/relics/" + f)
            sets[rs.name] = rs
        except Exception:
            pass
    return sets


def _poet_pieces():
    """诗人套 4+2 配置（无主词条）"""
    sets = _relic_sets()
    poet = RelicSet.from_json("data/relics/124_哀歌覆国的诗人.json").name
    keel = RelicSet.from_json("data/relics/323_永恒之地翁法罗斯.json").name
    return [
        RelicPiece(slot="head", set_name=poet, main_stat_type=StatType.HP_FLAT, main_stat_value=705),
        RelicPiece(slot="hands", set_name=poet, main_stat_type=StatType.ATK_FLAT, main_stat_value=352),
        RelicPiece(slot="body", set_name=poet, main_stat_type=StatType.CRIT_DMG, main_stat_value=64.8),
        RelicPiece(slot="feet", set_name=poet, main_stat_type=StatType.SPD_PERCENT, main_stat_value=25.0),
        RelicPiece(slot="planar_sphere", set_name=keel, main_stat_type=StatType.DMG_BONUS_ICE, main_stat_value=38.8),
        RelicPiece(slot="link_rope", set_name=keel, main_stat_type=StatType.ATK_PERCENT, main_stat_value=43.2),
    ], sets


class TestAnalyzeCharacter:
    def test_seele_dps_atk(self):
        p = _analyze_character(_load("seele"))
        assert p.role == "dps"
        assert p.primary_stat == "ATK"

    def test_xiadie_dps_hp(self):
        p = _analyze_character(_load("xiadie"))
        assert p.role == "dps"
        assert p.primary_stat == "HP"

    def test_xilian_dps_hp(self):
        p = _analyze_character(_load("xilian"))
        assert p.role == "dps"
        assert p.primary_stat == "HP"

    def test_fengjin_healer(self):
        p = _analyze_character(_load("fengjin"))
        assert p.role == "healer"
        assert p.primary_stat == "HP"
        assert p.has_heal

    def test_huohuo_healer(self):
        p = _analyze_character(_load("huohuo"))
        assert p.role == "healer"

    def test_bronya_support(self):
        p = _analyze_character(_load("bronya"))
        assert p.role == "support"

    def test_sparkle_support(self):
        p = _analyze_character(_load("sparkle"))
        assert p.role == "support"

    def test_empty_shell_fallback(self):
        p = _analyze_character(_load("himeko"))  # v6.10: acheron已录入, 换空壳样本(智识→dps兜底)
        assert p.role == "dps"
        assert p.primary_stat == "ATK"


class TestSpdConstraints:
    def test_xilian_180(self):
        cons = _extract_trace_spd_constraints(_load("xilian"))
        spd_cons = [c for c in cons if c.stat == StatType.SPD_PERCENT.value]
        assert spd_cons, "昔涟应有 SPD 约束"
        assert spd_cons[0].value == 180
        assert spd_cons[0].reward.get("DMG_BONUS_ALL") == 20.0

    def test_fengjin_200(self):
        cons = _extract_trace_spd_constraints(_load("fengjin"))
        spd_cons = [c for c in cons if c.stat == StatType.SPD_PERCENT.value]
        assert spd_cons and spd_cons[0].value == 200

    def test_yaoguang_120(self):
        cons = _extract_trace_spd_constraints(_load("yaoguang"))
        spd_cons = [c for c in cons if c.stat == StatType.SPD_PERCENT.value]
        assert spd_cons and spd_cons[0].value == 120

    def test_yinlang_160(self):
        cons = _extract_trace_spd_constraints(_load("yinlang"))
        spd_cons = [c for c in cons if c.stat == StatType.SPD_PERCENT.value]
        assert spd_cons and spd_cons[0].value == 160

    def test_xiadie_no_spd_constraint(self):
        """遐蝶行迹"HP≥50%→速度+40%"不应误匹配为 SPD 阈值"""
        cons = _extract_trace_spd_constraints(_load("xiadie"))
        spd_cons = [c for c in cons if c.stat == StatType.SPD_PERCENT.value]
        assert not spd_cons

    def test_poet_set_constraints(self):
        pieces, sets = _poet_pieces()
        cons = _extract_spd_constraints(_load("seele"), sets, pieces)
        spd_cons = [c for c in cons if c.op == "lt" and c.stat == StatType.SPD_PERCENT.value]
        assert len(spd_cons) >= 2  # lt 110 + lt 95


class TestRecommendSubstats:
    def test_seele_30_rolls(self):
        r = recommend_substats(_load("seele"), effective_rolls=30)
        assert sum(r.values()) == 50  # v7.3.1: 总词条固定 50
        # v7.3: 希儿无速度信号（三语义裁决: 无信号→权重0）→ 速度不进有效分配,
        # 仅剩均摊份额; 断点前瞻行为由 test_v73_substats 的阿格莱雅/符玄用例覆盖
        assert r["CRIT_RATE"] + r["CRIT_DMG"] >= 20
        assert r["SPD_PERCENT"] <= 5

    def test_xiadie_hp_over_atk(self):
        r = recommend_substats(_load("xiadie"), effective_rolls=30)
        assert sum(r.values()) == 50
        assert r["HP_percent"] > r["ATK_percent"]
        # v7.3: 暴击率行迹≥10 第三信号——遐蝶占位倍率 224% 不再误杀双暴
        assert r["CRIT_RATE"] + r["CRIT_DMG"] > 0

    def test_xilian_spd_180(self):
        r = recommend_substats(_load("xilian"), effective_rolls=30)
        assert sum(r.values()) == 50
        # base 101 + 鞋25 = 126, 需 (180-126)/3 = 18条（约束硬优先）;
        # v7.3 a方案: 断点前瞻使速度跨档收益全程可见 + "每超1点→冰抗穿2%"连续加成
        # → 有效预算速度全取; 双暴在更大有效预算下回填
        assert r["SPD_PERCENT"] >= 18
        r50 = recommend_substats(_load("xilian"), effective_rolls=50)
        assert r50["CRIT_RATE"] > 0

    def test_fengjin_spd_200(self):
        r = recommend_substats(_load("fengjin"), effective_rolls=50)
        assert sum(r.values()) == 50
        # base 110 + 鞋25 = 135, 需 (200-135)/3 = 22条; v7.3: 速度 cap(30) 后剩余回填 HP
        assert r["SPD_PERCENT"] >= 15
        assert r["HP_percent"] > 0

    def test_huohuo_no_crit_stack(self):
        r = recommend_substats(_load("huohuo"), effective_rolls=50)
        assert sum(r.values()) == 50
        assert r["HP_percent"] > 0
        # v7.3: 非有效词条均摊（各 ~2-3 条）, 治疗角色不堆暴击
        assert r["CRIT_DMG"] <= 10

    def test_empty_shell(self):
        r = recommend_substats(_load("pela"), effective_rolls=30)
        assert sum(r.values()) == 50

    def test_small_effective(self):
        r = recommend_substats(_load("seele"), effective_rolls=5)
        assert sum(r.values()) == 50  # 总仍 50
        # v7.5: 有效仅 5 条（单词条 ≤60% → 暴击至多 3 条）, 其余均摊
        assert r["CRIT_RATE"] + r["CRIT_DMG"] + r["ATK_percent"] == 5
        assert r["CRIT_RATE"] <= 3

    def test_keys_contract(self):
        r = recommend_substats(_load("xiadie"), effective_rolls=30)
        assert set(r.keys()) == set(FRONTEND_ROLL_KEYS)  # v7.3: 9 键（含效果命中）


class TestV55RecommendationBounds:
    def _lingsha_main_pieces(self):
        return [
            RelicPiece("head", "", main_stat_type=StatType.HP_FLAT, main_stat_value=705),
            RelicPiece("hands", "", main_stat_type=StatType.ATK_FLAT, main_stat_value=352),
            RelicPiece("body", "", main_stat_type=StatType.HEAL_BONUS, main_stat_value=34.5),
            RelicPiece("feet", "", main_stat_type=StatType.SPD_PERCENT, main_stat_value=25),
            RelicPiece("planar_sphere", "", main_stat_type=StatType.ATK_PERCENT, main_stat_value=43.2),
            RelicPiece("link_rope", "", main_stat_type=StatType.BREAK_EFFECT, main_stat_value=64.8),
        ]

    def test_recommendation_never_exceeds_main_stat_conflict_roll_cap(self):
        pieces = self._lingsha_main_pieces()
        result = recommend_substats(_load("lingsha"), pieces=pieces, effective_rolls=50)
        mains = {piece.slot: piece.main_stat_type for piece in pieces}

        for frontend_key, count in result.items():
            internal_key = StatType.SPD_PERCENT.value if frontend_key == "SPD_PERCENT" else frontend_key
            assert count <= _roll_cap(internal_key, mains, 50)

    def test_speed_light_cone_reduces_threshold_rolls(self):
        char = _load("lingsha")
        light_cone = LightCone(
            id="speed", name="speed", path=char.path,
            effects=[LightConeEffect(
                type="permanent_buff", attributes={"SPD_percent": 12.0},
            )],
        )

        without_light_cone = recommend_substats(char, effective_rolls=30)
        with_light_cone = recommend_substats(char, light_cone, effective_rolls=30)

        assert with_light_cone["SPD_PERCENT"] < without_light_cone["SPD_PERCENT"]

    def test_speed_threshold_gain_uses_the_full_optimization_window(self):
        char = _load("seele")
        profile = _analyze_character(char)
        stats = compute_combat_stats(char)
        stats.SPD = 131.0
        old_output = _expected_output(stats, char, profile)
        expected = old_output * (10000.0 / 150.0)

        assert _marginal_benefit(stats, char, profile, "SPD_percent", 3.0) == pytest.approx(expected)


class TestBreakChars:
    """v5.5: 击破四角色优化器适配"""

    def test_firefly_analyze_dps(self):
        p = _analyze_character(_load("firefly"))
        assert p.role == "dps"          # 自身状态技不算团队buff
        assert p.primary_stat == "ATK"
        assert p.is_break

    def test_firefly_recommend(self):
        r = recommend_substats(_load("firefly"), effective_rolls=30)
        assert sum(r.values()) == 50  # v7.3.1: 总词条固定 50
        assert set(r.keys()) == set(FRONTEND_ROLL_KEYS)
        assert r["SPD_PERCENT"] > 0     # 145 速度达标约束
        assert r["ATK_percent"] == 0    # 放弃攻击词条（用户确认）
        assert r["BREAK_EFFECT"] > 0    # 达标后堆击破特攻

    def test_lingsha_analyze_healer_atk(self):
        p = _analyze_character(_load("lingsha"))
        assert p.role == "healer"
        assert p.primary_stat == "ATK"  # ATK 基数治疗（HEAL_REGISTRY stat）

    def test_lingsha_recommend(self):
        r = recommend_substats(_load("lingsha"), effective_rolls=30)
        assert sum(r.values()) == 50  # v7.3.1: 总词条固定 50
        assert r["SPD_PERCENT"] > 0     # 134 达标
        assert r["BREAK_EFFECT"] > 0

    def test_fugue_analyze_support(self):
        p = _analyze_character(_load("fugue"))
        assert p.role == "support"      # 狐祈团队buff优先于直伤
        assert p.is_break

    def test_tbh_analyze_support(self):
        p = _analyze_character(_load("trailblazer_harmony"))
        assert p.role == "support"
        assert p.is_break

    def test_tbh_bounce_expanded(self):
        """v6.11 阶段0: 直伤权重改总倍率口径——击破辅助直伤为副, 击破权重主导"""
        p = _analyze_character(_load("trailblazer_harmony"))
        assert p.is_break
        assert p.weights["break"] > p.weights["direct"]  # 击破主导（旧口径按段数直伤 0.778 已废弃）
