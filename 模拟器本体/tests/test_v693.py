"""v6.9.2 regression tests for the v6.9.1 Codex review findings."""
import copy
import random

import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    PlayerStatus,
    SimState,
    SimUnit,
    _apply_player_status,
    _build_effective_stats,
    _busitu_apply_bait,
    _busitu_fua,
    _busitu_rebind_bait,
    _check_fatal,
    _enemy_for_damage,
    _gain_energy,
    _multihit_damage,
    _qianye_ai,
    _qianye_enter_wrath,
    _qianye_skill,
    _robin_concert_extra,
    _robin_ult,
    _ruanmei_field_apply,
    _ruanmei_tick,
    _ruanmei_xianyin_apply,
    _sunday_apply_cr_buff,
    _sunday_apply_mentor,
    _sunday_tick,
    _tick_buffs,
    _tick_enemy_statuses,
    _use_skill,
    _welt_apply_shizhong,
    _welt_apply_slow,
    _register_v69_skill_hooks,
    simulate,
)
from engine.core.effect_resolver import _trace_busitu_e1, _trace_busitu_trace3
from engine.models.character import load_character
from engine.models.enemy import Enemy


def _enemy(hp=500_000.0, res=0.0):
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
        element_res={e: res for e in ("物理", "火", "冰", "雷", "风", "量子", "虚数")},
    )


def _unit(cid, position=1, eidolon=0):
    char = load_character(cid, "data/characters")
    stats = compute_combat_stats(char, None, None, None)
    unit = SimUnit(char=char, base_stats=stats, position=position)
    unit.max_hp = unit.current_hp = stats.HP
    unit.eidolon_rank = eidolon
    return unit


def _config(unit, eidolon=None, initial_energy_pct=0.0):
    return {
        "char": unit.char,
        "lightcone": None,
        "relics": [],
        "relic_sets": {},
        "position": unit.position,
        "eidolon": unit.eidolon_rank if eidolon is None else eidolon,
        "initial_energy_pct": initial_energy_pct,
    }


def _state(*units, enemies=None):
    state = SimState(enemies=list(enemies or [_enemy()]), units=list(units))
    state.extra["navs"] = {i: 100.0 for i in range(len(units))}
    state.extra["stamp_counter"] = 0
    state.extra["av_stamp"] = {}
    return state


def _has_buff(unit, param_id):
    return any(getattr(buff, "param_id", "") == param_id for buff in unit.buffs)


class TestOwnerTimedEffects:
    def test_sunday_mentor_ticks_only_on_sunday_turn(self):
        sunday = _unit("sunday", position=1)
        ally = _unit("seele", position=2)
        state = _state(sunday, ally)
        _sunday_apply_mentor(state, sunday, ally)

        _tick_buffs(ally)
        _sunday_tick(state, sunday)
        _sunday_tick(state, sunday)
        assert _has_buff(ally, "sunday_mentor_cd")

        _sunday_tick(state, sunday)
        assert not _has_buff(ally, "sunday_mentor_cd")

    def test_ruan_mei_buffs_ignore_beneficiary_turns(self):
        ruan = _unit("ruan_mei", position=1)
        ally = _unit("seele", position=2)
        state = _state(ruan, ally)
        _ruanmei_xianyin_apply(state, ruan)
        _ruanmei_field_apply(state, ruan)

        for _ in range(4):
            _tick_buffs(ally)
        assert _has_buff(ally, "ruanmei_xianyin")
        assert _has_buff(ally, "ruanmei_field")

        _ruanmei_tick(state, ruan)
        assert _has_buff(ally, "ruanmei_xianyin")
        assert _has_buff(ally, "ruanmei_field")
        _ruanmei_tick(state, ruan)
        assert not _has_buff(ally, "ruanmei_field")
        _ruanmei_tick(state, ruan)
        assert not _has_buff(ally, "ruanmei_xianyin")

    def test_robin_concert_expires_on_first_marker_action(self):
        robin = _unit("robin", position=1)
        ally = _unit("seele", position=2)
        state = _state(robin, ally)
        _robin_ult(state, robin)
        marker = robin.marker

        state.current_av = marker.extra["next_av"]
        state.extra["_marker_sys"].handle_action(state, marker)

        assert not robin.extra.get("robin_concert")
        assert robin.marker is None
        assert not _has_buff(ally, "robin_concert")
        assert state.extra["navs"][0] == pytest.approx(state.current_av)


class TestQianyePipeline:
    def test_legacy_bounce_target_is_parsed_for_qianye_and_welt(self):
        qianye = load_character("qianye", "data/characters")
        welt = load_character("welt", "data/characters")
        assert qianye.skills["skill"].multipliers[1].target == "bounce"
        assert welt.skills["skill"].multipliers[0].target == "bounce"

    def test_trace1_starts_at_seventy_five_percent_energy(self):
        qianye = _unit("qianye")
        state = simulate([_config(qianye)], _enemy(), max_av=0.0)
        actual = state.units[0]
        assert actual.current_energy == pytest.approx(actual.char.max_energy * 0.75)

    def test_trace1_cleanses_when_energy_reaches_full(self):
        qianye = _unit("qianye")
        qianye.current_energy = qianye.char.max_energy - 1
        qianye.statuses.append(PlayerStatus(id="stun", name="眩晕", category="control"))
        state = _state(qianye)

        _gain_energy(qianye, 1.0, state=state)

        assert qianye.statuses == []

    def test_wrath_uses_real_seventy_speed_marker(self):
        qianye = _unit("qianye")
        state = _state(qianye)
        state.current_av = 25.0
        _qianye_enter_wrath(state, qianye)

        marker = qianye.marker
        assert marker is not None
        assert marker.marker_id == "qianye_wrath"
        assert marker.extra["next_av"] == pytest.approx(25.0 + 10_000.0 / 70.0)

        state.current_av = marker.extra["next_av"]
        state.extra["_marker_sys"].handle_action(state, marker)
        assert not qianye.extra.get("qianye_wrath")
        assert qianye.marker is None

    def test_fatal_recovery_is_exactly_half_max_hp(self):
        qianye = _unit("qianye")
        state = _state(qianye)
        qianye.extra["qianye_wrath"] = True
        qianye.current_hp = -qianye.max_hp

        _check_fatal(state, qianye)

        assert qianye.current_hp == pytest.approx(qianye.max_hp * 0.50)
        assert qianye.is_alive

    def test_wrath_traces_and_eidolons_modify_real_panels(self):
        qianye = _unit("qianye", position=1, eidolon=4)
        ally = _unit("seele", position=2)
        enemy = _enemy(res=0.0)
        state = _state(qianye, ally, enemies=[enemy])
        _qianye_enter_wrath(state, qianye)

        self_stats = _build_effective_stats(qianye, state)
        ally_stats = _build_effective_stats(ally, state)
        assert self_stats.DMG_REDUCTION == pytest.approx(0.50)
        assert self_stats.HEAL_BONUS == pytest.approx(0.50)
        assert ally_stats.DMG_BONUS_ALL == pytest.approx(1.00)
        assert _enemy_for_damage(enemy).get_res("火") == pytest.approx(-0.20)

    def test_e2_applies_team_followup_bonus(self):
        qianye = _unit("qianye", position=1, eidolon=2)
        ally = _unit("seele", position=2)
        state = _state(qianye, ally)
        stats = _build_effective_stats(ally, state)
        assert stats.DMG_BONUS_BY_ATTACK_TYPE.get("follow_up") is None
        _qianye_enter_wrath(state, qianye)
        active_stats = _build_effective_stats(ally, state)
        assert active_stats.DMG_BONUS_BY_ATTACK_TYPE.get("follow_up") == pytest.approx(0.75)

    def test_e6_hp_cost_grants_only_one_charge_in_same_turn(self):
        qianye = _unit("qianye", eidolon=6)
        state = _state(qianye)
        _qianye_enter_wrath(state, qianye)

        _qianye_skill(state, qianye, "skill")
        _qianye_skill(state, qianye, "skill")

        assert qianye.extra.get("qianye_charge") == 1

    def test_basic_attack_applies_taunt(self):
        qianye = _unit("qianye")
        enemy = _enemy()
        state = _state(qianye, enemies=[enemy])

        _use_skill(qianye, state, "basic_attack")

        assert enemy.has_status(status_id="taunt")

    def test_ai_routes_new_ultimate_through_common_skill_pipeline(self):
        qianye = _unit("qianye")
        enemy = _enemy(hp=5_000_000.0)
        state = _state(qianye, enemies=[enemy])
        _qianye_enter_wrath(state, qianye)
        qianye.current_energy = qianye.char.max_energy
        qianye.extra["qianye_overflow"] = 10.0

        _qianye_ai(qianye, state)

        assert state.action_counts.get("qianye") == 1
        assert qianye.current_energy == pytest.approx(10.0)
        assert enemy.HP < 5_000_000.0

    def test_ai_routes_released_skill_through_common_skill_pipeline(self):
        qianye = _unit("qianye")
        enemy = _enemy(hp=5_000_000.0)
        state = _state(qianye, enemies=[enemy])
        _qianye_enter_wrath(state, qianye)
        qianye.extra["qianye_charge"] = 5

        _qianye_ai(qianye, state)

        assert state.action_counts.get("qianye") == 1
        assert enemy.HP < 5_000_000.0


class TestWeltPerHitPipeline:
    def test_e4_res_down_refresh_is_dynamic_and_reversible(self):
        welt = _unit("welt", eidolon=4)
        enemy = _enemy(res=0.40)
        state = _state(welt, enemies=[enemy])

        _welt_apply_shizhong(state, welt, enemy)
        _welt_apply_shizhong(state, welt, enemy)
        assert enemy.get_res("虚数") == pytest.approx(0.40)
        assert _enemy_for_damage(enemy).get_res("虚数") == pytest.approx(0.10)

        _tick_enemy_statuses(state, enemy)
        _tick_enemy_statuses(state, enemy)
        assert _enemy_for_damage(enemy).get_res("虚数") == pytest.approx(0.40)

    def test_skill_first_hit_slows_then_four_hits_trigger_talent(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.0)
        welt = _unit("welt", eidolon=2)
        enemy = _enemy()
        state = _state(welt, enemies=[enemy])
        _register_v69_skill_hooks(state.skill_hooks)

        _use_skill(welt, state, "skill")

        assert welt.current_energy == pytest.approx(42.0)

    def test_e6_crit_modifiers_apply_to_bounce_main_damage(self, monkeypatch):
        monkeypatch.setattr(random, "choice", lambda seq: seq[0])
        monkeypatch.setattr(random, "random", lambda: 0.0)

        def run(eidolon):
            welt = _unit("welt", eidolon=eidolon)
            enemy = _enemy(hp=5_000_000.0)
            state = _state(welt, enemies=[enemy])
            _welt_apply_slow(state, welt, enemy)
            stats = _build_effective_stats(welt, state)
            damage = _multihit_damage(
                stats,
                [enemy],
                stats.ATK,
                72.0,
                "direct",
                "虚数",
                False,
                hits=5,
                skill_type="skill",
                u=welt,
                state=state,
            )
            return damage, stats

        e0_damage, e0_stats = run(0)
        e6_damage, _ = run(6)
        expected = (1.0 + min(e0_stats.CRIT_RATE + 0.30, 1.0) * (e0_stats.CRIT_DMG + 0.60)) / (
            1.0 + min(e0_stats.CRIT_RATE, 1.0) * e0_stats.CRIT_DMG
        )
        assert e6_damage / e0_damage == pytest.approx(expected)


class TestBusituEidolons:
    def test_e1_uses_thirty_six_percent_at_half_hp(self):
        busitu = _unit("busitu", eidolon=1)
        enemy = _enemy(hp=1_000.0)
        state = _state(busitu, enemies=[enemy])
        _trace_busitu_e1(busitu, state)

        enemy.HP = 500.0
        assert _enemy_for_damage(enemy).vulnerability == pytest.approx(0.36)

    def test_trace3_fua_crit_damage_applies_to_teammates(self):
        busitu = _unit("busitu", position=1)
        ally = _unit("seele", position=2)
        state = _state(busitu, ally)
        _trace_busitu_trace3(busitu, state)

        stats = _build_effective_stats(ally, state)
        assert stats.CRIT_DMG_BY_ATTACK_TYPE.get("follow_up") == pytest.approx(0.80)

    def test_e2_refunds_thirty_five_percent_of_removed_stacks(self):
        busitu = _unit("busitu", eidolon=2)
        enemy = _enemy(hp=5_000_000.0)
        state = _state(busitu, enemies=[enemy])
        busitu.extra["busitu_lanhan"] = 4.0
        _busitu_apply_bait(state, busitu, enemy)

        _busitu_fua(state, busitu, enemy, enhanced=True)

        assert busitu.extra["busitu_lanhan"] == pytest.approx(3.4)

    def test_e6_res_down_does_not_mutate_enemy_base_resistance(self):
        busitu = _unit("busitu", eidolon=6)
        enemy = _enemy(res=0.0)
        state = _state(busitu, enemies=[enemy])

        _busitu_apply_bait(state, busitu, enemy)
        assert enemy.get_res("雷") == pytest.approx(0.0)
        assert _enemy_for_damage(enemy).get_res("雷") == pytest.approx(-0.20)

        enemy.HP = 0.0
        _busitu_rebind_bait(state, busitu)
        assert not enemy.has_status(status_id="busitu_e6_res_down")


class TestSundayE6:
    def test_overflow_crit_converts_to_crit_damage(self):
        sunday = _unit("sunday", position=1, eidolon=6)
        ally = _unit("seele", position=2)
        ally.base_stats.CRIT_RATE = 0.80
        base_cd = ally.base_stats.CRIT_DMG
        state = _state(sunday, ally)
        for _ in range(3):
            _sunday_apply_cr_buff(state, sunday, ally)

        stats = _build_effective_stats(ally, state)
        assert stats.CRIT_RATE == pytest.approx(1.0)
        assert stats.CRIT_DMG == pytest.approx(base_cd + 0.80)

    def test_expired_stack_counter_restarts_at_one(self):
        sunday = _unit("sunday", position=1, eidolon=6)
        ally = _unit("seele", position=2)
        state = _state(sunday, ally)
        for _ in range(3):
            _sunday_apply_cr_buff(state, sunday, ally)
        buff = next(b for b in ally.buffs if b.param_id == "sunday_cr")
        buff.remaining_turns = 1

        _tick_buffs(ally)
        _sunday_apply_cr_buff(state, sunday, ally)

        assert ally.extra["sunday_cr_stacks"] == 1


class TestRobinPipeline:
    def test_trace2_initial_advance_is_consumed_when_navs_are_created(self):
        robin = _unit("robin")
        state = simulate([_config(robin)], _enemy(), max_av=0.0)
        actual = state.units[0]
        expected = 10_000.0 / actual.base_stats.SPD * 0.75
        assert state.extra["navs"][0] == pytest.approx(expected)

    def test_concert_rejects_control_status(self):
        robin = _unit("robin")
        state = _state(robin)
        _robin_ult(state, robin)

        applied = _apply_player_status(
            state,
            robin,
            PlayerStatus(id="stun", name="眩晕", category="control", base_chance=1.0),
        )

        assert applied is False
        assert robin.statuses == []

    def test_concert_kill_uses_complete_kill_pipeline(self):
        robin = _unit("robin", position=1)
        attacker = _unit("seele", position=2)
        enemy = _enemy(hp=1.0)
        state = _state(robin, attacker, enemies=[enemy])
        robin.extra["robin_concert"] = True
        state.extra["last_attack_targets"] = [enemy]
        seen = []
        state.hooks.register(
            "robin",
            "on_kill",
            lambda u, state, enemy=None, **_: seen.append(enemy),
        )

        _robin_concert_extra(state, attacker)

        assert state.extra["killed_this_action"] == 1
        assert state.extra["killed_total"] == 1
        assert seen == [enemy]


class TestRuanMeiE2:
    def test_broken_target_attack_bonus_changes_real_damage(self):
        def run(eidolon):
            attacker = _unit("seele", position=1)
            ruan = _unit("ruan_mei", position=2, eidolon=eidolon)
            enemy = _enemy(hp=5_000_000.0)
            enemy.is_broken = True
            state = _state(attacker, ruan, enemies=[enemy])
            before = enemy.HP
            _use_skill(attacker, state, "basic_attack")
            return before - enemy.HP, attacker.base_stats

        e0_damage, stats = run(0)
        e2_damage, _ = run(2)
        expected = (stats.ATK + stats._base_ATK * 0.40) / stats.ATK
        assert e2_damage / e0_damage == pytest.approx(expected)
