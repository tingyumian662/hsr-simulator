"""v6.11.0 遗器副词条优化回归（阶段 0~2）。"""
import pytest

from engine.core.relic_optimizer import (
    _analyze_character,
    recommend_substats,
)
from engine.models.character import load_character

DATA = "data/characters"


def _load(cid):
    return load_character(cid, DATA)


# ── 阶段 0 定位链修复 ──

def test_fuxuan_is_tank_not_dps():
    p = _analyze_character(_load("fu_xuan"))
    assert p.role == "tank"
    assert p.primary_stat == "HP"


def test_fuxuan_recommend_no_crit():
    r = recommend_substats(_load("fu_xuan"), effective_rolls=30)
    # v7.3.1: 双暴对坦无效 → 不进有效分配, 仅剩均摊份额（~2-3 条）
    assert r["CRIT_RATE"] <= 5
    assert r["CRIT_DMG"] <= 5
    assert r["HP_percent"] > 0  # 生命为主
    assert sum(r.values()) == 50  # 总词条固定 50（有效 30 + 均摊 20）


def test_seele_still_dps():
    p = _analyze_character(_load("seele"))
    assert p.role == "dps"
    r = recommend_substats(_load("seele"), effective_rolls=30)
    assert r["CRIT_RATE"] > 0
    assert r["CRIT_DMG"] > 0


def test_huohuo_still_healer():
    p = _analyze_character(_load("huohuo"))
    assert p.role == "healer"


def test_xiadie_xilian_still_dps():
    assert _analyze_character(_load("xiadie")).role == "dps"
    assert _analyze_character(_load("xilian")).role == "dps"


# ── 阶段 1 个体化权重链 ──

def test_hysilens_ehr_weight_high():
    """海瑟音: 命中行迹+DOT输出 → EHR 核心（1.0）, 双暴≈0"""
    from engine.core.relic_optimizer import _compute_substat_weights
    ch = _load("hysilens")
    p = _analyze_character(ch)
    w = _compute_substat_weights(ch, p)
    assert w["EFFECT_HIT_RATE"] == 1.0
    assert w["CRIT_RATE"] == 0.0
    assert w["CRIT_DMG"] == 0.0
    assert w["ATK_percent"] == 0.8


def test_acheron_no_ehr():
    """黄泉: 无命中行迹/无DOT → EHR=0, 双暴 1.0"""
    from engine.core.relic_optimizer import _compute_substat_weights
    ch = _load("acheron")
    p = _analyze_character(ch)
    w = _compute_substat_weights(ch, p)
    assert w["EFFECT_HIT_RATE"] == 0.0
    assert w["CRIT_RATE"] == 1.0
    assert w["CRIT_DMG"] == 1.0


def test_aglaea_spd_core():
    """阿格莱雅: 攻击力=速度×720% 换算型 → SPD 1.0"""
    from engine.core.relic_optimizer import _compute_substat_weights
    ch = _load("aglaea")
    p = _analyze_character(ch)
    w = _compute_substat_weights(ch, p)
    assert w["SPD_percent"] == 1.0


def test_changyeyue_xiadie_no_spd():
    """长夜月/遐蝶: 无速度信号 → SPD=0（移出有效池）"""
    from engine.core.relic_optimizer import _compute_substat_weights
    for cid in ("changyeyue", "xiadie"):
        ch = _load(cid)
        p = _analyze_character(ch)
        w = _compute_substat_weights(ch, p)
        assert w["SPD_percent"] == 0.0, cid


def test_fengjin_xilian_spd_threshold_type():
    """风堇/昔涟: 投入型阈值 → SPD 0.4（达标前约束硬优先 1.0）"""
    from engine.core.relic_optimizer import _compute_substat_weights
    for cid in ("fengjin", "xilian"):
        ch = _load(cid)
        p = _analyze_character(ch)
        w = _compute_substat_weights(ch, p)
        assert w["SPD_percent"] == 0.4, cid


def test_fuxuan_tank_weights():
    """符玄 tank: 双暴0 / HP 0.8 / SPD 0.4 / 抵抗 0.6"""
    from engine.core.relic_optimizer import _compute_substat_weights
    ch = _load("fu_xuan")
    p = _analyze_character(ch)
    w = _compute_substat_weights(ch, p)
    assert w["CRIT_RATE"] == 0.0 and w["CRIT_DMG"] == 0.0
    assert w["HP_percent"] == 0.8
    assert w["EFFECT_RES"] == 0.6
    assert w["SPD_percent"] == 0.4


# ── 阶段 2 完整响应/约束面板/毕业度 ──

def test_full_response_contract():
    from engine.core.relic_optimizer import recommend_substats_full
    full = recommend_substats_full(_load("seele"), effective_rolls=30)
    assert set(full.keys()) == {"rolls", "weights", "constraints", "graduation"}
    assert sum(full["rolls"].values()) == 50  # v7.3.1: 总词条固定 50


def test_keel_constraint_panel():
    """折断的龙骨: 效果抵抗≥30 约束进入面板, 达标前 suggest_rolls 正确"""
    from engine.core.relic_optimizer import recommend_substats_full
    from engine.models.equipment import RelicPiece, RelicSet
    from engine.constants import StatType
    keel = RelicSet.from_json("data/relics/310_折断的龙骨.json").name
    relic_sets = {keel: RelicSet.from_json("data/relics/310_折断的龙骨.json")}
    pieces = [
        RelicPiece(slot="head", set_name=keel, main_stat_type=StatType.HP_FLAT, main_stat_value=705),
        RelicPiece(slot="hands", set_name=keel, main_stat_type=StatType.ATK_FLAT, main_stat_value=352),
    ]
    full = recommend_substats_full(_load("seele"), pieces=pieces, relic_sets=relic_sets, effective_rolls=30)
    cons = {c["name"]: c for c in full["constraints"]}
    assert "效果抵抗≥30" in cons
    c = cons["效果抵抗≥30"]
    assert c["met"] is False  # 希儿行迹抵抗 10% < 30 → 未达标
    # 建议条数 = ceil((30 - 当前抵抗%) / 2.5)
    import math
    expected = math.ceil((30 - c["current"]) / 2.5)
    assert c["suggest_rolls"] == expected


def test_graduation_dps_target():
    from engine.core.relic_optimizer import recommend_substats_full
    full = recommend_substats_full(_load("seele"), effective_rolls=30)
    assert full["graduation"]["target_range"] == [30, 35]
    assert full["graduation"]["score_pct"] == 100  # 30/30 达下限


def test_invalid_stats_get_zero_rolls():
    """权重=0 的词条不进有效分配（v7.3.1: 仅获剩余均摊份额 ≤5 条）"""
    from engine.core.relic_optimizer import recommend_substats
    r = recommend_substats(_load("fu_xuan"), effective_rolls=30)
    assert r["CRIT_RATE"] <= 5 and r["CRIT_DMG"] <= 5  # 双暴无效 → 仅均摊


def test_substat_weights_json_override():
    """JSON substat_weights 手动覆盖推导值"""
    from engine.core.relic_optimizer import _compute_substat_weights
    ch = _load("seele")
    ch.substat_weights = {"CRIT_DMG": 0.5}
    p = _analyze_character(ch)
    w = _compute_substat_weights(ch, p)
    assert w["CRIT_DMG"] == 0.5
    assert w["CRIT_RATE"] == 1.0  # 未被覆盖的保持推导值
