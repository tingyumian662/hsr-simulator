"""P7: 超击破接线 + 纠缠推条 + 铁骑4pc 测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _apply_break_debuff, _begin_enemy_turn
from engine.runtime import SimUnit, SimState


def _enemy(hp=500000, toughness=30):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


def _state(e, u):
    s = SimState(enemies=[e], units=[u])
    s.extra.update({'navs': {0: 100.0, ('e', 0): 200.0},
                    'av_stamp': {0: 1, ('e', 0): 2},
                    'stamp_counter': 2})
    s.current_av = 0.0
    return s


class TestSuperBreak:
    def test_triggers_on_broken_target(self):
        """已击破目标被削韧（队内有超击破源忘归人）→ 超击破伤害入账"""
        u = _unit('seele')
        fugue = _unit('fugue')  # 忘归人在场: 天赋光环提供超击破源
        e = _enemy(toughness=10)  # 低韧: 1 次削韧击破, 第 2 次削韧触发超击破
        state = _state(e, u)
        state.units.append(fugue)
        hp0 = e.HP
        dmg0 = u.total_damage_dealt
        _use_skill(u, state, 'basic_attack')  # 击破（击破瞬间不算超击破）
        _use_skill(u, state, 'basic_attack')  # 已击破 + 忘归人光环 → 超击破
        log = '\n'.join(state.log)
        assert '击破弱点' in log
        assert '超击破' in log
        assert e.HP < hp0
        assert u.total_damage_dealt > dmg0

    def test_no_source_no_super_break(self):
        """v5.3 收窄: 无超击破源的队伍命中已击破目标不触发超击破"""
        u = _unit('seele')
        e = _enemy(toughness=0)
        e.is_broken = True
        state = _state(e, u)
        _use_skill(u, state, 'basic_attack')
        assert '超击破' not in '\n'.join(state.log)

    def test_nonweak_attack_does_not_trigger_super_break(self):
        """已击破但不具对应弱点时，攻击没有削韧也不能触发超击破。"""
        u = _unit('seele')
        fugue = _unit('fugue')
        e = _enemy(toughness=0)
        e.is_broken = True
        e.element_res['量子'] = 0.20
        state = _state(e, u)
        state.units.append(fugue)

        _use_skill(u, state, 'basic_attack')

        assert '超击破' not in '\n'.join(state.log)

    def test_no_super_break_before_break(self):
        """未击破目标: 只削韧不触发超击破"""
        u = _unit('seele')
        e = _enemy(toughness=200)  # 高韧
        state = _state(e, u)
        _use_skill(u, state, 'basic_attack')
        assert '超击破' not in '\n'.join(state.log)

    def test_toughness_efficiency_uses_effective_stats(self):
        """战斗中的削韧效率增益应进入实际削韧结算。"""
        from engine.runtime import TimedBuff

        u = _unit('seele')
        u.buffs.append(TimedBuff(
            source_id='test', attributes={'TOUGHNESS_EFFICIENCY': 50.0}, remaining_turns=1,
        ))
        e = _enemy(toughness=200)
        state = _state(e, u)

        _use_skill(u, state, 'basic_attack')

        assert e.toughness == pytest.approx(185.0, abs=1e-6)

    def test_super_break_damage_formula_blackbox(self):
        """黑盒: 单次已击破削韧的超击破 = 削韧值×(1+BE) 进入总伤"""
        u = _unit('seele')
        e = _enemy(toughness=0)
        e.is_broken = True
        e.toughness = 0.0
        state = _state(e, u)
        from engine.core.combat_engine import calculate_damage
        stats = compute_combat_stats(u.char, None, None, None)
        td = 20.0
        sbd = calculate_damage(stats, e, 0, 0, "super_break", '量子', 80,
                               False, toughness_dmg=td)
        expected_base = td * (1.0 + stats.BREAK_EFFECT)
        assert sbd.base_damage == pytest.approx(expected_base, rel=1e-9)


class TestEntangle:
    def test_entangle_pushes_20pct(self):
        """纠缠(量子): 敌方行动时推条 20% 回合值"""
        u = _unit('seele')
        e = _enemy()
        state = _state(e, u)
        _apply_break_debuff(e, '量子', u, state)
        assert e.break_debuff_name == '纠缠'
        _begin_enemy_turn(state, e)
        # 本次行动 AV = current + 10000/80 + 20%×10000/80（纠缠推条）
        expect = state.current_av + 10000.0 / 80.0 * 1.20
        assert state.extra['navs'][('e', 0)] == pytest.approx(expect, rel=1e-6)


class TestIronCavalry:
    def test_be_threshold_defpen(self):
        """铁骑4pc: BE≥150% → 击破/超击破无视20%防御（对比无遗器）"""
        from engine.core.damage import calculate_damage
        from engine.runtime import CharacterAsTarget
        from engine.core.attributes import CombatStats
        # 两套面板: 无遗器 vs 铁骑(条件激活)
        e = _enemy()
        e.is_broken = True
        e.toughness = 0.0
        stats_plain = CombatStats()
        stats_plain.BREAK_EFFECT = 1.5
        stats_iron = CombatStats()
        stats_iron.BREAK_EFFECT = 1.5
        stats_iron.DEF_PEN = 0.20  # 铁骑 20% 无视防御
        d_plain = calculate_damage(stats_plain, e, 0, 0, "super_break", '量子',
                                   80, False, toughness_dmg=100.0)
        d_iron = calculate_damage(stats_iron, e, 0, 0, "super_break", '量子',
                                  80, False, toughness_dmg=100.0)
        assert d_iron.final_damage > d_plain.final_damage  # 无视防御更高
