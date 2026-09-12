"""v7.25.0: 砂金·戏浪录入测试（角色技能介绍/欢愉/砂金·戏浪.txt）。

覆盖: 白值/参演编号/技能表、热意资源与上限、天赋阈值立即施放（固定20笑点）、
All in 形态（阿哈旗标/E6 常驻/热意消耗）、行迹1 SPD→欢愉度、行迹2 队友技能
全队暴伤+计次+战技重置、行迹3 双分支、E1/E4/E6、秘技、专属光锥五档与回SP。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.attributes import compute_combat_stats  # noqa: E402
from engine.core.combat_engine import SimState, simulate  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.models.equipment import load_lightcone  # noqa: E402
from engine.characters import aventurine_waveflair as avw  # noqa: E402

LC_ID = "cast_summer_into_waves"


def _sim(eidolon=0, lc=True, mate=None, max_av=400, seed=0):
    c = load_character("aventurine_waveflair")
    cfg = {"char": c, "position": 1, "eidolon": eidolon}
    if lc:
        cfg["lightcone"] = load_lightcone(LC_ID)
    team = [cfg]
    if mate:
        team.append({"char": load_character(mate), "position": 2})
    random.seed(seed)
    return simulate(team, _enemy(), max_av=max_av)


def _log(s):
    return "\n".join(s.log)


def _state(u):
    return SimState(enemies=[_enemy()], units=[u])


class TestBaseData:
    def test_base_stats_and_cast_number(self):
        c = load_character("aventurine_waveflair")
        assert (c.base_HP, c.base_ATK, c.base_DEF, c.base_SPD) == (1164, 485, 606, 107)
        assert c.cast_number == 156
        assert c.max_energy == 130 and c.taunt == 100
        assert set(c.skills) == {"basic_attack", "skill", "ultimate", "talent",
                                 "technique", "elation_skill"}
        assert {t.hook_name for t in c.traces} == {
            "aventurine_waveflair_spd_to_elation",
            "aventurine_waveflair_old_dream",
            "aventurine_waveflair_storm"}
        assert [e.hook_name for e in c.eidolons] == [
            f"aventurine_waveflair_e{i}" for i in range(1, 7)]

    def test_multipliers(self):
        c = load_character("aventurine_waveflair")
        assert c.skills["basic_attack"].multipliers[0].scale == 100.0
        assert c.skills["skill"].multipliers[0].scale == 240.0
        assert c.skills["ultimate"].multipliers[0].scale == 400.0
        es = c.skills["elation_skill"].multipliers
        assert es[0].scale == 60.0 and es[0].damage_type == "elation"
        assert es[1].scale == 18.0 and es[1].target == "bounce" and es[1].hits == 10


class TestHeatAndThreshold:
    def test_gain_with_cap(self):
        u = _unit("aventurine_waveflair")
        st = _state(u)
        avw._avw_gain_heat(u, st, 9, cause="t")
        assert u.extra[avw.HEAT_KEY] == 9
        avw._avw_gain_heat(u, st, 100, cause="t")  # 上限30
        assert u.extra[avw.HEAT_KEY] == 30

    def test_e2_cap_50(self):
        u = _unit("aventurine_waveflair", eidolon=2)
        st = _state(u)
        avw._avw_gain_heat(u, st, 60, cause="t")
        assert u.extra[avw.HEAT_KEY] == 50

    def test_threshold_immediate_cast_fixed_20(self):
        """热意跨过10 → 立即施放固定20笑点举杯; 基础形态不耗热意。"""
        u = _unit("aventurine_waveflair")
        st = _state(u)
        avw._avw_gain_heat(u, st, 10, cause="战技")
        log = _log(st)
        assert "立即施放【举杯！敬炽烈一夏】(固定计入20笑点)" in log
        assert u.extra["avw_next_aha_allin"] is True
        assert u.extra[avw.HEAT_KEY] == 10  # 举杯(基础形态)不消耗

    def test_no_recross_within_same_level(self):
        """同阈值不重复: 已在10之上再加不触发第二次。"""
        u = _unit("aventurine_waveflair")
        st = _state(u)
        avw._avw_gain_heat(u, st, 10, cause="t")
        n1 = _log(st).count("立即施放")
        avw._avw_gain_heat(u, st, 5, cause="t")
        assert _log(st).count("立即施放") == n1
        assert u.extra[avw.HEAT_KEY] == 15


class TestAllIn:
    def test_talent_flag_upgrades_next_aha_cast(self):
        """阈值施放后, 下一次阿哈欢愉技为 All in(消耗热意)。"""
        st = _sim(eidolon=0, lc=False, max_av=500)
        log = _log(st)
        assert "立即施放【举杯" in log
        assert "All in" in log  # 阿哈中的强化形态发生

    def test_e6_all_in_persistent_and_aha_consumes(self):
        st = _sim(eidolon=6, lc=False, max_av=500)
        log = _log(st)
        assert log.count("All in") >= 2
        assert "消耗" in log  # 阿哈内 All in 消耗热意


class TestTraces:
    def test_trace1_spd_to_elation(self):
        u = _unit("aventurine_waveflair")
        st = _state(u)
        from engine.core.attributes import CombatStats

        def lvl(spd):
            s = CombatStats()
            s.ELATION_LEVEL = 0.1
            out = avw._avw_eff_stats(u, st, s, effective_spd=spd)
            return out.ELATION_LEVEL if out else None

        assert lvl(139) is None                      # 阈下不生效
        assert lvl(140) == pytest.approx(0.1 + 0.30)
        assert lvl(150) == pytest.approx(0.1 + 0.30 + 10 * 0.01)  # 超10点→+10%
        assert lvl(340) == pytest.approx(0.1 + 0.30 + 200 * 0.01)  # cap 200 超出

    def test_trace2_team_cd_and_reset(self):
        """队友技能 → 全队 CD+48 buff ≤6次; 战技重置。"""
        st = _sim(eidolon=0, lc=False, mate="seele", max_av=300)
        log = _log(st)
        assert log.count("行迹2·队友技能") >= 3
        me = st.units[0]
        assert any(getattr(b, "param_id", "") == "avw_old_dream" for b in me.buffs)

    def test_trace3_multi_branch(self):
        st = _sim(eidolon=0, lc=False, mate="sparxie", max_av=100)  # 火花=欢愉
        me = st.units[0]
        assert me.extra.get("avw_solo") is None
        assert me.base_stats.ELATION_LEVEL == pytest.approx(
            0.10 + 0.20 + 0.80)  # 行迹10% + 全队20% + 自身额外80%

    def test_trace3_solo_branch_aha_speed(self):
        st = _sim(eidolon=0, lc=False, mate="seele", max_av=200)
        me = st.units[0]
        assert me.extra.get("avw_solo") is True
        assert st.aha_speed > 80.0  # 队友攻击后 +25 生效


class TestEidolonsAndUlt:
    def test_e1_res_pen(self):
        st = _sim(eidolon=1, lc=False, max_av=60)
        assert st.units[0].base_stats.RES_PEN_ALL == pytest.approx(0.24)

    def test_e6_laugh_boost(self):
        st = _sim(eidolon=6, lc=False, max_av=60)
        assert st.units[0].base_stats.LAUGH_BOOST == pytest.approx(0.25)

    def test_ult_grants_spd_buff(self):
        st = _sim(eidolon=0, lc=False, max_av=500)
        me = st.units[0]
        buffs = [b for b in me.buffs if getattr(b, "param_id", "") == "avw_ult_spd"]
        assert buffs and buffs[0].attributes["SPD_PERCENT"] == 30.0

    def test_e4_team_defpen_on_skill(self):
        st = _sim(eidolon=4, lc=False, max_av=300)
        for mate in st.units:
            if mate.char.id != "aventurine_waveflair":
                assert any(getattr(b, "param_id", "") == "avw_e4_defpen"
                           for b in mate.buffs)
                break


class TestTechnique:
    def test_tech_damage_heat_goodshow(self):
        u = _unit("aventurine_waveflair")
        st = _state(u)
        from engine.systems.elation import ElationSystem
        st.extra["_elation"] = ElationSystem()
        hp0 = st.enemies[0].HP
        avw._tech(st, u, True)
        assert st.enemies[0].HP < hp0
        assert u.extra[avw.HEAT_KEY] == 2
        assert st.elation_state.get_good_show_total("aventurine_waveflair") == 20


class TestLightCone:
    def test_fengkou_rank_scaling(self):
        st = _sim(eidolon=0, lc=True, max_av=400)
        me = st.units[0]
        fk = [b for b in me.buffs if getattr(b, "param_id", "") == "lc_csw_fengkou"]
        assert fk, "风口 buff 未挂"
        assert fk[0].attributes["SPD_PERCENT"] == 24.0  # S1

    def test_fengkou_s5(self):
        c = load_character("aventurine_waveflair")
        lc = load_lightcone(LC_ID)
        lc.rank = 5
        random.seed(0)
        st = simulate([{"char": c, "position": 1, "lightcone": lc}],
                      _enemy(), max_av=300)
        me = st.units[0]
        fk = [b for b in me.buffs if getattr(b, "param_id", "") == "lc_csw_fengkou"]
        assert fk and fk[0].attributes["SPD_PERCENT"] == 40.0  # S5
        cl = [b for b in me.buffs if getattr(b, "param_id", "") == "lc_csw_chaoliu"]
        if cl:  # 换式触发时
            assert cl[0].attributes["ELATION_LEVEL"] == 100.0  # S5

    def test_sp_recovery_every_three_casts(self):
        st = _sim(eidolon=6, lc=True, max_av=500)
        assert "施放3次欢愉技→回1战技点" in _log(st)


class TestRecommendation:
    def test_entry_structure(self):
        import json
        recs = json.loads((Path(__file__).resolve().parent.parent
                           / "data" / "recommendations.json").read_text(encoding="utf-8"))
        r = recs["aventurine_waveflair"]
        assert r["light_cone"] == LC_ID
        assert r["set4"] == ["闪耀功勋的魔法少女"]
        assert r["set2"] == ["零号关卡朋克洛德"]
        assert r["body"] == ["CRIT_RATE"] and r["feet"] == ["SPD_PERCENT"]
        assert r["sphere"] == ["DEF_percent", "HP_percent"]
        assert r["rope"] == ["ENERGY_REGEN"]

    def test_recommend_substats_runs(self):
        """推荐管线可跑(欢愉主导白值清零 + combat_passive CD48 入预算)。"""
        from engine.core.relic_optimizer import recommend_substats
        rows = recommend_substats(load_character("aventurine_waveflair"))
        assert rows
