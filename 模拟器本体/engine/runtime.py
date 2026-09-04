"""引擎运行时层（L0）——战斗数据类与叶子原语（重构 M1 自 combat_engine.py 逐字迁出）

依赖方向约束：仅依赖 models/hooks/attributes，绝不 import combat_engine。
combat_engine 顶部 re-import 本模块符号作为过渡桥；M1 已将 tests/engine/web 的外部
引用全量改指本模块（combat_engine 自身函数内 self-import 除外，随 M4-M6 消亡）。
"""
import copy
from dataclasses import dataclass, field

from engine.core.attributes import CombatStats
from engine.hooks.base import HookRegistry
from engine.models.character import Character
from engine.models.elation import ElationBattleState


# ---- 常量 ----

AV_PER_TURN = 10000.0

ENERGY_GAIN = {"basic_attack": 20, "skill": 30, "ultimate": 5,
               "basic_attack_enhanced": 20}  # v5.3 强化普攻回能（忘归人冉冉方炽等, 实机普攻类都回能）

DEFAULT_HP = 3000.0

INITIAL_SP = 3

MAX_SP = 5

# ---- 战斗单元（数据类） ----

@dataclass
class TimedBuff:
    """临时增益：有持续回合数，过期自动移除"""
    source_id: str
    attributes: dict  # {StatType: value}
    remaining_turns: int
    source_name: str = ""
    param_id: str = ""  # 技能参数ID（叠层识别用，如希儿战技加速）

@dataclass
class PlayerStatus:
    """玩家侧状态（v5.0 P4）: 敌方施加的控制/debuff 承载。

    与 TimedBuff 分离: 控制状态需要行动判定语义（眩晕跳过/冻结跳过+推条/嘲讽改选人），
    非数值叠加。倒计时在 Y 轴常规回合开始时判定（X 轴不 tick, AGENTS.md:17 一致）。
    """
    id: str
    name: str
    category: str = "debuff"   # "control" / "debuff"
    source: str = ""           # 来源（敌方 id）
    remaining_turns: int = 1
    attributes: dict = field(default_factory=dict)  # 附加属性（如减速 SPD_PERCENT 负值）
    base_chance: float = 1.0   # 基础命中率（EHR 检定）

@dataclass
class SimUnit:
    char: Character
    base_stats: CombatStats
    position: int
    current_hp: float = DEFAULT_HP
    max_hp: float = DEFAULT_HP
    current_energy: float = 0.0
    is_alive: bool = True
    total_damage_dealt: float = 0.0
    damage_log: list = field(default_factory=list)
    # 欢愉子系统使用的字段（由 ElationSystem 读写）
    hidden_score: float = 0.0
    invincible_active: bool = False
    invincible_basics_done: int = 0
    lc_ult_used: bool = False
    tb_cd_buff_turns: int = 0
    yao_res_pen_turns: int = 0
    lightcone: object = field(default=None)  # LightCone对象
    # 遗器动态追踪
    relic_flags: dict = field(default_factory=dict)    # 首次触发标志
    relic_stacks: dict = field(default_factory=dict)   # 叠层计数
    # 记忆命途字段
    yizhi: int = 0                     # 【忆质】（长夜月）
    xinrui: float = 0.0                # 【新蕊】（遐蝶）特殊能量，HP损失1:1转化，上限34000
    zhuiyi: float = 0.0                # 【追忆】（昔涟）特殊能量，上限27
    has_future: bool = False           # 【未来】token
    story_points: int = 0              # 献予真我之诗故事计数
    is_ripple: bool = False            # 【往昔的涟漪】状态
    is_darkness: bool = False          # 【至暗之谜】状态
    darkness_charges: int = 0          # 【至暗之谜】充能
    is_sovereign: bool = False         # 【至高之姿】状态（阿格莱雅）
    eidolon_rank: int = 0              # 已激活星魂数（0-6）
    last_target_id: str = ""           # 上次攻击目标(enemy.id)，用于忆灵自动选目标
    memsprite_unit: object = None      # 关联的忆灵 MemSpriteUnit
    marker: object = None              # 关联的行动条标记（v5.3: 浮元/完全燃烧倒计时）
    # 临时增益
    buffs: list = field(default_factory=list)  # [TimedBuff]
    # 玩家侧状态（v5.0 P4）: [PlayerStatus]
    statuses: list = field(default_factory=list)
    # 护盾值（v5.0 P5）: 受击优先吸收
    shield: float = 0.0
    # 光锥叠层/标记计数（v5.0.1）: {f"{lc_id}::{mark}": count}
    lc_stacks: dict = field(default_factory=dict)
    lc_stack_turns: dict = field(default_factory=dict)  # 同 key → 每层独立剩余回合 list[int]（v5.6 分层计时）
    extra: dict = field(default_factory=dict)

@dataclass
class SimState:
    enemies: list = field(default_factory=list)
    units: list = field(default_factory=list)
    skill_points: int = INITIAL_SP
    max_sp: int = MAX_SP
    current_av: float = 0.0
    turn_count: int = 0
    log: list = field(default_factory=list)
    # 通用扩展
    extra: dict = field(default_factory=dict)
    # 行动计数（按 char.id; 每普攻/战技/终结技 +1）
    action_counts: dict = field(default_factory=dict)
    # 轮次统计（模拟结束时按队内最慢角色计算）
    cycles: int = 0
    # 记忆命途：场上忆灵列表
    memsprites: list = field(default_factory=list)  # [MemSpriteUnit]
    # v5.3: 行动条标记列表（浮元/完全燃烧倒计时等非实体条目）
    markers: list = field(default_factory=list)     # [TimelineMarker]
    # 境界系统（全局互斥）
    realm_owner: str = ""              # 当前境界归属角色ID，空=无境界
    realm_turns: int = 0               # 境界剩余回合（-1=永久）
    realm_true_dmg: float = 0.0        # 境界真伤倍率（昔涟结界: 0.24）
    # 欢愉子系统使用的字段
    laugh_points: float = 0.0
    elation_state: ElationBattleState = field(default_factory=ElationBattleState)
    aha_speed: float = 80.0
    aha_next_av: float = float('inf')
    yao_field_active: bool = False
    yao_field_turns: int = 0
    hooks: HookRegistry = field(default_factory=HookRegistry)
    ai_registry: dict = field(default_factory=dict)
    skill_hooks: dict = field(default_factory=dict)
    # M5a: 角色包相位表（每局由 characters.build_phase_tables 注入; 引擎直调入口惰性自举）
    char_phases: dict = field(default_factory=dict)        # {char_id: {phase: fn(u, state, **ctx)}}
    observer_phases: dict = field(default_factory=dict)    # {phase: [fn(u, state, **ctx)]}
    turn_ticks: dict = field(default_factory=dict)         # {zone: [fn(u, state)]}
    effect_takeovers: dict = field(default_factory=dict)   # {param_id: fn(u, state, skill, skill_key, eff)}
    effect_mutators: dict = field(default_factory=dict)    # {param_id: fn(u, state, attrs, skill)}
    effect_pre_apply: dict = field(default_factory=dict)   # {param_id: fn(u, state, target)}
    debuff_takeovers: dict = field(default_factory=dict)   # {param_id: fn(u, state, target)}
    settle_pipeline: list = field(default_factory=list)    # [fn(u, state, skill, skill_key, total_dmg)] 保序结算管线
    _phase_tables_ready: bool = False

    @property
    def enemy(self):
        alive = [e for e in self.enemies if e.HP > 0]
        return alive[0] if alive else self.enemies[0]

    def alive_enemies(self):
        return [e for e in self.enemies if e.HP > 0]

# ---- 目标选择 / AV 叶子原语 ----

def _next_av(units: list[SimUnit], next_avs: dict) -> tuple:
    """返回下一个行动的单位 (idx, av)"""
    best, best_av = None, float('inf')
    for i, u in enumerate(units):
        if u.is_alive and i in next_avs and next_avs[i] < best_av:
            best_av, best = next_avs[i], i
    return best, best_av

def _select_targets(alive: list, tgt_type: str) -> list:
    if tgt_type == "all_enemies": return alive
    if tgt_type == "blast": return alive[:min(3, len(alive))]
    if tgt_type == "adjacent": return alive[1:min(3, len(alive))]  # v5.3 扩散相邻目标
    if tgt_type == "all_except_main": return alive[1:]  # v5.7 长夜月迷梦: 除主目标外全部
    return [alive[0]]

def _enemy_for_damage(enemy, skill_type=None):
    """构建只用于本次伤害计算的敌人视图，叠加状态提供的易伤。
    v6.6c: 海瑟音结界DEF-25% + 状态提供的减伤(dmg_reduction)在此消费"""
    vulnerability = enemy.status_attribute('vulnerability')
    if skill_type == 'ultimate':
        vulnerability += enemy.status_attribute('vulnerability_ultimate')
    # v5.4 爱如此刻永恒【空白】: 敌方全体受伤提高（当局永久, 施加于 enemy.extra）
    vulnerability += enemy.extra.get('love_blank_vuln', 0.0)
    # 不死途E1按受击目标当前生命比例实时选择24%/36%。
    busitu_vuln = enemy.extra.get('busitu_e1_vuln', 0.0)
    busitu_max_hp = enemy.extra.get('busitu_e1_max_hp', 0.0)
    if busitu_max_hp and enemy.HP <= busitu_max_hp * 0.50:
        busitu_vuln = enemy.extra.get('busitu_e1_half_vuln', busitu_vuln)
    vulnerability += busitu_vuln
    # v5.7: 阿格莱雅E1: 织线目标受到的伤害提高15%（单点: 所有伤害来源统一生效）
    vulnerability += enemy.extra.get('gossamer_dmg_bonus', 0.0)
    vulnerability += enemy.extra.get('dht_tongpao_vuln', 0.0)
    vulnerability += enemy.extra.get('yinlang_e1_vuln', 0.0)
    def_down = 0.75 if enemy.extra.get('hysilens_field') else 1.0
    dr_extra = enemy.status_attribute('dmg_reduction')
    res_down = enemy.status_attribute('res_down')
    if not vulnerability and def_down == 1.0 and not dr_extra and not res_down:
        return enemy
    effective = copy.deepcopy(enemy)
    effective.vulnerability += vulnerability
    effective.DEF *= def_down
    effective.dmg_reduction += dr_extra
    if res_down:
        for elem in effective.element_res:
            effective.element_res[elem] = effective.element_res.get(elem, 0.0) - res_down
    return effective

def _set_av(state, navs, key, value):
    """统一 AV 写入口：赋值 + 打达成顺序戳（同AV后到先动）"""
    navs[key] = value
    state.extra['stamp_counter'] = state.extra.get('stamp_counter', 0) + 1
    state.extra.setdefault('av_stamp', {})[key] = state.extra['stamp_counter']

def _stamp_av_key(state, key):
    """对任意行动条实体打达成顺序戳（v6.2.1b P3-1: 忆灵 next_av 直写路径的补戳入口）。

    忆灵的排程写在 extra['next_av'] 而非 navs, 历史路径均绕过 _set_av → 同AV并列时
    stamps.get(key, 0)=0 被当作"最早达成"而排最后, 违反"后到先动"。所有直写点必须
    同步调用本函数（角色/敌方路径直接改走 _set_av 即可）。
    """
    state.extra['stamp_counter'] = state.extra.get('stamp_counter', 0) + 1
    state.extra.setdefault('av_stamp', {})[key] = state.extra['stamp_counter']

# ---- 我方受击视图（Enemy 鸭子类型） ----

class CharacterAsTarget:
    """把 SimUnit/MemSpriteUnit 包装成伤害引擎所需的 Enemy 鸭子类型（只读视图）。
    calculate_damage 的 enemy 参数读取 DEF/dmg_reduction/vulnerability/element_res/is_broken 等。"""

    def __init__(self, unit, stats):
        self._unit = unit
        self._stats = stats

    @property
    def DEF(self):
        return self._stats.DEF

    @property
    def dmg_reduction(self):
        return self._stats.DMG_REDUCTION

    @property
    def vulnerability(self):
        return 0.0  # 我方易伤系统未建, MVP 留 0

    @property
    def is_broken(self):
        return False  # 我方无韧性条 → 韧性乘区恒 0.9

    @property
    def max_toughness(self):
        return 0.0

    @property
    def HP(self):
        return self._unit.current_hp

    @property
    def element_res(self):
        return {"物理": 0.0, "火": 0.0, "冰": 0.0, "雷": 0.0,
                "风": 0.0, "量子": 0.0, "虚数": 0.0}

    def get_res(self, element):
        return 0.0

    def status_attribute(self, key):
        return 0.0


def _hook_owner(state, char_id, fallback):
    """Resolve the unit that owns a broadcast hook.

    ``trigger_all`` keeps ``u`` as the event subject for compatibility, while
    ``char_id`` identifies the effect owner.  Broadcast handlers must use the
    owner when mutating energy, buffs, or owner-local state.
    """
    if char_id:
        owner = next((unit for unit in getattr(state, 'units', [])
                      if getattr(getattr(unit, 'char', None), 'id', None) == char_id), None)
        if owner is not None:
            return owner
    return fallback


def _tech_enemies(state):
    """存活敌人列表（无存活则全部）"""
    return [e for e in state.enemies if getattr(e, 'HP', 0) > 0] or list(state.enemies)
