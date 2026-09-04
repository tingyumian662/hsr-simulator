"""P2: ENERGY_REGEN 倍率 + 受击回能 + 藿藿禳命计时测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _enemy_attack, _begin_regular_turn
from engine.runtime import SimUnit, SimState


def _enemy(hp=500000, atk=100, attacks=None):
    return Enemy(id='x', name='X', HP=hp, ATK=atk, DEF=800, SPD=80,
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


class TestEnergyRegen:
    def test_basic_gain_scaled_by_er(self):
        """标准回能(普攻20) × ENERGY_REGEN 1.194 → 23.88"""
        u = _unit('seele')
        u.base_stats.ENERGY_REGEN = 1.194
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'basic_attack')
        assert u.current_energy == pytest.approx(20.0 * 1.194, abs=1e-6)

    def test_percent_gain_not_scaled(self):
        """藿藿终结技队友回20%上限: 不吃 ENERGY_REGEN（ER=1.5 仍回 20%）"""
        huohuo = _unit('huohuo')
        ally = _unit('seele', position=2)
        ally.base_stats.ENERGY_REGEN = 1.5
        state = SimState(enemies=[_enemy()], units=[huohuo, ally])
        _use_skill(huohuo, state, 'ultimate')
        assert ally.current_energy == pytest.approx(
            (ally.char.max_energy or 0) * 0.20, abs=1e-6)

    def test_gain_capped_at_max_energy(self):
        """回能上限截断（溢出即浪费, 实机语义）"""
        u = _unit('seele')
        u.current_energy = (u.char.max_energy or 999) - 5
        u.base_stats.ENERGY_REGEN = 1.194
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'basic_attack')
        assert u.current_energy == u.char.max_energy


class TestHitEnergy:
    def test_energy_gain_field(self):
        """attacks[].energy_gain=5 → 受击回能+5"""
        from engine.core.combat_engine import _enemy_attack
        u = _unit('seele')
        atk = [{**SWING[0], 'energy_gain': 5}]
        state = SimState(enemies=[_enemy(attacks=atk)], units=[u])
        _enemy_attack(state, state.enemies[0])
        assert u.current_energy == pytest.approx(5.0, abs=1e-6)

    def test_no_field_no_gain(self):
        """无 energy_gain 字段 → 不回能（既有敌人零影响）"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[u])
        _enemy_attack(state, state.enemies[0])
        assert u.current_energy == 0.0

    def test_energy_gain_respects_cap(self):
        """受击回能同样受上限截断"""
        u = _unit('seele')
        u.current_energy = (u.char.max_energy or 999) - 2
        atk = [{**SWING[0], 'energy_gain': 5}]
        state = SimState(enemies=[_enemy(attacks=atk)], units=[u])
        _enemy_attack(state, state.enemies[0])
        assert u.current_energy == u.char.max_energy


class TestRuming:
    def test_skill_attaches_ruming(self):
        """v6.10.6: 藿藿战技→藿藿自身获得禳命3回合（此前错误挂在受疗者身上2回合）"""
        huohuo = _unit('huohuo')
        seele = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[huohuo, seele])
        _use_skill(huohuo, state, 'skill')
        assert huohuo.extra.get('huohuo_ruming_turns') == 3
        assert seele.extra.get('huohuo_ruming_turns') is None
        assert huohuo.extra.get('huohuo_ruming_cleanse') == 6

    def test_ruming_heals_at_turn_start(self):
        """v6.10.6: 藿藿持禳命时, 我方目标回合开始回血; 藿藿回合开始递减"""
        from engine.core.combat_engine import _begin_regular_turn
        from engine.runtime import _set_av
        huohuo = _unit('huohuo')
        seele = _unit('seele', position=2)
        seele.current_hp = seele.max_hp * 0.5
        state = SimState(enemies=[_enemy()], units=[huohuo, seele])
        state.extra.update({'navs': {0: 100.0, 1: 200.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1, 1: 2}, 'stamp_counter': 2})
        state.current_av = 0.0
        _use_skill(huohuo, state, 'skill')
        assert huohuo.extra['huohuo_ruming_turns'] == 3
        # 战技把 seele 奶满, 重新压低血量以观测禳命回血
        seele.current_hp = seele.max_hp * 0.5
        hp0 = seele.current_hp
        # seele 常规回合开始 → 禳命回血（藿藿持有, 回合数不减——只有藿藿回合才递减）
        state.skill_points = 0
        state.current_av = 0.0
        _set_av(state, state.extra['navs'], 1, 0.0)  # 让 seele 先动
        _begin_regular_turn(state, seele)
        assert seele.current_hp > hp0  # 收到禳命治疗
        assert huohuo.extra['huohuo_ruming_turns'] == 3  # 队友回合不递减
