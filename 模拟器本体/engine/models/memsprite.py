"""忆灵数据模型 — 记忆命途角色的召唤单位"""
from dataclasses import dataclass, field


@dataclass
class MemSprite:
    """忆灵 — 记忆命途角色的专属召唤单位

    每个记忆角色的忆灵定义写在其天赋模块中，因角色而异。
    忆灵有独立血条、独立行动条（战斗模拟层实现），
    可被敌方攻击，受击概率受站位影响。
    """
    name: str                         # 忆灵名
    element: str = ""                 # 元素类型（通常继承召唤者）
    base_HP: float = 0.0              # 基础生命
    base_ATK: float = 0.0             # 基础攻击
    base_DEF: float = 0.0             # 基础防御
    base_SPD: float = 0.0             # 基础速度
    max_taunt: int = 100              # 嘲讽值（影响受击概率）
    skills: dict = field(default_factory=dict)     # 忆灵技 {"skill_key": Skill}
    inherit_ratios: dict = field(default_factory=dict)  # 属性继承 {"HP": 0.5, "ATK": 0.8, ...}
    # v5.0 P8: 废弃预留 — 引擎无消费点（敌方选人按 taunt 加权不按站位）, 保留防 JSON 兼容风险
    position_offset: int = -1         # 站位偏移（-1 = 召唤者左侧，1 = 右侧）
    is_backup: bool = False           # 后援单位：敌方不可选中，我方扩散不溅射（死龙等）
    current_HP: float = 0.0           # 当前生命（战斗运行时）
    is_summoned: bool = False         # 是否已在场上

    def calc_combat_position(self, summoner_position: int) -> float:
        """计算忆灵的战斗站位（用于受击表排序）。

        角色站位 1~4 号位从右往左。忆灵默认在召唤者左侧，
        站位值 = summoner_position + 0.5（在召唤者与左邻位之间）。
        """
        return summoner_position + 0.5 * self.position_offset

    def calc_inherited_stat(self, summoner_stat: float, stat_key: str) -> float:
        """计算继承后的属性值。未定义继承比例时返回原始值。"""
        ratio = self.inherit_ratios.get(stat_key, 1.0)
        return summoner_stat * ratio

    def take_damage(self, damage: float):
        """受到伤害"""
        self.current_HP = max(0.0, self.current_HP - damage)

    def is_alive(self) -> bool:
        return self.is_summoned and self.current_HP > 0
