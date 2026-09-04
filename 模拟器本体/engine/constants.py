"""游戏常量定义"""
from enum import Enum


class Element(str, Enum):
    PHYSICAL = "物理"
    FIRE = "火"
    ICE = "冰"
    LIGHTNING = "雷"
    WIND = "风"
    QUANTUM = "量子"
    IMAGINARY = "虚数"

# 七属性全集（单一事实源：combat_engine/web 此前各自硬编码, M4 批1 统一）
WEAKNESS_ELEMENTS = [e.value for e in Element]


class Path(str, Enum):
    DESTRUCTION = "毁灭"
    HUNT = "巡猎"
    ERUDITION = "智识"
    HARMONY = "同谐"
    NIHILITY = "虚无"
    PRESERVATION = "存护"
    ABUNDANCE = "丰饶"
    ELATION = "欢愉"
    REMEMBRANCE = "记忆"


class DamageType(str, Enum):
    DIRECT = "direct"          # 直伤
    BREAK = "break"            # 击破伤害
    DOT = "dot"                # 持续伤害
    SUPER_BREAK = "super_break"    # 超击破
    ADDITIONAL = "additional"    # 附加伤害
    TRUE_DAMAGE = "true_damage"  # 真实伤害
    ELATION = "elation"         # 欢愉伤害


class SkillType(str, Enum):
    BASIC_ATTACK = "basic_attack"
    SKILL = "skill"
    ULTIMATE = "ultimate"
    TALENT = "talent"
    TECHNIQUE = "technique"
    ELATION_SKILL = "elation_skill"   # 欢愉技


class TechniqueCategory(str, Enum):
    """秘技分类"""
    BATTLE_START = "battle_start"   # 开战秘技 — 进战后触发效果
    SUPPORT = "support"              # 辅助秘技 — 大地图使用，不进战


class TargetType(str, Enum):
    SELF = "self"
    SINGLE_ALLY = "single_ally"
    ALL_ALLIES = "all_allies"
    SINGLE_ENEMY = "single_enemy"
    BLAST = "blast"            # 扩散（主目标+相邻）
    ALL_ENEMIES = "all_enemies"
    BOUNCE = "bounce"          # 弹射（随机选择敌方存活目标，不鞭尸）


class BuffType(str, Enum):
    BUFF = "buff"
    DEBUFF = "debuff"
    STATUS_MARK = "status_mark"


class BuffSource(str, Enum):
    CHARACTER_SKILL = "character_skill"
    LIGHT_CONE = "light_cone"
    RELIC = "relic"
    ENEMY_SKILL = "enemy_skill"


class DurationType(str, Enum):
    FIXED_TURNS = "fixed_turns"
    CASTER_TURN = "caster_turn"
    TARGET_TURN = "target_turn"
    PERMANENT = "permanent"


class StatType(str, Enum):
    """属性标识符，与JSON数据文件中的键名一致"""
    # 基础值
    HP_BASE = "HP_base"
    ATK_BASE = "ATK_base"
    DEF_BASE = "DEF_base"
    SPD_BASE = "SPD_base"
    TAUNT = "taunt"
    # 固定值加成
    HP_FLAT = "HP_flat"
    ATK_FLAT = "ATK_flat"
    DEF_FLAT = "DEF_flat"
    # 百分比加成
    HP_PERCENT = "HP_percent"
    ATK_PERCENT = "ATK_percent"
    DEF_PERCENT = "DEF_percent"
    SPD_PERCENT = "SPD_percent"
    # 暴击
    CRIT_RATE = "CRIT_RATE"
    CRIT_DMG = "CRIT_DMG"
    # 击破
    BREAK_EFFECT = "BREAK_EFFECT"
    # 命中/抵抗
    EFFECT_HIT_RATE = "EFFECT_HIT_RATE"
    EFFECT_RES = "EFFECT_RES"
    # 能量
    ENERGY_REGEN = "ENERGY_REGEN"
    # 元素增伤
    DMG_BONUS_PHYSICAL = "DMG_BONUS_PHYSICAL"
    DMG_BONUS_FIRE = "DMG_BONUS_FIRE"
    DMG_BONUS_ICE = "DMG_BONUS_ICE"
    DMG_BONUS_LIGHTNING = "DMG_BONUS_LIGHTNING"
    DMG_BONUS_WIND = "DMG_BONUS_WIND"
    DMG_BONUS_QUANTUM = "DMG_BONUS_QUANTUM"
    DMG_BONUS_IMAGINARY = "DMG_BONUS_IMAGINARY"
    # 全增伤
    DMG_BONUS_ALL = "DMG_BONUS_ALL"
    # 技能类型增伤
    DMG_BONUS_BASIC = "DMG_BONUS_BASIC"
    DMG_BONUS_SKILL = "DMG_BONUS_SKILL"
    DMG_BONUS_ULTIMATE = "DMG_BONUS_ULTIMATE"
    DMG_BONUS_FOLLOWUP = "DMG_BONUS_FOLLOWUP"
    # 穿透
    RES_PEN_ALL = "RES_PEN_ALL"
    RES_PEN_PHYSICAL = "RES_PEN_PHYSICAL"
    RES_PEN_FIRE = "RES_PEN_FIRE"
    RES_PEN_ICE = "RES_PEN_ICE"
    RES_PEN_LIGHTNING = "RES_PEN_LIGHTNING"
    RES_PEN_WIND = "RES_PEN_WIND"
    RES_PEN_QUANTUM = "RES_PEN_QUANTUM"
    RES_PEN_IMAGINARY = "RES_PEN_IMAGINARY"
    DEF_PEN = "DEF_PEN"
    DEF_REDUCTION = "DEF_REDUCTION"       # 减防%（降低敌方防御力）
    # DoT增伤
    DMG_BONUS_DOT = "DMG_BONUS_DOT"       # 持续伤害增伤（独立于元素增伤）
    # 削韧
    TOUGHNESS_EFFICIENCY = "TOUGHNESS_EFFICIENCY"
    # 治疗/护盾
    HEAL_BONUS = "HEAL_BONUS"
    SHIELD_BONUS = "SHIELD_BONUS"
    # 欢愉
    ELATION_LEVEL = "ELATION_LEVEL"     # 欢愉度
    LAUGH_BOOST = "LAUGH_BOOST"         # 增笑
    # 减伤
    DMG_REDUCTION = "DMG_REDUCTION"  # 受到伤害降低

    # 易伤
    VULNERABILITY_APPLIED = "VULNERABILITY_APPLIED"  # 对敌方施加的易伤（通用）
    VULNERABILITY_APPLIED_ELATION = "VULNERABILITY_APPLIED_ELATION"  # 仅欢愉伤害

    # 无视防御（按伤害类型作用域）
    DEF_PEN_BREAK = "DEF_PEN_BREAK"      # 仅击破伤害
    DEF_PEN_ELATION = "DEF_PEN_ELATION"  # 仅欢愉伤害
    DEF_PEN_MEMSPRITE = "DEF_PEN_MEMSPRITE"  # 仅忆灵伤害

    # 无视防御（按攻击类别作用域: ATK_ 前缀）
    DEF_PEN_ATK_FOLLOW_UP = "DEF_PEN_ATK_FOLLOW_UP"  # 仅追加攻击

    # 增伤（按攻击类别作用域: ATK_ 前缀）
    DMG_BONUS_ATK_FOLLOW_UP = "DMG_BONUS_ATK_FOLLOW_UP"  # 仅追加攻击


# 元素增伤属性 -> 元素类型的映射
ELEMENT_DMG_STAT_TO_ELEMENT = {
    StatType.DMG_BONUS_PHYSICAL: Element.PHYSICAL,
    StatType.DMG_BONUS_FIRE: Element.FIRE,
    StatType.DMG_BONUS_ICE: Element.ICE,
    StatType.DMG_BONUS_LIGHTNING: Element.LIGHTNING,
    StatType.DMG_BONUS_WIND: Element.WIND,
    StatType.DMG_BONUS_QUANTUM: Element.QUANTUM,
    StatType.DMG_BONUS_IMAGINARY: Element.IMAGINARY,
}

# 抗性穿透属性 -> 元素类型的映射
RES_PEN_STAT_TO_ELEMENT = {
    StatType.RES_PEN_PHYSICAL: Element.PHYSICAL,
    StatType.RES_PEN_FIRE: Element.FIRE,
    StatType.RES_PEN_ICE: Element.ICE,
    StatType.RES_PEN_LIGHTNING: Element.LIGHTNING,
    StatType.RES_PEN_WIND: Element.WIND,
    StatType.RES_PEN_QUANTUM: Element.QUANTUM,
    StatType.RES_PEN_IMAGINARY: Element.IMAGINARY,
}

# 欢愉伤害等级基础值（80级常数）
ELATION_BASE_DAMAGE = 7535.107

# 击破伤害等级基础值（80级常数）
BREAK_BASE_DAMAGE = 3767.55

# 超击破固定系数
SUPER_BREAK_COEFFICIENT = 1.5

# 韧性未击破减伤系数
TOUGHNESS_UNBROKEN_MULT = 0.9

# 命途 → 默认嘲讽值
PATH_TAUNT_VALUES = {
    "毁灭": 125,
    "巡猎": 75,
    "智识": 75,
    "同谐": 100,
    "虚无": 100,
    "存护": 150,
    "丰饶": 100,
    "欢愉": 100,
    "记忆": 100,
}

# 击破属性基础倍率（元素 → 倍率）
BREAK_ELEMENT_MULTIPLIERS = {
    "物理": 1.0,
    "火": 1.0,
    "冰": 1.0,
    "雷": 2.0,
    "风": 1.0,
    "量子": 1.0,
    "虚数": 1.0,
}

# 遗器主属性为固定值的部位
FIXED_MAIN_STAT_SLOTS = {"head": StatType.HP_FLAT, "hands": StatType.ATK_FLAT}

# 遗器固定主属性满级(15级)数值
FIXED_MAIN_STAT_VALUES = {
    "head": 705.0,    # 小生命
    "hands": 352.0,   # 小攻击
}

# 遗器满级统一为15级
RELIC_MAX_LEVEL = 15

# 副词条种类（排除属性增伤、能量恢复、治疗加成）
SUB_STAT_TYPES = [
    StatType.HP_FLAT, StatType.ATK_FLAT, StatType.DEF_FLAT,
    StatType.HP_PERCENT, StatType.ATK_PERCENT, StatType.DEF_PERCENT,
    StatType.SPD_PERCENT,
    StatType.CRIT_RATE, StatType.CRIT_DMG,
    StatType.BREAK_EFFECT, StatType.EFFECT_HIT_RATE, StatType.EFFECT_RES,
]

# 遗器强化节点等级
RELIC_ENHANCE_LEVELS = [3, 6, 9, 12, 15]

# 副词条数值档位 (低/中/高档) × 5次全拉满理论最大值
# 格式: (低档, 中档, 高档, 5次高档最大值)
SUB_STAT_VALUES = {
    StatType.HP_FLAT:       (234.0, 270.0, 306.0, 1530.0),
    StatType.ATK_FLAT:      (16.0,  19.0,  23.0,  115.0),
    StatType.DEF_FLAT:      (16.0,  19.0,  23.0,  115.0),
    StatType.SPD_PERCENT:   (2.0,   3.0,   4.0,   20.0),
    StatType.HP_PERCENT:    (2.1,   2.5,   2.9,   14.5),
    StatType.ATK_PERCENT:   (2.1,   2.5,   2.9,   14.5),
    StatType.DEF_PERCENT:   (2.6,   3.1,   3.6,   18.0),
    StatType.CRIT_RATE:     (2.5,   3.0,   3.5,   17.5),
    StatType.CRIT_DMG:      (5.1,   5.8,   6.5,   32.5),
    StatType.BREAK_EFFECT:  (4.1,   4.8,   5.5,   27.5),
    StatType.EFFECT_HIT_RATE: (2.1, 2.5,   2.9,   14.5),
    StatType.EFFECT_RES:    (2.1,   2.5,   2.9,   14.5),
}

# v7.5.0（项目主指示）: 词条数值整体上调 ×1.1（≈中高档之间的小毕业平均档;
# 各属性高档/中档比值 1.12~1.33, ×1.2 会超过暴伤高档均值——调档只改此常量）
SUBSTAT_ROLL_FACTOR = 1.1
# v7.5.0（项目主指示）: 单一词条占有效词条数上限（实机一件遗器最多 4 种副词条类型,
# "30 条全堆一个属性"不现实——银狼Lv.999 全速度问题）
SUBSTAT_SINGLE_SHARE = 0.6

# 前端推荐响应契约键（大写 SPD_PERCENT; v7.3 项目主裁决: 效果命中入契约, 8 键扩 9 键）。
# v7.17.0 M7 键集单源: 前端/app.js、web/api.py、relic_optimizer 共用此序此键,
# 前端展示短键与标签由 web 层 /api/keysets 端点单源提供。
FRONTEND_ROLL_KEYS = ["CRIT_RATE", "CRIT_DMG", "ATK_percent", "SPD_PERCENT",
                      "HP_percent", "EFFECT_RES", "DEF_percent", "BREAK_EFFECT",
                      "EFFECT_HIT_RATE"]

# 前端副词条键 → 引擎内部键（仅速度大小写差异; 引擎侧双向兼容不变）
SUBSTAT_KEY_MAP = {k: (StatType.SPD_PERCENT.value if k == "SPD_PERCENT" else k)
                   for k in FRONTEND_ROLL_KEYS}


# 各部位主词条满级(+15)数值
RELIC_MAIN_STAT_VALUES = {
    "head": {StatType.HP_FLAT: 705.0},
    "hands": {StatType.ATK_FLAT: 352.0},
    "body": {
        StatType.HP_PERCENT: 43.2,
        StatType.ATK_PERCENT: 43.2,
        StatType.DEF_PERCENT: 54.0,
        StatType.CRIT_RATE: 32.4,
        StatType.CRIT_DMG: 64.8,
        StatType.HEAL_BONUS: 34.5,
        StatType.EFFECT_HIT_RATE: 43.2,
    },
    "feet": {
        StatType.HP_PERCENT: 43.2,
        StatType.ATK_PERCENT: 43.2,
        StatType.DEF_PERCENT: 54.0,
        StatType.SPD_PERCENT: 25.0,  # 速度（固定值，非百分比）
    },
    "planar_sphere": {
        StatType.HP_PERCENT: 43.2,
        StatType.ATK_PERCENT: 43.2,
        StatType.DEF_PERCENT: 54.0,
        StatType.DMG_BONUS_PHYSICAL: 38.8,
        StatType.DMG_BONUS_FIRE: 38.8,
        StatType.DMG_BONUS_ICE: 38.8,
        StatType.DMG_BONUS_LIGHTNING: 38.8,
        StatType.DMG_BONUS_WIND: 38.8,
        StatType.DMG_BONUS_QUANTUM: 38.8,
        StatType.DMG_BONUS_IMAGINARY: 38.8,
    },
    "link_rope": {
        StatType.HP_PERCENT: 43.2,
        StatType.ATK_PERCENT: 43.2,
        StatType.DEF_PERCENT: 54.0,
        StatType.BREAK_EFFECT: 64.8,
        StatType.ENERGY_REGEN: 19.4,
    },
}

# 各部位可选主词条池
RELIC_MAIN_STAT_POOL = {
    "head": [StatType.HP_FLAT],
    "hands": [StatType.ATK_FLAT],
    "body": [
        StatType.CRIT_RATE, StatType.CRIT_DMG, StatType.ATK_PERCENT,
        StatType.HP_PERCENT, StatType.DEF_PERCENT,
        StatType.HEAL_BONUS, StatType.EFFECT_HIT_RATE,
    ],
    "feet": [StatType.ATK_PERCENT, StatType.HP_PERCENT, StatType.DEF_PERCENT, StatType.SPD_PERCENT],
    "planar_sphere": [
        StatType.ATK_PERCENT, StatType.HP_PERCENT, StatType.DEF_PERCENT,
        StatType.DMG_BONUS_PHYSICAL, StatType.DMG_BONUS_FIRE, StatType.DMG_BONUS_ICE,
        StatType.DMG_BONUS_LIGHTNING, StatType.DMG_BONUS_WIND,
        StatType.DMG_BONUS_QUANTUM, StatType.DMG_BONUS_IMAGINARY,
    ],
    "link_rope": [
        StatType.ATK_PERCENT, StatType.HP_PERCENT, StatType.DEF_PERCENT,
        StatType.BREAK_EFFECT, StatType.ENERGY_REGEN,
    ],
}

# 技能类型 -> 对应增伤属性
SKILL_TYPE_DMG_BONUS = {
    SkillType.BASIC_ATTACK: StatType.DMG_BONUS_BASIC,
    SkillType.SKILL: StatType.DMG_BONUS_SKILL,
    SkillType.ULTIMATE: StatType.DMG_BONUS_ULTIMATE,
}
