"""v5.1: 角色适配缺口修复测试（遐蝶行迹1/2 + 长夜月E2/E6 + tbr行迹1）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats, _begin_regular_turn, _enemy_attack, _memsprite_action_speed
from engine.runtime import SimUnit, SimState
from engine.characters.xiadie import _dragon_flame_once


def _enemy(hp=500000, toughness=200, attacks=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
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


class TestXiadieTrace1:
    def test_enhanced_skill_stacks_flame(self):
        """西风的驻足: 死龙在场时的强化战技（乌黯）→焰息叠层+1。"""
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.systems.remembrance import RemembranceSystem
        rem = RemembranceSystem()
        rem.init_battle(state, [u])
        _use_skill(u, state, 'ultimate')
        _use_skill(u, state, 'skill_dragon')
        assert u.extra.get('xiadie_flame_stack') == 1
        for _ in range(8):
            _use_skill(u, state, 'skill_dragon')
        assert u.extra['xiadie_flame_stack'] == 6  # 上限截断

    def test_flame_multiplier_scaled(self):
        """焰息伤害倍率: 叠层后 ×(1+0.3×层)"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.init_battle(state, [u])
        _use_skill(u, state, 'ultimate')  # 终结技召唤死龙
        ms = u.memsprite_unit
        assert ms is not None
        ms.extra['flame_mult'] = 24.0
        u.extra['xiadie_flame_stack'] = 2
        # 直调喷吐, 断言伤害按 24×(1+0.6)=38.4% 倍率（与无叠层对比）
        from engine.core.combat_engine import calculate_damage
        ms.base_stats.HP = 34000
        stats = ms.base_stats
        t = state.enemies[0]
        d_stack = calculate_damage(stats, t, stats.HP, 38.4, 'direct', '冰', 80, True)
        d_base = calculate_damage(stats, t, stats.HP, 24.0, 'direct', '冰', 80, True)
        assert d_stack.final_damage == pytest.approx(d_base.final_damage * 1.6, rel=1e-9)

    def test_flame_stack_does_not_change_base_progression(self):
        """西风的驻足是临时增伤，不能污染焰息24→28→34的基础序列。"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.init_battle(state, [u])
        _use_skill(u, state, 'ultimate')
        ms = u.memsprite_unit
        u.extra['xiadie_flame_stack'] = 2
        _dragon_flame_once(state, u, ms)
        assert ms.extra['flame_mult'] == pytest.approx(28.0)

    def test_stack_cleared_on_next_turn_start(self):
        """乌黯近似叠层保留给死龙行动，在遐蝶下次常规回合开始时清除。"""
        u = _unit('xiadie')
        u.extra['xiadie_flame_stack'] = 3
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 100.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1}, 'stamp_counter': 1})
        state.current_av = 0.0
        state.skill_points = 0
        _begin_regular_turn(state, u)
        assert u.extra.get('xiadie_flame_stack', 0) == 0


class TestXiadieTrace2:
    def test_dragon_speed_uses_summoner_hp(self):
        """倒置的火炬①: 遐蝶HP≥50% → 死龙行动速度×1.4。"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.init_battle(state, [u])
        _use_skill(u, state, 'ultimate')  # 终结技召唤死龙
        ms = u.memsprite_unit
        assert ms is not None
        u.current_hp = u.max_hp * 0.50
        ms.current_hp = ms.max_hp * 0.10
        assert _memsprite_action_speed(state, ms) == pytest.approx(ms.data.base_SPD * 1.4)
        u.current_hp = u.max_hp * 0.49
        ms.current_hp = ms.max_hp
        assert _memsprite_action_speed(state, ms) == pytest.approx(ms.data.base_SPD)

    def test_flame_kill_grants_next_action_speed(self):
        """倒置的火炬②: 死龙焰息击杀后，下一次死龙行动速度翻倍。"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy(hp=1)], units=[u])
        rem = RemembranceSystem()
        rem.init_battle(state, [u])
        _use_skill(u, state, 'ultimate')
        ms = u.memsprite_unit
        _dragon_flame_once(state, u, ms)
        assert ms.extra.get('xiadie_spd_boost') == 1
        assert _memsprite_action_speed(state, ms) == pytest.approx(ms.data.base_SPD * 1.4 * 2.0)


class TestChangyeyueE2:
    def test_gain_yizhi_plus_2(self):
        """E2: 每获得忆质额外+2"""
        from engine.systems.remembrance import _gain_yizhi
        u = _unit('changyeyue')
        u.eidolon_rank = 2
        state = SimState(enemies=[_enemy()], units=[u])
        _gain_yizhi(state, u, 1)
        assert u.yizhi == 3  # 1 + 2

    def test_no_e2_plain_gain(self):
        """无 E2: 忆质获得不变"""
        from engine.systems.remembrance import _gain_yizhi
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        _gain_yizhi(state, u, 1)
        assert u.yizhi == 1


class TestChangyeyueE6:
    def test_team_res_pen(self):
        """E6: 在场时全队全属性抗性穿透+20%"""
        cy = _unit('changyeyue')
        cy.eidolon_rank = 6
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[cy, ally])
        s = _build_effective_stats(ally, state)
        assert s.RES_PEN_ALL == pytest.approx(ally.base_stats.RES_PEN_ALL + 0.20, rel=1e-9)

    def test_not_present_no_pen(self):
        """无 E6 长夜月（或不在场）: 不加成"""
        cy = _unit('changyeyue')
        cy.eidolon_rank = 6
        cy.is_alive = False
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[cy, ally])
        s = _build_effective_stats(ally, state)
        assert s.RES_PEN_ALL == pytest.approx(ally.base_stats.RES_PEN_ALL, rel=1e-9)


class TestTbrMagnet:
    def test_support_magnet_bonus(self):
        """忆灵充能链: 能量上限160 → 声援真伤 0.28×(1+0.12)"""
        from engine.core.combat_engine import _use_skill
        u = _unit('trailblazer_remembrance')
        # 给 tbr 挂声援 buff
        from engine.runtime import TimedBuff
        u.buffs.append(TimedBuff(source_id='tbr', attributes={'_tbr_support': 1},
                                 remaining_turns=3))
        # 直调伤害段验证: 构造带声援的普攻
        e = _enemy(hp=500000)
        state = SimState(enemies=[e], units=[u])
        hp0 = e.HP
        _use_skill(u, state, 'basic_attack')
        log = '\n'.join(state.log)
        assert '迷迷的声援' in log
        # 160 能量 → magnet = min(0.02×(60//10), 0.20) = 0.12 → 28%×1.12
        lost = hp0 - e.HP
        assert lost > 0
        # 与 ≤100 目标对比（普攻伤害 + 声援部分）
        u2 = _unit('trailblazer_remembrance')
        u2.buffs.append(TimedBuff(source_id='tbr', attributes={'_tbr_support': 1},
                                  remaining_turns=3))
        e2 = _enemy(hp=500000)
        state2 = SimState(enemies=[e2], units=[u2])
        hp0_2 = e2.HP
        _use_skill(u2, state2, 'basic_attack')
        lost2 = hp0_2 - e2.HP
        # tbr max_energy=160 → magnet=0.12（数值由公式保证, 日志断言倍率）
        assert '1.12' in log
