"""光锥、遗器数据模型"""
import json
from dataclasses import dataclass, field


@dataclass
class LightConeEffect:
    """光锥效果"""
    type: str            # "permanent_buff" / "conditional_buff" / "trigger_effect"
    condition: str = ""  # 条件描述或表达式
    attributes: dict = field(default_factory=dict)  # {StatType: value}, value 使用游戏内的百分比数值（如 40 表示 40%）
    param_id: str = ""   # 光锥触发器ID（用于LC_TRIGGERS注册表匹配）
    condition_code: str = ""  # v5.0 P3 机器可读条件码（event_*/state_*/typed_permanent/unsupported）
    target: str = "self"     # 效果目标: self / all_allies / all_allies_except_self / ally_main
    values: list = field(default_factory=list)  # v5.7: 精炼1-5档数值, 引擎按 rank 索引取值


@dataclass
class LightCone:
    """光锥"""
    id: str
    name: str
    path: str            # Path 值
    rarity: int = 5      # 稀有度 3-5
    rank: int = 1        # 叠影等级 1-5
    base_HP: float = 0.0
    base_ATK: float = 0.0
    base_DEF: float = 0.0
    effects: list = field(default_factory=list)  # list[LightConeEffect]

    @staticmethod
    def from_json(filepath: str) -> "LightCone":
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        effects = [
            LightConeEffect(
                type=e.get("type", "permanent_buff"),
                condition=e.get("condition", ""),
                attributes=e.get("attributes", {}),
                param_id=e.get("paramId", ""),
                condition_code=e.get("condition_code", ""),
                target=e.get("target", "self"),
                values=e.get("values", []),
            )
            for e in data.get("effects", [])
        ]
        return LightCone(
            id=data["id"],
            name=data["name"],
            rarity=data.get("rarity", 5),
            rank=data.get("rank", 1),
            path=data["path"],
            base_HP=data.get("base_HP", 0),
            base_ATK=data.get("base_ATK", 0),
            base_DEF=data.get("base_DEF", 0),
            effects=effects,
        )


@dataclass
class RelicPiece:
    """单个遗器"""
    slot: str            # "head" / "hands" / "body" / "feet" / "link_rope" / "planar_sphere"
    set_name: str        # 套装名
    rarity: int = 5
    level: int = 15
    main_stat_type: str = ""
    main_stat_value: float = 0.0
    sub_stats: dict = field(default_factory=dict)


@dataclass
class RelicSetEffect:
    """遗器套装效果"""
    pieces_required: int  # 2 或 4
    description: str
    attributes: dict = field(default_factory=dict)  # 直接属性加成 {StatType: value}
    condition: str = ""   # 条件触发描述


@dataclass
class RelicSet:
    """遗器套装定义"""
    name: str
    effects: list = field(default_factory=list)  # list[RelicSetEffect]

    @staticmethod
    def from_json(filepath: str) -> "RelicSet":
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        effects = [
            RelicSetEffect(
                pieces_required=e.get("pieces_required", 2),
                description=e.get("description", ""),
                attributes=e.get("attributes", {}),
                condition=e.get("condition", ""),
            )
            for e in data.get("effects", [])
        ]
        return RelicSet(name=data["name"], effects=effects)


def load_lightcone(lc_id: str, data_dir: str = "data/light_cones") -> LightCone:
    filepath = f"{data_dir}/{lc_id}.json"
    return LightCone.from_json(filepath)


def load_relic_set(set_name: str, data_dir: str = "data/relics") -> RelicSet:
    """加载遗器套装定义"""
    import os
    filename = set_name.replace(" ", "_").replace("·", "").lower()
    filepath = f"{data_dir}/{filename}.json"
    return RelicSet.from_json(filepath)
