"""Hook 系统 — 角色/装备/敌方的特殊机制接口

效果管线入口：模拟前通过 resolve_character_effects() 解析所有行迹/星魂/光锥/遗器
效果，注册到 HookRegistry。模拟中在关键触发点调用 trigger() 激活效果。
"""
from typing import Any, Callable
from dataclasses import dataclass, field


# Hook 事件类型列表
HOOK_EVENTS = [
    # 战斗生命周期
    "on_enter_battle",        # 进入战斗（初始化完成后）
    "on_wave_start",          # 波次开始
    # 回合生命周期
    "on_turn_start",          # 回合开始（角色行动前）
    "on_turn_end",            # 回合结束（buff tick 后）
    # 技能生命周期
    "on_before_skill",        # 技能使用前（SP/能量扣除前）
    "on_basic_attack",        # 普攻
    "on_skill",               # 战技
    "on_ultimate",            # 终结技
    "on_elation_skill",       # 欢愉技
    "on_after_skill",         # 技能使用后（伤害/buff/光锥触发后）
    # 伤害
    "on_before_damage",       # 造成伤害前，可修改伤害参数
    "on_after_damage",        # 造成伤害后
    "on_take_damage",         # 受到伤害时
    # 状态变化
    "on_kill",                # 击杀敌人时
    "on_weakness_break",      # 击破弱点时（attacker-only，u=击破者）
    "on_any_weakness_break",  # 我方任意成员造成击破时（v5.3: u=击破者, enemy=被击破目标）
    "on_energy_change",       # 能量变动时
    "on_enter_state",         # 进入特殊状态
    "on_exit_state",          # 退出特殊状态
    "on_ally_death",          # 我方角色阵亡时（v5.3: 光环失效处理, u=阵亡者）
    # HP 循环（记忆队核心事件）
    "on_heal",                # 治疗结算后（含治疗者/目标/治疗量）
    "on_shield",              # 护盾施加后（v6.7b: 大丽花行迹1受护盾再触发; 含施放者/目标/护盾量）
    "on_hp_loss",             # HP 被消耗时（主动扣血，含损失量/影响单位）
    # 忆灵生命周期
    "on_memsprite_summon",    # 忆灵被召唤时（含召唤者/忆灵单位）
    "on_future_consume",      # 未来 token 被消耗时（昔涟追忆）
    "on_memsprite_attack",    # 忆灵攻击/技能结算后（v5.2: 英豪4pc 忆灵CD等）
    # 友方/敌方
    "on_followup",            # 追加攻击动作结算后（千星/都蓝王朝等动作级效果）
    "on_followup_hit",        # 追加攻击每段伤害后（大公4pc等逐段效果）
    "on_ally_skill_targeted", # 我方单体技能选中某目标时（v5.6: 船长Help叠层; u=施放者, target=被选中者）
    "on_ally_attack",         # 友方攻击时
    "on_weakness_implant",    # v6.7 我方为敌添加弱点时（大丽花行迹3消费）
    "on_debuff_applied",      # v6.10 施放者使敌陷入负面时（黄泉天赋消费）
    "on_enemy_attack",        # 敌方攻击时
]


@dataclass
class ResolvedEffect:
    """解析后的效果 — 模拟前由 effect_resolver 生成，注册到 HookRegistry"""
    source: str          # "trace" | "eidolon" | "lightcone" | "relic"
    source_name: str     # 显示名，如 "行迹·速域转化"
    char_id: str         # 所属角色 ID
    trigger: str         # HookRegistry 事件名
    action: Callable     # (SimUnit, SimState, **ctx) -> None | any
    condition: Callable = None  # (SimUnit, SimState, **ctx) -> bool，None=始终触发
    priority: int = 0    # 同事件中执行优先级（数字越小越先执行）


class HookRegistry:
    """全局 Hook 注册表，按角色 ID + 事件类型索引"""

    def __init__(self):
        self._hooks: dict[str, dict[str, list]] = {}

    def register(self, character_id: str, event: str,
                 action: Callable, condition: Callable = None,
                 priority: int = 0, source: str = "", source_name: str = ""):
        """注册一个 Hook 处理函数"""
        effect = ResolvedEffect(
            source=source, source_name=source_name,
            char_id=character_id, trigger=event,
            action=action, condition=condition, priority=priority,
        )
        if character_id not in self._hooks:
            self._hooks[character_id] = {}
        if event not in self._hooks[character_id]:
            self._hooks[character_id][event] = []
        self._hooks[character_id][event].append(effect)
        # 按优先级排序
        self._hooks[character_id][event].sort(key=lambda e: e.priority)

    def register_effect(self, effect: ResolvedEffect):
        """注册一个已解析的效果对象"""
        self.register(
            character_id=effect.char_id, event=effect.trigger,
            action=effect.action, condition=effect.condition,
            priority=effect.priority, source=effect.source,
            source_name=effect.source_name,
        )

    def get_handlers(self, character_id: str, event: str) -> list:
        """获取某角色某事件的所有处理器"""
        return self._hooks.get(character_id, {}).get(event, [])

    def trigger(self, character_id: str, event: str, **kwargs) -> Any:
        """触发 Hook，依次调用所有处理器，返回最后一个非 None 结果。
        condition 返回 False 的处理器会被跳过。
        action 返回 True 表示"已完全处理，跳过后续"。
        """
        handlers = self.get_handlers(character_id, event)
        result = None
        for effect in handlers:
            # v5.2 问题2: 广播事件中 u=事件主体, 持有者由 char_id 区分
            # （handler 内 state.units 按 char_id 定位持有者）
            ctx = {**kwargs, 'char_id': effect.char_id}
            if effect.condition and not effect.condition(**ctx):
                continue
            r = effect.action(**ctx)
            if r is True:  # 信号：跳过后续处理
                return True
            if r is not None:
                result = r
        return result

    def trigger_all(self, event: str, **kwargs) -> dict:
        """对所有已注册角色触发事件。返回 {char_id: result}。"""
        results = {}
        for char_id in self._hooks:
            r = self.trigger(char_id, event, **kwargs)
            if r is not None:
                results[char_id] = r
        return results

    def clear(self):
        """清空所有注册（测试用）"""
        self._hooks.clear()
