"""敌方状态、减益路由和目标相关遗器测试。"""
from types import SimpleNamespace

from engine.core.attributes import CombatStats
from engine.core.combat_sim import (
    SimState, _apply_break_debuff, _apply_skill_effects,
    _apply_target_relic_modifiers, _multihit_damage,
)
from engine.models.character import Character, Skill, SkillEffect
from engine.models.enemy import Enemy, EnemyStatus


def _enemy(enemy_id='e'):
    return Enemy(id=enemy_id, name=enemy_id, HP=10000, DEF=0, SPD=80,
                 toughness=20, max_toughness=20, element_res={'虚数': 0})


def _unit(conditions=()):
    return SimpleNamespace(
        char=SimpleNamespace(id='wearer', name='Wearer'),
        _active_relic_conditions=set(conditions), extra={},
    )


def test_enemy_status_refresh_query_and_expire():
    enemy = _enemy()
    enemy.add_status(EnemyStatus('burn', '灼烧', 'dot', 'test', 2))
    enemy.add_status(EnemyStatus('burn', '灼烧', 'dot', 'test', 3))
    enemy.add_status(EnemyStatus('slow', '减速', 'debuff', 'test', 1))

    assert len(enemy.statuses) == 2
    assert enemy.dot_count() == 1
    assert enemy.debuff_count() == 2
    expired = enemy.tick_statuses()
    assert [status.id for status in expired] == ['slow']
    assert enemy.has_status(status_id='burn')


def test_dynamic_relics_are_target_specific_and_capped():
    stats = CombatStats(CRIT_RATE=0.40, CRIT_DMG=0.50)
    wearer = _unit({'defpen_per_dot', 'cr_vs_debuff', 'cd_per_debuff_count'})
    affected = _enemy('affected')
    for idx in range(4):
        affected.add_status(EnemyStatus(f'dot-{idx}', f'DOT{idx}', 'dot', 'test', 2))
    affected.add_status(EnemyStatus('break:虚数', '禁锢', 'control', 'test', 2))
    clean = _enemy('clean')

    boosted = _apply_target_relic_modifiers(stats, wearer, affected)
    untouched = _apply_target_relic_modifiers(stats, wearer, clean)

    assert boosted.DEF_PEN == 0.18
    assert boosted.CRIT_RATE == 0.50
    assert boosted.CRIT_DMG == 0.82
    assert untouched.DEF_PEN == 0
    assert untouched.CRIT_RATE == 0.40
    assert untouched.CRIT_DMG == 0.50


def test_pioneer_double_effect():
    stats = CombatStats(CRIT_DMG=0.50)
    wearer = _unit({'cd_per_debuff_count'})
    enemy = _enemy()
    for idx in range(3):
        enemy.add_status(EnemyStatus(f'd-{idx}', f'D{idx}', 'debuff', 'test', 2))
    wearer.extra['pioneer_double_turns'] = 1
    assert _apply_target_relic_modifiers(stats, wearer, enemy).CRIT_DMG == 0.74


def test_debuff_effect_routes_to_enemies_and_marks_pioneer():
    char = Character(id='caster', name='Caster', element='虚数', path='虚无')
    unit = SimpleNamespace(char=char, extra={}, _active_relic_conditions={'cd_per_debuff_count'})
    enemies = [_enemy('a'), _enemy('b')]
    state = SimState(enemies=enemies, units=[unit])
    skill = Skill(name='Debuff', type='skill', target='all_enemies', effects=[
        SkillEffect(type='debuff', target='all_enemies', value=16, param_id='凶星低语')
    ])

    _apply_skill_effects(unit, state, skill, 'skill')

    assert all(enemy.has_status(status_id='凶星低语') for enemy in enemies)
    assert all(enemy.status_attribute('vulnerability') == 0.16 for enemy in enemies)
    assert unit.extra['pioneer_double_pending'] is True


def test_break_debuff_syncs_legacy_fields_and_status():
    enemy = _enemy()
    attacker = _unit()
    _apply_break_debuff(enemy, '虚数', attacker)
    assert enemy.break_debuff_name == '禁锢'
    assert enemy.break_debuff_turns == 2
    assert enemy.has_status(status_id='break:虚数', category='control')


def test_memsprite_summon_reuses_the_state_remembrance_system():
    """召唤忆灵时复用本局的记忆系统，避免丢失运行时状态。"""
    summoned = []

    class StubRemembranceSystem:
        def summon_memsprite(self, state, unit, memsprite):
            summoned.append((state, unit, memsprite))

    memsprite = object()
    unit = SimpleNamespace(char=SimpleNamespace(memsprite=memsprite))
    state = SimState(enemies=[_enemy()], units=[unit])
    rem = StubRemembranceSystem()
    state.extra['_rem_sys'] = rem
    skill = SimpleNamespace(effects=[SimpleNamespace(type='summon_memsprite')])

    _apply_skill_effects(unit, state, skill, 'skill')

    assert state.extra['_rem_sys'] is rem
    assert summoned == [(state, unit, memsprite)]


def test_bounce_damage_reduces_target_hp():
    enemy = _enemy()
    stats = CombatStats(ATK=100, CRIT_RATE=0, CRIT_DMG=0.5)
    before = enemy.HP
    total = _multihit_damage(stats, [enemy], 100, 100, 'direct', '虚数', False, hits=3)
    assert total > 0
    assert before - enemy.HP == total
