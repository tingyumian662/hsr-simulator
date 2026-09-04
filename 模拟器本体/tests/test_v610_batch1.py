"""v6.10 批1 回归: 黄泉（虚无·雷·特殊能量残梦9）+ 全局能量开局规则

语义依据: 角色技能介绍/虚无/黄泉.txt + CLAUDE_HANDOFF v6.10 节
用户规则（2026-08-18）: 常规能量角色开局默认50%能量; 特殊能量角色开局0终结技进度"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats, simulate
from engine.characters.acheron import _acheron_gain_dream, _acheron_apply_jizhen, _acheron_ult, _acheron_talent_on_debuff, _acheron_skill, _acheron_original_damage_multiplier
from engine.runtime import SimState, SimUnit
from engine.characters.acheron import _trace_acheron_trace1


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': -0.2,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestGlobalEnergyRule:
    """v6.10 全局能量开局规则"""

    def test_regular_starts_50pct(self):
        """常规能量角色开局50%能量"""
        s = simulate([{'char': load_character('seele', 'data/characters'),
                       'position': 1}], _enemy(), max_av=0)
        assert s.units[0].current_energy == pytest.approx(60.0)  # 120×0.5

    def test_special_starts_zero(self):
        """特殊能量角色开局0进度"""
        for cid in ('acheron', 'feixiao', 'phainon', 'yinlang'):
            s = simulate([{'char': load_character(cid, 'data/characters'),
                           'position': 1}], _enemy(), max_av=0)
            assert s.units[0].current_energy == 0.0, cid

    def test_explicit_pct_overrides(self):
        """显式 initial_energy_pct 仍覆盖默认50%"""
        s = simulate([{'char': load_character('seele', 'data/characters'),
                       'position': 1, 'initial_energy_pct': 100}],
                     _enemy(), max_av=0)
        assert s.units[0].current_energy == pytest.approx(120.0)

    def test_special_ignores_gain_energy(self):
        """特殊角色 _gain_energy no-op（防秘技/光锥回能污染进度）"""
        u = _unit('acheron')
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.core.combat_engine import _gain_energy
        _gain_energy(u, 25.0, state=state)
        assert u.current_energy == 0.0


class TestAcheron:
    def test_trace1_open(self):
        """行迹1: 开局5残梦+集真赤5层"""
        u = _unit('acheron')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _trace_acheron_trace1(u, state)
        assert u.extra.get('acheron_dream') == 5
        assert e.extra.get('acheron_jizhen') == 5

    def test_skill_gains_dream_and_jizhen(self):
        """战技: +1残梦+集真赤1层"""
        u = _unit('acheron')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _acheron_skill(state, u)
        assert u.extra.get('acheron_dream') == 1
        assert e.extra.get('acheron_jizhen') == 1

    def test_talent_on_debuff(self):
        """天赋: 队友使敌陷入负面→+1残梦+集真赤（每次施放最多1次）"""
        u = _unit('acheron')
        ally = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, ally])
        _acheron_talent_on_debuff(ally, state, e)
        assert u.extra.get('acheron_dream') == 1
        assert e.extra.get('acheron_jizhen') == 1
        _acheron_talent_on_debuff(ally, state, e)  # 同一次施放不重复
        assert u.extra.get('acheron_dream') == 1

    def test_ult_requires_9_dream(self):
        """残梦不足9无法施放终结技"""
        u = _unit('acheron')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['acheron_dream'] = 8
        dmg0 = u.total_damage_dealt
        _acheron_ult(state, u)
        assert u.total_damage_dealt == dmg0
        assert any('残梦不足' in l for l in state.log)

    def test_ult_three_slashes_and_return(self):
        """满9残梦: 3×啼泽雨斩+黄泉返渡, 耗完残梦"""
        u = _unit('acheron')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['acheron_dream'] = 9
        e.extra['acheron_jizhen'] = 3
        _acheron_ult(state, u)
        assert u.total_damage_dealt > 0
        assert u.extra.get('acheron_dream') == 0
        assert e.extra.get('acheron_jizhen', 0) == 0  # 返渡清空集真赤
        assert any('啼泽雨斩' in l for l in state.log)
        assert any('黄泉终结技' in l for l in state.log)

    def test_trace2_nihility_bonus(self):
        """行迹2: 虚无队友1/2名→伤害115%/160%"""
        u = _unit('acheron')
        state = SimState(enemies=[_enemy()], units=[u])
        ally1 = _unit('welt', position=2)  # 虚无
        state2 = SimState(enemies=[_enemy()], units=[u, ally1])
        ally2 = _unit('silver_wolf', position=3)  # 虚无
        state3 = SimState(enemies=[_enemy()], units=[u, ally1, ally2])
        assert _acheron_original_damage_multiplier(u, state) == 1.0
        assert _acheron_original_damage_multiplier(u, state2) == 1.15
        assert _acheron_original_damage_multiplier(u, state3) == 1.60

    def test_ult_res_pen_and_jizhen_clear(self):
        """终结技期全抗-20%: 伤害含抗穿"""
        u = _unit('acheron')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['acheron_dream'] = 9
        _acheron_ult(state, u)
        assert u.total_damage_dealt > 0
