"""行动条标记系统（v5.3）— 浮元/完全燃烧倒计时等"仅存在于行动条"的非实体条目

与忆灵/召唤物不同：无 HP、不受击、不被选中、不参与死亡判定，只在 Y 轴行动条上
按自身速度行动。行动行为由 combat_sim 的 MARKER_ACTIONS 注册表注入（避免循环导入）。

- 浮元（灵砂）：90 速，行动次数计数（初始3/上限5/战技+3），归0或召唤者阵亡消失
- 完全燃烧倒计时（流萤）：70 速，行动时退出完全燃烧状态
"""
from dataclasses import dataclass, field


def _av_per_turn() -> float:
    """行动值常量（combat_sim 模块级, 惰性导入避免循环依赖）"""
    from engine.core.combat_sim import AV_PER_TURN
    return AV_PER_TURN


# 标记配置注册表（数据驱动, 行为由 MARKER_ACTIONS 分发）
MARKER_REGISTRY = {
    "lingsha_fuyuan": {
        "name": "浮元",
        "base_SPD": 90.0,
        "initial_charges": 3,      # 首次召唤行动次数
        "max_charges": 5,          # 行动次数上限
        "resummon_charges": 3,     # 在场时战技刷新 +3 次
    },
    "firefly_countdown": {
        "name": "完全燃烧倒计时",
        "base_SPD": 70.0,
    },
    "dht_longling": {
        "name": "龙灵",
        "base_SPD": 165.0,
    },
    "robin_concert": {
        "name": "协奏倒计时",
        "base_SPD": 90.0,
    },
    "qianye_wrath": {
        "name": "无量忿怒倒计时",
        "base_SPD": 70.0,
    },
    # v6.11.1 知更鸟·晴歌: Fever倒计时（140速, 行动扣50%气氛至少12点）
    "qingge_countdown": {
        "name": "Fever倒计时",
        "base_SPD": 140.0,
    },

}


@dataclass
class TimelineMarker:
    """行动条标记条目（非实体, 无 HP/受击/选中）"""
    marker_id: str
    summoner_id: str
    data: dict
    extra: dict = field(default_factory=dict)  # next_av / charges / 状态
    is_alive: bool = True

    @property
    def name(self) -> str:
        return self.data.get("name", self.marker_id)

    @property
    def action_spd(self) -> float:
        return self.data.get("base_SPD", 0.0)

    @property
    def is_backup(self) -> bool:
        return False


class TimelineMarkerSystem:
    """行动条标记系统: 生成/移除/提前/行动调度。

    action_handlers / despawn_handlers 由 combat_sim 注入
    （marker_id → fn(state, marker)），本模块不依赖 combat_sim。
    """

    def __init__(self):
        self.action_handlers: dict = {}
        self.despawn_handlers: dict = {}
        self.spawn_handlers: dict = {}  # v5.3: 入场副作用（灵砂E6抗性-20%等）

    def spawn(self, state, u, marker_id: str) -> TimelineMarker:
        """创建或刷新标记。已在场（召唤者关联）→ 按注册表刷新（如浮元+3次）。"""
        cfg = dict(MARKER_REGISTRY.get(marker_id, {}))
        if not cfg:
            state.log.append(f'  [WARN] 未注册标记 marker_id={marker_id}')
            return None
        existing = u.marker
        if existing and existing.is_alive:
            if 'resummon_charges' in cfg:
                charges = existing.extra.get('charges', 0) + cfg['resummon_charges']
                existing.extra['charges'] = min(cfg.get('max_charges', 99), charges)
                state.log.append(f'  {existing.name}刷新: 行动次数+{cfg["resummon_charges"]} → {existing.extra["charges"]}')
            return existing
        marker = TimelineMarker(marker_id=marker_id, summoner_id=u.char.id, data=cfg)
        marker.extra['next_av'] = state.current_av + _av_per_turn() / max(marker.action_spd, 1.0)
        if 'initial_charges' in cfg:
            marker.extra['charges'] = cfg['initial_charges']
        state.markers.append(marker)
        u.marker = marker
        state.log.append(f'  {marker.name}入场 (行动次数={marker.extra.get("charges", "-")})')
        handler = self.spawn_handlers.get(marker_id)
        if handler:
            handler(state, marker, u)
        return marker

    def despawn(self, state, marker: TimelineMarker) -> None:
        """移除标记 + 触发退出副作用（E6抗性恢复/退出燃烧等）"""
        handler = self.despawn_handlers.get(marker.marker_id)
        if handler:
            handler(state, marker)
        if marker in state.markers:
            state.markers.remove(marker)
        summoner = next((x for x in state.units if x.char.id == marker.summoner_id), None)
        if summoner and summoner.marker is marker:
            summoner.marker = None
        marker.is_alive = False
        state.log.append(f'  {marker.name}消失')

    def handle_action(self, state, marker: TimelineMarker) -> None:
        """标记行动: 更新行动条位置 → 分发行动 → 消耗行动次数"""
        marker.extra['next_av'] = state.current_av + _av_per_turn() / max(marker.action_spd, 1.0)
        delay = marker.extra.pop('delay_pending', 0.0)
        if delay:
            marker.extra['next_av'] += delay
        state.log.append(f'  {marker.name}行动')
        handler = self.action_handlers.get(marker.marker_id)
        if handler:
            handler(state, marker)
        if not marker.is_alive:
            return
        if 'charges' in marker.extra:
            marker.extra['charges'] -= 1
            if marker.extra['charges'] <= 0:
                self.despawn(state, marker)

    def advance(self, state, u, ratio: float) -> None:
        """标记行动条提前（正=提前, 负=延后）。如灵砂战技浮元提前20%/终结技100%"""
        m = u.marker
        if not (m and m.is_alive):
            return
        m.extra['next_av'] = max(
            0.0,
            m.extra.get('next_av', state.current_av) - (_av_per_turn() / max(m.action_spd, 1.0)) * ratio,
        )
        state.log.append(f'  {m.name}行动提前{ratio * 100:.0f}%')
