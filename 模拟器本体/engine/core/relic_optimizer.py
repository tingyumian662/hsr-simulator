"""遗器自动配装优化器 v2

条件约束 + 边际效益优化：
1. 先满足天赋/光锥/遗器套装的阈值条件
2. 剩余词条按边际效益（dDamage/dRoll）贪心分配
3. 双暴自动 1:2 配比，稀释感知
"""
import copy
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from engine.constants import (
    RELIC_MAIN_STAT_POOL, RELIC_MAIN_STAT_VALUES,
    SUB_STAT_TYPES, SUB_STAT_VALUES, StatType, Element,
    ELEMENT_DMG_STAT_TO_ELEMENT, ELATION_BASE_DAMAGE, BREAK_BASE_DAMAGE,
    SUBSTAT_ROLL_FACTOR, SUBSTAT_SINGLE_SHARE, FRONTEND_ROLL_KEYS,
)
from engine.models.character import Character
from engine.models.equipment import RelicPiece
from engine.core.attributes import compute_combat_stats, CombatStats
from engine.runtime import AV_PER_TURN
from engine.characters import BREAK_CHAR_CONFIG, BREAK_CHAR_IDS


RELIC_SLOTS_ORDER = ["head", "hands", "body", "feet", "planar_sphere", "link_rope"]

ACTION_WINDOW_AV = 150.0

# 默认优先级（用于无边际信息时的fallback）
DEFAULT_PRIORITY = {
    "CRIT_RATE": 10, "CRIT_DMG": 10, "ATK_percent": 8, "SPD_percent": 7,
    "HP_percent": 5, "DEF_percent": 4, "BREAK_EFFECT": 6,
    "EFFECT_HIT_RATE": 5, "EFFECT_RES": 3,
    "HP_flat": 2, "ATK_flat": 2, "DEF_flat": 1,
}

# 前端推荐响应契约键（大写 SPD_PERCENT）——v7.17.0 M7 键集单源:
# 定义唯一在 engine.constants.FRONTEND_ROLL_KEYS, 此处导入再导出（测试与本模块消费）

# v7.3.1（项目主纠正）: 总词条固定 50（6件×8~9条中值, 模拟器默认值, 不可调）;
# 用户可调的是有效词条数——默认 30, 上限 50（=全部词条均有效, 无非有效词条）
TOTAL_ROLLS = 50

# 内部分配使用的 key（StatType.value，SPD_percent 小写 p）
_FRONTEND_TO_INTERNAL = {k: (StatType.SPD_PERCENT.value if k == "SPD_PERCENT" else k)
                         for k in FRONTEND_ROLL_KEYS}
_INTERNAL_TO_FRONTEND = {v: k for k, v in _FRONTEND_TO_INTERNAL.items()}


@dataclass
class Constraint:
    """条件约束"""
    stat: str          # 目标属性 StatType 值
    op: str            # "lt" | "lte" | "gt" | "gte" | "eq"
    value: float       # 阈值
    reward: dict       # 达成后的属性奖励 {StatType: value}


@dataclass
class RelicBuild:
    pieces: dict
    total_effective_rolls: int
    effective_stats: list
    final_stats: CombatStats = None


@dataclass
class CharProfile:
    """角色定位分析结果（技能结构驱动，不依赖命途硬编码）"""
    role: str                    # "dps"|"healer"|"shielder"|"debuffer"|"support"|"unknown"
    primary_stat: str            # "ATK"|"HP"|"DEF" — 边际主缩放属性
    scaling_stats: set           # {"ATK","HP","DEF"}
    is_dps: bool = False
    has_heal: bool = False
    has_shield: bool = False
    has_debuff: bool = False
    needs_ehr: bool = False
    is_break: bool = False
    is_elation: bool = False
    weights: dict = field(default_factory=lambda: {
        "direct": 1.0, "heal": 0.0, "break": 0.0, "shield": 0.0, "elation": 0.0,
    })
    spd_per_point: list = field(default_factory=list)  # [(stat, per_point_decimal)]
    direct_scale: float = 0.0      # v6.11: 非普攻直伤总倍率
    dot_scale: float = 0.0         # v6.11: DOT 总倍率
    dot_signal: bool = False       # v6.11: DOT 输出型（含行迹文本识别）
    spd_convert: float = 0.0       # v7.3: 换算型行迹比率（攻击力等同速度×720% → 720.0）
    spd_tank_only: bool = False    # v7.3: 速度权重仅来自 tank 保底（跨第一个断点即止, 8-19 裁决"其他词条达标后适当分配"）
    stat_converts: dict = field(default_factory=dict)  # v7.4: 通用换算投资 {源属性: 比率%}（爆伤转拐等）
    atk_per_point: list = field(default_factory=list)  # v7.4: [(目标属性, 每N点攻击, 每份加成%, 上限加成%)] 攻击阈值连续换算
    atk_threshold: float = 0.0     # v7.4: 攻击力阈值行迹（>2000 等）


# v5.5: 击破角色副词条策略配置（数据驱动）
# spd_target: 固定速度达标值（流萤=完全燃烧四动 145 面板[2次击破延后模型, 用户确认];
#   其余击破角色 134 = 星铁 150 行动值回合 2 动阈值 floor(150×134/10000)=2）
# exclude_atk: 放弃攻击词条（流萤, 用户确认）

# v7.3: 击破定位显式配置（乱破 BE 行迹仅 13.3 不足 30 且纯空壳无文本信号）

# 命途兜底角色定位（仅空壳角色使用）
_PATH_ROLE = {
    "巡猎": "dps", "毁灭": "dps", "智识": "dps", "欢愉": "dps", "记忆": "dps",
    "同谐": "support", "虚无": "debuffer", "存护": "shielder", "丰饶": "healer",
}

_SPD_THRESHOLD_RE = re.compile(
    r"(?:SPD|速度)\s*(?:≥|>=|≧|>|大于等于|大于|不低于|超过)\s*(\d{2,3})\s*点?"
)
# "每超1点" 连续加成正则（v7.4: 兼容"超额每1点"变体——银狼Lv.999 欢愉度换算）
_SPD_PER_POINT_RE = re.compile(r"(?:每超|超额每)1点(?:SPD|速度)?[^。；;]*?(?:([^。；;]*?)(\d+(?:\.\d+)?)\s*%)")
# v7.3: 击破文本信号细化——裸"击破"二字会误伤银狼（"敌弱点被击破时植入缺陷"是 debuff 机制），
# 收窄为 击破特攻/超击破/击破伤害 关键词（阮·梅"冰击破伤害"/大丽花"击破特攻"仍命中）
_BREAK_TEXT_RE = re.compile(r"击破特攻|超击破|击破伤害")

# v7.4: 通用换算投资信号——"X提高…等同于（自身/角色名）Y的N%" → Y 持续投入（花火/布洛妮娅爆伤转拐）。
# 双锚点防误伤: ①目标属性+提高 在前（排除符玄"回复等同于生命上限5%"），②源属性+的N% 在后
# （排除希儿"等同于终结技伤害30%真伤"——"伤害"非子属性关键词）
_STAT_CONVERT_RE = re.compile(
    r"(?:暴击伤害|暴击率|攻击力|生命上限|防御力|速度)(?:提高|提高效果)[^。；;]*?"
    r"等同于[^。；;]*?(暴击伤害|暴击率|攻击力|速度|生命上限|防御力)的(\d+(?:\.\d+)?)%")
# v7.17.0: 中文关键词→属性的两张表共享核心 5 键（此前核心互抄, 现单源派生）。
# _KEYWORD_STAT 供子串扫描按迭代序取首个命中——构造序必须保持
# "伤害,核心5键,欢愉度,治疗量,抗性穿透,效果抵抗"（与旧序逐项一致）, 勿重排;
# _KEYWORD_TO_STAT 仅按 _STAT_CONVERT_RE 捕获组单键查表, 迭代序不参与语义。
_STAT_KEYWORD_CORE = {"暴击率": "CRIT_RATE", "暴击伤害": "CRIT_DMG", "攻击力": "ATK_percent",
                      "生命上限": "HP_percent", "防御力": "DEF_percent"}
_KEYWORD_TO_STAT = {**_STAT_KEYWORD_CORE, "速度": "SPD_percent"}

# v7.4: 攻击力阈值行迹（火花>2000→欢愉度、刻律德菈>2000→暴伤、开拓者·欢愉>1000、流萤>1800）
_ATK_THRESHOLD_RE = re.compile(r"(?:攻击力|ATK)\s*[≥>＞]+\s*(\d{3,4})")
# 每超N点→目标属性+M%（N≠1 的攻击口径; "每超200→"可无点字; 上限K%/最多+K% 可选封顶）
_ATK_PER_POINT_RE = re.compile(
    r"每超(\d+)点?[^。；;]*?(欢愉度|暴伤|暴击伤害|击破特攻|治疗量)\s*\+\s*(\d+(?:\.\d+)?)%"
    r"(?:[^。；;]*?(?:上限|最多\+?)(\d+(?:\.\d+)?)%)?")
_ATK_PP_TARGET = {"欢愉度": "ELATION_LEVEL", "暴伤": "CRIT_DMG", "暴击伤害": "CRIT_DMG",
                  "击破特攻": "BREAK_EFFECT", "治疗量": "HEAL_BONUS"}

# 阈值奖励关键词 → 属性（迭代序敏感: 子串扫描首个命中即返回, 见 _STAT_KEYWORD_CORE 注）
_KEYWORD_STAT = {
    "伤害": "DMG_BONUS_ALL", **_STAT_KEYWORD_CORE, "欢愉度": "ELATION_LEVEL",
    "治疗量": "HEAL_BONUS", "抗性穿透": "RES_PEN_ALL", "效果抵抗": "EFFECT_RES",
}

# 遗器套装 condition 码 → 约束（游戏知识映射，集中一处）
RELIC_CONDITION_CONSTRAINTS = {
    "low_spd_cr_boost": [  # 哀歌覆国的诗人 4pc
        Constraint(StatType.SPD_PERCENT.value, "lt", 110, {"CRIT_RATE": 20.0}),
        Constraint(StatType.SPD_PERCENT.value, "lt", 95, {"CRIT_RATE": 32.0}),
    ],
    "spd_threshold_120_atk": [  # 太空封印站 2pc
        Constraint(StatType.SPD_PERCENT.value, "gte", 120, {"ATK_percent": 12.0}),
    ],
    "spd_threshold_120_team_atk": [  # 不老者的仙舟 2pc
        Constraint(StatType.SPD_PERCENT.value, "gte", 120, {"ATK_percent": 8.0}),
    ],
    "spd_cr_threshold_and_first_elation": [  # 卜者 2pc
        Constraint(StatType.SPD_PERCENT.value, "gte", 120, {"CRIT_RATE": 10.0}),
        Constraint(StatType.SPD_PERCENT.value, "gte", 160, {"CRIT_RATE": 18.0}),
    ],
    "enter_combat_action_advance": [  # 翁瓦克 2pc
        Constraint(StatType.SPD_PERCENT.value, "gte", 120, {}),
    ],
    "effect_res_30_team_cd": [  # 折断的龙骨 2pc
        Constraint(StatType.EFFECT_RES.value, "gte", 30, {"CRIT_DMG": 10.0}),
    ],
    "ehr_threshold_50_def": [  # 贝洛伯格 2pc
        Constraint(StatType.EFFECT_HIT_RATE.value, "gte", 50, {"DEF_percent": 15.0}),
    ],
}


def _mid(stat_type: str) -> float:
    """副词条中档值（v7.5: ×SUBSTAT_ROLL_FACTOR 上调——小毕业平均档口径）"""
    v = SUB_STAT_VALUES.get(stat_type)
    return (v[1] * SUBSTAT_ROLL_FACTOR) if v else 0.0


def _main_val(slot: str, stat_type) -> float:
    """主词条满级值"""
    st = stat_type.value if hasattr(stat_type, 'value') else stat_type
    return RELIC_MAIN_STAT_VALUES.get(slot, {}).get(st, 0.0)


def _pick_main_stats(character: Character, effective_stats: list[str],
                     constraints: list[Constraint] = None,
                     rewards: dict = None, crit_target: float = 0.0) -> dict:
    """优选主词条。考虑约束和满爆目标。"""
    mains = {"head": StatType.HP_FLAT, "hands": StatType.ATK_FLAT}
    eff_set = set(effective_stats)
    cons = constraints or []
    rew = rewards or {}

    # 躯干: 根据是否满爆决定暴伤还是暴击衣
    body_pool = RELIC_MAIN_STAT_POOL["body"]
    cr_body_val = 32.4  # CRIT_RATE body 满级值
    cd_in_pool = StatType.CRIT_DMG in body_pool and StatType.CRIT_DMG.value in eff_set
    cr_in_pool = StatType.CRIT_RATE in body_pool and StatType.CRIT_RATE.value in eff_set

    if cd_in_pool or cr_in_pool:
        # 计算非 sub 来源的暴击率
        non_sub_cr = 0.05  # 基础
        non_sub_cr += character.trace_stats.get(StatType.CRIT_RATE.value, 0) / 100.0
        non_sub_cr += rew.get(StatType.CRIT_RATE.value, 0) / 100.0
        # sub 最大暴击率（6件各一条中档 3%）
        max_sub_cr = 6 * _mid(StatType.CRIT_RATE.value) / 100.0

        if crit_target > 0:
            # 选暴伤衣能到 100% 吗？
            with_cd_body = non_sub_cr + max_sub_cr
            if with_cd_body < crit_target and cr_in_pool:
                # 暴伤衣不够 → 选暴击衣
                mains["body"] = StatType.CRIT_RATE
            elif cd_in_pool:
                mains["body"] = StatType.CRIT_DMG
            elif cr_in_pool:
                mains["body"] = StatType.CRIT_RATE
            else:
                mains["body"] = max(body_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0))
        else:
            if cd_in_pool:
                mains["body"] = StatType.CRIT_DMG
            elif cr_in_pool:
                mains["body"] = StatType.CRIT_RATE
            else:
                mains["body"] = max(body_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0))
    else:
        mains["body"] = max(body_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0) if s.value in eff_set else -1)

    # 脚
    feet_pool = RELIC_MAIN_STAT_POOL["feet"]
    # 检查有无 SPD 上限约束（SPD < X）
    spd_capped = any(c.stat == StatType.SPD_PERCENT.value and c.op in ("lt", "lte") for c in cons)
    if StatType.SPD_PERCENT in feet_pool and StatType.SPD_PERCENT.value in eff_set and not spd_capped:
        mains["feet"] = StatType.SPD_PERCENT
    elif StatType.ATK_PERCENT in feet_pool:
        mains["feet"] = StatType.ATK_PERCENT
    else:
        mains["feet"] = max(feet_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0))

    # 球
    sphere_pool = RELIC_MAIN_STAT_POOL["planar_sphere"]
    element_dmg = None
    for st, elem in ELEMENT_DMG_STAT_TO_ELEMENT.items():
        if elem.value == character.element and st in sphere_pool:
            element_dmg = st
            break
    if element_dmg:
        mains["planar_sphere"] = element_dmg
    elif StatType.ATK_PERCENT in sphere_pool:
        mains["planar_sphere"] = StatType.ATK_PERCENT
    else:
        mains["planar_sphere"] = max(sphere_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0))

    # 绳: HP缩放角色优先HP%，否则ATK%
    rope_pool = RELIC_MAIN_STAT_POOL["link_rope"]
    hp_scaler = _is_hp_scaler(character)
    if hp_scaler and StatType.HP_PERCENT in rope_pool and StatType.HP_PERCENT.value in eff_set:
        mains["link_rope"] = StatType.HP_PERCENT
    elif StatType.ATK_PERCENT in rope_pool and StatType.ATK_PERCENT.value in eff_set:
        mains["link_rope"] = StatType.ATK_PERCENT
    elif StatType.ENERGY_REGEN in rope_pool and StatType.ENERGY_REGEN.value in eff_set:
        mains["link_rope"] = StatType.ENERGY_REGEN
    elif StatType.BREAK_EFFECT in rope_pool and StatType.BREAK_EFFECT.value in eff_set:
        mains["link_rope"] = StatType.BREAK_EFFECT
    else:
        mains["link_rope"] = max(rope_pool, key=lambda s: DEFAULT_PRIORITY.get(s.value, 0))

    return mains


def _cap(stat: str, mains: dict) -> int:
    """副词条在6件遗器上的最大可分配件数（排除主词条冲突）"""
    c = 0
    for slot in RELIC_SLOTS_ORDER:
        mv = mains.get(slot)
        if not mv:
            c += 1  # 空槽位可分配
            continue
        mv_val = mv.value if hasattr(mv, 'value') else mv
        if mv_val != stat:  # v7.3: 此前条件式运算符优先级错误恒真, 主词条冲突件从不排除
            c += 1
    return c


def _solve_constraints(character: Character, mains: dict, constraints: list[Constraint],
                       *, strict_lt: bool = False, cap_fn=None,
                       base_stats: CombatStats = None) -> tuple[dict[str, int], dict]:
    """解析约束，返回满足条件需要的词条分配 + 合并后的reward。

    返回: (mandatory_rolls, merged_rewards)
    strict_lt: True 时 lt 约束仅在当前值 < 阈值时才发奖励（否则无条件合并，保旧测试）
    cap_fn: 词条上限函数，默认 _cap（每件1条）；recommend 传 _roll_cap（每件5次强化）
    """
    mandatory = {}
    rewards = {}
    cap_fn = cap_fn or _cap

    for c in constraints:
        # v6.11 阶段2: 达标预算按约束属性取当前值——此前所有约束都读 SPD 值,
        # 导致龙骨 EFFECT_RES≥30 / 贝洛伯格 EHR≥50 的达标计算恒错（用速度值比 30/50）
        if base_stats is not None:
            if c.stat == StatType.SPD_PERCENT.value:
                current = base_stats.SPD  # SPD 阈值=最终速度值
            elif c.stat == StatType.ATK_PERCENT.value:
                current = base_stats.ATK  # v7.4: ATK 阈值=面板攻击值
            elif c.stat in (StatType.EFFECT_RES.value, StatType.EFFECT_HIT_RATE.value):
                current = getattr(base_stats, c.stat, 0.0) * 100.0  # 面板小数→百分数
            else:
                current = getattr(base_stats, c.stat, 0.0)
        else:
            # 主词条贡献
            main_contrib = 0.0
            for slot, mt in mains.items():
                mv = mt.value if hasattr(mt, 'value') else mt
                if mv == c.stat:
                    main_contrib += _main_val(slot, mt)
            # 行迹贡献（SPD 行迹按旧口径折入 base_SPD 的 flat 值, 与修复前语义一致）
            trace_contrib = character.trace_stats.get(c.stat, 0.0)
            current = (character.base_SPD if c.stat == StatType.SPD_PERCENT.value
                       else character.base_ATK * (1 + character.trace_stats.get("ATK_percent", 0.0) / 100.0)
                       if c.stat == StatType.ATK_PERCENT.value
                       else 0.0) + main_contrib + trace_contrib

        if c.op in ("gte", "gt"):
            deficit = c.value - current
            if deficit > 0:
                roll_val = _mid(c.stat)
                # v7.4: ATK 阈值的缺口是面板攻击点数, 中档词条换算为白值百分比
                if c.stat == StatType.ATK_PERCENT.value and roll_val > 0:
                    base_atk = (getattr(base_stats, "_base_ATK", 0) or character.base_ATK)
                    roll_val = base_atk * roll_val / 100.0
                if roll_val > 0:
                    needed = int(deficit / roll_val) + (1 if deficit % roll_val > 0 else 0)
                    cap_limit = cap_fn(c.stat, mains)
                    mandatory[c.stat] = min(needed, cap_limit)

        elif c.op in ("lt", "lte"):
            # 上限约束：strict_lt=True 且当前值 >= 阈值时不发奖励（诗人套语义）
            if strict_lt and current >= c.value:
                continue
            # 否则仅作为上限参考，不分配正词条

        # 合并 reward
        for k, v in c.reward.items():
            rewards[k] = rewards.get(k, 0.0) + v

    return mandatory, rewards


def _is_hp_scaler(character: Character) -> bool:
    """检查角色是否有 HP 缩放的伤害技能"""
    for sk in character.skills.values():
        for m in (sk.multipliers or []):
            if getattr(m, 'stat', '') == 'HP' and getattr(m, 'damage_type', '') == 'direct':
                return True
    return False


# ═══════════════════════════════════════════════════════════════════
# 角色定位分析器（技能结构驱动）
# ═══════════════════════════════════════════════════════════════════

def _memsprite_skills(char: Character) -> dict:
    """获取忆灵技能（遐蝶的 HP 倍率在死龙技能里）"""
    ms = getattr(char, 'memsprite', None)
    if ms and getattr(ms, 'skills', None):
        return ms.skills
    return {}


def _fallback_primary(char: Character, role: str) -> str:
    """v7.3: 空壳角色主缩放判据改基础白值结构。此前按行迹百分比判断, 但
    ATK+28%/HP+10%/DEF+22.5% 是人手一条的通用行迹, 导致 20 个角色主属性推错
    （银枝/克拉拉等攻击C全推了生命, 杰帕德等防御T推了攻击）。阈值已对全部
    空壳角色逐一核对: DEF≥600 全是防御缩放, HP≥1300且ATK<600 全是生命缩放。"""
    if role == "healer":
        return "ATK" if char.base_ATK >= 700 else "HP"  # 罗刹 757 ATK 基数治疗
    if char.base_DEF >= 600:
        return "DEF"
    if char.base_HP >= 1300 and char.base_ATK < 600:
        return "HP"
    if role == "shielder" or char.path == "存护":
        return "DEF"
    return "ATK"


def _fallback_profile_from_path(char: Character) -> CharProfile:
    """空壳角色（无技能数据）的兜底定位：命途定角色, 白值结构定缩放（v7.3）"""
    role = _PATH_ROLE.get(char.path, "support")
    primary = _fallback_primary(char, role)
    # v7.3: 击破信号在兜底路径同样生效（波提欧 BE 行迹 37.3 / 乱破显式配置）;
    # 输出权重镜像真实击破角色口径（直伤×0.3 归一后 break ≈0.8）
    is_break = (char.id in BREAK_CHAR_IDS
                or (char.trace_stats or {}).get("BREAK_EFFECT", 0) >= 30)
    weights = ({"direct": 0.2, "heal": 0.0, "break": 0.8, "shield": 0.0, "elation": 0.0}
               if is_break
               else {"direct": 1.0, "heal": 0.0, "break": 0.0, "shield": 0.0, "elation": 0.0})
    return CharProfile(
        role=role, primary_stat=primary, scaling_stats={primary},
        is_dps=(role == "dps"),
        has_heal=(role == "healer"), has_shield=(role == "shielder"),
        has_debuff=(role == "debuffer"), needs_ehr=(role == "debuffer"),
        is_break=is_break,
        is_elation=(role == "dps" and char.path == "欢愉"),
        weights=weights,
    )


def _parse_spd_threshold_reward(desc: str, threshold: int) -> dict:
    """解析阈值后的奖励（阈值后 25 字符内找关键词+百分比）"""
    idx = desc.find(str(threshold))
    if idx < 0:
        return {}
    seg = desc[idx + len(str(threshold)):]
    for kw, st in _KEYWORD_STAT.items():
        i = seg.find(kw)
        if i >= 0:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", seg[i:i + 25])
            if m:
                return {st: float(m.group(1))}
    return {}


def _parse_spd_per_point(desc: str) -> list:
    """解析"每超1点SPD→XX+1%"连续加成（v7.4: 目标属性可能在前文——
    银狼Lv.999"欢愉度+50%，超额每1点+2%"的+2%子句本身无关键词, 回看前文取目标）"""
    results = []
    for m in _SPD_PER_POINT_RE.finditer(desc):
        kw_text, num = m.group(1), float(m.group(2))
        ctx = desc[max(0, m.start() - 24):m.start()] + kw_text
        for kw, st in _KEYWORD_STAT.items():
            if kw in ctx:
                results.append((st, num / 100.0))
                break
    return results


def _analyze_character(char: Character, lc=None, pieces=None, relic_sets=None) -> CharProfile:
    """从技能结构识别角色定位（含忆灵技能）。空壳角色走命途兜底。"""
    skills = {**char.skills, **(_memsprite_skills(char) or {})}

    def _expand(m):
        """v5.5: 弹射/多段 multiplier 按 hits 展开（同谐战技 5 段不再当 1 次）"""
        return [m] * max(getattr(m, 'hits', 1), 1)

    muls = [mm for sk in skills.values() for m in (sk.multipliers or []) for mm in _expand(m)]
    effs = [e for sk in skills.values() for e in (sk.effects or [])]
    dmg_types = [m.damage_type for m in muls]
    trace_text = " ".join(t.description for t in (char.traces or []))

    # 空壳 → 命途兜底
    if not skills and not trace_text:
        return _fallback_profile_from_path(char)

    has_heal = any(e.type == "heal" for e in effs)
    has_shield = any(e.type == "shield" for e in effs)
    has_debuff = any(e.type == "debuff" for e in effs)
    has_buff = any(e.type == "buff" for e in effs)
    # v5.5: 团队向 buff（排除 self 状态技, 如流萤完全燃烧）
    team_buff = any(e.type == "buff" and e.target not in ("self",) for e in effs)
    # dps 判定只看非普攻技能（普攻人人都有 direct 伤害，不构成 dps 定位）
    non_basic_muls = [mm for skn, sk in skills.items()
                      if skn != "basic_attack" and "basic_attack" not in skn
                      for m in (sk.multipliers or []) for mm in _expand(m)]
    non_basic_dmg = [m.damage_type for m in non_basic_muls]
    has_direct = any(t in ("direct", "dot", "additional", "true_damage") for t in non_basic_dmg)
    # v7.3: 击破文本信号细化（_BREAK_TEXT_RE 注释见上）+ BE 行迹≥30 + 显式配置
    is_break = bool(any(t in ("break", "super_break") for t in dmg_types)
                    or _BREAK_TEXT_RE.search(trace_text)
                    or (char.trace_stats or {}).get("BREAK_EFFECT", 0) >= 30
                    or char.id in BREAK_CHAR_IDS)
    needs_ehr = has_debuff or "效果命中" in trace_text
    # v6.11 阶段0: 非普攻直伤总倍率（hits 已展开）——dps 门槛依据
    non_basic_total_scale = sum(getattr(m, 'scale', 0.0) or 0.0 for m in non_basic_muls)
    dot_total_scale = sum(getattr(m, 'scale', 0.0) or 0.0
                          for m in non_basic_muls if m.damage_type == 'dot')
    # v6.11 阶段1: DOT 输出信号（手写DOT路径无 multipliers, 靠行迹文本「DOT/持续伤害」识别——海瑟音）
    dot_signal = dot_total_scale > 0 or 'DOT' in trace_text.upper() or '持续伤害' in trace_text

    # 治疗在战技/终结技 → healer（风堇/藿藿）。
    # 排除: 目标为忆灵(memsprite)的治疗 / 自我回复(target=self，输出角色的生存机制)
    heal_in_main = any(
        e.type == "heal" and e.target not in ("memsprite", "self")
        for skn in ("skill", "ultimate")
        for e in (skills.get(skn).effects or []) if skills.get(skn)
    )

    # v6.11 阶段0: dps 门槛=总倍率≥300 或 直伤段数≥4（记忆角色倍率结构特殊但多段输出,
    # 如遐蝶死龙4段喷吐/昔涟德谬歌多段; 符玄只有1段100%倍率不达标）
    direct_segments = sum(1 for t in non_basic_dmg
                          if t in ("direct", "dot", "additional", "true_damage"))
    if heal_in_main:
        role = "healer"
    elif has_shield and not has_direct:
        role = "shielder"
    elif is_break and team_buff and not heal_in_main:
        role = "support"  # v5.5: 击破辅助（忘归人狐祈/同谐伴舞: 队友向buff; 攻击附带debuff如火弱点不算）
    elif has_debuff and not has_direct and not has_buff:
        role = "debuffer"
    elif has_direct and (non_basic_total_scale >= 300 or direct_segments >= 4
                          or (char.trace_stats or {}).get("CRIT_DMG", 0) >= 20):
        # 第三信号: 暴伤行迹≥20%（昔涟输出在诗篇手写机制, JSON倍率仅占位60%; 符玄只有暴击率18.7%不达标）
        role = "dps"
    elif has_direct:
        # v6.11 阶段0: 低倍率直伤不是主输出——存护=承伤型 tank, 其余=功能性 support
        # （符玄: 终结技仅100%HP倍率, 此前被误判 dps 推双暴）
        role = "tank" if char.path == "存护" else "support"
    elif has_buff:
        role = "support"
    else:
        # v7.3: unknown 且同谐/欢愉 → support（阮·梅等 buff 效果引擎手写, JSON effects 为空）
        role = "support" if char.path in ("同谐", "欢愉") else "unknown"

    # 主缩放属性：按伤害倍率 stat 加权（非普攻技能）
    stat_weight = {"ATK": 0.0, "HP": 0.0, "DEF": 0.0}
    for m in non_basic_muls:
        if getattr(m, 'stat', '') in stat_weight:
            stat_weight[m.stat] += getattr(m, 'scale', 0.0)
    if role == "healer":
        # v5.5: 治疗基数判断（命名 paramId 查 HEAL_REGISTRY 的 stat; 数字编码默认 HP）
        primary = "HP"
        try:
            from engine.core.combat_engine import HEAL_REGISTRY
            for skn in ("skill", "ultimate"):
                sk = skills.get(skn)
                if not sk:
                    continue
                for e in sk.effects:
                    if e.type == "heal" and e.target not in ("memsprite", "self"):
                        pid = getattr(e, 'param_id', '') or ''
                        named = HEAL_REGISTRY.get(pid)
                        if named and named.get("stat") == "ATK":
                            primary = "ATK"  # 灵砂: ATK 基数治疗
                        break
                if primary == "ATK":
                    break
        except ImportError:
            pass
    elif max(stat_weight.values()) > 0:
        primary = max(stat_weight, key=lambda k: stat_weight[k])
    else:
        ts = char.trace_stats or {}
        primary = "HP" if ts.get("HP_percent", 0) > 0 else "ATK"

    # v6.11 阶段0: 直伤权重改总倍率口径（此前按段数×0.5, 任何有攻击动画的角色都吃满直伤权重）
    # 0.25 保底（普攻人人都有）; 每 1000% 总倍率 +0.75 封顶
    direct_w = 0.25 + min(non_basic_total_scale / 1000.0, 0.75)
    if is_break:
        # v5.5: 击破角色直伤为副输出（JSON 占位倍率虚高, 真实输出以击破/超击破为主）
        direct_w *= 0.3
    total_w = direct_w + (0.6 if has_heal else 0) + (0.3 if is_break else 0)
    weights = {
        "direct": direct_w / total_w if total_w > 0 else 1.0,
        "heal": (0.6 / total_w) if has_heal else 0.0,
        "break": (0.3 / total_w) if is_break else 0.0,
        "shield": 0.0, "elation": 0.0,
    }
    elation_w = len([t for t in dmg_types if t == "elation"]) * 0.4
    if elation_w > 0:
        weights["elation"] = elation_w / (total_w + elation_w)
        weights = {k: v * (1 - weights["elation"]) for k, v in weights.items()}
        weights["elation"] = elation_w / (total_w + elation_w)

    # "每超1点" 行迹加成
    spd_per_point = []
    for t in (char.traces or []):
        spd_per_point += _parse_spd_per_point(t.description)

    # v7.3: 换算型行迹比率（"攻击力…等同…速度的720%" → 720.0）
    m_convert = _SPD_CONVERT_RE.search(trace_text)
    spd_convert = float(m_convert.group(1)) if m_convert else 0.0
    # v7.3: 速度信号仅 tank 保底（无换算/阈值/每超1点）→ 只允许跨第一个行动断点
    spd_tank_only = (role == "tank" and spd_convert <= 0 and not spd_per_point
                     and not _SPD_THRESHOLD_RE.search(trace_text))

    # v7.4: 通用换算投资——扫描行迹+技能文本（花火战技"暴击伤害提高等同于花火暴击伤害的24%"）
    kit_text = trace_text + " " + " ".join(
        " ".join(filter(None, (getattr(sk, "desc_text", ""), getattr(sk, "mech_text", ""))))
        for sk in skills.values())
    stat_converts = {}
    for m in _STAT_CONVERT_RE.finditer(kit_text):
        src = _KEYWORD_TO_STAT[m.group(1)]
        stat_converts[src] = max(stat_converts.get(src, 0.0), float(m.group(2)))

    # v7.4: 攻击力阈值行迹 + 每超N点连续换算（火花>2000每超100点欢愉度+5%等）
    atk_per_point, atk_threshold = [], 0.0
    for t in (char.traces or []):
        desc = t.description or ""
        m_t = _ATK_THRESHOLD_RE.search(desc)
        if not m_t:
            continue
        atk_threshold = max(atk_threshold, float(m_t.group(1)))
        m_pp = _ATK_PER_POINT_RE.search(desc)
        if m_pp:
            n_atk, per = int(m_pp.group(1)), float(m_pp.group(3))
            cap_pct = float(m_pp.group(4)) if m_pp.group(4) else None
            atk_per_point.append((_ATK_PP_TARGET[m_pp.group(2)], n_atk, per, cap_pct))

    return CharProfile(
        role=role, primary_stat=primary, scaling_stats={k for k, v in stat_weight.items() if v > 0},
        is_dps=(role == "dps"), has_heal=has_heal, has_shield=has_shield,
        has_debuff=has_debuff, needs_ehr=needs_ehr, is_break=is_break,
        is_elation=any(t == "elation" for t in dmg_types),
        weights=weights, spd_per_point=spd_per_point,
        direct_scale=non_basic_total_scale, dot_scale=dot_total_scale,
        dot_signal=dot_signal, spd_convert=spd_convert, spd_tank_only=spd_tank_only,
        stat_converts=stat_converts, atk_per_point=atk_per_point, atk_threshold=atk_threshold,
    )


# ═══════════════════════════════════════════════════════════════════
# v6.11 阶段1: 个体化副词条权重链（逐角色信号, 不按命途一刀切）
# ═══════════════════════════════════════════════════════════════════

_EHR_CONVERT_RE = re.compile(r"EHR\s*(?:≥|>=|>)\s*\d+\s*%.{0,40}?增伤")
_SPD_CONVERT_RE = re.compile(
    r"(?:攻击力|攻击).{0,14}?(?:等同|等于|为).{0,10}?速度.{0,10}?(\d{3})\s*%")


def _spd_signal_weight(char: Character, profile: CharProfile, trace_text: str) -> float:
    """SPD 三语义定权（项目主 2026-08-19 拍板）:
    - 换算型（攻击力=速度×720%, 阿格莱雅）→ 1.0 持续投入
    - 投入型（SPD≥180 / 每超1点→X）→ 0.4（达标前由约束 mandatory 硬优先=1.0 效果; 达标后 0.4 自由档）
    - tank 保底 0.4（承伤角色速度循环有价值, 其他词条达标后适当分配）
    - 产出型（HP≥50%→速度+40%）/ 无信号 → 0（长夜月/遐蝶不推速度）"""
    if _SPD_CONVERT_RE.search(trace_text):
        return 1.0
    if _SPD_THRESHOLD_RE.search(trace_text) or profile.spd_per_point:
        return 0.4
    if profile.role == "tank":
        return 0.4
    return 0.0


def _compute_substat_weights(char: Character, profile: CharProfile) -> dict:
    """返回 {内部stat key: 权重}。信号全自动提取, JSON substat_weights 可覆盖。"""
    ts = char.trace_stats or {}
    trace_text = " ".join(t.description for t in (char.traces or []))
    w: dict = {}

    # 双暴: dps 且（直伤总量≥300 或 暴伤行迹≥20 或 暴击率行迹≥10）且非 DOT 输出型 → 1.0; 否则 0
    # （海瑟音 DOT 核心: 直伤是载体, 双暴不作用于 DOT 伤害）
    # v7.3: 补暴击率行迹≥10 第三信号——JSON 占位倍率的暴击C（遐蝶224%/长夜月278%/姬子·启行116%）
    # 此前被误杀推纯生命/攻击; 海瑟音暴击率行迹 0 仍排除
    # v7.4: 欢愉主导角色按输出核心对待（欢愉伤害吃暴击）; 盾C（护盾按白值结算, 全库仅丹恒·腾荒
    # ATK 转盾）不堆双暴
    elation_dom = (profile.weights.get("elation", 0.0) > 0
                   and profile.weights["elation"] >= profile.weights.get("direct", 1.0) * 0.9)
    crit = 1.0 if ((profile.role == "dps" or elation_dom) and not profile.dot_signal
                   and (profile.direct_scale >= 300 or ts.get("CRIT_DMG", 0) >= 20
                        or ts.get("CRIT_RATE", 0) >= 10)) else 0.0
    if profile.has_shield:
        crit = 0.0
    w["CRIT_RATE"] = crit
    w["CRIT_DMG"] = crit

    # EHR: 行迹换算 → 1.0; DOT 输出且行迹送命中 → 1.0; 虚无命途/debuffer 定位的
    # debuff 套件 → 0.6; 无 → 0
    # （v7.3: EHR 边际量纲修复后 0.6 有了真实竞争力, 收窄适用面——藿藿/灵砂/流萤等
    # "附带 debuff"的奶/输出不再被推一堆命中, 进剩余均摊池）
    if _EHR_CONVERT_RE.search(trace_text):
        w["EFFECT_HIT_RATE"] = 1.0
    elif profile.dot_scale > 0 and ts.get("EFFECT_HIT_RATE", 0) > 0:
        w["EFFECT_HIT_RATE"] = 1.0
    elif profile.has_debuff and (char.path == "虚无" or profile.role == "debuffer"):
        w["EFFECT_HIT_RATE"] = 0.6
    else:
        w["EFFECT_HIT_RATE"] = 0.0

    # SPD 三语义
    w["SPD_percent"] = _spd_signal_weight(char, profile, trace_text)

    # 主缩放: primary 0.8 / 其他缩放 0.3 / 非缩放 0
    # v7.4: 欢愉主导（欢愉伤害 stat=NONE 不吃白值）→ 白值缩放词条清零（银狼Lv.999/爻光）
    # v7.4: 换算投资角色的其余缩放词条压到 0.3（花火/布洛妮娅的构建核心是换算源属性）
    for st in ("ATK_percent", "HP_percent", "DEF_percent"):
        if elation_dom:
            w[st] = 0.0
        elif st == profile.primary_stat + "_percent":
            w[st] = 0.8 if not profile.stat_converts else 0.3
        elif st in {s + "_percent" for s in profile.scaling_stats}:
            w[st] = 0.3
        else:
            w[st] = 0.0

    # 击破 / 抵抗
    w["BREAK_EFFECT"] = 1.0 if profile.is_break else 0.0
    w["EFFECT_RES"] = 0.6 if profile.role == "tank" else 0.0

    # v7.4: 通用换算投资信号（花火/布洛妮娅爆伤转全队拐、阿格莱雅速度转攻击）→ 源属性 1.0。
    # 仅单属性 1.0（爆伤换算不连带暴击对）
    for src in profile.stat_converts:
        if src in w:
            w[src] = max(w.get(src, 0.0), 1.0)    # JSON 手动覆盖（防未来信号失灵）
    for k, v in (char.substat_weights or {}).items():
        if k in w:
            w[k] = float(v)
    return w


# ═══════════════════════════════════════════════════════════════════
# SPD 阈值约束提取
# ═══════════════════════════════════════════════════════════════════

def _extract_trace_spd_constraints(char: Character) -> list:
    """从行迹文本提取 SPD 阈值约束（正则要求比较符紧邻 SPD/速度）"""
    cons = []
    for t in (char.traces or []):
        desc = t.description or ""
        m = _SPD_THRESHOLD_RE.search(desc)
        if not m:
            continue
        threshold = int(m.group(1))
        reward = _parse_spd_threshold_reward(desc, threshold)
        cons.append(Constraint(StatType.SPD_PERCENT.value, "gte", threshold, reward))
    return cons


def _extract_relic_constraints(relic_sets: dict, pieces: list) -> list:
    """从已装备遗器套装提取阈值约束（condition 码 → 静态表）"""
    if not pieces or not relic_sets:
        return []
    counts = Counter(p.set_name for p in pieces)
    cons = []
    for name, n in counts.items():
        rs = relic_sets.get(name)
        if not rs:
            continue
        for eff in rs.effects:
            if n >= eff.pieces_required and eff.condition in RELIC_CONDITION_CONSTRAINTS:
                cons += RELIC_CONDITION_CONSTRAINTS[eff.condition]
    return cons


def _merge_constraints(cons: list) -> list:
    """同 (stat, op, value) 去重合并 reward"""
    merged = {}
    for c in cons:
        key = (c.stat, c.op, c.value)
        if key in merged:
            for k, v in c.reward.items():
                merged[key].reward[k] = merged[key].reward.get(k, 0.0) + v
        else:
            merged[key] = c
    return list(merged.values())


def _extract_spd_constraints(char: Character, relic_sets: dict = None, pieces: list = None) -> list:
    """汇总行迹 + 遗器套装的阈值约束"""
    cons = _extract_trace_spd_constraints(char)
    cons += _extract_relic_constraints(relic_sets, pieces)
    return _merge_constraints(cons)


def _marginal_benefit_direct(stats: CombatStats, character: Character,
                              stat_type: str, roll_value: float) -> float:
    """计算直伤角色加一条词条的期望伤害增量。

    简化公式: E[damage] ∝ ATK × (1 + dmg_bonus) × (1 + CR × CD)
    """
    cr = stats.CRIT_RATE
    cd = stats.CRIT_DMG
    dmg_bonus = stats.DMG_BONUS_ALL + stats.DMG_BONUS.get(character.element, 0.0)

    # HP 缩放角色用 HP，否则 ATK
    hp_scaler = _is_hp_scaler(character)
    if hp_scaler:
        base_stat = stats.HP
        base_stat_val = stats._base_HP if stats._base_HP > 0 else character.base_HP
        pct_key = "HP_percent"
    else:
        base_stat = stats.ATK
        base_stat_val = stats._base_ATK if stats._base_ATK > 0 else character.base_ATK
        pct_key = "ATK_percent"

    current_expected = base_stat * (1.0 + dmg_bonus) * (1.0 + cr * cd)

    # 模拟加一条词条后
    new_stat, new_cr, new_cd, new_dmg = base_stat, cr, cd, dmg_bonus
    if stat_type == pct_key:
        new_stat += base_stat_val * (roll_value / 100.0)
    elif stat_type == "CRIT_RATE":
        new_cr = min(cr + roll_value / 100.0, 1.0)
    elif stat_type == "CRIT_DMG":
        new_cd += roll_value / 100.0
    elif stat_type == "DMG_BONUS_ALL":
        new_dmg += roll_value / 100.0
    else:
        return 0.0

    new_expected = max(new_stat, 0) * (1.0 + new_dmg) * (1.0 + min(new_cr, 1.0) * new_cd)
    return new_expected - current_expected


def _marginal_benefit_elation(stats: CombatStats, stat_type: str, roll_value: float) -> float:
    """欢愉角色边际效益"""
    el = stats.ELATION_LEVEL
    lb = stats.LAUGH_BOOST
    cr = stats.CRIT_RATE
    cd = stats.CRIT_DMG

    current = (1.0 + el) * (1.0 + lb) * (1.0 + cr * cd)

    new_el, new_cr, new_cd = el, cr, cd
    if stat_type == "ELATION_LEVEL":
        new_el += roll_value / 100.0
    elif stat_type == "LAUGH_BOOST":
        return 0.0  # 仅星魂提供，遗器不产出
    elif stat_type == "CRIT_RATE":
        new_cr = min(cr + roll_value / 100.0, 1.0)
    elif stat_type == "CRIT_DMG":
        new_cd += roll_value / 100.0
    else:
        return 0.0

    new = (1.0 + new_el) * (1.0 + lb) * (1.0 + min(new_cr, 1.0) * new_cd)
    return new - current


# ═══════════════════════════════════════════════════════════════════
# 统一边际效益（10000 行动值窗口总输出量纲）
# ═══════════════════════════════════════════════════════════════════

def _expected_output(stats: CombatStats, char: Character, profile: CharProfile) -> float:
    """10000 AV 窗口内的单次行动期望输出"""
    base = getattr(stats, profile.primary_stat)
    dmg = 1.0 + stats.DMG_BONUS_ALL + stats.DMG_BONUS.get(char.element, 0.0)
    crit = 1.0 + min(stats.CRIT_RATE, 1.0) * stats.CRIT_DMG
    w = profile.weights
    out = w["direct"] * base * dmg * crit
    if w["heal"] > 0:
        # v5.2: 治疗不吃暴击（原式 ×crit 错误鼓励治疗角色堆双暴）
        out += w["heal"] * base * (1.0 + stats.HEAL_BONUS)
    if w["break"] > 0:
        # v5.2: 击破伤害与角色主属性解耦（= 等级基础值 × (1+击破特攻), 与战斗公式一致）
        out += w["break"] * BREAK_BASE_DAMAGE * (1.0 + stats.BREAK_EFFECT)
    if w["elation"] > 0:
        out += w["elation"] * (1.0 + stats.ELATION_LEVEL) * (1.0 + stats.LAUGH_BOOST) * crit
    return out


def _with_roll(stats: CombatStats, stat_type: str, roll_value: float) -> CombatStats:
    """浅拷贝 stats，模拟加一条中档词条"""
    s = copy.copy(stats)
    if stat_type == "CRIT_RATE":
        s.CRIT_RATE = min(s.CRIT_RATE + roll_value / 100.0, 1.0)
    elif stat_type == "CRIT_DMG":
        s.CRIT_DMG += roll_value / 100.0
    elif stat_type == "ATK_percent":
        s.ATK += s._base_ATK * roll_value / 100.0
    elif stat_type == "HP_percent":
        s.HP += s._base_HP * roll_value / 100.0
    elif stat_type == "DEF_percent":
        s.DEF += s._base_DEF * roll_value / 100.0
    elif stat_type == "BREAK_EFFECT":
        s.BREAK_EFFECT += roll_value / 100.0
    elif stat_type == "EFFECT_HIT_RATE":
        s.EFFECT_HIT_RATE += roll_value / 100.0
    elif stat_type == "EFFECT_RES":
        s.EFFECT_RES += roll_value / 100.0
    elif stat_type == "HEAL_BONUS":
        s.HEAL_BONUS += roll_value / 100.0
    elif stat_type == "SPD_percent":
        s.SPD += roll_value  # flat SPD
    return s


def _marginal_benefit(stats: CombatStats, char: Character, profile: CharProfile,
                      stat_type: str, roll_value: float) -> float:
    """统一边际效益：10000 AV 窗口内的期望输出增量（含行动频率）"""
    spd = max(stats.SPD, 1.0)
    e_old = _expected_output(stats, char, profile)

    if stat_type == "SPD_percent":
        # v5.5: 实机回合离散行动次数（星铁 150 行动值回合）: N = floor(150×Spd/10000)
        # v7.3(a方案): 单条收益 = 连投 k 条到下一断点的均值收益——此前逐条口径下距断点
        # 多于 1 条时每条收益恒 0, 贪心永远起步不了（阿格莱雅基础102速到134需11条 → 30条全攻击）
        def _turns(spd_v):
            return int(150.0 * spd_v / AV_PER_TURN)
        n_old = _turns(spd)
        # v7.3: tank 保底速度只跨第一个行动断点（2026-08-19 裁决: 其他词条达标后适当分配）
        if profile.spd_tank_only and n_old >= 2:
            return 0.0
        next_bp = (n_old + 1) * AV_PER_TURN / 150.0  # 下一断点速度值
        k = max(1, math.ceil((next_bp - spd) / roll_value))
        n_new = _turns(spd + k * roll_value)
        # v7.5: 跨档收益衰减——第一档全额, 第二档起 ×0.5（抑制"跨完一档继续追下一档
        # 直到上限"的链条; 与单词条 60% 上限双保险）
        tier_factor = 1.0 if n_old < 2 else 0.5
        gain = e_old * (n_new - n_old) * (AV_PER_TURN / ACTION_WINDOW_AV) / k * tier_factor
        # "每超1点" 行迹加成（连续, 按条计）
        for st, per in profile.spd_per_point:
            if st == StatType.ELATION_LEVEL.value:
                gain += e_old * (roll_value * per) / (1.0 + stats.ELATION_LEVEL)
            else:
                gain += e_old * (roll_value * per)
        # v7.3: 换算型行迹（攻击力等同速度×720%）连续收益——三语义权重 1.0 此前形同虚设
        if profile.spd_convert > 0:
            conv_atk = roll_value * profile.spd_convert / 100.0
            gain += e_old * conv_atk / max(stats.ATK, 1.0)
        return gain

    e_new = _expected_output(_with_roll(stats, stat_type, roll_value), char, profile)
    gain = (e_new - e_old) * (AV_PER_TURN / spd)

    # v7.4: 攻击阈值后的连续换算收益（火花>2000每超100点欢愉度+5% / 刻律德菈每超100点暴伤+18% 等,
    # 与攻击本体收益叠加; 阈值以下不触发——达标由约束 mandatory 硬优先）
    if (stat_type == "ATK_percent" and profile.atk_per_point
            and stats.ATK >= profile.atk_threshold):
        base_atk = getattr(stats, "_base_ATK", 0) or char.base_ATK
        atk_pts = base_atk * roll_value / 100.0
        window = AV_PER_TURN / spd
        for target, n_pts, per, cap_pct in profile.atk_per_point:
            delta = atk_pts / n_pts * per / 100.0
            if cap_pct is not None:
                delta = min(delta, cap_pct / 100.0)
            if target == "ELATION_LEVEL":
                gain += e_old * delta / (1.0 + stats.ELATION_LEVEL) * window
            elif target == "CRIT_DMG":
                gain += e_old * min(stats.CRIT_RATE, 1.0) * delta * window
            elif target == "BREAK_EFFECT":
                gain += e_old * profile.weights.get("break", 0.0) * delta \
                    / (1.0 + stats.BREAK_EFFECT) * window
            elif target == "HEAL_BONUS":
                gain += e_old * profile.weights.get("heal", 0.0) * delta \
                    / (1.0 + stats.HEAL_BONUS) * window

    # v7.4: 通用换算投资收益（爆伤转全队拐等）——源属性本体对自身输出的贡献已计入
    # e_new-e_old, 此处补团队放大份额: 每条源属性词条 × 换算比率 × 受益队友数(≈3)。
    # 花火 CD 24% 换算: 每条 ≈ 5.8%×24% = 1.4% 团队暴伤, 压过自身生命/攻击词条
    conv_pct = profile.stat_converts.get(stat_type)
    if conv_pct:
        gain += e_old * 3.0 * (roll_value / 100.0) * (conv_pct / 100.0) * (AV_PER_TURN / spd)

    # EHR：debuff 覆盖率近似
    # v7.3: 兜底式补行动窗口因子（此前少乘 AV/SPD, 量纲差百倍——黑天鹅 ATK: EHR
    # = 1897:6.75, 全部虚无/DOT/debuffer 角色一条命中都推不到）
    if stat_type == "EFFECT_HIT_RATE" and profile.needs_ehr:
        gain = max(gain, e_old * 0.3 * (roll_value / 100.0)
                   / (1.0 + stats.EFFECT_HIT_RATE) * (AV_PER_TURN / spd))
    # EFFECT_RES：生存向低价值兜底（同补窗口因子）
    elif stat_type == "EFFECT_RES":
        gain = e_old * 0.1 * (roll_value / 100.0) \
            / (1.0 + stats.EFFECT_RES) * (AV_PER_TURN / spd)

    return gain


def _distribute_marginal(character: Character, mains: dict,
                          effective_stats: list[str], total_rolls: int,
                          mandatory: dict[str, int], rewards: dict,
                          constraints: list[Constraint] = None,
                          crit_target: float = 0.0,
                          target_atk: float = 0.0,
                          external_atk_pct: float = 0.0,
                          external_spd_pct: float = 0.0,
                          lc_base_atk: float = 0.0,
                          lc_base_hp: float = 0.0,
                          *, profile: CharProfile = None,
                          base_stats: CombatStats = None,
                          stat_caps: dict[str, int] = None,
                          weights: dict[str, float] = None) -> dict[str, int]:
    """按边际效益分配剩余词条。

    profile/base_stats/per_stat_cap 给出时使用统一边际效益（含SPD行动频率）；
    否则使用旧逻辑（保持测试兼容）。
    """
    distribution = dict(mandatory)
    use_new = profile is not None and base_stats is not None
    _b = lambda d, r: _build_stats_for_marginal(
        character, mains, d, r, external_atk_pct, external_spd_pct, lc_base_atk, lc_base_hp)
    if use_new:
        _b = lambda d, r: _build_stats_with_subs(base_stats, d)

    # === 暴击率满爆约束 ===
    CR_KEY = StatType.CRIT_RATE.value
    if crit_target > 0 and CR_KEY in effective_stats:
        temp = _b(distribution, rewards)
        current_cr = temp.CRIT_RATE
        if current_cr < crit_target:
            deficit = crit_target - current_cr
            roll_val = _mid(CR_KEY)
            if roll_val > 0:
                needed = int(deficit / (roll_val / 100.0)) + 1
                cap_limit = _cap(CR_KEY, mains)
                cr_mandatory = min(needed, cap_limit)
                if CR_KEY not in distribution:
                    distribution[CR_KEY] = 0
                distribution[CR_KEY] = max(distribution[CR_KEY], cr_mandatory)

    # === ATK目标约束 ===
    ATK_KEY = StatType.ATK_PERCENT.value
    if target_atk > 0 and ATK_KEY in effective_stats:
        temp = _b(distribution, rewards)
        current_atk = temp.ATK
        if current_atk < target_atk:
            deficit = target_atk - current_atk
            roll_val = character.base_ATK * (_mid(ATK_KEY) / 100.0)
            if roll_val > 0:
                needed = int(deficit / roll_val) + 1
                cap_limit = _cap(ATK_KEY, mains)
                atk_mandatory = min(needed, cap_limit)
                if ATK_KEY not in distribution:
                    distribution[ATK_KEY] = 0
                distribution[ATK_KEY] = max(distribution[ATK_KEY], atk_mandatory)

    allocated = sum(distribution.values())
    remaining = total_rolls - allocated
    if remaining <= 0:
        return distribution

    # 取有效词条中可通过遗器副词条产出的; v6.11 阶段1: 权重=0 的词条移出分配池
    sub_values = {s.value for s in SUB_STAT_TYPES}
    allocatable = [s for s in effective_stats if s in sub_values]
    if weights is not None:
        allocatable = [s for s in allocatable if weights.get(s, 0.0) > 0.0]
    caps = {s: _cap(s, mains) for s in allocatable}
    caps = {s: c for s, c in caps.items() if c > 0}

    if not caps:
        return distribution

    # 检查 SPD 上限约束
    spd_caps = {}
    if constraints:
        for c in constraints:
            if c.op in ("lt", "lte"):
                # 限制 SPD 分配不超过阈值
                current = base_stats.SPD if base_stats is not None else character.base_SPD
                if base_stats is None:
                    for slot, mt in mains.items():
                        mv = mt.value if hasattr(mt, 'value') else mt
                        if mv == c.stat:
                            current += _main_val(slot, mt)
                max_from_subs = max(0, c.value - current)
                max_rolls = int(max_from_subs / _mid(c.stat)) if _mid(c.stat) > 0 else 999
                spd_caps[c.stat] = max(0, max_rolls)

    # 构建临时 stats 用于边际计算
    temp_stats = _b(distribution, rewards)

    # 每属性上限：per_stat_cap 给出则每件5次强化模型，否则旧 _cap
    if stat_caps is not None:
        caps = {s: stat_caps.get(s, 0) for s in allocatable}

    for _ in range(remaining):
        best_stat = None
        best_gain = -1

        for st in allocatable:
            current_alloc = distribution.get(st, 0)
            cap = caps.get(st, 999)
            spd_cap = spd_caps.get(st, 999)
            effective_cap = min(cap, spd_cap)
            if current_alloc >= effective_cap:
                continue

            roll_val = _mid(st)
            if roll_val <= 0:
                continue

            # 计算边际收益
            if use_new:
                gain = _marginal_benefit(temp_stats, character, profile, st, roll_val)
            elif character.path == "欢愉":
                gain = _marginal_benefit_elation(temp_stats, st, roll_val)
            else:
                gain = _marginal_benefit_direct(temp_stats, character, st, roll_val)

            # 暴击率≥目标时不再分配（防止溢出浪费）
            if crit_target > 0 and st == CR_KEY:
                cr_after = temp_stats.CRIT_RATE + roll_val / 100.0
                if cr_after > crit_target + 0.001:
                    continue  # 溢出，跳过

            # v6.11 阶段1: 边际收益×个体化权重（权重 0 已从 allocatable 剔除）
            score = gain * (weights.get(st, 1.0) if weights is not None else 1.0)
            if score > best_gain:
                best_gain = score
                best_stat = st

        if best_stat is None:
            # 全到 cap 但还有剩余 → 塞入主缩放属性
            if use_new:
                fill = profile.primary_stat + "_percent"  # "HP_percent"（内部 key 首字母大写）
                if fill in distribution and distribution[fill] < caps.get(fill, 0):
                    distribution[fill] += 1
                    temp_stats = _b(distribution, rewards)
                    continue
            break

        distribution[best_stat] = distribution.get(best_stat, 0) + 1
        temp_stats = _b(distribution, rewards)

    return distribution


def _build_stats_for_marginal(character: Character, mains: dict,
                               distribution: dict[str, int], rewards: dict,
                               external_atk_pct: float = 0.0,
                               external_spd_pct: float = 0.0,
                               lc_base_atk: float = 0.0,
                               lc_base_hp: float = 0.0) -> CombatStats:
    """构建用于边际计算的简化 CombatStats"""
    stats = CombatStats()
    total_base_atk = character.base_ATK + lc_base_atk
    total_base_hp = character.base_HP + lc_base_hp
    stats._base_ATK = total_base_atk
    stats._base_HP = total_base_hp

    # 基础属性
    stats.ATK = total_base_atk
    stats.HP = total_base_hp
    stats.SPD = character.base_SPD

    # 外部加成（LC百分比/套装）
    stats.ATK += total_base_atk * (external_atk_pct / 100.0)
    stats.SPD *= (1.0 + external_spd_pct / 100.0)

    # 行迹
    for k, v in character.trace_stats.items():
        if k == "ATK_percent":
            stats.ATK += character.base_ATK * (v / 100.0)
        elif k == "CRIT_RATE":
            stats.CRIT_RATE += v / 100.0
        elif k == "CRIT_DMG":
            stats.CRIT_DMG += v / 100.0
        elif k == "SPD_percent":
            stats.SPD += v  # flat SPD from traces

    # 主词条
    for slot, mt in mains.items():
        val = _main_val(slot, mt)
        st = mt.value if hasattr(mt, 'value') else mt
        if st == "ATK_percent":
            stats.ATK += total_base_atk * (val / 100.0)
        elif st == "HP_percent":
            stats.HP += total_base_hp * (val / 100.0)
        elif st == "CRIT_RATE":
            stats.CRIT_RATE += val / 100.0
        elif st == "CRIT_DMG":
            stats.CRIT_DMG += val / 100.0
        elif st == "SPD_percent":
            stats.SPD += val

    # 副词条
    for st, count in distribution.items():
        roll_val = _mid(st)
        for _ in range(count):
            if st == "ATK_percent":
                stats.ATK += total_base_atk * (roll_val / 100.0)
            elif st == "CRIT_RATE":
                stats.CRIT_RATE += roll_val / 100.0
            elif st == "CRIT_DMG":
                stats.CRIT_DMG += roll_val / 100.0
            elif st == "SPD_percent":
                stats.SPD += roll_val
            elif st == "HP_percent":
                stats.HP += total_base_hp * (roll_val / 100.0)

    # 应用 reward
    for k, v in rewards.items():
        if k == "CRIT_RATE":
            stats.CRIT_RATE += v / 100.0
        elif k == "CRIT_DMG":
            stats.CRIT_DMG += v / 100.0
        elif k == "ELATION_LEVEL":
            stats.ELATION_LEVEL += v / 100.0
        elif k == "ATK_percent":
            stats.ATK += character.base_ATK * (v / 100.0)

    stats.CRIT_RATE = min(stats.CRIT_RATE, 1.0)
    return stats


def _build_relics(mains: dict, distribution: dict[str, int]) -> list[RelicPiece]:
    """按主词条+副词条分配构建6件遗器。不追求均匀，有位置塞即可。"""
    pieces = []
    subs_on_piece = {slot: {} for slot in RELIC_SLOTS_ORDER}
    remaining_slots = {slot: 4 for slot in RELIC_SLOTS_ORDER}

    # 按优先级排序词条 → 贪心塞入可用槽位
    sorted_stats = sorted(distribution.keys(),
                          key=lambda s: DEFAULT_PRIORITY.get(s, 5), reverse=True)

    for st in sorted_stats:
        need = distribution[st]
        for slot in RELIC_SLOTS_ORDER:
            if need <= 0:
                break
            mv = mains[slot].value if hasattr(mains[slot], 'value') else mains[slot]
            if mv == st:  # 主词条互斥
                continue
            if st in subs_on_piece[slot]:
                continue
            if remaining_slots[slot] <= 0:
                continue
            subs_on_piece[slot][st] = _mid(st)
            remaining_slots[slot] -= 1
            need -= 1

    for slot in RELIC_SLOTS_ORDER:
        mt = mains[slot]
        mv = _main_val(slot, mt)
        pieces.append(RelicPiece(
            slot=slot, set_name="优化套装", rarity=5, level=15,
            main_stat_type=mt.value if hasattr(mt, 'value') else mt,
            main_stat_value=mv,
            sub_stats=subs_on_piece[slot],
        ))
    return pieces


def optimize_relics(
    character: Character,
    effective_stats: list[str],
    total_rolls: int = 30,
    constraints: list[Constraint] = None,
    lightcone=None,
    relic_sets: dict = None,
    active_buffs: list[dict] = None,
    crit_target: float = 1.0,
    target_atk: float = 0.0,
    external_atk_pct: float = 0.0,
    external_spd_pct: float = 0.0,
    external_elation: float = 0.0,
    lc_base_atk: float = 0.0,
    lc_base_hp: float = 0.0,
) -> RelicBuild:
    """一键优化遗器配装（v2 条件约束 + 边际效益）。

    crit_target: 暴击率目标，主C默认1.0(100%)。设0则不强制满爆。
    target_atk: ATK目标值。external_atk/spd_pct: LC/套装外部加成(%).
    """
    cons = constraints or []

    # 0. 提取约束奖励（先于主词条选择）
    prelim_rewards = {}
    for c in cons:
        for k, v in c.reward.items():
            prelim_rewards[k] = prelim_rewards.get(k, 0.0) + v

    # 1. 主词条（考虑SPD上限约束 + 满爆目标 + ATK目标）
    mains = _pick_main_stats(character, effective_stats, cons, prelim_rewards, crit_target)

    # 若target_atk>0，强制绳选ATK%（如果不冲突）
    if target_atk > 0 and StatType.ATK_PERCENT in RELIC_MAIN_STAT_POOL.get("link_rope", []):
        mains["link_rope"] = StatType.ATK_PERCENT

    # 2. 满足阈值约束
    mandatory, rewards = _solve_constraints(character, mains, cons)

    # 3. 边际效益分配
    distribution = _distribute_marginal(
        character, mains, effective_stats, total_rolls,
        mandatory, rewards, cons, crit_target, target_atk,
        external_atk_pct, external_spd_pct, lc_base_atk, lc_base_hp,
    )

    # 4. 构建遗器
    pieces = _build_relics(mains, distribution)

    # 5. 完整计算（含所有主词条 + 副词条 + reward）
    # 先创建带 reward 的 buff
    import copy
    buffs = list(active_buffs) if active_buffs else []
    if rewards:
        buffs.append({"id": "constraint_reward", "attributes": rewards})

    stats = compute_combat_stats(character, lightcone, pieces, relic_sets, buffs)

    return RelicBuild(
        pieces={p.slot: p for p in pieces},
        total_effective_rolls=total_rolls,
        effective_stats=effective_stats,
        final_stats=stats,
    )


# ═══════════════════════════════════════════════════════════════════
# 副词条推荐主入口（/api/recommend 使用，契约 8 键）
# ═══════════════════════════════════════════════════════════════════

def _roll_cap(stat: str, mains: dict, total_rolls: int) -> int:
    """每件最多5次强化 × (6件 - 主词条冲突); 上限 total_rolls"""
    conflict = sum(1 for m in mains.values()
                   if (m == stat or getattr(m, 'value', None) == stat))  # v5.5: 兼容 StatType/str
    return min(total_rolls, 5 * (6 - conflict))


def _build_stats_with_subs(base: CombatStats, distribution: dict) -> CombatStats:
    """在 compute_combat_stats 基态（含主词条/套装/光锥）上追加中档副词条"""
    s = copy.copy(base)
    for st, cnt in distribution.items():
        v = _mid(st) * cnt
        if st == "ATK_percent":
            s.ATK += s._base_ATK * v / 100.0
        elif st == "HP_percent":
            s.HP += s._base_HP * v / 100.0
        elif st == "DEF_percent":
            s.DEF += s._base_DEF * v / 100.0
        elif st == "CRIT_RATE":
            s.CRIT_RATE = min(s.CRIT_RATE + v / 100.0, 1.0)
        elif st == "CRIT_DMG":
            s.CRIT_DMG += v / 100.0
        elif st == "BREAK_EFFECT":
            s.BREAK_EFFECT += v / 100.0
        elif st == "EFFECT_HIT_RATE":
            s.EFFECT_HIT_RATE += v / 100.0
        elif st == "EFFECT_RES":
            s.EFFECT_RES += v / 100.0
        elif st == "SPD_percent":
            s.SPD += v
    return s


def _trim_mandatory(mandatory: dict, constraints: list, total_rolls: int) -> dict:
    """强制词条超预算时按奖励价值裁剪（保高价值约束）"""
    if sum(mandatory.values()) <= total_rolls:
        return mandatory
    # 按约束奖励总价值排序保留
    reward_value = {}
    for c in constraints:
        if c.stat in mandatory:
            reward_value[c.stat] = reward_value.get(c.stat, 0) + sum(c.reward.values())
    trimmed = {}
    for st in sorted(mandatory, key=lambda s: -reward_value.get(s, 0)):
        take = min(mandatory[st], total_rolls - sum(trimmed.values()))
        if take > 0:
            trimmed[st] = take
        if sum(trimmed.values()) >= total_rolls:
            break
    return trimmed


_STAT_LABEL = {"SPD_percent": "速度", "EFFECT_RES": "效果抵抗",
               "EFFECT_HIT_RATE": "效果命中", "ATK_percent": "攻击力"}
# v6.11 阶段2: 毕业词条数按定位分档（项目主口径: dps 30~35 / 副C 28~32 / 辅助 25~28 / 击破 30+）
_GRADUATION_TARGET = {"dps": (30, 35), "break": (30, 35), "tank": (25, 28),
                      "healer": (25, 28), "shielder": (25, 28), "support": (25, 28),
                      "debuffer": (25, 28), "unknown": (25, 28)}


def recommend_substats_full(char: Character, lc=None, pieces=None, relic_sets=None,
                            effective_rolls: int = 30,
                            total_rolls: int = TOTAL_ROLLS) -> dict:
    """v6.11 阶段2 + v7.3/7.3.1: 完整推荐响应——rolls + weights + constraints + graduation。
    v7.3.1 口径（项目主纠正）: 总词条固定 50; `effective_rolls` 是用户可调的有效词条数
    （默认 30, 上限 50）——有效词条分配至上限后, 剩余（50−有效）在权重=0 的非有效
    词条间最大余数法均摊（受 roll_cap 约束）。recommend_substats 返回 9 键契约 rolls。"""
    profile = _analyze_character(char, lc, pieces, relic_sets)
    cons = _extract_spd_constraints(char, relic_sets, pieces)
    mains = {}
    for p in (pieces or []):
        mains[p.slot] = p.main_stat_type
    base = compute_combat_stats(char, lc, pieces or [], relic_sets or {})

    # v5.5: 击破角色策略配置（流萤: 完全燃烧四动速度达标 + 放弃攻击词条）
    break_cfg = BREAK_CHAR_CONFIG.get(char.id, {})
    spd_target = break_cfg.get("spd_target")
    if spd_target:
        cons = list(cons) + [Constraint(StatType.SPD_PERCENT.value, "gte", spd_target, {})]
    # v7.4: 攻击力阈值行迹 → ATK 约束（火花>2000 等）; exclude_atk 角色（流萤, 用户裁决
    # 放弃攻击词条）不加, 阈值换算不推翻既有裁决
    if profile.atk_threshold > 0 and not break_cfg.get("exclude_atk"):
        cons = list(cons) + [Constraint(StatType.ATK_PERCENT.value, "gte",
                                       profile.atk_threshold, {})]

    mandatory, rewards = _solve_constraints(
        char, mains, cons, strict_lt=True,
        cap_fn=lambda s, m: _roll_cap(s, m, total_rolls), base_stats=base,
    )
    mandatory = _trim_mandatory(mandatory, cons, effective_rolls)

    # 内部分配池：统一用 StatType.value（SPD_percent 小写 p）; EHR 已随 9 键契约入池
    internal_keys = [_FRONTEND_TO_INTERNAL[k] for k in FRONTEND_ROLL_KEYS]
    if break_cfg.get("exclude_atk"):
        internal_keys = [k for k in internal_keys if k != "ATK_percent"]
        mandatory.pop("ATK_percent", None)
    pool = [s for s in internal_keys if _roll_cap(s, mains, total_rolls) > 0]
    # v6.11 阶段1: 个体化权重链——权重=0 的词条移出分配池（tank 双暴/EHR 无信号/SPD 无需求等）
    sub_weights = _compute_substat_weights(char, profile)
    pool = [s for s in pool if sub_weights.get(s, 0.0) > 0.0]

    # v7.5: 单词条占有效词条数上限 60%（实机一件最多 4 种副词条类型, 全堆一个属性
    # 不真实——银狼Lv.999"30 条全速度"问题）; mandatory 约束达标不受此限（配装需求）
    stat_caps = {s: min(_roll_cap(s, mains, total_rolls),
                        math.ceil(effective_rolls * SUBSTAT_SINGLE_SHARE)) for s in pool}
    dist = _distribute_marginal(
        char, mains, pool, effective_rolls, mandatory, rewards, cons,
        profile=profile, base_stats=base, stat_caps=stat_caps,
        weights=sub_weights,
    )

    # 内部 key → 前端契约 key
    result = {}
    for fk in FRONTEND_ROLL_KEYS:
        ik = _FRONTEND_TO_INTERNAL[fk]
        result[fk] = dist.get(ik, 0)

    # v7.3 裁决: 剩余词条在非有效词条（权重=0）间平均分配（最大余数法, cap 内均摊）
    leftover = total_rolls - sum(result.values())
    spread_total = 0
    if leftover > 0:
        idle_keys = [_FRONTEND_TO_INTERNAL[fk] for fk in FRONTEND_ROLL_KEYS
                     if sub_weights.get(_FRONTEND_TO_INTERNAL[fk], 0.0) <= 0.0]
        share, extra = divmod(leftover, len(idle_keys)) if idle_keys else (0, 0)
        for i, ik in enumerate(idle_keys):
            fk = _INTERNAL_TO_FRONTEND[ik]
            add = share + (1 if i < extra else 0)
            room = max(0, _roll_cap(ik, mains, total_rolls) - result.get(fk, 0))
            add = min(add, room)
            result[fk] = result.get(fk, 0) + add
            spread_total += add

    # 兜底：非有效词条也塞满后仍有缺口 → 塞入主缩放属性（保留旧语义）
    gap = total_rolls - sum(result.values())
    if gap > 0:
        primary_key = profile.primary_stat + "_percent"
        fill_order = [primary_key] + [s for s in pool if s != primary_key]
        for internal_key in fill_order:
            front_key = _INTERNAL_TO_FRONTEND.get(internal_key)
            if front_key is None:
                continue
            available = _roll_cap(internal_key, mains, total_rolls) - result.get(front_key, 0)
            if available <= 0:
                continue
            added = min(gap, available)
            result[front_key] += added
            gap -= added
            if gap == 0:
                break

    # ── v6.11 阶段2: 约束面板与毕业度 ──
    constraints_info = []
    for c in cons:
        if c.stat == StatType.SPD_PERCENT.value:
            current = base.SPD
        elif c.stat == StatType.ATK_PERCENT.value:
            current = base.ATK  # v7.4: ATK 阈值=面板攻击值
        elif c.stat in (StatType.EFFECT_RES.value, StatType.EFFECT_HIT_RATE.value):
            current = getattr(base, c.stat, 0.0) * 100.0
        else:
            current = getattr(base, c.stat, 0.0)
        if c.op in ("lt", "lte"):
            met = current < c.value
            suggest = 0  # 上限约束不给建议条数
        else:
            met = current >= c.value
            need = max(0.0, c.value - current)
            mid = _mid(c.stat)
            if c.stat == StatType.ATK_PERCENT.value and mid > 0:
                mid = (getattr(base, "_base_ATK", 0) or char.base_ATK) * mid / 100.0  # v7.4: 点数口径
            suggest = int(need / mid) + (1 if need % mid > 0 else 0) if mid > 0 else 0
        op_label = "≥" if c.op in ("gte", "gt") else "＜"
        constraints_info.append({
            "name": f"{_STAT_LABEL.get(c.stat, c.stat)}{op_label}{c.value:.0f}",
            "stat": c.stat, "op": c.op, "threshold": c.value,
            "current": round(current, 1), "met": met, "suggest_rolls": suggest,
        })

    lo, hi = _GRADUATION_TARGET.get(profile.role, (25, 28))
    if profile.is_break:
        lo, hi = 30, 35
    # v7.3: 有效/非有效分口径统计——有效=优化器分配（约束达标+贪心+兜底回填）,
    # 非有效=均摊份额（流萤约束达标的速度此前被权重口径误计入非有效）
    # v7.3.1: budget=总词条(50 固定), effective_budget=用户可调有效词条数
    effective_used = sum(result.values()) - spread_total
    invalid_used = spread_total
    graduation = {
        "effective_used": effective_used,
        "effective_budget": effective_rolls,
        "budget": total_rolls,
        "invalid_used": invalid_used,
        "score_pct": min(100, round(effective_used / lo * 100)) if lo else 100,
        "gap_to_target": max(0, lo - effective_used),
        "target_range": [lo, hi],
    }

    return {"rolls": result, "weights": sub_weights,
            "constraints": constraints_info, "graduation": graduation}


def recommend_substats(char: Character, lc=None, pieces=None, relic_sets=None,
                       effective_rolls: int = 30,
                       total_rolls: int = TOTAL_ROLLS) -> dict:
    """唯一推荐入口（9 键契约）——完整响应见 recommend_substats_full。
    effective_rolls: 有效词条数（用户可调, 默认 30, 上限 50）; 总词条固定 50。"""
    return recommend_substats_full(char, lc, pieces, relic_sets,
                                   effective_rolls, total_rolls)["rolls"]
