"""角色、技能、行迹、星魂数据模型"""
import json
from dataclasses import dataclass, field
from typing import Optional
from engine.models.memsprite import MemSprite
from engine.constants import PATH_TAUNT_VALUES


@dataclass
class SkillMultiplier:
    """技能倍率"""
    stat: str           # 缩放属性: "ATK" / "HP" / "DEF"
    scale: float        # 倍率%，如 180.0 表示 180% 攻击力
    damage_type: str    # DamageType 的值: "direct" / "dot" / "break" / ...
    element: str        # Element 的值，决定伤害属性，默认空字符串表示继承角色元素
    hits: int = 1       # 弹射段数（JSON `_hits`，v5.3: 弹射技能逐跳倍率）
    target: str = ""    # 可选逐倍率目标: ""=技能target, "single_enemy"/"adjacent"（v5.3: 扩散分倍率）
    per_yizhi: bool = False  # 每点忆质倍率（JSON `_per_yizhi`，v5.6.1: 长夜月迷梦 12%×忆质）
    split: bool = False      # 总倍率由本段目标均分（JSON `_split`）


@dataclass
class SkillEffect:
    """技能附加效果"""
    type: str           # "toughness_reduction" / "buff" / "debuff" / "energy_regen" / "action_advance" / "heal" / "shield" / "cleanse" / "crowd_control"
    target: str         # TargetType 的值
    value: float        # 效果数值
    param_id: str = ""  # 引用的 Buff/Debuff 模板 ID


@dataclass
class Skill:
    """技能"""
    name: str
    type: str                          # SkillType 的值
    cost: dict = field(default_factory=dict)        # {"energy": 130} 或 {"skill_points": 1}
    target: str = "single_enemy"       # TargetType 的值
    multipliers: list = field(default_factory=list)  # list[SkillMultiplier]
    effects: list = field(default_factory=list)      # list[SkillEffect]
    technique_category: str = ""       # 仅秘技使用: "battle_start" / "support"
    desc_text: str = ""                # v7.4: JSON `_description` 原文（推荐器换算信号扫描用）
    mech_text: str = ""                # v7.4: JSON `_mechanism` 原文/递归拼接（同上）


def _flatten_json_text(value) -> str:
    """递归拼接 dict/list 中的字符串叶子（_mechanism 可能是嵌套 dict）"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "；".join(_flatten_json_text(v) for v in value.values())
    if isinstance(value, list):
        return "；".join(_flatten_json_text(v) for v in value)
    return ""


@dataclass
class Trace:
    """行迹"""
    name: str
    description: str
    hook_name: str = ""   # 对应 engine/hooks/characters/ 中的 Hook 函数名，空白表示纯数值被动


@dataclass
class Eidolon:
    """星魂"""
    rank: int            # 1-6
    name: str
    description: str
    hook_name: str = ""  # 对应 Hook 函数名


@dataclass
class Character:
    """角色完整数据"""
    # 基础信息
    id: str                     # 唯一标识，如 "jingliu"
    name: str                   # 角色名
    element: str                # Element 值
    path: str                   # Path 值
    level: int = 80
    ascension: int = 6          # 突破等级 0-6

    # 基础属性 (80级 + 满突破)
    base_HP: float = 0.0
    base_ATK: float = 0.0
    base_DEF: float = 0.0
    base_SPD: float = 0.0
    taunt: int = 100
    max_energy: int = 120       # 终结技能量上限
    energy_type: str = "regular"  # v6.10: regular=常规能量(开局50%) / special=特殊进度(开局0)

    # 行迹属性加成（已解锁的属性节点，永久加成）
    trace_stats: dict = field(default_factory=dict)  # {StatType: value}

    # 技能
    skills: dict = field(default_factory=dict)  # {"basic_attack": Skill, "skill": Skill, ...}
    traces: list = field(default_factory=list)  # list[Trace]
    eidolons: list = field(default_factory=list)  # list[Eidolon]

    # 欢愉
    cast_number: int = 0           # 参演编号（0 表示非欢愉角色）
    # v5.0 P8: 废弃预留 — 引擎无消费点（欢愉加成走 elation.py 专属面板）, 保留防 JSON 兼容风险
    elation_stats: dict = field(default_factory=dict)  # 欢愉角色额外属性 {StatType: value}

    # 记忆
    memsprite: Optional[MemSprite] = None  # 忆灵（仅记忆角色）

    # 黄金裔（昔涟献予之诗/岁月的旅人用）
    gold_offspring: bool = None  # None=JSON未标记(走ID集合兜底); True/False=JSON权威

    # 阿格莱雅至高之姿: 每层衣匠速度层使自身速度提高百分比（v5.7 数据化, 默认15%兜底）
    sovereign_spd_pct: float = 15.0

    # v6.11 阶段1: 副词条权重手动覆盖（可选, 防未来特殊角色信号失灵）{stat: 0~1.0}
    substat_weights: dict = field(default_factory=dict)

    def get_skill(self, skill_type: str) -> Optional[Skill]:
        return self.skills.get(skill_type)

    @staticmethod
    def from_json(filepath: str) -> "Character":
        """从 JSON 文件加载角色。"""
        with open(filepath, "r", encoding="utf-8") as f:
            return Character.from_dict(json.load(f))

    @staticmethod
    def from_dict(data: dict) -> "Character":
        """从已解析的 JSON 对象加载角色，便于无文件 I/O 的测试和 API。"""

        skills = {}
        for key, skill_data in data.get("skills", {}).items():
            multipliers = [
                SkillMultiplier(
                    stat=m["stat"],
                    scale=m["scale"],
                    damage_type=m.get("damageType", "direct"),
                    element=m.get("element", data["element"]),
                    hits=m.get("_hits", 1),
                    # Older character data used _target for per-multiplier routing.
                    # Keep it as a compatibility alias so bounce segments do not
                    # silently fall back to the skill-level target.
                    target=m.get("target", m.get("_target", "")),
                    per_yizhi=bool(m.get("_per_yizhi", False)),
                    split=bool(m.get("_split", False)),
                )
                for m in skill_data.get("multipliers", [])
            ]
            effects = [
                SkillEffect(
                    type=e["type"],
                    target=e.get("target", "single_enemy"),
                    value=e.get("value", 0),
                    param_id=e.get("paramId", ""),
                )
                for e in skill_data.get("effects", [])
            ]
            skills[key] = Skill(
                name=skill_data["name"],
                type=skill_data["type"],
                cost=skill_data.get("cost", {}),
                target=skill_data.get("target", "single_enemy"),
                multipliers=multipliers,
                effects=effects,
                technique_category=skill_data.get("technique_category", ""),
                desc_text=skill_data.get("_description", "") or "",
                mech_text=_flatten_json_text(skill_data.get("_mechanism", "")),
            )

        traces = [
            Trace(name=t.get("name", ""), description=t.get("description", ""), hook_name=t.get("hook_name", ""))
            for t in data.get("traces", [])
        ]
        eidolons = [
            Eidolon(rank=e.get("rank", i+1), name=e.get("name", ""), description=e.get("description", ""), hook_name=e.get("hook_name", ""))
            for i, e in enumerate(data.get("eidolons", []))
        ]

        return Character(
            id=data["id"],
            name=data["name"],
            element=data["element"],
            path=data["path"],
            level=data.get("level", 80),
            ascension=data.get("ascension", 6),
            base_HP=data.get("base_HP", 0),
            base_ATK=data.get("base_ATK", 0),
            base_DEF=data.get("base_DEF", 0),
            base_SPD=data.get("base_SPD", 0),
            taunt=data.get("taunt", PATH_TAUNT_VALUES.get(data.get("path", ""), 100)),
            max_energy=data.get("max_energy", 120),
            energy_type=data.get("energy_type", "regular"),
            trace_stats=data.get("trace_stats", {}),
            skills=skills,
            traces=traces,
            eidolons=eidolons,
            cast_number=data.get("cast_number", 0),
            elation_stats=data.get("elation_stats", {}),
            memsprite=_load_memsprite(data.get("memsprite")),
            gold_offspring=data.get("gold_offspring"),
            sovereign_spd_pct=data.get("sovereign_spd_pct", 15.0),
            substat_weights=data.get("substat_weights", {}),
        )


def _load_memsprite(data: Optional[dict]) -> Optional[MemSprite]:
    """从 JSON 数据加载忆灵"""
    if not data:
        return None

    skills = {}
    for key, skill_data in data.get("skills", {}).items():
        multipliers = [
            SkillMultiplier(
                stat=m["stat"],
                scale=m["scale"],
                damage_type=m.get("damageType", "direct"),
                element=m.get("element", data.get("element", "")),
                hits=m.get("_hits", 1),
                target=m.get("target", ""),
                per_yizhi=bool(m.get("_per_yizhi", False)),
                split=bool(m.get("_split", False)),
            )
            for m in skill_data.get("multipliers", [])
        ]
        effects = [
            SkillEffect(
                type=e["type"],
                target=e.get("target", "single_enemy"),
                value=e.get("value", 0),
                param_id=e.get("paramId", ""),
            )
            for e in skill_data.get("effects", [])
        ]
        skills[key] = Skill(
            name=skill_data["name"],
            type=skill_data["type"],
            cost=skill_data.get("cost", {}),
            target=skill_data.get("target", "single_enemy"),
            multipliers=multipliers,
            effects=effects,
            technique_category=skill_data.get("technique_category", ""),
        )

    return MemSprite(
        name=data.get("name", ""),
        element=data.get("element", ""),
        base_HP=data.get("base_HP", 0),
        base_ATK=data.get("base_ATK", 0),
        base_DEF=data.get("base_DEF", 0),
        base_SPD=data.get("base_SPD", 0),
        max_taunt=data.get("max_taunt", 100),
        skills=skills,
        inherit_ratios=data.get("inherit_ratios", {}),
        position_offset=data.get("position_offset", -1),
        is_backup=data.get("is_backup", False),
    )


def load_character(character_id: str, data_dir: str = "data/characters") -> Character:
    """便捷函数：按 ID 加载角色"""
    filepath = f"{data_dir}/{character_id}.json"
    return Character.from_json(filepath)
