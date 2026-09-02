"""v7.4.0 副词条推荐质量回归（项目主报障四例 + 全库同类扫描）。

项目主报障（2026-09-01）:
- 丹恒·腾荒是攻击力转盾量的存护, 给的有效词条却是双爆
- 花火需爆伤数值转全队拐, 给的全是生命
- 银狼Lv.999 速度攻击对半, 但她的欢愉伤害不吃攻击力
- 火花的特殊行迹有攻击力阈值, 副词条给的全是双爆

四根因: ①换算投资信号不通用（只认 SPD→ATK 且只扫行迹） ②属性阈值只解析速度
③欢愉主导角色主缩放权重未归零 ④护盾缩放属性不影响双暴判定
"""
import pytest

from engine.core.relic_optimizer import (
    _analyze_character, _compute_substat_weights, _parse_spd_per_point,
    _STAT_CONVERT_RE, recommend_substats, recommend_substats_full,
)
from engine.models.character import load_character

DATA = "data/characters"


def _load(cid):
    return load_character(cid, DATA)


class TestConversionSignal:
    """v7.4: 通用换算投资——"X提高…等同于（自身）Y的N%" → Y 持续投入"""

    def test_sparkle_cd_convert(self):
        # 花火战技: "暴击伤害提高等同于花火暴击伤害的24%+45%"
        p = _analyze_character(_load("sparkle"))
        assert p.stat_converts.get("CRIT_DMG") == pytest.approx(24.0)
        r = recommend_substats(_load("sparkle"))
        assert r["CRIT_DMG"] >= 15   # 爆伤转全队拐为核心词条（v7.5: 单词条 cap 18）
        assert r["HP_percent"] <= 15  # 次要（0.3 权重）, 不再主堆

    def test_bronya_cd_convert(self):
        # 布洛妮娅终结技: "暴击伤害提高等同于布洛妮娅暴击伤害的16%+20%"（同病同修）
        p = _analyze_character(_load("bronya"))
        assert p.stat_converts.get("CRIT_DMG") == pytest.approx(16.0)
        r = recommend_substats(_load("bronya"))
        assert r["CRIT_DMG"] >= 15  # v7.5: 单词条 cap 18

    def test_cd_convert_not_open_crit_pair(self):
        # 换算只抬源属性——花火的暴击率权重仍为 0（她的暴伤是给队友的, 不需要自暴击）
        ch = _load("sparkle")
        w = _compute_substat_weights(ch, _analyze_character(ch))
        assert w["CRIT_RATE"] == 0.0
        assert w["CRIT_DMG"] == 1.0

    def test_no_false_positive(self):
        # 希儿"等同于终结技伤害30%真伤"/符玄"回复等同于生命上限5%"不触发
        assert not _STAT_CONVERT_RE.search("额外受到1次等同于希儿终结技伤害30%的真实伤害")
        assert not _STAT_CONVERT_RE.search("施放战技时回复等同于自身生命上限5%的生命值")
        for cid in ("seele", "fu_xuan"):
            assert _analyze_character(_load(cid)).stat_converts == {}, cid


class TestAtkThreshold:
    """v7.4: 攻击力阈值行迹 → ATK 约束 + 每超换算连续收益"""

    def test_sparxie_atk_threshold(self):
        # 火花: 攻击力>2000 每超100点欢愉度+5%（上限80%）
        p = _analyze_character(_load("sparxie"))
        assert p.atk_threshold == 2000.0
        assert p.atk_per_point and p.atk_per_point[0][:3] == ("ELATION_LEVEL", 100, 5.0)
        full = recommend_substats_full(_load("sparxie"))
        assert full["rolls"]["ATK_percent"] >= 15
        names = [c["name"] for c in full["constraints"]]
        assert "攻击力≥2000" in names  # 阈值进入约束面板

    def test_cerydra_atk_threshold(self):
        # 刻律德菈: ATK>2000 每超100点暴伤+18%（上限360%）
        p = _analyze_character(_load("cerydra"))
        assert p.atk_threshold == 2000.0
        assert p.atk_per_point[0][:3] == ("CRIT_DMG", 100, 18.0)
        assert recommend_substats(_load("cerydra"))["ATK_percent"] >= 15

    def test_tb_elation_atk_threshold(self):
        # 开拓者·欢愉: ATK>1000 每超200→欢愉度+10%（"每超200→"无点字变体）
        p = _analyze_character(_load("trailblancer_elation" if False else "trailblazer_elation"))
        assert p.atk_threshold == 1000.0
        assert p.atk_per_point and p.atk_per_point[0][:3] == ("ELATION_LEVEL", 200, 10.0)

    def test_firefly_exclude_atk_wins(self):
        # 流萤同样有 ATK>1800 行迹, 但用户裁决"放弃攻击词条"优先——不加约束不堆攻击
        full = recommend_substats_full(_load("firefly"))
        assert full["rolls"]["ATK_percent"] == 0
        names = [c["name"] for c in full["constraints"]]
        assert "攻击力≥1800" not in names


class TestElationDominant:
    """v7.4: 欢愉主导角色——欢愉伤害不吃白值, 主缩放词条清零"""

    @pytest.mark.parametrize("cid,thr", [("yinlang", 160), ("yaoguang", 120)])
    def test_elation_chars_drop_atk(self, cid, thr):
        r = recommend_substats(_load(cid))
        assert r["ATK_percent"] <= 5   # 仅均摊份额（此前 20 条攻击）
        assert r["SPD_PERCENT"] >= 6   # 速度→欢愉度阈值/换算（v7.5 混合形态）
        names = [c["name"] for c in recommend_substats_full(_load(cid))["constraints"]]
        assert f"速度≥{thr}" in names

    def test_elation_dominant_crit_weight(self):
        # 欢愉主导按输出核心对待 → 暴击率行迹≥10 开双暴（欢愉伤害吃暴击）
        for cid in ("yinlang", "yaoguang"):
            ch = _load(cid)
            w = _compute_substat_weights(ch, _analyze_character(ch))
            assert w["CRIT_RATE"] == 1.0 and w["CRIT_DMG"] == 1.0, cid
            assert w["ATK_percent"] == 0.0, cid


class TestShieldScaler:
    """v7.4: 盾C（护盾按 ATK 结算）不堆双暴——丹恒·腾荒"""

    def test_dht_no_crit_stack(self):
        ch = _load("dan_heng_permansor_terrae")
        p = _analyze_character(ch)
        assert p.has_shield
        w = _compute_substat_weights(ch, p)
        assert w["CRIT_RATE"] == 0.0 and w["CRIT_DMG"] == 0.0
        r = recommend_substats(ch)
        assert r["CRIT_RATE"] + r["CRIT_DMG"] <= 10  # 仅均摊（此前双暴 41 条）
        assert r["ATK_percent"] >= 18               # 盾量/输出全吃攻击（v7.5: 单词条 cap 18）


class TestPerPointBackwardContext:
    """v7.4: "欢愉度+50%，超额每1点+2%"——目标属性在前文的每超解析"""

    def test_backward_keyword(self):
        pp = _parse_spd_per_point("速度≥160→欢愉度+50%，超额每1点+2%，上限100点")
        assert ("ELATION_LEVEL", pytest.approx(0.02)) in pp

    def test_forward_keyword_unchanged(self):
        pp = _parse_spd_per_point("每超1点→冰抗性穿透+2%（上限60点）")
        assert ("RES_PEN_ALL", pytest.approx(0.02)) in pp
