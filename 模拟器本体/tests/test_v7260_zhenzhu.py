"""v7.26.0: 真珠录入测试（角色技能介绍/欢愉/真珠.txt）+ 五则项目主裁决管线专项。

覆盖: 白值/参演编号/八键技能表、DEF伤害与DEF治疗双通道、好活点池(上限50/无限时长/
抵御值消耗)、深度学习/美学底本状态机(拉条三档/四欢愉额外回合/临时好活笑点给予-收回/
充能)、欢愉技rider四档与消耗、三行迹、E1-E6、秘技、专属光锥五档、两套新遗器、
推荐与DEF主词条; 管线裁决: 银狼隐藏分实时叠加/阿哈结算不喂分/爻光额外阿哈不动轴与池。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.attributes import compute_combat_stats  # noqa: E402
from engine.core.combat_engine import (  # noqa: E402
    HEAL_REGISTRY, SimState, simulate, _lc_rank_value)
from engine.models.character import load_character  # noqa: E402
from engine.models.equipment import load_lightcone  # noqa: E402
from engine.characters import zhenzhu as zz  # noqa: E402

LC_ID = "hues_devoted_to_tomorrow"


def _sim(mates=(), eidolon=0, lc=True, max_av=400, seed=0, zz_pos=1):
    cfg = {"char": load_character("zhenzhu"), "position": zz_pos,
           "eidolon": eidolon}
    if lc:
        cfg["lightcone"] = load_lightcone(LC_ID)
    team = [cfg] + [{"char": load_character(m), "position": i + 2}
                    for i, m in enumerate(mates)]
    random.seed(seed)
    return simulate(team, _enemy(), max_av=max_av)


def _log(s):
    return "\n".join(s.log)


def _state(*units):
    st = SimState(enemies=[_enemy()], units=list(units))
    return st


def _zz_unit(eidolon=0):
    return _unit("zhenzhu", eidolon=eidolon)


class TestBaseData:
    def test_base_stats_and_cast_number(self):
        c = load_character("zhenzhu")
        assert (c.base_HP, c.base_ATK, c.base_DEF, c.base_SPD) == (1203, 465, 727, 99)
        assert c.cast_number == 104
        assert c.max_energy == 180 and c.taunt == 100
        assert set(c.skills) == {"basic_attack", "basic_attack_enhanced",
                                 "zz_basic_dream", "skill", "ultimate",
                                 "talent", "technique", "elation_skill"}
        assert {t.hook_name for t in c.traces} == {
            "zhenzhu_art_barrier", "zhenzhu_perception_tolerance",
            "zhenzhu_insight_all"}
        assert [e.hook_name for e in c.eidolons] == [f"zhenzhu_e{i}" for i in range(1, 7)]

    def test_trace_stats(self):
        ts = load_character("zhenzhu").trace_stats
        assert ts["DEF_percent"] == 22.5 and ts["SPD_percent"] == 9.0
        assert ts["EFFECT_RES"] == 10.0 and ts["ELATION_LEVEL"] == 10.0

    def test_def_scaling_multipliers(self):
        c = load_character("zhenzhu")
        for key, scale in (("basic_attack", 90.0), ("basic_attack_enhanced", 100.0),
                           ("zz_basic_dream", 100.0)):
            m = c.skills[key].multipliers[0]
            assert m.stat == "DEF" and m.scale == scale, key
            assert m.damage_type == "direct" and m.element == "冰"
        assert c.skills["skill"].multipliers == []
        assert c.skills["elation_skill"].multipliers == []

    def test_heal_registry_def_entries(self):
        assert HEAL_REGISTRY["zhenzhu_skill_heal"] == \
            {"stat": "DEF", "hp_pct": 12.0, "flat": 240.0}
        assert HEAL_REGISTRY["zhenzhu_enh_heal"] == \
            {"stat": "DEF", "hp_pct": 8.0, "flat": 160.0}


class TestDefChannels:
    def test_def_damage_channel(self):
        """普攻 90% DEF 直伤: 手算对齐(未击破0.9/期望暴击口径由引擎统一)。"""
        u = _zz_unit()
        st = _state(u)
        from engine.core.combat_engine import _use_skill
        random.seed(3)
        _use_skill(u, st, "basic_attack")
        entry = u.damage_log[-1]
        assert entry[1] > 0 and entry[2] == "basic_attack"
        # 量级: 90% DEF(面板约 890) 的直伤(含减伤/韧性乘区)
        assert entry[1] > 350

    def test_def_heal_channel(self):
        """战技全队治疗: DEF 基数(12%DEF+240)。"""
        u = _zz_unit()
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        u2 = u
        u2.current_hp -= 500
        mate.current_hp -= 500
        from engine.core.combat_engine import _use_skill
        random.seed(3)
        st.skill_points = 5
        _use_skill(u2, st, "skill")
        stats = u2.base_stats
        expect = stats.DEF * 0.12 + 240.0
        healed = u2.current_hp - (stats.HP - 500)
        assert healed == pytest.approx(expect, rel=0.02), (healed, expect)

    def test_lowest_hp_extra_heal(self):
        """额外治疗落在生命百分比最低目标(战技 12%DEF+240 同额)。"""
        u = _zz_unit()
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        mate.current_hp = mate.max_hp * 0.2   # 百分比更低
        before = mate.current_hp
        zz._zz_extra_lowest_heal(u, st, "skill")
        assert mate.current_hp > before
        stats = u.base_stats
        assert mate.current_hp - before == pytest.approx(
            (stats.DEF * 0.12 + 240.0) * (1.0 + stats.HEAL_BONUS), rel=0.02)


class TestGoodshowPool:
    def test_gain_cap_50(self):
        u = _zz_unit()
        st = _state(u)
        zz._zz_gain(u, st, 60, cause="t")
        assert st.elation_state.get_good_show_total("zhenzhu") == 50.0

    def test_infinite_duration(self):
        """好活批次 duration=-1: tick 两回合不过期(裁决1 无限时长前提)。"""
        u = _zz_unit()
        st = _state(u)
        zz._zz_gain(u, st, 10, cause="t")
        es = st.elation_state
        es.tick_good_show("zhenzhu")
        es.tick_good_show("zhenzhu")
        es.tick_good_show("zhenzhu")
        assert es.get_good_show_total("zhenzhu") == 10.0
        assert all(b.remaining_turns < 0 for b in es.good_shows["zhenzhu"])

    def test_consume_good_show_newest_first(self):
        u = _zz_unit()
        st = _state(u)
        zz._zz_gain(u, st, 20, cause="a")
        zz._zz_gain(u, st, 30, cause="b")
        got = st.elation_state.consume_good_show("zhenzhu", 25)
        assert got == 25
        assert st.elation_state.get_good_show_total("zhenzhu") == 25.0

    def test_goodshow_zz_rewrites_external_grants(self):
        """外部来源(阿哈转化 duration=2)经 goodshow_zz 观察相位改写为无限。"""
        u = _zz_unit()
        st = _state(u)
        from engine.systems.elation import ElationSystem
        esys = ElationSystem()
        esys.grant_good_show(st, "zhenzhu", 7, duration=2, source="aha")
        batches = st.elation_state.good_shows["zhenzhu"]
        assert batches and all(b.remaining_turns == -1 for b in batches)


class TestTalentAbsorb:
    def test_block_consumes_pool(self):
        """抵挡60%: 伤害1000→先扣60%=600, 需3点好活(每点200)。"""
        u = _zz_unit()
        st = _state(u)
        zz._zz_gain(u, st, 10, cause="t")
        out = zz._zz_absorb_check(st, u, 1000.0)
        assert out == pytest.approx(400.0)
        assert st.elation_state.get_good_show_total("zhenzhu") == pytest.approx(7.0)

    def test_partial_pool(self):
        u = _zz_unit()
        st = _state(u)
        zz._zz_gain(u, st, 1, cause="t")   # 200抵御值
        out = zz._zz_absorb_check(st, u, 1000.0)
        assert out == pytest.approx(800.0)  # 只挡200
        assert st.elation_state.get_good_show_total("zhenzhu") == 0.0

    def test_low_hp_dr(self):
        u = _zz_unit()
        st = _state(u)
        u.current_hp = u.max_hp * 0.5
        zz._zz_gain(u, st, 50, cause="t")
        out = zz._zz_absorb_check(st, u, 1000.0)
        # 1000×0.7(DR)=700 → 挡60%=420 → 余 280
        assert out == pytest.approx(280.0)


class TestDeepLearning:
    def test_ult_designates_base_and_advance(self):
        """终结技: 指定底本(首个非自身欢愉) + 拉条15%(2欢愉)。"""
        st = _sim(mates=("sparxie", "seele"), max_av=1200, seed=7)
        log = _log(st)
        assert "获得1个额外回合" in log or "行动提前15%" in log
        pearl = next(x for x in st.units if x.char.id == "zhenzhu")
        assert pearl.extra.get("zz_base") is not None
        assert pearl.extra["zz_base"].char.id == "sparxie"

    def test_dream_form_when_base_elation(self):
        st = _sim(mates=("sparxie",), max_av=1200, seed=7)
        assert "行笔，幻造星月" in _log(st)

    def test_enhanced_form_when_base_not_elation(self):
        st = _sim(mates=("seele",), max_av=1200, seed=7)
        log = _log(st)
        assert "行笔，绘制末浪" in log and "幻造星月" not in log

    def test_charges_and_end(self):
        # 火花底本→幻造星月形态(回SP不耗SP), 三充能快速走完一个周期
        st = _sim(mates=("sparxie",), max_av=2000, seed=7)
        assert "充能耗尽, 结束" in _log(st)

    def test_four_elation_extra_turn_grant_reclaim(self):
        """4欢愉队: 底本额外回合开始+30好活+60笑点, 结束收回(只扣发出的份额)。"""
        st = _sim(mates=("sparxie", "evanescia", "yaoguang"), max_av=1500, seed=11)
        log = _log(st)
        assert "额外回合开始: +30好活+60笑点" in log
        assert "额外回合结束: 收回临时好活/笑点(60笑点)" in log

    def test_e2_double_grants(self):
        st = _sim(mates=("sparxie", "evanescia", "yaoguang"), eidolon=2,
                  max_av=1500, seed=11)
        assert "额外回合开始: +60好活+120笑点" in _log(st)

    def test_temp_laugh_reclaim_keeps_rest_of_pool(self):
        """裁决5: 回收只扣发出的60, 池内其余笑点不动。"""
        u = _zz_unit()
        st = _state(u)
        from engine.systems.elation import gain_laugh
        st.laugh_points = 25.0          # 既有笑点
        mate = _unit("sparxie", position=2)
        st.units.append(mate)
        zz._zz_extra_turn_start(mate, st, kind="zz_base")
        assert st.laugh_points == 85.0
        zz._zz_extra_turn_end(mate, st, kind="zz_base")
        assert st.laugh_points == 25.0  # 只收回60
        # 好活也已收回
        assert st.elation_state.get_good_show_total("sparxie") == 0.0


class TestRider:
    def test_tiers_by_elation_count(self):
        for mates, expect in (((), 10.0), ("seele", 10.0), ("sparxie", 15.0),
                              ("sparxie,evanescia", 20.0),
                              ("sparxie,evanescia,yaoguang", 40.0)):
            st = _sim(mates=tuple(mates.split(",")) if mates else (),
                      max_av=300, seed=1)
            pearl = next(x for x in st.units if x.char.id == "zhenzhu")
            # 手动触发一次欢愉技 arming 验证档位
            pearl.extra.pop("zz_rider_scale", None)
            zz._zz_elation_arm(pearl, st, "elation_skill")
            scales = [x.extra.get("zz_rider_scale") for x in st.units]
            assert all(s == expect for s in scales), (mates, scales)

    def test_e4_doubles(self):
        st = _sim(mates=("sparxie", "evanescia", "yaoguang"), eidolon=4,
                  max_av=300, seed=1)
        pearl = next(x for x in st.units if x.char.id == "zhenzhu")
        zz._zz_elation_arm(pearl, st, "elation_skill")
        assert pearl.extra["zz_rider_scale"] == 80.0

    def test_rider_consumed_on_attack(self):
        st = _sim(mates=("seele",), max_av=1200, seed=7)
        assert "攻击后追加" in _log(st) and "属性欢愉伤害" in _log(st)


class TestTraces:
    def test_trace1_base_ult_restores_90_energy(self):
        u = _zz_unit()
        base = _unit("sparxie", position=2)
        st = _state(u, base)
        u.extra["zz_base"] = base
        u.extra["zz_trace1_armed"] = True
        u.current_energy = 10.0
        zz._zz_after_ult(base, st)
        assert u.current_energy == pytest.approx(100.0)
        assert u.extra["zz_trace1_armed"] is False
        # 不叠加: 未重臂前再放终结技不加
        zz._zz_after_ult(base, st)
        assert u.current_energy == pytest.approx(100.0)

    def test_trace2_turn_gain_and_cap(self):
        u = _zz_unit()
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        u.extra["zz_t2_gained"] = 48.0
        zz._zz_ally_turn_start(mate, st)   # 队友回合: 上限只让 +2
        assert u.extra["zz_t2_gained"] == 50.0
        assert st.elation_state.get_good_show_total("zhenzhu") == pytest.approx(2.0)

    def test_trace2_reset_on_own_turn(self):
        u = _zz_unit()
        st = _state(u)
        u.extra["zz_t2_gained"] = 50.0
        zz._zz_ally_turn_start(u, st)   # 自身回合开始: 重置后 +5
        assert u.extra["zz_t2_gained"] == 5.0

    def test_trace2_res_sync(self):
        u = _zz_unit()
        st = _state(u)
        base_res = u.base_stats.EFFECT_RES
        zz._zz_gain(u, st, 5, cause="t")
        assert u.base_stats.EFFECT_RES == pytest.approx(base_res + 0.50)
        st.elation_state.consume_good_show("zhenzhu", 5)
        zz._zz_sync_pool(st)
        assert u.base_stats.EFFECT_RES == pytest.approx(base_res)

    def test_trace3_def_thresholds(self):
        from engine.core.attributes import CombatStats
        u = _zz_unit()
        st = _state(u)

        def _el(defv):
            s = CombatStats()
            s.DEF = defv
            s.ELATION_LEVEL = 0.1
            s.HEAL_BONUS = 0.0
            out = zz._zz_eff_stats(u, st, s, effective_spd=100)
            return out

        assert _el(2399.0) is None                       # 未达阈值
        s = _el(2400.0)
        assert s.ELATION_LEVEL == pytest.approx(0.1 + 0.32)
        s = _el(2500.0)
        assert s.ELATION_LEVEL == pytest.approx(0.1 + 0.32 + 0.03)
        s = _el(6000.0)                                  # 超量封顶3600
        assert s.ELATION_LEVEL == pytest.approx(0.1 + 0.32 + 1.08)
        assert s.HEAL_BONUS == pytest.approx(s.ELATION_LEVEL * 0.20)


class TestEidolons:
    def test_e1_team_elation_boost(self):
        # 4欢愉(真珠+火花+绯英+爻光) → 全队+60%; 真珠面板 = 行迹0.10 + 0.60
        st = _sim(mates=("sparxie", "evanescia", "yaoguang"), eidolon=1,
                  lc=False, max_av=200, seed=1)
        pearl = next(x for x in st.units if x.char.id == "zhenzhu")
        assert pearl.base_stats.ELATION_LEVEL == pytest.approx(0.10 + 0.60)
        # 2欢愉(真珠+火花) → +10%
        st2 = _sim(mates=("sparxie", "seele"), eidolon=1, lc=False,
                   max_av=200, seed=1)
        pearl2 = next(x for x in st2.units if x.char.id == "zhenzhu")
        assert pearl2.base_stats.ELATION_LEVEL == pytest.approx(0.10 + 0.10)

    def test_e1_fatal_save_twice(self):
        u = _zz_unit(eidolon=1)
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        st.extra["zz_e1_charges"] = 2
        mate.current_hp = 0.0
        assert zz._zz_e1_fatal_check(st, mate) is True
        assert mate.current_hp == mate.max_hp * 0.5
        mate.current_hp = 0.0
        assert zz._zz_e1_fatal_check(st, mate) is True
        mate.current_hp = 0.0
        assert zz._zz_e1_fatal_check(st, mate) is False   # 次数耗尽
        assert mate.current_hp == 0.0

    def test_e2_laugh_boost_team(self):
        st = _sim(mates=("sparxie",), eidolon=2, lc=False, max_av=200, seed=1)
        for x in st.units:
            assert x.base_stats.LAUGH_BOOST == pytest.approx(0.15)

    def test_e3_e5_skill_levels(self):
        from engine.core.effect_resolver import _eid_skill_levels
        u = _zz_unit(eidolon=5)
        st = _state(u)
        _eid_skill_levels(u, st)
        # E3: 终结技+2/普攻+1/欢愉技+1; E5: 战技+2/天赋+2/欢愉技+1(叠加=2)
        assert u.extra["skill_level_boost"] == {
            "ultimate": 2, "basic_attack": 1, "elation_skill": 2,
            "skill": 2, "talent": 2}

    def test_e6_respen_during_dl(self):
        u = _zz_unit(eidolon=6)
        mate = _unit("seele", position=2)
        st = _state(u, mate)
        base = u.base_stats.RES_PEN_ALL
        u.extra["zz_dl"] = True
        zz._zz_sync_e6(st)
        assert mate.base_stats.RES_PEN_ALL == pytest.approx(base + 0.20)
        u.extra["zz_dl"] = False
        zz._zz_sync_e6(st)
        assert mate.base_stats.RES_PEN_ALL == pytest.approx(base)


class TestTechnique:
    def test_technique_grants_dl_2_charges(self):
        u = _zz_unit()
        st = _state(u)
        zz._zz_tech(st, u, is_opener=True)
        assert u.extra["zz_dl"] is True and u.extra["zz_dl_charges"] == 2
        assert st.elation_state.get_good_show_total("zhenzhu") == 20.0


class TestLightcone:
    def test_five_tiers(self):
        for rank, vuln in ((1, 22.0), (3, 33.0), (5, 44.0)):
            u = _unit("zhenzhu", lc_id=LC_ID)
            u.lightcone.rank = rank
            assert _lc_rank_value(u, 22.0, code="event_elation_skill") == vuln
            assert _lc_rank_value(u, 10.0,
                                  code="event_elation_skill_hues_heal") == \
                {1: 10.0, 3: 15.0, 5: 20.0}[rank]

    def test_handler_fires_on_ally_targeted_elation(self):
        st = _sim(max_av=300, seed=1)
        assert "光锥[献给明日的色彩]" in _log(st)

    def test_permanent_def_panel(self):
        stats = compute_combat_stats(load_character("zhenzhu"),
                                     load_lightcone(LC_ID), None, None)
        bare = compute_combat_stats(load_character("zhenzhu"), None, None, None)
        assert stats.DEF > bare.DEF * 1.4   # 48% DEF up


class TestRelics:
    def test_sets_exist_and_structure(self):
        for name in ("戏梦点星的伶人", "贪噬禁果的异端", "生命的翁瓦克"):
            import json
            found = [p for p in Path("data/relics").glob("*.json")
                     if json.loads(p.read_text(encoding="utf-8"))["name"] == name]
            assert found, name

    def test_lingren_4pc_single_ally_target(self):
        from engine.core.relic_conditions import _on_ally_skill_elation_buff
        u = _zz_unit()
        mate = _unit("sparxie", position=2)
        st = _state(u, mate)
        zz._zz_gain(u, st, 15, cause="t")    # 好活≥10 → 附加全队CD
        _on_ally_skill_elation_buff(u, st, char_id="zhenzhu", target=mate,
                                    skill_key="ultimate")
        assert any(b.source_name == "伶人4pc" for b in mate.buffs)
        assert any(b.source_name == "伶人4pc" for b in u.buffs)

    def test_yiduan_4pc_basic_atk(self):
        from engine.core.relic_conditions import _on_basic_atk_buff
        u = _zz_unit()
        st = _state(u)
        _on_basic_atk_buff(u, st, skill_key="zz_basic_dream")
        assert any(b.source_name == "异端4pc" for b in u.buffs)
        u.buffs.clear()
        _on_basic_atk_buff(u, st, skill_key="skill")
        assert not u.buffs


class TestRecommend:
    def test_recommendation_entry(self):
        import json
        rec = json.loads(Path("data/recommendations.json")
                         .read_text(encoding="utf-8"))["zhenzhu"]
        assert rec["light_cone"] == LC_ID
        assert rec["set4"] == ["戏梦点星的伶人"]
        assert rec["body"][0] == "HEAL_BONUS" and "DEF_percent" in rec["rope"]

    def test_primary_stat_def_and_not_elation_dom(self):
        from engine.core.relic_optimizer import _analyze_character
        c = load_character("zhenzhu")
        prof = _analyze_character(c)
        assert prof.primary_stat == "DEF"
        # 欢愉权重为0(纯模块rider不进JSON倍率) → 非欢愉主导 → DEF词条不被清零
        assert prof.weights.get("elation", 0.0) < prof.weights.get("direct", 1.0) * 0.9


class TestPipelineRulings:
    """五则项目主裁决(2026-09-29)的管线专项。"""

    def test_teammate_laugh_syncs_hidden_score_realtime(self):
        """裁决3: 任何角色挣的笑点即时同步银狼隐藏分。"""
        u = _unit("zhenzhu")
        yl = _unit("yinlang", position=2)
        st = _state(u, yl)
        from engine.systems.elation import gain_laugh
        hs0 = yl.hidden_score
        gain_laugh(st, 7)
        assert st.laugh_points == 7.0
        assert yl.hidden_score == pytest.approx(hs0 + 7)

    def test_aha_settle_no_longer_feeds_hidden(self):
        u = _unit("yinlang")
        st = _state(u)
        from engine.systems.elation import ElationSystem
        st.extra["_elation"] = ElationSystem()
        hs0 = u.hidden_score
        st.laugh_points = 12.0
        st.extra["_elation"].execute_aha(st)
        # 阿哈施放期间她自己欢愉技+15笑(实时计入); 结算对池内既有12笑不再喂分
        # (旧口径: 15+27=42; 新口径: 15)
        assert u.hidden_score == pytest.approx(hs0 + 15.0)

    def test_temp_laugh_syncs_hidden_and_reclaim_keeps(self):
        """裁决1/3/5: 临时笑点授予即计入隐藏分, 回收不扣。"""
        u = _zz_unit()
        yl = _unit("yinlang", position=2)
        st = _state(u, yl)
        hs0 = yl.hidden_score
        zz._zz_extra_turn_start(yl, st, kind="zz_base")
        assert yl.hidden_score == pytest.approx(hs0 + 60)
        zz._zz_extra_turn_end(yl, st, kind="zz_base")
        assert yl.hidden_score == pytest.approx(hs0 + 60)   # 不扣

    def test_yao_extra_aha_keeps_axis_and_pool(self):
        """裁决4: 爻光终结技叫来额外阿哈——不动跑条阿哈轴/不动原笑点池。"""
        yao = _unit("yaoguang")
        st = _state(yao)
        from engine.systems.elation import ElationSystem
        esys = ElationSystem()
        st.extra["_elation"] = esys
        st.laugh_points = 33.0
        st.aha_speed = 100.0
        st.aha_next_av = 500.0
        yao.current_energy = yao.char.max_energy
        esys.execute_extra_aha(st, 20)
        assert st.laugh_points == pytest.approx(33.0)     # 池不动
        assert st.aha_next_av == pytest.approx(500.0)     # 轴不动
        # 每个欢愉角色+20好活(固定值转化)
        assert st.elation_state.get_good_show_total("yaoguang") == pytest.approx(20.0)


class TestTeamSmoke:
    def test_four_elation_team_runs(self):
        st = _sim(mates=("sparxie", "evanescia", "yaoguang"), max_av=2000, seed=3)
        pearl = next(x for x in st.units if x.char.id == "zhenzhu")
        assert pearl.total_damage_dealt > 0
        assert all(x.current_hp > 0 for x in st.units)
        log = _log(st)
        assert "幻造星月" in log          # 底本欢愉→深形态
        assert "额外回合开始" in log       # 4欢愉额外回合
