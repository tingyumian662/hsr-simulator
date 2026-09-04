"""Codex v6.10.1 review regressions for the public combat paths."""

import pytest

from engine.core.combat_engine import _begin_regular_turn, _build_effective_stats, _respawn_wave, _use_skill, simulate
from engine.characters.acheron import _acheron_ult
from engine.characters.sunday import _sunday_ult
from engine.runtime import SimState, SimUnit
from engine.core.attributes import compute_combat_stats
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus


def _enemy(*, hp=500_000, toughness=200):
    return Enemy(
        id="review_enemy",
        name="Review Enemy",
        HP=hp,
        ATK=100,
        DEF=800,
        SPD=80,
        toughness=toughness,
        max_toughness=toughness,
        level=80,
        element_res={
            "物理": 0.0,
            "火": 0.0,
            "冰": 0.0,
            "雷": 0.0,
            "风": 0.0,
            "量子": 0.0,
            "虚数": 0.0,
        },
    )


def _config(character_id, position):
    return {
        "char": load_character(character_id, "data/characters"),
        "position": position,
    }


def _direct_state(character_id, *, eidolon=0, enemy=None):
    character = load_character(character_id, "data/characters")
    stats = compute_combat_stats(character, None, None, None)
    unit = SimUnit(
        char=character,
        base_stats=stats,
        position=1,
        eidolon_rank=eidolon,
    )
    unit.max_hp = unit.current_hp = stats.HP
    state = SimState(enemies=[enemy or _enemy()], units=[unit])
    return state, unit


def test_acheron_talent_accepts_real_debuff_hook_contract():
    state = simulate(
        [_config("acheron", 1), _config("cipher", 2)],
        _enemy(),
        max_av=0,
    )
    acheron, cipher = state.units
    before = acheron.extra["acheron_dream"]

    _use_skill(cipher, state, "skill")

    assert acheron.extra["acheron_dream"] == before + 1


def test_acheron_talent_observes_silver_wolf_special_debuffs():
    state = simulate(
        [_config("acheron", 1), _config("silver_wolf", 2)],
        _enemy(),
        max_av=0,
    )
    acheron, silver_wolf = state.units
    before = acheron.extra["acheron_dream"]

    _use_skill(silver_wolf, state, "skill")

    assert acheron.extra["acheron_dream"] == before + 1


def test_acheron_ultimate_stops_cleanly_after_last_enemy_dies():
    state = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    acheron = state.units[0]
    state.enemies[0].HP = 1
    acheron.extra["acheron_dream"] = 9

    _acheron_ult(state, acheron)

    assert state.enemies[0].HP <= 0


def test_acheron_ultimate_ignores_weakness_and_triggers_break():
    state = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    acheron = state.units[0]
    enemy = state.enemies[0]
    enemy.element_res["雷"] = 0.40
    enemy.toughness = enemy.max_toughness = 10
    enemy.is_broken = False
    acheron.extra["acheron_dream"] = 9

    _acheron_ult(state, acheron)

    assert enemy.toughness == 0
    assert enemy.is_broken is True


def test_acheron_e2_runs_at_real_regular_turn_boundary():
    config = _config("acheron", 1)
    config["eidolon"] = 2
    state = simulate([config], _enemy(), max_av=0)
    acheron = state.units[0]
    state.skill_points = 0
    before = acheron.extra["acheron_dream"]

    _begin_regular_turn(state, acheron)

    assert acheron.extra["acheron_dream"] == before + 1


def test_acheron_trace2_multiplies_original_damage_once():
    solo = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    team = simulate(
        [_config("acheron", 1), _config("welt", 2)],
        _enemy(),
        max_av=0,
    )

    solo_before = solo.enemies[0].HP
    _use_skill(solo.units[0], solo, "basic_attack")
    solo_damage = solo_before - solo.enemies[0].HP

    team_before = team.enemies[0].HP
    _use_skill(team.units[0], team, "basic_attack")
    team_damage = team_before - team.enemies[0].HP

    assert team_damage / solo_damage == pytest.approx(1.15)


def test_acheron_e1_adds_crit_rate_only_against_debuffed_target():
    e0 = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    e1_config = _config("acheron", 1)
    e1_config["eidolon"] = 1
    e1 = simulate([e1_config], _enemy(), max_av=0)
    for state in (e0, e1):
        state.enemies[0].add_status(EnemyStatus(
            id="review_debuff",
            name="Review Debuff",
            category="debuff",
            source="review",
            remaining_turns=2,
        ))

    before = e0.enemies[0].HP
    _use_skill(e0.units[0], e0, "basic_attack")
    e0_damage = before - e0.enemies[0].HP

    before = e1.enemies[0].HP
    _use_skill(e1.units[0], e1, "basic_attack")
    e1_damage = before - e1.enemies[0].HP

    stats = _build_effective_stats(e0.units[0], e0)
    expected = (1 + (stats.CRIT_RATE + 0.18) * stats.CRIT_DMG) / (
        1 + stats.CRIT_RATE * stats.CRIT_DMG
    )
    assert e1_damage / e0_damage == pytest.approx(expected)


def test_acheron_e4_applies_ultimate_vulnerability_on_enemy_entry():
    config = _config("acheron", 1)
    config["eidolon"] = 4

    state = simulate([config], _enemy(), max_av=0)

    assert state.enemies[0].status_attribute("vulnerability_ultimate") == pytest.approx(0.08)


def test_acheron_e4_vulnerability_is_consumed_by_ultimate_damage():
    e3_config = _config("acheron", 1)
    e3_config["eidolon"] = 3
    e4_config = _config("acheron", 1)
    e4_config["eidolon"] = 4
    e3 = simulate([e3_config], _enemy(), max_av=0)
    e4 = simulate([e4_config], _enemy(), max_av=0)
    for state in (e3, e4):
        state.enemies[0].HP = 500_000
        state.enemies[0].toughness = state.enemies[0].max_toughness = 1000
        state.units[0].extra["acheron_dream"] = 9

    before_e3 = e3.enemies[0].HP
    before_e4 = e4.enemies[0].HP
    _acheron_ult(e3, e3.units[0])
    _acheron_ult(e4, e4.units[0])

    assert (before_e4 - e4.enemies[0].HP) / (before_e3 - e3.enemies[0].HP) == pytest.approx(1.08)


def test_acheron_e6_basic_counts_as_ultimate_and_ignores_weakness():
    e5, acheron_e5 = _direct_state("acheron", eidolon=5)
    e6, acheron_e6 = _direct_state("acheron", eidolon=6)
    for state, unit in ((e5, acheron_e5), (e6, acheron_e6)):
        state.enemies[0].element_res["雷"] = 0.40
        state.enemies[0].toughness = state.enemies[0].max_toughness = 1000
        unit.base_stats.DMG_BONUS_BY_SKILL_TYPE["ultimate"] = 1.0

    before = e5.enemies[0].HP
    _use_skill(acheron_e5, e5, "basic_attack")
    e5_damage = before - e5.enemies[0].HP

    before = e6.enemies[0].HP
    _use_skill(acheron_e6, e6, "basic_attack")
    e6_damage = before - e6.enemies[0].HP

    expected = (2.08 / 1.08) * (0.80 / 0.60)
    assert e6_damage / e5_damage == pytest.approx(expected)
    assert e5.enemies[0].toughness == 1000

    break_state, break_unit = _direct_state("acheron", eidolon=6)
    break_state.enemies[0].element_res["雷"] = 0.40
    break_state.enemies[0].toughness = break_state.enemies[0].max_toughness = 10
    _use_skill(break_unit, break_state, "basic_attack")
    assert break_state.enemies[0].is_broken is True


def test_acheron_jizhen_transfers_when_an_ally_kills_an_enemy():
    state = simulate(
        [_config("acheron", 1), _config("seele", 2)],
        _enemy(),
        max_av=0,
        num_enemies=2,
    )
    acheron, seele = state.units
    state.enemies[0].HP = 1
    state.enemies[0].extra["acheron_jizhen"] = 4
    state.enemies[1].extra.pop("acheron_jizhen", None)

    _use_skill(seele, state, "basic_attack")

    assert state.enemies[0].HP <= 0
    assert state.enemies[0].extra.get("acheron_jizhen", 0) == 0
    assert state.enemies[1].extra.get("acheron_jizhen", 0) == 4


def test_acheron_sixiang_is_consumed_after_ultimate():
    state = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    acheron = state.units[0]
    enemy = state.enemies[0]
    acheron.extra["acheron_dream"] = 9
    acheron.extra["acheron_sixiang"] = 2
    enemy.extra["acheron_jizhen"] = 3

    _acheron_ult(state, acheron)

    assert acheron.extra.get("acheron_sixiang", 0) == 0
    assert acheron.extra["acheron_dream"] == 2
    assert enemy.extra.get("acheron_jizhen", 0) == 2


def test_acheron_trace3_creates_a_three_turn_damage_buff():
    state = simulate([_config("acheron", 1)], _enemy(), max_av=0)
    acheron = state.units[0]
    enemy = state.enemies[0]
    acheron.extra["acheron_dream"] = 9
    enemy.extra["acheron_jizhen"] = 1

    _acheron_ult(state, acheron)

    buff = next(b for b in acheron.buffs if b.param_id == "acheron_leixin")
    assert buff.attributes["DMG_BONUS_ALL"] == pytest.approx(30.0)
    assert buff.remaining_turns == 3


def test_battle_start_technique_only_runs_for_the_real_opener():
    state = simulate(
        [_config("acheron", 1), _config("feixiao", 2)],
        _enemy(),
        max_av=0,
    )

    acheron, feixiao = state.units
    assert acheron.extra.get("acheron_sixiang") == 1
    assert state.extra.get("feixiao_tech_active") is None
    assert feixiao.extra.get("feixiao_fly") == 3


def test_acheron_technique_uses_break_pipeline():
    state = simulate(
        [_config("acheron", 1)],
        _enemy(toughness=10),
        max_av=0,
    )

    assert state.enemies[0].is_broken is True


def test_feixiao_technique_does_not_add_fly_on_later_waves():
    state = simulate([_config("feixiao", 1)], _enemy(), max_av=0)
    feixiao = state.units[0]
    assert feixiao.extra.get("feixiao_fly") == 4
    state.enemies[0].HP = 0

    _respawn_wave(state)

    assert feixiao.extra.get("feixiao_fly") == 4


def test_sunday_fixed_energy_restore_does_not_fill_special_energy():
    state = simulate(
        [_config("sunday", 1), _config("feixiao", 2)],
        _enemy(),
        max_av=0,
    )
    sunday, feixiao = state.units
    feixiao.extra["single_ally_priority"] = 1
    feixiao.current_energy = 0

    _sunday_ult(state, sunday)

    assert feixiao.current_energy == 0


def test_sunday_fixed_energy_restore_uses_normal_energy_for_regular_units():
    state = simulate(
        [_config("sunday", 1), _config("seele", 2)],
        _enemy(),
        max_av=0,
    )
    sunday, seele = state.units
    before = seele.current_energy

    _sunday_ult(state, sunday)

    assert seele.current_energy == pytest.approx(
        before + max(seele.char.max_energy * 0.20, 40.0)
    )


def test_feixiao_own_attacks_count_toward_two_attack_fly_gain():
    state = simulate([_config("feixiao", 1)], _enemy(), max_av=0)
    feixiao = state.units[0]
    feixiao.extra["feixiao_fly"] = 0

    _use_skill(feixiao, state, "basic_attack")
    _use_skill(feixiao, state, "basic_attack")

    assert feixiao.extra.get("feixiao_fly") == 1


def test_feixiao_tick_reads_previous_turn_fua_before_resetting_flags():
    state, feixiao = _direct_state("feixiao")
    feixiao.extra["feixiao_attack_count"] = 0
    feixiao.extra["feixiao_any_fua_this_turn"] = False

    from engine.characters.feixiao import _feixiao_tick
    _feixiao_tick(state, feixiao)

    assert feixiao.extra.get("feixiao_fua_used") is False
    assert feixiao.extra.get("feixiao_attack_count") == 1


def test_feixiao_e4_fua_doubles_toughness_and_adds_speed():
    state, feixiao = _direct_state("feixiao", eidolon=4)
    enemy = state.enemies[0]
    enemy.toughness = enemy.max_toughness = 20
    enemy.element_res["风"] = 0.0

    from engine.characters.feixiao import _feixiao_fua
    _feixiao_fua(state, feixiao, enemy)

    assert enemy.toughness == pytest.approx(10.0)
    stats = _build_effective_stats(feixiao, state)
    assert stats.SPD_PERCENT == pytest.approx(0.08)
    from engine.core.combat_engine import _effective_spd
    assert _effective_spd(feixiao, state) == pytest.approx(
        feixiao.base_stats.SPD * 1.08
    )


def test_feixiao_trace2_adds_follow_up_crit_damage():
    state, feixiao = _direct_state("feixiao")

    stats = _build_effective_stats(feixiao, state)

    assert stats.CRIT_DMG_BY_ATTACK_TYPE.get("follow_up") == pytest.approx(0.36)


def test_feixiao_ultimate_uses_ultimate_and_follow_up_damage_scopes():
    plain, plain_feixiao = _direct_state("feixiao")
    boosted, boosted_feixiao = _direct_state("feixiao")
    for state, unit in ((plain, plain_feixiao), (boosted, boosted_feixiao)):
        state.enemies[0].toughness = state.enemies[0].max_toughness = 1000
        unit.extra["feixiao_fly"] = 6
    boosted_feixiao.base_stats.DMG_BONUS_BY_SKILL_TYPE["ultimate"] = 1.0
    boosted_feixiao.base_stats.DMG_BONUS_BY_ATTACK_TYPE["follow_up"] = 1.0

    before = plain.enemies[0].HP
    from engine.characters.feixiao import _feixiao_ult
    _feixiao_ult(plain, plain_feixiao)
    plain_damage = before - plain.enemies[0].HP

    before = boosted.enemies[0].HP
    _feixiao_ult(boosted, boosted_feixiao)
    boosted_damage = before - boosted.enemies[0].HP

    assert boosted_damage / plain_damage == pytest.approx(3.0)


def test_feixiao_e6_fua_uses_ultimate_resistance_penetration():
    e5, feixiao_e5 = _direct_state("feixiao", eidolon=5)
    e6, feixiao_e6 = _direct_state("feixiao", eidolon=6)
    for state in (e5, e6):
        state.enemies[0].element_res["风"] = 0.40
        state.enemies[0].toughness = state.enemies[0].max_toughness = 1000

    from engine.characters.feixiao import _feixiao_fua
    before = e5.enemies[0].HP
    _feixiao_fua(e5, feixiao_e5, e5.enemies[0])
    e5_damage = before - e5.enemies[0].HP

    before = e6.enemies[0].HP
    _feixiao_fua(e6, feixiao_e6, e6.enemies[0])
    e6_damage = before - e6.enemies[0].HP

    assert e6_damage / e5_damage == pytest.approx(2.40 * (0.80 / 0.60))
