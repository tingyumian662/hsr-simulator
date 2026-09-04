"""v7.3.0 遗器副词条处理逻辑回归。

项目主裁决（2026-09-01）:
- 效果命中也是遗器副词条（契约 8 键 → 9 键）
- 速度按 a 方案修复（断点前瞻 + 换算型连续收益建模）
- 单遗器 8-9 词条为正常（升级提升按词条数计）, 总词条数取中值 50
- 非有效词条默认按剩余词条数平均分配
- 应修尽修: 空壳白值判据 / EHR 边际量纲 / crit 三信号 / is_break 细化 /
  _cap 优先级 / 前端契约（速度置灰键 + 命中格）
"""
from pathlib import Path

import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.relic_optimizer import (
    FRONTEND_ROLL_KEYS, _analyze_character, _cap, _compute_substat_weights,
    _marginal_benefit, recommend_substats, recommend_substats_full,
)
from engine.constants import StatType
from engine.models.character import load_character

DATA = "data/characters"


def _load(cid):
    return load_character(cid, DATA)


class TestShellPrimaryByBaseStats:
    """v7.3: 空壳主缩放判据改基础白值结构（此前 HP+10%/DEF+22.5% 通用行迹导致 20 角色误判）"""

    def test_atk_dps_not_hp(self):
        # 银枝/克拉拉/卡芙卡等攻击C此前因 HP+10% 通用行迹被判 HP
        for cid in ("argenti", "clara", "kafka", "boothill", "sushang"):
            assert _analyze_character(_load(cid)).primary_stat == "ATK", cid

    def test_def_tanks(self):
        # 杰帕德 DEF 12.5 行迹不达标曾落 ATK; 砂金·戏浪/萨博白值 DEF≥600
        for cid in ("gepard", "aventurine_waveflair", "saber", "march_7th"):
            assert _analyze_character(_load(cid)).primary_stat == "DEF", cid

    def test_blade_hp(self):
        # 刃 1358/543: HP≥1300 且 ATK<600 → HP（此前误判 ATK）
        assert _analyze_character(_load("blade")).primary_stat == "HP"

    def test_luocha_atk_healer(self):
        # 罗刹 ATK 基数治疗（白值 ATK 757 ≥ 700 例外）
        p = _analyze_character(_load("luocha"))
        assert p.role == "healer"
        assert p.primary_stat == "ATK"

    def test_misha_atk_not_def(self):
        # 米沙 DEF+22.5% 通用行迹曾误判 DEF
        assert _analyze_character(_load("misha")).primary_stat == "ATK"

    def test_argenti_recommend_atk(self):
        r = recommend_substats(_load("argenti"), effective_rolls=50)
        assert r["ATK_percent"] >= 25
        assert r["HP_percent"] <= 5  # 均摊, 不再主堆


class TestEhrContractAndMix:
    """v7.3: 效果命中入契约（9 键）+ 边际量纲修复（此前 EHR:ATK = 6.75:1897 恒输）"""

    def test_contract_nine_keys(self):
        assert "EFFECT_HIT_RATE" in FRONTEND_ROLL_KEYS
        assert len(FRONTEND_ROLL_KEYS) == 9

    def test_debuffer_gets_ehr(self):
        for cid in ("black_swan", "jiaoqiu", "kafka", "pela"):
            r = recommend_substats(_load(cid), effective_rolls=50)
            assert r["EFFECT_HIT_RATE"] >= 5, cid
            assert r["ATK_percent"] >= 10, cid

    def test_ehr_marginal_dimension(self):
        ch = _load("black_swan")
        p = _analyze_character(ch)
        stats = compute_combat_stats(ch)
        g_ehr = _marginal_benefit(stats, ch, p, "EFFECT_HIT_RATE", 2.5)
        g_atk = _marginal_benefit(stats, ch, p, "ATK_percent", 2.5)
        assert g_ehr > g_atk * 0.1  # 修复前 6.75 vs 1897（1/280 量纲差）

    def test_incidental_debuff_not_ehr_stacked(self):
        # 藿藿/流萤/灵砂的附带 debuff 不再吃 0.6 命中权重（收窄为虚无/debuffer 定位）
        for cid in ("huohuo", "lingsha"):
            ch = _load(cid)
            p = _analyze_character(ch)
            w = _compute_substat_weights(ch, p)
            assert w["EFFECT_HIT_RATE"] == 0.0, cid


class TestSpdLookaheadAndConvert:
    """v7.3 a 方案: 断点前瞻（连投 k 条按均值收益）+ 换算型连续收益"""

    def test_marginal_nonzero_far_from_breakpoint(self):
        # 距断点 11 条时旧口径收益恒 0 → 前瞻均值 > 0
        ch = _load("seele")
        p = _analyze_character(ch)
        stats = compute_combat_stats(ch)
        stats.SPD = 102.0
        assert _marginal_benefit(stats, ch, p, "SPD_percent", 3.0) > 0

    def test_aglaea_convert_parsed_and_used(self):
        ch = _load("aglaea")
        p = _analyze_character(ch)
        assert p.spd_convert == pytest.approx(720.0)
        r = recommend_substats(ch, effective_rolls=50)
        assert r["SPD_PERCENT"] >= 10  # 换算收益持续投入（此前 30 条全攻击 0 速度）

    def test_tank_spd_first_breakpoint_only(self):
        # 符玄 tank 保底 0.4 只跨第一个断点（8-19 裁决: 其他词条达标后适当分配）
        ch = _load("fu_xuan")
        p = _analyze_character(ch)
        assert p.spd_tank_only
        r = recommend_substats(ch, effective_rolls=50)
        assert 0 < r["SPD_PERCENT"] <= 15
        assert r["HP_percent"] >= 20  # 主体词条不受侵蚀

    def test_no_signal_still_zero_spd(self):
        # 三语义裁决: 无信号角色不主动投资速度——仅剩均摊份额（≤5 条, 不堆速度）
        assert recommend_substats(_load("seele"))["SPD_PERCENT"] <= 5
        assert recommend_substats(_load("bronya"))["SPD_PERCENT"] <= 5


class TestCritThirdSignal:
    """v7.3: 暴击率行迹≥10 第三信号——占位倍率暴击C不再被误杀"""

    @pytest.mark.parametrize("cid", ["xiadie", "changyeyue", "himeko_nova"])
    def test_placeholder_crit_dps_gets_crit(self, cid):
        r = recommend_substats(_load(cid), effective_rolls=50)
        assert r["CRIT_RATE"] + r["CRIT_DMG"] >= 10, cid

    def test_dot_dps_still_no_crit(self):
        # 海瑟音 DOT 核心: 暴击率行迹 0, 双暴仍排除
        r = recommend_substats(_load("hysilens"), effective_rolls=50)
        assert r["CRIT_RATE"] == 0


class TestIsBreakRefinement:
    """v7.3: 击破文本信号细化——裸"击破"不再误伤 debuff 机制描述"""

    def test_silver_wolf_not_break(self):
        ch = _load("silver_wolf")
        p = _analyze_character(ch)
        assert not p.is_break
        assert recommend_substats(ch, effective_rolls=50)["BREAK_EFFECT"] == 0

    def test_shell_break_chars(self):
        # 波提欧 BE 行迹 37.3 / 乱破显式配置 → 击破定位 + 134 速度达标
        for cid in ("boothill", "rappa"):
            ch = _load(cid)
            assert _analyze_character(ch).is_break, cid
            r = recommend_substats(ch, effective_rolls=50)
            assert r["BREAK_EFFECT"] >= 10, cid
            assert r["SPD_PERCENT"] >= 8, cid

    def test_real_break_text_still_detected(self):
        # 阮·梅"冰击破伤害"/大丽花"击破特攻"关键词仍命中
        assert _analyze_character(_load("ruan_mei")).is_break
        assert _analyze_character(_load("the_dahlia")).is_break


class TestLeftoverEvenSplit:
    """v7.3/7.3.1 裁决: 总词条固定 50; 可调有效词条数（默认 30, 上限 50）,
    有效词条至上限后剩余在非有效词条间平均分配"""

    def test_default_effective30_total50(self):
        r = recommend_substats(_load("seele"))  # 默认有效 30
        assert sum(r.values()) == 50
        assert r["CRIT_RATE"] + r["CRIT_DMG"] == 30  # 有效恰为 30
        assert r["SPD_PERCENT"] <= 5  # 无信号速度仅均摊

    def test_huohuo_even_spread(self):
        full = recommend_substats_full(_load("huohuo"), effective_rolls=30)
        r = full["rolls"]
        assert sum(r.values()) == 50
        # v7.5: 单词条 ≤60%×30=18（单一有效属性被 cap 截断, 剩余均摊加厚）
        assert r["HP_percent"] == 18
        idle = [v for k, v in r.items() if k != "HP_percent"]
        assert max(idle) - min(idle) <= 1  # 最大余数法均摊
        assert min(idle) >= 1

    def test_graduation_split(self):
        full = recommend_substats_full(_load("huohuo"), effective_rolls=30)
        g = full["graduation"]
        assert g["effective_used"] == 18  # v7.5: 单词条 cap 后的有效上限
        assert g["invalid_used"] == 32
        assert g["effective_budget"] == 30  # v7.3.1: 用户可调的有效词条预算
        assert g["budget"] == 50            # 总词条固定

    def test_dps_all_effective(self):
        # 有效词条上限 50 = 全部词条均有效（无非有效词条, 项目主裁决口径）
        r = recommend_substats(_load("seele"), effective_rolls=50)
        assert sum(r.values()) == 50
        assert r["CRIT_RATE"] + r["CRIT_DMG"] + r["ATK_percent"] == 50
        assert r["EFFECT_HIT_RATE"] == 0
        assert r["SPD_PERCENT"] == 0


class TestCapFix:
    """v7.3: _cap 条件式优先级修复（此前恒真, 主词条冲突件从不排除）"""

    def _mains(self):
        return {"head": StatType.HP_FLAT, "hands": StatType.ATK_FLAT,
                "body": StatType.CRIT_DMG, "feet": StatType.SPD_PERCENT,
                "planar_sphere": StatType.ATK_PERCENT, "link_rope": StatType.ATK_PERCENT}

    def test_main_stat_conflict_excluded(self):
        m = self._mains()
        assert _cap("CRIT_DMG", m) == 5  # body 主暴伤冲突
        assert _cap("SPD_percent", m) == 5  # feet 主速度冲突
        assert _cap("ATK_percent", m) == 4  # 球+绳双攻击冲突


class TestRoleAndFrontend:
    def test_ruan_mei_role_support(self):
        # buff 效果引擎手写、JSON effects 为空 → 命途兜底 support（此前 unknown）
        assert _analyze_character(_load("ruan_mei")).role == "support"

    def test_frontend_contract(self):
        # v7.17.0: 键集单源——前端不再内联键字面量, 契约改钉 /api/keysets 端点 + app.js 派生用法
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.api import router
        app = FastAPI()
        app.include_router(router, prefix="/api")
        payload = TestClient(app).get("/api/keysets").json()
        keys = [s["key"] for s in payload["substats"]]
        assert "EFFECT_HIT_RATE" in keys and len(keys) == 9  # 第 9 键效果命中
        spd = next(s for s in payload["substats"] if s["key"] == "SPD_PERCENT")
        assert spd["engine_key"] == "SPD_percent"  # 后端权重键是引擎内部键（速度小写 p）
        js = (Path(__file__).resolve().parents[1]
              / "web" / "static" / "app.js").read_text(encoding="utf-8")
        assert "KEYSETS.substats" in js  # getConfig/推荐映射由端点契约派生
        # v7.3.1: 可调输入=有效词条（默认 30/上限 50）; 总词条固定 50 并在工具栏只读标注
        assert 'id="eff${i}" value="30"' in js and 'max="50"' in js
        assert "TOTAL_ROLLS = 50" in js
        assert "effective_rolls" in js
        assert 'class="field total-field total-fixed"' in js  # 总词条 50 固定标注（禁用只读）
        assert js.count('value="50" type="number" disabled') == 1  # 槽位模板中一处, JS 循环生成四槽
