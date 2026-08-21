"""欢愉机制测试 — 伤害公式、笑点乘区、Aha速度、战斗状态"""
import pytest
from engine.core.damage import calc_elation_damage, calculate_damage, DamageBreakdown
from engine.core.attributes import CombatStats
from engine.models.enemy import Enemy
from engine.models.elation import (
    ElationBattleState, GoodShowInstance,
    calc_aha_speed, calc_laugh_multiplier,
)
from engine.constants import ELATION_BASE_DAMAGE


def _make_stats(elation_level=0.0, laugh_boost=0.0, cr=0.70, cd=1.40):
    stats = CombatStats()
    stats.ELATION_LEVEL = elation_level
    stats.LAUGH_BOOST = laugh_boost
    stats.CRIT_RATE = cr
    stats.CRIT_DMG = cd
    return stats


def _make_enemy(def_val=1000, res_val=0.0, dmg_red=0.0, vuln=0.0, is_broken=False):
    return Enemy(
        id="test", name="test", level=80, DEF=def_val,
        dmg_reduction=dmg_red, vulnerability=vuln, is_broken=is_broken,
        toughness=100, max_toughness=100,
        element_res={"物理": res_val, "火": 0, "冰": 0, "雷": 0, "风": 0, "量子": 0, "虚数": 0},
    )


class TestElationDamage:
    """欢愉伤害公式验证"""

    def test_basic_zero_stats(self):
        """无加成、无笑点、非暴击：伤害 = 7535.107 × 倍率（已击破，跳过韧性减伤）"""
        stats = _make_stats()
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=0,
        )
        expected = ELATION_BASE_DAMAGE * 1.0
        assert abs(result.final_damage - expected) < 0.1
        assert result.dmg_bonus_mult == 1.0
        assert result.elation_mult == 1.0

    def test_with_elation_level(self):
        """欢愉度 50%，无其他加成"""
        stats = _make_stats(elation_level=0.50)
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=0,
        )
        expected = ELATION_BASE_DAMAGE * 1.5
        assert abs(result.final_damage - expected) < 0.1

    def test_with_laugh_boost(self):
        """增笑 25%"""
        stats = _make_stats(elation_level=0.0, laugh_boost=0.25)
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=0,
        )
        expected = ELATION_BASE_DAMAGE * 1.25
        assert abs(result.final_damage - expected) < 0.1

    def test_elation_plus_boost(self):
        """欢愉度 50% + 增笑 25% = 1.875"""
        stats = _make_stats(elation_level=0.50, laugh_boost=0.25)
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=0,
        )
        expected = ELATION_BASE_DAMAGE * 1.5 * 1.25
        assert abs(result.final_damage - expected) < 0.1

    def test_laugh_n_multiplier(self):
        """笑点 N=5 → 1+25/245=1.1020"""
        stats = _make_stats()
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=5,
        )
        expected_mult = 1.0 + 25.0 / 245.0
        expected = ELATION_BASE_DAMAGE * expected_mult
        assert abs(result.elation_mult - expected_mult) < 0.001
        assert abs(result.final_damage - expected) < 0.1

    def test_laugh_n_100(self):
        """笑点 N=100 → 1+500/340=2.4706"""
        expected_mult = 1.0 + 500.0 / 340.0
        assert abs(expected_mult - 2.4706) < 0.001

    def test_with_crit(self):
        """欢愉暴击：CD=140% → crit_mult=2.4"""
        stats = _make_stats(elation_level=0.50, cd=1.40)
        enemy = _make_enemy(def_val=0, is_broken=True)

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=True, laugh_n=0,
        )
        assert result.crit_mult == 2.4
        expected = ELATION_BASE_DAMAGE * 1.5 * 2.4
        assert abs(result.final_damage - expected) < 0.1

    def test_defense_and_resistance(self):
        """防御区+抗性区影响欢愉伤害"""
        stats = _make_stats(elation_level=0.0)
        enemy = _make_enemy(def_val=1000, res_val=0.20)  # 20%物抗

        result = calc_elation_damage(
            stats=stats, enemy=enemy, multiplier=100.0,
            element="物理", attacker_level=80, is_crit=False, laugh_n=0,
        )
        # 防御区: 1000/(1000+1000)=0.5
        expected_def = 1000.0 / 2000.0
        assert abs(result.def_mult - expected_def) < 0.01
        # 抗性区: 1 - 0.20 = 0.8
        assert abs(result.res_mult - 0.8) < 0.01
        expected = ELATION_BASE_DAMAGE * expected_def * 0.8 * 0.9  # 0.9 = toughness unbroken
        assert abs(result.final_damage - expected) < 0.1

    def test_full_pipeline_via_unified(self):
        """通过统一入口 calculate_damage 计算欢愉伤害"""
        stats = _make_stats(elation_level=0.50, cd=1.40)
        enemy = _make_enemy(def_val=1000)

        result = calculate_damage(
            stats=stats, enemy=enemy,
            scaling_stat_value=0,  # 欢愉伤害不使用此参数
            multiplier=200.0,
            damage_type="elation", element="物理",
            attacker_level=80, is_crit=True, laugh_n=10,
        )
        # 基础 = 7535.107 * 2.0 = 15070.214
        assert abs(result.base_damage - 15070.214) < 0.1
        # 欢愉度+增笑: (1+0.5)*(1+0) = 1.5
        assert abs(result.dmg_bonus_mult - 1.5) < 0.01
        # 笑点: 1+50/250 = 1.2
        assert abs(result.elation_mult - 1.2) < 0.01
        # 防御: 1000/(1000+1000)=0.5
        expected_def = 1000.0 / 2000.0
        assert abs(result.def_mult - expected_def) < 0.01
        # 暴击: 2.4
        assert result.crit_mult == 2.4
        # 韧性未击破: 0.9
        assert abs(result.toughness_mult - 0.9) < 0.01
        expected = 15070.214 * 1.5 * 1.2 * expected_def * 2.4 * 0.9
        assert abs(result.final_damage - expected) < 1.0


class TestLaughMultiplier:
    """笑点/好活乘区曲线测试"""

    def test_n0(self):
        assert calc_laugh_multiplier(0) == 1.0

    def test_n1(self):
        m = calc_laugh_multiplier(1)
        expected = 1.0 + 5.0 / 241.0
        assert abs(m - expected) < 0.0001

    def test_n_10(self):
        m = calc_laugh_multiplier(10)
        expected = 1.0 + 50.0 / 250.0  # 1.2
        assert abs(m - 1.2) < 0.0001

    def test_n_240(self):
        """N=240 → 1 + 1200/480 = 3.5"""
        m = calc_laugh_multiplier(240)
        assert abs(m - 3.5) < 0.0001

    def test_n_approaches_infinity(self):
        """N极大时趋近 1+5=6"""
        m = calc_laugh_multiplier(1_000_000)
        assert abs(m - 6.0) < 0.01

    def test_negative_n(self):
        """负数视为0"""
        assert calc_laugh_multiplier(-5) == 1.0


class TestAhaSpeed:
    """阿哈速度公式验证"""

    def test_four_characters(self):
        """x=205, y=162, z=128, w=100 → 80 + 41 + 16.2 + 6.4 + 2.0 = 145.6"""
        speed = calc_aha_speed([205, 162, 128, 100])
        expected = 80 + 205/5 + 162/10 + 128/20 + 100/50
        assert abs(speed - expected) < 0.01

    def test_three_characters(self):
        """x=205, y=162, z=128 → 80 + 41 + 16.2 + 6.4 + 0 = 143.6"""
        speed = calc_aha_speed([205, 162, 128])
        expected = 80 + 41.0 + 16.2 + 6.4
        assert abs(speed - expected) < 0.01

    def test_one_character(self):
        """x=160 → 80 + 32 = 112"""
        speed = calc_aha_speed([160])
        expected = 80 + 32.0
        assert abs(speed - expected) < 0.01

    def test_zero_characters(self):
        """无欢愉角色 → 80"""
        assert calc_aha_speed([]) == 80.0

    def test_any_order_input(self):
        """输入顺序不影响结果（自动降序）"""
        s1 = calc_aha_speed([100, 200, 150, 120])
        s2 = calc_aha_speed([200, 150, 120, 100])
        assert abs(s1 - s2) < 0.001


class TestElationBattleState:
    """战斗状态管理"""

    def test_laugh_points(self):
        state = ElationBattleState()
        state.add_laugh_points(3)
        assert state.laugh_points == 3.0
        consumed = state.consume_all_laugh_points()
        assert consumed == 3.0
        assert state.laugh_points == 0.0

    def test_good_show_grant_and_tick(self):
        state = ElationBattleState()
        state.grant_good_show("char_a", 5.0)
        assert state.get_good_show_total("char_a") == 5.0

        state.tick_all_good_shows()  # 1 remaining
        assert state.get_good_show_total("char_a") == 5.0
        state.tick_all_good_shows()  # 0 remaining, expired
        assert state.get_good_show_total("char_a") == 0.0

    def test_multiple_batches_independent_timers(self):
        """多批次独立倒计时，层数叠加，到期分批扣除"""
        state = ElationBattleState()
        state.grant_good_show("char_a", 5.0, duration=2)  # batch 1
        state.grant_good_show("char_a", 3.0, duration=3)  # batch 2 (longer)
        assert state.get_good_show_total("char_a") == 8.0

        state.tick_all_good_shows()  # batch1=1, batch2=2
        assert state.get_good_show_total("char_a") == 8.0
        state.tick_all_good_shows()  # batch1 expired, batch2=1
        assert state.get_good_show_total("char_a") == 3.0
        state.tick_all_good_shows()  # batch2 expired
        assert state.get_good_show_total("char_a") == 0.0

    def test_separate_characters_independent(self):
        """每个角色独立存储，互不共享"""
        state = ElationBattleState()
        state.grant_good_show("char_a", 5.0)
        state.grant_good_show("char_b", 3.0)
        assert state.get_good_show_total("char_a") == 5.0
        assert state.get_good_show_total("char_b") == 3.0

    def test_battle_start_init(self):
        """战斗开局：每个欢愉角色 20 层，2 回合"""
        state = ElationBattleState()
        state.battle_start_init(["a", "b", "c"])
        assert state.get_good_show_total("a") == 20.0
        assert state.get_good_show_total("b") == 20.0
        assert state.get_good_show_total("c") == 20.0

        # 非欢愉角色不受影响
        assert state.get_good_show_total("d") == 0.0

    def test_battle_start_custom_duration(self):
        """角色天赋可延长开局好活当赏至3回合"""
        state = ElationBattleState()
        state.battle_start_init(["a"], duration=3)
        state.tick_all_good_shows()  # 2 remaining
        state.tick_all_good_shows()  # 1 remaining
        assert state.get_good_show_total("a") == 20.0
        state.tick_all_good_shows()  # expired
        assert state.get_good_show_total("a") == 0.0

    def test_aha_moment_convert(self):
        """阿哈时刻：笑点 N→全队获 N 层好活当赏（1:1），笑点清零"""
        state = ElationBattleState()
        state.add_laugh_points(10)

        n = state.aha_moment_convert(["a", "b"])
        assert n == 10.0
        assert state.laugh_points == 0.0
        assert state.get_good_show_total("a") == 10.0
        assert state.get_good_show_total("b") == 10.0

    def test_aha_moment_zero_laugh(self):
        """无笑点时阿哈时刻不产生好活当赏"""
        state = ElationBattleState()
        n = state.aha_moment_convert(["a"])
        assert n == 0.0
        assert state.get_good_show_total("a") == 0.0
