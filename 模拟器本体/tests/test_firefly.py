"""v5.3: 流萤（击破主C）测试"""
import copy
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _should_ult_now, _super_break_rate, _build_effective_stats
from engine.characters.firefly import _firefly_exit_combustion
from engine.runtime import SimUnit, SimState, TimedBuff
from engine.systems.timeline_marker import TimelineMarkerSystem
from engine.characters.firefly import _trace_firefly_t3_atk_to_be, _firefly_refresh_dr, _eid_firefly_e2_kill, _eid_firefly_e2_reset


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _mk_sys(state):
    sys = TimelineMarkerSystem()
    from engine.characters import (marker_actions, marker_despawns,
                                   marker_spawns)
    sys.action_handlers.update(marker_actions(state))
    sys.despawn_handlers.update(marker_despawns(state))
    sys.spawn_handlers.update(marker_spawns(state))
    state.extra['_marker_sys'] = sys
    return sys


def _enter_combustion(firefly, state):
    """进入完全燃烧（模拟终结技效果）"""
    firefly.current_energy = 240
    _use_skill(firefly, state, 'ultimate')


class TestSkill:
    def test_skill_hp_max_percent(self):
        """战技: 消耗40%生命上限"""
        firefly = _unit('firefly')
        hp0 = firefly.current_hp
        state = SimState(enemies=[_enemy()], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill')
        assert firefly.current_hp == pytest.approx(hp0 - firefly.max_hp * 0.40, abs=1e-6)

    def test_skill_energy_percent(self):
        """战技: 固定恢复60%能量上限（percent 不吃充能绳）"""
        firefly = _unit('firefly')
        state = SimState(enemies=[_enemy()], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill')
        assert firefly.current_energy == pytest.approx(240 * 0.60, abs=1e-6)


class TestCombustion:
    def test_enter_combustion(self):
        """终结技: 进入完全燃烧（SPD+60/行动提前100%/禁终结技）"""
        firefly = _unit('firefly')
        spd0 = firefly.base_stats.SPD
        state = SimState(enemies=[_enemy()], units=[firefly])
        state.extra['navs'] = {0: 500.0}
        _mk_sys(state)
        state.current_av = 100.0
        _enter_combustion(firefly, state)
        assert firefly.extra.get('combustion')
        assert firefly.base_stats.SPD == pytest.approx(spd0 + 60.0, abs=1e-6)
        assert state.extra['navs'][0] == pytest.approx(100.0, abs=1e-6)  # 行动提前100%
        assert _should_ult_now(firefly, state) is False  # 禁终结技
        # 倒计时 marker 创建（70速）
        assert firefly.marker is not None
        assert firefly.marker.action_spd == 70.0

    def test_countdown_exits_combustion(self):
        """倒计时行动 → 退出燃烧（SPD还原）"""
        firefly = _unit('firefly')
        spd0 = firefly.base_stats.SPD
        state = SimState(enemies=[_enemy()], units=[firefly])
        _mk_sys(state)
        state.current_av = 0.0
        _enter_combustion(firefly, state)
        m = firefly.marker
        state.extra['_marker_sys'].handle_action(state, m)
        assert not firefly.extra.get('combustion')
        assert firefly.base_stats.SPD == pytest.approx(spd0, abs=1e-6)
        assert firefly.marker is None  # 标记已移除

    def test_enhanced_basic_heal_and_damage(self):
        """强化普攻: 回20%生命上限 + 200%ATK"""
        firefly = _unit('firefly')
        firefly.current_hp = 100
        e = _enemy()
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'basic_attack_enhanced')
        assert firefly.current_hp == pytest.approx(100 + firefly.max_hp * 0.20, abs=1e-6)
        assert e.HP < 500000

    def test_enhanced_skill_be_formula(self):
        """强化战技: 主目标 (0.2×BE+200)%ATK（BE=200% → 240%）"""
        firefly = _unit('firefly')
        firefly.base_stats.BREAK_EFFECT = 2.0
        e = _enemy()
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill_enhanced')
        stats = _build_effective_stats(firefly, state)
        # 直伤 = ATK × 2.4（240%倍率）×(1+增伤...) 至少大于 ATK×2.4×0.5
        assert e.HP < 500000 - stats.ATK * 2.4 * 0.3
        # 火弱点植入
        assert e.element_res.get('火', 0.2) <= 0.0

    def test_enhanced_skill_uses_effective_break_effect(self):
        """强化战技的动态倍率应包含战斗中获得的击破特攻。"""
        from engine.core.combat_engine import calculate_damage
        from engine.runtime import _enemy_for_damage

        firefly = _unit('firefly')
        firefly.base_stats.BREAK_EFFECT = 1.0
        firefly.buffs.append(TimedBuff(
            source_id='test', attributes={'BREAK_EFFECT': 100.0}, remaining_turns=1,
        ))
        e = _enemy(toughness=200)
        # The skill's pre-damage weakness implant is part of the expected damage state.
        e.element_res['火'] = -0.20
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0
        stats = _build_effective_stats(firefly, state)
        expected = calculate_damage(
            stats, _enemy_for_damage(e), stats.ATK, 240.0, 'direct', '火', 80,
            stats.CRIT_RATE >= 0.5, crit_mode='expected',
        ).final_damage

        _use_skill(firefly, state, 'skill_enhanced')

        assert 500000 - e.HP == pytest.approx(expected, rel=1e-9)

    def test_fire_weakness_expires(self):
        """火弱点植入 2 回合后到期恢复"""
        from engine.core.combat_engine import _begin_enemy_turn
        firefly = _unit('firefly')
        e = _enemy()
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill_enhanced')
        old = e.element_res['火']
        assert old < 0
        _begin_enemy_turn(state, e)  # 回合1
        _begin_enemy_turn(state, e)  # 回合2 → 到期
        assert e.element_res['火'] == pytest.approx(0.0, abs=1e-9)

    def test_enhanced_skill_uses_new_fire_weakness_immediately(self):
        """强化战技植入火弱点后，本次攻击应立即削韧。"""
        firefly = _unit('firefly')
        e = _enemy(toughness=200)
        e.element_res['火'] = 0.20
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0

        _use_skill(firefly, state, 'skill_enhanced')

        assert e.element_res['火'] == pytest.approx(-0.20, abs=1e-9)
        assert e.toughness == pytest.approx(170.0, abs=1e-6)

    def test_refresh_fire_weakness_preserves_original_resistance(self):
        """重复施加火弱点只刷新持续时间，结束后恢复初始抗性。"""
        from engine.core.combat_engine import _begin_enemy_turn
        firefly = _unit('firefly')
        e = _enemy()
        e.element_res['火'] = 0.20
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0

        _use_skill(firefly, state, 'skill_enhanced')
        _begin_enemy_turn(state, e)  # 剩余 1 回合
        _use_skill(firefly, state, 'skill_enhanced')  # 刷新至 2 回合
        _begin_enemy_turn(state, e)
        _begin_enemy_turn(state, e)

        assert e.element_res['火'] == pytest.approx(0.20, abs=1e-9)

    def test_fire_weakness_and_lingsha_e6_restore_independently(self):
        """火弱点与浮元E6叠加后，无论先后结束都不覆盖另一效果。"""
        from engine.core.combat_engine import _begin_enemy_turn
        firefly = _unit('firefly')
        lingsha = _unit('lingsha', position=2, eidolon=6)
        e = _enemy()
        state = SimState(enemies=[e], units=[firefly, lingsha])
        state.current_av = 0.0
        sys = _mk_sys(state)

        sys.spawn(state, lingsha, 'lingsha_fuyuan')
        from engine.core.combat_engine import _apply_skill_effects
        _apply_skill_effects(
            firefly, state, firefly.char.skills['skill_enhanced'], 'skill_enhanced',
        )
        assert e.element_res['火'] == pytest.approx(-0.40, abs=1e-9)

        sys.despawn(state, lingsha.marker)
        assert e.element_res['火'] == pytest.approx(-0.20, abs=1e-9)
        _begin_enemy_turn(state, e)
        _begin_enemy_turn(state, e)
        assert e.element_res['火'] == pytest.approx(0.0, abs=1e-9)

    def test_e1_enhanced_skill_no_sp(self):
        """E1: 强化战技不消耗战技点"""
        firefly = _unit('firefly', eidolon=1)
        state = SimState(enemies=[_enemy()], units=[firefly])
        state.skill_points = 0
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill_enhanced')  # SP=0 也应施放
        assert '死星过载' in '\n'.join(state.log)

    def test_e1_defpen(self):
        """E1: 强化战技无视15%防御"""
        firefly = _unit('firefly', eidolon=1)
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[firefly])
        state.current_av = 0.0
        _use_skill(firefly, state, 'skill_enhanced')
        # 伤害包含 DEF_PEN+15%: 数值上高于无 E1
        assert e.HP < 500000

    def test_ult_break_dmg_bonus_on_enhanced_attack(self):
        """终结技: 强化攻击使目标受到萨姆造成的击破伤害+20%（本次攻击, 击破段）"""
        from engine.core.combat_engine import _apply_toughness_damage
        e1 = _enemy(toughness=10)
        firefly = _unit('firefly')
        firefly.extra['combustion'] = True
        state1 = SimState(enemies=[e1], units=[firefly])
        state1.current_av = 0.0
        stats1 = _build_effective_stats(firefly, state1)
        _apply_toughness_damage(state1, firefly, e1, 10.0, '火', 'skill_enhanced', stats1)
        e2 = _enemy(toughness=10)
        firefly2 = _unit('firefly')
        state2 = SimState(enemies=[e2], units=[firefly2])
        state2.current_av = 0.0
        stats2 = _build_effective_stats(firefly2, state2)
        _apply_toughness_damage(state2, firefly2, e2, 10.0, '火', 'basic_attack', stats2)
        dmg_burn = 500000 - e1.HP
        dmg_plain = 500000 - e2.HP
        # 燃烧强化攻击击破伤害 = 普攻击破伤害 × 1.2（其余乘区相同, 比值精确）
        assert dmg_burn == pytest.approx(dmg_plain * 1.2, rel=1e-6)


class TestTalent:
    def test_dr_curve(self):
        """天赋减伤: HP≤20% → 40%减伤"""
        firefly = _unit('firefly')
        firefly.current_hp = firefly.max_hp * 0.1
        state = SimState(enemies=[_enemy()], units=[firefly])
        _firefly_refresh_dr(firefly, state)
        assert firefly.base_stats.DMG_REDUCTION == pytest.approx(0.40, abs=1e-6)

    def test_dr_half_hp(self):
        """天赋减伤: HP=50% → 20%减伤（线性）"""
        firefly = _unit('firefly')
        firefly.current_hp = firefly.max_hp * 0.5
        state = SimState(enemies=[_enemy()], units=[firefly])
        _firefly_refresh_dr(firefly, state)
        assert firefly.base_stats.DMG_REDUCTION == pytest.approx(0.40 * 0.625, abs=1e-6)

    def test_trace3_atk_to_be(self):
        """行迹3: ATK>1800 每超10点 BE+0.8%"""
        firefly = _unit('firefly')
        firefly.base_stats.ATK = 2000.0
        be0 = firefly.base_stats.BREAK_EFFECT
        state = SimState(enemies=[_enemy()], units=[firefly])
        _trace_firefly_t3_atk_to_be(firefly, state)
        assert firefly.base_stats.BREAK_EFFECT == pytest.approx(
            be0 + (2000 - 1800) / 10.0 * 0.008, abs=1e-9)

    def test_super_break_rate_threshold(self):
        """行迹2: 燃烧下 BE≥300% → 150%转化; BE≥150% → 100%"""
        firefly = _unit('firefly')
        state = SimState(enemies=[_enemy()], units=[firefly])
        firefly.base_stats.BREAK_EFFECT = 1.6
        firefly.extra['combustion'] = True
        assert _super_break_rate(state, firefly) == pytest.approx(1.0, abs=1e-9)
        firefly.base_stats.BREAK_EFFECT = 3.2
        assert _super_break_rate(state, firefly) == pytest.approx(1.5, abs=1e-9)

    def test_super_break_rate_uses_effective_break_effect(self):
        """行迹2阈值包含战斗中获得的击破特攻。"""
        firefly = _unit('firefly')
        firefly.base_stats.BREAK_EFFECT = 1.0
        firefly.extra['combustion'] = True
        firefly.buffs.append(TimedBuff(
            source_id='test', attributes={'BREAK_EFFECT': 60.0}, remaining_turns=1,
        ))
        state = SimState(enemies=[_enemy()], units=[firefly])

        assert _build_effective_stats(firefly, state).BREAK_EFFECT == pytest.approx(1.6)
        assert _super_break_rate(state, firefly) == pytest.approx(1.0, abs=1e-9)


class TestEidolons:
    def test_e2_kill_extra_turn(self):
        """E2: 燃烧下强化攻击击杀→额外回合（每回合1次）"""
        firefly = _unit('firefly', eidolon=2)
        firefly.extra['combustion'] = True
        state = SimState(enemies=[_enemy()], units=[firefly])
        state.extra['extra_turns'] = []
        _eid_firefly_e2_kill(firefly, state)
        assert len(state.extra['extra_turns']) == 1
        # 再次触发被拒（每回合1次）
        _eid_firefly_e2_kill(firefly, state)
        assert len(state.extra['extra_turns']) == 1
        # 回合开始重置
        _eid_firefly_e2_reset(firefly, state)
        _eid_firefly_e2_kill(firefly, state)
        assert len(state.extra['extra_turns']) == 2

    def test_e6_res_pen_fire(self):
        """E6: 燃烧下火属性抗性穿透+20%"""
        firefly = _unit('firefly', eidolon=6)
        state = SimState(enemies=[_enemy()], units=[firefly])
        _mk_sys(state)
        state.current_av = 0.0
        _enter_combustion(firefly, state)
        assert firefly.base_stats.RES_PEN['火'] > 0.19
        # 退出还原
        _firefly_exit_combustion(firefly, state)
        assert firefly.base_stats.RES_PEN['火'] == pytest.approx(
            compute_combat_stats(firefly.char, None, None, None).RES_PEN['火'], abs=1e-9)
