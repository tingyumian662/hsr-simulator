"""v6.10 批2 回归: 飞霄（巡猎·风·特殊能量飞黄12）

语义依据: 角色技能介绍/巡猎/飞霄.txt + CLAUDE_HANDOFF v6.10 节"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _use_skill, _build_effective_stats,
    _feixiao_gain_fly, _feixiao_fua, _feixiao_count_attack,
    _feixiao_on_ally_attack, _feixiao_ult, _feixiao_skill, _feixiao_tick,
)


def _enemy(hp=500000, toughness=200, broken=False):
    e = Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness,
              max_toughness=toughness, level=80,
              element_res={'冰': 0, '量子': 0, '风': -0.2, '雷': 0,
                           '虚数': 0, '物理': 0, '火': 0})
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestFeixiao:
    def test_start_3_fly(self):
        """行迹1: 开局3飞黄（simulate 内联）"""
        from engine.core.combat_sim import simulate
        s = simulate([{'char': load_character('feixiao', 'data/characters'),
                       'position': 1}], _enemy(), max_av=0)
        # 3(行迹1) + 1(进战秘技岚身) = 4
        assert s.units[0].extra.get('feixiao_fly') == 4

    def test_count_attack_every_2(self):
        """天赋: 我方每2次攻击+1飞黄"""
        u = _unit('feixiao')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        u.extra['feixiao_fly'] = 3
        _feixiao_count_attack(state, ally)
        assert u.extra['feixiao_fly'] == 3  # 第1次
        _feixiao_count_attack(state, ally)
        assert u.extra['feixiao_fly'] == 4  # 第2次→+1

    def test_ally_attack_triggers_fua(self):
        """天赋: 队友攻击后立即FUA 110%ATK（每回合最多1次）"""
        u = _unit('feixiao')
        ally = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, ally])
        state.extra['last_attack_targets'] = [e]
        dmg0 = u.total_damage_dealt
        _feixiao_on_ally_attack(state, ally)
        assert u.total_damage_dealt > dmg0
        assert u.extra.get('feixiao_fua_used') is True
        assert any(getattr(b, 'param_id', '') == 'feixiao_fua_buff' for b in u.buffs)
        # 同回合不重复
        dmg1 = u.total_damage_dealt
        _feixiao_on_ally_attack(state, ally)
        assert u.total_damage_dealt == dmg1

    def test_skill_immediate_fua(self):
        """战技: 立即天赋FUA + 行迹3 ATK+48%"""
        u = _unit('feixiao')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        dmg0 = u.total_damage_dealt
        _feixiao_skill(state, u)
        assert u.total_damage_dealt > dmg0  # FUA
        assert any(getattr(b, 'param_id', '') == 'feixiao_trace3' for b in u.buffs)

    def test_ult_requires_6_fly(self):
        """飞黄不足6无法施放终结技"""
        u = _unit('feixiao')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['feixiao_fly'] = 5
        dmg0 = u.total_damage_dealt
        _feixiao_ult(state, u)
        assert u.total_damage_dealt == dmg0
        assert any('飞黄不足' in l for l in state.log)

    def test_ult_six_segments_and_final(self):
        """满6飞黄: 6段60%×1.3+160%末段, 耗6飞黄"""
        u = _unit('feixiao')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['feixiao_fly'] = 6
        _feixiao_ult(state, u)
        assert u.total_damage_dealt > 0
        assert u.extra['feixiao_fly'] == 0
        assert any('飞霄终结技' in l for l in state.log)

    def test_tick_resets_fua_and_trace1(self):
        """回合开始: 重置FUA计数; 行迹1上回合未FUA计入1次攻击"""
        u = _unit('feixiao')
        state = SimState(enemies=[_enemy()], units=[u])
        u.extra['feixiao_fua_used'] = True
        u.extra['feixiao_fly'] = 11
        _feixiao_tick(state, u)
        assert u.extra.get('feixiao_fua_used') is False
        assert u.extra.get('feixiao_attack_count') == 1  # 上回合未FUA计入1次

    def test_e2_fly_per_fua(self):
        """E2: 每FUA+1飞黄（每回合最多6次）"""
        u = _unit('feixiao', eidolon=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['feixiao_fly'] = 0
        _feixiao_fua(state, u, e)
        assert u.extra['feixiao_fly'] == 1
        assert u.extra.get('feixiao_e2_count') == 1
