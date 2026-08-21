"""Regression tests for the v6.10.4 Codex audit fixes."""

import json
from pathlib import Path

import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState,
    SimUnit,
    PlayerStatus,
    _begin_regular_turn,
    _enemy_attack,
    simulate,
    _use_skill,
)
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.effect_resolver import resolve_character_effects
from engine.systems.elation import ElationSystem, _tb_ai


def _enemy(hp=1_000_000.0):
    return Enemy(
        id="target",
        name="Target",
        HP=hp,
        ATK=100,
        DEF=800,
        SPD=80,
        toughness=200,
        max_toughness=200,
        level=80,
    )


def _unit(char_id, position=1, eidolon=0):
    char = load_character(char_id, "data/characters")
    stats = compute_combat_stats(char, None, None, None)
    unit = SimUnit(char=char, base_stats=stats, position=position)
    unit.max_hp = unit.current_hp = stats.HP
    unit.eidolon_rank = eidolon
    return unit


def _state(*units, enemies=None):
    return SimState(enemies=enemies or [_enemy()], units=list(units))


def test_json_elation_skill_uses_owner_good_show_multiplier():
    def dealt(good_show):
        unit = _unit("trailblazer_elation")
        state = _state(unit)
        if good_show:
            state.elation_state.grant_good_show(unit.char.id, good_show)
        _use_skill(unit, state, "elation_skill")
        return unit.total_damage_dealt

    without_good_show = dealt(0.0)
    with_good_show = dealt(100.0)

    expected_multiplier = 1.0 + 5.0 * 100.0 / (100.0 + 240.0)
    assert with_good_show / without_good_show == pytest.approx(expected_multiplier)


def test_good_show_ticks_only_after_owners_regular_turn():
    owner = _unit("trailblazer_elation", position=1)
    ally = _unit("bronya", position=2)
    state = _state(owner, ally)
    state.extra["_elation"] = ElationSystem()
    state.extra["navs"] = {0: 0.0, 1: 0.0}
    state.skill_points = 0
    state.elation_state.grant_good_show(owner.char.id, 20.0, duration=2)

    _begin_regular_turn(state, ally)
    batches = state.elation_state.good_shows[owner.char.id]
    assert [batch.remaining_turns for batch in batches] == [2]

    state.skill_points = 0
    _begin_regular_turn(state, owner)
    batches = state.elation_state.good_shows[owner.char.id]
    assert [batch.remaining_turns for batch in batches] == [1]


def test_good_show_expires_after_owner_uses_its_last_turn():
    owner = _unit("trailblazer_elation")
    state = _state(owner)
    state.extra["_elation"] = ElationSystem()
    state.extra["navs"] = {0: 0.0}
    state.skill_points = 0
    state.elation_state.grant_good_show(owner.char.id, 100.0, duration=1)
    observed = {}

    def record_good_show(u, state, **_):
        observed["during_turn"] = state.elation_state.get_good_show_total(u.char.id)

    state.hooks.register(owner.char.id, "on_turn_start", record_good_show)
    _begin_regular_turn(state, owner)

    assert observed["during_turn"] == 100.0
    assert state.elation_state.get_good_show_total(owner.char.id) == 0.0


@pytest.mark.parametrize(
    ("support_id", "expected_target_av"),
    [("bronya", 100.0), ("sparkle", 150.0)],
)
def test_single_ally_action_advance_updates_selected_targets_nav(
        support_id, expected_target_av):
    support = _unit(support_id, position=1)
    target = _unit("seele", position=2)
    target.base_stats.SPD = 100.0
    state = _state(support, target)
    state.current_av = 100.0
    state.extra["navs"] = {0: 180.0, 1: 200.0}
    state.skill_points = 5

    _use_skill(support, state, "skill")

    assert support.extra["lc_last_skill_target"] is target
    assert state.extra["navs"][1] == pytest.approx(expected_target_av)


@pytest.mark.parametrize("support_id", ["bronya", "sparkle"])
def test_single_ally_action_advance_does_not_advance_caster(support_id):
    support = _unit(support_id)
    support.base_stats.SPD = 100.0
    state = _state(support)
    state.current_av = 100.0
    state.extra["navs"] = {0: 1100.0}
    state.skill_points = 5

    _use_skill(support, state, "skill")

    assert support.extra["lc_last_skill_target"] is support
    assert state.extra["navs"][0] == 1100.0


def test_hysilens_skill_applies_three_turn_teamwide_vulnerability():
    hysilens = _unit("hysilens", position=1)
    attacker = _unit("seele", position=2)
    enemies = [_enemy() for _ in range(3)]
    state = _state(hysilens, attacker, enemies=enemies)
    state.skill_points = 5

    _use_skill(hysilens, state, "skill")
    statuses = [
        next(status for status in enemy.statuses if status.id == "hysilens_vuln")
        for enemy in enemies
    ]
    hp_before = enemies[0].HP
    _use_skill(attacker, state, "basic_attack")
    vulnerable_damage = hp_before - enemies[0].HP

    baseline_hysilens = _unit("hysilens", position=1)
    baseline_attacker = _unit("seele", position=2)
    baseline_enemy = _enemy()
    baseline = _state(baseline_hysilens, baseline_attacker, enemies=[baseline_enemy])
    hp_before = baseline_enemy.HP
    _use_skill(baseline_attacker, baseline, "basic_attack")
    baseline_damage = hp_before - baseline_enemy.HP

    assert [status.remaining_turns for status in statuses] == [3, 3, 3]
    assert [status.attributes for status in statuses] == [
        {"vulnerability": 0.20},
        {"vulnerability": 0.20},
        {"vulnerability": 0.20},
    ]
    assert vulnerable_damage / baseline_damage == pytest.approx(1.20)


def test_cipher_weak_reduces_outgoing_damage_for_blast_targets_only():
    cipher = _unit("cipher")
    attacker = _unit("hysilens", position=2)
    attacker.char.taunt = 0
    enemies = [_enemy() for _ in range(4)]
    for enemy in enemies:
        enemy.attacks = [{
            "name": "Hit",
            "multiplier": 100.0,
            "damage_type": "direct",
            "element": "物理",
            "target_type": "single_enemy",
        }]
    state = _state(cipher, attacker, enemies=enemies)
    state.skill_points = 5

    _use_skill(cipher, state, "skill")

    weak_statuses = [
        next((status for status in enemy.statuses if status.id == "cipher_weak"), None)
        for enemy in enemies
    ]
    assert [status is not None for status in weak_statuses] == [True, True, True, False]
    assert all(status.remaining_turns == 2 for status in weak_statuses[:3])
    assert all(status.attributes == {"outgoing_dmg_reduction": 0.10}
               for status in weak_statuses[:3])

    hp_before = sum(unit.current_hp for unit in state.units)
    weakened_damage = _enemy_attack(state, enemies[0])
    assert hp_before - sum(unit.current_hp for unit in state.units) \
        == pytest.approx(weakened_damage)

    baseline_cipher = _unit("cipher")
    baseline_enemy = _enemy()
    baseline_enemy.attacks = list(enemies[0].attacks)
    baseline = _state(baseline_cipher, enemies=[baseline_enemy])
    baseline_damage = _enemy_attack(baseline, baseline_enemy)

    assert weakened_damage / baseline_damage == pytest.approx(0.90)

    cipher.extra["cipher_fua_used"] = True
    hp_before = enemies[0].HP
    _use_skill(attacker, state, "basic_attack")
    player_damage = hp_before - enemies[0].HP

    baseline_attacker = _unit("hysilens", position=2)
    player_baseline_enemy = _enemy()
    player_baseline = _state(_unit("cipher"), baseline_attacker,
                             enemies=[player_baseline_enemy])
    hp_before = player_baseline_enemy.HP
    _use_skill(baseline_attacker, player_baseline, "basic_attack")
    baseline_player_damage = hp_before - player_baseline_enemy.HP

    assert player_damage / baseline_player_damage == pytest.approx(1.0)


def test_tb_ultimate_targets_elation_ally_and_uses_fixed_twenty_good_show():
    tb = _unit("trailblazer_elation", position=1)
    target = _unit("sparxie", position=2)
    state = _state(tb, target)
    system = ElationSystem()
    state.extra["_elation"] = system
    state.extra["navs"] = {0: 100.0, 1: 200.0}
    state.elation_state.grant_good_show(target.char.id, 100.0)
    target.statuses.append(PlayerStatus(
        id="frozen",
        name="Frozen",
        category="control",
        source="enemy",
        remaining_turns=1,
    ))
    tb.current_energy = tb.char.max_energy

    expected_target = _unit("sparxie")
    expected_state = _state(expected_target)
    expected_state.extra["_elation"] = ElationSystem()
    expected_state.laugh_points = 5.0
    expected_target.tb_cd_buff_turns = 3
    expected_state.elation_state.grant_good_show(expected_target.char.id, 20.0)
    _use_skill(expected_target, expected_state, "elation_skill")

    _tb_ai(tb, state, elation=system)

    assert tb.extra["lc_last_skill_target"] is target
    assert target.tb_cd_buff_turns == 3
    assert not target.statuses
    assert state.laugh_points == 5.0
    assert state.elation_state.get_good_show_total(target.char.id) == 110.0
    assert target.total_damage_dealt == pytest.approx(expected_target.total_damage_dealt)
    assert not any("paramId=cd_buff" in line or "paramId=laugh_gain_5" in line
                   for line in state.log)


def test_tb_e1_e2_ultimate_effects_follow_selected_ally():
    tb = _unit("trailblazer_elation", position=1, eidolon=2)
    target = _unit("sparxie", position=2)
    state = _state(tb, target)
    system = ElationSystem()
    state.extra["_elation"] = system
    state.extra["navs"] = {0: 100.0, 1: 200.0}
    tb.relic_stacks["tb_e1"] = 3
    tb.current_energy = tb.char.max_energy
    for effect in resolve_character_effects(
        tb.char,
        eidolon_rank=tb.eidolon_rank,
        registry=state.hooks,
    ):
        state.hooks.register_effect(effect)

    _tb_ai(tb, state, elation=system)

    assert state.elation_state.get_good_show_total(target.char.id) == 16.0
    assert state.elation_state.get_good_show_total(tb.char.id) == 0.0
    assert any(buff.param_id == "tb_e2" for buff in target.buffs)
    assert not any(buff.param_id == "tb_e2" for buff in tb.buffs)


def test_tb_ultimate_advances_non_elation_ally_by_fifty_percent():
    tb = _unit("trailblazer_elation", position=1)
    target = _unit("bronya", position=2)
    target.base_stats.SPD = 100.0
    state = _state(tb, target)
    system = ElationSystem()
    state.extra["_elation"] = system
    state.current_av = 100.0
    state.extra["navs"] = {0: 180.0, 1: 200.0}
    tb.current_energy = tb.char.max_energy

    _tb_ai(tb, state, elation=system)

    assert tb.extra["lc_last_skill_target"] is target
    assert state.extra["navs"][1] == pytest.approx(150.0)


def test_complete_roster_emits_no_unregistered_effect_warnings():
    complete_ids = []
    for path in sorted(Path("data/characters").glob("*.json")):
        if path.stem.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if "basic_attack" in (data.get("skills") or {}):
            complete_ids.append(path.stem)

    assert len(complete_ids) == 39
    for eidolon in (0, 6):
        for char_id in complete_ids:
            state = simulate(
                [{
                    "char": load_character(char_id, "data/characters"),
                    "position": 1,
                    "eidolon": eidolon,
                }],
                _enemy(hp=1_000_000_000.0),
                max_av=600,
            )
            unsupported = [
                line for line in state.log
                if "[WARN]" in line and "paramId=" in line
            ]
            assert unsupported == [], (char_id, eidolon, unsupported)


def test_yinlang_elation_skill_gains_hidden_score_once_per_cast():
    direct = _unit("yinlang")
    direct_state = _state(direct)
    direct_system = ElationSystem()
    direct_state.extra["_elation"] = direct_system

    _use_skill(direct, direct_state, "elation_skill")

    aha = _unit("yinlang")
    aha_state = _state(aha)
    aha_system = ElationSystem()
    aha_state.extra["_elation"] = aha_system
    aha_state.laugh_points = 1.0
    aha_system.execute_aha(aha_state)

    assert direct.hidden_score == 15.0
    assert aha.hidden_score == 16.0
