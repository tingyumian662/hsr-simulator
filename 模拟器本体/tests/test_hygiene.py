"""P8: 系统卫生测试（三事件触发 + 口径 + cycles/action_counts 暴露）"""
import glob
import json
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, _gain_energy, _apply_player_status, PlayerStatus,
    simulate,
)


def _enemy(hp=500000, attacks=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks)


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


class TestEvents:
    def test_on_energy_change_fires(self):
        """on_energy_change 事件（此前零触发）"""
        seen = []
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        def spy(**kw):
            seen.append(kw.get('amount'))
        state.hooks.register('seele', 'on_energy_change', spy)
        _gain_energy(u, 20.0, state=state)
        assert seen == [20.0]

    def test_on_enter_exit_state_fires(self):
        """on_enter_state / on_exit_state 事件（此前零触发）"""
        entered, exited = [], []
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        state.hooks.register('seele', 'on_enter_state',
                             lambda **kw: entered.append(kw.get('status')))
        state.hooks.register('seele', 'on_exit_state',
                             lambda **kw: exited.append(kw.get('status')))
        st = PlayerStatus(id='t', name='眩晕', category='control',
                          remaining_turns=1)
        _apply_player_status(state, u, st)
        assert len(entered) == 1
        from engine.core.combat_sim import _check_control_status
        _check_control_status(state, u)
        assert len(exited) == 1

    def test_events_no_side_effects_without_handlers(self):
        """无注册者时事件零行为"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        gained = _gain_energy(u, 20.0, state=state)
        assert gained == pytest.approx(20.0, abs=1e-9)


class TestStatsExposure:
    def test_cycles_and_action_counts(self):
        """simulate 返回 cycles 与 action_counts"""
        chars = [{'char': load_character('seele', 'data/characters'),
                  'position': 1}]
        s = simulate(chars, _enemy(), max_av=300)
        assert s.cycles >= 0
        assert sum(s.action_counts.values()) > 0

    def test_roster_counts(self):
        """口径: 完整 + 空壳（v6.10: 黄泉/飞霄录入 37→39; 空壳 57→55）"""
        full, shells = [], []
        for f in glob.glob('data/characters/*.json'):
            if f.endswith('_template.json') or f.endswith('.bak.json'):
                continue
            d = json.load(open(f, encoding='utf-8'))
            skills = d.get('skills') or {}
            has_basic = 'basic_attack' in skills or 'basic_attack_enhanced' in skills
            (full if has_basic else shells).append(d['id'])
        assert len(full) == 39
        assert len(shells) == 55
