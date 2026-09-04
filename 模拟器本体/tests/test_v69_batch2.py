"""v6.9 批2 回归: 知更鸟（同谐·物理·协奏状态机）+ 不死途（巡猎·雷·饲饵/婪酣）

语义依据: 角色技能介绍/同谐/知更鸟.txt、巡猎/不死途.txt + CLAUDE_HANDOFF v6.9 节
用户确认（2026-08-18）: 不死途速度=106; 不死途秘技=非进战"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats
from engine.characters.robin import _robin_skill, _robin_ult, _robin_concert_extra, _robin_tick, _robin_ai
from engine.characters.busitu import _busitu_skill, _busitu_ult, _busitu_fua, _busitu_on_ally_attack, _busitu_apply_bait, _busitu_bait_target
from engine.runtime import SimState, SimUnit
from engine.characters.robin import _trace_robin_trace2
from engine.characters.busitu import _trace_busitu_trace3, _trace_busitu_e1


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': -0.2,
                              '虚数': 0, '物理': -0.2, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestRobin:
    def test_skill_team_dmg_bonus(self):
        """战技: 全队增伤50%"""
        u = _unit('robin')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _robin_skill(state, u)
        assert any(getattr(b, 'attributes', {}).get('DMG_BONUS_ALL') == 50.0
                   for b in ally.buffs)

    def test_ult_concert_immediate_and_atk(self):
        """终结技: 队友立即行动 + 全队ATK+22.8%+200"""
        u = _unit('robin')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {0: 500.0, 1: 600.0}
        _robin_ult(state, u)
        assert u.extra.get('robin_concert') is True
        assert state.extra['navs'][1] == pytest.approx(0.0)  # 队友立即行动
        assert any(getattr(b, 'attributes', {}).get('ATK_percent') == 22.8
                   for b in ally.buffs)

    def test_concert_extra_fixed_crit(self):
        """协奏附加: 120%ATK物理伤固定双暴(CR100%/CD150%)"""
        u = _unit('robin')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['robin_concert'] = True
        state.extra['last_attack_targets'] = [e]
        dmg0 = u.total_damage_dealt
        _robin_concert_extra(state, u)
        assert u.total_damage_dealt > dmg0

    def test_concert_expire_immediate_action(self):
        """协奏2回合到期: 退出+立即行动"""
        u = _unit('robin')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {0: 500.0, 1: 600.0}
        _robin_ult(state, u)
        _robin_tick(state, u)  # 第1回合
        _robin_tick(state, u)  # 第2回合→到期
        assert not u.extra.get('robin_concert')
        assert state.extra['navs'][0] == pytest.approx(0.0)  # 知更鸟立即行动

    def test_trace2_start_advance(self):
        """行迹2: 开局行动提前25%"""
        u = _unit('robin')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 500.0}
        _trace_robin_trace2(u, state)
        assert state.extra['navs'][0] < 500.0


class TestBusitu:
    def test_skill_bait_and_sp(self):
        """战技: 目标成饲饵+回1SP+全敌DEF-40%"""
        u = _unit('busitu')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _busitu_skill(state, u, e)
        assert _busitu_bait_target(state) is e
        assert e.has_status(status_id='busitu_def_down')
        assert e.status_attribute('def_reduction') == pytest.approx(0.40)

    def test_talent_fua_on_bait_attacked(self):
        """天赋: 饲饵受其他目标攻击→回8能+耗1充能FUA+2层婪酣"""
        u = _unit('busitu')
        attacker = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, attacker])
        u.extra['busitu_charge'] = 2
        _busitu_apply_bait(state, u, e)
        state.extra['last_attack_targets'] = [e]
        e0 = u.current_energy
        _busitu_on_ally_attack(state, attacker)
        assert u.current_energy - e0 == pytest.approx(8.0)
        assert u.extra['busitu_charge'] == 1
        assert u.extra.get('busitu_lanhan', 0) >= 2
        assert u.total_damage_dealt > 0

    def test_trace2_fua_multiplier(self):
        """行迹2: FUA伤害+80%+每层婪酣+10%"""
        u = _unit('busitu')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.extra['busitu_lanhan'] = 5
        dmg0 = u.total_damage_dealt
        _busitu_fua(state, u, e, enhanced=False)
        assert u.total_damage_dealt > dmg0
        assert u.extra['busitu_lanhan'] >= 7  # FUA后+2层

    def test_ult_charge_and_enhanced_fua(self):
        """终结技: +3充能(上限3)+强化FUA"""
        u = _unit('busitu')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _busitu_ult(state, u, e)
        assert u.extra['busitu_charge'] == 3
        assert u.total_damage_dealt > 0
        assert any('不死途FUA' in l for l in state.log)

    def test_e1_team_vuln(self):
        """E1: 全敌受伤+24%"""
        u = _unit('busitu', eidolon=1)
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _trace_busitu_e1(u, state)
        assert e.extra.get('busitu_e1_vuln') == pytest.approx(0.24)

    def test_trace3_team_cd(self):
        """行迹3: 全队暴伤+40%"""
        u = _unit('busitu')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _trace_busitu_trace3(u, state)
        assert any(getattr(b, 'attributes', {}).get('CRIT_DMG') == 40.0
                   for b in ally.buffs)
