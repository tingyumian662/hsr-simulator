"""属性汇总管线 — 从角色基础 + 装备 + Buff 计算最终战斗属性"""
from dataclasses import dataclass, field
from typing import Optional
from engine.constants import ELEMENT_DMG_STAT_TO_ELEMENT
from engine.models.character import Character
from engine.models.equipment import LightCone, RelicPiece, RelicSet


@dataclass
class CombatStats:
    """战斗中角色的最终属性（所有加成汇总后的结果）"""
    HP: float = 0.0
    ATK: float = 0.0
    DEF: float = 0.0
    SPD: float = 0.0
    CRIT_RATE: float = 0.05      # 默认5%基础暴击
    CRIT_DMG: float = 0.50       # 默认50%基础爆伤
    BREAK_EFFECT: float = 0.0
    EFFECT_HIT_RATE: float = 0.0
    EFFECT_RES: float = 0.0
    ENERGY_REGEN: float = 1.0    # 100% = 1.0
    DMG_BONUS: dict = field(default_factory=lambda: {
        "物理": 0.0, "火": 0.0, "冰": 0.0, "雷": 0.0,
        "风": 0.0, "量子": 0.0, "虚数": 0.0,
    })
    DMG_BONUS_ALL: float = 0.0
    RES_PEN: dict = field(default_factory=lambda: {
        "物理": 0.0, "火": 0.0, "冰": 0.0, "雷": 0.0,
        "风": 0.0, "量子": 0.0, "虚数": 0.0,
    })
    RES_PEN_ALL: float = 0.0
    DEF_PEN: float = 0.0
    DEF_REDUCTION: float = 0.0        # 减防%（降低敌方防御值）
    DMG_BONUS_DOT: float = 0.0        # 持续伤害增伤（独立于元素增伤）
    TOUGHNESS_EFFICIENCY: float = 1.0
    HEAL_BONUS: float = 0.0
    SHIELD_BONUS: float = 0.0
    DMG_REDUCTION: float = 0.0  # 受到伤害降低（小数，0.24=24%）
    # 独立于增伤区的原伤害倍率（例如那刻夏E6）；默认不改变现有角色。
    DAMAGE_MULTIPLIER: float = 1.0
    SPD_PERCENT: float = 0.0
    # 欢愉
    ELATION_LEVEL: float = 0.0    # 欢愉度（小数，0.5 = 50%）
    LAUGH_BOOST: float = 0.0      # 增笑（小数，默认0，仅星魂提供）
    VULNERABILITY_APPLIED: float = 0.0  # 对敌方施加的易伤%（与敌方自身易伤加算）
    VULNERABILITY_APPLIED_BY_TYPE: dict = field(default_factory=dict)  # {"elation": 0.10, "break": 0.08}
    DEF_PEN_BY_TYPE: dict = field(default_factory=dict)  # {"break": 0.20, "elation": 0.15}
    DEF_PEN_MEMSPRITE: float = 0.0        # 仅忆灵伤害无视防御（v5.4: 致长夜的星光等）
    DEF_PEN_BY_ATTACK_TYPE: dict = field(default_factory=dict)  # {"follow_up": 0.24}
    DEF_PEN_BY_SKILL_TYPE: dict = field(default_factory=dict)  # {"ultimate": 0.54}（v5.6: 流光·仅终结技无视防御）
    DMG_BONUS_BY_ATTACK_TYPE: dict = field(default_factory=dict)  # {"follow_up": 0.30}
    DMG_BONUS_BY_SKILL_TYPE: dict = field(default_factory=dict)  # {"skill": 0.54, "basic": 0.30, "ultimate": 0.20}
    CRIT_DMG_BY_ATTACK_TYPE: dict = field(default_factory=dict)  # {"follow_up": 0.25}（v5.6: 都蓝王朝5层FUA暴伤）
    # 内部：记录未乘百分比之前的白值，供后续百分比加成使用
    _base_HP: float = 0.0
    _base_ATK: float = 0.0
    _base_DEF: float = 0.0
    _base_SPD: float = 0.0

    def get_effective_dmg_bonus(self, element: str, attack_type: str = None,
                                 skill_type: str = None) -> float:
        """获取有效增伤乘区 = 1 + 全增伤 + 元素增伤 + 攻击类别增伤 + 技能类型增伤"""
        total = 1.0 + self.DMG_BONUS_ALL + self.DMG_BONUS.get(element, 0.0)
        if attack_type:
            total += self.DMG_BONUS_BY_ATTACK_TYPE.get(attack_type, 0)
        if skill_type:
            total += self.DMG_BONUS_BY_SKILL_TYPE.get(skill_type, 0)
        return total

    def get_effective_res_pen(self, element: str) -> float:
        """获取对特定元素的有效抗性穿透"""
        return self.RES_PEN_ALL + self.RES_PEN.get(element, 0.0)

    def get_effective_def_pen(self) -> float:
        """无视防御%"""
        return self.DEF_PEN

    def get_total_def_reduction(self, damage_type: str = None, attack_type: str = None,
                                skill_type: str = None) -> float:
        """减防% + 无视防御%（全局 + 按伤害类型 + 按攻击类别 + 按技能类型）"""
        total = self.DEF_REDUCTION + self.DEF_PEN
        if damage_type:
            total += self.DEF_PEN_BY_TYPE.get(damage_type, 0)
        if attack_type:
            total += self.DEF_PEN_BY_ATTACK_TYPE.get(attack_type, 0)
        if skill_type:
            total += self.DEF_PEN_BY_SKILL_TYPE.get(skill_type, 0)
        return total

    def get_effective_dot_bonus(self, attack_type: str = None) -> float:
        """DoT增伤 = 1 + DOT增伤% + 全增伤% + 攻击类别增伤（不含元素增伤）"""
        total = 1.0 + self.DMG_BONUS_DOT + self.DMG_BONUS_ALL
        if attack_type:
            total += self.DMG_BONUS_BY_ATTACK_TYPE.get(attack_type, 0)
        return total


class StatAccumulator:
    """属性累加器：管理百分比和固定值的分批累加，避免过早应用顺序问题"""

    def __init__(self):
        self.hp_pct = 0.0
        self.atk_pct = 0.0
        self.def_pct = 0.0
        self.hp_flat = 0.0
        self.atk_flat = 0.0
        self.def_flat = 0.0

    def add(self, stat_type: str, value: float, stats: CombatStats):
        """累加一个属性值。value 使用游戏内数值（如 50 表示 50%）"""
        # 百分比加成 — 先暂存，最后统一乘
        if stat_type == "HP_percent":
            self.hp_pct += value / 100.0
        elif stat_type == "ATK_percent":
            self.atk_pct += value / 100.0
        elif stat_type == "DEF_percent":
            self.def_pct += value / 100.0
        elif stat_type in ("SPD_percent", "SPD_PERCENT"):
            stats.SPD_PERCENT += value / 100.0
        # 固定值加成 — 先暂存
        elif stat_type == "HP_flat":
            self.hp_flat += value
        elif stat_type == "ATK_flat":
            self.atk_flat += value
        elif stat_type == "DEF_flat":
            self.def_flat += value
        # 其他属性直接写入 CombatStats
        else:
            _apply_stat_direct(stats, stat_type, value)


def _apply_stat_direct(stats: CombatStats, stat_type: str, value: float):
    # v5.4 基础速度白值（黎明恰如此燃烧/将光阴织成黄金: +12 加算至白值, 吃百分比加成）
    if stat_type == "_base_SPD":
        stats._base_SPD += value
        stats.SPD += value
        return
    """将属性值应用到 CombatStats（在百分比乘法之后调用）。
    value 使用游戏内百分比数值（如 12 表示 12%）。
    对于 ATK_percent/HP_percent/DEF_percent，以白值为基准追加乘法。"""
    # 百分比加成 — 以白值（基础属性）为基准追加
    if stat_type == "HP_percent":
        stats.HP += stats._base_HP * (value / 100.0)
    elif stat_type == "ATK_percent":
        stats.ATK += stats._base_ATK * (value / 100.0)
    elif stat_type == "DEF_percent":
        stats.DEF += stats._base_DEF * (value / 100.0)
    elif stat_type in ("SPD_percent", "SPD_PERCENT"):
        stats.SPD += stats._base_SPD * (value / 100.0)
    # 固定值加成 — 直接加
    elif stat_type == "HP_flat":
        stats.HP += value
    elif stat_type == "ATK_flat":
        stats.ATK += value
    elif stat_type == "DEF_flat":
        stats.DEF += value
    # 暴击 / 暴伤 / 击破 / 命中 / 抵抗 / 能量 / 治疗 / 护盾 — 直接加
    elif stat_type == "CRIT_RATE":
        stats.CRIT_RATE += value / 100.0
    elif stat_type == "CRIT_DMG":
        stats.CRIT_DMG += value / 100.0
    elif stat_type == "BREAK_EFFECT":
        stats.BREAK_EFFECT += value / 100.0
    elif stat_type == "EFFECT_HIT_RATE":
        stats.EFFECT_HIT_RATE += value / 100.0
    elif stat_type == "EFFECT_RES":
        stats.EFFECT_RES += value / 100.0
    elif stat_type == "ENERGY_REGEN":
        stats.ENERGY_REGEN += value / 100.0
    elif stat_type == "HEAL_BONUS":
        stats.HEAL_BONUS += value / 100.0
    elif stat_type == "SHIELD_BONUS":
        stats.SHIELD_BONUS += value / 100.0
    # 增伤（按技能类型: DMG_BONUS_SKILL/BASIC/ULTIMATE）
    elif stat_type in ("DMG_BONUS_SKILL", "DMG_BONUS_BASIC", "DMG_BONUS_ULTIMATE"):
        skill_key = stat_type[len("DMG_BONUS_"):].lower()  # "skill"/"basic"/"ultimate"
        stats.DMG_BONUS_BY_SKILL_TYPE[skill_key] = stats.DMG_BONUS_BY_SKILL_TYPE.get(skill_key, 0) + value / 100.0
    # 增伤（按攻击类别）
    elif stat_type.startswith("DMG_BONUS_ATK_"):
        atk_type = stat_type[len("DMG_BONUS_ATK_"):].lower()
        stats.DMG_BONUS_BY_ATTACK_TYPE[atk_type] = stats.DMG_BONUS_BY_ATTACK_TYPE.get(atk_type, 0) + value / 100.0
    # 暴击伤害（按攻击类别, v5.6: 都蓝王朝 5层FUA暴伤+25%）
    elif stat_type.startswith("CRIT_DMG_ATK_"):
        atk_type = stat_type[len("CRIT_DMG_ATK_"):].lower()
        stats.CRIT_DMG_BY_ATTACK_TYPE[atk_type] = stats.CRIT_DMG_BY_ATTACK_TYPE.get(atk_type, 0) + value / 100.0
    elif stat_type == "DMG_BONUS_ALL":
        stats.DMG_BONUS_ALL += value / 100.0
    elif stat_type in ELEMENT_DMG_STAT_TO_ELEMENT:
        element = ELEMENT_DMG_STAT_TO_ELEMENT[stat_type].value
        stats.DMG_BONUS[element] += value / 100.0
    # 穿透
    elif stat_type == "RES_PEN_ALL":
        stats.RES_PEN_ALL += value / 100.0
    elif stat_type == "DEF_PEN":
        stats.DEF_PEN += value / 100.0
    elif stat_type == "DEF_REDUCTION":
        stats.DEF_REDUCTION += value / 100.0
    elif stat_type == "DMG_BONUS_DOT":
        stats.DMG_BONUS_DOT += value / 100.0
    # 削韧
    elif stat_type == "DMG_REDUCTION":
        stats.DMG_REDUCTION += value / 100.0
    elif stat_type == "TOUGHNESS_EFFICIENCY":
        stats.TOUGHNESS_EFFICIENCY += value / 100.0
    # 欢愉
    elif stat_type == "ELATION_LEVEL":
        stats.ELATION_LEVEL += value / 100.0
    elif stat_type == "LAUGH_BOOST":
        stats.LAUGH_BOOST += value / 100.0
    # 易伤
    elif stat_type == "VULNERABILITY_APPLIED":
        stats.VULNERABILITY_APPLIED += value / 100.0
    elif stat_type.startswith("VULNERABILITY_APPLIED_"):
        dmg_type = stat_type[len("VULNERABILITY_APPLIED_"):].lower()
        stats.VULNERABILITY_APPLIED_BY_TYPE[dmg_type] = stats.VULNERABILITY_APPLIED_BY_TYPE.get(dmg_type, 0) + value / 100.0
    # 仅忆灵伤害无视防御（v5.4）
    elif stat_type == "DEF_PEN_MEMSPRITE":
        stats.DEF_PEN_MEMSPRITE += value / 100.0
    # 按伤害类型的无视防御 (DEF_PEN_{DAMAGE_TYPE})
    elif stat_type.startswith("DEF_PEN_ATK_"):
        atk_type = stat_type[len("DEF_PEN_ATK_"):].lower()
        stats.DEF_PEN_BY_ATTACK_TYPE[atk_type] = stats.DEF_PEN_BY_ATTACK_TYPE.get(atk_type, 0) + value / 100.0
    # 按技能类型的无视防御 (DEF_PEN_SKILL_{SKILL_TYPE}, v5.6: 流光·仅终结技无视防御)
    # 须在通用 DEF_PEN_ 前缀分支之前
    elif stat_type.startswith("DEF_PEN_SKILL_"):
        skill_key = stat_type[len("DEF_PEN_SKILL_"):].lower()
        stats.DEF_PEN_BY_SKILL_TYPE[skill_key] = stats.DEF_PEN_BY_SKILL_TYPE.get(skill_key, 0) + value / 100.0
    elif stat_type.startswith("DEF_PEN_"):
        dmg_type = stat_type[len("DEF_PEN_"):].lower()
        stats.DEF_PEN_BY_TYPE[dmg_type] = stats.DEF_PEN_BY_TYPE.get(dmg_type, 0) + value / 100.0


def _eval_relic_condition(stats: CombatStats, condition: str,
                         base_attrs: dict, character) -> None:
    """评测遗器条件，将条件触发的额外属性注入 CombatStats。

    仅处理入场前一次性判断（B类）。战斗中动态条件（C/D/E/F/G/H类）
    由 engine/core/relic_conditions.py 通过 HookRegistry 处理。
    """
    if not condition:
        return

    _eff_spd = stats.SPD * (1.0 + stats.SPD_PERCENT)

    # ── B类：入场一次性判断 ──

    if condition == "spd_cr_threshold_and_first_elation":
        # 应天涉远的卜者 2pc: SPD≥120→CR+10%, ≥160→CR+18%
        if _eff_spd >= 160:
            stats.CRIT_RATE += 0.18
        elif _eff_spd >= 120:
            stats.CRIT_RATE += 0.10

    elif condition == "effect_res_30_team_cd":
        # 折断的龙骨 2pc: 效果抵抗≥30%→全队暴伤+10%
        if stats.EFFECT_RES >= 0.30:
            stats.CRIT_DMG += 0.10

    elif condition == "low_spd_cr_boost":
        # 哀歌覆国的诗人 4pc: SPD<110→CR+20%, <95→CR+32%
        if _eff_spd < 95:
            stats.CRIT_RATE += 0.32
        elif _eff_spd < 110:
            stats.CRIT_RATE += 0.20

    elif condition == "spd_threshold_120_atk":
        # 太空封印站 2pc: SPD≥120→ATK+12%
        if _eff_spd >= 120:
            stats.ATK += stats._base_ATK * 0.12

    elif condition == "spd_threshold_120_team_atk":
        # 不老者的仙舟 2pc: SPD≥120→全队ATK+8% (此处仅算自身)
        if _eff_spd >= 120:
            stats.ATK += stats._base_ATK * 0.08

    elif condition == "ehr_to_atk_capped":
        # 泛银河商业公司 2pc: ATK=25%×EHR, 上限25%
        bonus = min(stats.EFFECT_HIT_RATE * 0.25, 0.25)
        stats.ATK += stats._base_ATK * bonus

    elif condition == "ehr_threshold_50_def":
        # 筑城者的贝洛伯格 2pc: EHR≥50%→DEF+15%
        if stats.EFFECT_HIT_RATE >= 0.50:
            stats.DEF += stats._base_DEF * 0.15

    elif condition == "cd_threshold_first_atk_cr":
        # 星体差分机 2pc: CD≥120%→开局首击CR+60%
        if stats.CRIT_DMG >= 1.20:
            stats.CRIT_RATE = min(1.0, stats.CRIT_RATE + 0.60)

    elif condition == "enter_combat_energy_to_dmg":
        # 寰宇生研院 2pc: 能量≥200→每超出1点+0.2%DMG,上限32%
        max_energy = character.max_energy if hasattr(character, 'max_energy') else 0
        if max_energy >= 200:
            excess = max_energy - 200
            bonus = min(excess * 0.002, 0.32)
            stats.DMG_BONUS_ALL += bonus

    elif condition == "enter_combat_faction_cd":
        # 坠星启航地 2pc: 有同阵营队友→CD+32% (队伍上下文在 _apply_team_static_relics 处理)
        pass

    elif condition == "defpen_vs_quantum":
        # 繁星璀璨的天才 4pc: 基础无视10%防御; 量子弱点额外10%由 _apply_target_relic_modifiers 按目标判定
        stats.DEF_PEN += 0.10

    # NOTE: elation_level_40_80_cd 已移到 relic_conditions.py 动态处理
    # NOTE: ult_action_advance_25 / elation_laugh_def_pen_stack 在运行时动态处理
    # NOTE: 其余条件码由 HookRegistry 动态触发


def compute_combat_stats(
    character: Character,
    lightcone: Optional[LightCone] = None,
    relics: Optional[list[RelicPiece]] = None,
    relic_sets: Optional[dict[str, RelicSet]] = None,
    active_buffs: Optional[list[dict]] = None,
) -> CombatStats:
    """
    属性汇总管线：角色 + 光锥 + 遗器 + Buff → 最终战斗属性

    管线顺序:
    1. 基础属性 (角色 + 光锥基础值)
    2. 累积百分比和固定值 (行迹 + 遗器主副属性)
    3. 应用百分比和固定值到基础属性
    4. 光锥效果
    5. 遗器套装效果
    6. Buff/Debuff 修改器
    7. 速度最终计算 & 暴击率上限
    """
    stats = CombatStats()

    # Step 1: 基础属性
    stats.HP = character.base_HP + (lightcone.base_HP if lightcone else 0)
    stats.ATK = character.base_ATK + (lightcone.base_ATK if lightcone else 0)
    stats.DEF = character.base_DEF + (lightcone.base_DEF if lightcone else 0)
    stats.SPD = character.base_SPD

    acc = StatAccumulator()

    # Step 2: 行迹永久属性
    for st, v in (character.trace_stats or {}).items():
        # v6.6c P3: 数据约定——行迹固定速度记在 SPD_percent 键（角色录入规范）→ 按固定值加算;
        # 此前按百分比计算（+14→+14%速）, 与实机不符
        if st in ("SPD_percent", "SPD_PERCENT"):
            stats.SPD += v
        else:
            acc.add(st, v, stats)

    # Step 3: 遗器主属性 + 副属性
    if relics:
        for piece in relics:
            if piece.main_stat_type and piece.main_stat_value:
                if piece.main_stat_type in ("SPD_percent", "SPD_PERCENT"):
                    stats.SPD += piece.main_stat_value
                else:
                    acc.add(piece.main_stat_type, piece.main_stat_value, stats)
            for st, v in (piece.sub_stats or {}).items():
                if st in ("SPD_percent", "SPD_PERCENT"):
                    stats.SPD += v
                else:
                    acc.add(st, v, stats)

    # Step 4: 记录白值（供后续百分比加成使用），然后应用百分比和固定值
    stats._base_HP = stats.HP
    stats._base_ATK = stats.ATK
    stats._base_DEF = stats.DEF
    stats._base_SPD = stats.SPD
    stats.HP = stats.HP * (1.0 + acc.hp_pct) + acc.hp_flat
    stats.ATK = stats.ATK * (1.0 + acc.atk_pct) + acc.atk_flat
    stats.DEF = stats.DEF * (1.0 + acc.def_pct) + acc.def_flat

    # Step 5: 光锥效果（仅命途匹配时生效，不匹配时只有白值没有特效）
    if lightcone and lightcone.path == character.path:
        for effect in lightcone.effects:
            for attr, val in effect.attributes.items():
                _apply_stat_direct(stats, attr, val)

    # Step 6: 遗器套装效果
    if relics and relic_sets:
        set_counts = {}
        for piece in relics:
            set_counts[piece.set_name] = set_counts.get(piece.set_name, 0) + 1
        for set_name, count in set_counts.items():
            if set_name in relic_sets:
                for eff in relic_sets[set_name].effects:
                    if count >= eff.pieces_required:
                        # 基础属性
                        for attr, val in eff.attributes.items():
                            _apply_stat_direct(stats, attr, val)
                        # 条件评测
                        _eval_relic_condition(stats, eff.condition, eff.attributes, character)

    # Step 7: Buff/Debuff
    if active_buffs:
        for buff in active_buffs:
            for attr, val in buff.get("attributes", {}).items():
                _apply_stat_direct(stats, attr, val)

    # Step 8: 速度最终计算 & 暴击率上限
    stats.SPD = stats.SPD * (1.0 + stats.SPD_PERCENT)
    stats.CRIT_RATE = min(stats.CRIT_RATE, 1.0)

    return stats
