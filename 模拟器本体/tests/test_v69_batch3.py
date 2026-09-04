"""v6.9 批3 回归: 千冶·刃（虚无·火·无量忿怒状态机）

语义依据: 角色技能介绍/虚无/千冶·刃.txt + CLAUDE_HANDOFF v6.9 节"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats, _gain_energy
from engine.characters.qianye import _qianye_enter_wrath, _qianye_exit_wrath, _qianye_ult, _qianye_new_ult, _qianye_skill, _qianye_extra_skill, _qianye_gain_charge, _qianye_on_ally_attack, _qianye_tick, _qianye_apply_shaqizhaoshen
from engine.runtime import SimState, SimUnit
from engine.characters.qianye import _tech_qianye


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': -0.2})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestQianye:
    def test_ult_enter_wrath(self):
        """终结技: 全敌煞火缠身(DEF-30%+受伤50%)+耗20%生命上限+无量忿怒"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        hp0 = u.current_hp
        _qianye_ult(state, u)
        assert e.has_status(status_id='qianye_shaqi')
        assert u.current_hp == pytest.approx(hp0 - u.max_hp * 0.20)
        assert u.extra.get('qianye_wrath') is True
        assert any(getattr(b, 'attributes', {}).get('CRIT_RATE') == 20.0 for b in u.buffs)

    def test_wrath_enhanced_basic(self):
        """无量忿怒期普攻强化为淬锋断魄(100%生命上限)"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _qianye_enter_wrath(state, u)
        _use_skill(u, state, 'basic_attack')
        assert u.total_damage_dealt > 0
        assert any('普攻强化' in l for l in state.log)

    def test_skill_locked_outside_wrath(self):
        """非无量忿怒期战技不可用"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        dmg0 = u.total_damage_dealt
        _qianye_skill(state, u, 'skill')
        assert u.total_damage_dealt == dmg0  # 未造成伤害
        assert any('战技不可用' in l for l in state.log)

    def test_new_ult_damage(self):
        """千冶铸一，万劫烬灭: 300%生命上限火伤"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _qianye_new_ult(state, u)
        assert u.total_damage_dealt > 0
        assert any('千冶铸一' in l for l in state.log)

    def test_talent_charge_extra_skill(self):
        """天赋: 结界期攻击→煞火缠身+充能; 充能满→回25能+额外战技"""
        u = _unit('qianye')
        attacker = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, attacker])
        _qianye_enter_wrath(state, u)
        u.extra['qianye_charge'] = 8  # 差1到9
        state.extra['last_attack_targets'] = [e]
        e0 = u.current_energy
        _qianye_on_ally_attack(state, attacker)
        assert e.has_status(status_id='qianye_shaqi')
        assert u.current_energy - e0 >= 25.0  # 充能满回25能
        assert u.total_damage_dealt > 0  # 额外战技
        assert any('额外战技' in l for l in state.log)

    def test_fatal_not_dead(self):
        """致命攻击不死: 结界解除+回50%生命上限"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _qianye_enter_wrath(state, u)
        u.current_hp = 10
        _qianye_exit_wrath(state, u, fatal=True)
        # 回复50%生命上限=加算: 10 + 上限×50%
        assert u.current_hp == pytest.approx(10 + u.max_hp * 0.50)
        assert not u.extra.get('qianye_wrath')

    def test_countdown_expire(self):
        """70速倒计时到期: 结界解除"""
        u = _unit('qianye')
        state = SimState(enemies=[_enemy()], units=[u])
        state.current_av = 100.0
        _qianye_enter_wrath(state, u)
        state.current_av = 100.0 + 10000.0 / 70.0 + 1  # 超过143AV
        _qianye_tick(state, u)
        assert not u.extra.get('qianye_wrath')
        assert any('倒计时到期' in l for l in state.log)

    def test_overflow_energy(self):
        """行迹1: 溢出能量最多80, 终结技后恢复"""
        u = _unit('qianye')
        state = SimState(enemies=[_enemy()], units=[u])
        u.current_energy = 150
        _gain_energy(u, 50.0, state=state)  # 150+50=200, 截断至160, 溢出40
        assert u.current_energy == 160
        assert u.extra.get('qianye_overflow') == pytest.approx(40.0)
        # 施放千冶铸一(清空能量)恢复溢出
        _qianye_new_ult(state, u)
        assert u.current_energy == pytest.approx(40.0)

    def test_technique(self):
        """秘技(进战): 全敌嘲讽+自身受伤-90%"""
        u = _unit('qianye')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _tech_qianye(state, u, is_opener=True)
        assert any(getattr(b, 'param_id', '') == 'qianye_tech_dr' for b in u.buffs)
        assert any('十方无赦' in l for l in state.log)
