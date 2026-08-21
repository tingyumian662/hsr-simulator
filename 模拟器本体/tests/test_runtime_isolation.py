"""每局运行时隔离、欢愉行动条和错误传播回归测试。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, TimedBuff, _ai_regular_action, _build_effective_stats, simulate,
)
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.systems.elation import ElationSystem


def _enemy(element):
    return Enemy(id='runtime', name='Runtime', HP=500000, DEF=800, SPD=80,
                 toughness=20, max_toughness=20, element_res={element: 0})


def _run(char_id, max_av=500):
    char = load_character(char_id, 'data/characters')
    return simulate([{'char': char, 'position': 1}], _enemy(char.element), max_av=max_av)


def test_elation_aha_uses_monotonic_av_without_name_error():
    state = _run('yinlang')
    aha_avs = [float(line.split('AV=')[1].split()[0])
               for line in state.log if line.startswith('[Aha]')]
    assert len(aha_avs) >= 2
    assert aha_avs == sorted(aha_avs)
    assert not any('unit_av' in line or '[ERROR]' in line for line in state.log)


def test_sequential_simulations_do_not_leak_runtime_registries():
    first = _run('yinlang')
    normal = _run('seele')
    second = _run('yinlang')

    assert first.hooks is not normal.hooks and normal.hooks is not second.hooks
    assert 'yinlang' in first.ai_registry and 'yinlang' in second.ai_registry
    assert 'yinlang' not in normal.ai_registry
    assert normal.skill_hooks == {}
    assert first.skill_hooks is not second.skill_hooks


def test_concurrent_simulations_keep_state_isolated():
    with ThreadPoolExecutor(max_workers=2) as pool:
        elation_future = pool.submit(_run, 'yinlang')
        normal_future = pool.submit(_run, 'seele')
        elation, normal = elation_future.result(), normal_future.result()

    assert elation.hooks is not normal.hooks
    assert any(line.startswith('[Aha]') for line in elation.log)
    assert not any(line.startswith('[Aha]') for line in normal.log)
    assert 'yinlang' not in normal.ai_registry


def test_elation_effective_stats_include_regular_timed_buffs():
    char = load_character('yinlang', 'data/characters')
    unit = SimUnit(char=char, base_stats=compute_combat_stats(char), position=1)
    unit.buffs.append(TimedBuff(source_id='test', attributes={'CRIT_DMG': 20.0},
                                remaining_turns=1))
    unit.hidden_score = 10
    state = SimState(units=[unit])
    state.extra['_elation'] = ElationSystem()

    effective = _build_effective_stats(unit, state)
    assert effective.CRIT_DMG >= unit.base_stats.CRIT_DMG + 0.20
    assert effective.CRIT_RATE > unit.base_stats.CRIT_RATE


def test_ai_error_is_logged_and_rethrown():
    char = load_character('seele', 'data/characters')
    unit = SimUnit(char=char, base_stats=compute_combat_stats(char), position=1)
    state = SimState(units=[unit])

    def explode(*args, **kwargs):
        raise RuntimeError('intentional AI failure')

    state.ai_registry[char.id] = explode
    with pytest.raises(RuntimeError, match='intentional AI failure'):
        _ai_regular_action(state, unit)
    assert any('[ERROR]' in line and 'intentional AI failure' in line for line in state.log)
