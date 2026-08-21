"""敌人数据模型"""
import json
from dataclasses import dataclass, field

@dataclass
class EnemyStatus:
    id: str
    name: str
    category: str = "debuff"
    source: str = ""
    remaining_turns: int = 0
    removable: bool = True
    attributes: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return self.remaining_turns == 0


@dataclass
class Enemy:
    """敌人完整数据"""
    id: str
    name: str
    level: int = 80
    HP: float = 999999.0
    ATK: float = 500.0
    DEF: float = 1000.0
    SPD: float = 100.0
    toughness: float = 100.0          # 韧性当前值
    max_toughness: float = 100.0       # 韧性上限（用于恢复）
    extra_toughness: float = 0.0       # 额外韧性当前值（v5.3 忘归人云火昭, 0=无）
    extra_toughness_max: float = 0.0   # 额外韧性上限（=韧性上限×40%, 随击破后消耗）
    dmg_reduction: float = 0.0         # 减伤%，0~1 之间的小数
    vulnerability: float = 0.0         # 易伤%，0~1 之间的小数
    effect_res: float = 0.0            # 效果抵抗（v5.6 EHR 检定用; 默认0=无数据=必中, 真实敌人数据录入后激活）
    element_res: dict = field(default_factory=lambda: {
        "物理": 0.0, "火": 0.0, "冰": 0.0, "雷": 0.0,
        "风": 0.0, "量子": 0.0, "虚数": 0.0,
    })
    is_broken: bool = False            # 是否处于击破状态
    break_element: str = ""            # 击破属性
    break_debuff_name: str = ""        # 异常名称（裂伤/灼烧/冻结等）
    break_debuff_turns: int = 0        # 异常剩余回合
    statuses: list[EnemyStatus] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # 战斗扩展数据（如av_delayed）
    attacks: list = field(default_factory=list)  # 攻击技能表（element/damage_type/multiplier/target_type/priority）
    actions_per_turn: int = 1                     # v6.4c 精英双动: 每回合行动次数（实机精英=2）
    av: float = 0.0                              # 下次行动的绝对AV（行动条用）
    # 放在字段末尾以保持所有既有位置参数兼容；0 表示按入场 HP 初始化。
    max_hp: float = 0.0

    def __post_init__(self) -> None:
        if self.max_hp <= 0:
            self.max_hp = float(self.HP)

    def add_status(self, status: EnemyStatus) -> EnemyStatus:
        existing = next((s for s in self.statuses if s.id == status.id), None)
        if existing:
            existing.name = status.name
            existing.category = status.category
            existing.source = status.source
            existing.remaining_turns = status.remaining_turns
            existing.removable = status.removable
            existing.attributes = status.attributes.copy()
            return existing
        self.statuses.append(status)
        return status

    def remove_status(self, status_id: str) -> None:
        self.statuses = [s for s in self.statuses if s.id != status_id]

    def tick_statuses(self) -> list[EnemyStatus]:
        expired = []
        for status in self.statuses:
            if status.remaining_turns > 0:
                status.remaining_turns -= 1
                if status.remaining_turns == 0:
                    expired.append(status)
            # remaining_turns == -1: 永久（不递减不失效，由施加方手动移除，
            # 如灵砂E1 击破期间 DEF-20%）
        self.statuses = [s for s in self.statuses if not s.expired]
        return expired

    def debuff_count(self) -> int:
        return sum(s.category in {"debuff", "dot", "control"} for s in self.statuses)

    def dot_count(self) -> int:
        return sum(s.category == "dot" for s in self.statuses)

    def has_status(self, *, status_id: str = "", category: str = "", name: str = "") -> bool:
        return any((not status_id or s.id == status_id)
                   and (not category or s.category == category)
                   and (not name or s.name == name) for s in self.statuses)

    def status_attribute(self, key: str) -> float:
        return sum(float(s.attributes.get(key, 0.0)) for s in self.statuses)

    def get_res(self, element: str) -> float:
        """获取对特定元素的抗性，0~1 之间的小数"""
        return self.element_res.get(element, 0.0)

    def get_def_multiplier(self, attacker_level: int, def_reduction: float = 0.0) -> float:
        """防御乘区: (200+10Lv) / (200+10Lv + DEF×(1-min(减防+无视防御,100%)))"""
        effective_def = self.DEF * (1.0 - min(def_reduction, 1.0))
        base_value = 200.0 + 10.0 * attacker_level
        return base_value / (base_value + effective_def)

    @staticmethod
    def from_json(filepath: str) -> "Enemy":
        with open(filepath, "r", encoding="utf-8") as f:
            return Enemy.from_dict(json.load(f))

    @staticmethod
    def from_dict(data: dict) -> "Enemy":
        return Enemy(
            id=data["id"],
            name=data["name"],
            level=data.get("level", 80),
            HP=data.get("HP", 999999),
            max_hp=data.get("max_hp", data.get("HP", 999999)),
            ATK=data.get("ATK", 500),
            DEF=data.get("DEF", 1000),
            SPD=data.get("SPD", 100),
            toughness=data.get("toughness", 100),
            max_toughness=data.get("toughness", 100),
            dmg_reduction=data.get("dmg_reduction", 0.0),
            vulnerability=data.get("vulnerability", 0.0),
            effect_res=data.get("effect_res", 0.0),
            element_res=data.get("element_res", {}),
            attacks=data.get("attacks", []),
            actions_per_turn=data.get("actions_per_turn", 1),
        )


def load_enemy(enemy_id: str, data_dir: str = "data/enemies") -> Enemy:
    filepath = f"{data_dir}/{enemy_id}.json"
    return Enemy.from_json(filepath)
