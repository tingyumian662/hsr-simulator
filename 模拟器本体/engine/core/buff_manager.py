"""Buff/Debuff 生命周期管理器"""
from dataclasses import dataclass


@dataclass
class BuffInstance:
    """运行时 Buff 实例"""
    id: str
    name: str
    buff_type: str          # "buff" | "debuff" | "status_mark"
    source: str             # "character_skill" | "light_cone" | "relic" | "enemy_skill"
    target_id: str          # 目标角色/敌人ID
    caster_id: str          # 施放者ID
    attributes: dict        # {StatType: value}, value 使用游戏内百分比数值
    duration_type: str      # "fixed_turns" | "caster_turn" | "target_turn" | "permanent"
    remaining_turns: int = 0
    is_permanent: bool = False
    max_stacks: int = 1
    current_stacks: int = 1
    removable: bool = True

    def is_expired(self) -> bool:
        return not self.is_permanent and self.remaining_turns <= 0

    def tick(self):
        """回合倒计时"""
        if not self.is_permanent and self.remaining_turns > 0:
            self.remaining_turns -= 1


class BuffManager:
    """管理战斗中所有 Buff/Debuff 实例"""

    def __init__(self):
        self._buffs: dict[str, list[BuffInstance]] = {}  # target_id -> [buffs]

    def apply(self, buff_template: dict, caster_id: str, target_id: str) -> BuffInstance:
        """施加 Buff（从模板创建实例）"""
        # 检查是否已存在同 ID buff（不可叠加时刷新持续时间）
        existing = self.get_buffs_on(target_id)
        for eb in existing:
            if eb.id == buff_template["id"] and eb.max_stacks <= 1:
                dur = buff_template.get("duration", {})
                eb.remaining_turns = dur.get("turns", 0)
                return eb

        instance = BuffInstance(
            id=buff_template["id"],
            name=buff_template.get("name", ""),
            buff_type=buff_template.get("type", "buff"),
            source=buff_template.get("source", "character_skill"),
            target_id=target_id,
            caster_id=caster_id,
            attributes=buff_template.get("attributes", {}),
            duration_type=buff_template.get("duration", {}).get("type", "fixed_turns"),
            remaining_turns=buff_template.get("duration", {}).get("turns", 0),
            is_permanent=buff_template.get("duration", {}).get("permanent", False),
            max_stacks=buff_template.get("max_stacks", 1),
            current_stacks=1,
            removable=buff_template.get("removable", True),
        )

        if target_id not in self._buffs:
            self._buffs[target_id] = []
        self._buffs[target_id].append(instance)
        return instance

    def remove(self, target_id: str, buff_id: str):
        """移除指定 Buff"""
        if target_id in self._buffs:
            self._buffs[target_id] = [b for b in self._buffs[target_id] if b.id != buff_id]

    def get_buffs_on(self, target_id: str) -> list:
        """获取目标身上所有 Buff 实例"""
        return self._buffs.get(target_id, [])

    def get_active_buff_attributes(self, target_id: str) -> list[dict]:
        """获取目标身上所有未过期 Buff 的属性修改列表（供属性汇总使用）"""
        result = []
        for buff in self.get_buffs_on(target_id):
            if not buff.is_expired():
                result.append({"id": buff.id, "attributes": buff.attributes.copy()})
        return result

    def tick_target(self, target_id: str):
        """对指定目标的所有 Buff 进行回合倒计时"""
        if target_id in self._buffs:
            for buff in self._buffs[target_id]:
                buff.tick()
            # 清理过期 Buff
            self._buffs[target_id] = [b for b in self._buffs[target_id] if not b.is_expired()]

    def tick_all(self):
        """全局回合倒计时"""
        for target_id in list(self._buffs.keys()):
            self.tick_target(target_id)

    def clear(self):
        """清空所有 Buff"""
        self._buffs.clear()
