"""v7.17.0 (M7) —— Web 层键集单源与 app.js 抽离的结构钉扎。

契约链: engine/constants.FRONTEND_ROLL_KEYS+SUBSTAT_KEY_MAP（唯一源）
→ web/api.py GET /api/keysets（web 层短键/标签/主词条 options 单源）
→ web/static/app.js（fetch 派生, 零键字面量）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "static" / "app.js"
INDEX_HTML = ROOT / "web" / "templates" / "index.html"


def _keysets_payload():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    resp = TestClient(app).get("/api/keysets")
    assert resp.status_code == 200
    return resp.json()


class TestKeysetsEndpoint:
    def test_substats_contract_order_and_spd_pairing(self):
        from engine.constants import FRONTEND_ROLL_KEYS, SUBSTAT_KEY_MAP
        subs = _keysets_payload()["substats"]
        assert [s["key"] for s in subs] == list(FRONTEND_ROLL_KEYS) == list(SUBSTAT_KEY_MAP)
        assert len(subs) == 9  # v7.3 九键契约（含效果命中）
        spd = next(s for s in subs if s["key"] == "SPD_PERCENT")
        # 后端权重键是引擎内部键（速度小写 p）——前端置灰映射由此对键派生
        assert spd["engine_key"] == "SPD_percent" and spd["field"] == "spd" and spd["label"] == "速度"
        ehr = next(s for s in subs if s["key"] == "EFFECT_HIT_RATE")
        assert ehr["engine_key"] == "EFFECT_HIT_RATE" and ehr["field"] == "ehr" and ehr["label"] == "命中"
        fields = [s["field"] for s in subs]
        labels = [s["label"] for s in subs]
        assert len(set(fields)) == 9 and len(set(labels)) == 9
        for s in subs:
            assert s["engine_key"] == SUBSTAT_KEY_MAP[s["key"]]

    def test_main_stats_match_engine_pools_and_legacy_defaults(self):
        from engine.constants import RELIC_MAIN_STAT_POOL
        ms = _keysets_payload()["main_stats"]
        slot_map = {"body": "body", "feet": "feet",
                    "sphere": "planar_sphere", "rope": "link_rope"}
        assert set(ms) == set(slot_map)
        for slot, engine_slot in slot_map.items():  # 4 槽全覆盖（含 feet↔feet）
            values = [o["value"] for o in ms[slot]]
            # 前端主词条键历史大小写不一（ATK_percent=value 形, SPD_PERCENT=name 形）——
            # 集合比对按大写归一, 精确键形由下方 legacy-defaults 逐字断言钉住
            assert {v.upper() for v in values if v} == {s.name for s in RELIC_MAIN_STAT_POOL[engine_slot]}
        # 顺序/默认逐字承袭原内联 HTML
        assert ms["body"][0]["value"] == "CRIT_RATE"  # 首项暴击率
        assert [o["value"] for o in ms["body"] if o["selected"]] == ["CRIT_DMG"]  # 默认暴伤
        assert ms["feet"][0]["value"] == "" and ms["feet"][1]["value"] == "SPD_PERCENT"
        assert not any(o["selected"] for o in ms["feet"])  # 默认空 "--"
        assert ms["sphere"][0]["value"] == "DMG_BONUS_QUANTUM"  # 无 selected→首项即隐式默认
        assert not any(o["selected"] for o in ms["sphere"])
        assert [o["value"] for o in ms["rope"] if o["selected"]] == ["ENERGY_REGEN"]  # 默认充能


    def test_main_stats_full_options_pinned(self):
        # 验收 P3-3: 四槽全量 (value, label, selected) 逐项钉扎——防未来按引擎池序重排
        # （sphere 首项量子增伤是隐式默认, 顺序即行为）
        ms = _keysets_payload()["main_stats"]
        assert [(o["value"], o["label"], o["selected"]) for o in ms["body"]] == [
            ("CRIT_RATE", "暴击率", False), ("CRIT_DMG", "暴伤", True),
            ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
            ("DEF_percent", "防御%", False), ("HEAL_BONUS", "治疗加成", False),
            ("EFFECT_HIT_RATE", "效果命中", False)]
        assert [(o["value"], o["label"], o["selected"]) for o in ms["feet"]] == [
            ("", "--", False), ("SPD_PERCENT", "速度", False),
            ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
            ("DEF_percent", "防御%", False)]
        assert [(o["value"], o["label"], o["selected"]) for o in ms["sphere"]] == [
            ("DMG_BONUS_QUANTUM", "量子增伤", False), ("DMG_BONUS_PHYSICAL", "物理增伤", False),
            ("DMG_BONUS_FIRE", "火增伤", False), ("DMG_BONUS_ICE", "冰增伤", False),
            ("DMG_BONUS_LIGHTNING", "雷增伤", False), ("DMG_BONUS_WIND", "风增伤", False),
            ("DMG_BONUS_IMAGINARY", "虚数增伤", False),
            ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
            ("DEF_percent", "防御%", False)]
        assert [(o["value"], o["label"], o["selected"]) for o in ms["rope"]] == [
            ("ATK_percent", "攻击%", False), ("ENERGY_REGEN", "充能", True),
            ("HP_percent", "生命%", False), ("DEF_percent", "防御%", False),
            ("BREAK_EFFECT", "击破特攻", False)]


class TestSingleSource:
    def test_constants_are_the_only_definition(self):
        import engine.constants as C
        from engine.core import relic_optimizer as ro
        from web import api
        assert ro.FRONTEND_ROLL_KEYS is C.FRONTEND_ROLL_KEYS
        assert api.SUBSTAT_KEY_MAP is C.SUBSTAT_KEY_MAP


class TestMainStatCaseCompat:
    """v7.17.0 修复的既有缺陷回归: 主词条 value 形键（ATK_percent, 前端/recommendations
    实际键形）此前在 MAIN_VALUES 按 name 收录下恒取 0。"""

    def test_main_values_dual_keys(self):
        from web.api import MAIN_VALUES
        assert MAIN_VALUES["ATK_percent"] == 43.2
        assert MAIN_VALUES["HP_percent"] == 43.2 and MAIN_VALUES["DEF_percent"] == 54.0
        assert MAIN_VALUES["SPD_PERCENT"] == 25.0 and MAIN_VALUES["SPD_percent"] == 25.0
        assert MAIN_VALUES["CRIT_DMG"] == 64.8  # name==value 键不受影响

    def test_build_relic_pieces_applies_value_form_mains(self):
        from engine.constants import StatType
        from web.api import MAIN_STAT_TYPE, _build_relic_pieces
        assert MAIN_STAT_TYPE["ATK_percent"] == StatType.ATK_PERCENT
        pieces = _build_relic_pieces(
            {"body": "ATK_percent", "feet": "HP_percent",
             "sphere": "DEF_percent", "rope": "SPD_PERCENT"}, {})
        by_slot = {p.slot: p for p in pieces}
        assert by_slot["body"].main_stat_type == StatType.ATK_PERCENT
        assert by_slot["body"].main_stat_value == 43.2
        assert by_slot["feet"].main_stat_value == 43.2
        assert by_slot["planar_sphere"].main_stat_value == 54.0
        assert by_slot["link_rope"].main_stat_value == 25.0


class TestMimiChargeMigration:
    """v7.17.0: _mimi_charge_gain 迁 tbr + _gain_energy 能量库观察者相位化（v7.16.0 验收 P3-2）。"""

    def test_remembrance_no_longer_hosts_mimi(self):
        from engine.systems.remembrance import RemembranceSystem
        assert not hasattr(RemembranceSystem, '_mimi_charge_gain')
        from engine.characters import trailblazer_remembrance as tbr
        assert callable(tbr._mimi_charge_gain)

    def test_energy_bank_registered_as_observer(self):
        from engine.characters import trailblazer_remembrance as tbr
        assert tbr.OBSERVER_HOOKS.get('energy_gain_bank') is tbr._tbr_energy_bank

    def test_gain_energy_has_no_tbr_literal(self):
        import inspect
        from engine.core import combat_engine
        src = inspect.getsource(combat_engine._gain_energy)
        assert 'trailblazer_remembrance' not in src
        assert '_obs_phase' in src

    def test_energy_bank_semantics_direct_call(self):
        # 直调语义不变: 10 点能量一档 → +1% 充能, 银行余数留存
        from types import SimpleNamespace
        from engine.characters.trailblazer_remembrance import _tbr_energy_bank
        ms = SimpleNamespace(extra={'charge': 50.0}, is_alive=True)
        tbr_unit = SimpleNamespace(char=SimpleNamespace(id='trailblazer_remembrance'),
                                   extra={}, memsprite_unit=ms)
        state = SimpleNamespace(units=[tbr_unit], extra={}, current_av=100.0, log=[])
        _tbr_energy_bank(None, state, gained=25)
        assert tbr_unit.extra['tbr_energy_bank'] == 5.0  # 25 → 2 档(20) + 余 5
        assert ms.extra['charge'] == 52.0


class TestFrontendFiles:
    def test_index_html_references_appjs_and_has_no_stat_key_literals(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert '<script src="/static/app.js' in html
        for literal in ("CRIT_RATE", "SPD_PERCENT", "SPD_percent", "EFFECT_HIT_RATE",
                        "DMG_BONUS_QUANTUM", "ENERGY_REGEN"):
            assert literal not in html  # 静态标记零键（键集全部经 /api/keysets）

    def test_appjs_derives_from_keysets_no_hardcoded_maps(self):
        js = APP_JS.read_text(encoding="utf-8")
        assert "KEYSETS.substats" in js and "populateMainSelects" in js
        for literal in ("CRIT_RATE:'cr'", "CRIT_RATE: 'cr'", "SPD_percent:'spd'",
                        "['ehr','命中']", "EFFECT_HIT_RATE: parseInt",
                        "['cr','暴击']"):
            assert literal not in js  # 旧 7 份键集副本的硬编码形态全部清除
        assert "TOTAL_ROLLS = 50" in js  # 前端常量仍驻前端
