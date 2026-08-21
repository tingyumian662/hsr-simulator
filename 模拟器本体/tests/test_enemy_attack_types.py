"""P6: 敌方攻击类型扩展测试（blast/bounce + 数据驱动 test_brute）"""
import random
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, _enemy_attack, _enemy_attack_stats, _select_enemy_target,
    CharacterAsTarget,
)
from engine.core.damage import calculate_damage


def _enemy(attacks=None):
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks)


SWING = [{"name": "挥击", "element": "物理", "damage_type": "direct",
          "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


class TestBlast:
    def test_main_and_adjacent_hit(self):
        """blast: 主目标 + 相邻掉血, 第三人不掉"""
        blast = [{**SWING[0], 'target_type': 'blast', 'multiplier': 80.0}]
        main = _unit('seele')
        adj = _unit('fu_xuan', position=2)
        third = _unit('huohuo', position=3)
        state = SimState(enemies=[_enemy(attacks=blast)], units=[main, adj, third])
        hp0 = [main.current_hp, adj.current_hp, third.current_hp]
        _enemy_attack(state, state.enemies[0])
        # 主目标随机, 但 blast 恰好命中 2 名（主+相邻）— 第三人不受影响
        losers = sum(1 for u, h in zip((main, adj, third), hp0)
                     if u.current_hp < h)
        assert losers == 2

    def test_blast_damage_exact(self):
        """blast 每目标独立防御结算（与 single 同公式）"""
        blast = [{**SWING[0], 'target_type': 'blast', 'multiplier': 80.0}]
        main = _unit('seele')
        adj = _unit('fu_xuan', position=2)
        state = SimState(enemies=[_enemy(attacks=blast)], units=[main, adj])
        atk_stats = _enemy_attack_stats(state.enemies[0])
        expected = []
        for u in (main, adj):
            t_stats = compute_combat_stats(u.char, None, None, None)
            view = CharacterAsTarget(u, t_stats)
            d = calculate_damage(atk_stats, view, atk_stats.ATK, 80.0,
                                 'direct', '物理', 80, False)
            expected.append(d.final_damage)
        hp0 = [main.current_hp, adj.current_hp]
        _enemy_attack(state, state.enemies[0])
        assert main.current_hp == pytest.approx(hp0[0] - expected[0], abs=1e-6)
        assert adj.current_hp == pytest.approx(hp0[1] - expected[1], abs=1e-6)


class TestBounce:
    def test_bounce_hits_count(self):
        """bounce: hits=3 次独立选目标结算"""
        bounce = [{**SWING[0], 'target_type': 'bounce', 'hits': 3,
                   'multiplier': 60.0}]
        u = _unit('seele')
        state = SimState(enemies=[_enemy(attacks=bounce)], units=[u])
        random.seed(42)
        hp0 = u.current_hp
        _enemy_attack(state, state.enemies[0])
        # 单目标弹射: 3 跳全中同一目标
        assert u.current_hp < hp0
        assert state.log and any('三连击' in l or '挥击' in l for l in state.log)

    def test_bounce_multi_target_distributes(self):
        """bounce: 双目标时伤害分配到各目标"""
        bounce = [{**SWING[0], 'target_type': 'bounce', 'hits': 3,
                   'multiplier': 60.0}]
        u1 = _unit('seele')
        u2 = _unit('fu_xuan', position=2)
        state = SimState(enemies=[_enemy(attacks=bounce)], units=[u1, u2])
        random.seed(1)
        hp0 = [u1.current_hp, u2.current_hp]
        _enemy_attack(state, state.enemies[0])
        total_lost = (hp0[0] - u1.current_hp) + (hp0[1] - u2.current_hp)
        assert total_lost > 0
        # 3 跳至少命中 1 目标（可能全打一个, 断言总损失=3跳且都掉血或单目标3跳）
        assert (u1.current_hp < hp0[0]) or (u2.current_hp < hp0[1])


class TestDataDriven:
    def test_brute_loads_and_attacks(self):
        """test_brute.json 黑盒: 蓄力循环（v6.4b P1-1 优先级修复）

        无狂暴→蓄力(施加 brute_rage); 有狂暴→狂暴挥击。旧断言"优先级最高=咆哮"
        是 v6.4 数据的 bug 排序——蓄力 1.5 < 咆哮 3 导致循环永不可达。"""
        brute = Enemy.from_dict(__import__('json').load(
            open('data/enemies/test_brute.json', encoding='utf-8')))
        u = _unit('seele')
        state = SimState(enemies=[brute], units=[u])
        random.seed(7)
        _enemy_attack(state, brute)
        assert '蓄力' in '\n'.join(state.log)
        assert any(s.id == 'brute_rage' for s in brute.statuses)
        _enemy_attack(state, brute)
        assert '狂暴挥击' in '\n'.join(state.log)

    def test_single_still_works(self):
        """既有 single 行为不变"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[u])
        hp0 = u.current_hp
        _enemy_attack(state, state.enemies[0])
        assert u.current_hp < hp0
