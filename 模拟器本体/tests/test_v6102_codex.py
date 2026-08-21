"""v6.10.2 Codex regression tests for the full audit fixes."""
import copy
import json
import re
from types import SimpleNamespace
from pathlib import Path

import pytest

from engine.core.attributes import CombatStats, compute_combat_stats
from engine.core.combat_sim import (
    SimState,
    SimUnit,
    TimedBuff,
    _build_effective_stats,
    _anaxa_add_weakness,
    _anaxa_reveal_check,
    _cerydra_grant_jungong,
    _check_fatal,
    _commit_enemy_damage,
    _deduct_skill_point_cost,
    _dht_longling_action,
    _enemy_for_damage,
    _hysilens_field,
    _qianye_enter_wrath,
    _qianye_e6_gain_charge,
    _qianye_exit_wrath,
    _silver_wolf_apply_entry_effects,
    _sunday_apply_mentor,
    _sunday_apply_cr_buff,
    _sunday_skill,
    _sunday_tick,
    _tick_buffs,
    _target_attacker_stats,
    _use_skill,
    _register_elation_skill_hooks,
)
from engine.core.damage import calculate_damage
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.models.memsprite import MemSprite
from engine.systems.elation import ElationSystem
import engine.systems.elation as elation_module
from engine.systems.remembrance import MemSpriteUnit
from engine.core.effect_resolver import (
    EIDOLON_REGISTRY,
    TRACE_REGISTRY,
    _eid_bronya_e4,
    _trace_dht_trace2,
)
from engine.core.combat_utils import _tech_yinlang


def _enemy(hp=500000.0, res=0.0):
    return Enemy(id="target", name="Target", HP=hp, ATK=100, DEF=800,
                 SPD=80, toughness=200, max_toughness=200, level=80,
                 element_res={e: res for e in ("物理", "火", "冰", "雷", "风", "量子", "虚数")})


def _unit(cid, position=1, eidolon=0):
    char = load_character(cid, "data/characters")
    stats = compute_combat_stats(char, None, None, None)
    unit = SimUnit(char=char, base_stats=stats, position=position)
    unit.max_hp = unit.current_hp = stats.HP
    unit.eidolon_rank = eidolon
    return unit


def _state(*units, enemy=None):
    return SimState(enemies=[enemy or _enemy()], units=list(units))


def test_commit_enemy_damage_records_one_kill_and_on_kill():
    attacker = _unit("seele")
    target = _enemy(hp=1.0)
    state = _state(attacker, enemy=target)
    seen = []
    state.hooks.register("seele", "on_kill",
                         lambda u, state, enemy=None, **_: seen.append(enemy))

    actual_damage, killed = _commit_enemy_damage(state, attacker, target, 5.0)

    assert actual_damage == pytest.approx(1.0)
    assert killed is True
    assert state.extra["killed_total"] == 1
    assert state.extra["killed_this_action"] == 1
    assert seen == [target]


def test_anaxa_e6_is_independent_original_damage_multiplier():
    anaxa = _unit("anaxa", eidolon=6)
    state = _state(anaxa)
    stats = _build_effective_stats(anaxa, state)
    baseline = copy.deepcopy(stats)
    baseline.DAMAGE_MULTIPLIER = 1.0
    enemy = _enemy()

    boosted = calculate_damage(stats, _enemy_for_damage(enemy), stats.ATK, 100.0,
                                "direct", "风", 80, False)
    normal = calculate_damage(baseline, _enemy_for_damage(enemy), baseline.ATK, 100.0,
                              "direct", "风", 80, False)

    assert stats.DAMAGE_MULTIPLIER == pytest.approx(1.30)
    assert boosted.final_damage / normal.final_damage == pytest.approx(1.30)


def test_anaxa_revealed_target_consumes_trace3_and_reveal_bonus():
    anaxa = _unit("anaxa")
    target = _enemy()
    anaxa.extra["anaxa_trace3"] = True
    target.extra["anaxa_revealed"] = True
    for i, element in enumerate(("物理", "火", "冰", "雷", "风", "量子", "虚数")):
        target.add_status(EnemyStatus(
            id=f"anaxa_weak_{i}", name="弱点", category="debuff", source="anaxa",
            remaining_turns=2, attributes={"weakness_element": element}))
    stats = _target_attacker_stats(anaxa.base_stats, anaxa, _state(anaxa, enemy=target), target)

    assert stats.DEF_PEN == pytest.approx(0.28)
    assert stats.DMG_BONUS_ALL == pytest.approx(0.18)


def test_anaxa_counts_natural_weaknesses_for_reveal_and_trace3(monkeypatch):
    anaxa = _unit("anaxa")
    anaxa.extra["anaxa_trace3"] = True
    target = _enemy(res=0.20)
    target.element_res["物理"] = 0.0
    target.element_res["火"] = 0.0
    state = _state(anaxa, enemy=target)
    choices = iter(("冰", "雷", "风"))
    monkeypatch.setattr("engine.core.combat_sim.random.choice", lambda _: next(choices))

    for _ in range(3):
        _anaxa_add_weakness(state, anaxa, target)
    _anaxa_reveal_check(state, anaxa, target)
    stats = _target_attacker_stats(anaxa.base_stats, anaxa, state, target)

    assert target.extra["anaxa_revealed"] is True
    assert stats.DEF_PEN == pytest.approx(0.20)


def test_anaxa_refresh_preserves_original_resistance_snapshot(monkeypatch):
    anaxa = _unit("anaxa")
    target = _enemy(res=0.20)
    state = _state(anaxa, enemy=target)
    monkeypatch.setattr("engine.core.combat_sim.random.choice", lambda _: "冰")

    _anaxa_add_weakness(state, anaxa, target)
    for element in ("物理", "火", "雷", "风", "量子", "虚数"):
        target.element_res[element] = -0.20
    _anaxa_add_weakness(state, anaxa, target)
    status = next(s for s in target.statuses if s.id == "anaxa_weak_冰")

    assert status.attributes["weakness_old_res"] == pytest.approx(0.20)


def test_cerydra_rank_and_e6_buffs_are_consumed_dynamically():
    cerydra = _unit("cerydra", eidolon=6)
    ally = _unit("seele", position=2)
    state = _state(cerydra, ally)
    cerydra.extra["cerydra_charge"] = 5
    _cerydra_grant_jungong(state, cerydra, ally)

    stats = _build_effective_stats(ally, state)
    assert stats.DEF_PEN >= 0.36 - 1e-9
    assert stats.DMG_BONUS_ALL == pytest.approx(0.40)
    assert stats.RES_PEN_ALL >= 0.30 - 1e-9


def test_dht_trace2_uses_deferred_initial_advance():
    dht = _unit("dan_heng_permansor_terrae")
    state = _state(dht)
    state.extra["navs"] = {}

    _trace_dht_trace2(dht, state)

    assert dht.extra["initial_action_advance_ratio"] == pytest.approx(0.40)


def test_cerydra_e1_restores_energy_to_jungong_target_not_owner():
    cerydra = _unit("cerydra", eidolon=1)
    holder = _unit("seele", position=2)
    state = _state(cerydra, holder)

    _use_skill(cerydra, state, "skill")

    assert holder.current_energy == pytest.approx(2.0)
    assert cerydra.current_energy == pytest.approx(30.0)


def test_dht_enhanced_longling_uses_tongpao_damage_on_every_enemy():
    dht = _unit("dan_heng_permansor_terrae", eidolon=6)
    tongpao = _unit("seele", position=2)
    dht.char.traces = [t for t in dht.char.traces
                       if getattr(t, "hook_name", "") != "dht_trace3"]
    enemies = [_enemy(hp=10_000_000.0), _enemy(hp=10_000_000.0)]
    enemies[1].id = "target_2"
    state = SimState(enemies=enemies, units=[dht, tongpao])
    dht.extra.update({"dht_tongpao_id": tongpao.char.id, "dht_longling_enhanced": 1})
    tongpao.extra["dht_tongpao"] = True

    dht_stats = _build_effective_stats(dht, state)
    tongpao_stats = _build_effective_stats(tongpao, state)
    expected = []
    for enemy in enemies:
        main = calculate_damage(dht_stats, _enemy_for_damage(enemy), dht_stats.ATK, 80.0,
                                "direct", "物理", 80, False, attack_type="follow_up",
                                crit_mode="expected")
        attached = calculate_damage(tongpao_stats, _enemy_for_damage(enemy), tongpao_stats.ATK,
                                    160.0, "direct", tongpao.char.element, 80, False,
                                    attack_type="follow_up", crit_mode="expected")
        expected.append(main.final_damage + attached.final_damage)

    _dht_longling_action(state, SimpleNamespace(summoner_id=dht.char.id))

    assert [10_000_000.0 - enemy.HP for enemy in enemies] == pytest.approx(expected)


def test_dht_e6_grants_defense_penetration_but_not_resistance_penetration():
    dht = _unit("dan_heng_permansor_terrae", eidolon=6)
    tongpao = _unit("seele", position=2)
    tongpao.extra["dht_tongpao"] = True
    stats = _build_effective_stats(tongpao, _state(dht, tongpao))

    assert stats.DEF_PEN == pytest.approx(0.12)
    assert stats.RES_PEN_ALL == pytest.approx(0.0)


def test_phainon_e1_adds_ultimate_crit_damage_buff():
    phainon = _unit("phainon", eidolon=1)
    state = _state(phainon)
    phainon.extra["huozhong"] = 12

    _use_skill(phainon, state, "ultimate")

    assert any(getattr(buff, "param_id", "") == "phainon_e1_ult_cd"
               and buff.attributes.get("CRIT_DMG") == 50.0
               and buff.remaining_turns == 3
               for buff in phainon.buffs)


def test_phainon_e6_true_damage_is_36_percent_of_shenshen_total():
    phainon = _unit("phainon", eidolon=6)
    enemy = _enemy(hp=10_000_000.0, res=0.0)
    state = _state(phainon, enemy=enemy)
    phainon.extra.update({"kasier": True, "huishang": 4})
    stats = _build_effective_stats(phainon, state)
    expected_base = calculate_damage(
        stats, _enemy_for_damage(enemy), stats.ATK, 1170.0,
        "direct", "物理", 80, stats.CRIT_RATE >= 0.5,
        skill_type="skill", crit_mode="expected").final_damage

    _use_skill(phainon, state, "skill_shenshen")

    assert enemy.HP < 10_000_000.0
    assert (10_000_000.0 - enemy.HP) / expected_base == pytest.approx(1.36)


def test_bronya_e4_extra_attack_uses_kill_pipeline():
    bronya = _unit("bronya", eidolon=4)
    attacker = _unit("seele", position=2)
    target = _enemy(hp=1.0)
    state = _state(bronya, attacker, enemy=target)
    target.element_res["风"] = 0.0

    _eid_bronya_e4(attacker, state, target=target, skill_key="basic_attack")

    assert target.HP == 0.0
    assert state.extra["killed_total"] == 1


def test_hysilens_death_clears_field_and_resistance():
    hysilens = _unit("hysilens", eidolon=4)
    target = _enemy(res=0.20)
    state = _state(hysilens, enemy=target)
    _hysilens_field(state, hysilens)
    hysilens.current_hp = 0

    _check_fatal(state, hysilens)

    assert hysilens.is_alive is False
    assert state.extra["hysilens_field_turns"] == 0
    assert target.extra.get("hysilens_field") is False
    assert target.get_res("物理") == pytest.approx(0.20)


def test_hysilens_field_refresh_does_not_stack_e4_resistance_loss():
    hysilens = _unit("hysilens", eidolon=4)
    target = _enemy(res=0.20)
    state = _state(hysilens, enemy=target)

    _hysilens_field(state, hysilens)
    _hysilens_field(state, hysilens)
    from engine.core.combat_sim import _hysilens_remove_field
    _hysilens_remove_field(state, hysilens)

    assert target.get_res("物理") == pytest.approx(0.20)


def test_sunday_mentor_and_skill_buffs_cover_memsprite():
    sunday = _unit("sunday", eidolon=2)
    target = _unit("seele", position=2)
    memsprite = MemSpriteUnit(data=MemSprite(name="测试忆灵"), summoner_id="seele",
                               current_hp=100, max_hp=100,
                               base_stats=CombatStats(ATK=100, DEF=100))
    target.memsprite_unit = memsprite
    state = _state(sunday, target)

    _sunday_skill(state, sunday)
    _sunday_apply_mentor(state, sunday, target)
    assert any(b.param_id == "sunday_skill_dmg" for b in memsprite.buffs)
    assert any(b.param_id == "sunday_mentor_cd" for b in memsprite.buffs)
    _sunday_tick(state, sunday)
    assert any(b.param_id == "sunday_mentor_cd" for b in memsprite.buffs)


def test_sunday_e6_crit_stacks_reset_after_buff_expiry():
    sunday = _unit("sunday", eidolon=6)
    target = _unit("seele", position=2)
    state = _state(sunday, target)

    _sunday_apply_cr_buff(state, sunday, target)
    _sunday_apply_cr_buff(state, sunday, target)
    assert target.extra["sunday_cr_stacks"] == 2
    for _ in range(4):
        _tick_buffs(target)

    _sunday_apply_cr_buff(state, sunday, target)

    assert target.extra["sunday_cr_stacks"] == 1
    assert next(buff for buff in target.buffs
                if buff.param_id == "sunday_cr").attributes["CRIT_RATE"] == 20.0

def test_qianye_e6_gate_resets_after_exit_and_new_turn():
    qianye = _unit("qianye", eidolon=6)
    state = _state(qianye)
    _qianye_enter_wrath(state, qianye)
    _qianye_e6_gain_charge(state, qianye)
    assert qianye.extra["qianye_e6_charge_used"] is True
    _qianye_exit_wrath(state, qianye)
    assert "qianye_taunt_mult" not in qianye.extra


def test_silver_wolf_speed_trace_and_enhanced_basic_deal_damage():
    silver = _unit("yinlang", eidolon=6)
    enemy = _enemy(hp=2_000_000.0)
    state = _state(silver, enemy=enemy)
    state.extra["_elation"] = ElationSystem()
    silver.base_stats.SPD = 200
    stats = state.extra["_elation"].eff_stats(silver, state, base_stats=silver.base_stats)
    hp_before = enemy.HP

    state.extra["_elation"].silver_enhanced_basic(silver, state)

    assert stats.ELATION_LEVEL > silver.base_stats.ELATION_LEVEL
    assert enemy.HP < hp_before


def test_silver_wolf_entry_effects_and_hidden_score_thresholds():
    silver = _unit("yinlang", eidolon=6)
    enemy = _enemy(res=0.20)
    state = _state(silver, enemy=enemy)

    _silver_wolf_apply_entry_effects(state)

    assert "yinlang_e1_vuln" not in enemy.extra
    assert enemy.get_res("物理") == pytest.approx(0.0)

    silver.eidolon_rank = 2
    silver.invincible_active = True
    state.extra["_elation"] = ElationSystem()
    state.extra["extra_turns"] = []
    state.extra["_elation"].gain_hidden_score(state, silver, 120.0)

    assert state.extra["extra_turns"] == [(silver, "yinlang_e2")]
    assert silver.extra["yinlang_e2_next_threshold"] == pytest.approx(240.0)


def test_sunday_and_dht_death_cleanup_removes_linked_state():
    sunday = _unit("sunday")
    dht = _unit("dan_heng_permansor_terrae", position=2)
    ally = _unit("seele", position=3)
    enemy = _enemy()
    state = SimState(enemies=[enemy], units=[sunday, dht, ally])

    ally.extra["sunday_mentor"] = True
    ally.buffs.append(TimedBuff(source_id="sunday", attributes={}, remaining_turns=2))
    dht.extra["dht_tongpao_id"] = ally.char.id
    ally.extra["dht_tongpao"] = True
    enemy.extra["dht_tongpao_vuln"] = 0.20
    dht.current_hp = 0

    _check_fatal(state, dht)

    assert dht.is_alive is False
    assert "dht_tongpao_id" not in dht.extra
    assert "dht_tongpao" not in ally.extra
    assert "dht_tongpao_vuln" not in enemy.extra

    sunday.current_hp = 0
    _check_fatal(state, sunday)
    assert sunday.is_alive is False
    assert "sunday_mentor" not in ally.extra
    assert not any(getattr(b, "source_id", "") == "sunday" for b in ally.buffs)


def test_audited_json_base_stats_match_skill_txt():
    root = Path(__file__).parents[1]
    audited = {
        "cerydra": "角色技能介绍/同谐/刻律德菈.txt",
        "tribbie": "角色技能介绍/同谐/缇宝.txt",
        "dan_heng_permansor_terrae": "角色技能介绍/存护/丹恒·腾荒.txt",
        "anaxa": "角色技能介绍/智识/那刻夏.txt",
        "phainon": "角色技能介绍/毁灭/白厄.txt",
        "hysilens": "角色技能介绍/虚无/海瑟音.txt",
        "yinlang": "角色技能介绍/欢愉/银狼Lv.999.txt",
    }
    for char_id, txt_path in audited.items():
        text = (root / txt_path).read_text(encoding="utf-8")
        match = re.search(r"生攻防速\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", text)
        assert match, txt_path
        txt_stats = tuple(int(value) for value in match.groups())
        data = json.loads((root / "data" / "characters" / f"{char_id}.json").read_text(encoding="utf-8"))
        assert (data["base_HP"], data["base_ATK"], data["base_DEF"], data["base_SPD"]) == txt_stats


def _full_char_ids():
    """v6.10.3 P2-2: 从完整角色集合自动生成（_pending 空 + 具备普攻），
    替代手写白名单——此前漏掉爻光/开拓者·欢愉/赛飞儿/银狼/缇宝"""
    ids = []
    for f in sorted(Path("data/characters").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("_pending") == "" and "basic_attack" in (data.get("skills") or {}):
            ids.append(f.stem)
    return ids


def test_audited_trace_and_eidolon_hooks_are_registered():
    for char_id in _full_char_ids():
        data = json.loads(Path("data/characters", f"{char_id}.json").read_text(encoding="utf-8"))
        assert all(trace.get("hook_name") in TRACE_REGISTRY
                   for trace in data.get("traces", [])), char_id
        assert all(eid.get("hook_name") in EIDOLON_REGISTRY
                   for eid in data.get("eidolons", [])), char_id


def test_completed_role_metadata_has_no_stale_pending_marker():
    for char_id in _full_char_ids():
        data = json.loads(Path("data/characters", f"{char_id}.json").read_text(encoding="utf-8"))
        assert data.get("_pending", "") == "", char_id


def test_silver_wolf_enhanced_damage_matches_hp_loss_and_blindbox_decay(monkeypatch):
    silver = _unit("yinlang", eidolon=2)
    silver.hidden_score = 120.0
    silver.invincible_active = True
    enemy = _enemy(hp=1_000_000_000.0)
    state = _state(silver, enemy=enemy)
    elation = ElationSystem()
    state.extra["_elation"] = elation
    rolls = iter((0.0, 0.0, 0.5, 0.5))
    monkeypatch.setattr(elation_module.random, "random", lambda: next(rolls))
    monkeypatch.setattr(elation_module.random, "choice", lambda values: values[0])
    hp_before = enemy.HP

    elation.silver_enhanced_basic(silver, state)

    assert hp_before - enemy.HP == pytest.approx(silver.total_damage_dealt)
    assert silver.extra["yinlang_blindbox_prob"] == pytest.approx(0.20)


def test_silver_wolf_blindbox_triggers_from_skill_point_spend(monkeypatch):
    silver = _unit("yinlang", eidolon=4)
    silver.hidden_score = 60.0
    silver.invincible_active = True
    enemy = _enemy(hp=1_000_000.0)
    state = _state(silver, enemy=enemy)
    state.skill_points = 1
    state.elation_state = type("ElationState", (), {"get_good_show_total": lambda self, _cid: 1.0})()
    state.extra["_elation"] = ElationSystem()
    monkeypatch.setattr(elation_module.random, "random", lambda: 0.0)
    hp_before = enemy.HP

    assert _deduct_skill_point_cost(state, silver, 1) is True

    assert enemy.HP < hp_before
    assert silver.extra["yinlang_blindbox_prob"] == pytest.approx(0.20)


def test_silver_wolf_blindbox_listens_to_ally_skill_point_spend(monkeypatch):
    silver = _unit("yinlang", eidolon=1)
    ally = _unit("seele", position=2)
    silver.hidden_score = 60.0
    silver.invincible_active = True
    enemy = _enemy(hp=1_000_000.0)
    state = _state(silver, ally, enemy=enemy)
    state.skill_points = 1
    state.elation_state.grant_good_show("yinlang", 1.0)
    state.extra["_elation"] = ElationSystem()
    monkeypatch.setattr(elation_module.random, "random", lambda: 0.0)

    hp_before = enemy.HP
    assert _deduct_skill_point_cost(state, ally, 1) is True

    assert enemy.HP < hp_before
    assert silver.extra["yinlang_blindbox_prob"] == pytest.approx(0.20)


def test_silver_wolf_blindbox_counts_sparxie_burst_as_spend(monkeypatch):
    silver = _unit("yinlang")
    ally = _unit("seele", position=2)
    silver.hidden_score = 60.0
    silver.invincible_active = True
    enemy = _enemy(hp=1_000_000.0)
    state = _state(silver, ally, enemy=enemy)
    state.skill_points = 0
    state.extra["sparxie_burst_points"] = 1
    state.elation_state.grant_good_show("yinlang", 1.0)
    state.extra["_elation"] = ElationSystem()
    monkeypatch.setattr(elation_module.random, "random", lambda: 0.0)

    assert _deduct_skill_point_cost(state, ally, 1) is True

    assert enemy.HP < 1_000_000.0
    assert silver.extra["yinlang_blindbox_prob"] == pytest.approx(0.20)


def test_silver_wolf_e1_vulnerability_is_scoped_to_invincible_field():
    silver = _unit("yinlang", eidolon=1)
    enemy = _enemy()
    state = _state(silver, enemy=enemy)

    _silver_wolf_apply_entry_effects(state)
    assert "yinlang_e1_vuln" not in enemy.extra

    silver.invincible_active = True
    _silver_wolf_apply_entry_effects(state)
    assert enemy.extra["yinlang_e1_vuln"] == pytest.approx(0.20)

    silver.invincible_active = False
    _silver_wolf_apply_entry_effects(state)
    assert "yinlang_e1_vuln" not in enemy.extra


def test_silver_wolf_death_clears_e1_field():
    silver = _unit("yinlang", eidolon=1)
    enemy = _enemy()
    state = _state(silver, enemy=enemy)
    silver.invincible_active = True
    _silver_wolf_apply_entry_effects(state)
    silver.current_hp = 0

    _check_fatal(state, silver)

    assert silver.is_alive is False
    assert "yinlang_e1_vuln" not in enemy.extra


def test_silver_wolf_hidden_score_caps_at_300():
    silver = _unit("yinlang")
    state = _state(silver)
    elation = ElationSystem()

    elation.gain_hidden_score(state, silver, 500.0)

    assert silver.hidden_score == pytest.approx(300.0)


def test_silver_wolf_skill_generates_five_laughs_but_basic_does_not():
    silver = _unit("yinlang")
    state = _state(silver)
    state.extra["_elation"] = ElationSystem()
    hooks = {}
    _register_elation_skill_hooks(hooks)

    hooks["yinlang"][1](silver, state, "basic_attack")
    assert state.laugh_points == pytest.approx(0.0)
    hooks["yinlang"][1](silver, state, "skill")
    assert state.laugh_points == pytest.approx(5.0)
    assert silver.hidden_score == pytest.approx(5.0)


def test_silver_wolf_technique_triggers_each_wave_with_fixed_laugh_count(monkeypatch):
    silver = _unit("yinlang")
    enemy = _enemy(hp=1_000_000.0)
    state = _state(silver, enemy=enemy)
    state.extra["_elation"] = ElationSystem()
    monkeypatch.setattr(elation_module.random, "random", lambda: 0.0)

    _tech_yinlang(state, silver, is_opener=True)

    assert state.extra["yinlang_tech_active"] is True
    assert enemy.HP < 1_000_000.0
    assert silver.extra.get("yinlang_blindbox_prob") is None

    next_enemy = _enemy(hp=1_000_000.0)
    state.enemies = [next_enemy]
    state.extra["_elation"].silver_technique_wave(silver, state)
    assert next_enemy.HP < 1_000_000.0


def test_silver_wolf_good_show_adds_exact_basic_elation_damage():
    def basic_damage(with_good_show):
        silver = _unit("yinlang")
        enemy = _enemy(hp=10_000_000.0)
        state = _state(silver, enemy=enemy)
        state.extra["_elation"] = ElationSystem()
        if with_good_show:
            state.elation_state.grant_good_show("yinlang", 10.0)
        _use_skill(silver, state, "basic_attack")
        return 10_000_000.0 - enemy.HP, silver, state

    without, _, _ = basic_damage(False)
    with_bonus, silver, state = basic_damage(True)
    stats = _build_effective_stats(silver, state)
    expected = calculate_damage(
        stats, _enemy_for_damage(_enemy()), 0.0, 40.0, "elation", "虚数",
        80, stats.CRIT_RATE >= 0.5, laugh_n=10.0,
        skill_type="basic", crit_mode="expected").final_damage

    assert with_bonus - without == pytest.approx(expected)


def test_silver_wolf_invincible_state_blocks_skill_and_ultimate():
    silver = _unit("yinlang")
    enemy = _enemy()
    state = _state(silver, enemy=enemy)
    state.skill_points = 2
    silver.invincible_active = True
    hp_before = enemy.HP

    _use_skill(silver, state, "skill")
    _use_skill(silver, state, "ultimate")

    assert enemy.HP == pytest.approx(hp_before)
    assert state.skill_points == 2
    assert state.action_counts.get("yinlang", 0) == 0


def test_silver_wolf_e4_does_not_boost_ordinary_blindbox(monkeypatch):
    def blindbox_damage(rank):
        silver = _unit("yinlang", eidolon=rank)
        silver.hidden_score = 60.0
        silver.invincible_active = True
        enemy = _enemy(hp=10_000_000.0)
        state = _state(silver, enemy=enemy)
        state.laugh_points = 20.0
        state.elation_state.grant_good_show("yinlang", 1.0)
        elation = ElationSystem()
        monkeypatch.setattr(elation_module.random, "random", lambda: 0.5)
        return elation.silver_blindbox(silver, state)

    assert blindbox_damage(4) == pytest.approx(blindbox_damage(3))
