"""伤害计算引擎测试 — 五大体系公式（2026版）"""
import pytest
from engine.core.damage import (
    calculate_damage, calc_direct_damage, calc_dot_damage,
    calc_break_damage, calc_super_break_damage, calc_additional_damage,
)
from engine.core.attributes import CombatStats
from engine.models.enemy import Enemy
from engine.constants import (
    BREAK_BASE_DAMAGE, SUPER_BREAK_COEFFICIENT,
    TOUGHNESS_UNBROKEN_MULT,
)


def _make_stats(atk=2000, cr=0.70, cd=1.40, element_bonus=0.388):
    stats = CombatStats()
    stats.ATK = atk
    stats.CRIT_RATE = cr
    stats.CRIT_DMG = cd
    stats.DMG_BONUS["冰"] = element_bonus
    return stats


def _make_enemy(def_val=1000, res_ice=0.0, dmg_red=0.0, vuln=0.0, is_broken=False):
    return Enemy(
        id="test", name="test", level=80, DEF=def_val,
        dmg_reduction=dmg_red, vulnerability=vuln, is_broken=is_broken,
        toughness=100, max_toughness=100,
        element_res={"物理": 0, "火": 0, "冰": res_ice, "雷": 0, "风": 0, "量子": 0, "虚数": 0},
    )


def _def_expected(def_val, attacker_lv=80, reduction=0.0):
    base = 200 + 10 * attacker_lv  # = 1000 at Lv80
    return base / (base + def_val * (1 - min(reduction, 1.0)))


class TestDirectDamage:
    def test_basic_crit(self):
        """直伤暴击：1000/(1000+1000)=0.5防御区 + 0.9韧性减伤"""
        stats = _make_stats(atk=2000, cr=1.0, cd=1.0)
        enemy = _make_enemy(def_val=1000)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=2000,
            multiplier=200.0, element="冰", attacker_level=80, is_crit=True,
        )
        assert result.base_damage == 4000.0
        assert abs(result.dmg_bonus_mult - 1.388) < 0.01
        assert abs(result.def_mult - _def_expected(1000)) < 0.01
        assert result.res_mult == 1.0
        assert result.crit_mult == 2.0
        assert result.toughness_mult == TOUGHNESS_UNBROKEN_MULT
        expected = 4000 * 1.388 * _def_expected(1000) * 2.0 * TOUGHNESS_UNBROKEN_MULT
        assert abs(result.final_damage - expected) < 1.0

    def test_broken_no_toughness_penalty(self):
        """已击破 → 韧性乘区=1.0"""
        stats = _make_stats(atk=1000, cr=1.0, cd=0.0, element_bonus=0.0)
        enemy = _make_enemy(def_val=1000, is_broken=True)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=True,
        )
        assert result.toughness_mult == 1.0

    def test_enemy_resistance(self):
        """40%冰抗 → 抗性区=0.6"""
        stats = _make_stats(atk=1000, cr=1.0, cd=0.0, element_bonus=0.0)
        enemy = _make_enemy(def_val=0, res_ice=0.40)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=True,
        )
        assert abs(result.res_mult - 0.60) < 0.01

    def test_res_floor_010(self):
        """抗性乘区下限0.1：90%抗 → max(1-0.9, 0.1)=0.1"""
        stats = _make_stats(atk=1000, cr=1.0, cd=0.0, element_bonus=0.0)
        enemy = _make_enemy(def_val=0, res_ice=0.90)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=True,
        )
        assert abs(result.res_mult - 0.10) < 0.01

    def test_def_reduction(self):
        """减防50% → DEF=1000*(1-0.5)=500 → 6600/7100"""
        stats = _make_stats(atk=1000, cr=1.0, cd=0.0, element_bonus=0.0)
        stats.DEF_REDUCTION = 0.50
        enemy = _make_enemy(def_val=1000)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=True,
        )
        assert abs(result.def_mult - _def_expected(1000, reduction=0.5)) < 0.01


class TestDoTDamage:
    def test_dot_uses_dot_bonus_not_elemental(self):
        """DOT增伤30%+全增伤10% → 1.40，元素增伤50%不生效"""
        stats = CombatStats()
        stats.ATK = 2000
        stats.DMG_BONUS_DOT = 0.30
        stats.DMG_BONUS_ALL = 0.10
        stats.DMG_BONUS["冰"] = 0.50  # 不应影响DOT
        enemy = _make_enemy(def_val=1000)
        result = calc_dot_damage(
            stats=stats, enemy=enemy, scaling_stat_value=2000,
            multiplier=50.0, element="冰", attacker_level=80,
        )
        assert result.crit_mult == 1.0
        assert abs(result.dmg_bonus_mult - 1.40) < 0.01
        expected = 1000 * 1.40 * _def_expected(1000) * TOUGHNESS_UNBROKEN_MULT
        assert abs(result.final_damage - expected) < 1.0


class TestBreakDamage:
    def test_break_fire(self):
        """击破(火): 3767.55×2×1.0×(100+20)/40"""
        stats = CombatStats()
        stats.BREAK_EFFECT = 1.00
        enemy = _make_enemy(def_val=1000)
        result = calc_break_damage(
            stats=stats, enemy=enemy, element="火", attacker_level=80,
        )
        tf = (100 + 20) / 40  # 3.0
        expected_base = BREAK_BASE_DAMAGE * 2.0 * 1.0 * tf
        assert abs(result.base_damage - expected_base) < 0.1
        assert result.crit_mult == 1.0
        assert result.toughness_mult == 1.0  # 击破时跳过
        assert abs(result.final_damage - expected_base * _def_expected(1000)) < 1.0

    def test_break_lightning_2x(self):
        """击破(雷): 元素倍率2.0"""
        stats = CombatStats()
        enemy = _make_enemy(def_val=0)
        result = calc_break_damage(
            stats=stats, enemy=enemy, element="雷", attacker_level=80,
        )
        tf = (100 + 20) / 40
        expected_base = BREAK_BASE_DAMAGE * 1.0 * 2.0 * tf
        assert abs(result.base_damage - expected_base) < 0.1


class TestSuperBreakDamage:
    def test_super_break_toughness_formula(self):
        """超击破（v5.0 P7 实机公式）: 削韧值 × (1+击破特攻%)"""
        stats = CombatStats()
        stats.BREAK_EFFECT = 1.50
        enemy = _make_enemy(def_val=1000)
        result = calc_super_break_damage(
            stats=stats, enemy=enemy, element="物理", attacker_level=80,
            toughness_dmg=100.0,
        )
        assert abs(result.base_damage - 100.0 * 2.50) < 0.1
        assert result.toughness_mult == 1.0

    def test_super_break_fallback_old_formula(self):
        """toughness_dmg=0 回退旧公式（兼容既有调用）"""
        stats = CombatStats()
        stats.BREAK_EFFECT = 1.50
        enemy = _make_enemy(def_val=1000)
        result = calc_super_break_damage(
            stats=stats, enemy=enemy, element="物理", attacker_level=80,
        )
        expected_base = BREAK_BASE_DAMAGE * SUPER_BREAK_COEFFICIENT * 2.50
        assert abs(result.base_damage - expected_base) < 0.1


class TestAdditionalDamage:
    def test_additional(self):
        stats = _make_stats(atk=2000, cr=1.0, cd=1.0, element_bonus=0.50)
        enemy = _make_enemy(def_val=1000)
        result = calc_additional_damage(
            stats=stats, enemy=enemy, scaling_stat_value=2000,
            multiplier=30.0, attacker_level=80,
        )
        assert result.crit_mult == 1.0
        assert result.dmg_bonus_mult == 1.0
        expected = 600 * _def_expected(1000) * TOUGHNESS_UNBROKEN_MULT
        assert abs(result.final_damage - expected) < 1.0


class TestGlobalDR:
    def test_floor_001(self):
        """全局减伤下限0.01"""
        stats = _make_stats(atk=1000, cr=0, cd=0, element_bonus=0)
        enemy = _make_enemy(def_val=0, dmg_red=0.99)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=False,
        )
        assert abs(result.dmg_reduction_mult - 0.01) < 0.0001

    def test_normal(self):
        """10%减伤 → 0.9"""
        stats = _make_stats(atk=1000, cr=0, cd=0, element_bonus=0)
        enemy = _make_enemy(def_val=0, dmg_red=0.10)
        result = calc_direct_damage(
            stats=stats, enemy=enemy, scaling_stat_value=1000,
            multiplier=100.0, element="冰", attacker_level=80, is_crit=False,
        )
        assert abs(result.dmg_reduction_mult - 0.90) < 0.01


class TestTrueDamage:
    def test_true_damage_only(self):
        result = calculate_damage(
            stats=_make_stats(), enemy=_make_enemy(),
            scaling_stat_value=1000, multiplier=30.0,
            damage_type="true_damage", element="物理",
        )
        assert result.final_damage == 300.0
