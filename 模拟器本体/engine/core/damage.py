"""伤害计算引擎 — 五大体系完整公式（2026版）

逐乘区结构：
  基础伤害 × 增伤区 × 防御区 × 抗性区 × 暴击区
  × 韧性减伤区 × 全局减伤区 × 易伤区 × 真实伤害倍率
"""
from dataclasses import dataclass
from engine.core.attributes import CombatStats
from engine.models.enemy import Enemy
from engine.constants import (
    ELATION_BASE_DAMAGE, BREAK_BASE_DAMAGE, SUPER_BREAK_COEFFICIENT,
    TOUGHNESS_UNBROKEN_MULT, BREAK_ELEMENT_MULTIPLIERS,
)


@dataclass
class DamageBreakdown:
    """伤害各乘区明细"""
    base_damage: float = 0.0          # 基础伤害
    dmg_bonus_mult: float = 1.0       # 增伤区（直伤/DOT/欢愉度）
    def_mult: float = 1.0             # 防御区
    res_mult: float = 1.0             # 抗性区
    crit_mult: float = 1.0            # 双暴区
    toughness_mult: float = 1.0       # 韧性减伤区（未击破0.9，已击破1.0）
    dmg_reduction_mult: float = 1.0   # 全局减伤区 ∏(1 - 每条减伤%)
    vulnerability_mult: float = 1.0   # 易伤区
    true_dmg_mult: float = 1.0        # 真实伤害倍率
    elation_mult: float = 1.0         # 笑点/好活乘区（仅欢愉伤害）
    final_damage: float = 0.0         # 最终伤害

    def compute_final(self):
        self.final_damage = (
            self.base_damage
            * self.dmg_bonus_mult
            * self.def_mult
            * self.res_mult
            * self.crit_mult
            * self.toughness_mult
            * self.dmg_reduction_mult
            * self.vulnerability_mult
            * self.true_dmg_mult
            * self.elation_mult
        )

    def to_dict(self) -> dict:
        return {
            "base_damage": round(self.base_damage, 2),
            "dmg_bonus_mult": round(self.dmg_bonus_mult, 4),
            "def_mult": round(self.def_mult, 4),
            "res_mult": round(self.res_mult, 4),
            "crit_mult": round(self.crit_mult, 4),
            "toughness_mult": round(self.toughness_mult, 4),
            "dmg_reduction_mult": round(self.dmg_reduction_mult, 4),
            "vulnerability_mult": round(self.vulnerability_mult, 4),
            "true_dmg_mult": round(self.true_dmg_mult, 4),
            "elation_mult": round(self.elation_mult, 4),
            "final_damage": round(self.final_damage, 2),
            "total_multiplier": round(
                self.dmg_bonus_mult * self.def_mult * self.res_mult *
                self.crit_mult * self.toughness_mult *
                self.dmg_reduction_mult * self.vulnerability_mult *
                self.true_dmg_mult * self.elation_mult, 4
            ),
        }


# ===== 公共乘区辅助函数 =====

def _crit_dmg_value(stats: CombatStats, attack_type: str = None) -> float:
    """暴击伤害倍率（含攻击类别作用域, v5.6: 如都蓝王朝 5层FUA暴伤+25%）"""
    if attack_type:
        return stats.CRIT_DMG + stats.CRIT_DMG_BY_ATTACK_TYPE.get(attack_type, 0)
    return stats.CRIT_DMG


def _calc_def_mult(enemy: Enemy, stats: CombatStats, attacker_level: int,
                   damage_type: str = None, attack_type: str = None,
                   skill_type: str = None) -> float:
    """防御乘区: (200+10Lv) / (200+10Lv + DEF×(1 - min(减防%+无视防御%, 100%)))
    80级简化: 1000/(1000+有效防御)
    damage_type: 伤害类型作用域（如 "break"），None=全局
    attack_type: 攻击类别作用域（如 "follow_up"），None=全局
    skill_type: 技能类型作用域（如 "ultimate"），None=全局
    """
    total_def_reduction = min(stats.get_total_def_reduction(damage_type, attack_type, skill_type), 1.0)
    # 敌方侧减防 debuff（v5.3: 忘归人狐祈 DEF-18% / 灵砂E1 击破期间 DEF-20%）
    total_def_reduction = min(total_def_reduction + enemy.status_attribute('def_reduction'), 1.0)
    effective_def = enemy.DEF * (1.0 - total_def_reduction)
    base_value = 200.0 + 10.0 * attacker_level
    return base_value / (base_value + effective_def)


def _vuln_mult(enemy: Enemy, stats: CombatStats, damage_type: str = None) -> float:
    """易伤乘区: 1 + 敌方易伤 + 通用易伤 + 类型易伤"""
    total = enemy.vulnerability + stats.VULNERABILITY_APPLIED
    if damage_type:
        total += stats.VULNERABILITY_APPLIED_BY_TYPE.get(damage_type, 0)
        # 敌方侧类型易伤（v5.3: 灵砂【醇醉】受击破伤害提高25%）
        # 击破/超击破共用 vulnerability_break 键（醇醉对两者都生效）
        key = 'vulnerability_break' if damage_type in ('break', 'super_break') \
            else f'vulnerability_{damage_type}'
        total += enemy.status_attribute(key)
    return 1.0 + total


def _calc_res_mult(enemy: Enemy, element: str, stats: CombatStats) -> float:
    """抗性区: max(1 - 抗性 + 穿透, 0.1)
    抗性可被穿成负数(下限-100%→乘区=2.0), 正抗性有0.1下限保护
    """
    effective_res = max(enemy.get_res(element) - stats.get_effective_res_pen(element), -1.0)
    return max(1.0 - effective_res, 0.1)


def _calc_global_dr_mult(enemy: Enemy, stats: CombatStats = None,
                          extra_reductions: list[float] = None) -> float:
    """全局减伤区: (1-敌方减伤) × ∏(1-额外减伤) × (1-角色减伤)，下限0.01"""
    result = 1.0 - enemy.dmg_reduction
    if extra_reductions:
        for dr in extra_reductions:
            result *= (1.0 - dr)
    if stats and stats.DMG_REDUCTION > 0:
        result *= (1.0 - stats.DMG_REDUCTION)
    return max(result, 0.01)


def _calc_toughness_mult(enemy: Enemy, skip_toughness: bool = False) -> float:
    """韧性未击破减伤：未击破0.9，已击破1.0。击破/超击破可跳过"""
    if skip_toughness:
        return 1.0
    return 1.0 if enemy.is_broken else TOUGHNESS_UNBROKEN_MULT


# ===== 五大伤害体系 =====

def calc_direct_damage(
    stats: CombatStats,
    enemy: Enemy,
    scaling_stat_value: float,
    multiplier: float,
    element: str,
    attacker_level: int,
    is_crit: bool = True,
    true_dmg_ratio: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
    expected_crit: float = None,
) -> DamageBreakdown:
    """
    体系1：常规属性直伤
    基础面板 × 倍率% × 属性增伤区 × 双暴 × 易伤 × 防御 × 抗性 × 韧性减伤 × 全局减伤 × 真伤
    """
    bd = DamageBreakdown()
    bd.base_damage = scaling_stat_value * (multiplier / 100.0)
    bd.dmg_bonus_mult = stats.get_effective_dmg_bonus(element, attack_type, skill_type)
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="direct",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = _calc_res_mult(enemy, element, stats)
    # v5.2 问题4: expected_crit 非 None 时用期望暴击倍率 1+CR×CD（不再二元判定）
    # v5.6: 暴伤含攻击类别作用域（如都蓝王朝 FUA 暴伤）
    bd.crit_mult = expected_crit if expected_crit is not None else (
        (1.0 + _crit_dmg_value(stats, attack_type)) if is_crit else 1.0)
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=False)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "direct")
    bd.true_dmg_mult = 1.0 + true_dmg_ratio
    bd.compute_final()
    return bd


def calc_dot_damage(
    stats: CombatStats,
    enemy: Enemy,
    scaling_stat_value: float,
    multiplier: float,
    element: str,
    attacker_level: int,
    true_dmg_ratio: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
) -> DamageBreakdown:
    """
    体系2：DOT持续伤害
    ATK/HP × 倍率% × (1+DOT增伤%+全增伤%) × 易伤 × 防御 × 抗性 × 韧性减伤 × 全局减伤 × 真伤
    - 不吃暴击爆伤
    - 不吃属性伤害加成（用DOT增伤替代）
    """
    bd = DamageBreakdown()
    bd.base_damage = scaling_stat_value * (multiplier / 100.0)
    bd.dmg_bonus_mult = stats.get_effective_dot_bonus(attack_type)  # DOT增伤 + 全增伤，不含元素增伤
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="dot",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = _calc_res_mult(enemy, element, stats)
    bd.crit_mult = 1.0
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=False)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "dot")
    bd.true_dmg_mult = 1.0 + true_dmg_ratio
    bd.compute_final()
    return bd


def calc_break_damage(
    stats: CombatStats,
    enemy: Enemy,
    element: str,
    attacker_level: int,
    true_dmg_ratio: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
) -> DamageBreakdown:
    """
    体系3：弱点击破伤害
    3767.55 × (1+击破特攻%) × 元素倍率 × (韧性+20)/40
      × 易伤 × 防御 × 抗性 × 全局减伤 × 真伤
    - 不吃攻击面板/属性增伤/双暴
    - 触发时怪物已击破，跳过韧性减伤
    """
    bd = DamageBreakdown()
    element_mult = BREAK_ELEMENT_MULTIPLIERS.get(element, 1.0)
    toughness_factor = (enemy.max_toughness + 20.0) / 40.0
    bd.base_damage = (
        BREAK_BASE_DAMAGE * (1.0 + stats.BREAK_EFFECT)
        * element_mult * toughness_factor
    )
    bd.dmg_bonus_mult = 1.0
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="break",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = _calc_res_mult(enemy, element, stats)
    bd.crit_mult = 1.0
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=True)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "break")
    bd.true_dmg_mult = 1.0 + true_dmg_ratio
    bd.compute_final()
    return bd


def calc_super_break_damage(
    stats: CombatStats,
    enemy: Enemy,
    element: str,
    attacker_level: int,
    true_dmg_ratio: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
    toughness_dmg: float = 0.0,
) -> DamageBreakdown:
    """
    体系4：超击破伤害
    实机公式（v5.0 P7 修正, docs/specs 记录）: 削韧值 × (1+击破特攻%)
      × 易伤 × 防御 × 抗性 × 全局减伤 × 真伤
    - toughness_dmg>0 时用削韧值（实机语义）；=0 回退旧公式（BREAK_BASE×1.5, 兼容旧调用）
    - 触发时怪物已击破，跳过韧性减伤
    - 不吃攻击面板/属性增伤/双暴
    """
    bd = DamageBreakdown()
    if toughness_dmg > 0:
        bd.base_damage = toughness_dmg * (1.0 + stats.BREAK_EFFECT)
    else:
        bd.base_damage = (
            BREAK_BASE_DAMAGE * SUPER_BREAK_COEFFICIENT
            * (1.0 + stats.BREAK_EFFECT)
        )
    bd.dmg_bonus_mult = 1.0
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="super_break",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = _calc_res_mult(enemy, element, stats)
    bd.crit_mult = 1.0
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=True)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "super_break")
    bd.true_dmg_mult = 1.0 + true_dmg_ratio
    bd.compute_final()
    return bd


def calc_elation_damage(
    stats: CombatStats,
    enemy: Enemy,
    multiplier: float,
    element: str,
    attacker_level: int,
    is_crit: bool = True,
    laugh_n: float = 0.0,
    true_dmg_ratio: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
    expected_crit: float = None,
) -> DamageBreakdown:
    """
    体系5：欢愉伤害
    7535.107 × 倍率% × (1+欢愉度%) × (1+增笑%) × [1+5N/(N+240)]
      × 双暴 × 易伤 × 防御 × 抗性 × 韧性减伤 × 全局减伤 × 真伤
    """
    bd = DamageBreakdown()
    bd.base_damage = ELATION_BASE_DAMAGE * (multiplier / 100.0)
    bd.dmg_bonus_mult = (1.0 + stats.ELATION_LEVEL) * (1.0 + stats.LAUGH_BOOST)
    bd.elation_mult = (
        1.0 + (5.0 * laugh_n) / (laugh_n + 240.0) if laugh_n > 0 else 1.0
    )
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="elation",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = _calc_res_mult(enemy, element, stats)
    # v5.2 问题4: 期望暴击倍率（同 direct）; v5.6: 暴伤含攻击类别作用域
    bd.crit_mult = expected_crit if expected_crit is not None else (
        (1.0 + _crit_dmg_value(stats, attack_type)) if is_crit else 1.0)
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=False)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "elation")
    bd.true_dmg_mult = 1.0 + true_dmg_ratio
    bd.compute_final()
    return bd


def calc_additional_damage(
    stats: CombatStats,
    enemy: Enemy,
    scaling_stat_value: float,
    multiplier: float,
    attacker_level: int,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
) -> DamageBreakdown:
    """附加伤害：ATK × 倍率% × 防御 × 抗性 × 韧性减伤 × 全局减伤 × 易伤。不暴击、不吃增伤"""
    bd = DamageBreakdown()
    bd.base_damage = scaling_stat_value * (multiplier / 100.0)
    bd.dmg_bonus_mult = 1.0
    bd.def_mult = _calc_def_mult(enemy, stats, attacker_level, damage_type="additional",
                                 attack_type=attack_type, skill_type=skill_type)
    bd.res_mult = 1.0  # 附加伤害不受抗性影响
    bd.crit_mult = 1.0
    bd.toughness_mult = _calc_toughness_mult(enemy, skip_toughness=False)
    bd.dmg_reduction_mult = _calc_global_dr_mult(enemy, stats, extra_dmg_reductions)
    bd.vulnerability_mult = _vuln_mult(enemy, stats, "additional")
    bd.true_dmg_mult = 1.0
    bd.compute_final()
    return bd


# ===== 统一分发入口 =====

def calculate_damage(
    stats: CombatStats,
    enemy: Enemy,
    scaling_stat_value: float,
    multiplier: float,
    damage_type: str,
    element: str,
    attacker_level: int = 80,
    is_crit: bool = True,
    true_dmg_ratio: float = 0.0,
    laugh_n: float = 0.0,
    extra_dmg_reductions: list[float] = None,
    attack_type: str = None,
    skill_type: str = None,
    break_base: float = 0.0,
    element_coefficient: float = 1.0,
    toughness_dmg: float = 0.0,
    crit_mode: str = "boolean",
) -> DamageBreakdown:
    """统一伤害计算入口

    crit_mode: "boolean"=旧二元判定(is_crit); "expected"=期望暴击倍率 1+min(CR,1)×CD
    （v5.2 问题4: 战斗路径统一用 expected, 消除 CR 50% 阈值断层, 与优化器口径一致）
    """
    def _finish(bd: DamageBreakdown) -> DamageBreakdown:
        # 原伤害倍率必须在所有常规乘区结算后独立消费，不能并入 DMG_BONUS。
        multiplier = max(float(getattr(stats, "DAMAGE_MULTIPLIER", 1.0)), 0.0)
        if multiplier != 1.0:
            bd.final_damage *= multiplier
        return bd

    if damage_type == "direct":
        return _finish(calc_direct_damage(
            stats, enemy, scaling_stat_value, multiplier,
            element, attacker_level, is_crit, true_dmg_ratio, extra_dmg_reductions,
            attack_type, skill_type,
            expected_crit=(1.0 + min(stats.CRIT_RATE, 1.0) * _crit_dmg_value(stats, attack_type))
            if crit_mode == "expected" else None,
        ))
    elif damage_type == "dot":
        return _finish(calc_dot_damage(
            stats, enemy, scaling_stat_value, multiplier,
            element, attacker_level, true_dmg_ratio, extra_dmg_reductions,
            attack_type, skill_type,
        ))
    elif damage_type == "break":
        return _finish(calc_break_damage(
            stats, enemy, element, attacker_level,
            true_dmg_ratio, extra_dmg_reductions, attack_type, skill_type,
        ))
    elif damage_type == "super_break":
        return _finish(calc_super_break_damage(
            stats, enemy, element, attacker_level,
            true_dmg_ratio, extra_dmg_reductions, attack_type, skill_type,
            toughness_dmg,
        ))
    elif damage_type == "additional":
        return _finish(calc_additional_damage(
            stats, enemy, scaling_stat_value, multiplier,
            attacker_level, extra_dmg_reductions, attack_type, skill_type,
        ))
    elif damage_type == "true_damage":
        bd = DamageBreakdown()
        bd.base_damage = scaling_stat_value * (multiplier / 100.0)
        bd.true_dmg_mult = 1.0 + true_dmg_ratio
        bd.final_damage = bd.base_damage * bd.true_dmg_mult
        return _finish(bd)
    elif damage_type == "elation":
        return _finish(calc_elation_damage(
            stats, enemy, multiplier, element, attacker_level,
            is_crit, laugh_n, true_dmg_ratio, extra_dmg_reductions,
            attack_type, skill_type,
            expected_crit=(1.0 + min(stats.CRIT_RATE, 1.0) * _crit_dmg_value(stats, attack_type))
            if crit_mode == "expected" else None,
        ))
    else:
        return _finish(calc_direct_damage(
            stats, enemy, scaling_stat_value, multiplier,
            element, attacker_level, is_crit, true_dmg_ratio, extra_dmg_reductions,
        ))
