"""战斗模拟器 v4 — 通用回合制引擎

命途机制（欢愉/记忆等）按队伍组成条件激活，角色 AI 通过注册表调度。
所有角色/命途专属逻辑在 engine/systems/ 下维护，引擎本体保持通用。
"""
import copy
import random
from dataclasses import dataclass, field
from engine.models.character import Character
from engine.models.enemy import Enemy, EnemyStatus
from engine.models.elation import ElationBattleState
from engine.core.attributes import compute_combat_stats, CombatStats
from engine.core.damage import calculate_damage
from engine.core.team_mechanics import TeamMechanics
from engine.hooks.base import HookRegistry
from engine.systems.timeline_marker import TimelineMarker, TimelineMarkerSystem

# ---- 常量 ----
AV_PER_TURN = 10000.0
ENERGY_GAIN = {"basic_attack": 20, "skill": 30, "ultimate": 5,
               "basic_attack_enhanced": 20}  # v5.3 强化普攻回能（忘归人冉冉方炽等, 实机普攻类都回能）
DEFAULT_HP = 3000.0
INITIAL_SP = 3
MAX_SP = 5


# ---- 战斗单元 ----

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


def _tick_buffs(unit) -> list:
    """倒计时并清理过期 buff，返回被移除的 buff 列表"""
    expired = []
    for b in unit.buffs:
        # These effects are measured by the applier's turn or an independent
        # timeline marker. Beneficiary turns must not consume their duration.
        if getattr(b, 'param_id', '') in {
                'sunday_mentor_cd', 'ruanmei_xianyin', 'ruanmei_field',
                'robin_skill', 'robin_concert'}:
            continue
        # remaining_turns < 0 表示永久；不得在常规回合边界被递减或移除。
        if b.remaining_turns < 0:
            continue
        b.remaining_turns -= 1
        if b.remaining_turns <= 0:
            expired.append(b)
    for b in expired:
        unit.buffs.remove(b)
        if getattr(b, 'param_id', '') == 'sunday_cr':
            unit.extra.pop('sunday_cr_stacks', None)
    if unit.extra.pop('pioneer_double_pending', False):
        unit.extra['pioneer_double_turns'] = 1
    elif unit.extra.get('pioneer_double_turns', 0) > 0:
        unit.extra['pioneer_double_turns'] -= 1
    return expired


def _apply_luandie(state, t, source=None):
    """希儿E6乱蝶: 受击后追加30%终结技快照真伤（不递归, 触发次数3→0）
    "持续3回合"简化=3次触发（引擎无敌人回合概念）"""
    if t.extra.get('luandie', 0) > 0:
        dmg = 0.30 * t.extra.get('luandie_ult_dmg', 0.0)
        _commit_enemy_damage(state, source, t, dmg, damage_type='true_damage',
                             record_cipher=False)
        t.extra['luandie'] -= 1
        state.log.append(f'  乱蝶真伤: {dmg:.0f} (剩余{t.extra["luandie"]}次)')


def _fengjin_cleanse(state, u):
    """风堇行迹3·雷雨轻柔: 战技/终结技→解除全队1个负面效果
    （v5.0 P4: 优先清控制/减益状态, 再清负属性TimedBuff兜底）"""
    cleared = 0
    for eu in state.units:
        if not eu.is_alive:
            continue
        # ① 优先清 PlayerStatus（控制类优先级最高）
        for st in list(eu.statuses):
            if st.category in ('control', 'debuff'):
                eu.statuses.remove(st)
                state.hooks.trigger_all("on_exit_state", u=eu, state=state, status=st)
                cleared += 1
                break
        if cleared < 1:
            # ② 负属性 TimedBuff 兜底
            for b in list(eu.buffs):
                if any(v < 0 for v in b.attributes.values()):
                    eu.buffs.remove(b)
                    cleared += 1
                    break
        if cleared >= 1:
            break
    if cleared:
        state.log.append(f'  行迹3·雷雨轻柔: 净化全队{cleared}个负面效果')


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


def _gain_energy(u: SimUnit, amt: float, *, state=None, percent: bool = False,
                 apply_regen: bool = True) -> float:
    """统一回能入口（v5.0 P2）: 普通回能吃 ENERGY_REGEN 倍率, 百分比回能不吃。

    percent=True: 按能量上限百分比回（藿藿终结技队友回20%等, 实机不吃充能绳）。
    返回实际到账量（受上限截断; 溢出即浪费为实机语义, 无上限突破机制）。
    v6.10: 特殊能量角色(energy_type=special)无能量系统——残梦/飞黄/火种等走各自进度,
    通用回能对其 no-op（防止白厄秘技全队25能把 max_energy 当上限截断污染进度）。
    """
    if getattr(u.char, 'energy_type', 'regular') == 'special':
        return 0.0
    if percent:
        raw = (u.char.max_energy or 0) * amt
    else:
        er = ((_build_effective_stats(u, state).ENERGY_REGEN
               if state else u.base_stats.ENERGY_REGEN)
              if apply_regen else 1.0)
        raw = amt * er
    cap = u.char.max_energy or 999
    # v6.11.1 晴歌E6: Fever期间终结技可储存2次(能量上限×2, 放一次扣140保留溢出)
    if u.char.id == 'robin_summeretto' and u.eidolon_rank >= 6 \
            and u.extra.get('qingge_fever'):
        cap = cap * 2
    before = u.current_energy
    u.current_energy = min(cap, u.current_energy + raw)
    gained = u.current_energy - before
    # v6.9 千冶·刃行迹1·百炼骨: 溢出能量最多积攒80（终结技后恢复）
    if u.char.id == 'qianye' and raw > gained and state is not None:
        u.extra['qianye_overflow'] = min(80.0, u.extra.get('qianye_overflow', 0.0) + (raw - gained))
    if state is not None and gained > 0:
        # 千冶·刃行迹1: 能量恢复至上限时解除自身所有负面效果。
        if u.char.id == 'qianye' and u.current_energy >= (u.char.max_energy or 0) \
                and any(getattr(t, 'hook_name', '') == 'qianye_trace1'
                        for t in (u.char.traces or [])) and u.statuses:
            removed = list(u.statuses)
            u.statuses.clear()
            for status in removed:
                state.hooks.trigger_all('on_exit_state', u=u, state=state, status=status)
            state.log.append('  千冶·刃行迹1: 能量满, 解除自身所有负面效果')
        # v5.7: 开拓者·记忆天赋——我方全体每累计恢复10点能量→迷迷+1%充能（全渠道统一:
        # 施放技能/受击/光锥/藿藿终结技等回能一律经此, 此前只统计施放者自身技能回能）
        tbr = next((x for x in state.units
                    if x.char.id == 'trailblazer_remembrance' and x.memsprite_unit
                    and x.memsprite_unit.is_alive), None)
        if tbr:
            bank = tbr.extra.setdefault('tbr_energy_bank', 0.0) + gained
            full_pct = int(bank // 10)
            tbr.extra['tbr_energy_bank'] = bank - full_pct * 10
            if full_pct > 0:
                rem = state.extra.get('_rem_sys')
                ms = tbr.memsprite_unit
                if rem:
                    rem._mimi_charge_gain(state, ms, full_pct)
                else:
                    ms.extra['charge'] = min(100, ms.extra.get('charge', 0) + full_pct)
        # v5.0 P8: on_energy_change 事件（此前声明零触发）
        state.hooks.trigger_all("on_energy_change", u=u, state=state, amount=gained)
    return gained


def _gain_skill_points(state, amount: int = 1) -> int:
    """Recover team skill points and notify effects that count recovery events.

    Recovery-triggered effects count each requested point even when the shared
    resource is already capped, matching effects whose text explicitly includes
    overflow.
    """
    amount = max(int(amount), 0)
    before = state.skill_points
    state.skill_points = min(state.max_sp, state.skill_points + amount)
    for _ in range(amount):
        for owner in state.units:
            lc = getattr(owner, 'lightcone', None)
            if owner.is_alive and lc and lc.id == 'earthly_escapade' \
                    and lc.path == owner.char.path:
                _lc_masquerade_caiyan(state, owner)
    return state.skill_points - before


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

    @property
    def enemy(self):
        alive = [e for e in self.enemies if e.HP > 0]
        return alive[0] if alive else self.enemies[0]

    def alive_enemies(self):
        return [e for e in self.enemies if e.HP > 0]


# ---- 通用辅助 ----

def _next_av(units: list[SimUnit], next_avs: dict) -> tuple:
    """返回下一个行动的单位 (idx, av)"""
    best, best_av = None, float('inf')
    for i, u in enumerate(units):
        if u.is_alive and i in next_avs and next_avs[i] < best_av:
            best_av, best = next_avs[i], i
    return best, best_av


def _record_lc_attack_target(state, target) -> None:
    """Record each concrete enemy hit by the current attack, preserving hit order."""
    targets = state.extra.setdefault('lc_attack_target_refs', [])
    if target not in targets:
        targets.append(target)


def _lc_attacked_enemies(state) -> list:
    """Return living enemies hit by the current attack, with a single-target fallback."""
    if 'lc_attack_target_refs' in state.extra:
        return [t for t in state.extra.get('lc_attack_target_refs', [])
                if t in state.enemies and t.HP > 0]
    alive = state.alive_enemies() or state.enemies
    return alive[:1]


def _record_enemy_kill(state):
    """v6.6b P1-3: 每局敌方目标击杀累计（跨波, 白厄E1变身速度比例消费）。
    所有击杀检测点（直伤/弹射/忆灵/反击/DOT）必须调用, 统一口径。"""
    state.extra['killed_total'] = state.extra.get('killed_total', 0) + 1

def _record_kill_after_damage(state, u, target, before) -> bool:
    """v6.8.3: 手写伤害路径的统一击杀管线——killed_this_action / killed_total /
    on_kill / 光锥击杀事件。返回是否本次伤害造成击杀。"""
    if before <= 0 or getattr(target, 'HP', 0) > 0:
        return False
    if getattr(target, 'extra', {}).get('_kill_recorded', False):
        return False
    state.extra['killed_this_action'] = state.extra.get('killed_this_action', 0) + 1
    if hasattr(target, 'extra'):
        target.extra['_kill_recorded'] = True
    _record_enemy_kill(state)
    state.hooks.trigger(getattr(getattr(u, 'char', None), 'id', ''), "on_kill",
                        u=u, state=state, enemy=target)
    if u is not None:
        _process_lc_effects(u, state, "on_kill")
    if any(x.char.id == 'acheron' and x.is_alive for x in state.units):
        _acheron_jizhen_transfer(state)
    return True


def _commit_enemy_damage(state, u, target, amount, *, record_hit=True,
                         damage_type=None, skill_type=None, attack_type=None,
                         cipher_record_multiplier=1.0,
                         cipher_extra_rate=0.0,
                         cipher_record_amount=None,
                         record_cipher=True):
    """提交一次已计算的敌方伤害，并统一处理命中与击杀事件。

    调用方仍负责把返回值计入自己的动作总伤害；这样可以迁移附加伤害、DOT
    和弹射路径而不改变现有总伤害统计。已死亡目标不会再次触发击杀事件。
    """
    amount = max(float(amount or 0.0), 0.0)
    if target is None or amount <= 0.0 or getattr(target, 'HP', 0.0) <= 0.0:
        return 0.0, False
    before = target.HP
    target.HP = max(0.0, target.HP - amount)
    actual_damage = before - target.HP
    if state is not None and record_hit:
        _record_lc_attack_target(state, target)
        state.extra.setdefault('last_attack_targets', [])
        if target not in state.extra['last_attack_targets']:
            state.extra['last_attack_targets'].append(target)
    killed = _record_kill_after_damage(state, u, target, before) if state is not None else False
    if state is not None:
        # 旧调用默认是常规伤害；所有真伤入口必须显式标注，避免漏记自定义伤害。
        if (record_cipher and u is not None and actual_damage > 0
                and damage_type not in {'true', 'true_damage'}):
            cp = next((x for x in state.units
                       if x.char.id == 'cipher' and x.is_alive), None)
            if cp is not None:
                record_damage = actual_damage
                if cipher_record_amount is not None:
                    record_damage = min(record_damage,
                                        max(float(cipher_record_amount), 0.0))
                _cipher_record(state, cp, target, record_damage,
                               rate_multiplier=cipher_record_multiplier,
                               extra_rate=cipher_extra_rate)
        # damage 是实际 HP 损失；请求值另由 submitted_damage 提供。
        state.hooks.trigger_all("on_after_damage", u=u, state=state, enemy=target,
                                damage=actual_damage,
                                submitted_damage=amount,
                                damage_type=damage_type,
                                skill_type=skill_type,
                                attack_type=attack_type,
                                killed=killed)
    return actual_damage, killed


def _target_attacker_stats(stats, u, state, target, skill_type=None):
    """Apply target-dependent attacker stats without mutating the base panel."""
    if u is None or target is None:
        return stats
    result = stats
    if u.char.id == 'welt' and u.eidolon_rank >= 6 \
            and skill_type in ('skill', 'ultimate') \
            and (target.has_status(status_id='welt_slow') or target.has_status(name='减速')):
        result = copy.deepcopy(result)
        result.CRIT_RATE += 0.30
        result.CRIT_DMG += 0.60
    if u.char.id == 'acheron' and u.eidolon_rank >= 1 \
            and target.debuff_count() > 0:
        if result is stats:
            result = copy.deepcopy(result)
        result.CRIT_RATE = min(1.0, result.CRIT_RATE + 0.18)
    if u.char.id == 'acheron' and u.eidolon_rank >= 6 \
            and skill_type == 'ultimate':
        if result is stats:
            result = copy.deepcopy(result)
        result.RES_PEN_ALL += 0.20
    if u.char.id == 'anaxa' and getattr(target, 'extra', {}).get('anaxa_revealed'):
        if result is stats:
            result = copy.deepcopy(result)
        result.DMG_BONUS_ALL += 0.30 if u.eidolon_rank >= 6 else 0.18
    if u.char.id == 'anaxa' and u.extra.get('anaxa_trace3'):
        weak_count = len(_enemy_weakness_elements(target))
        if weak_count:
            if result is stats:
                result = copy.deepcopy(result)
            result.DEF_PEN += min(0.28, weak_count * 0.04)
    return result


def _target_scaling_stat(stats, value, stat_name, state, target):
    """Apply target-dependent scaling-stat bonuses for the current hit."""
    if stat_name != 'ATK' or state is None or target is None or not target.is_broken:
        return value
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive and x.eidolon_rank >= 2), None)
    if ruan is None:
        return value
    return value + stats._base_ATK * 0.40


def _skill_attack_type(state, skill_type):
    """千冶·刃E2 makes allied ultimate damage count as follow-up damage."""
    if skill_type != 'ultimate' or state is None:
        return None
    qianye = next((x for x in state.units
                   if x.char.id == 'qianye' and x.is_alive and x.eidolon_rank >= 2), None)
    return 'follow_up' if qianye is not None and _qianye_wrath_active(qianye) else None



def _multihit_damage(stats: CombatStats, targets: list, scaling_stat: float,
                      scale: float, dmg_type: str, element: str, is_crit: bool,
                      hits: int = 1, skill_type: str = None,
                      true_dmg_ratio: float = 0.0, u=None, state=None,
                      scaling_stat_name: str = None, attack_type: str = None,
                      laugh_n: float = 0.0) -> float:
    total = 0.0
    hit_targets = []
    for _ in range(hits):
        # v6.2.1: 每段重新选择存活目标（Codex P1-1: 固定列表会继续命中已死亡目标）
        alive_now = [e for e in targets if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        hit_targets.append(t)
        was_welt_slow = bool(
            u is not None and u.char.id == 'welt'
            and (t.has_status(status_id='welt_slow') or t.has_status(name='减速'))
        )
        if state is not None:
            _record_lc_attack_target(state, t)
        if state is not None and u is not None:
            _hn_count_hits(state, u)  # v6.7 歼破协议: 每段命中+1充能
        hit_stats = _apply_target_relic_modifiers(stats, u, t) if u is not None else stats
        # v6.7b 歼破协议: 战技弹射段暴击伤害额外+100%
        if skill_type == 'skill' and state is not None \
                and state.extra.get('hn_charge_skill_cd'):
            hit_stats = copy.deepcopy(hit_stats)
            hit_stats.CRIT_DMG += 1.0
        # v5.0 P3 补: 弹射路径同样应用目标相关光锥条件（星海巡航 HP≤50% 等）
        if u is not None and state is not None:
            hit_stats = _lc_target_correct(hit_stats, u, state, t)
        # v6.3.0 银狼E6: 目标每有1个负面效果伤害+20%, 最多+100%
        if u is not None and u.char.id == 'silver_wolf' and u.eidolon_rank >= 6:
            n = min(getattr(t, 'debuff_count', lambda: 0)(), 5)
            if n > 0:
                hit_stats = copy.deepcopy(hit_stats)
                hit_stats.DMG_BONUS_ALL += 0.20 * n
        hit_stats = _target_attacker_stats(hit_stats, u, state, t, skill_type)
        hit_scaling_stat = _target_scaling_stat(
            stats, scaling_stat, scaling_stat_name, state, t)
        hit_enemy = _enemy_for_damage(t)
        d = calculate_damage(hit_stats, hit_enemy, hit_scaling_stat, scale, dmg_type, element, 80,
                             hit_stats.CRIT_RATE >= 0.5 if u is not None else is_crit,
                             skill_type=skill_type, true_dmg_ratio=true_dmg_ratio,
                             attack_type=attack_type, laugh_n=laugh_n,
                             crit_mode="expected" if u is not None else "boolean")
        total += d.final_damage
        _, killed_by_hit = _commit_enemy_damage(
            state, u, t, d.final_damage, damage_type=dmg_type,
            skill_type=skill_type, attack_type=attack_type,
            cipher_record_amount=(d.final_damage / (1.0 + true_dmg_ratio)
                                  if true_dmg_ratio > 0 else None))
        if u is not None and state is not None:
            # v6.2.1: 逐段对齐 _use_skill 管线（Codex P1-1）——声援用实际伤害,
            # 乱蝶结算 + 击杀事件/光锥/倒置的火炬（此前弹射全漏, 且死亡目标仍被打）
            total += _apply_tbr_support(state, u, t, d.final_damage)
            _apply_luandie(state, t, u)
            if killed_by_hit:
                # v5.1: 遐蝶行迹2·倒置的火炬 — 乌黯击杀→死龙速度+100%/1回合
                if u.char.id == 'xiadie' and u.memsprite_unit and u.memsprite_unit.is_alive:
                    u.memsprite_unit.extra['xiadie_spd_boost'] = 1
                    state.log.append('  倒置的火炬: 死龙速度+100%(1回合)')
            # 瓦尔特战技逐段语义: 当段先判断原有减速触发天赋，
            # 再以75%基础概率施加/刷新减速，故首次命中后从第2段开始触发。
            if u.char.id == 'welt' and skill_type == 'skill' and t.HP > 0:
                if was_welt_slow:
                    total += _welt_talent_hit(state, u, t, hit_stats, skill_type)
                if t.HP > 0:
                    _welt_apply_slow(state, u, t)
    if state is not None:
        state.extra['last_multihit_targets'] = hit_targets
    return total


def _target_damage(stats: CombatStats, targets: list, scaling_stat: float,
                   scale: float, dmg_type: str, element: str, is_crit: bool,
                   skill_type: str = None) -> float:
    return sum(calculate_damage(stats, t, scaling_stat, scale, dmg_type, element, 80, is_crit,
                                skill_type=skill_type).final_damage
               for t in targets)


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


def _apply_tbr_support(state, u, t, dmg) -> float:
    """v5.7: 迷迷的声援单点——持有者每造成1次伤害→额外28%真伤（逐段触发, 实机语义;
    此前按"本次行动伤害总额"一次性结算, 多段技能少触发）。
    行迹1·磁石与长链: 能量上限>100每超10点→倍率+2%（最高+20%）; E4: 零能量目标+6%。
    对忆灵生效（E1）: 忆灵循环传 _tbr_support 持有者的 buff 检查。"""
    support = next((b for b in u.buffs
                    if getattr(b, 'attributes', {}).get('_tbr_support')), None)
    if support is None and getattr(u, 'memsprite_unit', None):
        support = next((b for b in u.memsprite_unit.buffs
                        if getattr(b, 'attributes', {}).get('_tbr_support')), None)
    if support is None or dmg <= 0:
        return 0.0
    magnet = 0.0
    if (u.char.max_energy or 0) > 100:
        magnet = min(0.02 * ((u.char.max_energy - 100) // 10), 0.20)
    tbr4 = next((x for x in state.units
                 if x.char.id == 'trailblazer_remembrance' and x.eidolon_rank >= 4), None)
    if tbr4 and (u.char.max_energy or 0) == 0:
        magnet += 0.06
    support_dmg = dmg * 0.28 * (1.0 + magnet)
    _commit_enemy_damage(state, u, t, support_dmg, damage_type='true_damage',
                         record_cipher=False)
    state.log.append(f'  迷迷的声援: 真伤+{support_dmg:.0f}(28%×{1+magnet:.2f})')
    return support_dmg


def _apply_target_relic_modifiers(stats, u, enemy):
    """按当前受击目标应用动态遗器属性，不污染基础面板。"""
    if u is None:
        return stats
    # v5.4 烦恼着，幸福着: 目标【温驯】状态下我方任意攻击者CD+12%/层（最多2层）
    # 温驯 status 只由该光锥施加 → 有温驯即佩戴了光锥, 无条件对所有命中者生效
    wenshun = next((st.attributes.get('wenshun_layers', 0) for st in enemy.statuses
                    if st.id == 'wenshun'), 0)
    if wenshun:
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 0.12 * wenshun
    conditions = getattr(u, '_active_relic_conditions', set())
    if not conditions:
        return stats
    result = copy.deepcopy(stats)
    if 'defpen_per_dot' in conditions:
        result.DEF_PEN += min(enemy.dot_count(), 3) * 0.06
    # v5.2 问题3e: 繁星4pc 量子弱点目标额外10%无视防御（非量子弱点不触发）
    if 'defpen_vs_quantum' in conditions and enemy.element_res.get('量子', 0.2) <= 0:
        result.DEF_PEN += 0.10
    if 'cr_vs_debuff' in conditions and enemy.debuff_count() > 0:
        result.CRIT_RATE = min(1.0, result.CRIT_RATE + 0.10)
        if enemy.has_status(name='禁锢') or enemy.has_status(status_id='break:虚数'):
            result.CRIT_DMG += 0.20
    if 'cd_per_debuff_count' in conditions:
        count = enemy.debuff_count()
        bonus = 0.12 if count >= 3 else (0.08 if count >= 2 else 0.0)
        if u.extra.get('pioneer_double_turns', 0) > 0:
            bonus *= 2
        result.CRIT_DMG += bonus
    return result


# ---- 遐蝶天赋：HP吸收（新蕊/死龙回血） ----

def xiadie_xinrui_cap(u) -> float:
    """遐蝶新蕊上限: 献予「生死」之诗→可溢出至200%（68000）"""
    return 68000.0 if u.extra.get('poem_shengsi') else 34000.0


def _xiadie_absorb_hp_loss(state, lost_amount: float, desc: str = ""):
    """遐蝶天赋·掌心淌过的荒芜: 我方全体(含忆灵)每损失1点HP→1点新蕊。
    死龙在场时无法获得新蕊，改为队友损失→死龙HP恢复。"""
    if lost_amount <= 0:
        return
    xiadie = next((u for u in state.units if u.char.id == 'xiadie' and u.is_alive), None)
    if not xiadie:
        return
    dragon = xiadie.memsprite_unit
    if dragon and dragon.is_alive:
        # 死龙在场: 转化为死龙HP恢复
        dragon.current_hp = min(dragon.max_hp, dragon.current_hp + lost_amount)
        state.log.append(f'  死龙回血+{lost_amount:.0f} (HP损失转化{desc}) HP={dragon.current_hp:.0f}/{dragon.max_hp:.0f}')
    else:
        # 死龙不在场: 1:1转化为新蕊 + 伤害叠层
        old_xr = xiadie.xinrui
        cap = xiadie_xinrui_cap(xiadie)
        xiadie.xinrui = min(cap, xiadie.xinrui + lost_amount)
        if xiadie.xinrui - old_xr > 1:
            state.log.append(f'  新蕊+{xiadie.xinrui - old_xr:.0f} (HP损失{desc}) → {xiadie.xinrui:.0f}/{cap:.0f}')
        # 伤害叠层(上限3层20%)
        st = getattr(xiadie, 'relic_stacks', {}) or {}
        cur = st.get('xiadie_dmg_buff', 0)
        if cur < 3:
            cur += 1
            xiadie.base_stats.DMG_BONUS_ALL += 0.20
            if xiadie.memsprite_unit:
                xiadie.memsprite_unit.base_stats.DMG_BONUS_ALL += 0.20
            st['xiadie_dmg_buff'] = cur
            xiadie.relic_stacks = st


# ---- 遗器条件 ----

def _get_relic_conditions(relics, relic_sets) -> set:
    if not relics or not relic_sets:
        return set()
    set_counts = {}
    for p in relics:
        set_counts[p.set_name] = set_counts.get(p.set_name, 0) + 1
    conds = set()
    for set_name, count in set_counts.items():
        if set_name not in relic_sets:
            continue
        for eff in relic_sets[set_name].effects:
            if count < eff.pieces_required or not eff.condition:
                continue
            # v5.1: 拆解组合码（'a+b' 防数据合并错误导致无法触发）
            for part in eff.condition.split('+'):
                conds.add(part.strip())
    return conds


# ---- Buff 注册表 ----
# 格式: {paramId: {stat: value, ...}}
# value 使用游戏内百分比数值（如 66 = 66%），引擎自动 /100

BUFF_REGISTRY = {
    # 希儿
    "seele_speed_buff": {"SPD_PERCENT": 25.0},
    "seele_buffed_state": {"DMG_BONUS_ALL": 80.0},
    # 布洛妮娅
    "bronya_skill_dmg_buff": {"DMG_BONUS_ALL": 66.0},
    "bronya_ult_buff": {"ATK_PERCENT": 55.0},  # CD部分特殊处理
    "bronya_technique_atk": {"ATK_PERCENT": 15.0},
    # 花火
    "sparkle_cd_buff": {},  # CD=花火CD*0.24+45, 动态计算
    # v6.10.6 C1: 谜诡改为真实3回合状态（_apply_skill_effects 特判挂 TimedBuff）, 不再满层近似DMG+60
    "sparkle_ult_buff": {},
    # 符玄
    "fuxuan_field": {"HP_PERCENT": 6.0, "CRIT_RATE": 12.0},
    "fuxuan_ult_trigger": {},
    "fuxuan_tech_barrier": {},
    # 长夜月
    "changyeyue_ult_state": {"_darkness": 1},  # 特殊标记，空属性触发至暗之谜状态
    "changyeyue_skill_cd": {"CRIT_DMG": 24.0},
    "changyeyue_tech_cd": {"CRIT_DMG": 24.0},
    # 遐蝶
    "xiadie_realm": {"_realm": 1},
    "xiadie_tech": {"_tech": 1},
    # 昔涟
    "xilian_field": {"_field": 1},
    "xilian_ult_ripple": {"_ripple": 1},
    "xilian_tech": {"_tech": 1},
    # 风堇
    "fengjin_ult_state": {"_clear_sky": 1},
    "fengjin_tech": {"_tech": 1},
    # 阿格莱雅
    "aglaea_sovereign": {"_sovereign": 1},
    # 藿藿
    "huohuo_ult_atk": {"ATK_PERCENT": 40.0},  # 终结技队友ATK(行迹控抗精通: ≥160能量队友64%)
    # v5.3 开拓者·同谐
    "tbh_band_dance": {"BREAK_EFFECT": 30.0, "_tbh_super_break": 1},  # 伴舞: 击破特攻+30%+超击破源
    # v5.3 忘归人
    "fugue_foxian": {"BREAK_EFFECT": 30.0, "_foxian": 1},   # 狐祈: 击破特攻+30%+狐祈标记
    "fugue_chizhuo": {"_chizhuo": 1},                       # 炽灼: 强化普攻切换标记
    # v5.3 流萤
    "firefly_combustion": {"_combustion": 1},               # 完全燃烧状态（_apply_skill_effects 特殊分支）
    # v6.7 火花
    "sparxie_live": {"_sparxie_live": 1},                   # 直播连线（下次普攻强化, 一次性）
    "sparxie_e2_cd": {"CRIT_DMG": 10.0},                    # E2: 每消耗1爆点暴伤+10% 2回合(叠4层, 消耗处动态叠加)
    "sparxie_e4_elation": {"ELATION_LEVEL": 36.0},          # E4: 终结技后自身欢愉度+36% 3回合
    # v6.7 大丽花
    "dahlia_field_buff": {"TOUGHNESS_EFFICIENCY": 50.0},    # 结界: 全队弱点击破效率+50%
    # v6.7 姬子·启行
    "himeko_nova_flag": {"DMG_BONUS_ALL": 20.0},            # 领航旗语: 全队伤害+20%
    # v6.11.1 知更鸟·晴歌
    "qingge_guest": {},                                      # 特邀嘉宾: 特殊标记(持有者及其召唤物攻击→晴歌气氛+2; 无法拉条队友)
}

# 命名 paramId 治疗注册表（藿藿等; 数字编码 "hpPct|flat" 走 split 解析回退）
HEAL_REGISTRY = {
    "huohuo_skill_heal_main":     {"hp_pct": 24.0, "flat": 640.0},
    "huohuo_skill_heal_adjacent": {"hp_pct": 19.2, "flat": 512.0},
    # v5.3 灵砂（ATK 基数治疗, 满级档; 行迹2 治疗量提高经 HEAL_BONUS 消费）
    "lingsha_skill_heal":  {"stat": "ATK", "hp_pct": 14.0, "flat": 420.0},
    "lingsha_ult_heal":    {"stat": "ATK", "hp_pct": 12.0, "flat": 360.0},
    "lingsha_fuyuan_heal": {"stat": "ATK", "hp_pct": 12.0, "flat": 360.0},
}

DEBUFF_REGISTRY = {
    # v5.6: base_chance = 实机基础概率（默认1.0=100%）, 由 _roll_effect_hit 检定
    "huohuo_tech_atk_down": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                             "attributes": {"ATK_DOWN": 0.25}},
    "凶星低语": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                "attributes": {"vulnerability": 0.16}},
    # v5.3 灵砂【醇醉】: 受击破伤害+25%（per-type 易伤, damage.py _vuln_mult 消费）
    "lingsha_chunzui": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                        "attributes": {"vulnerability_break": 0.25}},
    "hysilens_vuln": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                       "attributes": {"vulnerability": 0.20}},
    "cipher_weak": {"category": "debuff", "duration": 2, "base_chance": 1.20,
                    "attributes": {"outgoing_dmg_reduction": 0.10}},
    # v5.3 流萤火弱点植入（通用弱点机制: weakness_element → 立即改抗性, 到期恢复）
    # 弱点植入属元素机制不走 EHR 检定（_apply_skill_effects 内跳过）
    "firefly_fire_weakness": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                              "attributes": {"weakness_element": "火", "weakness_old_res": 0.0}},
    # v6.3.0 银狼终结技: 全敌DEF-45% 3回合（满级120%基础概率必中）
    "silver_wolf_def_down": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                             "attributes": {"def_reduction": 0.45}},
    # v6.7 绯英行迹1·行裁断: 狐狸老师施放攻击→全敌易伤12% 3回合
    "evanescia_vuln": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                       "attributes": {"vulnerability": 0.12}},
    # v6.7 大丽花终结技·败谢: 防御-18% + 共舞者属性弱点（弱点动态, _apply_skill_effects 特判）
    "the_dahlia_baisie": {"category": "debuff", "duration": 4, "base_chance": 1.0,
                          "attributes": {"def_reduction": 0.18}},
}

MANUAL_BUFF_PARAM_IDS = {
    'cerydra_jungong',
    'dht_tongpao',
    'res_pen_buff',
    'tribbie_shenqi',
    'yaoguang_field',
}

SPECIAL_DEBUFF_PARAM_IDS = {
    'silver_wolf_all_res_down',
    'silver_wolf_weakness',
}


# ---- v5.3 行动条标记行动分发（marker_id → fn(state, marker)）----

# ════════ v6.3.0 银狼机制（角色技能介绍/银狼.txt）════════
# v6.3.0b P1-7/P1-12: (消费键=小写, 数值, 独立status id, 显示名) ——
# 独立 id 使三类缺陷并存（此前固定 id 互相覆盖, debuff 计数被低估）
SILVER_WOLF_DEFECTS = [
    ('atk_down', 0.10, 'silver_wolf_defect_atk', '攻击力降低'),
    ('def_reduction', 0.12, 'silver_wolf_defect_def', '防御力降低'),
    ('spd_down', 0.06, 'silver_wolf_defect_spd', '速度降低'),
]


def _silver_wolf_apply_entry_effects(state):
    """银狼E1/E6：对当前波敌人施加入场领域效果。"""
    sw = next((x for x in state.units if x.char.id == 'yinlang'
               and x.is_alive), None)
    e1_active = bool(sw and sw.eidolon_rank >= 1 and sw.invincible_active)
    if e1_active:
        for enemy in state.enemies:
            enemy.extra['yinlang_e1_vuln'] = 0.20
    else:
        for enemy in state.enemies:
            enemy.extra.pop('yinlang_e1_vuln', None)
    if sw is None:
        return
    if sw.eidolon_rank >= 6:
        for enemy in state.enemies:
            if enemy.extra.get('silver_wolf_e6_entry_applied'):
                continue
            for elem, res in list(enemy.element_res.items()):
                enemy.element_res[elem] = -0.20 if res == 0.0 else 0.0
            enemy.extra['silver_wolf_e6_entry_applied'] = True
    if sw.eidolon_rank >= 1 or sw.eidolon_rank >= 6:
        fields = []
        if e1_active:
            fields.append('E1结界易伤')
        if sw.eidolon_rank >= 6:
            fields.append('E6禁限弱点')
        state.log.append(f'  银狼星魂: 当前波敌人入场效果已施加({"、".join(fields) or "无结界"})')


def _silver_wolf_trace1_active(u):
    return u.extra.get('silver_wolf_trace1', False)


def _silver_wolf_implant_defect(state, u, target):
    """银狼天赋: 攻击后100%基础概率植入1个随机缺陷（3回合; 行迹1+1=4回合）
    缺陷: 攻击力-10%/防御力-12%/速度-6% 三选一（银狼.txt 天赋·等待程序响应…）"""
    if target is None or getattr(target, 'HP', 0) <= 0:
        return False
    from engine.models.enemy import EnemyStatus
    key, val, sid, name = random.choice(SILVER_WOLF_DEFECTS)
    duration = 4 if _silver_wolf_trace1_active(u) else 3
    target.add_status(EnemyStatus(id=sid, name=name, category='debuff',
                                  source='silver_wolf', remaining_turns=duration,
                                  attributes={key: val}))
    state.log.append(f'  银狼缺陷: {target.name or target.id} {name}-{val*100:.0f}% ({duration}回合)')
    return True


def _apply_silver_wolf_weakness(u, state, target):
    """银狼战技: 添加1个队友属性弱点（优先编队第一位角色属性, 抗性-20% 3回合;
    若为原属性弱点不降抗; 仅保留最新1个——status id 固定覆盖）
    v6.3.0b P1-11: 快照记录首次施加前的纯抗性(剔除全抗降低偏移); 同元素刷新保留
    首快照; 换元素先还原旧元素再写新快照; 到期由 _begin_enemy_turn 恢复。"""
    from engine.models.enemy import EnemyStatus
    # 优先编队第一位（position 最小）角色属性
    first = min(state.units, key=lambda x: getattr(x, 'position', 99))
    elem = first.char.element
    existing = next((s for s in target.statuses if s.id == 'silver_wolf_weakness'), None)
    all_res_active = target.has_status(status_id='silver_wolf_all_res_down')
    # 换元素: 先按旧快照还原旧元素（全抗降低仍在时重挂偏移）
    if existing and existing.attributes.get('weakness_element') != elem:
        old_elem = existing.attributes.get('weakness_element')
        old_res = existing.attributes.get('weakness_old_res',
                                          target.get_res(old_elem) + (0.13 if all_res_active else 0.0))
        target.element_res[old_elem] = old_res - (0.13 if all_res_active else 0.0)
        state.log.append(f'  银狼弱点更换: {old_elem}抗性恢复({old_res*100:.0f}%)')
        existing = None
    # 快照: 同元素刷新保留首次快照; 新植入取纯抗性(当前抗性+全抗降低偏移)
    if existing is None:
        old_res = target.get_res(elem) + (0.13 if all_res_active else 0.0)
    else:
        old_res = existing.attributes.get('weakness_old_res',
                                          target.get_res(elem) + (0.13 if all_res_active else 0.0))
    new_res = old_res - 0.20 if old_res > 0 else old_res  # 原属性弱点不降抗
    target.element_res[elem] = (min(new_res, -0.2) if old_res > 0 else new_res) \
        - (0.13 if all_res_active else 0.0)
    # v6.7 弱点植入事件（大丽花行迹3消费）
    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                            element=elem, target=target)
    if existing:
        existing.remaining_turns = 3
        existing.attributes['weakness_element'] = elem
        existing.attributes['weakness_old_res'] = old_res
    else:
        target.add_status(EnemyStatus(id='silver_wolf_weakness', name='弱点植入', category='debuff',
                                      source='silver_wolf', remaining_turns=3,
                                      attributes={'weakness_element': elem,
                                                  'weakness_old_res': old_res}))
    state.log.append(f'  银狼弱点植入: {elem} (抗性{old_res*100:.0f}%→{target.element_res[elem]*100:.0f}%, 3回合)')
    return True


def _apply_silver_wolf_all_res_down(u, state, target):
    """银狼战技: 全属性抗性-13% 2回合（100%基础概率; 到期由 _begin_enemy_turn 恢复）"""
    from engine.models.enemy import EnemyStatus
    existing = next((s for s in target.statuses if s.id == 'silver_wolf_all_res_down'), None)
    if existing:
        existing.remaining_turns = 2
    else:
        target.add_status(EnemyStatus(id='silver_wolf_all_res_down', name='全抗降低', category='debuff',
                                      source='silver_wolf', remaining_turns=2,
                                      attributes={}))
        for elem in list(target.element_res):
            target.element_res[elem] = target.element_res.get(elem, 0) - 0.13
    state.log.append(f'  银狼全抗-13%: {target.name or target.id} (2回合)')
    return True


def _pick_fire_weak_target(pool):
    """优先选择韧性值>0且有火属性弱点的目标"""
    if not pool:
        return None
    fire_weak = [t for t in pool if t.toughness > 0 and t.element_res.get('火', 0.2) <= 0]
    return random.choice(fire_weak) if fire_weak else random.choice(pool)


def _marker_heal_allies(state, healer, heal_id):
    """行动条标记治疗（HEAL_REGISTRY 命名, ATK/HP 基数, 含 HEAL_BONUS）"""
    named = HEAL_REGISTRY.get(heal_id)
    if not named:
        return 0.0
    stats = _build_effective_stats(healer, state)
    heal_base = stats.ATK if named.get("stat") == "ATK" else stats.HP
    amt = (heal_base * named["hp_pct"] / 100 + named["flat"]) * (1.0 + stats.HEAL_BONUS)
    tgt_list = [x for x in state.units if x.is_alive] + \
               [ms for ms in state.memsprites if ms.is_alive]
    for t in tgt_list:
        t.current_hp = min(t.max_hp, t.current_hp + amt)
    state.hooks.trigger_all("on_heal", u=healer, state=state, healer=healer,
                            targets=tgt_list, heal_amt=amt)
    _fengjin_talent_heal_buff(state, healer)
    # 行动条标记治疗与角色技能治疗使用同一光锥事件管线。
    state.extra['lc_last_heal_amt'] = amt
    _process_lc_effects(healer, state, "on_heal")
    state.log.append(f'  治疗: {amt:.0f}×{len(tgt_list)}人')
    return amt


def _lingsha_fuyuan_action(state, marker):
    """浮元行动: 全队追击(75%ATK火伤)+随机单体(75%ATK)+削韧(全体每目标10+单体10)
    +净化全员1负面+全队治疗; E4治疗最低HP队友; E6额外4次50%ATK+每次削韧5（用户确认数值）"""
    summoner = next((x for x in state.units if x.char.id == 'lingsha' and x.is_alive), None)
    sys = state.extra.get('_marker_sys')
    if summoner is None:
        if sys:
            sys.despawn(state, marker)  # 召唤者阵亡浮元消失（双保险）
        return
    stats = _build_effective_stats(summoner, state)
    alive = state.alive_enemies() or state.enemies
    if not alive:
        return
    dmg_total = 0.0

    def _fua_damage_hit(t, scale, toughness):
        """浮元单段伤害：大公逐段事件在命中后广播。"""
        nonlocal dmg_total
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale, "direct", "火", 80,
                             False, crit_mode="expected", attack_type="follow_up")
        _commit_enemy_damage(state, summoner, t, d.final_damage)
        dmg_total += d.final_damage
        dmg_total += _apply_toughness_damage(state, summoner, t, toughness, "火", "talent", stats)
        state.hooks.trigger_all("on_followup_hit", u=summoner, state=state)

    # ① 全体追击 + 削韧（每目标10）
    for t in list(alive):
        if t.HP <= 0:
            continue
        _fua_damage_hit(t, 75.0, 10.0)
    # ② 额外随机单体（优先韧>0且火弱点）+ 削韧10
    pool = [t for t in alive if t.HP > 0]
    if pool:
        single = _pick_fire_weak_target(pool)
        _fua_damage_hit(single, 75.0, 10.0)
        # ③ E6: 额外4次（50%ATK + 每次削韧5）
        if summoner.eidolon_rank >= 6:
            for _ in range(4):
                tgt = _pick_fire_weak_target([t for t in alive if t.HP > 0])
                if tgt is None:
                    break
                _fua_damage_hit(tgt, 50.0, 5.0)
    summoner.total_damage_dealt += dmg_total
    state.log.append(f'  浮元: 全队追击+单体 {dmg_total:.0f}')
    _qingge_notify_attack(state, summoner, dealt=dmg_total > 0)  # v7.1.0 P1: marker行动攻击补气氛
    # 追加攻击动作完成后仅广播一次：温驯/流光、千星、都蓝等均为动作级效果。
    # 浮元此前遗漏光锥侧 on_followup 处理。
    _process_lc_effects(summoner, state, "on_followup")
    state.hooks.trigger_all("on_followup", u=summoner, state=state)
    # ④ 净化全员1负面
    _fengjin_cleanse(state, summoner)
    # ⑤ 全队治疗（12%ATK+360, 满级; ATK基数）
    _marker_heal_allies(state, summoner, "lingsha_fuyuan_heal")
    # ⑥ E4: 治疗当前HP最低队友 40%ATK
    if summoner.eidolon_rank >= 4:
        alive_units = [x for x in state.units if x.is_alive]
        if alive_units:
            lowest = min(alive_units, key=lambda x: x.current_hp / max(x.max_hp, 1))
            amt = stats.ATK * 0.40 * (1.0 + stats.HEAL_BONUS)
            lowest.current_hp = min(lowest.max_hp, lowest.current_hp + amt)
            state.hooks.trigger_all("on_heal", u=summoner, state=state, healer=summoner,
                                    targets=[lowest], heal_amt=amt)
            _fengjin_talent_heal_buff(state, summoner)
            state.log.append(f'  浮元E4: 治疗{lowest.char.name}+{amt:.0f}')


def _lingsha_fuyuan_spawn_e6(state, marker, summoner):
    """灵砂E6: 浮元在场→敌方全属性抗性-20%（消失时恢复）"""
    if summoner.eidolon_rank >= 6:
        for e in state.enemies:
            for elem in e.element_res:
                e.element_res[elem] -= 0.20
            e.extra['lingsha_e6_res_down'] = e.extra.get('lingsha_e6_res_down', 0) + 1
        state.log.append('  灵砂E6: 敌方全属性抗性-20%')


def _lingsha_fuyuan_despawn(state, marker):
    """灵砂E6: 浮元消失→恢复敌方全属性抗性"""
    for e in state.enemies:
        n = e.extra.get('lingsha_e6_res_down', 0)
        if n > 0:
            for elem in e.element_res:
                e.element_res[elem] += 0.20 * n
            e.extra['lingsha_e6_res_down'] = 0
            state.log.append('  灵砂E6: 敌方全属性抗性恢复')


def _firefly_exit_combustion(u, state):
    """退出完全燃烧: 还原属性（倒计时行动/其他退出路径）"""
    if not u.extra.get('combustion'):
        return
    u.extra['combustion'] = False
    u.base_stats.SPD -= 60.0
    u.base_stats.EFFECT_RES -= 0.30
    u.base_stats.BREAK_EFFECT -= 0.25  # 行迹1 燃烧期加成还原
    if u.eidolon_rank >= 4:
        u.base_stats.EFFECT_RES -= 0.50
    if u.eidolon_rank >= 6:
        u.base_stats.RES_PEN['火'] -= 0.20
    u.extra.pop('countdown_turns', None)
    state.log.append('  完全燃烧结束: 状态解除')


def _firefly_countdown_action(state, marker):
    """完全燃烧倒计时行动(70速): 解除燃烧 + 移除标记"""
    summoner = next((x for x in state.units if x.char.id == 'firefly' and x.is_alive), None)
    if summoner:
        _firefly_exit_combustion(summoner, state)
    sys = state.extra.get('_marker_sys')
    if sys:
        sys.despawn(state, marker)



def _robin_concert_marker_action(state, marker):
    """v6.9.1: 协奏90速倒计时行动——知更鸟自己的回合被协奏跳过,
    必须由独立行动条标记触发到期（Codex P1-1）。"""
    robin = next((x for x in state.units
                  if x.char.id == marker.summoner_id and x.is_alive), None)
    if robin is not None:
        _robin_tick(state, robin)
    if robin is not None and _robin_concert_active(robin):
        marker.extra['next_av'] = state.current_av + AV_PER_TURN / 90.0
        return
    sys = state.extra.get('_marker_sys')
    if sys:
        sys.despawn(state, marker)


def _qianye_wrath_marker_action(state, marker):
    """无量忿怒70速倒计时首次行动时立即解除结界。"""
    qianye = next((x for x in state.units
                   if x.char.id == marker.summoner_id and x.is_alive), None)
    if qianye is not None:
        _qianye_exit_wrath(state, qianye, fatal=False)
        state.log.append('  无量忿怒倒计时到期: 结界解除')
    elif marker.is_alive:
        sys = state.extra.get('_marker_sys')
        if sys:
            sys.despawn(state, marker)


MARKER_ACTIONS = {
    "lingsha_fuyuan": _lingsha_fuyuan_action,
    "firefly_countdown": _firefly_countdown_action,
    "robin_concert": _robin_concert_marker_action,
    "qianye_wrath": _qianye_wrath_marker_action,
    # "qingge_countdown": 延迟注册（函数定义在模块后部, 见晴歌区块末尾）
}
MARKER_DESPAWN = {
    "lingsha_fuyuan": _lingsha_fuyuan_despawn,
}
MARKER_SPAWN = {
    "lingsha_fuyuan": _lingsha_fuyuan_spawn_e6,
}


def _pick_single_ally_target(state, u):
    """单体友方技能目标解析（v5.6）:
    优先携带 single_ally_priority 标记的存活角色（船长4pc持有者, 需被队友选中叠Help）
    → 默认主C seele → 施放者自身。数据驱动, 未来嘲讽角色复用同标记。"""
    for x in state.units:
        if x.is_alive and x.extra.get('single_ally_priority'):
            return x
    seele = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if seele is not None:
        return seele
    return next((x for x in state.units if x.is_alive and x is not u), u)


def _roll_effect_hit(u, state, enemy, effect_name, base_chance=1.0):
    """玩家→敌方 debuff 命中检定（v5.6）:
    最终命中 = 基础概率 × (1 + 效果命中) × (1 - 敌方效果抵抗)
    enemy.effect_res 默认 0（无数据=必中, 真实敌人数据录入后激活）。
    击破异常不检定（实机弱点击破必中, 见 _apply_break_debuff）。
    防御: 无 base_stats 的 mock 单元按 EHR=0 处理（测试构造）。"""
    ehr = 0.0
    if getattr(u, 'base_stats', None) is not None:
        ehr = _build_effective_stats(u, state).EFFECT_HIT_RATE
    chance = base_chance * (1.0 + ehr) * (1.0 - enemy.effect_res)
    if chance >= 1.0:
        return True
    if random.random() >= chance:
        state.log.append(f'  抵抗: {enemy.name or enemy.id} 抵抗{effect_name} (命中{chance:.0%})')
        return False
    return True


def _apply_skill_effects(u: SimUnit, state: SimState, skill, skill_key: str):
    """将技能的 effects[] 转化为 TimedBuff 应用到目标"""
    from engine.models.character import SkillEffect

    single_ally_target = None
    has_single_ally_target = getattr(skill, 'target', None) == 'single_ally' or any(
        (getattr(eff, 'target', None) if not isinstance(eff, dict)
         else eff.get('target')) == 'single_ally'
        for eff in skill.effects
    )
    if has_single_ally_target:
        single_ally_target = _pick_single_ally_target(state, u)
        u.extra['lc_last_skill_target'] = single_ally_target
        state.hooks.trigger_all("on_ally_skill_targeted", u=u, state=state,
                                target=single_ally_target, skill_key=skill_key)

    for eff in skill.effects:
        etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')

        # 忆灵召唤：通用处理器
        if etype == 'summon_memsprite':
            if u.char.memsprite:
                from engine.systems.remembrance import RemembranceSystem
                if state.extra.get('_rem_sys') is None:
                    state.extra['_rem_sys'] = RemembranceSystem()
                state.extra['_rem_sys'].summon_memsprite(state, u, u.char.memsprite)
            continue

        # v5.3 行动条标记（浮元/完全燃烧倒计时）: 惰性创建系统
        if etype == 'spawn_marker':
            sys = _ensure_marker_system(state)  # v6.3.0b P1-1: 统一惰性创建入口
            param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
            sys.spawn(state, u, param_id)
            continue

        # v5.3 行动提前（标记提前/自身提前, 首次接线）
        if etype == 'action_advance':
            target = eff.target if hasattr(eff, 'target') else eff.get('target', 'self')
            ratio = (eff.value if hasattr(eff, 'value') else eff.get('value', 0)) / 100.0
            if target == 'marker':
                sys = state.extra.get('_marker_sys')
                if sys:
                    sys.advance(state, u, ratio)
            elif target == 'self':
                navs = state.extra.get('navs', {})
                uidx = state.units.index(u) if u in state.units else -1
                if uidx >= 0 and uidx in navs:
                    navs[uidx] = max(0, navs[uidx] - (AV_PER_TURN / _effective_spd(u, state)) * ratio)
                    state.log.append(f'  行动提前: {u.char.name} {ratio*100:.0f}%')
            elif target in ('single_ally', 'ally') and single_ally_target is not None \
                    and single_ally_target is not u:
                # v6.11.1 特邀嘉宾: 持有者无法使其他友方目标获得行动提前
                if any(getattr(b, 'param_id', '') == 'qingge_guest' for b in u.buffs):
                    state.log.append('  【特邀嘉宾】: 无法使其他友方获得行动提前')
                    continue
                navs = state.extra.get('navs', {})
                target_idx = (state.units.index(single_ally_target)
                              if single_ally_target in state.units else -1)
                if target_idx >= 0 and target_idx in navs:
                    advanced_av = max(
                        state.current_av,
                        navs[target_idx]
                        - (AV_PER_TURN / _effective_spd(single_ally_target, state)) * ratio,
                    )
                    _set_av(state, navs, target_idx, advanced_av)
                    state.log.append(
                        f'  行动提前: {single_ally_target.char.name} {ratio*100:.0f}%')
            continue

        if etype == 'debuff':
            param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
            target_type = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
            template = DEBUFF_REGISTRY.get(param_id)
            if template is None and param_id not in SPECIAL_DEBUFF_PARAM_IDS:
                state.log.append(f'  [WARN] 未注册debuff paramId={param_id or "<empty>"}, 按通用减益记录')
                template = {"category": "debuff", "duration": 2,
                            "attributes": {"value": getattr(eff, 'value', 0)}}
            targets = _select_targets(state.alive_enemies() or state.enemies, target_type)
            applied_targets = []
            for target in targets:
                # v6.3.0 银狼战技: 动态元素弱点（队伍第一位角色属性, 非固定元素）
                if param_id == 'silver_wolf_weakness':
                    if _apply_silver_wolf_weakness(u, state, target):
                        applied_targets.append(target)
                    continue
                # v6.3.0 银狼战技: 全属性抗性降低（改 element_res, 到期恢复）
                if param_id == 'silver_wolf_all_res_down':
                    if _apply_silver_wolf_all_res_down(u, state, target):
                        applied_targets.append(target)
                    continue
                # v6.7 大丽花终结技·败谢: 防御-18% + 共舞者属性弱点（弱点动态, 特判）
                if param_id == 'the_dahlia_baisie':
                    _apply_dahlia_baisie(u, state, target)
                    applied_targets.append(target)
                    continue
                attrs = template["attributes"].copy()
                # v5.6: 命中检定（弱点植入属元素机制不走 EHR, 实机必中）
                if 'weakness_element' not in attrs and not _roll_effect_hit(
                        u, state, target, param_id or skill.name, template.get("base_chance", 1.0)):
                    continue
                # v5.3 弱点植入（流萤强化战技火弱点）: 立即改抗性, 到期由 _begin_enemy_turn 恢复
                if 'weakness_element' in attrs:
                    elem = attrs['weakness_element']
                    status_id = param_id or f'{u.char.id}:{skill_key}:debuff'
                    existing = next((s for s in target.statuses if s.id == status_id), None)
                    e6_res_down = target.extra.get('lingsha_e6_res_down', 0) * 0.20
                    # 刷新同一植入状态时，保留首次施加前的基础抗性快照；
                    # 浮元E6 的全抗降低独立叠加，不能被植入效果覆盖。
                    attrs['weakness_old_res'] = (
                        existing.attributes.get('weakness_old_res', target.get_res(elem))
                        if existing and existing.attributes.get('weakness_element') == elem
                        else target.get_res(elem) + e6_res_down
                    )
                    target.element_res[elem] = min(attrs['weakness_old_res'], -0.2) - e6_res_down
                    state.log.append(f'  {elem}弱点植入 → {target.name or target.id} (2回合)')
                    # v6.7 弱点植入事件（大丽花行迹3消费）
                    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                                            element=elem, target=target)
                target.add_status(EnemyStatus(
                    id=param_id or f'{u.char.id}:{skill_key}:debuff',
                    name=param_id or skill.name,
                    category=template["category"],
                    source=u.char.id,
                    remaining_turns=template["duration"],
                    attributes=attrs,
                ))
                # v6.10 on_debuff_applied 事件（黄泉天赋消费）
                state.hooks.trigger_all("on_debuff_applied", u=u, state=state,
                                        target=target)
                applied_targets.append(target)
            if applied_targets:
                state.log.append(f'  debuff {param_id or skill.name} → {len(applied_targets)}名敌人')
                if 'cd_per_debuff_count' in getattr(u, '_active_relic_conditions', set()):
                    u.extra['pioneer_double_pending'] = True
                # v5.0.1: 施加 debuff 事件（好戏开演·戏法叠层）
                _process_lc_effects(u, state, "on_debuff_apply")
            continue

        # v5.0 P5: 护盾效果（盾值 = eff.value × (1+SHIELD_BONUS)）
        if etype == 'shield':
            t_type = eff.target if hasattr(eff, 'target') else 'self'
            if t_type in ('all_allies', 'all'):
                targets = [x for x in state.units if x.is_alive]
            elif t_type in ('single_ally', 'ally'):
                targets = [x for x in state.units if x.is_alive and x is not u][:1] or [u]
            else:
                targets = [u]
            bonus = _build_effective_stats(u, state).SHIELD_BONUS
            shield_val = float(getattr(eff, 'value', 0) or 0) * (1.0 + bonus)
            for t in targets:
                t.shield += shield_val
                state.hooks.trigger_all("on_shield", u=u, state=state,
                                        targets=[t], shield_amt=shield_val)
                # v6.11.1 晴歌行迹2·即兴蓝调(护盾侧) + 渠道b气氛（与治疗共享每回合去重）
                _qingge_on_heal_shield(state, provider=u, targets=[t])
                state.log.append(f'  护盾: {t.char.name}+{shield_val:.0f}')
                # 隐士4pc: 持盾队友→CD+15%（受盾者佩戴时激活）
                if 'shield_ally_cd' in getattr(t, '_active_relic_conditions', set()):
                    t.buffs.append(TimedBuff(source_id='隐士4pc', attributes={'CRIT_DMG': 15.0},
                                             remaining_turns=2, source_name='隐士4pc'))
                    state.log.append(f'  隐士4pc: {t.char.name} CD+15%(持盾)')
            continue
        # v5.0 P4: 净化效果（解除目标 1 个控制/减益状态, 负属性 buff 兜底）
        if etype == 'cleanse':
            t_type = eff.target if hasattr(eff, 'target') else 'all_allies'
            if t_type in ('all_allies', 'all'):
                targets = [x for x in state.units if x.is_alive]
            elif t_type in ('single_ally', 'ally'):
                targets = [single_ally_target] if single_ally_target else []
            else:
                targets = [u]
            cleared = 0
            for t in targets:
                for st in list(getattr(t, 'statuses', [])):
                    if st.category in ('control', 'debuff'):
                        t.statuses.remove(st)
                        state.hooks.trigger_all("on_exit_state", u=t, state=state, status=st)
                        cleared += 1
                        break
                if cleared < 1:
                    for b in list(t.buffs):
                        if any(v < 0 for v in b.attributes.values()):
                            t.buffs.remove(b)
                            cleared += 1
                            break
            if cleared:
                state.log.append(f'  净化: 解除{cleared}个负面效果')
            continue
        if etype != 'buff':
            continue

        param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
        if param_id == 'laugh_gain_5':
            amount = eff.value if hasattr(eff, 'value') else eff.get('value', 0.0)
            state.laugh_points += amount
            state.log.append(f'  笑点+{amount:.0f}')
            continue
        if param_id == 'sparkle_ult_buff':
            # v6.10.6 C1: 花火终结技——全体谜诡3回合 + 回6战技点(溢出记录≤10)
            for eu in state.units:
                if eu.is_alive:
                    eu.buffs = [b for b in eu.buffs
                                if getattr(b, 'param_id', '') != 'sparkle_mystery']
                    eu.buffs.append(TimedBuff(source_id='sparkle', attributes={},
                                              remaining_turns=3, param_id='sparkle_mystery',
                                              source_name='谜诡'))
            _sparkle_ult_sp(state)
            state.log.append('  花火终结技: 全体谜诡(3回合) + 回6战技点')
            continue
        if param_id == 'cd_buff':
            if single_ally_target is not None:
                single_ally_target.tb_cd_buff_turns = 3
                state.log.append(
                    f'  暴击伤害+50% → {single_ally_target.char.name} (3回合)')
            continue
        if param_id == 'hidden_score_gain':
            amount = eff.value if hasattr(eff, 'value') else eff.get('value', 0.0)
            elation = state.extra.get('_elation')
            if elation is not None:
                elation.gain_hidden_score(state, u, amount)
            else:
                u.hidden_score = min(300.0, u.hidden_score + amount)
            continue
        if param_id in MANUAL_BUFF_PARAM_IDS:
            continue
        if param_id not in BUFF_REGISTRY:
            state.log.append(f'  [WARN] 未注册buff paramId={param_id or "<empty>"}')
            continue
        attrs = BUFF_REGISTRY[param_id].copy()

        # v5.3 流萤完全燃烧状态（进入）
        if param_id == 'firefly_combustion' and u.char.id == 'firefly':
            if u.extra.get('combustion'):
                state.log.append('  [WARN] 已在完全燃烧状态')
                continue
            u.extra['combustion'] = True
            u.extra['countdown_turns'] = 3.0  # 倒计时由行动条 marker 调度（70速）
            u.base_stats.SPD += 60.0
            u.base_stats.EFFECT_RES += 0.30
            u.base_stats.BREAK_EFFECT += 0.25  # 行迹1: 燃烧期击破特攻+25%
            navs = state.extra.get('navs', {})
            uidx = state.units.index(u) if u in state.units else -1
            if uidx >= 0 and uidx in navs:
                navs[uidx] = state.current_av  # 行动提前100%
            if u.eidolon_rank >= 4:
                u.base_stats.EFFECT_RES += 0.50  # E4: 燃烧期效果抵抗+50%
            if u.eidolon_rank >= 6:
                u.base_stats.RES_PEN['火'] += 0.20  # E6: 火属性抗性穿透+20%
            u.extra['ff_trace1_pull'] = 0  # 行迹1 倒计时延后计数（每燃烧期3次）
            u.extra['ff_e2_used'] = False
            state.log.append('  进入【完全燃烧】: 速度+60, 行动提前100%, 击破效率+50%, 强化普攻/强化战技')
            continue

        # v5.3 忘归人狐祈: 仅最新目标生效（先移除全体旧狐祈）; E6炽灼时全队化
        if param_id == 'fugue_foxian':
            for eu in state.units:
                kept = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'fugue_foxian']
                if len(kept) != len(eu.buffs):
                    eu.buffs = kept
                eu.extra.pop('_foxian', None)
            e6_all = (u.eidolon_rank >= 6 and
                      any(getattr(b, 'attributes', {}).get('_chizhuo') for b in u.buffs))
            if e6_all:
                tgt = [x for x in state.units if x.is_alive]
                state.log.append('  E6: 炽灼状态狐祈对我方全体生效')
            else:
                # 单目标狐祈: 主C惯例（与 single_ally 通用分支口径一致）
                main = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
                tgt = [main] if main else [u]
            for t in tgt:
                t.buffs.append(TimedBuff(source_id=u.char.id,
                                         attributes=dict(BUFF_REGISTRY['fugue_foxian']),
                                         remaining_turns=3, source_name=skill.name,
                                         param_id='fugue_foxian'))
                t.extra['_foxian'] = True  # 狐祈标记（削韧减半/效率/击破伤害判定用）
                state.log.append(f'  buff 狐祈 → {t.char.name} (3回合)')
            continue

        # 特殊处理：花火战技CD buff（E6: 额外+花火暴伤30%）
        if param_id == 'sparkle_cd_buff':
            cd_val = u.base_stats.CRIT_DMG * 0.24 + 0.45
            if u.eidolon_rank >= 6:
                cd_val += u.base_stats.CRIT_DMG * 0.30
            attrs = {'CRIT_DMG': round(cd_val * 100, 1)}
            # v6.10.6 C1: E6 战技CD效果扩散至持有谜诡的队友; E1 施放战技时花火SPD+15%刷新
            if u.eidolon_rank >= 6:
                for eu in state.units:
                    if eu.is_alive and any(getattr(b, 'param_id', '') == 'sparkle_mystery'
                                           for b in eu.buffs):
                        eu.buffs = [b for b in eu.buffs
                                    if getattr(b, 'param_id', '') != 'sparkle_cd_buff']
                        eu.buffs.append(TimedBuff(source_id='sparkle',
                                                  attributes=dict(attrs),
                                                  remaining_turns=2,
                                                  param_id='sparkle_cd_buff',
                                                  source_name='花火战技·梦游鱼(E6扩散)'))
            if u.eidolon_rank >= 1:
                u.buffs = [b for b in u.buffs
                           if getattr(b, 'param_id', '') != 'sparkle_e1_spd']
                u.buffs.append(TimedBuff(source_id='sparkle',
                                         attributes={'SPD_PERCENT': 15.0},
                                         remaining_turns=2, param_id='sparkle_e1_spd',
                                         source_name='花火E1·悬置怀疑'))

        # 特殊处理：布洛妮娅终结技CD部分
        if param_id == 'bronya_ult_buff':
            cd_val = u.base_stats.CRIT_DMG * 0.16 + 0.20
            attrs = {'ATK_PERCENT': 55.0, 'CRIT_DMG': round(cd_val * 100, 1)}

        # 献予「岁月」之诗: 长夜月战技CD额外+长夜月暴伤12%
        if param_id == 'changyeyue_skill_cd' and u.extra.get('poem_suiyue'):
            attrs = {'CRIT_DMG': 24.0 + u.base_stats.CRIT_DMG * 12.0}

        # v6.7 大丽花战技: 开启结界——统一走 _dahlia_field_apply（设置回合数+全队buff,
        # v6.7b: 此前战技只走通用 buff 路径不设 dahlia_field_turns, 未破韧转化核心机制失效）
        if param_id == 'dahlia_field_buff':
            _dahlia_field_apply(state, u)
            continue

        target_type = eff.target if hasattr(eff, 'target') else eff.get('target', 'self')
        duration = 2  # 默认2回合

        # 特殊持续时间（布洛妮娅E6: 战技增伤+1回合）
        if param_id == 'bronya_skill_dmg_buff':
            duration = 2 if u.eidolon_rank >= 6 else 1
        elif param_id == 'fuxuan_field':
            duration = 3
        elif param_id == 'fengjin_ult_state':
            duration = 3
        elif param_id == 'himeko_nova_flag':
            # v6.7b: 领航旗语 3回合（此前默认2）+ 立即恢复所有助战技次数（txt）
            duration = 3
            # v7.2.0 #3: E5战技+2 → 旗语增伤按战技等级消费(每级+5%, 基准Lv10=20%)
            attrs['DMG_BONUS_ALL'] = 20.0 * _skill_level_factor(u, 'skill')
            if u.char.id == 'himeko_nova':
                state.extra['hn_support_uses'] = _hn_support_cap(u)
                state.log.append('  领航旗语: 立即恢复所有助战技使用次数')
        elif param_id in ('tbh_band_dance', 'fugue_foxian', 'fugue_chizhuo'):
            duration = 3  # v5.3: 伴舞/狐祈/炽灼均为3回合

        # 长夜月终结技→至暗之谜
        if param_id == 'changyeyue_ult_state' and u.char.id == 'changyeyue':
            # v6.2.1: 防重入——重复终结技仅刷新充能, 加成不叠加（Harness P1-2）
            if not u.is_darkness:
                # 至暗之谜: 敌方全体受伤+30%（施加到敌方，全队受益）
                for e in state.enemies:
                    e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
                u.base_stats.DMG_BONUS_ALL += 0.60
                if u.memsprite_unit:
                    u.memsprite_unit.base_stats.DMG_BONUS_ALL += 0.60
            u.is_darkness = True
            u.darkness_charges = 2
            if u.eidolon_rank >= 2:
                u.darkness_charges += 2
            state.log.append(f'  进入【至暗之谜】(充能={u.darkness_charges}): 敌方受伤+30%, 伤害+60%')
            continue

        # 遐蝶终结技→遗世冥域（召唤死龙由 summon_memsprite 通用处理器完成）
        if param_id == 'xiadie_realm' and u.char.id == 'xiadie':
            # 遗世冥域
            if state.realm_owner and state.realm_owner != 'xiadie':
                state.log.append(f'  [WARN] 境界已被{state.realm_owner}占据，无法展开遗世冥域')
                continue
            state.realm_owner = 'xiadie'
            state.realm_turns = 3
            for e in state.enemies:
                for elem in e.element_res:
                    e.element_res[elem] -= 0.20
            state.log.append('  展开【遗世冥域】(3回合): 敌方全属性抗性-20%')
            continue

        # 昔涟战技→结界
        # v7.2.0 项目主裁决: 昔涟没有境界技能（她是遐蝶/白厄的售后角色）——
        # 结界不读写 realm_owner, 不参与境界互斥; 独立倒计时存 xilian_field_turns
        if param_id == 'xilian_field' and u.char.id == 'xilian':
            state.extra['xilian_field_turns'] = 2
            # 昔涟E2: 结界真伤=基础24% + 每有1名不同角色获得德谬歌忆灵技增益+6%（上限48%）
            # v6.2: 累计口径——已获增益角色记录于 xilian_e2_gifted, 由 _xilian_support_skill 递增
            gifted = state.extra.get('xilian_e2_gifted', set())
            if u.eidolon_rank >= 2:
                state.realm_true_dmg = min(0.48, 0.24 + 0.06 * len(gifted))
                state.log.append(f'  昔涟E2: 结界真伤={state.realm_true_dmg:.2f} ({len(gifted)}名角色获增益)')
            else:
                state.realm_true_dmg = 0.24
            state.log.append('  展开结界(2回合): 我方伤害附加24%真伤')
            u.zhuiyi += 3
            continue

        # 昔涟终结技→进入涟漪 + 激活全体队友终结技（单场1次）
        if param_id == 'xilian_ult_ripple' and u.char.id == 'xilian':
            if u.extra.get('xilian_ult_used'):
                state.log.append('  [WARN] 昔涟终结技单场只能施放1次')
                continue
            u.extra['xilian_ult_used'] = True
            u.is_ripple = True
            u.base_stats.CRIT_RATE += 0.50
            if u.memsprite_unit:
                u.memsprite_unit.base_stats.CRIT_RATE += 0.50
            state.extra['xilian_field_turns'] = -1  # v7.2.0: 结界永久(独立于境界系统)
            u.story_points += 1  # 终结技后+1故事
            state.log.append('  进入【往昔的涟漪】: CR+50%, 结界永久, 普攻强化')
            # 首次终结技第二个效果（用户确认实机）: 选择释放 花与箭/此诗献予 共2次忆灵技
            # （第1次由召唤立即行动完成; 此处补第2次; 每次扣12追忆, 终结技总消耗24）
            if u.memsprite_unit and u.memsprite_unit.is_alive:
                from engine.systems.remembrance import RemembranceSystem
                rem = state.extra.get('_rem_sys') or RemembranceSystem()
                rem._xilian_memsprite_action(state, u, u.memsprite_unit)
            # 昔涟E6: 首次终结技→全队拉条100%
            if u.eidolon_rank >= 6:
                navs = state.extra.get('navs', {})
                for i, eu in enumerate(state.units):
                    if eu.is_alive and i in navs \
                            and not _guest_advance_blocked(state, u, eu):
                        navs[i] = state.current_av
                state.log.append('  昔涟E6: 首次终结技→全队拉条100%')
            # 激活全体队友的终结技（入 X 轴队列排队，不消耗回合）
            for eu in state.units:
                if eu is u or not eu.is_alive:
                    continue
                if eu.char.id == 'changyeyue':
                    continue  # 长夜月终结技为状态技，不在此触发
                if eu.char.max_energy and eu.char.max_energy > 0:
                    _enqueue_ult(state, eu)
                    state.log.append(f'  >>> 昔涟终结技激活: {eu.char.name} 终结技入队')
            continue  # 已经处理完毕，不需要创建TimedBuff

        # 阿格莱雅终结技→至高之姿
        if param_id == 'aglaea_sovereign' and u.char.id == 'aglaea':
            u.is_sovereign = True
            # v5.7: 终结技→自身立即行动（阿格莱雅.txt: 进入【至高之姿】状态并使自身立即行动）
            navs = state.extra.get('navs', {})
            uid = state.units.index(u)
            if uid in navs:
                navs[uid] = state.current_av
                state.log.append('  终结技: 自身立即行动')
            # 献予「浪漫」之诗: 持【浪漫】进入至高→双方伤害+72%无视36%防御
            if u.extra.get('poem_langman') and not u.extra.get('romantic_applied'):
                from engine.systems.remembrance import _romantic_apply
                _romantic_apply(u)
                state.log.append('  献予「浪漫」之诗: 至高之姿增强生效(72%/36%)')
            # 获得衣匠忆灵天赋的速度提高层数（每层15%速度）
            u.extra['sovereign_spd_stack'] = 0
            # 行动序列倒计时（100速度，简化：衣匠回合时检查）
            u.extra['countdown_turns'] = 3
            # 行迹3·短视之惩: 至高之姿时攻击力 += 阿格莱雅速度×720% + 衣匠速度×360%
            spd_bonus_atk = u.base_stats.SPD * 7.20
            ms = u.memsprite_unit
            if ms:
                spd_bonus_atk += ms.base_stats.SPD * 3.60
            u.base_stats.ATK += spd_bonus_atk
            if ms:
                ms.base_stats.ATK += spd_bonus_atk
            u.extra['sovereign_atk_bonus'] = spd_bonus_atk  # v6.2.1: 退出时对称回减
            state.log.append(f'  短视之惩: 攻击力+{spd_bonus_atk:.0f}(速度×720%+衣匠速度×360%)')
            # 至高之姿: 获得衣匠忆灵天赋速度层数(每层自身速度+15%, v5.7 数据驱动)
            if ms:
                stack = ms.extra.get('spd_stack', 0)
                if stack > 0:
                    spd_gain = u.base_stats._base_SPD * (u.char.sovereign_spd_pct / 100) * stack
                    u.base_stats.SPD += spd_gain
                    u.extra['sovereign_spd_bonus'] = spd_gain
                    state.log.append(f'  至高之姿: 衣匠{stack}层速度→自身速度+{spd_gain:.0f}')
            # E6: 至高之姿时自身与衣匠雷抗穿透+20%
            if u.eidolon_rank >= 6:
                u.base_stats.RES_PEN['雷'] += 0.20
                if ms:
                    ms.base_stats.RES_PEN['雷'] += 0.20
                state.log.append('  E6: 雷属性抗性穿透+20%')
            state.log.append('  进入【至高之姿】: 普攻强化为孤锋千吻, 无法施放战技')
            continue

        # 解析目标
        targets = []
        if target_type == 'self':
            targets = [u]
        elif target_type == 'single_ally':
            main = single_ally_target or _pick_single_ally_target(state, u)
            targets = [main]
        elif target_type == 'all_allies' or target_type == 'all_allies_but_self':
            targets = [x for x in state.units if x.is_alive and x != u] if 'but_self' in target_type else [x for x in state.units if x.is_alive]

        for target in targets:
            # 希儿战技加速: 同ID buff 上限1层(rank0刷新)/2层(E2), 移除最旧保持滚动
            if param_id == 'seele_speed_buff':
                cap = 2 if u.eidolon_rank >= 2 else 1
                same = [b for b in target.buffs if getattr(b, 'param_id', '') == 'seele_speed_buff']
                while len(same) >= cap:
                    target.buffs.remove(same.pop(0))
            tb = TimedBuff(source_id=u.char.id, attributes=attrs, remaining_turns=duration,
                           source_name=skill.name, param_id=param_id)
            target.buffs.append(tb)

        if targets:
            names = ','.join(t.char.name for t in targets[:2])
            if len(targets) > 2: names += f'...(+{len(targets)-2})'
            state.log.append(f'  buff {param_id} → {names} ({duration}回合)')


# ---- 光锥效果处理器 ----

def _lc_team_advance(state, ratio, actor=None):
    """全队行动提前（各自速度）; v7.1.0: actor持【特邀嘉宾】时只拉自己(防永动机)"""
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu.is_alive and i in navs and not _guest_advance_blocked(state, actor, eu):
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * ratio)
    state.log.append(f'  光锥拉条: 全队{ratio*100:.0f}%')


def _lc_ally_buff(state, unit, attrs, duration):
    """给下一个行动队友加buff"""
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if target:
        tb = TimedBuff(source_id=unit.char.id, attributes=attrs, remaining_turns=duration)
        target.buffs.append(tb)
        state.log.append(f'  光锥buff → {target.char.name}({duration}回合)')


def _lc_sp_recovery(state, interval=2):
    """每N次终结技回1SP"""
    c = state.extra.get('lc_sp_counter', 0) + 1
    state.extra['lc_sp_counter'] = c
    if c >= interval:
        _gain_skill_points(state)
        state.extra['lc_sp_counter'] = 0
        state.log.append('  光锥回SP')


def _lc_wave_heal(state, ratio=0.80):
    """波次开始群体回血"""
    for u in state.units:
        if u.is_alive and u.current_hp < u.max_hp:
            lost = u.max_hp - u.current_hp
            heal = lost * ratio
            u.current_hp = min(u.max_hp, u.current_hp + heal)
            if heal > 1:
                state.log.append(f'  波次回血: {u.char.name}+{heal:.0f}HP')


# 光锥触发器注册表: {paramId: (event_type, handler)}
# event_type: "on_ult" / "on_skill" / "on_wave_start"
LC_TRIGGERS = {
    "lc_but_the_battle_isnt_over_sp": ("on_ult", lambda s, u, ctx: _lc_sp_recovery(s, 2)),
    "lc_but_the_battle_isnt_over_dmg": ("on_skill", lambda s, u, ctx: _lc_ally_buff(s, u, {'DMG_BONUS_ALL': 30.0}, 1)),
    "lc_dance_dance_dance_advance": ("on_ult", lambda s, u, ctx: _lc_team_advance(s, 0.24, actor=u)),
    "lc_she_already_shut_her_eyes_heal": ("on_wave_start", lambda s, u, ctx: _lc_wave_heal(s, 0.80)),
}

# ---- 光锥 condition 动态化（v5.0 P3） ----
# 事件型条件码 → 触发事件（_process_lc_effects 的 event_type 命名）
LC_EVENT_CODES = {
    "event_battle_start": "on_battle_start",
    "event_skill_after": "on_skill",
    "event_ult_after": "on_ult",
    "event_basic_after": "on_basic_attack",
    "event_kill": "on_kill",
    "event_wave_start": "on_wave_start",
    "event_followup": "on_followup",
    "event_hit_taken": "on_hit_taken",
    # v5.4 无用效果重审扩展
    "event_self_attack": "on_self_attack",
    "event_attack": "on_attack",
    "event_weakness_break": "on_weakness_break",
    "event_turn_start": "on_self_turn_start",
    "event_heal": "on_heal",
    "event_hp_loss": "on_hp_loss",
    "event_memsprite_attack": "on_memsprite_attack",
    "event_memsprite_despawn": "on_memsprite_despawn",
}

def _lc_rank_value(u, default, code=None):
    """v5.7: 从光锥效果 values（精炼1-5档）按当前叠影等级取值; 无数据回退默认（S1）。
    code: 按 condition_code 区分同光锥多条 values（如生命当付之一炬 弱点增伤/减防双条）"""
    eff = next((e for e in getattr(u.lightcone, 'effects', [])
                if getattr(e, 'values', None) and (code is None or e.condition_code == code)),
               None)
    if not eff or not eff.values:
        return default
    idx = min(max(getattr(u.lightcone, 'rank', 1) - 1, 0), len(eff.values) - 1)
    return eff.values[idx]


# v5.1: 无属性数据的事件型效果（回能/回血/回SP类）动作处理
# 键: (光锥id, 事件令牌) → (state, u) 动作
LC_EVENT_ACTIONS = {
    # 时代铭记: 施放终结技攻击后恢复1个战技点
    ("epoch_etched_in_golden_blood", "on_ult"):
        lambda s, u: (_gain_skill_points(s),
                      s.log.append('  光锥[epoch_etched_in_golden_blood] 终结技回1SP')),
    # 当她决定看见: 每波次开始恢复15能量
    ("when_she_decided_to_see", "on_wave_start"):
        lambda s, u: (_gain_energy(u, 15.0, state=s),
                      s.log.append('  光锥[when_she_decided_to_see] 波次回15能量')),
    # 镜中故我: 每个波次开始时我方全体恢复10点能量
    ("past_self_in_mirror", "on_wave_start"):
        lambda s, u: ([_gain_energy(x, 10.0, state=s) for x in s.units if x.is_alive],
                      s.log.append('  光锥[past_self_in_mirror] 波次全队回10能量')),
    # 镜中故我: 终结技后的全队增伤由事件缓冲器处理；击破特攻达标时额外回1SP。
    ("past_self_in_mirror", "on_ult"):
        lambda s, u: _lc_restore_sp_if_be_threshold(s, u),
    # 孤独的疗愈: 陷入装备者持续伤害效果的敌方目标被消灭时回6能量（近似: 击杀即回）
    ("solitary_healing", "on_kill"):
        lambda s, u: (_gain_energy(u, 6.0, state=s),
                      s.log.append('  光锥[solitary_healing] 击杀回6能量')),
    # 无可取代的东西: 消灭敌方目标或受到攻击后回装备者攻击力8%的生命
    ("something_irreplaceable", "on_kill"):
        lambda s, u: _lc_heal_pct_atk(s, u, 0.08, '击杀回血'),
    ("something_irreplaceable", "on_hit_taken"):
        lambda s, u: _lc_heal_pct_atk(s, u, 0.08, '受击回血'),
    # v5.4 无用效果重审修复（全部为实机有效效果的建模）
    # 等价交换: 回合开始随机为1个能量<50%的队友回16能量
    ("quid_pro_quo", "on_self_turn_start"):
        lambda s, u: _lc_quid_pro_quo(s, u),
    # 致长夜的星光: 忆灵消失时回8能量
    ("starlight_to_the_long_night", "on_memsprite_despawn"):
        lambda s, u: (_gain_energy(u, 8.0, state=s),
                      s.log.append('  光锥[starlight_to_the_long_night] 忆灵消失回8能量')),
    # 回到大地的飞行: 队友对单体施放2次战技或终结技后回1战技点
    ("a_grounded_ascent", "on_skill"):
        lambda s, u: _lc_grounded_ascent_counter(s, u),
    ("a_grounded_ascent", "on_ult"):
        lambda s, u: _lc_grounded_ascent_counter(s, u),
    # 今日亦是和平的一日: 增伤 = 能量上限×0.4%（最多计入160点）
    ("today_is_another_peaceful_day", "on_battle_start"):
        lambda s, u: _lc_energy_cap_dmg_bonus(s, u),
    # 生命当付之一炬: 攻击使目标DEF降低 2回合（v5.7: 按叠影档 12/15/18/21/24%）
    ("life_should_be_cast_to_flames", "on_self_attack"):
        lambda s, u: _lc_apply_enemy_def_down(s, u, _lc_rank_value(u, 12.0, code='event_self_attack') / 100,
                                              2, 'def_reduction', 'life_flames_def_down'),
    # 决心如汗珠般闪耀: 击中时16%基础概率使目标【攻陷】DEF-16% 1回合
    ("resolution_shines_as_pearls_of_sweat", "on_self_attack"):
        lambda s, u: _lc_apply_gongxian(s, u),
    # 梦应归于何处: 造成击破伤害时使目标【溃败】2回合（装备者击破伤害+24%/敌速-20%）
    ("whereabouts_should_dreams_rest", "on_weakness_break"):
        lambda s, u: _lc_apply_kubai(s, u),
    # 烦恼着，幸福着: 施放追加攻击后使目标【温驯】最多2层（我方命中温驯目标CD+12%/层）
    ("worrisome_blissful", "on_followup"):
        lambda s, u: _lc_apply_wenshun(s, u),
    # 时节不居: 治疗记录→攻击后附加伤害36%（每回合最多1次）
    ("time_waits_for_no_one", "on_heal"):
        lambda s, u: _lc_record_heal(s, u),
    ("time_waits_for_no_one", "on_self_attack"):
        lambda s, u: _lc_heal_record_extra_damage(s, u),
    ("time_waits_for_no_one", "on_attack"):
        lambda s, u: _lc_heal_record_extra_damage(s, u),
    # 如泥酣眠: 普攻/战技未造成暴击时CR+36% 1回合（期望模式: 按未暴击概率1-CR触发, 3回合CD）
    ("sleep_like_the_dead", "on_self_attack"):
        lambda s, u: _lc_sleep_like_dead_miss_crit(s, u),
    # v5.4 第二批（用户提供S1数值后实现）
    # 落日时起舞/制胜的瞬间: 受击概率提高500%/200%（×6/×3, 用户确认）
    ("dance_at_sunset", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 6.0),
    ("moment_of_victory", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 3.0),
    # 与行星相会: 我方造成与装备者相同属性伤害时提高12%（v5.7: 按叠影档 values 取）
    ("planetary_rendezvous", "on_battle_start"):
        lambda s, u: _lc_same_element_dmg_bonus(s, u, _lc_rank_value(u, 12.0)),
    # 朗道的选择: 受击概率提高200%（×3, 用户确认）
    ("landaus_choice", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 3.0),
    # 后会有期: 普攻/战技后对随机受击目标造成48%ATK附加伤害（v5.7: 按叠影档 values 取, /100 转小数）
    ("we_will_meet_again", "on_basic_attack"):
        lambda s, u: _lc_extra_flat_damage(s, u, _lc_rank_value(u, 48.0) / 100),
    ("we_will_meet_again", "on_skill"):
        lambda s, u: _lc_extra_flat_damage(s, u, _lc_rank_value(u, 48.0) / 100),
    # 在火的远处: 单次受击/自耗≥25%生命上限→回15%HP+增伤 2回合（3回合CD; v5.7: 增伤按叠影档）
    ("flames_afar", "on_hp_loss"):
        lambda s, u: _lc_flames_afar(s, u, 0.25, _lc_rank_value(u, 25.0)),
    # 生命当付之一炬: 回合开始回10能量（弱点增伤60%在 _lc_target_correct 消费）
    ("life_should_be_cast_to_flames", "on_self_turn_start"):
        lambda s, u: (_gain_energy(u, 10.0, state=s),
                      s.log.append('  光锥[life_should_be_cast_to_flames] 回合开始回10能量')),
    # 美梦小镇大冒险: 施放技能后全队对应类型增伤（最新类型生效, v5.7: 按叠影档）
    ("dreamville_adventure", "on_basic_attack"):
        lambda s, u: _lc_childlike_mark(s, u, 'basic_attack'),
    ("dreamville_adventure", "on_skill"):
        lambda s, u: _lc_childlike_mark(s, u, 'skill'),
    ("dreamville_adventure", "on_ult"):
        lambda s, u: _lc_childlike_mark(s, u, 'ultimate'),
    # 论剑: 多次击中同一目标叠层/层（5层, 换目标重置; v5.7: 按叠影档）
    ("swordplay", "on_self_attack"):
        lambda s, u: _lc_swordplay_stack(s, u),
    # 爱如此刻永恒: 忆灵技后【空白】/【诗行】（当局永久, 用户确认; 双持×1.6）
    ("this_love_forever", "on_memsprite_attack"):
        lambda s, u: _lc_love_forever(s, u),
    # v6.11.1 你将起身歌唱(晴歌专属): 终结技后回1战技点
    ("rise_and_sing", "on_ult"):
        lambda s, u: (_gain_skill_points(s),
                      s.log.append('  光锥[你将起身歌唱] 终结技回1战技点')),
    # 你将起身歌唱: 进战行动提前(叠影档) + 【新声】2回合全队速度提高(叠影档)
    ("rise_and_sing", "on_battle_start"):
        lambda s, u: _rise_and_sing_entry(s, u),
}


def _lc_heal_pct_atk(state, u, pct, tag):
    """光锥回血动作: 回装备者攻击力 pct 的生命（不可超上限）"""
    atk = _build_effective_stats(u, state).ATK
    heal = atk * pct
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    state.log.append(f'  光锥[{getattr(u, "lightcone", None).id if getattr(u, "lightcone", None) else "lc"}] {tag}+{heal:.0f}')


def _lc_restore_sp_if_be_threshold(state, u):
    """镜中故我: 终结技后，击破特攻达到150%时恢复1个战技点。"""
    if _build_effective_stats(u, state).BREAK_EFFECT < 1.5:
        return
    _gain_skill_points(state)
    state.log.append('  光锥[past_self_in_mirror] 击破特攻达标→回1SP')


def _lc_apply_event_effect(state, u, event):
    """光锥事件缓冲器: 事件触发时按 condition_code 匹配的 effect 挂 TimedBuff。

    对应属性已在 _apply_lc_condition_corrections 恒负向抵消（事件型属性本不该常驻），
    这里挂上临时 buff 恢复。duration 从 condition 文本提取（'N回合'），默认 2。
    """
    lc = getattr(u, 'lightcone', None)
    if not lc:
        return
    code = next((k for k, v in LC_EVENT_CODES.items() if v == event), None)
    if not code:
        return
    import re
    matched = [eff for eff in lc.effects
               if getattr(eff, 'condition_code', '') == code]
    action = LC_EVENT_ACTIONS.get((lc.id, event))
    if action and any(not (eff.attributes or {}) for eff in matched):
        action(state, u)
    for eff in matched:
        attrs = eff.attributes or {}
        if not attrs:
            # v5.1: 回能/回血/回SP类动作处理（LC_EVENT_ACTIONS）
            if not action:
                state.log.append(f'  [WARN] 光锥[{lc.id}]事件效果无属性数据(未建模): {eff.condition[:40]}…')
            continue
        m = re.search(r'(\d+)\s*回合', eff.condition or '')
        dur = int(m.group(1)) if m else 2
        targets = [x for x in state.units if x.is_alive] if eff.target == 'all_allies' else [u]
        for t in targets:
            t.buffs.append(TimedBuff(source_id=lc.id, attributes=attrs,
                                     remaining_turns=dur,
                                     source_name=f'光锥·{lc.name}'))
            state.log.append(f'  光锥[{lc.id}] {event}: {t.char.name} +{list(attrs.keys())} ({dur}回合)')


def _lc_negative(s, attrs):
    """对面板副本负向抵消属性（与 attributes.py 静态施加同基线）"""
    for k, v in (attrs or {}).items():
        _apply_stat(s, k, -v)


# 状态门槛型条件码求值器: (u, state, target) -> bool（True=条件满足保留属性）
def _lc_state_enemies_ge_3(u, state, target=None):
    return len([e for e in state.enemies if e.HP > 0]) >= 3


def _lc_state_spd_threshold(u, state, target=None):
    """速度>100 阈值（于夜色中: 每超10点普攻战技+6%/终结技暴伤+12%, 各6层）
    手算战斗内速度: base_stats 已含遗器与静态百分比折叠, 战斗 buff/status 的
    SPD_PERCENT 只加字段不折叠——不调 _effective_spd, 避免经
    _build_effective_stats → 光锥条件修正 互相递归"""
    extra_pct = 0.0
    for b in getattr(u, 'buffs', []):
        extra_pct += getattr(b, 'attributes', {}).get('SPD_PERCENT', 0.0)
    for st in getattr(u, 'statuses', []):
        extra_pct += getattr(st, 'attributes', {}).get('SPD_PERCENT', 0.0)
    spd = u.base_stats.SPD + u.base_stats._base_SPD * (extra_pct / 100.0)
    return spd > 100.0


def _lc_state_energy_cap(u, state, target=None):
    return (u.char.max_energy or 0) >= 300


def _lc_state_be_threshold(u, state, target=None):
    return u.base_stats.BREAK_EFFECT >= 1.5


def _lc_state_energy_full(u, state, target=None):
    return u.current_energy >= (u.char.max_energy or 0)


def _lc_state_shield(u, state, target=None):
    return getattr(u, 'shield', 0.0) > 0.0


def _lc_state_elation(u, state, target=None):
    return any(x.char.id == 'trailblazer_elation' for x in state.units if x.is_alive)


def _lc_state_team(u, state, target=None):
    return state.max_sp >= 6


def _lc_state_taunt(u, state, target=None):
    return True  # 嘲讽值+75: 由 _select_enemy_target 读 condition_code 加权


def _lc_state_hp50_target(u, state, target):
    if target is None:
        return True
    bp = state.extra.get('enemy_blueprint')
    if bp and bp.HP > 0:
        return target.HP <= bp.HP * 0.50
    max_hp = max((e.HP for e in state.enemies if e.HP > 0), default=0)
    return max_hp > 0 and target.HP <= max_hp * 0.50


def _lc_state_target_debuff(u, state, target):
    if target is None:
        return True
    return target.debuff_count() > 0


def _lc_state_ehr_ge_80(u, state, target=None):
    """效果命中≥80%（好戏开演 2 段: ATK+36%）
    读静态面板避免 _build_effective_stats 递归（EHR 战斗 buff 少见, 简化）"""
    return u.base_stats.EFFECT_HIT_RATE >= 0.8


LC_STATE_EVALUATORS = {
    "state_enemies_ge_3": _lc_state_enemies_ge_3,
    "state_spd_threshold": _lc_state_spd_threshold,
    "state_energy_cap_ge_300": _lc_state_energy_cap,
    "state_be_threshold": _lc_state_be_threshold,
    "state_energy_full": _lc_state_energy_full,
    "state_shield_related": _lc_state_shield,
    "state_elation_path": _lc_state_elation,
    "state_team_related": _lc_state_team,
    "state_taunt_buff": _lc_state_taunt,
    "state_hp_below_50_target": _lc_state_hp50_target,
    "state_target_debuff_related": _lc_state_target_debuff,
    "state_ehr_ge_80": _lc_state_ehr_ge_80,
}
# 目标相关码: 需伤害循环内按目标求值
LC_TARGET_CODES = ("state_hp_below_50_target", "state_target_debuff_related",
                   "count:target_debuffs")


def _lc_count_evaluate(state, u, source, target=None) -> int:
    """计数型光锥条件: 按计数源返回当前值（v5.0.1）"""
    if source == 'enemies_alive':
        return len([e for e in state.enemies if e.HP > 0])
    if source == 'sp_spent':
        return state.extra.get('lc_sp_spent', 0)
    if source == 'target_debuffs':
        if target is None:
            return 0
        return target.debuff_count()
    return 0


def _lc_scope_targets(state, owner, target_kind):
    """返回光锥效果的玩家侧目标，供叠层的跨单位面板结算复用。"""
    alive = [x for x in state.units if x.is_alive]
    if target_kind == 'all_allies':
        return alive
    if target_kind == 'all_allies_except_self':
        return [x for x in alive if x is not owner]
    if target_kind == 'ally_main':
        return [x for x in alive if x is not owner][:1] or [owner]
    return [owner]


def _apply_lc_condition_corrections(u, state, s, target=None, only_codes=None):
    """光锥条件修正: 对不满足条件的 effect 属性负向抵消（s 为副本）。

    - event_*: 恒负向抵消（属性由事件缓冲器挂 TimedBuff 恢复）
    - state_*: 求值不满足 → 负向抵消; 目标相关码在 target 传入时按目标求值
    - typed_permanent: 限定属性型常驻; unsupported: 保持常驻 + WARN 一次
    - only_codes: v6.2.1 仅处理前缀匹配的码（结算副本只重跑目标码,
      避免 event_*/stack_*/count:* 在 _lc_target_correct 里被二次负向抵消）
    """
    lc = getattr(u, 'lightcone', None)
    if not lc or lc.path != u.char.path:
        return
    if state is None:
        return  # 无战斗上下文（初始AV等）不修正, 保守保留属性
    warned = state.extra.setdefault('lc_warned', set())
    for eff in lc.effects:
        code = getattr(eff, 'condition_code', '')
        attrs = eff.attributes or {}
        if not code or not attrs:
            continue
        if only_codes and not any(code.startswith(c) for c in only_codes):
            continue
        if code.startswith('event_'):
            _lc_negative(s, attrs)
        elif code == 'typed_permanent':
            continue
        elif code == 'unsupported':
            if state and lc.id not in warned:
                warned.add(lc.id)
                state.log.append(f'  [WARN] 光锥[{lc.id}]条件未建模(保持常驻近似): {eff.condition[:36]}…')
        elif code.startswith('stack:') or code.startswith('stack_full:'):
            # v5.0.1 叠层/标记: attrs=满层总量, 按 (max-count)/max 比例抵消
            _, key, max_s = code.split(':')
            # 静态属性只初始化在持有者面板中。若效果目标是另一名队友，
            # 持有者必须移除静态残留，实际加成由团队分发补入。
            if u not in _lc_scope_targets(state, u, getattr(eff, 'target', 'self')):
                _lc_negative(s, attrs)
                continue
            count = u.lc_stacks.get(f'{lc.id}::{key}', 0)
            mx = int(max_s)
            if code.startswith('stack_full:'):
                if count < mx:
                    _lc_negative(s, attrs)  # 未叠满 → 全量抵消
            elif count < mx:
                r = (mx - min(count, mx)) / mx
                _lc_negative(s, {k: v * r for k, v in attrs.items()})
        elif code.startswith('count:'):
            # v5.0.1 计数型（场上敌人数/SP消耗/目标debuff数）
            _, source, max_s = code.split(':')
            # 目标负面数只能在伤害循环拿到具体目标后结算。此处预先抵消会使
            # _lc_target_correct 再抵消一次，造成属性重复扣除。
            if source == 'target_debuffs' and target is None:
                continue
            count = _lc_count_evaluate(state, u, source, target)
            mx = int(max_s)
            if count < mx:
                r = (mx - min(count, mx)) / mx
                _lc_negative(s, {k: v * r for k, v in attrs.items()})
        elif code in LC_STATE_EVALUATORS:
            if not LC_STATE_EVALUATORS[code](u, state, target):
                _lc_negative(s, attrs)


def _apply_team_lc_stack_buffs(u, state, s):
    """将其他角色光锥的叠层型队伍/单体增益应用到目标有效面板。"""
    if state is None:
        return
    for owner in state.units:
        if owner is u or not owner.is_alive:
            continue
        lc = getattr(owner, 'lightcone', None)
        if not lc or lc.path != owner.char.path:
            continue
        for eff in lc.effects:
            code = getattr(eff, 'condition_code', '') or ''
            target_kind = getattr(eff, 'target', 'self')
            if target_kind not in ('all_allies', 'all_allies_except_self', 'ally_main'):
                continue
            if u not in _lc_scope_targets(state, owner, target_kind):
                continue
            attrs = eff.attributes or {}
            if not attrs or not (code.startswith('stack:') or code.startswith('stack_full:')):
                continue
            _, key, max_s = code.split(':')
            mx = int(max_s)
            if mx <= 0:
                continue
            count = min(u.lc_stacks.get(f'{lc.id}::{key}', 0), mx)
            scale = 1.0 if code.startswith('stack_full:') and count >= mx else (
                0.0 if code.startswith('stack_full:') else count / mx)
            for attr, value in attrs.items():
                _apply_stat(s, attr, value * scale)


def _lc_target_correct(stats, u, state, target):
    """伤害循环内按目标求值目标相关光锥条件（返回可能为副本）"""
    lc = getattr(u, 'lightcone', None)
    if not lc:
        return stats
    need = any(any((getattr(e, 'condition_code', '') or '').startswith(c)
                   for c in LC_TARGET_CODES)
               and (e.attributes or {}) for e in lc.effects)
    # v5.4 生命当付之一炬/论剑: 目标相关消费（不受 need 门控, 数值由引擎内联）
    inline_target = lc.id in ('life_should_be_cast_to_flames', 'swordplay')
    if not need and not inline_target:
        return stats
    s = copy.deepcopy(stats)
    # v6.2.1: 结算副本只重跑目标码（Harness P1-1: 此前全量重跑使 event_*/stack_* 二次抵消）
    _apply_lc_condition_corrections(u, state, s, target=target, only_codes=LC_TARGET_CODES)
    # v5.4 生命当付之一炬: 目标拥有装备者添加的弱点 → 装备者伤害提高（v5.7: 按叠影档）
    if lc.id == 'life_should_be_cast_to_flames':
        for st in getattr(target, 'statuses', []):
            if st.source == u.char.id and 'weakness_element' in st.attributes:
                s.DMG_BONUS_ALL += _lc_rank_value(u, 60.0, code='state:enemy_has_weakness') / 100
                break
    # v5.4 论剑: 目标与叠层记录一致 → 伤害×(1+每层%×层)（v5.7: 每层%按叠影档, 最多5层）
    if lc.id == 'swordplay' and u.extra.get('swordplay_tid') == target.id:
        s.DMG_BONUS_ALL += (_lc_rank_value(u, 16.0) / 100) * u.extra.get('swordplay_layers', 0)
    return s


def _lc_maybe_gain_stack(state, unit, eff, event_type, target_count=0):
    """叠层/标记触发分发（v5.0.1）: stack_gain:{event}:{key}:{max}:{target}:{duration}:{op}

    event 支持 '|' 并集; target: self/all_allies/ally_main; duration 0=无限;
    op: ''/turn_end:-1/clear_on:attack/clear_on:self_turn_start/per_target
    """
    parts = eff.condition_code.split(':', 6)
    # 格式: stack_gain / event / key / max / [target] / [duration] / [op]
    # op 可含内部冒号（turn_end:-1 / clear_on:attack）, 用 split(':',6) 保留末段
    events, key = parts[1], parts[2]
    mx = int(parts[3]) if len(parts) > 3 and parts[3] else 1
    target_kind = parts[4] if len(parts) > 4 and parts[4] else 'self'
    duration = int(parts[5]) if len(parts) > 5 and parts[5] else 0
    op = parts[6] if len(parts) > 6 else ''
    # 清除事件与 gain 事件可能不同（烈阳: gain on_ult / clear on_self_turn_start）
    # → 清除判定独立于 gain 事件匹配, 且先于叠层（演算: 攻击后重置再按命中叠）
    lc_id = getattr(unit, 'lightcone', None).id if getattr(unit, 'lightcone', None) else 'lc'
    if 'clear_on:attack' in op and event_type == 'on_self_attack':
        u_stacks, u_turns = [], []
        for t in ([x for x in state.units if x.is_alive] if target_kind == 'all_allies' else [unit]):
            u_stacks.append(t.lc_stacks); u_turns.append(t.lc_stack_turns)
        for st, tn in zip(u_stacks, u_turns):
            st.pop(f'{lc_id}::{key}', None); tn.pop(f'{lc_id}::{key}', None)
    if 'clear_on:self_turn_start' in op and event_type == 'on_self_turn_start':
        for t in ([x for x in state.units if x.is_alive] if target_kind == 'all_allies' else [unit]):
            t.lc_stacks.pop(f'{lc_id}::{key}', None)
            t.lc_stack_turns.pop(f'{lc_id}::{key}', None)
        return  # 纯清除事件（烈阳: gain 是 on_ult, 本事件不叠层）
    if event_type not in events.split('|'):
        return
    gain = 1
    if 'per_target' in op:
        gain = max(target_count, 0)  # 按本次命中敌数叠层
    if target_kind == 'all_allies':
        targets = [x for x in state.units if x.is_alive]
    elif target_kind == 'ally_main':
        targets = [x for x in state.units if x.is_alive and x is not unit][:1] or [unit]
    else:
        targets = [unit]
    for t in targets:
        key_full = f'{lc_id}::{key}'
        cur = t.lc_stacks.get(key_full, 0)
        nxt = min(cur + gain, mx)
        if nxt > cur:
            t.lc_stacks[key_full] = nxt
            state.log.append(f'  光锥[{lc_id}] {key} 叠层 {nxt}/{mx}')
            if duration > 0:
                # v5.6: 每层独立计时——新层各自 append 独立倒计时（不再扁平刷新）
                turns = t.lc_stack_turns.setdefault(key_full, [])
                for _ in range(nxt - cur):
                    turns.append(duration)
        elif duration > 0 and gain > 0 and nxt == cur == mx:
            # v5.6: 满层后再触发 → 替换最旧层（每层独立计时, 实机满层覆盖最旧）
            turns = t.lc_stack_turns.setdefault(key_full, [])
            for _ in range(min(gain, mx)):
                if turns:
                    turns.pop(0)
                turns.append(duration)
            state.log.append(f'  光锥[{lc_id}] {key} 满层刷新 {cur}/{mx}')


def _lc_maybe_remove_stack(state, unit, eff, event_type):
    """叠层移除触发: stack_remove:{event}:{key}:{target}"""
    parts = eff.condition_code.split(':')
    events, key = parts[1], parts[2]
    if event_type not in events.split('|'):
        return
    key_full = f'{getattr(unit, "lightcone", None).id if getattr(unit, "lightcone", None) else "lc"}::{key}'
    for t in ([x for x in state.units if x.is_alive] if len(parts) > 3 and parts[3] == 'all_allies' else [unit]):
        t.lc_stacks.pop(key_full, None)
        t.lc_stack_turns.pop(key_full, None)


def _lc_tick_stacks(state, u):
    """叠层回合倒计时（v5.0.1）: Y 轴常规回合结束调用（X 轴不 tick）
    v5.6: 每层独立计时——列表内每层各自 -1, 归零的层消失, 层数同步 len"""
    for key in list(u.lc_stack_turns):
        raw = u.lc_stack_turns[key]
        turns = [raw] if isinstance(raw, int) else raw
        alive = [t - 1 for t in turns if t - 1 > 0]
        if alive:
            u.lc_stack_turns[key] = alive
            u.lc_stacks[key] = len(alive)
        else:
            u.lc_stack_turns.pop(key, None)
            u.lc_stacks.pop(key, None)
    # turn_end 衰减（i_venture_forth_to_hunt 流光: 回合结束掉1层）
    lc = getattr(u, 'lightcone', None)
    if lc:
        for eff in lc.effects:
            code = eff.condition_code or ''
            if code.startswith('stack_gain:') and code.endswith('turn_end:-1'):
                key = code.split(':', 6)[2]
                key_full = f'{lc.id}::{key}'
                cur = u.lc_stacks.get(key_full, 0)
                if cur > 0:
                    nxt = cur - 1
                    if nxt <= 0:
                        u.lc_stacks.pop(key_full, None)
                        u.lc_stack_turns.pop(key_full, None)
                    else:
                        u.lc_stacks[key_full] = nxt
                        turns = u.lc_stack_turns.get(key_full)
                        if turns:
                            turns.pop(0)  # v5.6: 回合结束移除最旧一层


# ---- v5.4 光锥效果建模 helpers（无用效果重审修复）----

def _lc_quid_pro_quo(s, u):
    """等价交换: 回合开始随机为1个能量<50%的队友回16能量"""
    cands = [x for x in s.units if x.is_alive and x is not u
             and x.char.max_energy > 0
             and x.current_energy < x.char.max_energy * 0.5]
    if cands:
        t = random.choice(cands)
        gained = _gain_energy(t, 16.0, state=s)
        s.log.append(f'  光锥[quid_pro_quo] 等价交换: {t.char.name} 回能+{gained:.0f}')


def _lc_grounded_ascent_counter(s, u):
    """回到大地的飞行: 单体辅助技回能、叠圣咏，每2次回1战技点。"""
    if u.char.max_energy <= 0:
        return  # 无常规能量系统角色（如欢愉）不算
    if u.extra.get('lc_last_skill_target_type') != 'single_ally':
        return

    _gain_energy(u, 6.0, state=s)
    target = u.extra.get('lc_last_skill_target')
    if target not in s.units or not target.is_alive:
        target = next((x for x in s.units if x.char.id == 'seele' and x.is_alive), u)
    # v5.6: 圣咏每层独立 TimedBuff（每层持续3回合各自计时, _build_effective_stats 逐buff累加）;
    # 满3层后再获得 → 替换最旧层（实机每层独立 + 满层覆盖最旧）
    hymns = [b for b in target.buffs
             if getattr(b, 'param_id', '') == 'grounded_ascent_hymn']
    if len(hymns) >= 3:
        target.buffs.remove(hymns.pop(0))
    target.buffs.append(TimedBuff(source_id='a_grounded_ascent',
                                  attributes={'DMG_BONUS_ALL': 15.0},
                                  remaining_turns=3,
                                  source_name='光锥·回到大地的飞行',
                                  param_id='grounded_ascent_hymn'))

    u.extra['lc_grounded_count'] = u.extra.get('lc_grounded_count', 0) + 1
    s.log.append(f'  光锥[a_grounded_ascent] 回能6; {target.char.name}圣咏{len(hymns) + 1}/3')
    if u.extra['lc_grounded_count'] >= 2:
        u.extra['lc_grounded_count'] = 0
        _gain_skill_points(s)
        s.log.append('  光锥[a_grounded_ascent] 回到大地的飞行: 回1战技点')


def _lc_energy_cap_dmg_bonus(s, u):
    """今日亦是和平的一日: 增伤 = min(能量上限,160)×0.4%"""
    dmg = min(u.char.max_energy or 0, 160) * 0.4
    u.buffs.append(TimedBuff(source_id='today_is_another_peaceful_day',
                             attributes={'DMG_BONUS_ALL': dmg},
                             remaining_turns=-1,
                             source_name='光锥·今日亦是和平的一日'))
    s.log.append(f'  光锥[today_is_another_peaceful_day] 增伤+{dmg:.1f}% (能量上限{min(u.char.max_energy or 0, 160):.0f}×0.4%)')


def _lc_apply_enemy_def_down(s, u, amount, turns, attr_key, status_id, base_chance=1.0):
    """通用: 攻击使敌方目标防御力降低（EnemyStatus def_reduction 消费链路）
    v5.6: 接入统一 EHR 检定（文本无概率描述的效果=必中 base_chance=1.0）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, status_id, base_chance):
            continue
        t.add_status(EnemyStatus(id=status_id, name=status_id, category='debuff',
                                 source=u.char.id, remaining_turns=turns,
                                 attributes={attr_key: amount}))
        s.log.append(f'  光锥[{getattr(u.lightcone, "id", "?")}] {t.name or t.id} DEF-{amount*100:.0f}% ({turns}回合)')


def _lc_apply_gongxian(s, u):
    """决心如汗珠般闪耀: 击中使目标【攻陷】DEF降低 1回合
    v5.6: 接入统一 EHR 检定; 精炼公式（用户确认）:
    精1~精5 触发基础概率 60%~100%（每精一层+10%）; 减防 12%~16%（每精一层+1%）"""
    rank = getattr(getattr(u, 'lightcone', None), 'rank', 1) or 1
    base_chance = 0.60 + 0.10 * (rank - 1)
    def_down = 0.12 + 0.01 * (rank - 1)
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, '攻陷', base_chance=base_chance):
            s.log.append(f'  光锥[resolution_shines_as_pearls_of_sweat] {t.name or t.id} 攻陷未命中({base_chance:.0%})')
            continue
        t.add_status(EnemyStatus(id='gongxian', name='攻陷', category='debuff',
                                 source=u.char.id, remaining_turns=1,
                                 attributes={'def_reduction': def_down}))
        s.log.append(f'  光锥[resolution_shines_as_pearls_of_sweat] {t.name or t.id} 攻陷: DEF-{def_down*100:.0f}% (1回合)')


def _lc_apply_kubai(s, u):
    """梦应归于何处: 造成击破伤害时使目标【溃败】2回合（击破伤害+24%在 _apply_toughness_damage 消费）
    v5.6: 文本无概率描述→必中, 统一检定入口（base_chance=1.0）"""
    t = s.extra.get('lc_break_enemy')
    if t is None:
        alive = s.alive_enemies() or s.enemies
        t = alive[0] if alive else None
    if t and _roll_effect_hit(u, s, t, '溃败', base_chance=1.0):
        t.add_status(EnemyStatus(id='kubai', name='溃败', category='debuff',
                                 source=u.char.id, remaining_turns=2,
                                 attributes={'break_dmg_bonus': 0.24, 'spd_down': 0.20}))
        s.log.append(f'  光锥[whereabouts_should_dreams_rest] {t.name or t.id} 溃败 (2回合)')


def _lc_apply_wenshun(s, u):
    """烦恼着，幸福着: 施放追加攻击后使目标【温驯】最多2层
    v5.6: 文本无概率描述→必中, 统一检定入口（base_chance=1.0）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, '温驯', base_chance=1.0):
            continue
        existing = next((st for st in t.statuses if st.id == 'wenshun'), None)
        layers = (existing.attributes.get('wenshun_layers', 0) if existing else 0) + 1
        layers = min(layers, 2)
        t.add_status(EnemyStatus(id='wenshun', name='温驯', category='debuff',
                                 source=u.char.id, remaining_turns=1,
                                 attributes={'wenshun_layers': layers}))
        s.log.append(f'  光锥[worrisome_blissful] {t.name or t.id} 温驯{layers}层')


def _lc_record_heal(s, u):
    """时节不居: 记录治疗量（累计, 攻击后按36%附加伤害）"""
    amt = u.extra.get('lc_heal_record', 0.0)
    u.extra['lc_heal_record'] = amt + s.extra.get('lc_last_heal_amt', 0.0)


def _lc_heal_record_extra_damage(s, u):
    """时节不居: 攻击后按记录治疗量36%对随机受击敌附加伤害（每回合最多1次）"""
    if u.extra.get('lc_heal_record_used_this_turn'):
        return
    record = u.extra.get('lc_heal_record', 0.0)
    if record <= 0:
        return
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    u.extra['lc_heal_record_used_this_turn'] = True
    u.extra['lc_heal_record'] = 0.0
    extra = record * 0.36
    t = random.choice(targets)
    _commit_enemy_damage(s, u, t, extra)
    u.total_damage_dealt += extra
    s.log.append(f'  光锥[time_waits_for_no_one] 时节不居: 附加伤害{extra:.0f} (治疗记录{record:.0f}×36%)')


def _lc_sleep_like_dead_miss_crit(s, u):
    """如泥酣眠: 普攻/战技未暴击时CR+36% 1回合（期望模式按未暴击概率1-CR判定, 3回合CD）"""
    if u.extra.get('lc_sleep_cd', 0) > 0:
        return
    skill_key = u.extra.get('lc_last_skill_key', s.extra.get('lc_last_skill_key'))
    if skill_key not in ('basic_attack', 'skill', 'basic_attack_enhanced'):
        return
    stats = _build_effective_stats(u, s)
    if random.random() < (1.0 - min(stats.CRIT_RATE, 1.0)):
        duration = 1 if s.extra.get('action_ctx') == 'extra' else 2
        u.buffs.append(TimedBuff(source_id='sleep_like_the_dead',
                                 attributes={'CRIT_RATE': 36.0},
                                 remaining_turns=duration,
                                 source_name='光锥·如泥酣眠'))
        u.extra['lc_sleep_cd'] = 3
        s.log.append('  光锥[sleep_like_the_dead] 未暴击: CR+36% (1回合)')


def _lc_set_taunt_mult(s, u, mult):
    """受击概率提高（×mult, 常驻）: 落日时起舞×6 / 制胜的瞬间×3"""
    u.extra['taunt_mult'] = mult
    s.log.append(f'  光锥[{getattr(u.lightcone, "id", "?")}] 受击概率×{mult}')


def _lc_same_element_dmg_bonus(s, u, bonus):
    """与行星相会: 我方造成与装备者相同属性的伤害提高%（全队, 永久）"""
    # 战斗层 _apply_stat 的元素增伤键为 DMG_BONUS_{中文元素}
    elem_key = f'DMG_BONUS_{u.char.element}'
    for eu in s.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='planetary_rendezvous',
                                      attributes={elem_key: bonus},
                                      remaining_turns=-1,
                                      source_name='光锥·与行星相会'))
    s.log.append(f'  光锥[planetary_rendezvous] 全队{elem_key}+{bonus}%')


def _lc_extra_flat_damage(s, u, ratio):
    """后会有期: 普攻/战技后对随机受击目标造成ATK×ratio附加伤害（不受加成）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    t = random.choice(targets)
    extra = _build_effective_stats(u, s).ATK * ratio
    _commit_enemy_damage(s, u, t, extra)
    u.total_damage_dealt += extra
    s.log.append(f'  光锥[we_will_meet_again] 后会有期: 附加伤害{extra:.0f} (48%ATK)')


def _lc_flames_afar(s, u, threshold, bonus):
    """在火的远处: 单次受击/自耗≥25%生命上限→回15%HP+增伤bonus% 2回合（3回合CD; v5.7 bonus按叠影档）"""
    cd = u.extra.get('flames_afar_cd', 0)
    if cd > 0:
        return
    lost = s.extra.get('lc_last_hp_loss', 0.0)
    if lost < u.max_hp * threshold:
        return
    u.current_hp = min(u.max_hp, u.current_hp + u.max_hp * 0.15)
    u.buffs.append(TimedBuff(source_id='flames_afar', attributes={'DMG_BONUS_ALL': bonus},
                             remaining_turns=2, source_name='光锥·在火的远处'))
    u.extra['flames_afar_cd'] = 3
    s.log.append(f'  光锥[flames_afar] 在火的远处: 回15%HP, 增伤+{bonus}% (2回合)')


def _lc_childlike_mark(s, u, skill_type):
    """美梦小镇大冒险: 最新技能类型【童心】→ 全队该类型技能增伤（v5.7: 按叠影档）"""
    u.extra['childlike_type'] = skill_type
    stat_type = {
        'basic_attack': 'DMG_BONUS_BASIC',
        'skill': 'DMG_BONUS_SKILL',
        'ultimate': 'DMG_BONUS_ULTIMATE',
    }[skill_type]
    bonus = _lc_rank_value(u, 12.0)
    # 移除旧童心buff, 挂新（全队, 永久直至下次技能）
    for eu in s.units:
        if not eu.is_alive:
            continue
        eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'childlike']
        eu.buffs.append(TimedBuff(source_id='dreamville_adventure',
                                  attributes={stat_type: bonus},
                                  remaining_turns=-1,
                                  source_name='光锥·美梦小镇大冒险',
                                  param_id='childlike'))
    s.log.append(f'  光锥[dreamville_adventure] 童心: 全队{skill_type}增伤+12%')


def _lc_swordplay_stack(s, u):
    """论剑: 多次击中同一目标叠层16%/层（5层, 换目标重置, S1）"""
    tid = s.extra.get('lc_attack_first_target_id', '')
    if not tid:
        return
    if u.extra.get('swordplay_tid') == tid:
        u.extra['swordplay_layers'] = min(u.extra.get('swordplay_layers', 0) + 1, 5)
    else:
        u.extra['swordplay_tid'] = tid
        u.extra['swordplay_layers'] = 1
    s.log.append(f'  光锥[swordplay] 论剑: 叠层{u.extra["swordplay_layers"]}/5')


def _lc_masquerade_caiyan(s, u):
    """游戏尘寰: 每恢复1战技点(含溢出)→1层【彩焰】, 4层→移除彩焰获【假面】4回合。
    假面的队友增益由既有 stack:jiamian 机制消费（lc_stacks 叠层, 队友+CR10/CD28 排除佩戴者）。"""
    layers = u.extra.get('masquerade_caiyan', 0) + 1
    if layers >= 4:
        u.extra['masquerade_caiyan'] = 0
        # 刷新全队 jiamian 叠层（4回合）→ 队友增益经 _apply_team_lc_stack_buffs 生效
        for eu in s.units:
            if not eu.is_alive:
                continue
            eu.lc_stacks['earthly_escapade::jiamian'] = 1
            eu.lc_stack_turns['earthly_escapade::jiamian'] = [4]  # v5.6: 分层容器（假面为状态刷新语义, 单层）
        s.log.append('  光锥[earthly_escapade] 【彩焰】满4层 → 【假面】4回合')
    else:
        u.extra['masquerade_caiyan'] = layers
        s.log.append(f'  光锥[earthly_escapade] 【彩焰】{layers}/4')


def _lc_refresh_love_blank(s):
    """Apply the strongest active permanent Blank mark to the current enemy wave."""
    blank = max((0.16 if owner.extra.get('love_poem') else 0.10
                 for owner in s.units
                 if owner.is_alive and owner.extra.get('love_blank')), default=0.0)
    for enemy in s.enemies:
        if blank:
            enemy.extra['love_blank_vuln'] = blank
        else:
            enemy.extra.pop('love_blank_vuln', None)


def _lc_love_forever(s, u):
    """爱如此刻永恒: 忆灵技后【空白】(敌方易伤10%)/【诗行】(全队CD+16%), 当局永久; 双持×1.6"""
    tgt_type = s.extra.get('lc_last_memsprite_target', '')
    if tgt_type in ('single_ally', 'ally', 'all_allies'):
        u.extra['love_blank'] = True  # 空白: 敌方全体受伤+10%
    else:
        u.extra['love_poem'] = True  # 诗行: 我方全体CD+16%
    both = u.extra.get('love_blank') and u.extra.get('love_poem')
    poem = 0.16 * (1.6 if both else 1.0)
    # 空白 → 敌方 vulnerability（全队受益）; 诗行 → 全队CD buff
    _lc_refresh_love_blank(s)
    if u.extra.get('love_poem'):
        for eu in s.units:
            if not eu.is_alive:
                continue
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'love_poem']
            eu.buffs.append(TimedBuff(source_id='this_love_forever',
                                      attributes={'CRIT_DMG': poem * 100.0},
                                      remaining_turns=-1,
                                      source_name='光锥·爱如此刻永恒【诗行】',
                                      param_id='love_poem'))
    s.log.append(f'  光锥[this_love_forever] 空白:{u.extra.get("love_blank")} 诗行:{u.extra.get("love_poem")}')


def _process_lc_effects(unit, state, event_type):
    """根据事件类型触发匹配的光锥特效（v5.0.1: 含叠层/标记分发）"""
    lc = getattr(unit, 'lightcone', None)
    if not lc or lc.path != unit.char.path:
        return  # getattr 守卫: 忆灵可被 _apply_hit 命中
    if event_type == 'on_self_turn_start':
        for key in ('lc_sleep_cd', 'flames_afar_cd'):
            if unit.extra.get(key, 0) > 0:
                unit.extra[key] -= 1
        unit.extra['lc_heal_record_used_this_turn'] = False
    for eff in lc.effects:
        pid = eff.param_id
        if pid in LC_TRIGGERS:
            evt, handler = LC_TRIGGERS[pid]
            if evt == event_type:
                handler(state, unit, None)
                return
    # v5.0.1: 叠层/标记触发分发
    for eff in lc.effects:
        code = eff.condition_code or ''
        if code.startswith('stack_gain:'):
            _lc_maybe_gain_stack(state, unit, eff, event_type,
                                 target_count=state.extra.get('lc_attack_targets', 0))
        elif code.startswith('stack_remove:'):
            _lc_maybe_remove_stack(state, unit, eff, event_type)
    # v5.0 P3: 通用事件缓冲器（condition_code 匹配, 挂 TimedBuff 恢复被抵消属性）
    _lc_apply_event_effect(state, unit, event_type)


# ---- 技能执行 ----

def _bounce_hits(u, mult, state=None) -> int:
    """弹射段数（v5.3 开拓者·同谐E6: 战技额外伤害次数+2, 1+4→1+6）
    v6.7: 绯英行迹3·瞰众乐 终结技弹射+1/2/4（敌数≥3/2/1）; 火花E6 欢愉技每笑点+1次(上限40)"""
    hits = mult.hits
    if u.char.id == 'trailblazer_harmony' and u.eidolon_rank >= 6:
        hits += 2
    if u.char.id == 'evanescia' and state is not None:
        alive = state.alive_enemies()
        n = len(alive)
        if n >= 3:
            hits += 1
        elif n == 2:
            hits += 2
        elif n == 1:
            hits += 4
    if u.char.id == 'sparxie' and u.eidolon_rank >= 6 and state is not None:
        hits += min(int(state.laugh_points), 40)  # E6: 每1笑点额外伤害次数+1
    return hits


def _toughness_efficiency(u, state, skill_key, stats=None) -> float:
    """削韧效率（面板 × 各角色弱点击破效率源, v5.3 模块化注册）"""
    eff = (stats or _build_effective_stats(u, state)).TOUGHNESS_EFFICIENCY
    if u.char.id == 'fugue' and u.eidolon_rank >= 6:
        eff *= 1.5  # 忘归人E6: 自身弱点击破效率+50%
    if u.char.id == 'lingsha' and u.eidolon_rank >= 1:
        eff *= 1.5  # 灵砂E1: 自身弱点击破效率+50%
    if u.char.id == 'firefly' and u.extra.get('combustion') \
            and skill_key in ('basic_attack_enhanced', 'skill_enhanced'):
        eff *= 1.5  # 完全燃烧: 强化攻击弱点击破效率+50%
        if u.eidolon_rank >= 6:
            eff += 0.5  # 流萤E6: 击破效率+50%（v5.7 用户确认实机加算: 1.5+0.5=2.0）
    if u.extra.get('_foxian'):
        fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
        if fugue and fugue.eidolon_rank >= 1:
            eff *= 1.5  # 忘归人E1: 狐祈者弱点击破效率+50%
    return eff


def _no_weakness_pen(u, skill_key) -> bool:
    """全额无视弱点削韧: 遐蝶E6 / 忘归人终结技"""
    if u.char.id == 'xiadie' and u.eidolon_rank >= 6:
        return True
    if u.char.id == 'fugue' and skill_key == 'ultimate':
        return True
    if u.char.id == 'acheron' and (
            skill_key == 'ultimate'
            or (u.eidolon_rank >= 6 and skill_key in ('basic_attack', 'skill'))):
        return True
    if u.char.id == 'feixiao' and skill_key == 'ultimate':
        return True
    return False


def _super_break_rate(state, u, stats=None) -> float:
    """v5.3 超击破转化率（0=不触发）。源注册表:
    忘归人天赋(全队光环) / 同谐【伴舞】(持有者) / 流萤行迹2(燃烧+BE阈值)。
    多源线性求和（实机多源各触发一次实例）。"""
    rate = 0.0
    fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
    if fugue is not None:
        rate += 1.0  # 忘归人天赋: 我方攻击击破状态敌人→削韧转化为超击破（满级100%）
    if any(getattr(b, 'attributes', {}).get('_tbh_super_break') for b in u.buffs):
        rate += 1.0  # 同谐【伴舞】
    if u.char.id == 'firefly' and u.extra.get('combustion'):
        break_effect = (stats or _build_effective_stats(u, state)).BREAK_EFFECT
        if break_effect >= 3.0:
            rate += 1.5  # 流萤行迹2: BE≥300% → 150%转化
        elif break_effect >= 1.5:
            rate += 1.0  # 流萤行迹2: BE≥150% → 100%转化
    return rate


def _apply_toughness_damage(state, u, t, base_toughness, break_element, skill_key, stats) -> float:
    """v5.3 抽取: 单目标削韧结算（行为与原内联块一致+新增）。
    主韧性削韧→击破效果（伤害/延后/异常/hooks）; 已击破目标→超击破（源收窄）;
    云火昭（忘归人天赋）独立削减, 削至0二次击破。返回本次削韧造成的伤害。"""
    efficiency = _toughness_efficiency(u, state, skill_key, stats)
    toughness_dmg = base_toughness * efficiency
    no_weakness_pen = _no_weakness_pen(u, skill_key)
    can_tough = (t.element_res.get(break_element, 0.2) <= 0 or no_weakness_pen)
    # 狐祈者: 无对应弱点目标也可削减（50%效率）——非全额无视, 与既有无视弱点不叠加
    if u.extra.get('_foxian') and not no_weakness_pen \
            and t.element_res.get(break_element, 0.2) > 0:
        toughness_dmg *= 0.5
        can_tough = True
    # v5.0 P7: 铁骑4pc — 击破特攻≥150% → 击破/超击破无视20%防御（本地 DEF_PEN）
    sb_stats = stats
    if ('be_threshold_defpen' in getattr(u, '_active_relic_conditions', set())
            and stats.BREAK_EFFECT >= 1.5):
        sb_stats = copy.deepcopy(stats)
        sb_stats.DEF_PEN += 0.20
    # 流萤E1: 强化战技无视15%防御
    if u.char.id == 'firefly' and u.eidolon_rank >= 1 and skill_key == 'skill_enhanced':
        if sb_stats is stats:
            sb_stats = copy.deepcopy(stats)
        sb_stats.DEF_PEN += 0.15
    # 忘归人E4: 狐祈者造成的击破/超击破伤害+20%
    break_mult = 1.0
    if u.extra.get('_foxian'):
        fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
        if fugue and fugue.eidolon_rank >= 4:
            break_mult = 1.20
    # v5.3 流萤终结技: 强化攻击使敌方目标受到萨姆造成的击破伤害+20%（满级, 持续至本次攻击结束）
    if u.char.id == 'firefly' and u.extra.get('combustion') \
            and skill_key in ('basic_attack_enhanced', 'skill_enhanced'):
        break_mult *= 1.20
    # v5.4 梦应归于何处: 目标【溃败】状态下装备者对其击破伤害+24%
    if t.has_status(status_id='kubai') and getattr(u.lightcone, 'id', '') == 'whereabouts_should_dreams_rest':
        break_mult *= 1.24
    # 同谐行迹1: 伴舞触发的超击破伤害按敌人数+20%~60%
    tbh_mult = 1.0
    if any(getattr(b, 'attributes', {}).get('_tbh_super_break') for b in u.buffs):
        n = min(len(state.alive_enemies()), 5)
        tbh_mult = {5: 1.2, 4: 1.3, 3: 1.4, 2: 1.5, 1: 1.6}[n]

    added = 0.0
    # 主韧性削韧（仅弱点属性、无视弱点或狐祈者减半无视）
    if t.toughness > 0 and t.max_toughness > 0 and can_tough:
        t.toughness = max(0, t.toughness - toughness_dmg)
        # v6.7 大丽花结界: 未破韧目标承受的削韧值也能转化为超击破（削韧与转化同时发生）
        if _dahlia_field_active(state) and not t.is_broken:
            rate = _super_break_rate(state, u, stats) + _dahlia_super_break_rate(state, u, t)
            if rate > 0:
                sbd = calculate_damage(sb_stats, t, 0, 0, "super_break",
                                       break_element, 80, False,
                                       toughness_dmg=toughness_dmg)
                sbd.final_damage *= rate * break_mult * tbh_mult
                _commit_enemy_damage(state, u, t, sbd.final_damage)
                u.total_damage_dealt += sbd.final_damage
                added += sbd.final_damage
                state.log.append(f'  结界超击破: {t.name or t.id} {sbd.final_damage:.0f}(削韧{toughness_dmg:.0f})')
        if t.toughness <= 0 and not t.is_broken:
            t.is_broken = True
            # 击破瞬间结算击破伤害
            bd = calculate_damage(sb_stats, t, 0, 0, "break", break_element, 80, False)
            bd.final_damage *= break_mult
            _commit_enemy_damage(state, u, t, bd.final_damage)
            u.total_damage_dealt += bd.final_damage
            added += bd.final_damage
            state.log.append(f'  击破弱点! {t.name or t.id} 击破={bd.final_damage:.0f}({break_element})')
            # 强制行动延后25%
            t.extra['av_delayed'] = 2500.0
            # 属性异常DOT
            _apply_break_debuff(t, break_element, u, state)
            state.extra['lc_break_enemy'] = t  # v5.4 光锥击破事件目标（梦应归于何处溃败）
            state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
            _process_lc_effects(u, state, "on_weakness_break")  # v5.0.1
            state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                                    enemy=t, skill_key=skill_key)  # v5.3
    elif (t.is_broken or _dahlia_field_active(state)) and can_tough:
        # v5.3 超击破: 需超击破源（转化率>0）, 伤害=削韧值×(1+击破特攻)×转化率×修饰
        # v6.7 大丽花结界: 未破韧目标削韧也能转化（用户 2026-08-15 确认转化率与天赋同率60%）
        rate = _super_break_rate(state, u, stats) + _dahlia_super_break_rate(state, u, t)
        if rate > 0:
            sbd = calculate_damage(sb_stats, t, 0, 0, "super_break",
                                   break_element, 80, False,
                                   toughness_dmg=toughness_dmg)
            sbd.final_damage *= rate * break_mult * tbh_mult
            _commit_enemy_damage(state, u, t, sbd.final_damage)
            u.total_damage_dealt += sbd.final_damage
            added += sbd.final_damage
            state.log.append(f'  超击破: {t.name or t.id} {sbd.final_damage:.0f}(削韧{toughness_dmg:.0f})')

    # 云火昭（忘归人天赋）: 独立于主韧性, 击破后仍可削减, 削至0二次击破（元素=攻击者元素）
    if can_tough and t.is_broken and t.extra_toughness_max > 0 and t.extra_toughness > 0:
        t.extra_toughness = max(0, t.extra_toughness - toughness_dmg)
        if t.extra_toughness <= 0:
            cf = calculate_damage(sb_stats, t, 0, 0, "break", break_element, 80, False)
            cf.final_damage *= break_mult
            _commit_enemy_damage(state, u, t, cf.final_damage)
            u.total_damage_dealt += cf.final_damage
            added += cf.final_damage
            t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 2500.0
            _apply_break_debuff(t, break_element, u, state)
            state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
            _process_lc_effects(u, state, "on_weakness_break")
            state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                                    enemy=t, skill_key=skill_key)
            state.log.append(f'  云火昭击破! {t.name or t.id} 二次击破={cf.final_damage:.0f}({break_element})')
    return added


def _deduct_skill_point_cost(state, u, sp_cost) -> bool:
    """v6.7 抽取: 战技点扣费统一入口。火花【爆点】优先抵扣（消耗爆点视为消耗战技点,
    爆点抵扣部分不计入 lc_sp_spent 光锥SP消耗计数）。返回是否扣费成功。"""
    state.extra['_last_sp_spent'] = 0
    if sp_cost <= 0:
        return True
    spent_points = 0
    burst = state.extra.get('sparxie_burst_points', 0.0)
    if burst > 0:
        used = min(float(sp_cost), burst)
        state.extra['sparxie_burst_points'] = burst - used
        sp_cost -= int(used)
        spent_points += int(used)
        # 火花E2: 每消耗1爆点自身暴伤+10% 2回合（最多叠4层; v6.7b: 总层数封顶,
        # 此前每次消耗 append 独立 buff 可突破4层上限）
        spx = next((x for x in state.units
                    if x.char.id == 'sparxie' and x.is_alive and x.eidolon_rank >= 2), None)
        if spx and used > 0:
            from engine.core.combat_sim import TimedBuff
            existing = next((b for b in spx.buffs
                             if getattr(b, 'param_id', '') == 'sparxie_e2_cd'), None)
            cur_stacks = int(round(existing.attributes.get('CRIT_DMG', 0.0) / 10.0))                 if existing is not None else 0
            new_stacks = min(4, cur_stacks + int(used))
            if existing is not None and new_stacks > 0:
                existing.attributes['CRIT_DMG'] = 10.0 * new_stacks
                existing.remaining_turns = 2  # 再消耗刷新持续时间
            elif new_stacks > 0:
                spx.buffs.append(TimedBuff(source_id='sparxie',
                                           attributes={'CRIT_DMG': 10.0 * new_stacks},
                                           remaining_turns=2, param_id='sparxie_e2_cd',
                                           source_name='火花E2爆点暴伤'))
        if sp_cost <= 0:
            state.extra['_last_sp_spent'] = spent_points
            state.log.append(f'  {u.char.name}: 爆点抵扣{used:.0f}战技点')
            silver = next((ally for ally in state.units
                           if ally.char.id == 'yinlang' and ally.is_alive
                           and ally.invincible_active), None)
            elation = state.extra.get('_elation')
            if silver and elation:
                for _ in range(spent_points):
                    elation.silver_blindbox(silver, state)
            return True
    if state.skill_points < sp_cost:
        state.log.append(f'  [WARN] {u.char.name} 战技点不足({state.skill_points}<{sp_cost}), 无法使用技能')
        return False
    state.skill_points -= sp_cost
    spent_points += int(sp_cost)
    state.extra['_last_sp_spent'] = spent_points
    # v6.10.6 C2: 花火幻相——每消耗1战技点叠1层(上限3), 每层全敌受伤+4%(2回合刷新);
    # E2 每层额外降防10%; 行迹2 单回合耗≥3→下次战技免SP; 行迹1 持CD buff者耗SP→花火回1能量
    sparkle = next((x for x in state.units
                    if x.char.id == 'sparkle' and x.is_alive), None)
    if sparkle is not None and sp_cost > 0:
        stacks = min(3, sparkle.extra.get('sparkle_huanxiang', 0) + sp_cost)
        sparkle.extra['sparkle_huanxiang'] = stacks
        for e in state.enemies:
            if getattr(e, 'HP', 0) <= 0:
                continue
            e.add_status(EnemyStatus(id='sparkle_huanxiang_vuln', name='受伤提高',
                                     category='debuff', source='sparkle',
                                     remaining_turns=2,
                                     attributes={'vulnerability': stacks * 0.04}))
            if sparkle.eidolon_rank >= 2:
                e.add_status(EnemyStatus(id='sparkle_huanxiang_def', name='防御降低',
                                         category='debuff', source='sparkle',
                                         remaining_turns=2,
                                         attributes={'def_reduction': stacks * 0.10}))
        state.log.append(f'  幻相: {stacks}层 全敌受伤+{stacks * 4}%'
                         + (' 降防+10%/层' if sparkle.eidolon_rank >= 2 else ''))
        turn_spent = state.extra.get('sparkle_turn_sp_spent', 0) + sp_cost
        state.extra['sparkle_turn_sp_spent'] = turn_spent
        if turn_spent >= 3:
            state.extra['sparkle_free_skill'] = True
        if any(getattr(t, 'hook_name', '') == 'sparkle_basic_energy'
               for t in (sparkle.char.traces or []))                 and any(getattr(b, 'param_id', '') == 'sparkle_cd_buff' for b in u.buffs):
            _gain_energy(sparkle, 1.0, state=state)
            state.log.append('  花火行迹1: 持CD buff者耗SP→花火+1能量')
    # v5.0.1: SP 消耗计数（花花世界迷人眼: 每消耗1SP无视5%防御）
    state.extra['lc_sp_spent'] = state.extra.get('lc_sp_spent', 0) + sp_cost
    # 银狼结界监听结界内任意我方目标的实际战技点消耗。
    silver = next((ally for ally in state.units
                   if ally.char.id == 'yinlang' and ally.is_alive
                   and ally.invincible_active), None)
    if silver:
        elation = state.extra.get('_elation')
        if elation:
            for _ in range(spent_points):
                elation.silver_blindbox(silver, state)
    return True


def _skill_level_factor(u, skill_key):
    """E3/E5 数据层当前采用的统一成长因子（每额外等级 +5%）。"""
    levels = (u.extra.get('skill_level_boost', {}) or {}).get(skill_key, 0)
    return 1.0 + 0.05 * max(int(levels), 0)


def _use_skill(u: SimUnit, state: SimState, skill_key: str,
               laugh_n_override: float = None):
    # v5.3 忘归人: 炽灼状态普攻强化为冉冉方炽
    if u.char.id == 'fugue' and skill_key == 'basic_attack' and \
            any(getattr(b, 'attributes', {}).get('_chizhuo') for b in u.buffs):
        skill_key = 'basic_attack_enhanced'
    # v6.7 火花: 直播连线（一次性, 用户 2026-08-15 确认仅下次普攻强化）
    if u.char.id == 'sparxie' and skill_key == 'basic_attack' and \
            u.extra.get('sparxie_live'):
        skill_key = 'basic_attack_enhanced'
        u.extra.pop('sparxie_live', None)
        state.log.append('  直播连线: 普攻强化为【百花齐放，胜者独享！】')
    # v6.9 千冶·刃: 无量忿怒期普攻强化为淬锋断魄
    if u.char.id == 'qianye' and skill_key == 'basic_attack' \
            and u.extra.get('qianye_wrath'):
        skill_key = 'basic_attack_enhanced'
        state.log.append('  无量忿怒: 普攻强化为【淬锋，断魄】')
    # v6.10 黄泉天赋: 每次施放技能最多触发1次——技能开始清施放者标记
    u.extra.pop('acheron_talent_triggered', None)
    # Some character-specific handlers add debuffs directly instead of going
    # through _apply_skill_effects. Keep a structured snapshot so those paths
    # still participate in the shared on_debuff_applied contract.
    debuffs_before = {
        id(enemy): copy.deepcopy([
            status for status in enemy.statuses
            if status.category in ('debuff', 'dot', 'control')
        ])
        for enemy in state.enemies
    }
    skill = u.char.skills.get(skill_key)
    qianye_new_ult = u.char.id == 'qianye' and skill_key == 'skill_enhanced'
    # 千冶·刃技能门控走统一入口。新终结技继续执行下方完整技能、
    # 伤害、击杀、光锥和 Hook 管线，不再提前返回到手写结算。
    if u.char.id == 'qianye':
        if skill_key == 'skill' and not _qianye_wrath_active(u):
            state.log.append('  [WARN] 千冶·刃: 未处于无量忿怒, 战技不可用')
            return
        if qianye_new_ult and (not _qianye_wrath_active(u)
                               or u.current_energy < (u.char.max_energy or 0)):
            state.log.append('  [WARN] 千冶·刃: 新终结技需要无量忿怒与满能量')
            return

    if not skill:
        return
    if u.char.id == 'yinlang' and u.invincible_active \
            and skill_key in ('skill', 'ultimate'):
        state.log.append('  [WARN] 银狼无敌玩家期间无法施放战技或终结技')
        return
    if qianye_new_ult and u.eidolon_rank >= 6:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 1.50
    if u.char.id == 'cerydra' and skill_key == 'ultimate' and u.eidolon_rank >= 4 \
            and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale += 240.0
    if u.char.id == 'anaxa' and skill_key == 'skill' and u.eidolon_rank >= 4:
        stacks = min(2, u.extra.get('anaxa_e4_stacks', 0) + 1)
        u.extra['anaxa_e4_stacks'] = stacks
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'anaxa_e4_atk']
        u.buffs.append(TimedBuff(source_id='anaxa', attributes={'ATK_PERCENT': 30.0 * stacks},
                                 remaining_turns=2, param_id='anaxa_e4_atk',
                                 source_name='那刻夏E4'))
    # v6.10.3 P1-6: 技能等级覆盖层（E3/E5, 每级+5%）
    skill_level_factor = _skill_level_factor(u, skill_key)
    if skill_level_factor > 1.0:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= skill_level_factor
        for eff in (skill.effects or []):
            if getattr(eff, 'type', '') == 'shield':
                eff.value = getattr(eff, 'value', 0.0) * skill_level_factor
    # v6.10.3 P1-3: 爻光E4（阿哈额外回合全体欢愉技×1.5）/ E6（自身欢愉技倍率×2）
    if skill_key == 'elation_skill' and state.extra.get('yao_e4_aha') and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 1.5
    if u.char.id == 'yaoguang' and skill_key == 'elation_skill' \
            and u.eidolon_rank >= 6 and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 2.0
    is_ultimate_action = skill_key == 'ultimate' or qianye_new_ult

    # v6.8.2: 每次技能动作开始清空上次命中的目标缓存——
    # last_multihit_targets 只在弹射路径写入, 不清会污染下一动作的
    # 缇宝结界/海瑟音天赋/那刻夏天赋等“受击目标”判定。
    state.extra.pop('last_attack_targets', None)
    state.extra.pop('last_multihit_targets', None)
    state.extra.pop('last_hit_segments', None)  # 逐段命中（含重复段, 那刻夏逐段计数用）
    state.extra.pop('cipher_action_main_target', None)
    state.extra.pop('cipher_action_targets', None)
    if u.char.id == 'cipher' and skill_key in ('skill', 'ultimate'):
        cipher_alive = state.alive_enemies() or state.enemies
        if cipher_alive:
            main_target = cipher_alive[0]
            _cipher_set_laozhuke(state, u, main_target)
            state.extra['cipher_action_main_target'] = main_target
            state.extra['cipher_action_targets'] = cipher_alive[:min(3, len(cipher_alive))]


    # v5.7 开拓者·记忆E4: 能量上限为0的我方目标主动施放技能→迷迷+3%充能
    if (u.char.max_energy or 0) == 0:
        tbr4 = next((x for x in state.units
                     if x.char.id == 'trailblazer_remembrance' and x.eidolon_rank >= 4
                     and x.memsprite_unit and x.memsprite_unit.is_alive), None)
        if tbr4:
            rem = state.extra.get('_rem_sys')
            ms = tbr4.memsprite_unit
            if rem:
                ch = rem._mimi_charge_gain(state, ms, 3)
                state.log.append(f'  开拓者·记忆E4: 零能量单位施技→迷迷充能+3% → {ch:.0f}%')

    # Hook: 技能使用前
    if state.hooks.trigger(u.char.id, "on_before_skill",
                            u=u, state=state, skill_key=skill_key, skill=skill):
        return
    # 光锥攻击后事件在伤害循环内派发，必须提前记录本次技能而不是读取上次技能。
    u.extra['lc_last_skill_key'] = skill_key
    u.extra['lc_last_skill_target_type'] = skill.target
    has_single_ally_target = skill.target == 'single_ally' or any(
        getattr(effect, 'target', '') == 'single_ally' for effect in skill.effects)
    if has_single_ally_target:
        u.extra['lc_last_skill_target'] = _pick_single_ally_target(state, u)
    else:
        u.extra.pop('lc_last_skill_target', None)
    state.extra['lc_last_skill_key'] = skill_key

    # SP 与能量（通用）
    sp_cost = skill.cost.get("skill_points", 0)
    # v6.10.6 C2: 花火行迹2·人造花——单回合耗SP≥3后, 下次战技免SP
    if u.char.id == 'sparkle' and skill_key == 'skill' and state.extra.get('sparkle_free_skill'):
        state.extra.pop('sparkle_free_skill', None)
        sp_cost = 0
        state.log.append('  花火行迹2: 本次战技免SP')
    # v5.3 流萤E1: 强化战技不消耗战技点
    if u.char.id == 'firefly' and u.eidolon_rank >= 1 and skill_key == 'skill_enhanced':
        sp_cost = 0
    if not _deduct_skill_point_cost(state, u, sp_cost):
        return
    spent_skill_points = int(state.extra.pop('_last_sp_spent', 0))
    if skill_key in ("basic_attack", "basic_attack_enhanced"):
        # v5.3: 强化普攻也恢复1战技点（实机普攻类统一恢复; 忘归人冉冉方炽等）
        # v5.7: 数据驱动例外——阿格莱雅孤锋千吻/昔涟向着爱与明天"无法恢复战技点"(_sp_recover: 0)
        if skill.cost.get("_sp_recover", 1):
            _gain_skill_points(state)
    # 终结技消耗全部能量；其他技能回复能量
    if is_ultimate_action:
        # v6.11.1 晴歌E6: Fever期终结技扣140保留溢出(储存2次语义)
        if u.char.id == 'robin_summeretto' and u.eidolon_rank >= 6 \
                and u.extra.get('qingge_fever'):
            u.current_energy = max(0.0, u.current_energy
                                   - (skill.cost.get('energy') or u.char.max_energy or 0))
        else:
            u.current_energy = 0
        # v6.10.6 D: 通用终结技后回能——消费 JSON effects 的 energy_regen（26角色声明的"终结技后恢复5能量"此前被静默忽略）
        for eff in (skill.effects or []):
            if getattr(eff, 'type', '') == 'energy_regen':
                amt = getattr(eff, 'value', 0.0) or 0.0
                if amt > 0:
                    _gain_energy(u, float(amt), state=state)
        # v6.10.6 B2: 藿藿禳命——我方施放终结技时触发回血
        _huohuo_ruming_heal_all(state, u)
        # 万敌终结技: 积攒20点天赋充能
        if u.char.id == 'mydei' and not u.extra.get('charge_locked'):
            u.extra['mydei_charge'] = min(200, u.extra.get('mydei_charge', 0) + 20)
            state.log.append(f'  诛天焚骨的王座: 充能+20 → {u.extra["mydei_charge"]:.0f}/200')
            # v5.7: 目标与相邻目标嘲讽2回合; 记录下次弑神登神优先目标(仅最新目标生效)
            _alive = state.alive_enemies() or state.enemies
            tgt = _select_targets(_alive, 'blast')
            if tgt:
                _apply_enemy_taunt(state, u, tgt, turns=2)
                u.extra['mydei_priority_target_id'] = tgt[0].id
        # 开拓者·记忆终结技: 未完的尾声+1史诗(最多2层)；迷迷+40%充能
        if u.char.id == 'trailblazer_remembrance':
            u.extra['tbr_epic'] = min(2, u.extra.get('tbr_epic', 0) + 1)
            state.log.append(f'  未完的尾声: 史诗+1 → {u.extra["tbr_epic"]}/2')
            if u.memsprite_unit and u.memsprite_unit.is_alive:
                ms = u.memsprite_unit
                rem = state.extra.get('_rem_sys')
                ch = rem._mimi_charge_gain(state, ms, 40) if rem else ms.extra.get('charge', 0) + 40
                state.log.append(f'  终结技: 迷迷充能+40% → {ch:.0f}%')
        # v6.7 火花终结技: 倍率=(0.6×欢愉度+50%)ATK + 2笑点 + 行迹1 + E4
        if u.char.id == 'sparxie':
            # v6.7b: E4 先结算再取面板——"施放终结技时"欢愉度+36%应计入本次倍率
            if u.eidolon_rank >= 4:
                state.laugh_points += 5
                u.buffs.append(TimedBuff(source_id='sparxie', attributes={'ELATION_LEVEL': 36.0},
                                         remaining_turns=3, param_id='sparxie_e4_elation',
                                         source_name='火花E4·表情管理'))
                state.log.append('  火花E4: +5笑点 + 欢愉度+36%(3回合)')
            spx_stats = _build_effective_stats(u, state)
            skill = copy.deepcopy(skill)
            main = next((m for m in skill.multipliers
                         if m.target in ('all_enemies', None, '')), skill.multipliers[0])
            bonus = spx_stats.ELATION_LEVEL * 60.0  # 0.6×欢愉度(面板小数)×100
            main.scale = main.scale + bonus
            state.log.append(f'  火花终结技: 欢愉度{spx_stats.ELATION_LEVEL*100:.0f}%→倍率+{bonus:.1f}%')
            state.laugh_points += 2
            n_elation = sum(1 for x in state.units if x.char.path == "欢愉")
            extra_laugh, extra_burst = {1: (2, 1), 2: (4, 1), 3: (8, 4)}.get(n_elation, (0, 0))
            state.laugh_points += extra_laugh
            state.extra['sparxie_burst_points'] = \
                state.extra.get('sparxie_burst_points', 0.0) + extra_burst
            state.log.append(f'  火花终结技: +2笑点, 行迹1(欢愉{n_elation})额外+{extra_laugh}笑点+{extra_burst}爆点')
        # v6.7 绯英E6: 首终结技回120能量（每再施放4次触发1次）
        if u.char.id == 'evanescia' and u.eidolon_rank >= 6:
            cnt = u.extra.get('evanescia_ult_count', 0) + 1
            u.extra['evanescia_ult_count'] = cnt
            if cnt % 4 == 1:
                _gain_energy(u, 120.0, state=state)
                state.log.append(f'  绯英E6: 首终结技回120能量(第{cnt}次, 每4次触发)')
        # v6.7 姬子·启行终结技: 双模式内联（光束/脉冲/最后一击）
        if u.char.id == 'himeko_nova':
            _hn_ultimate(state, u)
            _ult_post(state, u)
            return  # 伤害已内联结算, 跳过后续通用伤害循环
        # v6.11.1 晴歌终结技: 拉条+回能+特邀嘉宾内联（无伤害, 跳过通用循环）
        if u.char.id == 'robin_summeretto':
            _qingge_ultimate(state, u)
            _ult_post(state, u)
            _process_lc_effects(u, state, "on_ult")  # 补通用路径的光锥终结技事件
            _hn_count_ally_ult(state, u)  # v7.2.0 #6: 提前return前补裁决协议计数
            return
        # v6.7 大丽花终结技: 300%ATK由敌方全体均分（白厄最后一击先例）
        if u.char.id == 'the_dahlia':
            alive_n = len(state.alive_enemies())
            if alive_n > 0:
                skill = copy.deepcopy(skill)
                for m in skill.multipliers:
                    if m.stat == 'ATK':
                        m.scale = m.scale / alive_n
        # v6.7 同行协议·裁决: 队友主动终结技计数
        _hn_count_ally_ult(state, u)
    else:
        # v5.3 流萤战技: 固定恢复60%能量上限（取代标准战技回能, 下方特殊分支处理）
        gain = 0 if (u.char.id == 'firefly' and skill_key == 'skill') else ENERGY_GAIN.get(skill_key, 0)
        # v6.7 火花: 战技无能量恢复（txt 尖叫！火花花连线中无回能行）;
        # 强化普攻【百花齐放】能量恢复40（txt）
        if u.char.id == 'sparxie' and skill_key == 'skill':
            gain = 0
        elif u.char.id == 'sparxie' and skill_key == 'basic_attack_enhanced':
            gain = 40.0
        elif u.char.id == 'evanescia' and skill_key == 'elation_skill':
            gain = 5.0  # txt 欢愉技: 能量恢复5（v6.7b 补）
        _gain_energy(u, gain, state=state)  # v5.7: 迷迷充能 bank 已统一迁入 _gain_energy

    # 特殊能量消耗（新蕊/追忆/万敌充能）
    zhuiyi_cost = skill.cost.get("_zhuiyi", 0)
    if zhuiyi_cost > 0 and u.char.id == 'xilian':
        if u.zhuiyi < zhuiyi_cost:
            state.log.append(f'  [WARN] 追忆不足({u.zhuiyi:.0f}<{zhuiyi_cost})')
            return
        u.zhuiyi -= zhuiyi_cost
        state.log.append(f'  追忆-{zhuiyi_cost} → {u.zhuiyi:.0f}/27')
    # 史诗消耗（开拓者·记忆强化普攻: 消耗1层【史诗】）
    epic_cost = skill.cost.get("_epic", 0)
    if epic_cost > 0 and u.char.id == 'trailblazer_remembrance':
        cur_epic = u.extra.get('tbr_epic', 0)
        if cur_epic < epic_cost:
            state.log.append(f'  [WARN] 史诗不足({cur_epic}<{epic_cost})')
            return
        u.extra['tbr_epic'] = cur_epic - epic_cost
        state.log.append(f'  史诗-{epic_cost} → {u.extra["tbr_epic"]}/2')

    charge_cost = skill.cost.get("_mydei_charge", 0)
    if charge_cost > 0 and u.char.id == 'mydei':
        # 献予「纷争」之诗: 免费施放(不耗充能) — 跳过扣减但保留E1变形
        if not u.extra.get('poem_fenzheng_free'):
            cur = u.extra.get('mydei_charge', 0)
            if cur < charge_cost:
                state.log.append(f'  [WARN] 充能不足({cur:.0f}<{charge_cost})')
                return
            u.extra['mydei_charge'] = cur - charge_cost
            u.extra['charge_locked'] = True  # 弑神登神期间无法积攒充能
            state.log.append(f'  充能-{charge_cost} → {cur - charge_cost:.0f}/200 (弑神登神)')
        # E1: 弑神登神主目标倍率+30%，且变成对敌方全体（按主目标倍率）
        # v5.7: deepcopy 防跨战斗污染（原实现直接改 char.skills 会跨模拟叠乘）
        if u.eidolon_rank >= 1:
            skill = copy.deepcopy(skill)
            main = next((m for m in skill.multipliers
                         if m.target in ('single_enemy', None, '')), skill.multipliers[0])
            main.scale = main.scale * 1.30
            main.target = 'all_enemies'
            skill.multipliers = [main]
            skill.target = 'all_enemies'
            state.log.append('  E1: 弑神登神倍率+30%且变全体')

    # 开拓者·记忆: 战技→迷迷回60%生命+10%充能；强化普攻→迷迷+10%充能
    if u.char.id == 'trailblazer_remembrance' and u.memsprite_unit and u.memsprite_unit.is_alive:
        ms = u.memsprite_unit
        if skill_key == "skill":
            ms.current_hp = min(ms.max_hp, ms.current_hp + ms.max_hp * 0.60)
            rem = state.extra.get('_rem_sys')
            ch = rem._mimi_charge_gain(state, ms, 10) if rem else ms.extra.get('charge', 0) + 10
            state.log.append(f'  战技: 迷迷回血60% + 充能+10% → {ch:.0f}%')
        elif skill_key == "basic_attack_enhanced":
            rem = state.extra.get('_rem_sys')
            ch = rem._mimi_charge_gain(state, ms, 10) if rem else ms.extra.get('charge', 0) + 10
            state.log.append(f'  强化普攻: 迷迷充能+10% → {ch:.0f}%')

    # 昔涟普攻/强化普攻获取追忆（天赋：众愿啊，汇流如歌）
    if u.char.id == 'xilian':
        zhuiyi_gain = {"basic_attack": 1, "basic_attack_enhanced": 3}.get(skill_key, 0)
        if zhuiyi_gain > 0:
            u.zhuiyi = min(27, u.zhuiyi + zhuiyi_gain)
            state.log.append(f'  追忆+{zhuiyi_gain} → {u.zhuiyi:.0f}/27')
    # 遐蝶终结技消耗新蕊
    if skill_key == 'ultimate' and u.char.id == 'xiadie':
        # 献予「生死」之诗: 清零前捕获溢出(34000以上部分), 召唤死龙时消费→强化晦翼
        u.extra['shengsi_overflow'] = max(0.0, u.xinrui - 34000.0)
        u.xinrui = 0

    # 全队HP消耗（遐蝶战技等，包含使用者自身，含忆灵）
    hp_pct_allies = skill.cost.get("hp_percent_allies", 0)
    if hp_pct_allies > 0:
        total_lost = 0.0
        affected = []
        # 角色
        for eu in state.units:
            if eu.is_alive:
                lost = eu.current_hp * (hp_pct_allies / 100.0)
                eu.current_hp = max(1, eu.current_hp - lost)
                total_lost += lost
                affected.append((eu, lost))
        # 忆灵（我方全体含忆灵；skill_dragon 除外——"除死龙以外"不扣死龙）
        for ms in state.memsprites:
            if not ms.is_alive:
                continue
            if skill_key == 'skill_dragon' and ms is u.memsprite_unit:
                continue  # 死龙不被自身强化战技扣血
            lost = ms.current_hp * (hp_pct_allies / 100.0)
            ms.current_hp = max(1, ms.current_hp - lost)
            total_lost += lost
            affected.append((ms, lost))
        # 遐蝶天赋：HP损失→新蕊/死龙回血（统一吸收）
        if u.char.id == "xiadie":
            _xiadie_absorb_hp_loss(state, total_lost, "全队HP消耗")
        # Hook: HP损失事件（Layer 1 — 与现有硬编码并行）
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                 total_lost=total_lost, affected=affected,
                                 skill_key=skill_key)
        _dispatch_changyeyue_hp_loss(state, affected)
        # 光锥 HP 损失事件属于实际失血单位；只触发施法者会漏掉受影响队友。
        for affected_unit, _lost in affected:
            if isinstance(affected_unit, SimUnit):
                state.extra['lc_last_hp_loss'] = _lost  # v5.4 在火的远处: 单次损失判定
                _process_lc_effects(affected_unit, state, "on_hp_loss")

    # 自身HP消耗（万敌战技/弑王成王/长夜月战技: 消耗当前生命%，不足降至1点）
    hp_pct_self = skill.cost.get("hp_percent_self", 0) or skill.cost.get("hp_percent", 0)
    if hp_pct_self > 0:
        lost = u.current_hp * (hp_pct_self / 100.0)
        u.current_hp = max(1, u.current_hp - lost)
        state.log.append(f'  HP消耗: -{lost:.0f} ({hp_pct_self}%当前生命) → {u.current_hp:.0f}')
        # 万敌天赋·以血还血: 每损失1%生命=1充能(最多200)
        if u.char.id == 'mydei' and not u.extra.get('charge_locked'):
            pct_lost = lost / max(u.max_hp, 1) * 100.0
            charge = min(200, u.extra.get('mydei_charge', 0) + pct_lost)
            u.extra['mydei_charge'] = charge
            state.log.append(f'  以血还血: 充能+{pct_lost:.0f} → {charge:.0f}/200')
        # 自身扣血也发 on_hp_loss（符玄E6累计/风堇E2等; 当前无注册者时零行为）
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                total_lost=lost, affected=[(u, lost)], skill_key=skill_key)
        state.extra['lc_last_hp_loss'] = lost
        _process_lc_effects(u, state, "on_hp_loss")
        _dispatch_changyeyue_hp_loss(state, [(u, lost)])

    # 自身HP消耗（按生命上限%: v5.3 流萤战技消耗40%生命上限, 不足降至1点）
    hp_pct_max_self = skill.cost.get("hp_percent_max_self", 0)
    if hp_pct_max_self > 0:
        lost = u.max_hp * (hp_pct_max_self / 100.0)
        u.current_hp = max(1, u.current_hp - lost)
        state.log.append(f'  HP消耗: -{lost:.0f} ({hp_pct_max_self}%生命上限) → {u.current_hp:.0f}')
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                total_lost=lost, affected=[(u, lost)], skill_key=skill_key)
        state.extra['lc_last_hp_loss'] = lost
        _process_lc_effects(u, state, "on_hp_loss")
        _dispatch_changyeyue_hp_loss(state, [(u, lost)])

    # v5.3 流萤战技: 固定恢复60%能量上限 + 自身行动提前25%（满级档）
    if u.char.id == 'firefly' and skill_key == 'skill':
        _gain_energy(u, 0.6, state=state, percent=True)
        navs = state.extra.get('navs', {})
        uidx = state.units.index(u) if u in state.units else -1
        if uidx >= 0 and uidx in navs:
            navs[uidx] = max(0, navs[uidx] - (AV_PER_TURN / _effective_spd(u, state)) * 0.25)
        state.log.append('  战技: 回60%能量上限, 行动提前25%')
    # v5.3 流萤强化战技: 倍率=击破特攻依赖（主(0.2×BE+200)%, 相邻(0.1×BE+100)%, 最多算360%BE）
    if u.char.id == 'firefly' and skill_key == 'skill_enhanced' and skill.multipliers:
        be = min(_build_effective_stats(u, state).BREAK_EFFECT, 3.6)
        skill = copy.deepcopy(skill)  # 动态倍率不污染角色数据
        for m in skill.multipliers:
            m.scale = (be * 0.1 * 100 + 100.0) if m.target == 'adjacent' else (be * 0.2 * 100 + 200.0)

    # Hook: 按技能类型触发
    skill_event = {
        "basic_attack": "on_basic_attack", "skill": "on_skill",
        "ultimate": "on_ultimate", "elation_skill": "on_elation_skill",
    }.get(skill_key)
    if qianye_new_ult:
        skill_event = 'on_ultimate'
    if skill_event:
        state.hooks.trigger(u.char.id, skill_event, u=u, state=state,
                             skill_key=skill_key, skill=skill)

    # 献予「创世」之诗: 开拓者强化普攻后→德谬歌额外回合自动花与箭
    # (德谬歌SPD=0禁止入X轴, 直调 _use_memsprite_skill 防除零)
    if u.char.id == 'trailblazer_remembrance' and skill_key == 'basic_attack_enhanced' \
            and u.extra.get('poem_chuangshi'):
        xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
        rem = state.extra.get('_rem_sys')
        if xilian and rem and xilian.memsprite_unit and xilian.memsprite_unit.is_alive:
            rem._use_memsprite_skill(state, xilian, xilian.memsprite_unit, "memsprite_basic")
            state.log.append('  献予「创世」之诗: 德谬歌额外回合→花与箭')

    # 风堇行迹3·雷雨轻柔: 战技/终结技→净化全队1个负面效果
    if u.char.id == 'fengjin' and skill_key in ('skill', 'ultimate'):
        _fengjin_cleanse(state, u)
        # 献予「天空」之诗: 战技/终结技后消耗1层
        layers = u.extra.get('poem_tiankong', 0)
        if layers > 0:
            u.extra['poem_tiankong'] = layers - 1
            state.log.append(f'  献予「天空」之诗: 消耗1层 ({layers-1}层)')

    # 角色钩子（每局注册）
    for hook in state.skill_hooks.get(u.char.id, []):
        result = hook(u, state, skill_key)
        if result is True:  # 钩子返回 True 表示已完全处理，跳过后续伤害
            return
    # v6.9.1: 不死途首战技——仅目标已是饲饵时才保留额外100%段
    if u.char.id == 'busitu' and skill_key == 'skill' \
            and not u.extra.get('busitu_skill_was_bait') and skill.multipliers:
        skill = copy.deepcopy(skill)
        skill.multipliers = [m for m in skill.multipliers if m.scale != 100.0]
        state.log.append('  不死途战技: 首次施加饲饵, 无额外100%段')


    # 遗器触发
    if skill_key == "ultimate" and "ult_action_advance_25" in getattr(u, '_active_relic_conditions', set()):
        advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.25
        u._pending_action_advance = advance
        state.log.append(f'  翔鹰拉条: +{advance:.0f}AV')

    # 伤害计算
    total_dmg = 0.0
    lc_targets_hit = 0  # v5.0.1: 本次攻击命中目标数（per_target 叠层用）
    state.extra['lc_attack_target_refs'] = []
    effects_pre_applied = False
    if u.char.id == 'firefly' and skill_key == 'skill_enhanced':
        # 火弱点必须在本次伤害/削韧前生效。
        _apply_skill_effects(u, state, skill, skill_key)
        effects_pre_applied = True
    if skill.multipliers:
        stats = _build_effective_stats(u, state)
        alive = state.alive_enemies() or state.enemies
        # v5.7: 万敌"下一次弑神登神优先攻击指定敌方单体"——优先目标存活时置首
        if u.char.id == 'mydei' and skill_key == 'skill_shenshen':
            pid = u.extra.get('mydei_priority_target_id')
            if pid and any(e.id == pid for e in alive):
                alive.sort(key=lambda e: 0 if e.id == pid else 1)
        st = None
        if skill_key == "basic_attack": st = "basic"
        elif skill_key in ("skill", "ultimate"): st = skill_key
        elif qianye_new_ult: st = 'ultimate'
        if u.char.id == 'acheron' and u.eidolon_rank >= 6 \
                and skill_key in ('basic_attack', 'skill'):
            st = 'ultimate'
        attack_type = _skill_attack_type(state, st)

        action_targets = []  # v6.3.0b P1-10: 本次攻击实际命中目标（银狼天赋/E1/E4 消费）
        for mult in skill.multipliers:
            sc = stats.ATK if mult.stat == "ATK" else (stats.HP if mult.stat == "HP" else 0)
            is_crit = stats.CRIT_RATE >= 0.5
            laugh_n = (
                (laugh_n_override if laugh_n_override is not None
                 else state.elation_state.get_good_show_total(u.char.id))
                if mult.damage_type == 'elation' else 0.0
            )
            # v5.3: 逐倍率目标（忘归人强化普攻: 主目标/相邻目标分倍率）
            mt = mult.target or skill.target
            targets = _select_targets(alive, mt)
            mult_scale = (mult.scale / len(targets)
                          if mult.split and targets else mult.scale)
            lc_targets_hit += len(targets)

            if mt == "bounce":
                hits = _bounce_hits(u, mult, state)  # v5.3 弹射段数（含同谐E6 +2; v6.7 绯英/火花）
                # v6.2.1: 声援/乱蝶/击杀管线已移入 _multihit_damage 逐段处理（Codex P1-1）
                total_dmg += _multihit_damage(stats, alive, sc, mult_scale,
                                              mult.damage_type, mult.element or u.char.element, is_crit,
                                              hits, st, true_dmg_ratio=state.realm_true_dmg, u=u, state=state,
                                              scaling_stat_name=mult.stat, attack_type=attack_type,
                                              laugh_n=laugh_n)
                # v6.8.2: 弹射命中汇总进 action_targets, 缓存立即清空防跨动作污染;
                # 重复段另存 last_hit_segments 供那刻夏「每击中1次」逐段计数（Harness 补）
                _mh = state.extra.get('last_multihit_targets', [])
                action_targets.extend(_mh)
                state.extra['last_multihit_targets'] = []
                state.extra['last_hit_segments'] = list(_mh)
            else:
                action_targets.extend(targets)
                for t in targets:
                    _record_lc_attack_target(state, t)
                    _hn_count_hits(state, u)  # v6.7 歼破协议: 每击中1目标+1充能
                    t_stats = _apply_target_relic_modifiers(stats, u, t)
                    # v6.7b 歼破协议: 战技造成的暴击伤害额外+100%
                    if st == 'skill' and state.extra.get('hn_charge_skill_cd'):
                        t_stats = copy.deepcopy(t_stats)
                        t_stats.CRIT_DMG += 1.0
                    # v5.0 P3: 光锥目标相关条件（对HP≤50%目标增伤等）
                    t_stats = _lc_target_correct(t_stats, u, state, t)
                    # v6.3.0 银狼E6: 目标每有1个负面效果伤害+20%, 最多+100%
                    if u.char.id == 'silver_wolf' and u.eidolon_rank >= 6:
                        n = min(getattr(t, 'debuff_count', lambda: 0)(), 5)
                        if n > 0:
                            t_stats = copy.deepcopy(t_stats)
                            t_stats.DMG_BONUS_ALL += 0.20 * n
                    t_stats = _target_attacker_stats(t_stats, u, state, t, st)
                    target_sc = _target_scaling_stat(stats, sc, mult.stat, state, t)
                    t_crit = t_stats.CRIT_RATE >= 0.5
                    # 布洛妮娅行迹·号令: 普攻必暴
                    if u.char.id == 'bronya' and skill_key == 'basic_attack':
                        t_crit = True
                    # 希儿行迹·斩尽(与E1同效果, 无条件): 对HP≤80%目标暴击+15%且无视20%防御
                    # (v5.2 期望模式: CR+15% 写入面板副本进入期望公式, DEF_PEN 同副本)
                    if u.char.id == 'seele':
                        bp = state.extra.get('enemy_blueprint') or state.enemies[0]
                        if bp.HP > 0 and t.HP <= bp.HP * 0.80:
                            t_crit = True  # 旧布尔模式兼容（期望模式忽略）
                            t_stats = copy.deepcopy(t_stats)
                            t_stats.CRIT_RATE = min(1.0, t_stats.CRIT_RATE + 0.15)
                            t_stats.DEF_PEN += 0.20
                    d = calculate_damage(t_stats, _enemy_for_damage(t, st), target_sc, mult_scale, mult.damage_type,
                                         mult.element or u.char.element, 80, t_crit,
                                         skill_type=st, true_dmg_ratio=state.realm_true_dmg,
                                         attack_type=attack_type, laugh_n=laugh_n,
                                         crit_mode="expected")
                    if u.char.id == 'acheron' and st in ('basic', 'skill', 'ultimate'):
                        d.final_damage *= _acheron_original_damage_multiplier(u, state)
                    total_dmg += d.final_damage
                    # 符玄E6·种陵: 终结技伤害+累计损失×200%（累计在 on_hp_loss 封顶符玄生命120%）
                    if u.char.id == 'fu_xuan' and u.eidolon_rank >= 6 and skill_key == 'ultimate':
                        bonus = state.extra.get('fuxuan_lost_hp_total', 0.0) * 2.0
                        if bonus > 0:
                            d.final_damage += bonus
                            total_dmg += bonus
                            state.log.append(f'  E6种陵: 损失增幅+{bonus:.0f}')
                    _, killed_by_hit = _commit_enemy_damage(
                        state, u, t, d.final_damage,
                        damage_type=mult.damage_type, skill_type=st,
                        attack_type=attack_type,
                        cipher_record_amount=(
                            d.final_damage / (1.0 + state.realm_true_dmg)
                            if state.realm_true_dmg > 0 else None))
                    # v5.7: 迷迷的声援逐段触发（每造成1次伤害→额外28%真伤, 实机逐段）
                    total_dmg += _apply_tbr_support(state, u, t, d.final_damage)
                    # 希儿行迹·离析(与E6同效果, 无条件): 终结技命中→目标陷入【乱蝶】(真伤30%快照, 3次触发)
                    if u.char.id == 'seele' and skill_key == 'ultimate':
                        if t.HP > 0:
                            t.extra['luandie'] = 3
                            t.extra['luandie_ult_dmg'] = d.final_damage
                            state.log.append('  乱蝶: 目标陷入乱蝶(真伤30%×3)')
                    # 乱蝶受击追加真伤（先结算本次伤害, 乱蝶真伤致死也计入击杀）
                    _apply_luandie(state, t, u)
                    # 击杀检测（希儿再现/on_kill钩子）
                    if killed_by_hit:
                        # v5.1: 遐蝶行迹2·倒置的火炬 — 乌黯击杀→死龙速度+100%/1回合
                        if u.char.id == 'xiadie' and u.memsprite_unit and u.memsprite_unit.is_alive:
                            u.memsprite_unit.extra['xiadie_spd_boost'] = 1
                            state.log.append('  倒置的火炬: 死龙速度+100%(1回合)')
                # 波次中段刷新：全灭后立即补充敌人
                if not state.alive_enemies():
                    _respawn_wave(state)

        # v6.3.0b P1-10: 本次攻击实际命中目标去重存档（含被本次击杀者; 银狼消费）
        _seen = set()
        state.extra['last_attack_targets'] = [
            t for t in action_targets
            if t is not None and not (id(t) in _seen or _seen.add(id(t)))]

        # 银狼Lv.999天赋：持有好活时，普攻/战技对实际受击且仍存活的目标
        # 追加40%虚数欢愉伤害；强化普攻在其专属路径中已直接改为欢愉伤害。
        if u.char.id == 'yinlang' and skill_key in ('basic_attack', 'skill') \
                and state.elation_state.get_good_show_total('yinlang') > 0:
            laugh_n = state.elation_state.get_good_show_total('yinlang')
            extra_total = 0.0
            for t in state.extra['last_attack_targets']:
                if t.HP <= 0:
                    continue
                extra_stats = _target_attacker_stats(stats, u, state, t, st)
                extra = calculate_damage(
                    extra_stats, _enemy_for_damage(t, st), 0.0, 40.0,
                    'elation', u.char.element, 80,
                    extra_stats.CRIT_RATE >= 0.5, laugh_n=laugh_n,
                    skill_type=st, attack_type=attack_type,
                    crit_mode='expected')
                _commit_enemy_damage(state, u, t, extra.final_damage)
                extra_total += extra.final_damage
            if extra_total > 0:
                total_dmg += extra_total
                state.log.append(f'  银狼持好活: {skill_key}追加40%欢愉伤害 {extra_total:.0f}')

        u.total_damage_dealt += total_dmg
        # v5.0.1: 攻击完成事件（自身 + 广播）— 叠层触发（歌咏/织锦/龙吟等）
        state.extra['lc_attack_targets'] = lc_targets_hit
        # v5.4 论剑: 记录本次攻击首个目标（同目标叠层判定）
        _first = state.extra.get('lc_attack_target_refs', [])
        state.extra['lc_attack_first_target_id'] = _first[0].id if _first else ''
        _process_lc_effects(u, state, "on_self_attack")
        for eu in state.units:
            if eu.is_alive:
                _process_lc_effects(eu, state, "on_attack")
        # 符玄E6·种陵: 终结技结算后清空累计损失（循环外, 多目标只清一次）
        if u.char.id == 'fu_xuan' and u.eidolon_rank >= 6 and skill_key == 'ultimate':
            if state.extra.get('fuxuan_lost_hp_total', 0.0) > 0:
                state.extra['fuxuan_lost_hp_total'] = 0.0
                state.log.append('  E6种陵: 累计损失已清空')
        # 普攻后全队监听（布洛妮娅E4等"队友普攻→追加攻击"类效果）
        if skill_key == 'basic_attack' and state.alive_enemies():
            first_target = state.alive_enemies()[0]
            state.hooks.trigger_all("on_ally_attack", u=u, state=state,
                                    skill_key=skill_key, skill=skill, target=first_target)

        # ---- v6.7 角色追加结算（伤害循环后, 目标信息就绪）----
        # 绯英持好活当赏: 战技对受击目标16%物理欢愉伤害; 终结技全体23%+随机目标28%
        if u.char.id == 'evanescia' and total_dmg > 0 and state.alive_enemies():
            _evanescia_goodshow_extra(state, u, skill_key)
        # 火花强化普攻: 互动陷阱结算(20%/10%追加+礼物+天赋10%弹射) + 持好活40%/20%欢愉追加
        if u.char.id == 'sparxie' and skill_key == 'basic_attack_enhanced':
            _sparxie_enhanced_settle(state, u)
        # v6.8.1: 火花持好活当赏→终结技额外全体48%火属性欢愉伤害（txt 天赋:66, 此前整段缺失）
        if u.char.id == 'sparxie' and skill_key == 'ultimate' \
                and state.elation_state.get_good_show_total('sparxie') > 0 \
                and state.alive_enemies():
            _sparxie_ult_elation_extra(state, u)
        # 大丽花天赋: 共舞者攻击→E1固定削韧; 另一共舞者攻击→FUA 5×30%(每回合最多1次)
        # v6.7b: 大丽花自身攻击也触发 E1（txt 共舞者含自身）
        if total_dmg > 0 and state.alive_enemies():
            _dahlia_on_ally_attack(state, u)
        # v6.9 瓦尔特: 附加伤害(减速目标100%/行迹2 80-120%/E1 40%) + 战技逐段减速 + 失重推条
        if u.char.id == 'welt' and total_dmg > 0:
            _welt_extra_damage(state, u, skill_key)
        # v6.9.1: 失重通用受击钩子——任何我方攻击命中失重目标→行动延后4%(≤8次/回合)
        # + 行迹1 易伤叠层(最多10层, 此前只瓦尔特攻击触发且固定10%)
        _welt_ally_hit_hooks(state, skill_key)
        # v6.9 阮·梅: 结界期攻击后对受击目标挂【残梅绽】
        if u.char.id != 'ruan_mei' and total_dmg > 0 and _ruanmei_field_active(state):
            for t in state.extra.get('last_attack_targets', []):
                _ruanmei_apply_canmei(state, u, t)
        # v6.9 知更鸟: 协奏期每次我方攻击后附加120%ATK物理伤(固定双暴)
        if total_dmg > 0:
            _robin_concert_extra(state, u)
        # v6.11.1 晴歌: 我方目标施放攻击→晴歌气氛+1(特邀嘉宾持有者额外+2/E2/律动/偏离和弦)
        if total_dmg > 0:
            _qingge_on_ally_attack(state, u)
        # v6.9 不死途: 饲饵受其他目标攻击→回8能+耗1充能FUA
        if u.char.id != 'busitu' and total_dmg > 0:
            _busitu_on_ally_attack(state, u)
        # v6.9 千冶·刃: 结界期我方每次攻击→目标煞火缠身+1充能
        if total_dmg > 0:
            _qianye_on_ally_attack(state, u)
        # v6.10 飞霄: 每2次攻击+1飞黄 + 队友攻击后立即FUA
        if total_dmg > 0:
            _feixiao_count_attack(state, u, is_ult=(skill_key == 'ultimate'))
            _feixiao_on_ally_attack(state, u)
        if u.char.id == 'qianye' and skill_key in ('basic_attack', 'basic_attack_enhanced') \
                and total_dmg > 0:
            _apply_enemy_taunt(state, u, state.extra.get('last_attack_targets', []), turns=1)

        # 【迷迷的声援】已单点化于 _apply_tbr_support（v5.7: 逐段伤害触发, 见伤害循环）

        # 阿格莱雅天赋·金玫之指: 攻击使最新目标陷入【间隙织线】
        if u.char.id == 'aglaea' and total_dmg > 0 and skill.multipliers:
            for e in state.enemies:
                e.extra['gossamer'] = False
                e.extra.pop('gossamer_dmg_bonus', None)  # v5.7: 换目标时同步清除易伤
            targets = _select_targets(state.alive_enemies() or state.enemies,
                                      skill.target if skill.target != 'blast' else 'single_enemy')
            if targets:
                targets[0].extra['gossamer'] = True
                # E1: 织线目标受到的伤害提高15%
                if u.eidolon_rank >= 1:
                    targets[0].extra['gossamer_dmg_bonus'] = 0.15
                state.log.append(f'  【间隙织线】: {targets[0].name or targets[0].id}')
                # E4: 阿格莱雅攻击后也能使衣匠获得速度层
                if u.eidolon_rank >= 4 and u.memsprite_unit and u.memsprite_unit.is_alive:
                    ms = u.memsprite_unit
                    stack = ms.extra.get('spd_stack', 0)
                    if stack < 7:
                        ms.extra['spd_stack'] = stack + 1
                        state.log.append(f'  E4: 阿格莱雅攻击→衣匠速度层+1 ({stack+1}/7)')
                # 献予「浪漫」之诗: 阿格莱雅攻击后消耗【浪漫】回70能量
                if u.extra.pop('poem_langman', None):
                    _gain_energy(u, 70.0, state=state)
                    state.log.append(f'  献予「浪漫」之诗: 攻击回70能量 ({u.current_energy:.0f})')

        # 削韧计算（仅在有伤害倍率且实际造成伤害时触发）
        if total_dmg > 0:
            toughness_targets = state.alive_enemies() or state.enemies
            for eff in skill.effects:
                etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
                if etype != 'toughness_reduction':
                    continue
                base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
                eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
                break_element = u.char.element
                if eff_target == 'bounce':
                    # v5.3 弹射削韧: 每跳均分总削韧, 逐跳随机目标（同谐行迹2: 首跳+100%）
                    hits = _bounce_hits(u, skill.multipliers[0], state) if skill.multipliers else 1
                    per_hit = base_toughness / hits
                    first_double = bool(u.extra.pop('tbh_bounce_first_double', False))
                    for i in range(hits):
                        tgt = random.choice(toughness_targets)
                        td = per_hit * (2.0 if (i == 0 and first_double) else 1.0)
                        total_dmg += _apply_toughness_damage(
                            state, u, tgt, td, break_element, skill_key, stats)
                else:
                    tgt_list = _select_targets(toughness_targets, eff_target)
                    for t in tgt_list:
                        total_dmg += _apply_toughness_damage(
                            state, u, t, base_toughness, break_element, skill_key, stats)

    # 日志
    # v5.7: 开拓者·记忆终结技→迷迷240%ATK全体冰伤（文档: 使迷迷对敌方全体造成240%攻击力伤害;
    # 伤害由忆灵释放, JSON multipliers 为空, 须在 if skill.multipliers 块外, 此前伤害段缺失）
    if skill_key == 'ultimate' and u.char.id == 'trailblazer_remembrance' \
            and u.memsprite_unit and u.memsprite_unit.is_alive:
        ms = u.memsprite_unit
        from engine.systems.remembrance import _ms_effective_stats
        ms_stats = _ms_effective_stats(ms, state)
        # v5.7 E6: 终结技的暴击率固定为100%
        ult_crit = u.eidolon_rank >= 6 or ms_stats.CRIT_RATE >= 0.5
        mimi_damage = 0.0
        for t in (state.alive_enemies() or state.enemies):
            d = calculate_damage(ms_stats, _enemy_for_damage(t), ms_stats.ATK, 240.0,
                                 "direct", "冰", 80, ult_crit,
                                 skill_type="ultimate", true_dmg_ratio=state.realm_true_dmg,
                                 crit_mode="expected")
            total_dmg += d.final_damage
            mimi_damage += d.final_damage
            _commit_enemy_damage(
                state, u, t, d.final_damage,
                cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
            _apply_luandie(state, t, u)
        # 削韧20（主削韧块在 if skill.multipliers 内被跳过, 此处按 JSON ultimate effects 结算）
        u_stats = _build_effective_stats(u, state)
        for eff in skill.effects:
            etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
            if etype != 'toughness_reduction':
                continue
            base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
            eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
            for t in _select_targets(state.alive_enemies() or state.enemies, eff_target):
                total_dmg += _apply_toughness_damage(
                    state, u, t, base_toughness, "冰", skill_key, u_stats)
        state.log.append(f'  终结技: 迷迷240%ATK全体冰伤')
        u.total_damage_dealt += mimi_damage
        _qingge_notify_attack(state, u, dealt=mimi_damage > 0)  # v7.1.0 P1: 0倍率终结技补气氛
    u.damage_log.append((skill.name, total_dmg, skill_key))
    state.log.append(f'[{state.current_av:6.0f}AV] {u.char.name} {skill.name}: {total_dmg:.0f}')
    # 行动计数（轮次统计用）
    state.action_counts[u.char.id] = state.action_counts.get(u.char.id, 0) + 1

    # v6.3.0 银狼机制（角色技能介绍/银狼.txt）
    if u.char.id == 'silver_wolf' and total_dmg > 0:
        # 天赋: 每次施放攻击后 100% 概率给受击目标植入1个随机缺陷
        # v6.3.0b P1-10: 只遍历本次攻击实际命中目标（此前用 alive_enemies 扩大到全部存活敌）
        hit_targets = state.extra.get('last_attack_targets') or []
        for t in hit_targets:
            _silver_wolf_implant_defect(state, u, t)
        # E1/E4: 终结技后每负面回7能量(上限5次) + 每负面附加20%ATK量子伤(每目标上限5次)
        if skill_key == 'ultimate':
            from engine.core.damage import calculate_damage as _cd
            for t in hit_targets:
                if getattr(t, 'HP', 0) <= 0:
                    continue
                n = min(t.debuff_count(), 5)
                if n <= 0:
                    continue
                if u.eidolon_rank >= 1:
                    _gain_energy(u, 7.0 * n, state=state)
                    state.log.append(f'  银狼E1: 每负面回能+{7*n:.0f}')
                if u.eidolon_rank >= 4:
                    stats = _build_effective_stats(u, state)
                    add_d = _cd(stats, _enemy_for_damage(t), stats.ATK, 20.0 * n,
                                'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                                crit_mode='expected')
                    _commit_enemy_damage(state, u, t, add_d.final_damage)
                    u.total_damage_dealt += add_d.final_damage
                    state.log.append(f'  银狼E4: 每负面附加{add_d.final_damage:.0f}(20%×{n})')
        # 天赋: 弱点转移（被消灭目标若带银狼弱点→转移给存活未添加的敌人, 优先精英）
        for t in list(state.enemies):
            if t.HP > 0:
                continue
            st = next((s for s in t.statuses if s.id == 'silver_wolf_weakness'), None)
            if st is None:
                continue
            candidates = [e for e in state.enemies if e.HP > 0
                          and not any(s.id == 'silver_wolf_weakness' for s in e.statuses)]
            if candidates:
                elite = [e for e in candidates if getattr(e, 'is_elite', False)]
                to = (elite or candidates)[0]
                to.add_status(copy.deepcopy(st))
                state.log.append(f'  银狼弱点转移: {t.name or t.id}→{to.name or to.id}')
    elif u.char.id != 'silver_wolf':
        # E2: 我方目标攻击时, 银狼100%概率给受击目标植入随机缺陷
        sw = next((x for x in state.units if x.char.id == 'silver_wolf'
                   and x.is_alive and x.eidolon_rank >= 2), None)
        if sw and total_dmg > 0:
            # v6.3.0b P1-10: 同样只遍历实际命中目标
            for t in (state.extra.get('last_attack_targets') or []):
                if getattr(t, 'HP', 0) > 0:
                    _silver_wolf_implant_defect(state, sw, t)

    # ── v6.6 批1: 缇宝 / 刻律德菈 / 丹恒·腾荒 ──
    if u.char.id == 'tribbie':
        # 战技: 【神启】3回合
        if skill_key == 'skill':
            _tribbie_apply_shenqi(u, state)
        # 终结技: 结界 + 重置队友FUA次数 + E6 立即FUA
        if skill_key == 'ultimate':
            _tribbie_ult_field(u, state)
            # v6.6c P1: 缇宝终结技重置每个队友的 FUA 次数（此前 tribbie_ult_used 门锁反了——
            # 首次开大后队友 FUA 永远被封死）
            for k in list(u.extra.keys()):
                if k.startswith('tribbie_fua_'):
                    del u.extra[k]
            if u.eidolon_rank >= 6:
                _tribbie_talent_fua(state, u)
    # 缇宝天赋: 队友终结技→FUA（每角色1次/缇宝终结技重置）
    if u.char.id != 'tribbie' and skill_key == 'ultimate':
        trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
        if trib and not trib.extra.get(f'tribbie_fua_{u.char.id}'):
            trib.extra[f'tribbie_fua_{u.char.id}'] = True
            _tribbie_talent_fua(state, trib)
    # 缇宝结界受击附加（任何我方攻击命中后; v6.8.1: 传受击目标集合+本次总伤害）
    if state.extra.get('tribbie_field_turns', 0) > 0 and total_dmg > 0:
        trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
        if trib:
            # v6.8.2 极简会话: 弹射命中已在 2781 汇总进 last_attack_targets 且随后清空
            # multihit 缓存, 此处只取 last_attack_targets（Harness 修: 删除悬空表达式+续行符）
            hit_targets = list(state.extra.get('last_attack_targets') or [])
            seen = set()
            uniq = []
            for t in hit_targets:
                if t is not None and id(t) not in seen:
                    seen.add(id(t))
                    uniq.append(t)
            _tribbie_field_extra_damage(state, trib, uniq, total_dmg)

    if u.char.id == 'cerydra':
        # 战技: 军功授予
        if skill_key == 'skill':
            ally = _pick_single_ally_target(state, u)
            if ally:
                _cerydra_grant_jungong(state, u, ally)
                # 行迹3: 战技后自身+军功者SPD+20 3回合（v6.6c: 防重入 + 记录受buff者, 到期回减正确对象）
                if not u.extra.get('cerydra_spd_buff_turns'):
                    u.base_stats.SPD += 20
                    ally.base_stats.SPD += 20
                    u.extra['cerydra_spd_buff_ally'] = ally.char.id
                u.extra['cerydra_spd_buff_turns'] = 3
                if u.eidolon_rank >= 1:
                    _gain_energy(ally, 2.0, state=state)
        # 终结技: 充能+2 + 无军功者→队伍第一 + 附加重置
        if skill_key == 'ultimate':
            u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 2)
            # v6.8.1: 军功者死亡也算无军功者（_cerydra_jungong_target 校验存活）
            if not _cerydra_jungong_target(state, u):
                first = min(state.units, key=lambda x: getattr(x, 'position', 99))
                if first is not u:
                    _cerydra_grant_jungong(state, u, first)
            u.extra['cerydra_fua_count'] = 0
    # 刻律德菈天赋: 军功者攻击后60%ATK风附加（20次/终结技重置）
    if u.extra.get('cerydra_jungong') and total_dmg > 0:
        cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if cery:
            cnt = cery.extra.get('cerydra_fua_count', 0)
            if cnt < 20:
                cery.extra['cerydra_fua_count'] = cnt + 1
                _gain_energy(cery, 5.0, state=state)  # v6.6c: 行迹3 军功者攻击回能5点（模块级函数, 勿局部import）
                stats = _build_effective_stats(cery, state)
                for t in (state.alive_enemies() or state.enemies):
                    if getattr(t, 'HP', 0) > 0:
                        attached_scale = 60.0 * (3.0 if cery.eidolon_rank >= 6 else 1.0)
                        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, attached_scale,
                                             'direct', '风', 80, stats.CRIT_RATE >= 0.5,
                                             crit_mode='expected')
                        _commit_enemy_damage(state, cery, t, d.final_damage)
                        cery.total_damage_dealt += d.final_damage
                state.log.append(f'  刻律德菈附加: {attached_scale:.0f}%ATK风 ({cnt+1}/20)')
    # v6.6c: 奇袭——爵位者战技后消耗6充能降回军功
    if skill_key == 'skill' and u.extra.get('cerydra_juewei'):
        cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if cery and cery.extra.get('cerydra_juewei_target') == u.char.id:
            _cerydra_qixi(state, cery, u)
    # 刻律德菈军功者终结技→充能+1（行迹2, 1次/场）
    if u.extra.get('cerydra_jungong') and skill_key == 'ultimate':
        cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if cery and not cery.extra.get('cerydra_trace2_used'):
            cery.extra['cerydra_trace2_used'] = True
            cery.extra['cerydra_charge'] = min(8, cery.extra.get('cerydra_charge', 0) + 1)
            _cerydra_check_promote(state, cery, u)
            state.log.append('  行迹·见者: 军功者终结技→充能+1')

    if u.char.id == 'dan_heng_permansor_terrae':
        # 战技: 同袍 + 全队护盾 + 龙灵
        if skill_key == 'skill':
            ally = _pick_single_ally_target(state, u)
            if ally:
                if u.extra.get('dht_tongpao_id') and u.extra['dht_tongpao_id'] != ally.char.id:
                    ally2 = next((x for x in state.units if x.char.id == u.extra['dht_tongpao_id']), None)
                    if ally2:
                        ally2.extra['dht_tongpao'] = False
                for enemy in state.enemies:
                    enemy.extra.pop('dht_tongpao_vuln', None)
                u.extra['dht_tongpao_id'] = ally.char.id
                ally.extra['dht_tongpao'] = True
                if u.eidolon_rank >= 6:
                    for enemy in state.enemies:
                        enemy.extra['dht_tongpao_vuln'] = 0.20
                _dht_apply_shield(state, u, 20.0, 400, '渊渟岳峙')
                _dht_summon_longling(state, u, ally)
        # 终结技: 龙灵强化
        if skill_key == 'ultimate':
            u.extra['dht_longling_enhanced'] = 2 + (2 if u.eidolon_rank >= 2 else 0)
            if u.eidolon_rank >= 1:
                _gain_skill_points(state, 1)
                tong = next((x for x in state.units
                             if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
                if tong:
                    tong.buffs = [b for b in tong.buffs
                                  if getattr(b, 'param_id', '') != 'dht_e1_respen']
                    tong.buffs.append(TimedBuff(source_id='dht',
                                                attributes={'RES_PEN_ALL': 18.0},
                                                remaining_turns=3, param_id='dht_e1_respen',
                                                source_name='丹恒·腾荒E1'))
            if u.eidolon_rank >= 2:
                sys = state.extra.get('_marker_sys')
                if sys and u.marker:
                    sys.advance(state, u, 1.0)
            state.log.append(f'  龙灵强化: {u.extra["dht_longling_enhanced"]}次行动')
            if u.eidolon_rank >= 6:
                tong = next((x for x in state.units
                             if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
                if tong:
                    tong_stats = _build_effective_stats(tong, state)
                    for target in list(state.alive_enemies()):
                        d = calculate_damage(
                            tong_stats, _enemy_for_damage(target), tong_stats.ATK, 330.0,
                            'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                            skill_type='ultimate', attack_type='follow_up',
                            crit_mode='expected')
                        _commit_enemy_damage(state, u, target, d.final_damage)
                        u.total_damage_dealt += d.final_damage
                    state.log.append('  丹恒·腾荒E6: 同袍附加330%ATK群攻')
    if total_dmg > 0 and u.extra.get('dht_tongpao'):
        dht = next((x for x in state.units
                    if x.char.id == 'dan_heng_permansor_terrae' and x.is_alive), None)
        if dht and any(getattr(t, 'hook_name', '') == 'dht_trace2'
                       for t in (dht.char.traces or [])):
            _gain_energy(dht, 6.0, state=state)
            marker_system = state.extra.get('_marker_sys')
            if marker_system and dht.marker:
                marker_system.advance(state, dht, 0.15)
    # ── v6.6 批2: 海瑟音 / 那刻夏 / 赛飞儿 ──
    if u.char.id == 'hysilens':
        if skill_key == 'ultimate':
            _hysilens_field(state, u)
            stats = _build_effective_stats(u, state)
            from engine.core.damage import calculate_damage as _cd
            for e in state.enemies:
                for st in list(e.statuses):
                    if st.category != 'dot':
                        continue
                    mult = st.attributes.get('multiplier', 0) or 0
                    if mult > 0:
                        d = _cd(stats, _enemy_for_damage(e), stats.ATK,
                                mult * 1.5 * (1.16 if u.eidolon_rank >= 1 else 1.0), 'dot',
                                st.attributes.get('element', '物理'), 80, False)
                        _commit_enemy_damage(state, u, e, d.final_damage)
                        u.total_damage_dealt += d.final_damage
            state.log.append('  行迹·泡沫: 现存DOT立即结算150%')
            state.extra['hysilens_trigger_count'] = 0
    if total_dmg > 0:
        hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
        if hs:
            # v6.8.1: 仅被击中的目标陷入（txt:56「我方目标攻击时使被击中的敌方目标陷入」,
            # 此前对所有存活敌挂 DOT 且排除海瑟音自己）
            hit = list(state.extra.get('last_attack_targets') or [])
            seen = set()
            for t in hit:
                if t is None or t.HP <= 0 or id(t) in seen:
                    continue
                seen.add(id(t))
                _hysilens_apply_dot(state, hs, t, e1_double=True)  # E1: 天赋路径额外陷入一次
    # v6.6c P1: 引爆窗口已改挂 DOT 跳伤路径（敌方回合②, 见 _begin_enemy_turn）——
    # 此处原「我方任意攻击→对所有场域敌人触发反打」过宽, 已移除。

    if u.char.id == 'anaxa':
        if skill_key == 'skill' and u.eidolon_rank >= 1:
            if not u.extra.get('anaxa_e1_first_skill_used'):
                u.extra['anaxa_e1_first_skill_used'] = True
                _gain_skill_points(state, 1)
            from engine.models.enemy import EnemyStatus
            for target in state.extra.get('last_attack_targets', []):
                if target.HP > 0:
                    target.add_status(EnemyStatus(
                        id='anaxa_e1_def_down', name='那刻夏E1', category='debuff',
                        source='anaxa', remaining_turns=2,
                        attributes={'def_reduction': 0.16},
                    ))
        if total_dmg > 0:
            # v6.8.1: 每击中1次→为目标添加1个弱点（txt:53, 逐段含弹射重复段;
            # 此前对所有存活敌各加1个）
            # 逐段: 去重目标 + 弹射重复段（last_hit_segments, v6.8.2 防缓存清空丢段信息）
            # v6.8.3: 优先逐段命中（弹射含重复段）, 缺失回退去重目标集
            hit = list(state.extra.get('last_hit_segments') or state.extra.get('last_attack_targets') or [])
            for t in hit:
                if t is not None and t.HP > 0:
                    _anaxa_add_weakness(state, u, t)
                    _anaxa_reveal_check(state, u, t)
        # v6.6c P2: 献予「理性」——战技伤害次数+3（单次生效, 用后消费）
        if skill_key == 'skill' and u.extra.get('poem_lixing'):
            stats = _build_effective_stats(u, state)
            sk = u.char.skills.get('skill')
            if sk and sk.multipliers:
                m = sk.multipliers[0]
                sc = stats.ATK if m.stat == 'ATK' else (stats.HP if m.stat == 'HP' else 0)
                for _ in range(3):
                    for t in (state.alive_enemies() or state.enemies):
                        if getattr(t, 'HP', 0) <= 0:
                            continue
                        d = calculate_damage(stats, _enemy_for_damage(t), sc, m.scale,
                                             m.damage_type, m.element or u.char.element, 80,
                                             stats.CRIT_RATE >= 0.5, skill_type='skill',
                                             crit_mode='expected')
                        _commit_enemy_damage(state, u, t, d.final_damage)
                        u.total_damage_dealt += d.final_damage
            u.extra.pop('poem_lixing', None)
            state.log.append('  献予「理性」: 战技额外3次伤害已结算')
        if skill_key == 'ultimate':
            from engine.models.enemy import EnemyStatus
            for e in state.enemies:
                existing_ult = {st.attributes.get('weakness_element')
                                for st in e.statuses if st.id.startswith('anaxa_ult_weak')}
                for el in WEAKNESS_ELEMENTS:
                    old = e.get_res(el)
                    if old > 0:
                        e.element_res[el] = min(old, -0.2)
                        e.add_status(EnemyStatus(
                            id='anaxa_ult_weak_' + el, name='弱点', category='debuff',
                            source='anaxa', remaining_turns=1,
                            attributes={'weakness_element': el, 'weakness_old_res': old}))
                    elif el not in existing_ult:
                        # 已是弱点(天赋添加): 仅挂升华标记(至目标回合开始), 不重复改抗
                        e.add_status(EnemyStatus(
                            id='anaxa_ult_weak_' + el, name='弱点', category='debuff',
                            source='anaxa', remaining_turns=1,
                            attributes={'weakness_element': el, 'weakness_old_res': old}))
            state.log.append('  【升华】: 全7属性弱点+硬控')
            # v6.6c P2: 质性揭露目标受硬控（禁锢 2回合, 敌方回合跳过+推条2500）
            for e in state.enemies:
                if e.extra.get('anaxa_revealed') and getattr(e, 'HP', 0) > 0:
                    e.add_status(EnemyStatus(id='anaxa_imprison', name='禁锢',
                                             category='control', source='anaxa',
                                             remaining_turns=2))
                    state.log.append(f'  【升华】硬控: {e.name or e.id} 禁锢(2回合)')

    if u.char.id == 'cipher':
        if skill_key == 'skill':
            # v6.6c P1: 防重入（此前每次战技+30%ATK永久漂移）; 2回合到期回减（回合开始 tick）
            if not u.extra.get('cipher_atk_buff'):
                u.base_stats.ATK += u.base_stats._base_ATK * 0.30
            u.extra['cipher_atk_buff'] = 2
        if skill_key == 'ultimate':
            rec = u.extra.get('cipher_record', 0.0)
            t0 = state.extra.get('cipher_action_main_target')
            record_targets = state.extra.get('cipher_action_targets', [])
            if t0 and record_targets and rec > 0:
                record_damage = 0.0
                dealt, _ = _commit_enemy_damage(
                    state, u, t0, rec * 0.25, damage_type='true_damage',
                    skill_type='ultimate', record_cipher=False)
                record_damage += dealt
                shared = rec * 0.75 / len(record_targets)
                for target in record_targets:
                    dealt, _ = _commit_enemy_damage(
                        state, u, target, shared, damage_type='true_damage',
                        skill_type='ultimate', record_cipher=False)
                    record_damage += dealt
                u.total_damage_dealt += record_damage
                state.log.append(
                    f'  猫咪怪盗: 记录真伤25%主目标+75%均分 {record_damage:.0f}')
            keep = rec * 0.20 if u.eidolon_rank >= 6 else 0.0
            u.extra['cipher_record'] = keep
            state.log.append('  记录清空(E6返还%.0f)' % keep)

    # ── v6.6 批3: 白厄（变身状态机）──
    if u.char.id == 'phainon':
        if skill_key == 'skill':
            _phainon_gain_huozhong(state, u, 2)
        if skill_key == 'ultimate':
            # 耗12火种变身（扣减由 _phainon_transform 统一处理）
            if u.extra.get('huozhong', 0) >= 12:
                _phainon_transform(state, u)
                if u.eidolon_rank >= 1:
                    u.buffs = [b for b in u.buffs
                               if getattr(b, 'param_id', '') != 'phainon_e1_ult_cd']
                    u.buffs.append(TimedBuff(
                        source_id='phainon', attributes={'CRIT_DMG': 50.0},
                        remaining_turns=3, param_id='phainon_e1_ult_cd',
                        source_name='白厄E1'))
            else:
                state.log.append(f'  [WARN] 火种不足({u.extra.get("huozhong", 0)}<12)')
        # 卡形态: 普攻/战技切换（skill_enhanced=弑魂焚诏, basic_attack_enhanced=血棘渡亡）
        if u.extra.get('kasier'):
            if skill_key in ('basic_attack', 'basic_attack_enhanced'):
                _phainon_gain_huishang(state, u, 2)
            if skill_key in ('skill', 'skill_enhanced'):
                # 弑魂焚诏: 毁伤+敌数 + 弑魂之炽1层(E4+4) + 敌方全体立即行动
                n_enemies = len(state.alive_enemies() or state.enemies)
                _phainon_gain_huishang(state, u, n_enemies)
                stacks = 1 + (4 if u.eidolon_rank >= 4 else 0)
                u.extra['shihun_stacks'] = stacks
                navs = state.extra.get('navs', {})
                for i, e in enumerate(state.enemies):
                    _set_av(state, navs, ('e', i), state.current_av)  # 敌立即行动(后到先动)
                # v6.6b P1-1: 减伤走 TimedBuff 实际面板（此前 DMG_REDUCTION_TAKEN 无消费端且累加不还原）
                if not any(getattr(b, 'source_name', '') == '弑魂之炽减伤' for b in u.buffs):
                    u.buffs.append(TimedBuff(source_id='phainon_shihun',
                                             attributes={'DMG_REDUCTION': 75.0},
                                             remaining_turns=-1, source_name='弑魂之炽减伤'))
                u.extra['shihun_dr'] = 0.75
                state.log.append(f'  弑魂焚诏: 毁伤+{n_enemies} 弑魂之炽+{stacks}层, 敌立即行动, 减伤75%')
            if skill_key == 'skill_shenshen':
                # 死星天裁: 耗毁伤≤4点, 每点4次45%ATK弹射
                spent = min(u.extra.get('huishang', 0), 4)
                u.extra['huishang'] = u.extra.get('huishang', 0) - spent
                stats = _build_effective_stats(u, state)
                total = 0.0
                # v6.6b P2-2: 无存活敌人跳过; 弹射每段重选存活目标（同 v6.2.1b P1-1 口径）
                alive = state.alive_enemies()
                for _ in range(spent * 4):
                    alive_now = [e for e in alive if e.HP > 0]
                    if not alive_now:
                        break
                    t = random.choice(alive_now)
                    d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 45.0,
                                         'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                         skill_type='skill',
                                         crit_mode='expected')
                    _commit_enemy_damage(state, u, t, d.final_damage)
                    total += d.final_damage
                if spent >= 4 and alive:
                    for t in alive:
                        if t.HP <= 0:
                            continue
                        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 450.0 / len(alive),
                                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                             skill_type='skill',
                                             crit_mode='expected')
                        _commit_enemy_damage(state, u, t, d.final_damage)
                        total += d.final_damage
                if u.eidolon_rank >= 6 and spent > 0:
                    highest = max(state.alive_enemies(), key=lambda x: x.HP, default=None)
                    if highest is not None:
                        true_dmg = total * 0.36
                        _commit_enemy_damage(state, u, highest, true_dmg,
                                             damage_type='true_damage',
                                             record_cipher=False)
                        total += true_dmg
                        state.log.append(f'  白厄E6: 死星天裁后真伤{true_dmg:.0f}')
                if u.eidolon_rank >= 2 and spent >= 4:
                    state.extra.setdefault('extra_turns', []).append((u, 'extra'))
                    state.log.append('  白厄E2: 消耗4点毁伤获得额外回合')
                u.total_damage_dealt += total
                state.log.append(f'  死星天裁: {total:.0f} (耗毁伤{spent})')
                _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 0倍率技能补气氛
    # 白厄天赋: 被点名+1火种（队友点名+暴伤30% 3回合）
    # v6.8.1: 判定白厄是否为技能目标（此前任意队友普攻/战技都触发）;
      # v6.8.2: 覆盖 all_allies_but_self 与全队终结技（txt「成为技能目标时」）;
    # 暴伤改 TimedBuff 刷新（此前裸改 base_stats 永久叠加）
    if u.char.id != 'phainon' and skill_key in ('skill', 'basic_attack', 'ultimate') \
            and getattr(skill, 'target', '') in ('single_ally', 'ally', 'all_allies',
                                                  'all_allies_but_self', 'all'):
        ph = next((x for x in state.units if x.char.id == 'phainon' and x.is_alive), None)
        if ph:
            hit_ph = True
            if getattr(skill, 'target', '') in ('single_ally', 'ally'):
                ally = _pick_single_ally_target(state, u)
                hit_ph = ally is not None and ally.char.id == 'phainon'
            if hit_ph:
                _phainon_gain_huozhong(state, ph, 1)
                ph.buffs = [b for b in ph.buffs
                            if getattr(b, 'param_id', '') != 'phainon_cd_buff']
                ph.buffs.append(TimedBuff(source_id='phainon', attributes={'CRIT_DMG': 30.0},
                                          remaining_turns=3, param_id='phainon_cd_buff',
                                          source_name='被点名'))
                state.log.append(f'  白厄天赋: 被{u.char.name}点名 → +1火种 +暴伤30%(3回合)')



    # 治疗处理
    for eff in skill.effects:
        if eff.type != 'heal':
            continue
        # 治疗量: HP%/ATK% + 固定值。paramId 数字编码 "hpPct|flat"，或 HEAL_REGISTRY 命名注册
        named = HEAL_REGISTRY.get(eff.param_id or '')
        if named:
            heal_pct = named["hp_pct"]
            heal_flat = named["flat"]
            heal_stat = named.get("stat", "HP")  # v5.3: 灵砂治疗为 ATK 基数
        else:
            parts = eff.param_id.split('|') if eff.param_id else []
            heal_pct = float(parts[0]) if len(parts) > 0 else 0
            heal_flat = float(parts[1]) if len(parts) > 1 else 0
            heal_stat = "HP"
        healing_stats = _build_effective_stats(u, state)
        heal_base = healing_stats.ATK if heal_stat == "ATK" else healing_stats.HP
        # v5.3: 治疗量加成消费（灵砂行迹2 治疗量提高 = BE×10% 上限20%）
        heal_bonus = healing_stats.HEAL_BONUS
        heal_amt = ((heal_base * (heal_pct / 100) + heal_flat)
                    * (1.0 + heal_bonus) * skill_level_factor)
        # 风堇行迹1·暴风停歇: 每超1点SPD→治疗量+1%(上限200点)
        if u.char.id == 'fengjin' and u.base_stats.SPD > 200:
            heal_amt *= 1.0 + min(u.base_stats.SPD - 200, 200) / 100.0
        if eff.target == 'memsprite' and u.memsprite_unit and u.memsprite_unit.is_alive:
            # 治疗忆灵（阿格莱雅战技：为衣匠回复50%衣匠生命上限; 风堇: 按风堇生命上限, v5.7）
            ms = u.memsprite_unit
            base = u.base_stats.HP if u.char.id == 'fengjin' else ms.max_hp
            heal_val = ((base * (heal_pct / 100) + heal_flat)
                        * skill_level_factor)
            ms.current_hp = min(ms.max_hp, ms.current_hp + heal_val)
            state.log.append(f'  治疗忆灵: {ms.data.name}+{heal_val:.0f}HP')
            continue
        if eff.target in ('all_allies', 'all_allies_but_memsprite'):
            # 全队治疗: 角色 + 忆灵（忆灵治疗也计入→收容的暗潮转化）
            tgt_list = [eu for eu in state.units if eu.is_alive]
            if eff.target == 'all_allies':
                tgt_list += [ms for ms in state.memsprites if ms.is_alive]
        elif eff.target == 'single_ally':
            # 单目标治疗（藿藿战技主目标）: 主C惯例
            main = u.extra.get('lc_last_skill_target')
            if main not in state.units or not main.is_alive:
                main = _pick_single_ally_target(state, u)
            tgt_list = [main] if main else [u]
        elif eff.target == 'blast':
            # 相邻治疗（藿藿战技相邻目标）: 主C + 相邻1人(简化: 施法者之外的队友)
            main = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
            if main:
                others = [x for x in state.units if x.is_alive and x != main and x != u]
                adj = others[0] if others else None
                tgt_list = [main] + ([adj] if adj else [])
            else:
                tgt_list = [u]
        else:
            tgt_list = [u]
        for t in tgt_list:
            amt = heal_amt
            # 风堇行迹2·阴云莞尔: 对HP≤50%目标治疗量+25%
            if u.char.id == 'fengjin' and t.current_hp <= t.max_hp * 0.50:
                amt = heal_amt * 1.25
            # 藿藿E4·坐卧不离: 目标低血治疗加成(线性插值, 最多+80%)
            if u.char.id == 'huohuo' and u.eidolon_rank >= 4:
                miss = 1.0 - t.current_hp / t.max_hp
                amt = heal_amt * (1.0 + 0.80 * miss)
            # 万敌行迹1·血祥罩衫: 受疗提高0.75%/每超4000点100生命（最多计入4000）— v5.7 门槛
            if hasattr(t, 'char') and hasattr(t.char, 'id') and t.char.id == 'mydei':
                excess_hundreds = min(max(0, t.max_hp - 4000), 4000) // 100
                amt *= 1.0 + 0.0075 * excess_hundreds
            t.current_hp = min(t.max_hp, t.current_hp + amt)
            # 万敌E2: 血仇期间接受治疗→40%治疗转充能(累计40点)
            if hasattr(t, 'char') and hasattr(t.char, 'id') and t.char.id == 'mydei' \
                    and t.eidolon_rank >= 2 and t.extra.get('is_blood_debt'):
                if not t.extra.get('charge_locked'):
                    converted = min(40 - t.extra.get('e2_heal_converted', 0),
                                    amt * 0.40)
                    if converted > 0:
                        t.extra['e2_heal_converted'] = t.extra.get('e2_heal_converted', 0) + converted
                        t.extra['mydei_charge'] = min(200, t.extra.get('mydei_charge', 0) + converted)
                        state.log.append(f'  E2: 治疗转充能+{converted:.0f} → {t.extra["mydei_charge"]:.0f}/200')
        ms = u.memsprite_unit
        if ms and hasattr(ms, 'cumulative_healing'):
            heal_val = heal_amt * len(tgt_list)
            # 献予「天空」之诗: 风堇持层时治疗计入小伊卡×1.72
            if u.char.id == 'fengjin' and u.extra.get('poem_tiankong', 0) > 0:
                heal_val *= 1.72
            ms.cumulative_healing = getattr(ms, 'cumulative_healing', 0) + heal_val
        state.log.append(f'  治疗: {heal_amt:.0f}×{len(tgt_list)}人')
        # v6.10.6 B1: 藿藿战技→藿藿自身获得禳命3回合（E1延长1回合; 此前错误挂在受疗者身上2回合）
        if u.char.id == 'huohuo' and skill_key == 'skill':
            _huohuo_ruming_gain(state, u, 3 + (1 if u.eidolon_rank >= 1 else 0))
        # Hook: 治疗事件 → 遐蝶收容的暗潮(xinrui转化)通过 on_heal 钩子触发
        state.hooks.trigger_all("on_heal", u=u, state=state,
                                 healer=u, targets=tgt_list, heal_amt=heal_amt)
        _fengjin_talent_heal_buff(state, u)
        # v5.4 光锥治疗事件（时节不居: 记录治疗量）
        state.extra['lc_last_heal_amt'] = heal_amt
        _process_lc_effects(u, state, "on_heal")

    # 技能效果→TimedBuff
    if not effects_pre_applied:
        _apply_skill_effects(u, state, skill, skill_key)

    # 藿藿终结技·尾巴·遣神役鬼: 队友回20%能量上限 + ATK buff 2回合
    # (行迹·控抗精通: 能量上限≥160的队友额外ATK+24% → 40→64)
    if u.char.id == 'huohuo' and skill_key == 'ultimate':
        for eu in state.units:
            if eu is u or not eu.is_alive:
                continue
            _gain_energy(eu, 0.20, state=state, percent=True)  # v5.7: 统一入口(迷迷充能bank)
            atk_val = 64.0 if (eu.char.max_energy or 0) >= 160 else 40.0
            eu.buffs.append(TimedBuff(source_id='huohuo', attributes={'ATK_PERCENT': atk_val},
                                      remaining_turns=2, source_name='藿藿终结技',
                                      param_id='huohuo_ult_atk'))
        state.log.append(f'  藿藿终结技: 队友回20%能量上限 + ATK+40/64% 2回合')

    if qianye_new_ult:
        overflow = u.extra.pop('qianye_overflow', 0.0)
        if overflow > 0:
            u.current_energy = min(u.char.max_energy, u.current_energy + overflow)
            state.log.append(f'  千冶·刃行迹1: 溢出能量{overflow:.0f}恢复')

    # 光锥特效触发
    lc_event = None
    if is_ultimate_action: lc_event = "on_ult"
    elif skill_key == "skill": lc_event = "on_skill"
    elif skill_key == "basic_attack": lc_event = "on_basic_attack"  # v5.0 P3
    elif skill_key == "elation_skill": lc_event = "on_elation_skill"  # v5.0.1 启航
    if lc_event:
        _process_lc_effects(u, state, lc_event)
    # v5.1: 遐蝶行迹1·西风的驻足 — 【乌黯】对应死龙在场时的强化战技。
    if u.char.id == 'xiadie' and skill_key == 'skill_dragon':
        u.extra['xiadie_flame_stack'] = min(6, u.extra.get('xiadie_flame_stack', 0) + 1)
        state.log.append(f'  西风的驻足: 焰息叠层+1 → {u.extra["xiadie_flame_stack"]}/6')
    # v5.2 问题3c: 星体差分机——首次攻击后移除首击CR加成（本次攻击已吃到）
    if u.extra.get('diff_machine_cr'):
        u.base_stats.CRIT_RATE = max(0.0, u.base_stats.CRIT_RATE - u.extra.pop('diff_machine_cr'))
        state.log.append('  星体差分机: 首击暴击加成已消耗')

    # Hook: 技能使用后
    if not u.extra.get('acheron_talent_triggered'):
        for enemy in state.enemies:
            negative_statuses = [
                status for status in enemy.statuses
                if status.category in ('debuff', 'dot', 'control')
            ]
            if negative_statuses != debuffs_before.get(id(enemy), []):
                state.hooks.trigger_all('on_debuff_applied', u=u, state=state,
                                        target=enemy)
                break
    # v6.10.3 P1-1: 赛飞儿天赋FUA/E2易伤/E4附加——我方攻击命中老主顾后触发（修正反向）
    _cipher_attack_aftermath(state, u, skill_key)
    _cipher_ensure_laozhuke(state)
    # v6.10.3 P1-3: 爻光天赋【大吉大利】——持有好活当赏时我方攻击后额外欢愉伤害
    _yaoguang_dajidali(state, u, skill_key,
                        spent_skill_points=spent_skill_points)
    # v6.10.3 P1-4: 开拓者·欢愉战技/天赋/行迹2内联接线
    _tb_skill_aftermath(state, u, skill_key)
    state.hooks.trigger(u.char.id, "on_after_skill", u=u, state=state,
                        skill_key=skill_key, skill=skill, total_dmg=total_dmg)
    # 本次技能键已在伤害事件前写入 u.extra；state.extra 保留兼容读取。


# ---- 角色技能钩子注册表 ----
# 格式: {char_id: [hook_fn(unit, state, skill_key), ...]}
# 在 simulate() 中按队伍组成动态注册

def _register_elation_skill_hooks(skill_hooks):
    """注册欢愉命途角色的技能钩子（由 simulate 在检测到欢愉队时调用）"""

    def _laugh_gen(u: SimUnit, state: SimState, skill_key: str):
        """笑点生成"""
        # v6.10.3 P1-4: 开拓者天赋"施放攻击后"覆盖欢愉技
        is_tb_elation = (u.char.id == 'trailblazer_elation' and skill_key == 'elation_skill')
        if u.char.path != "欢愉" or (skill_key not in ("basic_attack", "skill") and not is_tb_elation):
            return
        if u.char.id == 'yinlang':
            # TXT仅战技明确获得5笑点；普攻不产笑点。
            laugh = 5 if skill_key == 'skill' else 0
            if laugh <= 0:
                return
            state.laugh_points += laugh
            elation = state.extra.get('_elation')
            if elation:
                elation.gain_hidden_score(state, u, laugh)
            else:
                u.hidden_score = min(300.0, u.hidden_score + laugh)
            return
        if u.char.id == 'yaoguang':
            state.laugh_points += 3
            return
        bonus = {"yaoguang": 3, "trailblazer_elation": 3}.get(u.char.id, 0)
        if bonus and u.char.id == "trailblazer_elation":
            _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口
        if state.elation_state.get_good_show_total(u.char.id) > 0:
            bonus += 3
        laugh = 3 + bonus
        state.laugh_points += laugh

    def _yaoguang_field(u: SimUnit, state: SimState, skill_key: str):
        if u.char.id == "yaoguang" and skill_key == "skill":
            _yaoguang_open_field(state, u)

    def _huohuo_energy(u: SimUnit, state: SimState, skill_key: str):
        if u.char.id == "huohuo" and skill_key == "basic_attack":
            _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口

    def _yaoguang_energy(u: SimUnit, state: SimState, skill_key: str):
        if u.char.id == "yaoguang" and skill_key == "basic_attack":
            _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口

    def _silver_invincible_elation(u: SimUnit, state: SimState, skill_key: str):
        """银狼无敌玩家欢愉技：6×90%弹射"""
        if u.char.id == "yinlang" and u.invincible_active and skill_key == "elation_skill":
            total = 0.0
            s = _build_effective_stats(u, state)
            for _ in range(6):
                alive = state.alive_enemies()
                if not alive:
                    break
                t = random.choice(alive)
                laugh_n = u.hidden_score
                if u.eidolon_rank >= 4:
                    laugh_n += state.laugh_points * 5.0
                d = calculate_damage(s, _enemy_for_damage(t), 0, 90.0, "elation",
                                     u.char.element, 80, s.CRIT_RATE >= 0.5,
                                     laugh_n=laugh_n, crit_mode="expected")
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
            u.total_damage_dealt += total
            skill = u.char.skills.get("elation_skill")
            u.damage_log.append(((skill.name + "(无敌)") if skill else "elation", total, "elation_inv"))
            state.log.append(f'[{state.current_av:6.0f}AV] {u.char.name} 欢愉技(无敌): {total:.0f}')
            u.extra['yinlang_blindbox_prob'] = 1.0
            _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 提前return欢愉技补气氛
            return True

    def _evanescia_skill_laugh(u: SimUnit, state: SimState, skill_key: str):
        """绯英战技: 额外+10笑点; 欢愉技: 额外+5好活当赏（v6.7b 补, txt 欢愉技）"""
        if u.char.id == "evanescia" and skill_key == "skill":
            state.laugh_points += 10
            state.log.append('  绯英战技: 额外+10笑点')
        elif u.char.id == "evanescia" and skill_key == "elation_skill":
            elation = state.extra.get('_elation')
            if elation:
                elation.grant_good_show(state, 'evanescia', 5.0,
                                        source='evanescia_elation_skill')
                state.log.append('  绯英欢愉技: 额外+5好活当赏')

    def _sparxie_skill_live(u: SimUnit, state: SimState, skill_key: str):
        """火花战技: 开启直播连线(下次普攻强化, 一次性) + 互动陷阱+1(上限20, v6.7)"""
        if u.char.id == "sparxie" and skill_key == "skill":
            u.extra['sparxie_live'] = True
            used = u.extra.get('sparxie_trap_uses', 0)
            u.extra['sparxie_trap_uses'] = min(20, used + 1)
            state.log.append(f'  火花战技: 直播连线开启(一次性) + 互动陷阱+1({min(20, used+1)}/20)')

    def _sparxie_elation_burst(u: SimUnit, state: SimState, skill_key: str):
        """火花欢愉技: 额外+2爆点（v6.7, 弹射段数见 _bounce_hits E6）"""
        if u.char.id == "sparxie" and skill_key == "elation_skill":
            state.extra['sparxie_burst_points'] = \
                state.extra.get('sparxie_burst_points', 0.0) + 2
            state.log.append('  火花欢愉技: +2爆点')

    for cid, hooks in [("yinlang", [_silver_invincible_elation, _laugh_gen]),
                       ("yaoguang", [_laugh_gen, _yaoguang_field, _yaoguang_energy]),
                       ("trailblazer_elation", [_laugh_gen]),
                       ("huohuo", []),
                       ("evanescia", [_laugh_gen, _evanescia_skill_laugh]),   # v6.7
                       ("sparxie", [_laugh_gen, _sparxie_skill_live, _sparxie_elation_burst])]:  # v6.7
        skill_hooks.setdefault(cid, []).extend(hooks)

def _register_v69_skill_hooks(skill_hooks):
    """v6.9 批1 技能钩子（伤害计算前执行）: 星期日战技/终结技、瓦尔特终结技、阮·梅战技/终结技"""

    from engine.core.combat_sim import (_sunday_skill as _sunday_skill_fn,
                                        _sunday_ult as _sunday_ult_fn,
                                        _welt_ult as _welt_ult_fn,
                                        _ruanmei_xianyin_apply, _ruanmei_field_apply)

    def _sunday_skill(u, state, skill_key):
        if u.char.id == "sunday" and skill_key == "skill":
            _sunday_skill_fn(state, u)

    def _sunday_ult(u, state, skill_key):
        if u.char.id == "sunday" and skill_key == "ultimate":
            _sunday_ult_fn(state, u)

    def _welt_ult(u, state, skill_key):
        if u.char.id == "welt" and skill_key == "ultimate":
            _welt_ult_fn(state, u)

    def _ruanmei_skill(u, state, skill_key):
        if u.char.id == "ruan_mei" and skill_key == "skill":
            _ruanmei_xianyin_apply(state, u)

    def _ruanmei_ult(u, state, skill_key):
        if u.char.id == "ruan_mei" and skill_key == "ultimate":
            _ruanmei_field_apply(state, u)
    # v6.9.1: 批1只注册批1角色, 避免全局业务函数签名污染（Codex P0-1 根因二）
    for cid, hooks in [("sunday", [_sunday_skill, _sunday_ult]),
                       ("welt", [_welt_ult]),
                       ("ruan_mei", [_ruanmei_skill, _ruanmei_ult])]:
        skill_hooks.setdefault(cid, []).extend(hooks)
    return


    for cid, hooks in [("sunday", [_sunday_skill, _sunday_ult]),
                       ("welt", [_welt_ult]),
                       ("ruan_mei", [_ruanmei_skill, _ruanmei_ult]),
                       ("robin", [_robin_skill, _robin_ult]),
                       ("busitu", [_busitu_skill, _busitu_ult])]:
        skill_hooks.setdefault(cid, []).extend(hooks)


def _register_v69b2_hooks(skill_hooks):
    """v6.9 批2 技能钩子: 知更鸟战技/终结技、不死途战技/终结技"""
    from engine.core.combat_sim import (_robin_skill as _robin_skill_fn,
                                        _robin_ult as _robin_ult_fn,
                                        _busitu_skill as _busitu_skill_fn,
                                        _busitu_ult as _busitu_ult_fn)

    def _robin_skill(u, state, skill_key):
        if u.char.id == "robin" and skill_key == "skill":
            _robin_skill_fn(state, u)

    def _robin_ult(u, state, skill_key):
        if u.char.id == "robin" and skill_key == "ultimate":
            _robin_ult_fn(state, u)

    def _busitu_skill(u, state, skill_key):
        if u.char.id == "busitu" and skill_key == "skill":
            from engine.core.combat_sim import _pick_single_ally_target
            target = state.alive_enemies()[0] if state.alive_enemies() else None
            _busitu_skill_fn(state, u, target)

    def _busitu_ult(u, state, skill_key):
        if u.char.id == "busitu" and skill_key == "ultimate":
            from engine.core.combat_sim import _pick_single_ally_target
            target = state.alive_enemies()[0] if state.alive_enemies() else None
            _busitu_ult_fn(state, u, target)
    # v6.9.1: 批2只注册批2角色, 千冶·刃由 _register_v69b3_hooks 注册（Codex P0-1 根因一）
    for cid, hooks in [("robin", [_robin_skill, _robin_ult]),
                       ("busitu", [_busitu_skill, _busitu_ult])]:
        skill_hooks.setdefault(cid, []).extend(hooks)
    return


    for cid, hooks in [("robin", [_robin_skill, _robin_ult]),
                       ("busitu", [_busitu_skill, _busitu_ult]),
                       ("qianye", [_qianye_skill_hook, _qianye_ult_hook])]:
        skill_hooks.setdefault(cid, []).extend(hooks)


def _register_v69b3_hooks(skill_hooks):
    """v6.9 批3 技能钩子: 千冶·刃战技(解放)/终结技"""
    from engine.core.combat_sim import (_qianye_skill as _qianye_skill_fn,
                                        _qianye_ult as _qianye_ult_fn)

    def _qianye_skill(u, state, skill_key):
        if u.char.id == "qianye" and skill_key == "skill":
            _qianye_skill_fn(state, u, skill_key)

    def _qianye_ult(u, state, skill_key):
        if u.char.id == "qianye" and skill_key == "ultimate":
            _qianye_ult_fn(state, u)

    skill_hooks.setdefault("qianye", []).extend([_qianye_skill, _qianye_ult])


def _register_v610_hooks(skill_hooks):
    """v6.10 黄泉技能钩子: 战技(+1残梦+集真赤)/终结技(三段)"""
    from engine.core.combat_sim import (_acheron_skill as _acheron_skill_fn,
                                        _acheron_ult as _acheron_ult_fn)

    def _acheron_skill(u, state, skill_key):
        if u.char.id == "acheron" and skill_key == "skill":
            _acheron_skill_fn(state, u)

    def _acheron_ult(u, state, skill_key):
        if u.char.id == "acheron" and skill_key == "ultimate":
            _acheron_ult_fn(state, u)

    skill_hooks.setdefault("acheron", []).extend([_acheron_skill, _acheron_ult])


def _register_v610b2_hooks(skill_hooks):
    """v6.10 飞霄技能钩子: 战技(立即FUA+行迹3)/终结技(六段)"""
    from engine.core.combat_sim import (_feixiao_skill as _feixiao_skill_fn,
                                        _feixiao_ult as _feixiao_ult_fn)

    def _feixiao_skill(u, state, skill_key):
        if u.char.id == "feixiao" and skill_key == "skill":
            _feixiao_skill_fn(state, u)

    def _feixiao_ult(u, state, skill_key):
        if u.char.id == "feixiao" and skill_key == "ultimate":
            _feixiao_ult_fn(state, u)

    skill_hooks.setdefault("feixiao", []).extend([_feixiao_skill, _feixiao_ult])


def _acheron_original_damage_multiplier(u, state) -> float:
    """奈落 is an independent original-damage multiplier, not DMG bonus."""
    if state is None or u.char.id != 'acheron':
        return 1.0
    nihility_allies = sum(
        1 for ally in state.units
        if ally.is_alive and ally is not u and ally.char.path == '虚无'
    )
    max_requirement = 1 if u.eidolon_rank >= 2 else 2
    if nihility_allies >= max_requirement:
        return 1.60
    if nihility_allies >= 1:
        return 1.15
    return 1.0


# ---- 有效面板（含欢愉 buff） ----

def _build_effective_stats(u: SimUnit, state=None) -> CombatStats:
    """按基础面板、临时 Buff、命途修饰构建当前有效面板。"""
    s = copy.deepcopy(u.base_stats)
    for b in u.buffs:
        for attr, val in b.attributes.items():
            _apply_stat(s, attr, val)
    # 玩家侧减益也可以携带数值属性（如减速 SPD_PERCENT）。它们必须和
    # TimedBuff 一样参与面板计算，直到在常规回合边界过期或被净化。
    for status in getattr(u, 'statuses', []):
        for attr, val in getattr(status, 'attributes', {}).items():
            _apply_stat(s, attr, val)
    # v6.10.3 P1-3: 爻光终结技抗穿已移至下方统一动态段（此前此处+0.20与新增+0.24双算）
    if state is not None and state.extra.get('_elation'):
        s = state.extra['_elation'].eff_stats(u, state, base_stats=s)
    if u.char.id == 'feixiao' and any(
            getattr(trace, 'hook_name', '') == 'feixiao_trace2'
            for trace in (u.char.traces or [])):
        s.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] = \
            s.CRIT_DMG_BY_ATTACK_TYPE.get('follow_up', 0.0) + 0.36
    # v5.0 P3: 光锥条件修正（自身条件; 目标相关条件由 _lc_target_correct 在伤害循环处理）
    _apply_lc_condition_corrections(u, state, s)
    _apply_team_lc_stack_buffs(u, state, s)
    # v5.1: 长夜月E6 — 在场时我方全体全属性抗性穿透+20%
    if state is not None:
        cy6 = next((x for x in state.units
                    if x.char.id == 'changyeyue' and x.is_alive and x.eidolon_rank >= 6), None)
        if cy6:
            s.RES_PEN_ALL += 0.20
        # v6.7 绯英E6: 欢愉伤害增笑15% + 每100好活当赏额外增笑2%（最多计入1000点）
        ev6 = next((x for x in state.units
                    if x.char.id == 'evanescia' and x.is_alive and x.eidolon_rank >= 6), None)
        if ev6 is u:
            gs = state.elation_state.get_good_show_total('evanescia')
            s.LAUGH_BOOST += 0.15 + 0.02 * min(int(gs // 100), 10)
        # v6.6c: 海瑟音行迹3 EHR>60%每10%增伤15%上限90%（E2 对全队）
        hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
        if hs and (hs is u or hs.eidolon_rank >= 2):
            ehr = hs.base_stats.EFFECT_HIT_RATE
            if ehr > 0.60:
                s.DMG_BONUS_ALL += min(0.90, (ehr - 0.60) / 0.10 * 0.15)
        # v6.6c: 刻律德菈行迹1 ATK>2000 每多100点暴伤+18% 上限360%
        ce = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if ce is u and s.ATK > 2000:
            s.CRIT_DMG += min(3.60, (s.ATK - 2000) // 100 * 0.18)
        # v6.10.3 P1-2: 赛飞儿行迹3 天赋FUA暴伤+100%（动态, 仅追加攻击）
        cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
        if cp is u and any(getattr(t, 'hook_name', '') == 'cipher_trace1'
                           for t in (cp.char.traces or [])):
            cipher_spd = s.SPD + s._base_SPD * s.SPD_PERCENT
            if cipher_spd >= 170:
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.50)
            elif cipher_spd >= 140:
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.25)
        if cp is u and any(getattr(t, 'hook_name', '') == 'cipher_trace3'
                           for t in (cp.char.traces or [])):
            s.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] = \
                s.CRIT_DMG_BY_ATTACK_TYPE.get('follow_up', 0.0) + 1.00
        # v6.10.3 P1-3: 爻光行迹/星魂动态面板（此前行迹1暴伤/行迹3/E2/E6/终结技抗穿均缺失或错位）
        yao = next((x for x in state.units if x.char.id == 'yaoguang' and x.is_alive), None)
        if yao is not None:
            if state.yao_field_active:
                s.ELATION_LEVEL += state.extra.get('yaoguang_field_elation_bonus', 0.0)
            if u is yao and any(getattr(t, 'hook_name', '') == 'yaoguang_cd_and_sp'
                                for t in (yao.char.traces or [])):
                s.CRIT_DMG += 0.60  # 行迹1·神闲意满: 自身暴伤+60%
            if u is yao and any(getattr(t, 'hook_name', '') == 'yaoguang_spd_to_elation'
                                for t in (yao.char.traces or [])) and s.SPD >= 120:
                s.ELATION_LEVEL += 0.30 + min(s.SPD - 120.0, 200.0) * 0.01  # 行迹3·开屏有礼
            if yao.eidolon_rank >= 2 and state.yao_field_active:
                s.SPD_PERCENT += 0.12  # E2: 结界期间全队速度+12%
                s.ELATION_LEVEL += 0.16  # E2: 结界期间全队欢愉度+16%
            if yao.eidolon_rank >= 6:
                s.LAUGH_BOOST += 0.25  # E6: 我方全体欢愉伤害增笑25%
        if u.yao_res_pen_turns > 0:
            s.RES_PEN_ALL += 0.24  # 终结技: 全队全属性抗性穿透+24%（3回合）
            if yao is not None and yao.eidolon_rank >= 1:
                s.DEF_PEN_BY_TYPE['elation'] = \
                    s.DEF_PEN_BY_TYPE.get('elation', 0.0) + 0.20  # E1: 欢愉伤害无视20%防御
        # v6.10.3 P1-4: 开拓者·欢愉行迹1/行迹3动态面板 + 终结技CD buff（非欢愉路径消费）
        if u.char.id == 'trailblazer_elation':
            if any(getattr(t, 'hook_name', '') == 'trailblazer_cr_and_sp'
                   for t in (u.char.traces or [])):
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.15)  # 行迹1·跟你爆了: 自身暴击率+15%
            if any(getattr(t, 'hook_name', '') == 'trailblazer_atk_to_elation'
                   for t in (u.char.traces or [])) and s.ATK > 1000:
                s.ELATION_LEVEL += min(0.60, (s.ATK - 1000.0) // 200.0 * 0.10)  # 行迹3·快哉快哉
        if u.tb_cd_buff_turns > 0:
            s.CRIT_DMG += 0.50
        # v6.10.6 C: 花火行迹3·夜想曲——全队ATK+45%; 持战技CD buff者全抗穿+10%; E1 谜诡持有者ATK+40%
        spk = next((x for x in state.units if x.char.id == 'sparkle' and x.is_alive), None)
        if spk is not None:
            if any(getattr(t, 'hook_name', '') == 'sparkle_team_cd'
                   for t in (spk.char.traces or [])):
                s.ATK += s._base_ATK * 0.45  # 行迹3: 全队攻击力+45%
                if any(getattr(b, 'param_id', '') == 'sparkle_cd_buff' for b in u.buffs):
                    s.RES_PEN_ALL += 0.10
            if spk.eidolon_rank >= 1 and any(getattr(b, 'param_id', '') == 'sparkle_mystery'
                                             for b in u.buffs):
                s.ATK += s._base_ATK * 0.40
        # 刻律德菈军功/爵位与星魂：全部按当前持有者动态消费，避免换目标后残留。
        if ce:
            cery_target = _cerydra_jungong_target(state, ce)
            if u is cery_target:
                if ce.eidolon_rank >= 1:
                    s.DEF_PEN += 0.16
                if u.extra.get('cerydra_juewei'):
                    s.DEF_PEN += 0.20
                if ce.eidolon_rank >= 2:
                    s.DMG_BONUS_ALL += 0.40
                if ce.eidolon_rank >= 6:
                    s.RES_PEN_ALL += 0.20
            elif u is ce and cery_target is not None:
                if ce.eidolon_rank >= 2:
                    s.DMG_BONUS_ALL += 1.60
                if ce.eidolon_rank >= 6:
                    s.RES_PEN_ALL += 0.20
        # 千冶·刃E2: 我方终结技视为追加攻击，并使全队追加攻击伤害+75%。
        qianye = next((x for x in state.units
                       if x.char.id == 'qianye' and x.is_alive), None)
        if qianye and qianye.eidolon_rank >= 2:
            if _qianye_wrath_active(qianye):
                s.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] = \
                    s.DMG_BONUS_BY_ATTACK_TYPE.get('follow_up', 0.0) + 0.75
        # 千冶·刃行迹3/E4: 结界期间团队增伤；虚无队友决定终结技分支。
        if qianye and _qianye_wrath_active(qianye) and any(
                getattr(t, 'hook_name', '') == 'qianye_trace3'
                for t in (qianye.char.traces or [])):
            s.DMG_BONUS_ALL += 0.50 + (0.50 if qianye.eidolon_rank >= 4 else 0.0)
            has_other_nihility = any(
                x is not qianye and x.is_alive and x.char.path == '虚无'
                for x in state.units)
            if has_other_nihility:
                s.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = \
                    s.DMG_BONUS_BY_SKILL_TYPE.get('ultimate', 0.0) + 0.75
            elif u is qianye:
                s.DMG_BONUS_ALL += 0.75
        if u.char.id == 'anaxa' and u.eidolon_rank >= 6:
            s.DAMAGE_MULTIPLIER = 1.30
        if u.char.id == 'phainon' and u.eidolon_rank >= 2 and u.extra.get('kasier'):
            s.RES_PEN['物理'] = s.RES_PEN.get('物理', 0.0) + 0.20
        # 丹恒·腾荒同袍相关效果：动态消费，换同袍后不会残留面板。
        dht = next((x for x in state.units
                    if x.char.id == 'dan_heng_permansor_terrae' and x.is_alive), None)
        if dht and u.extra.get('dht_tongpao'):
            s.ATK += dht.base_stats.ATK * 0.15
            if dht.eidolon_rank >= 4:
                s.DMG_REDUCTION += 0.20
            if dht.eidolon_rank >= 6:
                s.DEF_PEN += 0.12
        # 星期日E6: 仅其天赋层数允许暴击溢出按1%→2%转暴伤。
        sunday_e6 = any(x.char.id == 'sunday' and x.is_alive and x.eidolon_rank >= 6
                        for x in state.units)
        if sunday_e6 and any(getattr(b, 'param_id', '') == 'sunday_cr'
                             for b in getattr(u, 'buffs', [])) and s.CRIT_RATE > 1.0:
            s.CRIT_DMG += (s.CRIT_RATE - 1.0) * 2.0
            s.CRIT_RATE = 1.0
    return s



def _effective_spd(u: SimUnit, state=None) -> float:
    """有效速度: 含 SPD_PERCENT buff 折叠后的行动条速度。

    AV 计算统一走此函数（v5.0 接线: SPD_PERCENT 不再只影响伤害）。
    折叠语义: 静态 SPD_PERCENT 已由 attributes.py:362 折进 base_stats.SPD 且字段残留，
    故此处只叠加战斗 TimedBuff 新增的百分比（_apply_stat 战斗层只加字段不折叠）。
    注意: 忆灵速度走运行时字段 action_spd（v5.2 问题1: 不写 MemSprite 配置）。
    """
    s = _build_effective_stats(u, state)
    buff_pct = s.SPD_PERCENT - u.base_stats.SPD_PERCENT  # 战斗中新增部分
    return max(s.SPD + s._base_SPD * buff_pct, 1.0)


def _memsprite_action_speed(state, ms_unit) -> float:
    """返回忆灵下一次 Y 轴行动使用的速度。"""
    spd = ms_unit.action_spd
    if getattr(ms_unit.data, 'name', '') != '死龙':
        return spd
    xiadie = next((u for u in state.units
                   if u.char.id == 'xiadie' and u.is_alive), None)
    if not xiadie:
        return spd
    # 遐蝶行迹2: 遐蝶自身生命值不低于50%时，死龙速度提高40%。
    if xiadie.current_hp >= xiadie.max_hp * 0.5:
        spd *= 1.4
    if ms_unit.extra.get('xiadie_spd_boost'):
        spd *= 2.0
    return spd


def _apply_stat(stats: CombatStats, stat_type: str, value: float):
    """将单个属性值应用到 CombatStats（不依赖 character 上下文）"""
    pct_types = {"HP_percent": "HP_PERCENT", "ATK_percent": "ATK_PERCENT",
                 "DEF_percent": "DEF_PERCENT", "SPD_percent": "SPD_PERCENT",
                 "SPD_PERCENT": "SPD_PERCENT",
                 "CRIT_RATE": "CRIT_RATE", "CRIT_DMG": "CRIT_DMG",
                 "BREAK_EFFECT": "BREAK_EFFECT", "EFFECT_HIT_RATE": "EFFECT_HIT_RATE",
                 "EFFECT_RES": "EFFECT_RES", "ENERGY_REGEN": "ENERGY_REGEN",
                 "DMG_BONUS_ALL": "DMG_BONUS_ALL", "DMG_BONUS_DOT": "DMG_BONUS_DOT",
                 "HEAL_BONUS": "HEAL_BONUS", "SHIELD_BONUS": "SHIELD_BONUS",
                 "TOUGHNESS_EFFICIENCY": "TOUGHNESS_EFFICIENCY",
                 "ELATION_LEVEL": "ELATION_LEVEL", "LAUGH_BOOST": "LAUGH_BOOST",
                 "VULNERABILITY_APPLIED": "VULNERABILITY_APPLIED",
                 "DMG_REDUCTION": "DMG_REDUCTION", "DEF_PEN": "DEF_PEN",
                 "DEF_REDUCTION": "DEF_REDUCTION", "RES_PEN_ALL": "RES_PEN_ALL",
                 "HP_PERCENT": "HP_PERCENT", "ATK_PERCENT": "ATK_PERCENT",
                 "DEF_PERCENT": "DEF_PERCENT", "SPD_PERCENT": "SPD_PERCENT"}
    field_name = pct_types.get(stat_type, stat_type)
    if stat_type in pct_types or field_name != stat_type:
        setattr(stats, field_name, getattr(stats, field_name, 0) + value / 100.0)
        # 百分比属性折叠进实际面板（用 _base_* 白值）— 否则 ATK/DEF/HP_PERCENT 只设动态属性无人消费
        # SPD_PERCENT 不在此处直接折叠；_effective_spd() 会以白值合并战斗中
        # 新增的百分比，供行动条和所有拉条统一使用。
        pct = value / 100.0
        if field_name == 'ATK_PERCENT':
            stats.ATK += getattr(stats, '_base_ATK', stats.ATK) * pct
        elif field_name == 'DEF_PERCENT':
            stats.DEF += getattr(stats, '_base_DEF', stats.DEF) * pct
        elif field_name == 'HP_PERCENT':
            stats.HP += getattr(stats, '_base_HP', stats.HP) * pct
    elif stat_type.startswith("DMG_BONUS_ATK_"):
        # v5.6: 按攻击类别增伤（火舞·仅追加攻击）, 须在 DMG_BONUS_ 前缀之前
        key = stat_type[len("DMG_BONUS_ATK_"):].lower()
        stats.DMG_BONUS_BY_ATTACK_TYPE[key] = stats.DMG_BONUS_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("CRIT_DMG_ATK_"):
        # v5.6: 按攻击类别暴伤（都蓝王朝·5层FUA暴伤+25%）
        key = stat_type[len("CRIT_DMG_ATK_"):].lower()
        stats.CRIT_DMG_BY_ATTACK_TYPE[key] = stats.CRIT_DMG_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DMG_BONUS_") and stat_type != "DMG_BONUS_ALL" and stat_type != "DMG_BONUS_DOT":
        # v5.4 元素增伤（中文键 DMG_BONUS_虚数 等）优先于技能类型增伤
        if stat_type[len("DMG_BONUS_"):] in ("物理", "火", "冰", "雷", "风", "量子", "虚数"):
            stats.DMG_BONUS[stat_type[len("DMG_BONUS_"):]] += value / 100.0
            return
        # DMG_BONUS_SKILL / DMG_BONUS_BASIC / DMG_BONUS_ULTIMATE
        key = stat_type[len("DMG_BONUS_"):].lower()
        stats.DMG_BONUS_BY_SKILL_TYPE[key] = stats.DMG_BONUS_BY_SKILL_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("VULNERABILITY_APPLIED_"):
        key = stat_type[len("VULNERABILITY_APPLIED_"):].lower()
        stats.VULNERABILITY_APPLIED_BY_TYPE[key] = stats.VULNERABILITY_APPLIED_BY_TYPE.get(key, 0) + value / 100.0
    elif stat_type == "DEF_PEN_MEMSPRITE":
        stats.DEF_PEN_MEMSPRITE += value / 100.0
    elif stat_type == "_base_SPD":
        stats._base_SPD += value
        stats.SPD += value
    elif stat_type.startswith("DEF_PEN_SKILL_"):
        # v5.6: 按技能类型无视防御（流光·仅终结技）, 须在 DEF_PEN_ 前缀之前
        key = stat_type[len("DEF_PEN_SKILL_"):].lower()
        stats.DEF_PEN_BY_SKILL_TYPE[key] = stats.DEF_PEN_BY_SKILL_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DEF_PEN_ATK_"):
        key = stat_type[len("DEF_PEN_ATK_"):].lower()
        stats.DEF_PEN_BY_ATTACK_TYPE[key] = stats.DEF_PEN_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DEF_PEN_"):
        key = stat_type[len("DEF_PEN_"):].lower()
        stats.DEF_PEN_BY_TYPE[key] = stats.DEF_PEN_BY_TYPE.get(key, 0) + value / 100.0
    else:
        # 含 DMG_BONUS_{ELEMENT} 等
        for elem in ["物理", "火", "冰", "雷", "风", "量子", "虚数"]:
            if stat_type == f"DMG_BONUS_{elem}":
                stats.DMG_BONUS[elem] = stats.DMG_BONUS.get(elem, 0) + value / 100.0
                return
        # fallback: RES_PEN 等
        if stat_type.startswith("RES_PEN_"):
            elem = stat_type[len("RES_PEN_"):]
            if elem != "ALL":
                stats.RES_PEN[elem] = stats.RES_PEN.get(elem, 0) + value / 100.0


# ---- 弱点击破 ----

# 元素击破→DOT系数 (倍率 = 系数 × 击破基础值)
BREAK_DOT_MULTIPLIERS = {
    "物理": {"name": "裂伤", "multiplier": 1.0, "type": "dot"},
    "火":   {"name": "灼烧", "multiplier": 1.0, "type": "dot"},
    "冰":   {"name": "冻结", "multiplier": 0.0, "type": "freeze"},
    "雷":   {"name": "触电", "multiplier": 1.0, "type": "dot"},
    "风":   {"name": "风化", "multiplier": 1.0, "type": "dot_wind"},
    "量子": {"name": "纠缠", "multiplier": 0.0, "type": "entangle"},
    "虚数": {"name": "禁锢", "multiplier": 0.0, "type": "imprison"},
}

def _apply_break_debuff(enemy, element: str, attacker, state=None):
    """击破时对敌人施加属性异常状态（state 传入时对 DOT 类写攻击者面板快照, 供敌方回合跳伤）
    v5.6 说明: 击破异常不做 EHR 检定——实机弱点击破时异常必中（免疫除外）"""
    info = BREAK_DOT_MULTIPLIERS.get(element, {})
    if not info:
        return
    enemy.break_element = element
    enemy.break_debuff_name = info["name"]
    enemy.break_debuff_turns = 2  # 异常持续2回合
    category = 'dot' if info["type"] in ('dot', 'dot_wind') else (
        'control' if info["type"] in ('freeze', 'imprison') else 'debuff'
    )
    status = EnemyStatus(
        id=f'break:{element}', name=info["name"], category=category,
        source=getattr(getattr(attacker, 'char', None), 'id', ''),
        remaining_turns=2, removable=True,
    )
    # DOT 快照: 击破时定格攻击者有效面板（数值确定性, 攻击者死亡/换人/buff衰减不影响）
    if state is not None and hasattr(attacker, 'base_stats'):
        status.attributes['dot_snapshot'] = copy.deepcopy(_build_effective_stats(attacker, state))
        status.attributes['dot_element'] = element
        status.attributes['dot_multiplier'] = info["multiplier"] * 100.0
    enemy.add_status(status)
    if info["type"] == "freeze":
        # 冰冻结：仅标记，50%推条在敌人行动时结算（turn_start处理）
        pass
    elif info["type"] == "imprison":
        # 虚数禁锢：减速20%（记录被减部分, 倒计时结束恢复）
        enemy.SPD += enemy.extra.pop('imprison_speed_reduced', 0.0)  # 还原残留记录, 防重复施加叠加
        enemy.extra['imprison_speed_reduced'] = enemy.SPD * 0.20
        enemy.SPD *= 0.80


# ---- 波次 ----

def _apply_firefly_tech_wave(state, u):
    """v6.3.0 流萤秘技·Δ指令-焦土陨击: 每波次开始时全敌火弱点2回合 + 200%ATK火伤 + 削韧20
    （流萤.txt 秘技: 每个波次开始时为敌方全体添加火属性弱点, 此后造成200%ATK伤害）"""
    from engine.models.enemy import EnemyStatus
    alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0] or list(state.enemies)
    for e in alive:
        existing = next((s for s in e.statuses if s.id == 'firefly_fire_weakness'), None)
        if existing:
            existing.remaining_turns = max(existing.remaining_turns, 2)
        else:
            # v6.3.0b P1-3: 同步改火抗（此前只挂状态→削韧门控按火抗判定丢失, 到期无法恢复快照）
            old_res = e.get_res('火')
            e.element_res['火'] = min(old_res, -0.2)
            e.add_status(EnemyStatus(id='firefly_fire_weakness', name='火弱点', category='debuff',
                                     source='firefly', remaining_turns=2,
                                     attributes={'weakness_element': '火', 'weakness_old_res': old_res}))
    stats = _build_effective_stats(u, state)
    for e in alive:
        d = calculate_damage(stats, e, stats.ATK, 200.0, 'direct', '火', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
        _apply_toughness_damage(state, u, e, 20.0, '火', 'technique', stats)
    state.log.append(f'[秘技·焦土陨击] 第{state.extra.get("wave", 1)}波: 全敌火弱点2回合 + 200%ATK火伤')


def _respawn_wave(state):
    """全场敌人死亡后重生（保留 blueprint 在 state.extra 中; v6.5 异构敌人按模板列表逐只重建）"""
    bps = state.extra.get('enemy_blueprints')
    bp = state.extra.get('enemy_blueprint')
    n = state.extra.get('num_enemies', 3)
    if not bp and not bps:
        return
    state.extra['wave'] = state.extra.get('wave', 1) + 1
    state.enemies = []
    navs = state.extra.get('navs', {})
    if bps:
        pairs = [(tpl, i) for i, tpl in enumerate(bps)]
    else:
        pairs = [(bp, i) for i in range(n)]
    for tpl, i in pairs:
        # deepcopy: 浅拷贝会让同波敌人共享 extra/statuses（av_delayed 互相污染）
        e = copy.deepcopy(tpl)
        w = state.extra['wave']
        e.id = f'{tpl.id}_w{w}_{i}'
        state.enemies.append(e)
        # 重置敌方行动条 AV（含 stamp）
        _set_av(state, navs, ('e', i), state.current_av + AV_PER_TURN / max(e.SPD, 1.0))
    _acheron_apply_entry_effects(state)
    # v6.10.3 P1-2: 赛飞儿行迹3 新波敌人重建易伤（对称维护）
    cipher = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cipher and any(getattr(t, 'hook_name', '') == 'cipher_trace3'
                      for t in (cipher.char.traces or [])):
        _cipher_trace3_apply_vuln(state)
    if cipher:
        _cipher_pick_laozhuke(state, cipher)
    anaxa = next((x for x in state.units if x.char.id == 'anaxa'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if anaxa:
        _anaxa_apply_entry_effects(state, anaxa)
    # v6.3.0 流萤秘技: 每波次重新施加火弱点+伤害（秘技生效时）
    if state.extra.get('firefly_tech_active'):
        ff = next((x for x in state.units if x.char.id == 'firefly' and x.is_alive), None)
        if ff:
            _apply_firefly_tech_wave(state, ff)
    # v6.7 姬子·启行秘技: 每个波次开始时立即施放1次战技（拓星巡航, 进战）
    if state.extra.get('hn_tech_active'):
        hn = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
        if hn:
            _use_skill(hn, state, 'skill')
            state.log.append('  姬子·启行秘技: 本波次开始立即施放战技')
    # v6.9 知更鸟秘技: 每个波次开始回5能量（酣醉序曲, 非进战领域）
    if state.extra.get('robin_tech_active'):
        robin = next((x for x in state.units
                      if x.char.id == 'robin' and x.is_alive), None)
        if robin:
            from engine.core.combat_sim import _gain_energy
            _gain_energy(robin, 5.0, state=state)
            state.log.append('  知更鸟秘技: 本波次回5能量')
    # v6.10 飞霄秘技: 每波200%ATK必暴风伤(每多1敌+100%上限1000%)+1飞黄（岚身, 进战）
    if state.extra.get('feixiao_tech_active'):
        feixiao = next((x for x in state.units
                        if x.char.id == 'feixiao' and x.is_alive), None)
        if feixiao:
            from engine.core.combat_sim import (_build_effective_stats,
                                                calculate_damage,
                                                _commit_enemy_damage)
            alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
            if alive:
                stats = _build_effective_stats(feixiao, state)
                scale = 200.0 + min(len(alive) - 1, 8) * 100.0  # 每多1敌+100%, 上限1000%
                for e in alive:
                    d = calculate_damage(stats, e, stats.ATK, scale,
                                         'direct', '风', 80, True, crit_mode='boolean')
                    _commit_enemy_damage(state, feixiao, e, d.final_damage)
                    feixiao.total_damage_dealt += d.final_damage
                state.log.append(f'  飞霄秘技: 每波{scale:.0f}%ATK必暴({len(alive)}敌)')
            state.log.append(f'  飞霄秘技: 新波伤害结算，飞黄保持{feixiao.extra["feixiao_fly"]}/12')
    if state.extra.get('acheron_tech_active'):
        acheron = next((x for x in state.units if x.char.id == 'acheron' and x.is_alive), None)
        if acheron:
            from engine.core.combat_utils import _tech_acheron
            _tech_acheron(state, acheron, is_opener=True)
    # v6.8b: 白厄秘技: 每个波次开始全敌200%ATK物理伤（终结之始, 进战）
    if state.extra.get('phainon_tech_active'):
        phn = next((x for x in state.units
                    if x.char.id == 'phainon' and x.is_alive), None)
        if phn:
            _apply_phainon_tech_wave(state, phn)
    # v6.2.1: 至暗之谜期间新波敌人同样获得+30%易伤（Codex P2-7: 此前跨波口径漂移,
    # 退出时 _exit_darkness 对称回减）
    if any(getattr(u, 'is_darkness', False) and u.is_alive for u in state.units):
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
    # v6.3.0b P1-9: 银狼E2「入战受伤+20%」对新波敌人同样生效
    sw_e2 = next((x for x in state.units if x.char.id == 'silver_wolf'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if sw_e2:
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.20
    _silver_wolf_apply_entry_effects(state)
    if state.extra.get('yinlang_tech_active'):
        yinlang = next((x for x in state.units
                        if x.char.id == 'yinlang' and x.is_alive), None)
        elation = state.extra.get('_elation')
        if yinlang and elation:
            elation.silver_technique_wave(yinlang, state)
    # v6.7b: 大丽花E2「敌方目标入场时陷败谢+全抗-20%」对新波敌人同样生效
    dl_e2 = next((x for x in state.units if x.char.id == 'the_dahlia'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if dl_e2:
        for e in state.enemies:
            for elem in list(e.element_res.keys()):
                e.element_res[elem] = e.get_res(elem) - 0.20
            _apply_dahlia_baisie(dl_e2, state, e, turns=3)
        state.log.append('  大丽花E2: 新波敌人全抗-20% + 败谢(3回合)')
    # v6.8.1: 缇宝结界易伤+30% 对新波敌人重建（到期回减由 _tribbie_remove_field 对称）
    if state.extra.get('tribbie_field_turns', 0) > 0:
        trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
        if trib:
            for e in state.enemies:
                e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
            state.log.append('  缇宝结界: 新波敌人受伤+30%重建')
    # v6.8.1: 海瑟音结界（ATK-15%/DEF-25%/E4全抗-20%）对新波敌人重建
    if state.extra.get('hysilens_field_turns', 0) > 0:
        hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
        if hs:
            for e in state.enemies:
                e.extra['hysilens_field'] = True
                if hs.eidolon_rank >= 4:
                    for elem in list(e.element_res):
                        e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
                    e.extra['hysilens_e4_res'] = True
            state.log.append('  海瑟音结界: 新波敌人重建' + (' + E4全抗-20%' if hs.eidolon_rank >= 4 else ''))
    # v6.6b P1-2: 白厄变身期间新波敌人同样植入物理弱点
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('kasier') and x.is_alive), None)
    if ph:
        _phainon_implant_phys_weak(state)
    busitu = next((x for x in state.units
                   if x.char.id == 'busitu' and x.is_alive), None)
    if busitu:
        _busitu_rebind_bait(state, busitu)
    qianye = next((x for x in state.units
                   if x.char.id == 'qianye' and x.is_alive
                   and _qianye_wrath_active(x)), None)
    if qianye:
        _qianye_sync_wrath_enemy_effects(state, qianye)
    _lc_refresh_love_blank(state)
    w = state.extra.get('wave', 1)
    state.log.append(f'--- 第{w}波 ---')
    # 波次开始：触发所有角色光锥的 on_wave_start 特效
    for u in state.units:
        if u.is_alive:
            _process_lc_effects(u, state, "on_wave_start")
            state.hooks.trigger(u.char.id, "on_wave_start", u=u, state=state)


# ---- 通用AI ----

def _register_generic_ai(ai_registry: dict, units: list):
    """为队伍中的角色注册通用AI（非特定命途）"""
    for u in units:
        cid = u.char.id

        if cid == 'bronya':
            def bronya_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
                if unit.current_energy >= unit.char.max_energy:
                    _use_skill(unit, state, 'ultimate')
                elif state.skill_points > 0 and target:
                    _use_skill(unit, state, 'skill')
                    # 战技100%拉条（v7.1.0: 持特邀嘉宾时封锁——防永动机, 自拉条不受影响）
                    for i, eu in enumerate(state.units):
                        if eu == target and i in navs \
                                and not _guest_advance_blocked(state, unit, eu):
                            navs[i] = state.current_av
                            break
                    state.log.append(f'  拉条100% → {target.char.name}')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = bronya_ai

        elif cid == 'sparkle':
            def sparkle_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                # v6.10.6 E: 删除手工拉条——通用 action_advance（_apply_skill_effects）已处理50%,
                # 此前双重拉条实际接近100%; 目标选择也不再硬编码希儿（战技效果自行选目标）
                if unit.current_energy >= unit.char.max_energy:
                    _use_skill(unit, state, 'ultimate')
                elif state.skill_points > 0:
                    _use_skill(unit, state, 'skill')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = sparkle_ai

        elif cid == 'fu_xuan':
            def fuxuan_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                # 阵法倒计时
                field_turns = state.extra.get('fuxuan_field_turns', 0)
                if field_turns > 0:
                    state.extra['fuxuan_field_turns'] = field_turns - 1
                    # v6.3.0b P1-6: 阵法到期→鉴知HP上限快照回退（秘技施加, 角色+忆灵）
                    if state.extra['fuxuan_field_turns'] <= 0:
                        for eu in [x for x in state.units if x.is_alive] \
                                + [x for x in state.memsprites if x.is_alive]:
                            orig = eu.extra.pop('fuxuan_tech_orig_maxhp', None)
                            if orig is not None:
                                eu.max_hp = orig
                                eu.current_hp = min(orig, eu.current_hp)
                        state.log.append('  穷观阵到期: 鉴知HP上限回退')
                if unit.current_energy >= unit.char.max_energy:
                    _use_skill(unit, state, 'ultimate')
                elif state.skill_points > 0 and state.extra.get('fuxuan_field_turns', 0) <= 0:
                    state.extra['fuxuan_field_turns'] = 3  # 3回合阵法
                    _use_skill(unit, state, 'skill')
                    state.log.append('  符玄展开穷观阵(3回合)')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = fuxuan_ai

        elif cid == 'seele':
            def seele_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                """希儿AI: SP>0→战技，否则普攻。终结技由phase-1拦截，再现由击杀触发"""
                if state.skill_points > 0:
                    _use_skill(unit, state, 'skill')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = seele_ai

        elif cid == 'mydei':
            def mydei_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                """万敌AI: 血仇检查→能量满→终结技→战技/普攻"""
                _mydei_blood_debt_tick(unit, state, navs, uidx)
                if unit.current_energy >= unit.char.max_energy:
                    _use_skill(unit, state, 'ultimate')
                elif state.skill_points > 0:
                    _use_skill(unit, state, 'skill')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = mydei_ai

        elif cid == 'firefly':
            def firefly_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
                """流萤AI: 完全燃烧→强化普攻/强化战技; 非燃烧→终结技→战技→普攻"""
                if unit.extra.get('combustion'):
                    if state.skill_points > 0:
                        _use_skill(unit, state, 'skill_enhanced')
                    else:
                        _use_skill(unit, state, 'basic_attack_enhanced')
                elif unit.current_energy >= unit.char.max_energy:
                    _use_skill(unit, state, 'ultimate')
                elif state.skill_points > 0:
                    _use_skill(unit, state, 'skill')
                else:
                    _use_skill(unit, state, 'basic_attack')
            ai_registry[cid] = firefly_ai


def _mydei_blood_debt_tick(u, state, navs=None, uidx=None):
    """万敌天赋·以血还血: 血仇状态机

    - 充能≥100且非血仇 → 进入血仇(消耗100充能+回血+行动提前100%+生命上限50%+防御0)
    - 血仇中回合开始 → 自动施放弑王成王
    - 充能≥150且血仇 → 额外回合+自动弑神登神(消耗150充能)
    """
    charge = u.extra.get('mydei_charge', 0)
    is_debt = u.extra.get('is_blood_debt', False)
    u.extra['charge_locked'] = False  # 回合开始解锁充能积攒

    if not is_debt and charge >= 100:
        # 进入血仇: 消耗100充能 + 回血 + 行动提前100% + 生命上限+50% + 防御0
        # v6.2.1: 快照面板（Harness P1-3, 退出时对称还原, 此前退出不回减→永久漂移）
        u.extra['blood_debt_snapshot'] = {
            'max_hp': u.max_hp,
            'base_HP': u.base_stats.HP,
            'base_DEF': u.base_stats.DEF,
            'e2_defpen': 0.15 if u.eidolon_rank >= 2 else 0.0,
            'e4_critdmg': 0.30 if u.eidolon_rank >= 4 else 0.0,
        }
        u.extra['mydei_charge'] = charge - 100
        u.extra['is_blood_debt'] = True
        heal = u.max_hp * 0.20
        u.current_hp = min(u.max_hp, u.current_hp + heal)
        u.max_hp = u.max_hp * 1.50  # 生命上限+50%
        u.base_stats.HP = u.max_hp
        u.base_stats.DEF = 0
        # E2: 血仇期间无视防御15%
        if u.eidolon_rank >= 2:
            u.base_stats.DEF_PEN += 0.15
            state.log.append('  E2: 无视防御+15%')
        # E4: 血仇期间暴伤+30%
        if u.eidolon_rank >= 4:
            u.base_stats.CRIT_DMG += 0.30
            state.log.append('  E4: 暴击伤害+30%')
        if navs is not None and uidx is not None:
            _set_av(state, navs, uidx, state.current_av)  # 行动提前100%（v6.2.1b: 走统一入口补戳）
        state.log.append(f'  进入【血仇】: 生命上限+50%(={u.max_hp:.0f}), 防御=0, 行动提前100%, 回血{heal:.0f}')
        # 血仇回合开始自动弑王成王
        _use_skill(u, state, 'skill_enhanced')
        return

    if is_debt:
        # 血仇中: 回合开始自动弑王成王
        _use_skill(u, state, 'skill_enhanced')
        # 充能≥阈值(E6:100, 默认150) → 弑神登神入 X 轴队列（额外回合）
        need = u.extra.get('shenshen_cost', 150)
        if u.extra.get('mydei_charge', 0) >= need and not any(
                x is u and k == 'extra' for x, k in state.extra.get('extra_turns', [])):
            state.log.append(f'  充能≥{need}: 弑神登神入额外回合队列')
            state.extra.setdefault('extra_turns', []).append((u, 'extra'))


def _mydei_fatal_recovery(u, state):
    """万敌血仇致命攻击: 不会死亡。水与泥土(3次)不退出血仇；否则清空充能退出+回50%生命"""
    retain = u.extra.get('debt_retain_charges', 0)
    if retain > 0:
        # 水与泥土: 血仇状态受致命攻击不退出
        u.extra['debt_retain_charges'] = retain - 1
        u.current_hp = max(1, u.max_hp * 0.01)
        state.log.append(f'  水与泥土: 致命攻击不退出血仇({retain-1}/3剩余)')
        return
    # 退出: 清空充能 + 对称还原面板 + 回50%生命上限
    # v6.2.1: 先还原面板再回血（实机回血基于退仇后的生命上限）
    snap = u.extra.pop('blood_debt_snapshot', None)
    if snap:
        u.max_hp = snap['max_hp']
        u.base_stats.HP = snap['base_HP']
        u.base_stats.DEF = snap['base_DEF']
        u.base_stats.DEF_PEN -= snap['e2_defpen']
        u.base_stats.CRIT_DMG -= snap['e4_critdmg']
        state.log.append('  血仇面板还原(HP/DEF/无视防御/暴伤)')
    u.extra['is_blood_debt'] = False
    u.extra['mydei_charge'] = 0
    u.current_hp = u.max_hp * 0.50
    state.log.append(f'  致命攻击: 退出【血仇】, 回50%生命({u.current_hp:.0f})')


# ---- 行动条第四象限模型（X轴额外回合队列） ----

def _should_ult_now(u, state) -> bool:
    """判定单位是否可以释放终结技（满资源）"""
    if not isinstance(u, SimUnit) or not u.is_alive:
        return False
    cid = u.char.id
    if _hn_realm_blocks_ult(state, u):
        return False  # v7.2.0 裁决A: 拓星视界占境界→遐蝶/白厄永封终结技
    if u.char.path == "欢愉":
        return False  # 欢愉特殊资源，由各自 AI 管理
    if cid == 'firefly' and u.extra.get('combustion'):
        return False  # v5.3 完全燃烧期间无法施放终结技
    if cid == 'phainon':
        # v6.8.1: 白厄能量上限12仅为资源计数器, 普攻/战技回能即截满——按火种≥12判定,
        # 否则每次行动后都排入假终结技空转（刷日志/虚增行动计数）
        return u.extra.get('huozhong', 0) >= 12
    if cid == 'xiadie':
        return u.xinrui >= xiadie_xinrui_cap(u) and not (u.memsprite_unit and u.memsprite_unit.is_alive)
    if cid == 'xilian':
        # 用户确认实机: 召唤德谬歌后, 忆灵技(选择释放花与箭/此诗献予)类似终结技——
        # 只要追忆≥12 可随时选择释放; 首次终结技需追忆≥24 且单场1次
        if u.is_ripple:
            return u.zhuiyi >= 12
        return u.zhuiyi >= 24 and not u.extra.get('xilian_ult_used')
    return bool(u.char.max_energy and 0 < u.char.max_energy < 1000
                and u.current_energy >= u.char.max_energy)


def _enqueue_ult(state, u):
    """终结技入 X 轴队列"""
    # v5.0 P4: 冻结期间禁止额外回合（用户实机语义: 冻结下无法触发任何 X 轴回合）
    if any(getattr(st, 'name', '') == '冻结' for st in getattr(u, 'statuses', [])):
        state.log.append(f'  {u.char.name}冻结中: 无法释放终结技')
        return
    if any(x is u for x, k in state.extra.get('extra_turns', [])):
        return  # 已在队列
    state.extra.setdefault('extra_turns', []).append((u, 'ult'))
    state.log.append(f'  >>> 终结技入队: {u.char.name}')


def _sweep_ults(state):
    """行动后：全队满资源→追加 X 轴队尾（替代原 _check_ult_interrupts）"""
    guard = state.extra.setdefault('ult_chain_guard', 0)
    if guard >= 4:
        return
    for u in state.units:
        if _should_ult_now(u, state):
            _enqueue_ult(state, u)
            state.extra['ult_chain_guard'] = guard + 1
            return  # 一次一个


def _ai_ult_check(state, actor):
    """常规回合 phase-1：actor 自己/全队满资源→开大入队+hold actor"""
    guard = state.extra.setdefault('ult_chain_guard', 0)
    if guard >= 4:
        return False
    # actor 自己优先
    if _should_ult_now(actor, state):
        _enqueue_ult(state, actor)
        state.extra['ult_chain_guard'] = guard + 1
        return True
    # 全队（例2: 遐蝶待机时风堇开大）
    for u in state.units:
        if u is actor:
            continue
        if _should_ult_now(u, state):
            _enqueue_ult(state, u)
            state.extra['ult_chain_guard'] = guard + 1
            return True
    return False


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


def _ensure_marker_system(state):
    """行动条标记系统惰性创建入口（v6.3.0b P1-1: 秘技阶段与技能效果共用）。

    _marker_sys 原只在 spawn_marker 技能效果分支首次创建, 灵砂秘技执行点在其之前
    → sys 为 None 时浮元从未召唤。所有 spawn/advance 路径统一经本函数。
    """
    sys = state.extra.get('_marker_sys')
    if sys is None:
        sys = TimelineMarkerSystem()
        sys.action_handlers.update(MARKER_ACTIONS)
        sys.despawn_handlers.update(MARKER_DESPAWN)
        sys.spawn_handlers.update(MARKER_SPAWN)
        state.extra['_marker_sys'] = sys
    return sys


def _next_y_actor(state):
    """Y 轴选择：合并角色+SPD>0忆灵，按(av, -stamp)取最小（后到先动）"""
    navs = state.extra.get('navs', {})
    stamps = state.extra.get('av_stamp', {})
    candidates = []  # (av, -stamp, key, unit)
    for i, u in enumerate(state.units):
        if u.is_alive and i in navs:
            candidates.append((navs[i], -stamps.get(i, 0), i, u))
    if state.memsprites:
        for ms in state.memsprites:
            if not ms.is_alive or ms.action_spd <= 0:
                continue  # 界外忆灵(SPD=0)不走Y轴
            key = ('ms', id(ms))
            ms_av = ms.extra.get('next_av')
            if ms_av is None:
                ms_av = state.current_av + AV_PER_TURN / ms.action_spd
            candidates.append((ms_av, -stamps.get(key, 0), key, ms))
    # v5.3: 行动条标记（浮元/倒计时）
    if state.markers:
        for m in state.markers:
            if not m.is_alive:
                continue
            key = ('mk', id(m))
            m_av = m.extra.get('next_av')
            if m_av is None:
                m_av = state.current_av + AV_PER_TURN / max(m.action_spd, 1.0)
            candidates.append((m_av, -stamps.get(key, 0), key, m))
    # 敌方（Y轴行动条, 死亡敌人 HP<=0 天然过滤）
    for i, e in enumerate(state.enemies):
        if e.HP > 0:
            key = ('e', i)
            e_av = navs.get(key, e.av)
            candidates.append((e_av, -stamps.get(key, 0), key, e))
    if not candidates:
        return None, float('inf')
    best = min(candidates, key=lambda c: (c[0], c[1]))
    return best[3], best[0]


# ════════ 敌方攻击系统（Y轴行动条 + 选人 + 受击 + 死亡）════════

def _fengjin_talent_heal_buff(state, healer):
    """风堇天赋·疗愈世间的晨曦（风堇.txt）: 风堇或小伊卡提供治疗 →
    小伊卡造成的伤害+80%/层（2回合, 最多3层, 每层独立, 满层替换最旧）"""
    fengjin = None
    char = getattr(healer, 'char', None)
    if getattr(char, 'id', None) == 'fengjin':
        fengjin = healer
    elif getattr(healer, 'summoner_id', '') == 'fengjin':
        fengjin = next((x for x in state.units
                        if x.char.id == 'fengjin' and x.is_alive), None)
    if fengjin is None:
        return
    ms = fengjin.memsprite_unit
    if not ms or not ms.is_alive:
        return
    layers = [b for b in ms.buffs if getattr(b, 'param_id', '') == 'fengjin_talent_dmg']
    if len(layers) >= 3:
        ms.buffs.remove(layers.pop(0))
    ms.buffs.append(TimedBuff(source_id='fengjin', attributes={'DMG_BONUS_ALL': 80.0},
                              remaining_turns=2, source_name='疗愈世间的晨曦',
                              param_id='fengjin_talent_dmg'))
    state.log.append(f'  疗愈世间的晨曦: 小伊卡伤害+80% ({min(len(layers) + 1, 3)}/3)')


def _dispatch_changyeyue_hp_loss(state, affected):
    """长夜月天赋·今夜与我同行（用户提供长夜月.txt）:
    长夜月或忆灵「长夜」生命降低 → 双方暴伤+60% 2回合 + 长夜月+2忆质。
    每目标每次受击最多触发1次——按受击/扣血事件分发, 天然满足。"""
    for af, _lost in affected:
        cy = None
        char = getattr(af, 'char', None)
        cid = getattr(char, 'id', None)
        if cid == 'changyeyue':
            cy = af
        elif getattr(af, 'summoner_id', '') == 'changyeyue':
            cy = next((x for x in state.units
                       if x.char.id == 'changyeyue' and x.is_alive), None)
        if cy is None:
            continue
        from engine.core.relic_conditions import _apply_timed_buff
        _apply_timed_buff(cy, state, 'CRIT_DMG', 60.0, 2, source='天赋·今夜与我同行',
                          param_id='changyeyue_talent_cd')
        if cy.memsprite_unit and cy.memsprite_unit.is_alive:
            _apply_timed_buff(cy.memsprite_unit, state, 'CRIT_DMG', 60.0, 2,
                              source='天赋·今夜与我同行', param_id='changyeyue_talent_cd')
        from engine.systems.remembrance import _gain_yizhi
        _gain_yizhi(state, cy, 2)
        state.log.append(f'  今夜与我同行: 生命降低→忆质+2 ({cy.yizhi}), 双方暴伤+60%')


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


def _enemy_attack_stats(enemy) -> CombatStats:
    """敌方攻击方面板（只 ATK; 不暴击/无增伤穿透）
    v6.3.0b P1-7: 攻击力降低缺陷在此消费（此前只复制 enemy.ATK, atk_down 无消费端）
    v6.4: 敌方自增益 ATK_PERCENT 消费（与玩家降攻并存）
    v6.4b: ATK_PERCENT/ATK_percent 别名兼容（与 SPD 的 _enemy_status_sum 对称）
    v6.6c P1: 海瑟音结界敌ATK-15%"""
    s = CombatStats()
    s.ATK = enemy.ATK * (1.0 - enemy.status_attribute('atk_down')) \
        * (1.0 + _enemy_status_sum(enemy, 'ATK_PERCENT', 'ATK_percent')) \
        * (0.85 if enemy.extra.get('hysilens_field') else 1.0)
    s.DMG_REDUCTION = enemy.status_attribute('outgoing_dmg_reduction')
    s.CRIT_RATE = 0.0
    s.CRIT_DMG = 0.0
    return s


def _select_enemy_target(state, attacker=None):
    """敌方选人: 嘲讽加权随机（角色 taunt + 忆灵 max_taunt, 排除 is_backup 后援）
    v5.7: attacker=发起攻击的敌方单位时, 若其带【嘲讽】状态(我方施加)→强制选择施加者"""
    if attacker is not None:
        applier_id = next((st.attributes.get('applier')
                           for st in getattr(attacker, 'statuses', [])
                           if getattr(st, 'name', '') == '嘲讽'), None)
        if applier_id:
            forced = next((u for u in state.units
                           if u.char.id == applier_id and u.is_alive), None)
            if forced:
                return forced
    candidates = []
    for u in state.units:
        if u.is_alive:
            # v5.4 受击概率提高（落日时起舞×6/制胜的瞬间×3: taunt_mult 由 LC 设置）
            taunt = (u.char.taunt * u.extra.get('taunt_mult', 1.0)
                     * u.extra.get('qianye_taunt_mult', 1.0))
            candidates.append((taunt, u))
    for ms in state.memsprites:
        if ms.is_alive and not ms.is_backup:
            candidates.append((ms.data.max_taunt, ms))
    if not candidates:
        return None
    # v5.0 P4: 嘲讽状态强制选中（持【嘲讽】的存活单位）
    taunted = [c for _, c in candidates if isinstance(c, SimUnit)
               and any(getattr(st, 'name', '') == '嘲讽' for st in c.statuses)]
    if taunted:
        return taunted[0]
    weights = [w for w, _ in candidates]
    return random.choices([c for _, c in candidates], weights=weights, k=1)[0]


def _apply_enemy_taunt(state, applier, enemies, turns=2):
    """v5.7 嘲讽通用机制（万敌终结技; 后续受击概率.txt 8 角色复用）:
    对敌方施加【嘲讽】——被嘲讽敌人攻击时强制选择施加者（_select_enemy_target 消费）"""
    from engine.models.enemy import EnemyStatus
    for e in enemies:
        if e is None or getattr(e, 'HP', 0) <= 0:
            continue
        existing = next((s for s in e.statuses if s.id == 'taunt'), None)
        if existing:
            existing.remaining_turns = max(existing.remaining_turns, turns)
        else:
            e.statuses.append(EnemyStatus(id='taunt', name='嘲讽', category='debuff',
                                          source=applier.char.id, remaining_turns=turns,
                                          attributes={'applier': applier.char.id}))
        state.log.append(f'  嘲讽: {e.name or e.id} → 只能攻击{applier.char.name}({turns}回合)')


def _apply_player_status(state, target, status: PlayerStatus) -> bool:
    """敌方 debuff 施加管线（v5.0 P4）: 免疫检查 → EHR 命中检定 → 施加。

    免疫消费: 万敌血仇免疫控制（debt_control_immune）、符玄遁甲星舆次数
    （fuxuan_cc_resist_charges）。命中检定: calc_effect_probability(base_chance, 0, RES)。
    """
    from engine.core.combat_utils import calc_effect_probability
    if not isinstance(target, SimUnit) or not target.is_alive:
        return False
    # ① 免疫检查（仅控制类; 符玄次数是全队机制, 检查队内符玄）
    if status.category == 'control':
        if target.char.id == 'robin' and _robin_concert_active(target):
            state.log.append(f'  知更鸟协奏: 免疫{status.name}')
            return False
        if target.char.id == 'mydei' and target.extra.get('is_blood_debt') \
                and target.extra.get('debt_control_immune'):
            state.log.append(f'  万敌三十僭主: 免疫{status.name}')
            return False
        # v5.7: 长夜月至暗之谜/忆质≥16 免疫控制类负面状态（长夜月.txt 终结技/天赋）
        if target.char.id == 'changyeyue' and \
                (target.is_darkness or getattr(target, 'yizhi', 0) >= 16):
            state.log.append(f'  长夜月至暗/忆质≥16: 免疫{status.name}')
            return False
        fu = next((x for x in state.units
                   if x.char.id == 'fu_xuan' and x.is_alive), None)
        if fu and fu.extra.get('fuxuan_cc_resist_charges', 0) > 0:
            fu.extra['fuxuan_cc_resist_charges'] -= 1
            state.log.append(f'  符玄遁甲星舆: 抵抗{status.name}(剩余{fu.extra["fuxuan_cc_resist_charges"]}次)')
            return False
    # ② EHR 命中检定（目标 EFFECT_RES 含藿藿控抗/风堇抗性）
    res = _build_effective_stats(target, state).EFFECT_RES
    chance = calc_effect_probability(status.base_chance, 0.0, res)
    if chance < 1.0 and random.random() >= chance:
        state.log.append(f'  抵抗: {target.char.name} 抵抗{status.name} (命中{chance:.0%})')
        return False
    # ③ 施加（同 id 刷新）
    existing = next((s for s in target.statuses if s.id == status.id), None)
    if existing:
        existing.remaining_turns = max(existing.remaining_turns, status.remaining_turns)
        state.log.append(f'  {status.name}刷新: {target.char.name}')
    else:
        target.statuses.append(status)
        state.log.append(f'  {status.name} → {target.char.name} ({status.remaining_turns}回合)')
    state.hooks.trigger_all("on_enter_state", u=target, state=state,
                            status=status)  # v5.0 P8 事件
    return True


def _check_control_status(state, u):
    """Y 轴常规回合开始的状态判定（v5.0 P4）:
    - 冻结: 跳过本回合 + 推条5000 + 倒计时; 期间 X 轴禁止（_enqueue_ult/_exec_extra_turn 拦截）
    - 眩晕: 跳过本回合 + 倒计时
    - 减速型（纠缠/禁锢）: 由 statuses 挂的负 SPD 经 _effective_spd 生效, 不拦截
    返回 True 表示本回合被跳过。
    """
    if not u.statuses:
        return False
    for st in list(u.statuses):
        st.remaining_turns -= 1
        if st.category == 'control' and st.name in ('冻结', '眩晕'):
            if st.name == '冻结':
                navs = state.extra.get('navs', {})
                i = state.units.index(u)
                if i in navs:
                    navs[i] += 5000.0  # 冻结: 跳过回合 + 推条5000（用户实机语义）
            state.log.append(f'  {st.name}: {u.char.name}跳过本回合')
            # v6.2.1 注: turn_count 在跳过路径与 hold/resume 存在双计（Harness P3-6）,
            # 当前仅展示用; 实现"每回合"类机制前必须修正
            state.turn_count += 1
            state.extra['killed_this_action'] = 0
            state.extra['ult_chain_guard'] = 0
            _tick_buffs(u)
            state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
            if st.remaining_turns <= 0:  # 最后一次跳过后移除
                u.statuses.remove(st)
                state.hooks.trigger_all("on_exit_state", u=u, state=state, status=st)
            return True
        # 非跳过型状态在本回合的行动值重算后到期。否则 duration=1 的
        # 减速会保留到下一次行动值重算，额外影响一个完整回合。
        if st.remaining_turns <= 0:
            u.statuses.remove(st)
            state.hooks.trigger_all("on_exit_state", u=u, state=state, status=st)
    return False


def _enemy_attack(state, enemy):
    """敌方攻击: 选技能(priority最高) → 选目标 → 反向伤害结算 → 受击钩子"""
    if not enemy.attacks:
        state.log.append(f'  {enemy.name or enemy.id}行动(无攻击技能)')
        return None
    # v6.4: 蓄力条件选择——requires_buff 未满足的技能跳过
    # v6.4b P1-2: 全部受限且条件未满足 → no-op 回合（此前 or enemy.attacks 回退会释放受限技能,
    # 违反字段语义; AV 推进在 _begin_enemy_turn ⑤ 无条件执行, 无行动条死循环风险）
    eligible = [a for a in enemy.attacks
                if not a.get('requires_buff')
                # v6.4b: 只认 category=='buff' 的自增益（同名玩家 debuff 不得误判满足）
                or any(s.id == a['requires_buff']
                       and getattr(s, 'category', '') == 'buff'
                       for s in enemy.statuses)]
    if not eligible:
        state.log.append(f'  {enemy.name or enemy.id}无可用技能(全部受限且条件未满足): 跳过本回合')
        return None
    atk = max(eligible, key=lambda a: a.get('priority', 0))
    target = _select_enemy_target(state, attacker=enemy)
    if target is None:
        return None
    stats = _enemy_attack_stats(enemy)
    # v6.4: 敌方自增益（attacks[].self_buffs: 蓄力/狂暴等; 施加于自身, 复用 statuses 容器与倒计时）
    for sb in atk.get('self_buffs', []) or []:
        _enemy_apply_self_buff(state, enemy, sb)
    # v5.0 P6: 目标类型扩展（single/all_enemies/blast/bounce）
    t_type = atk.get('target_type', 'single_enemy')
    if t_type == 'all_enemies':
        targets = [u for u in state.units if u.is_alive] + \
                  [ms for ms in state.memsprites if ms.is_alive and not ms.is_backup]
    elif t_type == 'blast':
        others = [x for x in state.units if x.is_alive and x is not target] + \
                 [ms for ms in state.memsprites
                  if ms.is_alive and ms is not target and not ms.is_backup]
        targets = [target] + others[:1]
    elif t_type == 'bounce':
        hits = max(int(atk.get('hits', 3)), 1)
        targets = []
        for _ in range(hits):
            t2 = _select_enemy_target(state, attacker=enemy)
            if t2 is not None:
                targets.append(t2)
    else:
        targets = [target]

    # Each target owns a distinct defensive panel. Reusing the selected target's
    # result would make an area attack deal the same pre-mitigation damage to all.
    damages = []
    for hit_target in targets:
        t_stats = (_build_effective_stats(hit_target, state)
                   if isinstance(hit_target, SimUnit) else hit_target.base_stats)
        view = CharacterAsTarget(hit_target, t_stats)
        d = calculate_damage(stats, view, stats.ATK, atk.get('multiplier', 100.0),
                             atk.get('damage_type', 'direct'), atk.get('element', '物理'),
                             enemy.level, False)
        damages.append((hit_target, d.final_damage))

    # v6.2.1: 带默认值防 StopIteration（Harness P2-3: 弹射未命中初选目标时 damages 无该目标）
    primary_damage = next((dmg for hit_target, dmg in damages if hit_target is target), 0.0)
    target_name = '全体' if len(targets) > 1 else (
        getattr(target, 'name', None) or getattr(getattr(target, 'char', None), 'name', str(target))
    )
    state.log.append(f'[{state.current_av:6.0f}AV] {enemy.name or enemy.id} {atk.get("name", "攻击")}: {primary_damage:.0f} → {target_name}')
    state.hooks.trigger_all("on_enemy_attack", u=target, state=state,
                            attacker=enemy, target=target, damage=primary_damage)
    for hit_target, damage in damages:
        _distribute_damage(state, hit_target, damage, enemy)
    # 受击回能（v5.0 P2, 数据驱动: attacks[].energy_gain, 默认0）
    eg = atk.get('energy_gain', 0) or 0
    if eg > 0:
        for hit_target, _d in damages:
            if isinstance(hit_target, SimUnit):
                gained = _gain_energy(hit_target, eg, state=state)
                if gained > 0:
                    state.log.append(f'  受击回能: {hit_target.char.name} +{gained:.0f}')
    # v5.0 P4: 敌方攻击施加 debuff（数据驱动: attacks[].debuffs, 默认无）
    # v6.4: debuffs[].target 支持 "main"（仅初选目标）/ "all"（全部命中, 默认）
    # v6.4b P2: main 契约=初选目标本身, 不以"实际命中"为前提（弹射可能全程未命中初选目标）
    for deb in atk.get('debuffs', []) or []:
        if deb.get('target', 'all') == 'main':
            deb_targets = [(target, 0.0)] if target is not None and target.is_alive else []
        else:
            deb_targets = damages
        for hit_target, _d in deb_targets:
            _apply_player_status(state, hit_target, PlayerStatus(
                id=deb.get('id', f'enemy:{enemy.id}:{deb.get("name", "debuff")}'),
                name=deb.get('name', '负面状态'),
                category=deb.get('category', 'debuff'),
                source=enemy.id,
                remaining_turns=deb.get('duration', 2),
                attributes=dict(deb.get('attributes', {}) or {}),
                base_chance=deb.get('base_chance', 1.0),
            ))
    return primary_damage


def _apply_hit(state, target, amount, enemy):
    """受击结算: 盾吸收 → 扣血 → 万敌充能/E4 → 符玄自回血 → 钩子 → 死亡检查"""
    if amount <= 0 or not getattr(target, 'is_alive', True):
        return
    # v5.0 P5: 护盾优先吸收（盾吸收部分不算 HP 损失, 不触发 on_hp_loss/万敌充能）
    if getattr(target, 'shield', 0.0) > 0:
        absorbed = min(target.shield, amount)
        target.shield -= absorbed
        amount -= absorbed
        if absorbed > 0:
            t_name = getattr(getattr(target, 'char', None), 'name', '忆灵')
            state.log.append(f'  护盾吸收: {t_name} 盾-{absorbed:.0f}')
    # v6.10.6 A1: 扣血钳制——任何单位 HP 不得为负（不变量）; 实际损失用于后续管线
    before_hp = target.current_hp
    target.current_hp -= amount
    if target.current_hp < 0.0:
        target.current_hp = 0.0
    actual_lost = before_hp - target.current_hp
    if amount <= 0:
        return  # 盾完全吸收: 无实际 HP 损失, 不触发后续受击管线
    # 万敌受击充能（行迹1: 每损失1%生命=1充能; 生命上限每超4000点100→充能比例+2.5%, 最多计入4000; 弑神登神期间锁充能）
    if isinstance(target, SimUnit) and target.char.id == 'mydei' and not target.extra.get('charge_locked'):
        pct = actual_lost / max(target.max_hp, 1) * 100.0
        excess_hundreds = min(max(0, target.max_hp - 4000), 4000) // 100  # v5.7: 行迹1门槛
        charge_mult = 1.0 + 0.025 * excess_hundreds
        target.extra['mydei_charge'] = min(200, target.extra.get('mydei_charge', 0) + pct * charge_mult)
        state.log.append(f'  受击充能: +{pct * charge_mult:.1f} → {target.extra["mydei_charge"]:.0f}/200')
    # 万敌E4: 受击回10%生命上限
    if isinstance(target, SimUnit) and target.char.id == 'mydei' and target.eidolon_rank >= 4:
        heal = target.max_hp * 0.10
        target.current_hp = min(target.max_hp, target.current_hp + heal)
        state.log.append(f'  万敌E4: 受击回10%生命 +{heal:.0f}')
    # 符玄天赋·太一玄机: HP≤50% → 回已损失90%（E1 +1次）
    if isinstance(target, SimUnit) and target.char.id == 'fu_xuan':
        used = target.extra.get('fuxuan_self_heal_used', 0)
        cap = 1 + (1 if target.eidolon_rank >= 1 else 0)
        if used < cap and target.current_hp <= target.max_hp * 0.5:
            heal = (target.max_hp - target.current_hp) * 0.9
            target.current_hp = min(target.max_hp, target.current_hp + heal)
            target.extra['fuxuan_self_heal_used'] = used + 1
            state.log.append(f'  太一玄机: 回血{heal:.0f} ({used + 1}/{cap}次)')
    # v6.10.6 A2: HP损失管线统一广播实际损失（此前广播请求值, 溢出伤害污染记录/充能）
    _xiadie_absorb_hp_loss(state, actual_lost, "受击")
    state.hooks.trigger_all("on_hp_loss", u=target, state=state,
                            total_lost=actual_lost, affected=[(target, actual_lost)])
    _dispatch_changyeyue_hp_loss(state, [(target, actual_lost)])
    # v5.0.1: 光锥 HP 损失事件（此身为剑·月蚀）
    if isinstance(target, SimUnit):
        state.extra['lc_last_hp_loss'] = actual_lost  # v5.4 在火的远处: 单次受击损失
        _process_lc_effects(target, state, "on_hp_loss")
    # 受击钩子（符玄E4 挂这里）
    state.hooks.trigger_all("on_take_damage", u=target, state=state,
                            attacker=enemy, damage=amount)
    if isinstance(target, SimUnit) and target.char.id == 'qianye' \
            and _qianye_wrath_active(target):
        if any(getattr(t, 'hook_name', '') == 'qianye_trace2'
               for t in (target.char.traces or [])) and enemy is not None:
            _qianye_apply_shaqizhaoshen(state, target, enemy)
            _qianye_gain_charge(state, target, 1)
        _qianye_e6_gain_charge(state, target)
    # v6.10.3 P1-1: 赛飞儿天赋已移至我方攻击命中老主顾后触发（_cipher_attack_aftermath）,
    # 原受击路径方向反了（敌人攻击我方才触发）。
    # v5.0.1: 光锥受击事件（moment_of_victory 防御 buff / 此身为剑 月蚀）
    if isinstance(target, SimUnit):
        _process_lc_effects(target, state, "on_hit_taken")
    # 遗器受击叠层（拳王ATK/莳者CR; 攻击侧由 on_after_skill 注册触发）
    if isinstance(target, SimUnit):
        conds = getattr(target, '_active_relic_conditions', set()) or set()
        if 'stack_atk_on_hit' in conds or 'stack_cr_on_hit' in conds:
            from engine.core.relic_conditions import _stack_atk_on_hit, _stack_cr_on_hit
            if 'stack_atk_on_hit' in conds:
                _stack_atk_on_hit(target, state)
            if 'stack_cr_on_hit' in conds:
                _stack_cr_on_hit(target, state)
    _check_fatal(state, target)


def _distribute_damage(state, target, amount, enemy):
    """符玄天赋: 穷观阵激活时全队减伤18% + 承伤65%分配（一次应用防递归; 两次独立结算天然无递归）"""
    fuxuan = next((u for u in state.units if u.char.id == 'fu_xuan' and u.is_alive), None)
    field = fuxuan is not None and state.extra.get('fuxuan_field_turns', 0) > 0
    if field:
        amount *= 0.82  # 全队减伤18%
        state.log.append(f'  穷观阵减伤18%: {amount:.0f}')
    if field and isinstance(target, SimUnit) and target is not fuxuan:
        _apply_hit(state, target, amount * 0.35, enemy)
        _apply_hit(state, fuxuan, amount * 0.65, enemy)
        t_name = getattr(target, 'name', None) or target.char.name
        state.log.append(f'  符玄承伤65%: 符玄-{amount * 0.65:.0f} / {t_name}-{amount * 0.35:.0f}')
    else:
        _apply_hit(state, target, amount, enemy)


def _check_fatal(state, target):
    """死亡管线: 致命保护链（万敌血仇→符玄E2→藿藿E2）→ 真正死亡"""
    # v6.10.6 A1: 双保险——任何路径进入死亡检查时 HP 不得为负
    if target.current_hp < 0:
        target.current_hp = 0.0
    if target.current_hp > 0:
        return
    # v6.9.1: 千冶·刃无量忿怒致命保护——不死+退出结界+回50%生命上限
    if isinstance(target, SimUnit) and target.char.id == 'qianye' \
            and target.extra.get('qianye_wrath'):
        target.current_hp = 0.0
        _qianye_exit_wrath(state, target, fatal=True)
        return

    if not isinstance(target, SimUnit):
        # 忆灵也可被敌方选中；其死亡必须从行动列表和召唤者引用中清理。
        target.current_hp = 0.0
        target.is_alive = False
        summoner = next((u for u in state.units
                         if u.char.id == getattr(target, 'summoner_id', None)), None)
        rem = state.extra.get('_rem_sys')
        if rem and summoner:
            rem.despawn_memsprite(state, summoner, target, reason="enemy_attack")
        elif target in state.memsprites:
            state.memsprites.remove(target)
            if summoner and summoner.memsprite_unit is target:
                summoner.memsprite_unit = None
        state.log.append(f'  {getattr(getattr(target, "data", None), "name", "忆灵")}被消灭')
        return
    # ① 万敌血仇致命保护
    if target.char.id == 'mydei' and target.extra.get('is_blood_debt'):
        _mydei_fatal_recovery(target, state)
        return
    # ② 符玄E2·柔兆（单场1次+全队回70%）
    from engine.core.effect_resolver import _fuxuan_e2_fatal_check
    if _fuxuan_e2_fatal_check(state):
        return
    # ③ 藿藿E2·镇尾锁灵（2次+全队回50%）
    from engine.core.effect_resolver import _huohuo_e2_fatal_check
    if _huohuo_e2_fatal_check(state):
        return
    # 真正死亡
    target.is_alive = False
    state.extra.get('navs', {}).pop(state.units.index(target), None)
    if target.char.id == 'cipher':
        _cipher_trace3_remove_vuln(state)
        state.log.append('  赛飞儿阵亡→行迹3敌方易伤解除')
    if target.char.id == 'sparkle':
        bonus = target.extra.pop('sparkle_max_sp_bonus', 0)
        if bonus:
            state.max_sp = max(5, state.max_sp - bonus)
            state.log.append(f'  花火阵亡→战技点上限-{bonus}')
    if target.char.id == 'yinlang':
        target.invincible_active = False
        target.invincible_basics_done = 0
        target.extra.pop('yinlang_e2_next_threshold', None)
        target.extra['yinlang_blindbox_prob'] = 1.0
        for enemy in state.enemies:
            enemy.extra.pop('yinlang_e1_vuln', None)
        state.log.append('  银狼阵亡→无敌玩家结界解除')
    if target.char.id == 'hysilens':
        _hysilens_remove_field(state, target)
        state.log.append('  海瑟音阵亡→结界解除')
    if target.char.id == 'sunday':
        for ally in state.units:
            ally.extra.pop('sunday_mentor', None)
            ally.extra.pop('sunday_cr_stacks', None)
            ally.buffs = [b for b in ally.buffs
                          if getattr(b, 'source_id', '') != 'sunday']
            ms = getattr(ally, 'memsprite_unit', None)
            if ms is not None:
                ms.buffs = [b for b in ms.buffs
                            if getattr(b, 'source_id', '') != 'sunday']
        state.log.append('  星期日阵亡→蒙福者与星期日增益解除')
    dht = next((x for x in state.units
                if x.char.id == 'dan_heng_permansor_terrae'), None)
    tongpao_died = bool(dht and target.char.id == dht.extra.get('dht_tongpao_id'))
    if target.char.id == 'dan_heng_permansor_terrae' or tongpao_died:
        owner = target if target.char.id == 'dan_heng_permansor_terrae' else dht
        if owner:
            owner.extra.pop('dht_tongpao_id', None)
            owner.extra.pop('dht_longling_enhanced', None)
            if owner.marker:
                marker_system = state.extra.get('_marker_sys')
                if marker_system:
                    marker_system.despawn(state, owner.marker)
        for ally in state.units:
            ally.extra.pop('dht_tongpao', None)
        for enemy in state.enemies:
            enemy.extra.pop('dht_tongpao_vuln', None)
        state.log.append('  丹恒·腾荒/同袍阵亡→龙灵与同袍效果解除')
    if target.memsprite_unit and target.memsprite_unit.is_alive:
        rem = state.extra.get('_rem_sys')
        if rem:
            rem.despawn_memsprite(state, target, target.memsprite_unit, reason="summoner_death")
    # v5.3: 行动条标记随召唤者阵亡移除（浮元/完全燃烧倒计时）
    if target.marker:
        sys = state.extra.get('_marker_sys')
        if sys:
            sys.despawn(state, target.marker)
    # v5.3: 阵亡事件（光环失效: 忘归人天赋/同谐E4等）
    state.hooks.trigger_all("on_ally_death", u=target, state=state)
    # v5.7: 昔涟阵亡→结界解除（实机: 当昔涟陷入无法战斗状态时, 结界也会被解除）
    if target.char.id == 'xilian' and state.extra.get('xilian_field_turns'):
        state.extra['xilian_field_turns'] = 0
        state.realm_true_dmg = 0
        state.log.append('  昔涟阵亡→结界解除')
    state.log.append(f'  {target.char.name} 阵亡')


def _tick_break_dot(state, enemy, status):
    """击破 DOT 跳伤（敌方回合结算, 快照面板）: 不吃双暴/元素增伤, 吃DOT增伤+全增伤"""
    snap = status.attributes.get('dot_snapshot')
    if not snap:
        return 0.0
    d = calculate_damage(snap, enemy, snap.ATK, status.attributes.get('dot_multiplier', 100.0),
                         "dot", status.attributes.get('dot_element', '物理'), 80)
    source = next((u for u in state.units
                   if u.char.id == getattr(status, 'source', '')), None)
    _commit_enemy_damage(state, source, enemy, d.final_damage)
    state.log.append(f'  {status.name}: {d.final_damage:.0f} → {enemy.name or enemy.id}')
    return d.final_damage


def _enemy_eff_spd(enemy) -> float:
    """敌方有效速度（v5.4 光锥敌方速降消费: 梦应归于何处【溃败】速度-20%等）
    v6.4: 敌方自增益 SPD_PERCENT 消费（与速降并存）
    v6.4b P1-3: SPD_PERCENT/SPD_percent 别名兼容（AGENTS.md 不变量; 每条状态只取其一, 防双键重复叠加）"""
    spd_pct = _enemy_status_sum(enemy, 'SPD_PERCENT', 'SPD_percent')
    return enemy.SPD * (1.0 - enemy.status_attribute('spd_down')) * (1.0 + spd_pct)


def _enemy_status_sum(enemy, *keys):
    """敌方状态属性别名族求和（v6.4b P1-3）: 同一状态只认第一个命中的别名键。"""
    total = 0.0
    for s in enemy.statuses:
        for k in keys:
            v = s.attributes.get(k)
            if v is not None:
                total += float(v)
                break
    return total


def _enemy_apply_self_buff(state, enemy, sb):
    """敌方自增益施加（v6.4b 子代理 P2/P3）: id 与玩家负面状态撞名时不覆盖;
    同 id 刷新取 max 剩余回合（与 _apply_player_status 的 max 语义一致）;
    id 缺失兜底不崩溃。requires_buff 只认 category=='buff' 状态（见 _enemy_attack）。"""
    sid = sb.get('id') or f'self_buff:{sb.get("name", "unknown")}'
    existing = next((s for s in enemy.statuses if s.id == sid), None)
    if existing is not None and existing.category != 'buff':
        state.log.append(f'  [WARN] 自增益[{sid}]与敌方负面状态同名, 跳过施加')
        return
    duration = sb.get('duration', 1)
    attrs = dict(sb.get('attributes', {}) or {})
    if existing is not None:
        existing.remaining_turns = max(existing.remaining_turns, duration)
        existing.attributes.update(attrs)
    else:
        enemy.add_status(EnemyStatus(id=sid, name=sb.get('name', '强化'),
                                     category='buff', source=enemy.id,
                                     remaining_turns=duration, attributes=attrs))
    state.log.append(f'  {enemy.name or enemy.id} {sb.get("name", "强化")}: '
                     f'自身增益 {duration}回合')


def _tick_enemy_statuses(state, enemy):
    """③ 敌方状态倒计时 + 到期恢复（v6.4c 抽出: 冻结跳过回合共用, 项目主确认冻结回合照算倒计时）"""
    expired_statuses = enemy.tick_statuses()
    # v5.3 弱点植入到期恢复（流萤火弱点: status 带 weakness_element/weakness_old_res）
    # v6.3.0b P1-11: 银狼弱点快照=纯抗性; 与全抗降低同批到期时先还原弱点元素,
    # 全抗恢复跳过这些元素防双重回加。
    expired_weak_elems = {st.attributes.get('weakness_element')
                          for st in expired_statuses
                          if st.id == 'silver_wolf_weakness'}
    for st in expired_statuses:
        if 'weakness_element' in st.attributes:
            elem = st.attributes['weakness_element']
            if st.id == 'silver_wolf_weakness':
                pure = st.attributes.get('weakness_old_res', 0.0)
                still_all_res = enemy.has_status(status_id='silver_wolf_all_res_down')
                enemy.element_res[elem] = pure - (0.13 if still_all_res else 0.0)
            else:
                e6_res_down = enemy.extra.get('lingsha_e6_res_down', 0) * 0.20
                enemy.element_res[elem] = st.attributes.get('weakness_old_res', 0.0) - e6_res_down
            state.log.append(f'  {elem}弱点植入结束, 抗性恢复')
        # v6.3.0 银狼战技全抗降低到期恢复（silver_wolf_all_res_down）
        if st.id == 'silver_wolf_all_res_down':
            for elem in list(enemy.element_res):
                if elem in expired_weak_elems:
                    continue  # 同批到期的弱点已按纯抗性恢复, 防双重回加
                enemy.element_res[elem] = enemy.element_res.get(elem, 0) + 0.13
            state.log.append(f'  银狼全抗-13%结束, 抗性恢复')
    break_status = next((s for s in enemy.statuses if s.id.startswith('break:')), None)
    enemy.break_debuff_turns = break_status.remaining_turns if break_status else 0
    for expired in expired_statuses:
        if not expired.id.startswith('break:'):
            state.log.append(f'  {expired.name}解除 {enemy.name or enemy.id}')
            continue
        name = expired.name
        if name == "禁锢":
            enemy.SPD += enemy.extra.pop('imprison_speed_reduced', 0.0)
            state.log.append(f'  禁锢解除: 速度恢复 {enemy.name or enemy.id}')
        else:
            state.log.append(f'  {name}解除 {enemy.name or enemy.id}')
        enemy.break_debuff_name = ""
    return expired_statuses


def _enemy_exec_action(state, enemy):
    """④ 敌方单次行动（v6.4c 抽出: 精英双动每动复用; 行动后 sweep 让满能玩家大招可插入两动之间）"""
    _enemy_attack(state, enemy)
    # v6.6 白厄: 敌方攻击后叠加弑魂之炽（v6.6b P1-7: 仅存活且在变身中的白厄）
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('shihun_stacks', 0) > 0
               and x.is_alive and x.extra.get('kasier')), None)
    if ph:
        ph.extra['shihun_stacks'] = ph.extra.get('shihun_stacks', 0) + 1
        state.log.append(f'  弑魂之炽叠层: {ph.extra["shihun_stacks"]}层')
    _sweep_ults(state)


def _enemy_turn_end(state, enemy, enemy_attacked: bool = True):
    """④b 纠缠推条 + ⑤ AV 更新（v6.4c 抽出: 一回合多动只在最后一动后执行一次）

    v6.6b P1-7: enemy_attacked=False（冻结跳过）时只推条, 不叠层不反击;
    反击仅限存活且在变身中的白厄。"""
    # v6.6 白厄: 敌方行动完毕后立即发动弑魂反击（每层倍率+20%）并解除状态
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('shihun_stacks', 0) > 0
               and x.is_alive and x.extra.get('kasier')), None)
    if ph and enemy.HP > 0 and enemy_attacked:
        stacks = ph.extra.pop('shihun_stacks', 0)
        _phainon_shihun_counter(state, ph, stacks)
        # v6.6b P1-1: 反击后解除减伤
        ph.extra.pop('shihun_dr', None)
        ph.buffs = [b for b in ph.buffs
                    if getattr(b, 'source_name', '') != '弑魂之炽减伤']
    # ④b 纠缠（v5.0 P7）: 量子击破异常 — 敌方行动时推条20%回合值
    if any(s.name == '纠缠' for s in enemy.statuses):
        push = AV_PER_TURN / max(_enemy_eff_spd(enemy), 1.0) * 0.20
        enemy.extra['av_delayed'] = enemy.extra.get('av_delayed', 0) + push
        state.log.append(f'  纠缠: 推条{push:.0f}')
    # ⑤ AV 更新（av_delayed 三写零读就此闭环: 击破2500/冻结5000）
    delay = enemy.extra.pop('av_delayed', 0.0)
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i),
            state.current_av + AV_PER_TURN / max(_enemy_eff_spd(enemy), 1.0) + delay)
    _reset_qianye_e6_charge_gate(state)


def _enemy_pending_step(state):
    """②b 精英双动第二行动（v6.4c）: 主循环在 X 轴清空后调用; 返回 True=已处理需 continue。

    破韧打断（口径A, 项目主确认）: 同回合内被击破 → 第二行动取消,
    击破的 2500 推条已在破韧时写入 av_delayed, 回合结束时作用于下一回合。
    """
    epa = state.extra.get('enemy_pending_action')
    if epa is None:
        return False
    enemy = epa
    if enemy.HP > 0 and enemy.extra.get('_actions_left', 0) > 0:
        if enemy.is_broken:
            state.log.append(f'  {enemy.name or enemy.id}破韧打断: 第二行动取消(推条作用于下回合)')
        else:
            _enemy_exec_action(state, enemy)
        enemy.extra['_actions_left'] -= 1
    if enemy.HP <= 0 or enemy.extra['_actions_left'] <= 0:
        if enemy.HP > 0:
            _enemy_turn_end(state, enemy)
        state.extra['enemy_pending_action'] = None
    return True


def _begin_enemy_turn(state, enemy):
    """敌方常规回合（Y轴）: 冻结检查 → 韧性恢复 → DOT结算 → 状态倒计时 → 行动（精英双动）→ av_delayed 消费

    v6.4c 精英双动（项目主确认实机语义）: actions_per_turn>1 时同一回合连续行动多次,
    行动之间主循环①清空 X 轴（玩家终结技/破韧打断窗口）; 第一行动上的 self_buff 第二行动立即吃到。
    """
    state.turn_count += 1
    for qianye in state.units:
        if qianye.char.id == 'qianye':
            qianye.extra.pop('qianye_e6_charge_used', None)
    # v6.9.1: 失重受击延后次数每回合重置（txt:48 每目标每回合最多8次）
    enemy.extra['welt_shizhong_count'] = 0
    state.extra['action_ctx'] = 'enemy'  # 敌方回合不 tick 我方 buff（AGENTS.md:17）
    # ⓪ 控制（v5.0 P7 用户实机语义; v6.6c 扩展禁锢）: 即将开始常规回合时跳过该回合 + 推条 + 解除
    # v6.4c 点1: 冻结跳过回合 buff/debuff 同样计算倒计时（DOT 跳伤仍不结算）
    cc = next((s for s in enemy.statuses
               if s.category == 'control' and s.name == '冻结'
               or (s.category == 'control' and s.name == '禁锢'
                   and not s.id.startswith('break:'))), None)  # 击破异常禁锢走③恢复, 不走跳过
    if cc:
        enemy.statuses.remove(cc)
        push = 5000.0 if cc.name == '冻结' else cc.attributes.get('delay_amount', 2500.0)
        enemy.extra['av_delayed'] = enemy.extra.get('av_delayed', 0) + push
        enemy.break_debuff_name = ""
        enemy.break_debuff_turns = 0
        _tick_enemy_statuses(state, enemy)
        state.log.append(f'  {enemy.name or enemy.id} {cc.name}: 跳过行动+推条{push:.0f}, {cc.name}解除')
        _enemy_turn_end(state, enemy, enemy_attacked=False)  # v6.6b P1-7: 未行动不反击
        return
    # ① 韧性恢复（击破后敌人行动时复原）
    if enemy.is_broken:
        # v6.9 阮·梅【残梅绽】: 尝试从破韧恢复时触发——延长破韧(跳过恢复)+推条+冰击破伤害
        ruan = next((x for x in state.units
                     if x.char.id == 'ruan_mei' and x.is_alive), None)
        if ruan is not None and _ruanmei_canmei_trigger_v3(state, ruan, enemy):
            state.log.append(f'  {enemy.name or enemy.id} 残梅绽延长破韧')
            return  # 跳过后续恢复/DOT/行动（破韧保持, 本回合仍不行动）
        enemy.is_broken = False
        enemy.toughness = enemy.max_toughness
        enemy.break_debuff_name = ""
        # v5.3 灵砂E1: 击破期间 DEF-20% 随韧性恢复结束
        enemy.remove_status('lingsha_e1_def_down')
        state.log.append(f'  {enemy.name or enemy.id} 韧性恢复')
    # ② DOT 结算（先跳伤再递减 → 持续2回合恰好2跳）
    # v6.6c P1: 海瑟音DOT（hysilens_*）同样结算; 结界引爆窗口挂在此处（敌受DOT跳→反打）
    # v6.8.1: 海瑟音E1「我方持续伤害×116%」全队 DOT 统一结算乘（含击破DOT）
    hs_e1 = next((x for x in state.units
                  if x.char.id == 'hysilens' and x.is_alive and x.eidolon_rank >= 1), None)
    # v6.8.3: 反打次数上限绑定“敌方回合开始时”边界（此前只在终结技重置, 跨回合累计）
    state.extra['hysilens_trigger_count'] = 0

    for s in list(enemy.statuses):
        if s.category != 'dot':
            continue
        if s.id.startswith('break:'):
            d = _tick_break_dot(state, enemy, s)
            if hs_e1 and d > 0 and enemy.HP > 0:
                _commit_enemy_damage(state, hs_e1, enemy, d * 0.16)
        elif s.id.startswith('hysilens_'):
            d = _tick_hysilens_dot(state, enemy, s)
            if hs_e1 and d > 0 and enemy.HP > 0:
                _commit_enemy_damage(state, hs_e1, enemy, d * 0.16)
            hs = next((x for x in state.units
                       if x.char.id == 'hysilens' and x.is_alive), None)
            if hs and enemy.HP > 0:
                _hysilens_dot_trigger_v3(state, hs, enemy)
    if enemy.HP <= 0:
        enemy.HP = 0.0
        if not enemy.extra.get('_kill_recorded'):
            _record_enemy_kill(state)
        state.log.append(f'  {enemy.name or enemy.id} 被持续伤害击败')
        return
    # ③ 状态倒计时 + 到期恢复
    _tick_enemy_statuses(state, enemy)
    # ④ 第一行动
    _enemy_exec_action(state, enemy)
    # ⑤ 精英双动（v6.4c）: 剩余行动挂 pending, 由主循环 ②b 在 X 轴清空后执行
    acts = max(int(getattr(enemy, 'actions_per_turn', 1) or 1), 1)
    if acts > 1 and enemy.HP > 0:
        enemy.extra['_actions_left'] = acts - 1
        state.extra['enemy_pending_action'] = enemy
    else:
        _enemy_turn_end(state, enemy)


def _exec_extra_turn(state, unit, kind):
    """X 轴额外回合执行（从左往右）"""
    state.extra['action_ctx'] = 'extra'
    # v7.2.0 #7: 姬子行迹2额外回合开始→解除防循环标记(此后再使用助战技可再触发)
    if isinstance(unit, SimUnit):
        unit.extra.pop('hn_trace2_pending', None)
    # v5.0 P4: 冻结期间禁止额外回合（含已入队行动, 用户实机语义）
    if isinstance(unit, SimUnit) and any(getattr(st, 'name', '') == '冻结'
                                         for st in unit.statuses):
        state.log.append(f'  {unit.char.name}冻结中: 额外回合被跳过')
        state.turn_count += 1
        # v6.2.1b P3-9（评审确认设计意图, 勿补 _sweep_ults）: 跳过期间无能量事件,
        # 对他人 sweep 是无操作; 对自己 sweep 会把被跳过的冻结终结技立刻重入队→
        # 主循环弹出自转直到 ult_chain_guard 顶满。解冻后常规回合 phase-1 会重新入队, 大招不丢只顺延。
        # v6.2.1: 冻结跳过同样执行再现收尾（Codex P1-4: 残留 seele_in_extra 会永久封死再现）
        if unit.char.id == 'seele':
            unit.extra['seele_in_extra'] = False
            unit.buffs = [b for b in unit.buffs
                          if getattr(b, 'source_name', '') != '再现增幅']
            state.log.append('  再现结束: 增幅解除')
        _reset_qianye_e6_charge_gate(state)
        return
    # 希儿增幅: 击杀瞬间获得的 pending 状态, 在 X 轴首个希儿行动(终结技或再现)时激活
    # (增幅覆盖 X 轴上到增幅回合结束的一切行动; 回到 Y 轴常规回合时已撤销)
    if (isinstance(unit, SimUnit) and unit.char.id == 'seele'
            and unit.extra.get('seele_amplify_pending')):
        if not any(getattr(b, 'source_name', '') == '再现增幅' for b in unit.buffs):
            unit.buffs.append(TimedBuff(source_id='seele', attributes={"DMG_BONUS_ALL": 80.0},
                                        remaining_turns=1, source_name='再现增幅'))
            state.log.append('  再现: 增幅生效(80%增伤)')
        unit.extra['seele_amplify_pending'] = False
    from engine.systems.remembrance import RemembranceSystem
    if kind == 'ult':
        state.log.append(f'  === 额外回合[终结技]: {getattr(unit, "char", unit).name if hasattr(unit, "char") else unit.data.name} ===')
        if hasattr(unit, 'char'):
            if unit.char.id == 'xilian' and unit.is_ripple and unit.zhuiyi >= 12:
                _use_skill(unit, state, "ultimate_ripple")
                # 忆灵技释放（追忆-12 + 德谬歌行动: 花与箭/此诗献予, 用户确认实机）
                if unit.memsprite_unit and unit.memsprite_unit.is_alive:
                    from engine.systems.remembrance import RemembranceSystem
                    rem = state.extra.get('_rem_sys') or RemembranceSystem()
                    rem._xilian_memsprite_action(state, unit, unit.memsprite_unit)
            else:
                _use_skill(unit, state, "ultimate")
            _ult_post(state, unit)
        _sweep_ults(state)
    else:
        _dispatch_extra_action(state, unit)
        # 规则4: 额外回合内满能量→追加队尾（排队）
        if isinstance(unit, SimUnit) and _should_ult_now(unit, state):
            _enqueue_ult(state, unit)
        _sweep_ults(state)
        # 希儿增幅: 额外回合结束解除
        if isinstance(unit, SimUnit) and unit.char.id == 'seele':
            unit.extra['seele_in_extra'] = False
            unit.buffs = [b for b in unit.buffs
                          if getattr(b, 'source_name', '') != '再现增幅']
            state.log.append('  再现结束: 增幅解除')
    _reset_qianye_e6_charge_gate(state)


def _dispatch_extra_action(state, unit):
    """额外回合动作分发（按身份）"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    state.extra['_rem_sys'] = rem
    if isinstance(unit, SimUnit):
        cid = unit.char.id
        if cid == 'seele':
            # 希儿再现: 再行动一次（普攻/战技）; 增幅已在 _exec_extra_turn 开头激活
            state.log.append(f'  === 额外回合[再现]: {unit.char.name} ===')
            if state.skill_points > 0:
                _use_skill(unit, state, "skill")
            else:
                _use_skill(unit, state, "basic_attack")
        elif cid == 'mydei':
            # 万敌弑神登神
            state.log.append(f'  === 额外回合[弑神登神]: {unit.char.name} ===')
            _use_skill(unit, state, "skill_shenshen")
        else:
            state.log.append(f'  === 额外回合: {unit.char.name} ===')
            # v6.7: 统一走 _ai_regular_action（直接 ai_fn(unit, state) 缺 elation/navs 等 kwargs）
            _ai_regular_action(state, unit)
    else:
        # 忆灵额外回合（小伊卡/德谬歌/界外）
        if unit.data.name == '小伊卡':
            # 小伊卡额外回合: 乌云乌云+天赋追加治疗
            summoner = next((x for x in state.units if x.char.id == 'fengjin'), None)
            if summoner and summoner.extra.get('clear_sky_turns', 0) > 0:
                ms = unit
                if ms.cumulative_healing > 0:
                    # 风堇E6: 乌云乌云→全队回复12%累计治疗并清空
                    if summoner.eidolon_rank >= 6:
                        heal_amt = ms.cumulative_healing * 0.12
                        # v6.2.1: 全队含忆灵（Codex P2-6）
                        for eu in [x for x in state.units if x.is_alive] \
                                + [x for x in state.memsprites if x.is_alive]:
                            eu.current_hp = min(eu.max_hp, eu.current_hp + heal_amt)
                        ms.cumulative_healing = 0
                        state.log.append(f'  乌云乌云: 全队回复12%并清空({heal_amt:.0f}HP)')
                    else:
                        # v5.6.1: 乌云乌云是直伤, 与其他直伤一样走伤害管线吃乘区
                        # （防御/抗性/增伤/双暴/易伤）; 倍率=累计治疗×20%（用户确认）
                        base = ms.cumulative_healing * 0.20
                        alive = state.alive_enemies()
                        from engine.systems.remembrance import _ms_effective_stats
                        ms_stats = _ms_effective_stats(ms, state)
                        total = 0.0
                        for t in alive:
                            d = calculate_damage(
                                ms_stats, _enemy_for_damage(t), base, 100.0,
                                "direct", "风", 80, ms_stats.CRIT_RATE >= 0.5,
                                crit_mode="expected")
                            _commit_enemy_damage(state, summoner, t, d.final_damage)
                            total += d.final_damage
                        ms.total_damage_dealt += total
                        summoner.total_damage_dealt += total
                        state.log.append(f'  乌云乌云: {total:.0f}伤害(累计治疗={ms.cumulative_healing:.0f})')
                        _qingge_notify_attack(state, summoner, dealt=total > 0)  # v7.1.0 P1: X轴忆灵直伤分支补气氛
                        ms.cumulative_healing *= 0.50
                # 天赋追加治疗: 2%风堇HP+20 × 全队
                bonus_heal = summoner.base_stats.HP * 0.02 + 20
                # 风堇行迹1·暴风停歇: 每超1点SPD→治疗量+1%(上限200点)
                if summoner.base_stats.SPD > 200:
                    bonus_heal *= 1.0 + min(summoner.base_stats.SPD - 200, 200) / 100.0
                # v6.2.1: 全队含忆灵（Codex P2-6: 天赋治疗/on_heal/累计治疗统一包含忆灵）
                alive_allies = [eu for eu in state.units if eu.is_alive] \
                    + [x for x in state.memsprites if x.is_alive]
                total_healed = 0.0
                for eu in alive_allies:
                    amt = bonus_heal
                    # 风堇行迹2·阴云莞尔: 对HP≤50%目标治疗量+25%
                    if eu.current_hp <= eu.max_hp * 0.50:
                        amt = bonus_heal * 1.25
                    eu.current_hp = min(eu.max_hp, eu.current_hp + amt)
                    total_healed += amt
                # 献予「天空」之诗: 风堇持层时治疗计入小伊卡×1.72
                if summoner.extra.get('poem_tiankong', 0) > 0:
                    total_healed *= 1.72
                ms.cumulative_healing += total_healed
                state.log.append(f'  小伊卡天赋: +{bonus_heal:.0f}HP×{len(alive_allies)}人')
                # 追加治疗也计入新蕊(on_heal hook)
                state.hooks.trigger_all("on_heal", u=summoner, state=state,
                                         healer=summoner, targets=alive_allies, heal_amt=bonus_heal)
                _fengjin_talent_heal_buff(state, summoner)
            return
        rem.handle_memsprite_action(state, unit, regular_turn=False)


def _apply_team_static_relics(state):
    """v5.2: 队伍上下文静态遗器条件（S1③+S2⑤）

    - 仙舟2pc (spd_threshold_120_team_atk): 佩戴者SPD≥120 → 全队ATK+8%（佩戴者自身已由静态层获得）
    - 龙骨2pc (effect_res_30_team_cd): 佩戴者EFFECT_RES≥30% → 全队CD+10%（同上）
    - 坠星2pc (enter_combat_faction_cd): 有同阵营队友(简化:任意队友) → 佩戴者CD+32%
    幂等: state.extra['team_static_applied'] 去重
    """
    if state.extra.get('team_static_applied'):
        return
    state.extra['team_static_applied'] = True
    for u in state.units:
        conds = getattr(u, '_active_relic_conditions', set()) or set()
        if 'spd_threshold_120_team_atk' in conds and u.base_stats.SPD >= 120:
            for eu in state.units:
                if eu is not u:
                    eu.base_stats.ATK += eu.base_stats._base_ATK * 0.08
            state.log.append('  仙舟2pc: 全队ATK+8%')
        if 'effect_res_30_team_cd' in conds and u.base_stats.EFFECT_RES >= 0.30:
            for eu in state.units:
                if eu is not u:
                    eu.base_stats.CRIT_DMG += 0.10
            state.log.append('  龙骨2pc: 全队CD+10%')
        if 'enter_combat_faction_cd' in conds:
            if any(x is not u and x.is_alive for x in state.units):
                u.base_stats.CRIT_DMG += 0.32
                state.log.append('  坠星2pc: 同阵营队友→CD+32%')


def _begin_regular_turn(state, u):
    """Y 轴常规回合开始"""
    navs = state.extra.get('navs', {})
    unit_idx = state.units.index(u)
    # 希儿增幅防御: 常规回合开始前 X轴必已清空(增幅回合已撤销), 清异常残留的 pending
    if u.char.id == 'seele':
        u.extra['seele_amplify_pending'] = False
    # 更新 AV + stamp（v5.0: 有效速度含 SPD_PERCENT buff）
    next_av = state.current_av + AV_PER_TURN / _effective_spd(u, state)
    if getattr(u, '_pending_action_advance', 0) > 0:
        next_av -= u._pending_action_advance
        u._pending_action_advance = 0
    _set_av(state, navs, unit_idx, next_av)
    state.turn_count += 1
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    for qianye in state.units:
        if qianye.char.id == 'qianye':
            qianye.extra.pop('qianye_e6_charge_used', None)

    # 西风的驻足按“遐蝶本回合”近似：强化战技后保留到下一个常规回合开始，
    # 让之后的死龙 Y 轴行动能够实际读取该加成。
    if u.char.id == 'xiadie':
        u.extra.pop('xiadie_flame_stack', None)

    # v6.6 缇宝: 神启/结界回合开始-1（实机"自身每回合开始时持续回合数减1"）
    if u.char.id == 'tribbie':
        st = u.extra.get('tribbie_shenqi_turns', 0)
        if st > 0:
            st -= 1
            if st <= 0:
                _tribbie_remove_shenqi(u, state)
            else:
                u.extra['tribbie_shenqi_turns'] = st
        ft = state.extra.get('tribbie_field_turns', 0)
        if ft > 0:
            ft -= 1
            if ft <= 0:
                _tribbie_remove_field(u, state)
            else:
                state.extra['tribbie_field_turns'] = ft
    # v6.6c P1: 海瑟音结界倒计时 + 到期恢复（此前只设不消费=永久）
    if u.char.id == 'hysilens':
        ft = state.extra.get('hysilens_field_turns', 0)
        if ft > 0:
            ft -= 1
            if ft <= 0:
                _hysilens_remove_field(state, u)
            else:
                state.extra['hysilens_field_turns'] = ft
    # v6.6c P1: 赛飞儿 ATK+30% 到期回减 + FUA 回合重置
    if u.char.id == 'cipher':
        t = u.extra.get('cipher_atk_buff', 0)
        if t > 0:
            t -= 1
            if t <= 0:
                u.extra['cipher_atk_buff'] = 0
                u.base_stats.ATK -= u.base_stats._base_ATK * 0.30
                state.log.append('  猫咪怪盗: ATK+30%到期回减')
            else:
                u.extra['cipher_atk_buff'] = t
        # v6.10.3 P1-1: E1 FUA ATK+80% 2回合到期回减
        t1 = u.extra.get('cipher_e1_atk_buff', 0)
        if t1 > 0:
            t1 -= 1
            if t1 <= 0:
                u.extra.pop('cipher_e1_atk_buff', None)
                u.base_stats.ATK -= u.base_stats._base_ATK * 0.80
                state.log.append('  赛飞儿E1: FUA ATK+80%到期回减')
            else:
                u.extra['cipher_e1_atk_buff'] = t1
        u.extra.pop('cipher_fua_used', None)  # 老主顾FUA 1次/回合重置
    # v6.6 刻律德菈: 军功SPD buff 3回合到期回减（行迹3）
    # v6.6c P1: 回减记录的受buff者（此前回减当前军功者, 换目标后打错对象）
    if u.char.id == 'cerydra' and u.extra.get('cerydra_spd_buff_turns', 0) > 0:
        t = u.extra['cerydra_spd_buff_turns'] - 1
        if t <= 0:
            u.base_stats.SPD -= 20
            jid = u.extra.pop('cerydra_spd_buff_ally', None)
            jg = next((x for x in state.units if x.char.id == jid and x.is_alive), None) if jid else None
            if jg:
                jg.base_stats.SPD -= 20
            u.extra['cerydra_spd_buff_turns'] = 0
        else:
            u.extra['cerydra_spd_buff_turns'] = t
    # v6.9.1: v6.9 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'sunday':
        _sunday_tick(state, u)
    if u.char.id == 'ruan_mei':
        _ruanmei_tick(state, u)
    if u.char.id == 'robin':
        _robin_skill_tick(state, u)
    if u.char.id == 'acheron':
        _acheron_tick(state, u)
    if u.char.id == 'feixiao':
        _feixiao_tick(state, u)
    if u.char.id == 'anaxa' and any(
            getattr(t, 'hook_name', '') == 'anaxa_trace1'
            for t in (u.char.traces or [])):
        from engine.core.effect_resolver import _trace_anaxa_turn_energy
        _trace_anaxa_turn_energy(u, state)


    # 子系统计时（行动前）
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_turn_start(state, u)
    if state.extra.get('_rem_sys'):
        state.extra['_rem_sys'].tick_turn(state, u)

    # v6.10.6 C2: 花火行迹2 单回合SP消耗计数重置（我方常规回合开始）
    state.extra['sparkle_turn_sp_spent'] = 0
    # Hook: 回合开始
    state.hooks.trigger(u.char.id, "on_turn_start", u=u, state=state)
    # v5.0.1: 光锥自身回合开始事件（烈阳移除等）
    _process_lc_effects(u, state, "on_self_turn_start")
    # v5.0 P4: 控制状态判定（冻结/眩晕跳过本回合）
    if _check_control_status(state, u):
        if state.extra.get('_elation'):
            state.extra['_elation'].tick_good_show_turn(state, u)
        return

    # v6.10.6 B2: 藿藿禳命——藿藿自身状态（X轴不tick, 仅常规回合）:
    # 藿藿回合开始先递减; 藿藿持有时我方目标回合开始回血
    if u.char.id == 'huohuo':
        _huohuo_ruming_tick(state, u)
    _huohuo_ruming_heal_all(state, u)

    # 未来 token 消耗
    if u.char.id != 'xilian' and u.has_future:
        u.has_future = False
        xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
        if xilian:
            xilian.zhuiyi = min(27, xilian.zhuiyi + 1)
            sources = xilian.extra.setdefault('zhuiyi_sources', set())
            sources.add(u.char.id)
            state.log.append(f'  未来消耗→昔涟追忆+1 ({xilian.zhuiyi:.0f}/27) 来源={u.char.name}')
            state.hooks.trigger_all("on_future_consume", u=u, state=state)

    # 万敌致命攻击检查
    if u.char.id == 'mydei' and u.current_hp <= 0 and u.extra.get('is_blood_debt'):
        _mydei_fatal_recovery(u, state)

    # 遐蝶行迹3: 任意单位行动后重置治疗转化计数
    state.extra['xiadie_heal_conv'] = 0.0
    # v5.7: 万敌E2: 任意单位行动后重置可累计的治疗转充能（此前40点上限变整场累计）
    mydei = next((x for x in state.units if x.char.id == 'mydei' and x.is_alive), None)
    if mydei:
        mydei.extra['e2_heal_converted'] = 0.0

    # 阿格莱雅E2: 其他单位行动→清除无视防御层
    if u.char.id != 'aglaea':
        aglaea = next((x for x in state.units if x.char.id == 'aglaea'), None)
        if aglaea and aglaea.eidolon_rank >= 2 and aglaea.extra.get('aglaea_e2_stack', 0) > 0:
            stack = aglaea.extra['aglaea_e2_stack']
            aglaea.base_stats.DEF_PEN = max(0, aglaea.base_stats.DEF_PEN - 0.14 * stack)
            if aglaea.memsprite_unit:
                aglaea.memsprite_unit.base_stats.DEF_PEN = max(
                    0, aglaea.memsprite_unit.base_stats.DEF_PEN - 0.14 * stack)
            aglaea.extra['aglaea_e2_stack'] = 0
            state.log.append(f'  E2: {u.char.name}行动→无视防御层清除')

    # phase-1: 终结技优先决策（规则3: 常规回合释放终结技→自己回合挤到X轴）
    if _ai_ult_check(state, u):
        state.extra['pending_turn'] = u
        state.extra['turn_hold_guard'] = state.extra.get('turn_hold_guard', 0) + 1
        return

    # 普攻/战技结束回合
    _ai_regular_action(state, u)
    _seele_reproduce_check(state, u, 'regular')
    _sweep_ults(state)
    _tick_buffs(u)
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_good_show_turn(state, u)
    _lc_tick_stacks(state, u)  # v5.0.1: 光锥叠层回合倒计时（Y轴回合末）
    state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
    _reset_qianye_e6_charge_gate(state)


def _resume_regular_turn(state, u):
    """回到被挤出的常规回合（规则3: 终结技执行完回自己回合）"""
    state.turn_count += 1
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    # 防死循环: 同一常规回合被hold≥2次→强制结束
    hold = state.extra.get('turn_hold_guard', 0)
    if hold >= 3:
        state.log.append(f'  [WARN] {u.char.name}常规回合被hold过多次, 强制行动')
    _ai_regular_action(state, u)
    _seele_reproduce_check(state, u, 'regular')
    _sweep_ults(state)
    _tick_buffs(u)
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_good_show_turn(state, u)
    _lc_tick_stacks(state, u)  # v5.0.1: 光锥叠层回合倒计时
    state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
    _reset_qianye_e6_charge_gate(state)
    state.extra['turn_hold_guard'] = 0


def _seele_reproduce_check(state, u, ctx):
    """希儿【再现】: 常规回合击杀→1个额外回合（不能无限续杯）
    增幅=击杀瞬间获得的战利品(挂 pending 标志, 不进 buffs, 免被本回合末 _tick_buffs 误杀);
    X轴首个希儿行动(终结技或再现)时激活(_exec_extra_turn 开头补施), 增幅回合结束撤销
    ——实机: 战技动画中释放的终结技排在增幅回合前也能吃到增幅"""
    if u.char.id != 'seele' or u.extra.get('seele_in_extra'):
        return
    if state.extra.get('killed_this_action', 0) > 0 and ctx == 'regular':
        u.extra['seele_in_extra'] = True
        u.extra['seele_amplify_pending'] = True
        state.extra.setdefault('extra_turns', []).append((u, 'extra'))
        state.log.append('  【再现】: 击杀→获得额外回合, 进入增幅状态')


def _ai_regular_action(state, u):
    """常规行动统一入口（普攻/战技，AI主体）"""
    ai_fn = state.ai_registry.get(u.char.id)
    try:
        if ai_fn:
            ai_fn(u, state, elation=state.extra.get('_elation'),
                  max_av=state.extra.get('_max_av', 1000),
                  navs=state.extra.get('navs', {}),
                  uidx=state.units.index(u))
        else:
            _default_ai(u, state)
        _hn_ally_auto_support(state, u)  # v7.2.0 #8: 队友消费助战技次数呼唤拓星者
    except Exception as e:
        state.log.append(f'  [ERROR] {u.char.name} AI崩溃: {e}')
        import traceback
        state.log.append(f'  {traceback.format_exc()}')
        raise


def _hn_ally_auto_support(state, u):
    """v7.2.0 #8: 非姬子我方角色行动后自动使用助战技——
    姬子·启行在场且全队共享次数>0时呼唤「拓星者」（不占该角色行动, 消耗1次共享次数）。
    此前助战技次数池只有姬子自己的AI与协议触发在消费, 队友从不使用。"""
    if not isinstance(u, SimUnit) or u.char.id == 'himeko_nova' or not u.is_alive:
        return
    hn = next((x for x in state.units
               if x.char.id == 'himeko_nova' and x.is_alive), None)
    if hn is None or state.extra.get('hn_support_uses', 0) <= 0:
        return
    _hn_support_skill(state, u)


def _ult_post(state, unit):
    """终结技执行后的附加动作（风堇雨过天晴等）"""
    if hasattr(unit, 'char') and unit.char.id == 'fengjin':
        # 雨过天晴: 3回合，不叠加
        if not unit.extra.get('clear_sky_turns'):
            unit.extra['clear_sky_turns'] = 3
            # v6.2.1: 全队含忆灵（项目主确认: 忆灵算全队, 死龙亦算忆灵; Codex P2-6）
            sky_targets = [eu for eu in state.units if eu.is_alive] \
                + [ms for ms in state.memsprites if ms.is_alive]
            for eu in sky_targets:
                # v5.7: 记录原HP上限, 状态结束回退; 刷新时按原值重算防叠乘（此前永久改写+叠乘）
                if 'clear_sky_orig_maxhp' not in eu.extra:
                    eu.extra['clear_sky_orig_maxhp'] = eu.max_hp
                orig = eu.extra['clear_sky_orig_maxhp']
                # 风堇E1: 雨过天晴HP上限加成+50%（整体×1.5: 30%→45%, 600→900）
                eu.max_hp = orig * (1.45 if unit.eidolon_rank >= 1 else 1.30) \
                    + (900 if unit.eidolon_rank >= 1 else 600)
                eu.current_hp = min(eu.max_hp, eu.current_hp)
            state.log.append('  雨过天晴: 全队HP上限+30%+600 (3回合, 含忆灵)')
        # 小伊卡额外回合入队（例2连锁）
        from engine.systems.remembrance import RemembranceSystem
        rem = state.extra.get('_rem_sys') or RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem._fengjin_extra_turn(state, unit)


# ---- 主模拟 ----

def simulate(configs: list[dict], enemy_template: Enemy, max_av: float = 1000.0,
             num_enemies: int = 1, enemy_templates: list = None) -> SimState:
    """v6.5: enemy_templates 非空时逐模板创建异构敌人（每怪独立 HP/韧性/弱点/精英双动）,
    波次重生按模板列表重建; 否则沿用单模板×num_enemies 的旧契约。"""
    # v5.2 问题1修复: 配置模型是调用方输入, 记忆系统会为忆灵同步速度/后援状态,
    # 必须在局内副本上运行, 避免污染调用方 Character/LightCone/RelicSet 实例。
    configs = copy.deepcopy(configs)
    enemy_template = copy.deepcopy(enemy_template)
    # 敌人
    enemies = []
    if enemy_templates:
        for i, tpl in enumerate(enemy_templates):
            e = copy.deepcopy(tpl)
            e.id = f'{tpl.id}_{i}'
            enemies.append(e)
        num_enemies = len(enemies)
    else:
        for i in range(num_enemies):
            e = copy.deepcopy(enemy_template)
            e.id = f'{enemy_template.id}_{i}'
            enemies.append(e)
    state = SimState(enemies=enemies)

    # 角色
    units = []
    for cfg in configs:
        char = cfg["char"]
        relics, relic_sets = cfg.get("relics"), cfg.get("relic_sets")
        stats = compute_combat_stats(char, cfg.get("lightcone"), relics, relic_sets)
        u = SimUnit(char=char, base_stats=stats, position=cfg.get("position", 0),
                    lightcone=cfg.get("lightcone"))
        u.max_hp = stats.HP
        u.current_hp = stats.HP
        # v6.10 全局能量规则: 常规能量角色开局默认50%能量(显式initial_energy_pct覆盖);
        # 特殊能量角色(energy_type=special)开局0终结技进度
        if getattr(char, 'energy_type', 'regular') == 'special':
            u.current_energy = 0.0
        else:
            u.current_energy = (char.max_energy or 1) * cfg.get("initial_energy_pct", 50.0) / 100.0
        u.eidolon_rank = cfg.get("eidolon", 0)
        u._active_relic_conditions = _get_relic_conditions(relics, relic_sets)
        # v5.6: 单体技能目标优先级标记——船长4pc持有者需被队友选中叠Help
        # （数据驱动: 条件名来自 data/relics/126_恶海逐波的船长.json 复合条件; 未来嘲讽角色复用同标记）
        if 'help_stack_gain' in u._active_relic_conditions:
            u.extra['single_ally_priority'] = 1
        units.append(u)
    state.units = units

    # ── 效果解析：读取所有角色行迹/光锥/遗器/星魂，注册到 HookRegistry ──
    from engine.core.effect_resolver import register_team_effects
    register_team_effects(configs, state.hooks)

    # 队伍检测：按需激活子系统，构建AI注册表
    mechanics = TeamMechanics(units)
    elation = None
    ai_registry = {}

    # ── 欢愉子系统 ──
    if mechanics.has("elation"):
        from engine.systems.elation import ElationSystem, CHARACTER_AI as ELATION_AI
        elation = ElationSystem()
        ai_registry.update(ELATION_AI)
        _register_elation_skill_hooks(state.skill_hooks)

    # ── v6.7 姬子·启行（助战技子系统）──
    if any(u.char.id == 'himeko_nova' for u in units):
        hn_nova = next(u for u in units if u.char.id == 'himeko_nova')
        state.extra['hn_support_uses'] = 1  # 天赋: 全队1次助战技使用次数
        # 特殊效果免费助战技（单场2次, 终结技后刷新; E1: 3次）
        state.extra['hn_protocol_uses'] = 3 if hn_nova.eidolon_rank >= 1 else 2
        ai_registry['himeko_nova'] = _hn_ai

    # ── v6.9 批1: 星期日/瓦尔特/阮·梅 ──
    if any(u.char.id in ('sunday', 'welt', 'ruan_mei') for u in units):
        _register_v69_skill_hooks(state.skill_hooks)
        if any(u.char.id == 'sunday' for u in units):
            ai_registry['sunday'] = _sunday_ai
        if any(u.char.id == 'welt' for u in units):
            ai_registry['welt'] = _welt_ai
        if any(u.char.id == 'ruan_mei' for u in units):
            ai_registry['ruan_mei'] = _ruanmei_ai
    # ── v6.9 批2: 知更鸟/不死途 ──
    if any(u.char.id in ('robin', 'busitu') for u in units):
        _register_v69b2_hooks(state.skill_hooks)
        if any(u.char.id == 'robin' for u in units):
            ai_registry['robin'] = _robin_ai
        if any(u.char.id == 'busitu' for u in units):
            ai_registry['busitu'] = _busitu_ai
        # 不死途天赋: 初始2充能
        for u in units:
            if u.char.id == 'busitu':
                u.extra['busitu_charge'] = 2
    # ── v6.9 批3: 千冶·刃 ──
    if any(u.char.id == 'qianye' for u in units):
        _register_v69b3_hooks(state.skill_hooks)
        ai_registry['qianye'] = _qianye_ai
    # ── v6.10 黄泉 ──
    if any(u.char.id == 'acheron' for u in units):
        _register_v610_hooks(state.skill_hooks)
        ai_registry['acheron'] = _acheron_ai
        # 天赋事件: 任意施放者使敌陷入负面→+1残梦+集真赤
        state.hooks.register('acheron', 'on_debuff_applied',
                             _acheron_talent_on_debuff, source_name='黄泉天赋')
    # ── v6.10 飞霄 ──
    if any(u.char.id == 'feixiao' for u in units):
        _register_v610b2_hooks(state.skill_hooks)
        ai_registry['feixiao'] = _feixiao_ai
        for u in units:
            if u.char.id == 'feixiao':
                u.extra['feixiao_fly'] = 3  # 行迹1: 开局3飞黄
                u.extra['feixiao_fua_used'] = False

    # ── 记忆子系统 ──
    remembrance = None
    if mechanics.has("remembrance"):
        from engine.systems.remembrance import RemembranceSystem
        remembrance = RemembranceSystem()
        # 注册记忆角色AI
        for u in units:
            if u.char.id == "xiadie":
                ai_registry["xiadie"] = lambda unit, state, **ctx: (
                    remembrance.xiadie_ai(unit, state, **ctx)
                )
            elif u.char.id == "changyeyue":
                def _cy_ai(unit, state, **ctx):
                    if unit.current_energy >= unit.char.max_energy:
                        _use_skill(unit, state, "ultimate")
                    else:
                        _use_skill(unit, state, "skill")
                ai_registry["changyeyue"] = _cy_ai
            elif u.char.id == "xilian":
                ai_registry["xilian"] = lambda unit, state, **ctx: (
                    remembrance.xilian_ai(unit, state, **ctx)
                )
            elif u.char.id == "fengjin":
                ai_registry["fengjin"] = lambda unit, state, **ctx: (
                    remembrance.fengjin_ai(unit, state, **ctx)
                )
            elif u.char.id == "aglaea":
                ai_registry["aglaea"] = lambda unit, state, **ctx: (
                    remembrance.aglaea_ai(unit, state, **ctx)
                )
            elif u.char.id == "trailblazer_remembrance":
                ai_registry["trailblazer_remembrance"] = lambda unit, state, **ctx: (
                    remembrance.tbr_ai(unit, state, **ctx)
                )
            elif u.char.id == "robin_summeretto":
                ai_registry["robin_summeretto"] = lambda unit, state, **ctx: (
                    remembrance.qingge_ai(unit, state, **ctx)
                )

    # 通用AI注册：非欢愉角色通过此入口注册
    _register_generic_ai(ai_registry, units)

    # 遗器开局效果
    p1 = next((u for u in units if u.position == 1), None)
    for u in units:
        if u.position != 1 and p1 and "not_first_slot_atk_buff" in getattr(u, '_active_relic_conditions', set()):
            p1.base_stats.ATK += p1.base_stats._base_ATK * 0.12
            state.log.append(f'[Init] 露莎卡({u.char.name}->{p1.char.name}): 1号位ATK+12%')

    # 战斗初始化
    if elation:
        elation.init_battle(state, units)
    if remembrance:
        remembrance.init_battle(state, units)

    # v6.7b: 大丽花天赋先于 on_enter_battle——E2 入场败谢/行迹1 需共舞者已绑定
    if any(x.char.id == 'the_dahlia' for x in units):
        _dahlia_talent_open(state)
    # Hook: 进入战斗（所有角色初始化完成后触发，含行迹开局效果）
    for u in units:
        state.hooks.trigger(u.char.id, "on_enter_battle", u=u, state=state)
    _acheron_apply_entry_effects(state)
    # v5.0 P3: 光锥战斗开始事件（event_battle_start 缓冲器）
    for u in units:
        _process_lc_effects(u, state, "on_battle_start")
    # 首波同样是波次开始。此前仅在 _respawn_wave() 派发，导致“每波次开始”
    # 的光锥效果从第二波才生效。
    for u in units:
        if u.is_alive:
            _process_lc_effects(u, state, "on_wave_start")
            state.hooks.trigger(u.char.id, "on_wave_start", u=u, state=state)

    # v5.2: 队伍上下文静态遗器条件（仙舟/龙骨全队增益 + 坠星同阵营CD）
    # 静态层(compute_combat_stats)单角色无队伍信息, 此处补全队传播。幂等。
    _apply_team_static_relics(state)
    # v5.2 问题3c: 星体差分机"首击CR+60%"——记录实际加成量, 首次攻击后扣回
    for u in units:
        conds = getattr(u, '_active_relic_conditions', set()) or set()
        if 'cd_threshold_first_atk_cr' in conds and u.base_stats.CRIT_DMG >= 1.20:
            u.extra['diff_machine_cr'] = min(1.0, u.base_stats.CRIT_RATE + 0.60) \
                - u.base_stats.CRIT_RATE

    # 初始 AV（含达成顺序戳）。战斗开始事件已施加的光锥 Buff 及状态条件
    # 需要战斗上下文参与求值；开局拉条在 navs 尚未创建时由遗器处理器暂存比例。
    unit_next_avs = {}
    for i, u in enumerate(units):
        initial_av = AV_PER_TURN / _effective_spd(u, state)
        advance_ratio = u.extra.pop('initial_action_advance_ratio', 0.0)
        unit_next_avs[i] = max(0.0, initial_av * (1.0 - advance_ratio))
    state.extra['navs'] = unit_next_avs  # 供光锥处理器拉条使用
    state.extra['stamp_counter'] = 0
    state.extra['av_stamp'] = {}
    for i in unit_next_avs:
        state.extra['stamp_counter'] += 1
        state.extra['av_stamp'][i] = state.extra['stamp_counter']
    # X 轴额外回合队列 + 常规回合暂存
    state.extra['extra_turns'] = []
    state.extra['pending_turn'] = None
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    state.extra['turn_hold_guard'] = 0
    if state.enemies:
        state.extra['enemy_blueprint'] = copy.deepcopy(state.enemies[0])
        # v6.5: 异构敌人模板列表（波次重生按列表逐模板重建）
        if len(state.enemies) > 1 and enemy_templates:
            state.extra['enemy_blueprints'] = [copy.deepcopy(e) for e in state.enemies]
        state.extra['num_enemies'] = num_enemies
        state.extra['wave'] = 1
        # 敌方行动条初始 AV（与角色同规则; 角色先打 stamp → 同AV敌方后动）
        for i, e in enumerate(state.enemies):
            _set_av(state, state.extra.get('navs', {}), ('e', i),
                    AV_PER_TURN / max(e.SPD, 1.0))
    # 每局 AI 注册表 + 子系统引用（供 _begin_regular_turn 使用）
    state.ai_registry = dict(ai_registry)
    state.extra['_elation'] = elation
    state.extra['_rem_sys'] = remembrance
    state.extra['_max_av'] = max_av

    # v6.3.0: 秘技系统（support 全开 + battle_start 开怪者; 用户 2026-08-14 确认语义）
    _cipher_ensure_laozhuke(state)
    from engine.core.combat_utils import apply_techniques
    apply_techniques(state, units)
    # v6.3.0b P1-8: 银狼行迹1·生成 开局激活（缺陷+1回合常驻; 击破植入走 on_any_weakness_break）
    for u in units:
        if u.char.id == 'silver_wolf' and any(
                getattr(t, 'hook_name', '') == 'silver_wolf_trace1_gen'
                for t in (u.char.traces or [])):
            u.extra['silver_wolf_trace1'] = True
    # v6.3.0b P1-9: 银狼E2「敌方进入战斗受伤+20%」——初始波与重生波统一施加
    sw_e2 = next((x for x in units if x.char.id == 'silver_wolf'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if sw_e2:
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.20
        state.log.append('  银狼E2: 敌方进入战斗受伤+20%')
    # ---- 主循环（第四象限模型）----
    state.log.append(f'[DEBUG] 主循环开始: max_av={max_av}, units={len(units)}, enemies={len(state.enemies)}')
    for i, u in enumerate(units):
        state.log.append(f'[DEBUG]   {u.char.name}({u.char.id}): SPD={u.base_stats.SPD:.0f} HP={u.current_hp:.0f} alive={u.is_alive}')
    while True:
        # ① X轴队列非空 → 执行队头（从左往右）
        if state.extra['extra_turns']:
            unit, kind = state.extra['extra_turns'].pop(0)
            if not (unit.is_alive if hasattr(unit, 'is_alive') else unit.is_alive()):
                continue
            state.turn_count += 1
            _exec_extra_turn(state, unit, kind)
            continue
        # ② X轴清空 → 回到被挤出的常规回合
        pu = state.extra['pending_turn']
        if pu is not None:
            state.extra['pending_turn'] = None
            if pu.is_alive:
                _resume_regular_turn(state, pu)
            continue
        # ②b v6.4c 精英双动第二行动（X轴已排空 = 玩家终结技/破韧打断窗口已过）
        if _enemy_pending_step(state):
            continue
        # ③ 时间窗退出（X队列已排空）
        if state.current_av >= max_av:
            break
        # 全队阵亡检查
        if not any(u.is_alive for u in state.units):
            state.log.append('=== 全队阵亡, 模拟结束 ===')
            break
        # 波次刷新
        if not state.alive_enemies() and state.extra.get('enemy_blueprint'):
            _respawn_wave(state)
        # ④ Y轴: 合并角色+忆灵+敌方的后到先动选择
        actor, actor_av = _next_y_actor(state)
        # v6.6 白厄: 卡厄斯兰那额外回合（均分排程, 优先于其他单位）
        ph = next((x for x in state.units if x.char.id == 'phainon'
                   and x.extra.get('kasier') and x.is_alive), None)
        # v6.6b P1-5: 卡厄斯兰那额外回合不得越过 max_av（此前分支先于时间窗检查）
        if ph and ph.extra.get('kasier_next_av', 1e18) <= actor_av \
                and ph.extra.get('kasier_next_av', 1e18) < max_av:
            state.current_av = ph.extra['kasier_next_av']
            _phainon_kasier_act(state, ph)
            continue
        if actor is None or actor_av >= max_av:
            break
        # 阿哈
        if elation and elation.check_aha(state, actor_av, max_av):
            state.current_av = state.aha_next_av
            state.turn_count += 1
            elation.execute_aha(state)
            continue
        state.current_av = actor_av
        if isinstance(actor, Enemy):
            # 敌方: 常规回合（攻击）
            _begin_enemy_turn(state, actor)
        elif isinstance(actor, SimUnit):
            # 角色: 常规回合
            _begin_regular_turn(state, actor)
        elif isinstance(actor, TimelineMarker):
            # v5.3 行动条标记（浮元/倒计时）: 更新行动条后执行行动
            state.turn_count += 1
            sys = state.extra.get('_marker_sys')
            if sys:
                sys.handle_action(state, actor)
        else:
            # Y 轴忆灵: 更新 AV+stamp 后行动
            state.turn_count += 1
            key = ('ms', id(actor))
            # 遐蝶行迹2·倒置的火炬: 遐蝶HP≥50%时死龙SPD+40%；
            # 焰息击杀后的+100%仅消耗于死龙下一次行动。
            spd = _memsprite_action_speed(state, actor)
            if getattr(actor.data, 'name', '') == '死龙':
                actor.extra.pop('xiadie_spd_boost', None)
            _set_av(state, state.extra.get('navs', {}), key,
                    state.current_av + AV_PER_TURN / max(spd, 1.0))
            rem = state.extra.get('_rem_sys')
            if rem:
                rem.handle_memsprite_action(state, actor, regular_turn=True)

    state.log.append(f'\n=== 模拟结束 AV={state.current_av:.0f} {state.turn_count}回合 ===')
    # 轮次统计: 以队内最慢存活角色行动值为一轮的近似（竞速语义）
    alive_spds = [u.base_stats.SPD for u in state.units if u.is_alive]
    state.cycles = int(state.current_av * min(alive_spds) / AV_PER_TURN) if alive_spds else 0
    return state


def _default_ai(u: SimUnit, state: SimState):
    if u.current_energy >= u.char.max_energy and u.char.max_energy > 0 \
            and not _hn_realm_blocks_ult(state, u):  # v7.2.0 裁决A
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


# ════════ v6.6 批1: 缇宝/刻律德菈/丹恒·腾荒（角色技能介绍/同谐、存护）════════

# ── 缇宝（同谐·量子）──

def _tribbie_apply_shenqi(u, state, turns=3):
    """【神启】: 全队全属性抗性穿透+24%（回合开始-1）
    v6.6c P1: 防重入——重复战技只刷新回合, 不再叠加穿透（此前每次+0.24永久漂移）"""
    if u.extra.get('tribbie_shenqi_turns', 0) > 0:
        u.extra['tribbie_shenqi_turns'] = max(u.extra.get('tribbie_shenqi_turns', 0), turns)
        state.log.append(f'  【神启】刷新 ({u.extra["tribbie_shenqi_turns"]}回合)')
        return
    u.extra['tribbie_shenqi_turns'] = turns
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.RES_PEN_ALL = getattr(eu.base_stats, 'RES_PEN_ALL', 0.0) + 0.24
    state.log.append(f'  【神启】: 全队全抗穿透+24% ({turns}回合)')


def _tribbie_remove_shenqi(u, state):
    """神启到期: 回减全队穿透"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.RES_PEN_ALL = max(0.0, getattr(eu.base_stats, 'RES_PEN_ALL', 0.0) - 0.24)
    u.extra['tribbie_shenqi_turns'] = 0
    state.log.append('  【神启】到期: 全队全抗穿透-24%')


def _tribbie_ult_field(u, state):
    """缇宝结界: 敌受伤+30% 2回合 + 受击后对最高HP目标附加12%HP量子伤
    v6.6c P1: 防重入——重复终结技只刷新回合, 不再叠加易伤（此前每次+0.30永久漂移）"""
    if state.extra.get('tribbie_field_turns', 0) > 0:
        state.extra['tribbie_field_turns'] = 2
        state.log.append('  缇宝结界刷新 (2回合)')
        return
    state.extra['tribbie_field_turns'] = 2
    for e in state.enemies:
        e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
    state.log.append('  缇宝结界: 敌受伤+30% (2回合)')


def _tribbie_remove_field(u, state):
    """结界到期: 回减敌易伤"""
    for e in state.enemies:
        e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.30)
    state.extra['tribbie_field_turns'] = 0
    state.log.append('  缇宝结界到期: 敌受伤-30%')


def _tribbie_field_extra_damage(state, u, hit_targets, total_dmg=0.0):
    """结界受击附加: 每有1名目标受到攻击→对被攻击目标中最高HP者1次12%HP量子伤
    （E2: ×1.2+额外1次; E1: 附加目标真伤=本次攻击总伤害24%）
    v6.8.1: 目标限定被攻击集合（此前从全体存活敌选）+按受击目标数结算次数;
    E1 真伤基数改 total_dmg（此前=附加伤害24%, 量级错误）。"""
    if state.extra.get('tribbie_field_turns', 0) <= 0:
        return
    targets = [t for t in (hit_targets or []) if t is not None and t.HP > 0]
    if not targets:
        return
    stats = _build_effective_stats(u, state)
    total = 0.0
    for _ in range(len(targets)):  # txt: 每有1名目标受到攻击→1次
        live = [t for t in targets if t.HP > 0]
        if not live:
            break
        target = max(live, key=lambda e: e.HP)
        d = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0 * (1.2 if u.eidolon_rank >= 2 else 1.0),
                             'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
        _, killed = _commit_enemy_damage(state, u, target, d.final_damage)
        u.total_damage_dealt += d.final_damage
        total += d.final_damage
        if killed:
            continue

        if u.eidolon_rank >= 2:
            before = target.HP

            d2 = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0 * 1.2,
                                  'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
            _, killed = _commit_enemy_damage(state, u, target, d2.final_damage)
            u.total_damage_dealt += d2.final_damage
            total += d2.final_damage
            if killed:
                continue

        # 献予「门径」之诗: 结界附加伤害额外+1次（v6.6c P2 实装消费）
        if u.extra.get('poem_menjing'):
            before = target.HP

            d3 = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0,
                                  'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                                  crit_mode='expected')
            _, killed = _commit_enemy_damage(state, u, target, d3.final_damage)
            u.total_damage_dealt += d3.final_damage
            total += d3.final_damage
            if killed:
                continue

        # E1: 附加目标真伤 = 本次攻击总伤害×24%（txt:5）
        if u.eidolon_rank >= 1:
            before = target.HP

            td = total_dmg * 0.24
            _, killed = _commit_enemy_damage(state, u, target, td,
                                             damage_type='true_damage',
                                             record_cipher=False)
            u.total_damage_dealt += td
            total += td
            if killed:
                continue

    state.log.append(f'  缇宝结界附加: {total:.0f} ({len(targets)}名受击×12%HP)')


def _tribbie_talent_fua(state, u):
    """天赋: 队友终结技→FUA 18%HP群伤（每角色1次/缇宝终结技重置）
    v6.8.1: E6「天赋FUA伤害+729%」实装（常驻×8.29）;
    行迹1「FUA后增伤72%×3层3回合」改 TimedBuff 滚动（此前只写 tribbie_talent_stack 无消费点）"""
    if u.char.id != 'tribbie':
        return
    stats = _build_effective_stats(u, state)
    e6_mult = 8.29 if u.eidolon_rank >= 6 else 1.0  # 1 + 729%
    total = 0.0
    for e in state.alive_enemies() or state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e), stats.HP, 18.0 * e6_mult,
                             'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        total += d.final_damage

    u.total_damage_dealt += total
    # 行迹1: 增伤72%×层数 3回合（TimedBuff 滚动, 消费走通用面板）
    # v6.8.2 极简会话: buff 已过期（被回合边界移除）时从 0 层重新叠
    # （Harness 修: 原 if u.is_alive 块缩进错误, 死亡路径 stacks 未定义会 UnboundLocalError）
    active = next((b for b in u.buffs if getattr(b, 'param_id', '') == 'tribbie_trace1_stack'), None)
    stacks = min(3, (u.extra.get('tribbie_talent_stack', 0) if active else 0) + 1)
    u.extra['tribbie_talent_stack'] = stacks
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'tribbie_trace1_stack']
    u.buffs.append(TimedBuff(source_id='tribbie', attributes={'DMG_BONUS_ALL': 72.0 * stacks},
                             remaining_turns=3, param_id='tribbie_trace1_stack',
                             source_name='行迹1·增伤'))
    state.log.append(f'  缇宝天赋FUA: {total:.0f}(18%HP×{e6_mult:.2f}E6) 行迹1增伤{stacks}/3层')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


# ── 刻律德菈（同谐·风）──

def _cerydra_grant_jungong(state, u, target):
    """【军功】: 目标获军功（仅最新）
    v6.8.1: 换目标清除旧军功者状态（标记/爵位属性对称回退/行迹3 SPD 迁移）——
    此前旧目标仍可触发军功 FUA/回能且升变穿透不回退。"""
    old = u.extra.get('cerydra_jungong_id')
    if old and old != target.char.id:
        old_t = next((x for x in state.units if x.char.id == old), None)
        if old_t:
            old_t.extra['cerydra_jungong'] = False
            if old_t.extra.get('cerydra_juewei'):
                _cerydra_qixi(state, u, old_t)  # 对称回减升变属性（含律法暴伤）
            else:
                old_t.extra['cerydra_juewei'] = False
            # 行迹3 SPD buff 迁移到新军功者（buff 生效期内）
            if u.extra.get('cerydra_spd_buff_turns', 0) > 0 \
                    and u.extra.get('cerydra_spd_buff_ally') == old:
                old_t.base_stats.SPD -= 20
                target.base_stats.SPD += 20
                u.extra['cerydra_spd_buff_ally'] = target.char.id
                state.log.append(f'  行迹3: SPD buff 迁移 {old_t.char.name}→{target.char.name}')
        u.extra['cerydra_charge'] = 0  # 换目标充能归零
        state.log.append('  军功换目标: 旧目标状态清除+充能归零')
    u.extra['cerydra_jungong_id'] = target.char.id
    was_juewei = bool(target.extra.get('cerydra_juewei'))

    target.extra['cerydra_jungong'] = True
    target.extra['cerydra_juewei'] = False
    if was_juewei:
        target.extra['cerydra_juewei'] = True

    u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 1)
    state.log.append(f'  【军功】: {target.char.name} +充能{u.extra["cerydra_charge"]}/8')
    _cerydra_check_promote(state, u, target)


def _cerydra_check_promote(state, u, target):
    """充能≥6: 升【爵位】（解控+战技CD+72%+全抗穿透10%）"""
    if target.extra.get('cerydra_juewei') or u.extra.get('cerydra_charge', 0) < 6:
        return
    target.extra['cerydra_juewei'] = True
    # v6.6c P1: 实装升变效果——全抗穿透+10%+暴伤72%。
    target.base_stats.RES_PEN_ALL = getattr(target.base_stats, 'RES_PEN_ALL', 0.0) + 0.10
    target.base_stats.CRIT_DMG += 0.72
    target.extra['cerydra_rank_title_cd'] = True
    if u.extra.get('poem_lvfa'):
        target.base_stats.CRIT_DMG += 0.30  # 献予「律法」: 军功者暴伤+30%
    # 解控（清控制类状态）
    target.statuses = [s for s in target.statuses
                       if getattr(s, 'category', '') != 'control']
    u.extra['cerydra_juewei_target'] = target.char.id
    state.log.append(f'  【爵位】: {target.char.name} 升变(解控+战技CD+72%+全抗穿透10%)')


def _cerydra_qixi(state, u, target):
    """奇袭: 爵位者战技后触发——消耗6充能降回军功（v6.6c: 对称回减穿透/律法暴伤 + 律法充能+1）"""
    if not target.extra.get('cerydra_juewei'):
        return
    u.extra['cerydra_charge'] = max(0, u.extra.get('cerydra_charge', 0) - 6)
    target.extra['cerydra_juewei'] = False
    target.base_stats.RES_PEN_ALL = max(0.0, getattr(target.base_stats, 'RES_PEN_ALL', 0.0) - 0.10)
    if target.extra.pop('cerydra_rank_title_cd', False):
        target.base_stats.CRIT_DMG = max(0.0, target.base_stats.CRIT_DMG - 0.72)
    if u.extra.get('poem_lvfa'):
        target.base_stats.CRIT_DMG -= 0.30
        u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 1)
        state.log.append('  献予「律法」: 奇袭后充能+1')
    u.extra.pop('cerydra_juewei_target', None)
    state.log.append(f'  奇袭结束: 【爵位】→【军功】(充能{u.extra["cerydra_charge"]}/8)')


def _cerydra_jungong_target(state, u):
    """当前军功持有者（仅最新）"""
    jid = u.extra.get('cerydra_jungong_id')
    if jid:
        return next((x for x in state.units if x.char.id == jid and x.is_alive), None)
    return None


# ── 丹恒·腾荒（存护·物理）──

def _dht_apply_shield(state, u, amount_pct, flat, source):
    """全队护盾: 20%ATK+400（叠加上限=战技护盾量300%）"""
    stats = _build_effective_stats(u, state)
    source_skill = 'skill' if source == '渊渟岳峙' else 'talent'
    amt = ((stats.ATK * amount_pct / 100 + flat)
           * _skill_level_factor(u, source_skill))
    cap = ((stats.ATK * 0.20 + 400)
           * _skill_level_factor(u, 'skill'))  # 战技基础护盾为上限基准
    for eu in state.units:
        if eu.is_alive:
            eu.shield = min(cap * 3.0, getattr(eu, 'shield', 0.0) + amt)
    state.log.append(f'  {source}: 全队护盾+{amt:.0f} (上限{cap*3:.0f})')


def _dht_summon_longling(state, u, target):
    """【龙灵】召唤: 165速行动条标记（行动=解控+护盾10%ATK+200）
    v6.6c P1: 修复 spawn 签名（此前 4 参 TypeError/静默不召唤）"""
    if u.marker and u.marker.is_alive:
        return
    sys = _ensure_marker_system(state)
    sys.spawn(state, u, 'dht_longling')
    state.log.append('  召唤【龙灵】(165速)')


def _dht_longling_action(state, marker):
    """龙灵行动: 解控+护盾10%ATK+200; 强化→追加攻击80%ATK+同袍80%附加
    v6.6c P1: 签名对齐 action_handlers(state, marker); 净化修复（此前 [:] 空列表 no-op）"""
    u = next((x for x in state.units
              if x.char.id == marker.summoner_id and x.is_alive), None)
    if u is None:
        return
    _dht_apply_shield(state, u, 20.0 if u.eidolon_rank >= 2 else 10.0,
                      400 if u.eidolon_rank >= 2 else 200, '龙灵')
    stats = _build_effective_stats(u, state)
    # 行迹3：龙灵行动时给当前护盾最低的存活队友追加护盾。
    if any(getattr(t, 'hook_name', '') == 'dht_trace3' for t in (u.char.traces or [])):
        candidates = [x for x in state.units if x.is_alive]
        if candidates:
            target = min(candidates, key=lambda x: getattr(x, 'shield', 0.0))
            cap = stats.ATK * 0.20 + 400.0
            target.shield = min(cap * 3.0,
                                target.shield + stats.ATK * 0.05 + 100.0)
    # 净化我方负面（控制+负面状态）
    for eu in state.units:
        if eu.is_alive and eu.statuses:
            before = len(eu.statuses)
            eu.statuses = [s for s in eu.statuses
                           if getattr(s, 'category', '') not in ('control', 'debuff')]
            if len(eu.statuses) < before:
                state.log.append(f'  龙灵净化: {eu.char.name} 解除{before - len(eu.statuses)}个负面')
    # 终结技强化: 追加攻击80%ATK + 同袍80%附加（每次行动消耗1层）
    attacked = False  # v7.1.0 P1: marker行动是否构成攻击(供晴歌气氛触发)
    if u.extra.get('dht_longling_enhanced', 0) > 0:
        u.extra['dht_longling_enhanced'] -= 1
        alive = state.alive_enemies()
        total = 0.0
        tong = next((x for x in state.units
                     if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
        tong_stats = _build_effective_stats(tong, state) if tong else None
        for e in alive:
            d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK,
                                 80.0, 'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='skill', attack_type='follow_up',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, e, d.final_damage)
            total += d.final_damage
            if tong_stats is not None and e.HP > 0:
                attached_scale = 160.0 if u.eidolon_rank >= 2 else 80.0
                attached = calculate_damage(
                    tong_stats, _enemy_for_damage(e), tong_stats.ATK, attached_scale,
                    'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                    skill_type='skill', attack_type='follow_up', crit_mode='expected')
                _commit_enemy_damage(state, u, e, attached.final_damage)
                total += attached.final_damage
        # 行迹3：强化龙灵对当前生命最高的敌人追加同袍攻击力40%。
        highest = max((x for x in alive if x.HP > 0), key=lambda x: x.HP, default=None)
        has_trace3 = any(getattr(t, 'hook_name', '') == 'dht_trace3'
                         for t in (u.char.traces or []))
        if has_trace3 and highest is not None and tong_stats is not None:
            extra = calculate_damage(
                tong_stats, _enemy_for_damage(highest), tong_stats.ATK, 40.0,
                'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                skill_type='skill', attack_type='follow_up', crit_mode='expected')
            _commit_enemy_damage(state, u, highest, extra.final_damage)
            total += extra.final_damage
        u.total_damage_dealt += total
        state.log.append(f'  龙灵强化攻击: {total:.0f}(剩余强化{u.extra["dht_longling_enhanced"]}次)')
        attacked = attacked or total > 0
    # 献予「大地」之诗: 龙灵3次攻击附加同袍护盾80%伤害
    if u.extra.get('poem_dadi_attacks', 0) > 0:
        u.extra['poem_dadi_attacks'] -= 1
        tong = next((x for x in state.units
                     if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
        if tong and getattr(tong, 'shield', 0) > 0:
            dmg = tong.shield * 0.80
            t = next(iter(state.alive_enemies() or []), None)
            if t:
                _commit_enemy_damage(state, u, t, dmg)
                u.total_damage_dealt += dmg
                state.log.append(f'  献予「大地」: 龙灵附加{dmg:.0f}(同袍盾80%)')
                attacked = True
    state.log.append('  龙灵行动: 解控+护盾')
    _qingge_notify_attack(state, u, dealt=attacked)  # v7.1.0 P1: marker行动攻击补气氛


# v6.6c P1: 龙灵行动注册（函数定义在文件后部, 追加注册防 NameError）
MARKER_ACTIONS['dht_longling'] = _dht_longling_action


# ════════ v6.6 批2: 海瑟音/那刻夏/赛飞儿（角色技能介绍/虚无、智识）════════

# ── 海瑟音（虚无·物理）──

HYSILENS_DOTS = [
    ('风化', '风', 25.0), ('灼烧', '火', 25.0), ('触电', '雷', 25.0),
    ('裂伤', '物理', 0.0),  # 裂伤按敌HP 20% 封顶 25%ATK
]


def _hysilens_apply_dot(state, u, target, count=1, e1_double=False):
    """海瑟音天赋/秘技: 挂随机DOT（优先不同状态）
    v6.6c: 存快照供敌方回合结算; 海洋诗立即结算60%。
    v6.8.1: E1 116% 改在 DOT 结算统一乘（全队持续伤害口径, 此前挂时乘只覆盖海瑟音自己）;
    E1「额外陷入一次」=天赋路径双挂（e1_double）。"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if e1_double and u.eidolon_rank >= 1:
        count = 2
    snap = copy.deepcopy(_build_effective_stats(u, state))
    for _ in range(count):
        import random
        # v6.8.1: pool 随挂载更新（此前循环前算一次, 多挂时同状态覆盖只剩1种）
        existing = {s.name for s in target.statuses if s.id.startswith('hysilens_dot')}
        pool = [d for d in HYSILENS_DOTS if d[0] not in existing] or HYSILENS_DOTS
        name, elem, mult = random.choice(pool)
        target.add_status(EnemyStatus(
            id=f'hysilens_dot_{name}', name=name, category='dot',
            source='hysilens', remaining_turns=2,
            attributes={'element': elem, 'multiplier': mult,
                        'dot_snapshot': snap,
                        'dot_type': 'break' if name == '裂伤' else 'std'}))
        # 献予「海洋」之诗: DOT 立即结算60%
        if u.extra.get('poem_haiyang'):
            d = _hysilens_dot_damage(target, name, elem, mult, snap) * (1.16 if u.eidolon_rank >= 1 else 1.0)
            _commit_enemy_damage(state, u, target, d)
            u.total_damage_dealt += d
            if d > 0:
                state.log.append(f'  献予「海洋」: 立即结算{d:.0f}')
    state.log.append(f'  海瑟音DOT: {target.name or target.id} +{count}种')


def _hysilens_dot_damage(enemy, name, elem, mult, snap):
    """海瑟音单跳DOT伤害（裂伤=敌HP20%封顶25%ATK; 其余=ATK×倍率）"""
    if name == '裂伤':
        return min(enemy.HP * 0.20, snap.ATK * 0.25)
    d = calculate_damage(snap, _enemy_for_damage(enemy), snap.ATK, mult,
                         'dot', elem, 80, False)
    return d.final_damage


def _tick_hysilens_dot(state, enemy, s):
    """v6.6c P1: 海瑟音DOT敌方回合跳伤（此前 hysilens_* 永不结算）"""
    snap = s.attributes.get('dot_snapshot')
    if not snap:
        return 0.0
    dmg = _hysilens_dot_damage(enemy, s.name,
                               s.attributes.get('element', '物理'),
                               s.attributes.get('multiplier', 25.0), snap)
    owner = next((x for x in state.units
                  if x.char.id == s.source and x.is_alive), None)
    _commit_enemy_damage(state, owner, enemy, dmg)
    if owner is not None:
        owner.total_damage_dealt += dmg
    state.log.append(f'  {s.name}: {dmg:.0f} → {enemy.name or enemy.id}')
    return dmg


def _hysilens_field(state, u, turns=3):
    """海瑟音结界: 敌ATK-15%/DEF-25% + DOT引爆
    v6.6c P1: 实装属性消费（_enemy_attack_stats/_enemy_for_damage 读 hysilens_field）;
    E4: 结界期敌全抗-20%"""
    state.extra['hysilens_field_turns'] = turns
    for e in state.enemies:
        e.extra['hysilens_field'] = True
        if u.eidolon_rank >= 4:
            if not e.extra.get('hysilens_e4_res'):
                for elem in list(e.element_res):
                    e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
                e.extra['hysilens_e4_res'] = True
    state.log.append(f'  海瑟音结界: 敌ATK-15%/DEF-25% ({turns}回合)' + (' + E4全抗-20%' if u.eidolon_rank >= 4 else ''))


def _hysilens_remove_field(state, u):
    for e in state.enemies:
        e.extra['hysilens_field'] = False
        if e.extra.pop('hysilens_e4_res', False):
            for elem in list(e.element_res):
                e.element_res[elem] = e.element_res.get(elem, 0) + 0.20
    state.extra['hysilens_field_turns'] = 0


def _hysilens_dot_trigger_v3(state, u, target):
    """v6.8.3: 海瑟音结界反打——立即结算 80%ATK 物理 DOT。
    旧实现写 hysilens_echo 状态但无 dot_snapshot, 下一回合结算恒为 0 且会自触发;
    这里不新增任何状态, 天然不可递归。触发次数由 _begin_enemy_turn 在每个敌方回合开始重置。"""
    if not target.extra.get('hysilens_field') or getattr(target, 'HP', 0) <= 0:
        return 0.0
    cap = 12 if u.eidolon_rank >= 6 else 8
    cnt = state.extra.get('hysilens_trigger_count', 0)
    if cnt >= cap:
        return 0.0
    state.extra['hysilens_trigger_count'] = cnt + 1
    mult = 80.0 * (1.2 if u.eidolon_rank >= 6 else 1.0) * (1.16 if u.eidolon_rank >= 1 else 1.0)
    stats = _build_effective_stats(u, state)
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, mult,
                         'dot', '物理', 80, False)
    _commit_enemy_damage(state, u, target, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  噬魂回响: {d.final_damage:.0f} → {target.name or target.id} ({cnt+1}/{cap})')
    return d.final_damage



# ── 那刻夏（智识·风）──

WEAKNESS_ELEMENTS = ['物理', '火', '冰', '雷', '风', '量子', '虚数']


def _enemy_weakness_elements(target):
    """Return every live weakness, including natural and implanted sources."""
    elements = {element for element in WEAKNESS_ELEMENTS
                if target.element_res.get(element, 0.20) <= 0.0}
    elements.update(status.attributes.get('weakness_element')
                    for status in getattr(target, 'statuses', [])
                    if status.attributes.get('weakness_element'))
    return elements


def _anaxa_apply_entry_effects(state, u):
    """那刻夏E2：每个敌人进入波次时添加1个弱点并全抗降低20%。"""
    if u is None or u.char.id != 'anaxa' or u.eidolon_rank < 2:
        return
    from engine.models.enemy import EnemyStatus
    for enemy in state.enemies:
        if getattr(enemy, 'HP', 0) <= 0 or enemy.extra.get('anaxa_e2_applied'):
            continue
        _anaxa_add_weakness(state, u, enemy)
        enemy.add_status(EnemyStatus(
            id='anaxa_e2_res_down', name='那刻夏E2', category='debuff', source='anaxa',
            remaining_turns=-1, attributes={'res_down': 0.20},
        ))
        enemy.extra['anaxa_e2_applied'] = True
    if state.enemies:
        state.log.append('  那刻夏E2: 敌入场添加弱点+全抗-20%')


def _anaxa_add_weakness(state, u, target):
    """天赋: 每击中+1随机弱点（3回合, 优先未有）"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    # 自然弱点参与“质性揭露”和行迹3计数，但天赋的“尚未拥有”标记
    # 仍以那刻夏自身已添加的弱点状态为准，避免天然全弱点目标把每次命中
    # 都刷新到同一个随机状态。
    existing = {status.attributes.get('weakness_element')
                for status in target.statuses if status.id.startswith('anaxa_weak')}
    pool = [el for el in WEAKNESS_ELEMENTS if el not in existing] or WEAKNESS_ELEMENTS
    import random
    elem = random.choice(pool)
    existing_status = next((s for s in target.statuses
                            if s.id == f'anaxa_weak_{elem}'), None)
    old_res = (existing_status.attributes.get('weakness_old_res', target.get_res(elem))
               if existing_status else target.get_res(elem))
    current_res = target.get_res(elem)
    target.element_res[elem] = min(current_res, -0.2) if current_res > 0 else current_res
    target.add_status(EnemyStatus(
        id=f'anaxa_weak_{elem}', name='弱点', category='debuff',
        source='anaxa', remaining_turns=3,
        attributes={'weakness_element': elem, 'weakness_old_res': old_res}))
    state.log.append(f'  那刻夏弱点: {elem} (+1)')
    # v6.7 弱点植入事件（大丽花行迹3消费）
    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                            element=elem, target=target)


def _anaxa_reveal_check(state, u, target):
    """≥5不同弱点→【质性揭露】"""
    weaks = _enemy_weakness_elements(target)
    if len(weaks) >= 5:
        target.extra['anaxa_revealed'] = True
        state.log.append(f'  【质性揭露】: {target.name or target.id} ({len(weaks)}弱点)')


# ── 赛飞儿（虚无·量子）──

def _cipher_set_laozhuke(state, u, target):
    """设置唯一【老主顾】，并同步整场减防诗词的目标倍率。"""
    if target is None:
        return None
    for e in state.enemies:
        e.extra['cipher_laozhuke'] = e is target
    if u.extra.get('poem_guiji'):
        from engine.models.enemy import EnemyStatus
        for e in state.enemies:
            if getattr(e, 'HP', 0) <= 0:
                continue
            val = 0.20 if e is target else 0.12
            st = next((s for s in e.statuses if s.id == 'cipher_guiji_def'), None)
            if st is not None:
                st.attributes['def_reduction'] = val
            else:
                e.add_status(EnemyStatus(id='cipher_guiji_def', name='DEF降低',
                                         category='debuff', source='cipher',
                                         remaining_turns=-1,
                                         attributes={'def_reduction': val}))
    return target


def _cipher_pick_laozhuke(state, u):
    """【老主顾】: 生命上限最高者（仅最新）"""
    alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
    if not alive:
        return None
    target = max(alive, key=lambda e: getattr(e, 'max_hp', e.HP))
    return _cipher_set_laozhuke(state, u, target)


def _cipher_ensure_laozhuke(state):
    cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cp is None:
        return None
    if any(getattr(e, 'HP', 0) > 0 and e.extra.get('cipher_laozhuke')
           for e in state.enemies):
        return None
    return _cipher_pick_laozhuke(state, cp)


def _cipher_record(state, u, target, dmg, is_laozhuke=None, *,
                   rate_multiplier=1.0, extra_rate=0.0):
    """记录伤害: 老主顾12%/其他8%（不含溢出; 行迹1速度档加成）"""
    if dmg <= 0:
        return
    if is_laozhuke is None:
        is_laozhuke = bool(target.extra.get('cipher_laozhuke'))
    rate = 0.12 if is_laozhuke else 0.08
    rate *= _skill_level_factor(u, 'talent')
    effective = _build_effective_stats(u, state)
    spd = effective.SPD + effective._base_SPD * effective.SPD_PERCENT
    if spd >= 170:
        rate *= 2.0
    elif spd >= 140:
        rate *= 1.5
    if u.eidolon_rank >= 1:
        rate *= 1.5
    rate = rate * max(float(rate_multiplier), 0.0) + max(float(extra_rate), 0.0)
    u.extra['cipher_record'] = u.extra.get('cipher_record', 0.0) + dmg * rate


def _tb_skill_aftermath(state, u, skill_key):
    """v6.10.3 P1-4: 开拓者·欢愉战技/天赋/行迹2内联接线:
    - 战技: 获得20好活当赏; 持有好活时战技额外33%雷欢愉伤害(用全队最高好活层数计算)
    - 行迹2·阿哈咬它!: 我方目标施放欢愉技后→开拓者下次战技额外+2好活"""
    tb = next((x for x in state.units
               if x.char.id == 'trailblazer_elation' and x.is_alive), None)
    if tb is None:
        return
    has_trace2 = any(getattr(t, 'hook_name', '') == 'trailblazer_goodshow_boost'
                     for t in (tb.char.traces or []))
    if skill_key == 'elation_skill' and has_trace2:
        state.extra['tb_trace2_pending'] = True
        return
    if u is tb and skill_key == 'skill':
        elation = state.extra.get('_elation')
        if elation is None:
            from engine.systems.elation import ElationSystem
            elation = ElationSystem()
        elation.grant_good_show(state, 'trailblazer_elation', 20.0,
                                duration=2, source='trailblazer_skill')
        if state.extra.get('tb_trace2_pending') and has_trace2:
            state.extra.pop('tb_trace2_pending', None)
            elation.grant_good_show(state, 'trailblazer_elation', 2.0,
                                    duration=2, source='trailblazer_trace2')
            state.log.append('  开拓者行迹2: 下次战技额外+2好活')
        if state.elation_state.get_good_show_total('trailblazer_elation') > 0:
            best = max((state.elation_state.get_good_show_total(x.char.id)
                        for x in state.units if x.is_alive), default=0.0)
            stats = _build_effective_stats(tb, state)
            total = 0.0
            for e in (state.alive_enemies() or state.enemies):
                if getattr(e, 'HP', 0) <= 0:
                    continue
                talent_scale = 30.0 * _skill_level_factor(tb, 'talent')
                d = calculate_damage(stats, _enemy_for_damage(e), 0, talent_scale, 'elation',
                                     '雷', 80, stats.CRIT_RATE >= 0.5,
                                     laugh_n=best, crit_mode='expected')
                _commit_enemy_damage(state, tb, e, d.final_damage,
                                     damage_type='elation', skill_type='talent')
                total += d.final_damage
            tb.total_damage_dealt += total
            state.log.append(f'  开拓者天赋: 战技额外雷欢愉伤害{total:.0f}(最高好活{best:.0f})')


def _yaoguang_open_field(state, yao, *, source='skill'):
    """展开或刷新爻光结界；增益持续时间只跟随爻光自身回合。"""
    was_active = state.yao_field_active
    state.yao_field_active = False
    try:
        yao_stats = _build_effective_stats(yao, state)
    finally:
        state.yao_field_active = was_active
    state.yao_field_active = True
    state.yao_field_turns = 3
    state.extra['yaoguang_field_elation_bonus'] = yao_stats.ELATION_LEVEL * 0.20
    # 清理 Harness 旧实现遗留的受益者倒计时 Buff，避免双算或结界结束后残留。
    for unit in state.units:
        unit.buffs = [b for b in unit.buffs
                      if getattr(b, 'param_id', '') != 'yaoguang_field_elation']
    state.log.append(
        f'  爻光结界: 全队欢愉度+{yao_stats.ELATION_LEVEL * 20:.0f}%(3回合,{source})')


def _yaoguang_close_field(state):
    state.yao_field_active = False
    state.yao_field_turns = 0
    state.extra.pop('yaoguang_field_elation_bonus', None)
    for unit in state.units:
        unit.buffs = [b for b in unit.buffs
                      if getattr(b, 'param_id', '') != 'yaoguang_field_elation']


def _yaoguang_dajidali(state, u, skill_key, spent_skill_points=None):
    """v6.10.3 P1-3: 爻光天赋【大吉大利】——爻光持有【好活当赏】时, 我方目标攻击后
    对随机1个击中的目标额外造成1次20%对应属性欢愉伤害; 本次攻击消耗战技点则额外触发1次;
    攻击者欢愉度低于爻光时该次欢愉伤害使用爻光欢愉度计算"""
    yao = next((x for x in state.units if x.char.id == 'yaoguang' and x.is_alive), None)
    if yao is None or state.elation_state.get_good_show_total('yaoguang') <= 0:
        return
    hits = [t for t in state.extra.get('last_attack_targets', []) or []
            if t is not None and getattr(t, 'HP', 0) > 0]
    if not hits:
        return
    stats = _build_effective_stats(u, state)
    yao_elation = _build_effective_stats(yao, state).ELATION_LEVEL
    if stats.ELATION_LEVEL < yao_elation:
        stats = copy.deepcopy(stats)
        stats.ELATION_LEVEL = yao_elation
    laugh_n = state.elation_state.get_good_show_total(u.char.id)
    if spent_skill_points is None:
        skill = u.char.skills.get(skill_key)
        spent_skill_points = ((skill.cost or {}).get('skill_points', 0)
                              if skill is not None else 0)
    times = 2 if spent_skill_points > 0 else 1
    total = 0.0
    for _ in range(times):
        t = random.choice(hits)
        talent_scale = 20.0 * _skill_level_factor(yao, 'talent')
        d = calculate_damage(stats, _enemy_for_damage(t), 0, talent_scale, 'elation',
                             u.char.element, 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    if total > 0:
        state.log.append(f'  大吉大利: {total:.0f} ({"×2" if times == 2 else "×1"})')


def _cipher_e4_extra(state, cp, target):
    """赛飞儿E4: 【老主顾】受我方攻击→赛飞儿对其50%ATK量子附加伤害"""
    stats = _build_effective_stats(cp, state)
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, 50.0,
                         'additional', '量子', 80, False,
                         crit_mode='expected')
    _commit_enemy_damage(state, cp, target, d.final_damage,
                         damage_type='additional')
    cp.total_damage_dealt += d.final_damage
    state.log.append(f'  赛飞儿E4: 老主顾附加{d.final_damage:.0f}')


def _cipher_attack_aftermath(state, u, skill_key):
    """v6.10.3 P1-1: 赛飞儿攻击后接线（on_after_skill 之前调用）:
    - 天赋: 我方其他目标攻击命中【老主顾】→ 赛飞儿对老主顾 FUA 150%ATK（每回合1次, E6×4.5）
    - E1: 施放天赋FUA时 ATK+80% 持续2回合
    - E2: 赛飞儿击中敌方 → 120%基础概率易伤30% 2回合
    - E4: 【老主顾】受我方目标攻击（含赛飞儿）→ 50%ATK量子附加"""
    hit = [t for t in state.extra.get('last_attack_targets', []) or []
           if t is not None and getattr(t, 'HP', 0) > 0]
    if not hit:
        return
    cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cp is None:
        return
    if u.char.id == 'cipher':
        # E2: 自身攻击命中→易伤（同 ID 状态由 Enemy.add_status 刷新持续时间）
        if cp.eidolon_rank >= 2:
            for t in hit:
                if _roll_effect_hit(cp, state, t, '赛飞儿E2易伤', base_chance=1.20):
                    t.add_status(EnemyStatus(id='cipher_e2_vuln', name='易伤',
                                             category='debuff', source='cipher',
                                             remaining_turns=2,
                                             attributes={'vulnerability': 0.30}))
        # E4: 赛飞儿自己攻击老主顾也触发附加
        if cp.eidolon_rank >= 4:
            for t in hit:
                if t.extra.get('cipher_laozhuke'):
                    _cipher_e4_extra(state, cp, t)
        return
    # 队友攻击老主顾: 天赋FUA + E4附加
    lz = next((t for t in hit if t.extra.get('cipher_laozhuke')), None)
    if lz is None:
        return
    if cp.eidolon_rank >= 4:
        _cipher_e4_extra(state, cp, lz)
    if cp.extra.get('cipher_fua_used'):
        return
    cp.extra['cipher_fua_used'] = True
    # E1: 施放FUA时 ATK+80% 2回合（本次FUA即生效）
    if cp.eidolon_rank >= 1 and not cp.extra.get('cipher_e1_atk_buff'):
        cp.base_stats.ATK += cp.base_stats._base_ATK * 0.80
    cp.extra['cipher_e1_atk_buff'] = 2
    stats = _build_effective_stats(cp, state)
    scale = 150.0 * _skill_level_factor(cp, 'talent')
    if cp.eidolon_rank >= 6:
        scale *= 4.5
    d = calculate_damage(stats, _enemy_for_damage(lz), stats.ATK, scale,
                         'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                         skill_type='skill', attack_type='follow_up',
                         crit_mode='expected')
    _commit_enemy_damage(state, cp, lz, d.final_damage,
                         damage_type='direct', skill_type='talent',
                         attack_type='follow_up',
                         cipher_extra_rate=0.16 if cp.eidolon_rank >= 6 else 0.0)
    cp.total_damage_dealt += d.final_damage
    state.log.append(f'  猫咪怪盗FUA: {d.final_damage:.0f}(老主顾受击)')


def _sparkle_ult_sp(state):
    """v6.10.6 C3: 花火终结技回6战技点, 溢出记录≤10（TXT 花火.txt:39）"""
    cap = state.max_sp
    before = state.skill_points
    state.skill_points = min(cap, before + 6)
    overflow = max(0, (before + 6) - cap)
    state.extra['sparkle_sp_reserve'] = min(
        10, state.extra.get('sparkle_sp_reserve', 0) + overflow)
    state.log.append(f'  花火终结技: SP {before}→{state.skill_points} '
                     f'(溢出记录{state.extra["sparkle_sp_reserve"]:.0f})')


def _sparkle_turn_end_reserve(state, u):
    """v6.10.6 C3: 我方角色回合结束后, 花火消耗溢出记录补战技点至上限（TXT 花火.txt:39）"""
    reserve = state.extra.get('sparkle_sp_reserve', 0)
    if reserve <= 0 or state.skill_points >= state.max_sp:
        return
    need = state.max_sp - state.skill_points
    use = min(need, reserve)
    state.skill_points += use
    state.extra['sparkle_sp_reserve'] = reserve - use
    state.log.append(f'  花火记录: 回合结束补{use:.0f}SP')


def _huohuo_ruming_gain(state, u, turns):
    """v6.10.6 B1: 藿藿获得禳命——自身状态, 净化次数刷新6次（TXT 藿藿.txt:41-44）"""
    u.extra['huohuo_ruming_turns'] = turns
    u.extra['huohuo_ruming_cleanse'] = 6
    state.log.append(f'  禳命: 藿藿获得禳命({turns}回合, 净化6次)')


def _huohuo_ruming_tick(state, u):
    """v6.10.6 B1: 藿藿自身常规回合开始——禳命持续回合-1（TXT: 藿藿每回合开始减1）"""
    if u.char.id != 'huohuo':
        return
    t = u.extra.get('huohuo_ruming_turns', 0)
    if t > 0:
        t -= 1
        if t <= 0:
            u.extra.pop('huohuo_ruming_turns', None)
            u.extra.pop('huohuo_ruming_cleanse', None)
            state.log.append('  禳命到期')
        else:
            u.extra['huohuo_ruming_turns'] = t


def _huohuo_ruming_heal_all(state, trigger_unit):
    """v6.10.6 B2/B3/B5: 藿藿持有禳命时, 触发单位回 4.5%藿藿生命上限+120,
    随后全队每个 HP≤50% 目标再回同量; 净化1负面(6次); 行迹3仅禳命治疗回1能量"""
    hh = next((x for x in state.units
               if x.char.id == 'huohuo' and x.is_alive), None)
    if hh is None or hh.extra.get('huohuo_ruming_turns', 0) <= 0:
        return
    heal = hh.max_hp * 0.045 + 120.0
    low = [x for x in state.units
           if x.is_alive and x is not trigger_unit
           and x.current_hp <= x.max_hp * 0.5]
    targets = [trigger_unit] + low if trigger_unit is not None and trigger_unit.is_alive else low
    seen = set()
    for t in targets:
        if id(t) in seen:
            continue
        seen.add(id(t))
        before = t.current_hp
        t.current_hp = min(t.max_hp, t.current_hp + heal)
        amount = t.current_hp - before
        if amount <= 0:
            continue
        # B3: 净化——解除目标1个负面（单次禳命6次, 再获禳命刷新）
        if hh.extra.get('huohuo_ruming_cleanse', 0) > 0:
            neg = [s for s in t.statuses
                   if getattr(s, 'category', '') in ('debuff', 'control')]
            if neg:
                t.statuses.remove(neg[0])
                hh.extra['huohuo_ruming_cleanse'] -= 1
                state.log.append(f'  禳命净化: {t.char.name}解除1负面(余{hh.extra["huohuo_ruming_cleanse"]}次)')
        # on_heal 钩子（藿藿E6/收容的暗潮等统一入口）
        state.hooks.trigger_all("on_heal", u=hh, state=state,
                                healer=hh, targets=[t], heal_amt=amount)
        # B5: 行迹3·怯惧应激——仅禳命治疗触发回1能量
        if any(getattr(tr, 'hook_name', '') == 'huohuo_energy_cycle'
               for tr in (hh.char.traces or [])):
            _gain_energy(hh, 1.0, state=state)
        state.log.append(f'  禳命治疗: {t.char.name}+{amount:.0f}')


def _cipher_trace3_apply_vuln(state):
    """v6.10.3 P1-2: 赛飞儿行迹3 敌方受伤+40%——对称维护（入场/新波）, 幂等标记防重复叠加"""
    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0 and not e.extra.get('cipher_trace3_vuln'):
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.40
            e.extra['cipher_trace3_vuln'] = True


def _cipher_trace3_remove_vuln(state):
    """v6.10.3 P1-2: 赛飞儿死亡/离场时移除行迹3 易伤（对称回减）"""
    for e in state.enemies:
        if e.extra.pop('cipher_trace3_vuln', False):
            e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.40)


# ════════ v6.6 批3: 白厄（毁灭·物理, 变身状态机）════════

def _phainon_gain_huozhong(state, u, amt):
    """白厄火种: 上限12+溢出3(E6无上限); 达到12激活终结技"""
    cap = 15 if u.eidolon_rank < 6 else 999
    u.extra['huozhong'] = min(cap, u.extra.get('huozhong', 0) + amt)
    state.log.append(f'  火种+{amt} → {u.extra["huozhong"]}/12')


def _phainon_gain_huishang(state, u, amt):
    """卡厄斯兰那毁伤"""
    u.extra['huishang'] = min(6, u.extra.get('huishang', 0) + amt)
    state.log.append(f'  毁伤+{amt} → {u.extra["huishang"]}/6')


def _phainon_trace3_atk_stack(state, u):
    """白厄行迹3·照见英雄本色: 进入战斗/变身结束时 ATK+50%, 最多叠加2层
    （v6.8.1: 归位——此前 v6.8b 误装到秘技, txt:99 行迹3）"""
    stacks = u.extra.get('phainon_atk_stacks', 0)
    if stacks >= 2:
        return
    u.extra['phainon_atk_stacks'] = stacks + 1
    u.buffs.append(TimedBuff(source_id='phainon',
                             attributes={'ATK_PERCENT': 50.0},
                             remaining_turns=-1, source_name='终结之始'))
    state.log.append(f'  白厄秘技: 攻击力+50% ({stacks + 1}/2层)')


def _apply_phainon_tech_wave(state, u):
    """白厄秘技·终结之始: 波次开始时全敌200%ATK物理伤害（v6.8b 补, txt 秘技; 仿流萤先例）"""
    stats = _build_effective_stats(u, state)
    total = 0.0
    for e in state.enemies:
        before = e.HP
        if before <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK, 200.0,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
        total += d.final_damage
    state.log.append(f'  白厄秘技: 本波次全敌200%ATK物理伤 {total:.0f}')


def _phainon_transform(state, u):
    """变身为卡厄斯兰那（用户 2026-08-14 精确语义）:
    8 额外回合均分于白厄回合周期——卡厄斯兰那速度=基础速度×0.6(E1 66%~84%),
    每回合间隔 = AV_PER_TURN/(基础×0.6)/8; 队友从进度条离开(非无法战斗, 被动/buff/忆灵照常);
    火种溢出直接计入下一次终结技(无延迟)。"""
    from engine.models.enemy import EnemyStatus
    base_spd = u.base_stats._base_SPD or u.base_stats.SPD
    ratio = 0.60
    if u.eidolon_rank >= 1:
        kills = state.extra.get('killed_total', 0)
        ratio = 0.66 + min(0.18, kills * 0.015)
    interval = AV_PER_TURN / max(base_spd * ratio, 1.0) / 8.0
    # 火种溢出（变身消耗12后的剩余, 退出时直接计入——统一在此扣12）
    cur = u.extra.get('huozhong', 0)
    u.extra['huozhong_overflow'] = max(0, cur - 12)
    u.extra['huozhong'] = min(cur, 12)
    u.extra['kasier'] = True
    u.extra['kasier_interval'] = interval
    u.extra['kasier_next_av'] = state.current_av  # 第1回合立即行动
    u.extra['kasier_turns'] = 8
    u.extra['kasier_done'] = 0
    # 队友离场: 角色从进度条离开(存原值恢复), 忆灵保留; 白厄自身也脱离常规排程
    navs = state.extra.get('navs', {})
    u.extra['kasier_ally_navs'] = {}
    for i, eu in enumerate(state.units):
        if i in navs:
            u.extra['kasier_ally_navs'][i] = navs.pop(i)
    _phainon_implant_phys_weak(state)
    state.log.append(f'  变身【卡厄斯兰那】: 8额外回合(间隔{interval:.0f}AV, 速度=基础×{ratio:.2f}) + 队友离场 + 敌物理弱点')


def _phainon_implant_phys_weak(state):
    """v6.6b P1-2: 变身期间所有敌人统一植入物理弱点（原抗性≤0 也降到 -0.2）;
    重复植入不覆盖快照; 退出变身按快照恢复, 波次重生时重植入。"""
    from engine.models.enemy import EnemyStatus
    for e in state.enemies:
        if any(s.id == 'phainon_phys_weak' for s in e.statuses):
            continue
        old_res = e.get_res('物理')
        e.element_res['物理'] = min(old_res, -0.2)
        e.add_status(EnemyStatus(id='phainon_phys_weak', name='物理弱点', category='debuff',
                                 source='phainon', remaining_turns=-1,
                                 attributes={'weakness_element': '物理', 'weakness_old_res': old_res}))


def _phainon_kasier_end(state, u):
    """退出变身: 恢复队友进度条 + 火种返还(溢出+行迹1的3点, 无延迟) + 清除弑魂/物理弱点"""
    u.extra['kasier'] = False
    navs = state.extra.get('navs', {})
    for i, av in (u.extra.pop('kasier_ally_navs', {}) or {}).items():
        navs[i] = av
    overflow = u.extra.pop('huozhong_overflow', 0)
    bonus = 3  # 行迹1: 变身结束+3火种
    # v6.6b P1-4: E6 火种无上限契约不得被 15 点截断
    cap = 15 if u.eidolon_rank < 6 else 10 ** 9
    u.extra['huozhong'] = min(cap, overflow + bonus)
    # v6.6b P1-2: 物理弱点按快照恢复并移除状态
    for e in state.enemies:
        st = next((s for s in e.statuses if s.id == 'phainon_phys_weak'), None)
        if st is not None:
            e.element_res['物理'] = st.attributes.get('weakness_old_res', e.get_res('物理'))
            e.remove_status('phainon_phys_weak')
    # v6.6b P1-7: 清除弑魂状态（反击/减伤）
    u.extra.pop('shihun_stacks', None)
    u.extra.pop('shihun_dr', None)
    u.buffs = [b for b in u.buffs if getattr(b, 'source_name', '') != '弑魂之炽减伤']
    # v6.8.1: 行迹3「变身结束时 ATK+50%」第二层（最多2层, 无条件——行迹3 非秘技）"""
    _phainon_trace3_atk_stack(state, u)
    state.log.append(f'  退出变身: 队友回归进度条 + 火种返还{overflow + bonus}(溢出{overflow}+行迹1的3) + 物理弱点/弑魂清除')


def _phainon_kasier_act(state, u):
    """卡厄斯兰那额外回合执行: 前7回合AI行动(毁伤≥4死星天裁/毁伤≥1弑魂焚诏/毁伤0普攻),
    第8回合=最后一击(960%ATK全体均分)并结束变身"""
    # v6.6b P1-6: 对齐额外回合生命周期（action_ctx/回合计数/sweep; X 轴不 tick 常规 Buff 不变）
    state.extra['action_ctx'] = 'extra'
    state.turn_count += 1
    done = u.extra.get('kasier_done', 0) + 1
    u.extra['kasier_done'] = done
    # v6.8.1: 额外回合开始时仍持【弑魂之炽】→立即反击并解除（txt:50, 此前缺失）
    if u.extra.get('shihun_stacks', 0) > 0:
        _phainon_shihun_counter(state, u, u.extra['shihun_stacks'])
        u.extra.pop('shihun_stacks', None)
        u.extra.pop('shihun_dr', None)
        u.buffs = [b for b in u.buffs if getattr(b, 'source_name', '') != '弑魂之炽减伤']
        state.log.append('  额外回合开始持弑魂→立即反击并解除')
    if done >= 8:
        stats = _build_effective_stats(u, state)
        # v6.6b P2-2: 无存活敌人则跳过伤害（此前回退打尸体）; 伤害按终结技分类（P2-1）
        alive = state.alive_enemies()
        total = 0.0
        if alive:
            for t in alive:
                before = t.HP
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 960.0 / len(alive),
                                     'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate',
                                     crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
        u.total_damage_dealt += total
        state.log.append(f'  最后一击: {total:.0f}(960%ATK均分)')
        _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 内联最后一击补气氛
        _phainon_kasier_end(state, u)
        _sweep_ults(state)
        return
    # AI: 毁伤≥4 死星天裁; 毁伤≥1 弑魂焚诏; 毁伤0 普攻
    hs = u.extra.get('huishang', 0)
    if hs >= 4:
        _use_skill(u, state, 'skill_shenshen')
    elif hs >= 1:
        _use_skill(u, state, 'skill_enhanced')
    else:
        _use_skill(u, state, 'basic_attack_enhanced')
    u.extra['kasier_next_av'] = u.extra.get('kasier_next_av', state.current_av)         + u.extra.get('kasier_interval', 20.0)
    _sweep_ults(state)  # v6.6b P1-6: 额外回合后统一 sweep


def _phainon_shihun_counter(state, u, stacks):
    """弑魂反击: 40%ATK全体 + 4×30%ATK弹射, 每层+20%"""
    stats = _build_effective_stats(u, state)
    mult = 40.0 * (1 + 0.20 * stacks)
    total = 0.0
    # v6.6b P2-2: 无存活敌人则跳过（此前回退打尸体）; 反击按战技+追加攻击分类（P2-1）
    alive = state.alive_enemies()
    for e in alive:
        before = e.HP
        d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK, mult,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        total += d.final_damage
    for _ in range(4):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 30.0 * (1 + 0.20 * stacks),
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        # v6.8.1: 弹射段击杀统一口径（此前漏计数→白厄E1速度比例漏算）
    u.total_damage_dealt += total
    state.log.append(f'  弑魂反击: {total:.0f} ({stacks}层×20%)')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 反击路径补气氛


# ════════════ v6.7 大丽花机制（角色技能介绍/虚无/大丽花.txt）════════════

def _flat_toughness_with_break(state, u, t, amount, element='火', skill_key='talent', stats=None):
    """固定削韧统一入口（v6.7b Harness）: 手写削韧段不再裸改 toughness 绕过击破事件——
    击破时结算击破伤害+25%延后+属性异常+hooks, 与 _apply_toughness_damage 主韧性分支同口径。
    返回击破伤害量。"""
    if t is None or getattr(t, 'HP', 0) <= 0 or t.toughness <= 0 or t.is_broken or t.max_toughness <= 0:
        return 0.0
    t.toughness = max(0, t.toughness - amount)
    if t.toughness > 0 or t.is_broken:
        return 0.0
    t.is_broken = True
    sb_stats = stats or _build_effective_stats(u, state)
    bd = calculate_damage(sb_stats, t, 0, 0, "break", element, 80, False)
    _commit_enemy_damage(state, u, t, bd.final_damage)
    u.total_damage_dealt += bd.final_damage
    t.extra['av_delayed'] = 2500.0
    _apply_break_debuff(t, element, u, state)
    state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
    _process_lc_effects(u, state, "on_weakness_break")
    state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                            enemy=t, skill_key=skill_key)
    state.log.append(f'  固定削韧击破! {t.name or t.id} 击破={bd.final_damage:.0f}({element})')
    return bd.final_damage


def _dahlia_field_active(state) -> bool:
    """大丽花结界激活判断（战技/秘技共用 dahlia_field_turns, 领域互斥天然满足）"""
    return state.extra.get('dahlia_field_turns', 0) > 0


def _dahlia_ensure_dancers(state):
    """共舞者维持（txt 天赋）: 每当场上不存在另一位共舞者（死亡/未绑定）,
    使自身与击破特攻最高队友共同成为共舞者。"""
    dahlia = next((x for x in state.units if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    dancers = state.extra.get('dahlia_dancers', [])
    partner_ids = [cid for cid in dancers if cid != 'the_dahlia']
    if partner_ids and any(x.char.id == cid and x.is_alive
                           for cid in partner_ids for x in state.units):
        return
    others = [x for x in state.units if x.is_alive and x.char.id != 'the_dahlia']
    if not others:
        state.extra['dahlia_dancers'] = ['the_dahlia']
        return

    def _be(x):
        try:
            return _build_effective_stats(x, state).BREAK_EFFECT
        except Exception:
            return 0.0
    partner = max(others, key=lambda x: (_be(x), -getattr(x, 'position', 99)))
    state.extra['dahlia_dancers'] = ['the_dahlia', partner.char.id]
    state.log.append(f'  大丽花天赋: 共舞者重绑={partner.char.name}')


def _dahlia_super_break_rate(state, u, t) -> float:
    """大丽花超击破转化率源（v6.7, 与 _super_break_rate 线性求和）:
    - 天赋: 共舞者攻击破韧目标 → 60%（满级档 30%/60%）
    - 结界: 未破韧目标削韧也能转化 → 60%（用户 2026-08-15 确认与天赋同率）
    - E1: 超击破倍率全队生效（非共舞者 +0.6; 共舞者再 +0.4 合计 1.0）"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return 0.0
    rate = 0.0
    dancers = state.extra.get('dahlia_dancers', [])
    is_dancer = u.char.id in dancers
    if is_dancer and t.is_broken:
        rate += 0.6  # 天赋: 共舞者攻击破韧目标
    if _dahlia_field_active(state) and not t.is_broken:
        rate += 0.6  # 结界: 未破韧目标
    # v6.7b: E1 只放大"天赋超击破"(攻击破韧目标)——未破韧结界转化不叠加
    if dahlia.eidolon_rank >= 1 and t.is_broken:
        rate += 0.6 if not is_dancer else 0.4  # E1: 全队生效, 共舞者合计1.0
    return rate


def _dahlia_talent_open(state):
    """大丽花天赋·谁在害怕康士坦丝?（simulate 初始化调用）:
    开战回35能量 + 自身与击破特攻最高队友成为【共舞者】"""
    dahlia = next((x for x in state.units if x.char.id == 'the_dahlia'), None)
    if dahlia is None:
        return
    _gain_energy(dahlia, 35.0, state=state)
    others = [x for x in state.units if x.is_alive and x.char.id != 'the_dahlia']
    if not others:
        state.extra['dahlia_dancers'] = ['the_dahlia']
        return
    # 击破特攻最高（tiebreak 按站位靠前）
    def _be(x):
        try:
            return _build_effective_stats(x, state).BREAK_EFFECT
        except Exception:
            return 0.0
    partner = max(others, key=lambda x: (_be(x), -getattr(x, 'position', 99)))
    state.extra['dahlia_dancers'] = ['the_dahlia', partner.char.id]
    state.log.append(f'  大丽花天赋: 回35能量; 共舞者={dahlia.char.name}+{partner.char.name}')


def _dahlia_field_apply(state, u):
    """大丽花战技/秘技结界: 3回合 + 全队弱点击破效率+50%
    v6.7b: 重复施放先移除旧 buff（防 +50% 叠加漂移, 同 v6.6c 缇宝结界口径）"""
    state.extra['dahlia_field_turns'] = 3
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'dahlia_field_buff']
            eu.buffs.append(TimedBuff(source_id='the_dahlia',
                                      attributes={'TOUGHNESS_EFFICIENCY': 50.0},
                                      remaining_turns=3, param_id='dahlia_field_buff',
                                      source_name='大丽花结界'))
    state.log.append('  大丽花结界: 开启(3回合), 全队弱点击破效率+50%')


def _dahlia_fua(state):
    """大丽花天赋追加攻击: 5次×30%ATK随机单体(每段削韧3, 含击破结算), 命中破韧目标→本次削韧值(3)转200%超击破
    E4: 段数5→10 + 每次击中目标受伤+12% 2回合; E6: 共舞者行动提前20%;
    天赋能量恢复2; 行迹2: 每施放2次FUA回1战技点。
    v6.7b: 每段重新选择存活目标(清场回退); 击杀统一口径; 超击破段不打尸体。"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    stats = _build_effective_stats(dahlia, state)
    hits = 10 if dahlia.eidolon_rank >= 4 else 5
    total = 0.0
    for _ in range(hits):
        alive = state.alive_enemies()
        if not alive:
            break
        t = random.choice(alive)
        # 天赋削韧值3（含击破结算; 本次削韧刚击破的目标同样满足"处于弱点击破状态"）
        _flat_toughness_with_break(state, dahlia, t, 3.0, '火', 'talent', stats)
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 30.0,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='talent', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, dahlia, t, d.final_damage)
        total += d.final_damage
        # E4: 每次击中目标受伤+12% 2回合
        if dahlia.eidolon_rank >= 4 and t.HP > 0:
            t.add_status(EnemyStatus(id='dahlia_e4_vuln', name='花蕊蚀伤',
                                     category='debuff', source='the_dahlia',
                                     remaining_turns=2,
                                     attributes={'vulnerability': 0.12}))
        # 天赋: 对破韧目标造成伤害后→本次削韧值(3)转1次200%超击破（满级 100%/200%; 不打尸体）
        if t.is_broken and t.HP > 0:
            sb = calculate_damage(stats, _enemy_for_damage(t), 0, 0, 'super_break',
                                  '火', 80, False, toughness_dmg=3.0)
            sb.final_damage *= 2.0
            _commit_enemy_damage(state, dahlia, t, sb.final_damage)
            total += sb.final_damage
    dahlia.total_damage_dealt += total
    dahlia.damage_log.append(('天赋追加攻击', total, 'follow_up'))
    _gain_energy(dahlia, 2.0, state=state)  # txt 天赋: 能量恢复2
    # 行迹2·致哀故人: 施放FUA回1战技点, 每施放2次触发1次
    if any(getattr(tr, 'hook_name', '') == 'the_dahlia_trace2' for tr in (dahlia.char.traces or [])):
        cnt = dahlia.extra.get('dahlia_fua_count', 0) + 1
        dahlia.extra['dahlia_fua_count'] = cnt
        if cnt % 2 == 0:
            _gain_skill_points(state, 1)
            state.log.append('  大丽花行迹2: 第2次FUA→回1战技点')
    state.log.append(f'  大丽花天赋FUA: {total:.0f} ({hits}次×30%, 回2能量)')
    # E6: 施放天赋追加攻击时所有共舞者行动提前20%
    if dahlia.eidolon_rank >= 6:
        navs = state.extra.get('navs', {})
        for cid in state.extra.get('dahlia_dancers', []):
            partner = next((x for x in state.units if x.char.id == cid and x.is_alive), None)
            if partner and not _guest_advance_blocked(state, dahlia, partner):
                pidx = state.units.index(partner)
                if pidx in navs:
                    adv = (AV_PER_TURN / _effective_spd(partner, state)) * 0.20
                    _set_av(state, navs, pidx, max(0, navs[pidx] - adv))
        state.log.append('  大丽花E6: 共舞者行动提前20%')
    _qingge_notify_attack(state, dahlia, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _dahlia_e1_flat(state, dahlia):
    """大丽花E1: 共舞者(含大丽花)施放攻击后, 对受到攻击的敌方目标25%韧性上限固定削韧
    （≥10≤300, 每目标1次; v6.7b: 限定受击目标, 此前误对全体存活敌施加）"""
    if dahlia.eidolon_rank < 1 or not state.alive_enemies():
        return
    targets = [t for t in (state.extra.get('last_attack_targets') or []) if t.HP > 0]
    if not targets:
        return
    for t in targets:
        if t.extra.get('dahlia_e1_flat_used'):
            continue
        flat = min(max(0.25 * t.max_toughness, 10.0), 300.0)
        t.extra['dahlia_e1_flat_used'] = True
        if t.toughness > 0 and not t.is_broken:
            _flat_toughness_with_break(state, dahlia, t, flat, '火', 'talent')
            state.log.append(f'  大丽花E1: 固定削韧{flat:.0f}')


def _dahlia_on_ally_attack(state, u):
    """大丽花天赋: 共舞者攻击后 E1固定削韧(含大丽花自身); 敌方目标受到另一位共舞者攻击后
    → FUA（每回合最多1次）"""
    _dahlia_ensure_dancers(state)
    dancers = state.extra.get('dahlia_dancers', [])
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None or u.char.id not in dancers:
        return
    # E1: 共舞者(含大丽花)攻击后固定削韧
    _dahlia_e1_flat(state, dahlia)
    # 天赋FUA: 仅另一位共舞者攻击触发（每回合最多1次, 大丽花回合开始重置）
    if u.char.id == 'the_dahlia':
        return
    if dahlia.extra.get('dahlia_fua_used'):
        return
    dahlia.extra['dahlia_fua_used'] = True
    _dahlia_fua(state)


def _apply_dahlia_baisie(u, state, target, turns=4):
    """大丽花终结技·败谢: 防御-18% + 添加所有共舞者属性弱点（快照恢复）
    v6.7b: 同元素重复施放保留首次快照（此前取当前抗性致快照污染, 到期恢复成-0.2）;
    主状态同 id 覆盖=刷新持续回合, 防减防叠加。"""
    _dahlia_ensure_dancers(state)
    target.add_status(EnemyStatus(id='the_dahlia_baisie', name='败谢',
                                  category='debuff', source='the_dahlia',
                                  remaining_turns=turns,
                                  attributes={'def_reduction': 0.18}))
    elems = ['火']
    for cid in state.extra.get('dahlia_dancers', []):
        partner = next((x for x in state.units if x.char.id == cid), None)
        if partner and partner.char.element not in elems:
            elems.append(partner.char.element)
    for elem in elems:
        existing = next((s for s in target.statuses if s.id == f'dahlia_weak_{elem}'), None)
        if existing is not None:
            old = existing.attributes.get('weakness_old_res', target.get_res(elem))
        else:
            old = target.get_res(elem)
        target.element_res[elem] = min(old, -0.2)
        target.add_status(EnemyStatus(id=f'dahlia_weak_{elem}', name=f'{elem}弱点',
                                      category='debuff', source='the_dahlia',
                                      remaining_turns=turns,
                                      attributes={'weakness_element': elem,
                                                  'weakness_old_res': old}))
        state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                                element=elem, target=target)
    state.log.append(f'  败谢: 防御-18% + 弱点{"/".join(elems)} ({turns}回合)')


# ════════════ v6.7 绯英机制（角色技能介绍/欢愉/绯英.txt）════════════

def _evanescia_fox_teacher_fua(state, u):
    """绯英天赋·狐狸老师FUA: 全体100%ATK物理伤害+削韧10(含击破结算)+回10能量;
    持好活时全体追加25%物理欢愉伤害; 行迹1: 全敌易伤12% 3回合;
    E1: 额外触发1次欢愉技+10好活当赏。
    v6.7b: 主段改普通物理直伤（txt 主段是物理属性伤害, 25%欢愉是持好活追加段）。"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    laugh_n = state.elation_state.get_good_show_total('evanescia') \
        if state.extra.get('_elation') else 0
    total = 0.0
    for t in alive:
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 100.0,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             attack_type='follow_up', crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, u, t, 10.0, '物理', 'talent', stats)
        # 持好活: 狐狸老师全体25%物理欢愉伤害（不打尸体）
        if laugh_n > 0 and t.HP > 0:
            before2 = t.HP
            d2 = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 25.0,
                                  'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                  laugh_n=laugh_n, attack_type='follow_up',
                                  crit_mode='expected')
            _commit_enemy_damage(state, u, t, d2.final_damage)
            total += d2.final_damage
    u.total_damage_dealt += total
    _gain_energy(u, 10.0, state=state)  # 回10能量（再触发互转, 正确闭环）
    # 行迹1·行裁断: 全敌易伤12% 3回合
    if any(getattr(tr, 'hook_name', '') == 'evanescia_trace1' for tr in (u.char.traces or [])):
        for t in alive:
            t.add_status(EnemyStatus(id='evanescia_vuln', name='行裁断',
                                     category='debuff', source='evanescia',
                                     remaining_turns=3,
                                     attributes={'vulnerability': 0.12}))
        state.log.append('  狐狸老师: 全敌易伤12% 3回合(行裁断)')
    # E1: 额外触发1次欢愉技 + 10好活当赏
    if u.eidolon_rank >= 1:
        _use_skill(u, state, 'elation_skill')
        elation = state.extra.get('_elation')
        if elation:
            elation.grant_good_show(state, 'evanescia', 10.0, source='evanescia_e1')
        state.log.append('  绯英E1: 额外欢愉技 + 10好活当赏')
    state.log.append(f'  狐狸老师FUA: {total:.0f} (100%ATK全体, 回10能量)')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _evanescia_goodshow_extra(state, u, skill_key):
    """绯英持好活当赏时的追加欢愉伤害（天赋）:
    - 战技: 对受击目标 16% 物理欢愉伤害
    - 终结技: 全体 23% + 随机目标 28%
    - 狐狸老师FUA全体25% 在 _evanescia_fox_teacher_fua 内处理（laugh_n 参与）"""
    if state.elation_state.get_good_show_total('evanescia') <= 0:
        return
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    if not alive:
        return
    # v6.7b: 终结技欢愉伤害至少计入等同于能量上限的好活当赏（txt 天赋）
    laugh_n = max(state.elation_state.get_good_show_total('evanescia'),
                  float(u.char.max_energy or 0))
    total = 0.0
    if skill_key == 'skill':
        # 战技: 对受到攻击的敌方目标（主目标+相邻）各16%欢愉伤害
        for t in alive[:3]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 16.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='skill',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        state.log.append(f'  绯英持好活: 战技追加16%欢愉伤害 {total:.0f}')
    elif skill_key == 'ultimate':
        for t in alive:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 23.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='ultimate',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        alive_now = [t for t in alive if t.HP > 0]
        if alive_now:
            t = random.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 28.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='ultimate',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        state.log.append(f'  绯英持好活: 终结技追加23%全体+28%随机 {total:.0f}')
    u.total_damage_dealt += total


# ════════════ v6.7 火花机制（角色技能介绍/欢愉/火花.txt）════════════

def _sparxie_ult_elation_extra(state, u):
    """火花持好活当赏→终结技额外全体48%火属性欢愉伤害（txt 天赋:66, v6.8.1 补）"""
    laugh_n = state.elation_state.get_good_show_total('sparxie')
    if laugh_n <= 0:
        return
    stats = _build_effective_stats(u, state)
    total = 0.0
    for t in state.alive_enemies():
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 48.0,
                             'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, skill_type='ultimate',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    state.log.append(f'  火花持好活: 终结技追加48%欢愉伤害 {total:.0f}')


def _sparxie_enhanced_settle(state, u):
    """火花强化普攻追加结算:
    - 持好活: 天赋 40%主目标+20%相邻 欢愉伤害
    - 互动陷阱(消耗1次): 20%主目标+10%相邻 + 随机礼物
      (红红火火=2笑点2SP / 恍恍惚惚=1笑点) + 天赋每陷阱1次10%欢愉弹射"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    if not alive:
        return
    laugh_n = state.elation_state.get_good_show_total('sparxie')
    main = alive[0]
    adj = alive[1:min(3, len(alive))]
    total = 0.0
    # 持好活: 天赋 40%主+20%相邻
    if laugh_n > 0:
        for t, scale in [(main, 40.0)] + [(t, 20.0) for t in adj]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale,
                                 'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='basic',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
    # 互动陷阱（txt: 消耗战技点1; v6.7b: 补扣费, SP 不足则本次不发动、次数保留）
    traps = u.extra.get('sparxie_trap_uses', 0)
    if traps > 0 and _deduct_skill_point_cost(state, u, 1):
        u.extra['sparxie_trap_uses'] = traps - 1
        for t, scale in [(main, 20.0)] + [(t, 10.0) for t in adj]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # 随机礼物
        if random.random() < 0.5:
            state.laugh_points += 2
            _gain_skill_points(state, 2)
            state.log.append('  互动陷阱: 红红火火(+2笑点+2战技点)')
        else:
            state.laugh_points += 1
            state.log.append('  互动陷阱: 恍恍惚惚(+1笑点)')
        # 天赋: 每发动1次陷阱→强化普攻额外1次10%欢愉弹射
        if laugh_n > 0:
            alive_now = [e for e in alive if e.HP > 0]
            if not alive_now:
                alive_now = alive
            t = random.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 10.0,
                                 'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='basic',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
    if total > 0:
        u.total_damage_dealt += total
        state.log.append(f'  火花强化普攻追加: {total:.0f}')


# ════════════ v6.7 姬子·启行机制（角色技能介绍/智识/姬子·启行.txt）════════════

# 开拓同行角色定义（txt: 开拓者(所有命途)/姬子/姬子•启行/三月七(存护/巡猎)/长夜月/
# 丹恒/丹恒•饮月/丹恒•腾荒/瓦尔特/星期日）
def _hn_realm_blocks_ult(state, u) -> bool:
    """v7.2.0 裁决A: 姬子·启行在场=境界【拓星视界】永久占据境界位——
    遐蝶(遗世冥域)/白厄(卡厄斯兰那)的境界类终结技永久无法施放(与实机相同)。"""
    if not isinstance(u, SimUnit) or u.char.id not in ('xiadie', 'phainon'):
        return False
    return any(x.char.id == 'himeko_nova' and x.is_alive for x in state.units)


HIMEKO_NOVA_COMPANIONS = {
    'trailblazer_destruction', 'trailblazer_elation', 'trailblazer_harmony',
    'trailblazer_preservation', 'trailblazer_remembrance',
    'himeko', 'himeko_nova', 'march_7th', 'march_7th_hunt', 'changyeyue',
    'dan_heng', 'dan_heng_imbibitor_lunae', 'dan_heng_permansor_terrae',
    'welt', 'sunday',
}
# 同行协议·裁决（开拓者/丹恒/星期日）
HIMEKO_NOVA_VERDICT = {
    'trailblazer_destruction', 'trailblazer_elation', 'trailblazer_harmony',
    'trailblazer_preservation', 'trailblazer_remembrance',
    'dan_heng', 'dan_heng_imbibitor_lunae', 'dan_heng_permansor_terrae', 'sunday',
}
# 同行协议·歼破（三月七/长夜月/瓦尔特/姬子）
HIMEKO_NOVA_CHARGE = {'march_7th', 'march_7th_hunt', 'changyeyue', 'welt', 'himeko'}


def _hn_support_cap(u) -> int:
    """助战技使用次数上限: 1(天赋) + 1(E2)"""
    return 2 if u.eidolon_rank >= 2 else 1


def _hn_support_skill(state, user, *, no_charge=False):
    """助战技·开拓与你同行: 用姬子·启行面板结算（视为她施放战技, 不调 _use_skill 防递归）。
    全队80%ATK+3×12%弹射（姬子本人200%+4×32%, E1弹射+1）; 姬子使用不耗次数(行迹1)+
    天赋全抗穿透20%/暴伤80%; 非姬子使用者回4能量; 行迹2开拓同行→额外回合;
    E2×130%; E4全队抗穿; E6×175%+姬子+1源能（自用/他用均+1）。
    v6.7b: 削韧无视弱点（视为姬子施放战技）+击破结算; 弹射段击杀计数;
    歼破协议充能计数（no_charge=协议免费助战技不计数）; 姬子自用 E4 抗穿 30%。"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None:
        return
    is_self = user.char.id == 'himeko_nova'
    # 次数检查（姬子使用不消耗=行迹1）
    if not is_self:
        uses = state.extra.get('hn_support_uses', 0)
        if uses <= 0:
            return
        state.extra['hn_support_uses'] = uses - 1
    alive = state.alive_enemies()
    if not alive:
        state.log.append('  助战技·开拓与你同行: 无存活目标, 未施放')
        return
    stats = _build_effective_stats(himeko, state)
    if is_self:
        # 天赋: 姬子使用时全抗穿透20%/30%(E4)+暴伤80%（均不可叠加, 技能级）
        # v7.2.0 #3: E5天赋+2 → 每级+5%惯例消费
        talent_factor = _skill_level_factor(himeko, 'talent')
        stats = copy.deepcopy(stats)
        stats.RES_PEN_ALL += (0.30 if himeko.eidolon_rank >= 4 else 0.20) * talent_factor
        stats.CRIT_DMG += 0.80 * talent_factor
    elif himeko.eidolon_rank >= 4:
        # E4: 非姬子使用→全队全抗穿透（姬子额外+10%）; 百分比按原始数值口径
        for eu in state.units:
            if eu.is_alive:
                extra = 30.0 if eu.char.id == 'himeko_nova' else 20.0
                eu.buffs.append(TimedBuff(source_id='himeko_nova',
                                          attributes={'RES_PEN_ALL': extra},
                                          remaining_turns=1, source_name='姬子E4抗穿'))
    # 歼破协议: 战技造成的暴击伤害额外提高100%（助战技视为战技）
    if state.extra.get('hn_charge_skill_cd'):
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 1.0
    aoe_scale = 200.0 if is_self else 80.0
    bounce_hits = (4 if is_self else 3) + (1 if himeko.eidolon_rank >= 1 else 0)
    bounce_scale = 32.0 if is_self else 12.0
    mult = 1.0
    if himeko.eidolon_rank >= 2:
        mult *= 1.30  # E2: 助战技伤害×130%
    if himeko.eidolon_rank >= 6:
        mult *= 1.75  # E6: 我方用助战技伤害+75%
    total = 0.0
    for t in alive:
        if not no_charge:
            _hn_count_hits(state, user)  # 歼破协议: 每击中1目标+1充能
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, aoe_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, user, t, d.final_damage)
        total += d.final_damage
        # 削韧（群攻10/单攻5）: 视为姬子施放战技→无视弱点; 击破按火属性结算
        if t.toughness > 0:
            _flat_toughness_with_break(state, himeko, t, 10.0, '火', 'support_skill', stats)
    for _ in range(bounce_hits):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        if not no_charge:
            _hn_count_hits(state, user)
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, bounce_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, user, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, himeko, t, 5.0, '火', 'support_skill', stats)
    user.total_damage_dealt += total
    # 使用助战技视为姬子·启行施放的战技 → 姬子按战技回能
    _gain_energy(himeko, ENERGY_GAIN.get('skill', 30.0), state=state)
    # E6: 我方使用或发动助战技→姬子+1源能（自用/他用一致）
    if himeko.eidolon_rank >= 6:
        himeko.extra['hn_source_energy'] = min(
            6, himeko.extra.get('hn_source_energy', 0) + 1)
    if is_self:
        himeko.damage_log.append(('开拓与你同行(自己)', total, 'support_skill'))
    else:
        # 非姬子使用者: 回4能量 + 行迹2额外回合（可插入施放终结技）
        _gain_energy(user, 4.0, state=state)
        # v7.2.0 #7: 行迹2按次触发（原实现每角色全场仅1次=误读防循环条款）;
        # E2: 非开拓同行角色使用助战技也获得额外回合
        trace2_ok = (user.char.id in HIMEKO_NOVA_COMPANIONS
                     or himeko.eidolon_rank >= 2)
        already_queued = any(x is user for x, k in state.extra.get('extra_turns', []))
        if trace2_ok and not already_queued \
                and not user.extra.get('hn_trace2_pending'):
            user.extra['hn_trace2_pending'] = True  # 额外回合内不再触发(防循环)
            state.extra.setdefault('extra_turns', []).append((user, 'ult'))
            state.log.append(f'  姬子行迹2: {user.char.name}获得额外回合(终结技位)')
    state.log.append(f'  助战技·开拓与你同行: {user.char.name} {total:.0f}'
                     f'({"姬子面板" if is_self else "回4能量"})')
    _qingge_notify_attack(state, user, dealt=total > 0)  # v7.1.0 P1: 助战技(不调_use_skill)补气氛


def _hn_ultimate(state, u):
    """姬子·启行终结技·我们，亦是逐星的巨人（v7.2.0 裁决B 输出手法）:
    行迹3开局+3源能 → 脉冲 → 3×光束 → 脉冲 → 3×光束 → 脉冲 → 最后一击
    脉冲: 消耗当前全部源能——基础10%全体 + 每额外1点1次15%随机单体
          (行迹3当次源能≥3→单体倍率×1.3; E6当次源能≥6→额外160%全体);
    光束: 16%全体 +1源能(E6+2), 上限3(E6:6);
    最后一击: 3×80%随机单体; 任意段清场(无存活敌)→跳过剩余段直接收尾。
    v7.2.0 #3: E3终结技+2 → 全部内联倍率×_skill_level_factor(ultimate)(每级+5%)"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    cap = 6 if u.eidolon_rank >= 6 else 3
    mult = 1.30 if u.eidolon_rank >= 2 else 1.0  # E2: 终结技伤害×130%
    ult_factor = _skill_level_factor(u, 'ultimate')  # v7.2.0 #3: E3终结技+2
    beam_scale = 16.0 * ult_factor
    pulse_scale = 10.0 * ult_factor
    last_scale = 80.0 * ult_factor
    trace3 = any(getattr(tr, 'hook_name', '') == 'himeko_nova_trace3'
                 for tr in (u.char.traces or []))
    # 行迹3: 施放终结技立即+3源能（=手法启动资源）
    if trace3:
        u.extra['hn_source_energy'] = min(cap, u.extra.get('hn_source_energy', 0) + 3)
    total = 0.0
    src_used = 0
    cleared = [False]

    def _alive_now():
        return [e for e in alive if e.HP > 0]

    def _beam_volley(times):
        """超频粒子光束: 每次全体16%+削韧2, 每次+1源能(E6+2)"""
        nonlocal total
        for _ in range(times):
            if not _alive_now():
                cleared[0] = True
                return
            for t in _alive_now():
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, beam_scale * mult,
                                     'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate', crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
                _flat_toughness_with_break(state, u, t, 2.0, '火', 'ultimate', stats)
            gain = 2 if u.eidolon_rank >= 6 else 1  # E6: 光束额外+1源能
            u.extra['hn_source_energy'] = min(
                cap, u.extra.get('hn_source_energy', 0) + gain)

    def _pulse():
        """轨道歼灭脉冲: 消耗全部源能——10%全体 + 每额外1点1次15%随机单体(行迹3≥3×1.3)"""
        nonlocal total, src_used
        src = u.extra.get('hn_source_energy', 0)
        u.extra['hn_source_energy'] = 0
        src_used += src
        for t in _alive_now():
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, pulse_scale * mult,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            _flat_toughness_with_break(state, u, t, 2.0, '火', 'ultimate', stats)
        bounce_scale = 15.0 * ult_factor
        if trace3 and src >= 3:
            bounce_scale *= 1.3  # 行迹3: 当次源能≥3时脉冲单体倍率+30%
        for _ in range(max(0, src - 1)):  # 每额外1点源能1次随机单体
            if not _alive_now():
                cleared[0] = True
                return
            t = random.choice(_alive_now())
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, bounce_scale * mult,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # E6: 当次源能≥6→额外160%全体
        if u.eidolon_rank >= 6 and src >= 6:
            for t in _alive_now():
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK,
                                     160.0 * ult_factor * mult,
                                     'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate', crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage

    # 裁决B 手法: 脉冲-3光束-脉冲-3光束-脉冲-最后一击
    _pulse()
    if not cleared[0]:
        _beam_volley(3)
    if not cleared[0]:
        _pulse()
    if not cleared[0]:
        _beam_volley(3)
    if not cleared[0]:
        _pulse()
    # 最后一击: 3次×80%随机单体
    for _ in range(3):
        if not _alive_now():
            break
        t = random.choice(_alive_now())
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, last_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='ultimate', crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    u.damage_log.append(('我们，亦是逐星的巨人', total, 'ultimate'))
    # 终结技后: 特殊效果额外助战技次数刷新（2次/E1:3次）
    state.extra['hn_protocol_uses'] = 3 if u.eidolon_rank >= 1 else 2
    state.log.append(f'  姬子·启行终结技: {total:.0f} '
                     f'(脉冲-3光束-脉冲-3光束-脉冲-最后一击, 源能消耗{src_used})')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 内联终结技路径补气氛


def _hn_count_ally_ult(state, u):
    """同行协议·裁决: 队友主动施放终结技→计数, 达阈值(2/E1:1)→无消耗助战技"""
    if u.char.id == 'himeko_nova':
        return
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None or not state.extra.get('hn_verdict'):
        return
    threshold = 1 if himeko.eidolon_rank >= 1 else 2  # E1: 所需终结技次数-1
    cnt = state.extra.get('hn_verdict_ult_count', 0) + 1
    state.extra['hn_verdict_ult_count'] = cnt
    if cnt >= threshold:
        state.extra['hn_verdict_ult_count'] = 0
        _hn_try_protocol_support(state, himeko)
        state.log.append(f'  裁决协议: 队友{cnt}次终结技→无消耗助战技')


def _hn_count_hits(state, u):
    """同行协议·歼破: 每击中1名敌方目标+1充能, 达9点(E1:6)→无消耗助战技(本次不获充能)
    v6.7b: 删除姬子自身排除——txt 未排除姬子; 免费助战技不计数由调用方 no_charge 控制。"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None or not state.extra.get('hn_charge_mode'):
        return
    cap = 6 if himeko.eidolon_rank >= 1 else 9  # E1: 所需充能-3
    charge = state.extra.get('hn_charge', 0) + 1
    if charge >= cap:
        state.extra['hn_charge'] = 0
        _hn_try_protocol_support(state, himeko)
        state.log.append(f'  歼破协议: 充能{charge}点→无消耗助战技(本次不获充能)')
    else:
        state.extra['hn_charge'] = charge


def _hn_try_protocol_support(state, himeko):
    """特殊效果免费助战技（单场最多2次, 姬子终结技后刷新）"""
    uses = state.extra.get('hn_protocol_uses', 0)
    if uses <= 0:
        state.log.append('  同行协议: 特殊效果次数已耗尽')
        return
    state.extra['hn_protocol_uses'] = uses - 1
    _hn_support_skill(state, himeko, no_charge=True)  # txt: 本次助战技无法获得充能


def _hn_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """姬子·启行 AI: 满能量→终结技; 助战技轮转（自身不耗次数但1回合CD, 期间战技维持
    领航旗语+恢复次数）; 战技/普攻兜底
    v7.2.0 #2: cd 置2——置1后同回合末尾减1=无效CD, 导致永远助战技、旗语3回合后
    永久丢失; 置2后实际序列=助战技→战技→助战技→战技(旗语持续维持)"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif u.extra.get('hn_skill_cd', 0) <= 0:
        _hn_support_skill(state, u)
        u.extra['hn_skill_cd'] = 2
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")
    if u.extra.get('hn_skill_cd', 0) > 0:
        u.extra['hn_skill_cd'] -= 1



# ════════════ v6.9 星期日机制（角色技能介绍/同谐/星期日.txt）════════════

def _sunday_pick_target(state, u):
    """星期日技能目标: 我方单体（含忆灵; 优先存活）"""
    from engine.core.combat_sim import _pick_single_ally_target
    return _pick_single_ally_target(state, u)


def _sunday_linked_targets(target):
    """星期日单体效果同时覆盖角色及其存活召唤物。"""
    result = [target]
    memsprite = getattr(target, 'memsprite_unit', None)
    if memsprite is not None and memsprite.is_alive:
        result.append(memsprite)
    return result


def _sunday_target_name(target):
    return getattr(getattr(target, 'char', None), 'name', None) \
        or getattr(getattr(target, 'data', None), 'name', '召唤物')


def _sunday_apply_mentor(state, u, target):
    """【蒙福者】: CD+30%×星期日CD+12%, 3回合, 仅最新目标生效;
    E2: 蒙福者伤害+30%; E6: 终结技也可为目标添加天赋CR"""
    from engine.core.combat_sim import TimedBuff, _build_effective_stats
    # 清除旧蒙福者标记（仅最新目标）
    for eu in state.units:
        for part in _sunday_linked_targets(eu):
            part.extra.pop('sunday_mentor', None)
            part.buffs = [b for b in part.buffs
                          if getattr(b, 'param_id', '') != 'sunday_mentor_cd']
    s = _build_effective_stats(u, state)
    cd_bonus = s.CRIT_DMG * 0.30 + 0.12
    attrs = {'CRIT_DMG': cd_bonus * 100.0}
    if u.eidolon_rank >= 2:
        attrs['DMG_BONUS_ALL'] = 30.0  # E2: 蒙福者伤害+30%
    for part in _sunday_linked_targets(target):
        part.extra['sunday_mentor'] = True
        part.buffs.append(TimedBuff(source_id='sunday', attributes=dict(attrs),
                                    remaining_turns=3, param_id='sunday_mentor_cd',
                                    source_name='蒙福者'))
    # E6: 终结技为目标添加天赋CR效果
    if u.eidolon_rank >= 6:
        for part in _sunday_linked_targets(target):
            _sunday_apply_cr_buff(state, u, part, from_ult=True)
    state.log.append(f'  蒙福者: {_sunday_target_name(target)} CD+{cd_bonus*100:.1f}% 3回合(仅最新)')


def _sunday_apply_cr_buff(state, u, target, from_ult=False):
    """天赋CR+20% 3回合（E6: 可叠3层+持续+1+终结技也可添加+溢出暴击率1%→2%暴伤）"""
    from engine.core.combat_sim import TimedBuff
    if from_ult and u.eidolon_rank < 6:
        return  # 终结技添加仅E6
    if not from_ult and u.eidolon_rank < 6:
        # 普通: 单层20% 3回合（同id刷新）
        target.buffs = [b for b in target.buffs if getattr(b, 'param_id', '') != 'sunday_cr']
        target.buffs.append(TimedBuff(source_id='sunday', attributes={'CRIT_RATE': 20.0},
                                      remaining_turns=3, param_id='sunday_cr',
                                      source_name='天赋·倾诉之肉身'))
        state.log.append(f'  星期日天赋: {_sunday_target_name(target)} CR+20% 3回合')
        return
    # E6: 叠3层+持续+1
    duration = 4
    existing = [b for b in target.buffs if getattr(b, 'param_id', '') == 'sunday_cr']
    stacks = min(3, target.extra.get('sunday_cr_stacks', 0) + 1)
    target.extra['sunday_cr_stacks'] = stacks
    for b in existing:
        target.buffs.remove(b)
    target.buffs.append(TimedBuff(source_id='sunday', attributes={'CRIT_RATE': 20.0 * stacks},
                                  remaining_turns=duration, param_id='sunday_cr',
                                  source_name='天赋·倾诉之肉身'))
    state.log.append(f'  星期日E6天赋: {_sunday_target_name(target)} CR+{20*stacks:.0f}% {duration}回合')


def _sunday_skill(state, u):
    """战技·纸与仪典的恩赐: 单体+召唤物立即行动(同谐不触发)+增伤30%(有召唤物50%)2回合;
    天赋CR+20% 3回合; 蒙福者回1SP; 行迹3净化; E1无视16%防御+召唤物40% 2回合"""
    from engine.core.combat_sim import (TimedBuff, _set_av, _build_effective_stats,
                                        _gain_skill_points, _pick_single_ally_target)
    target = _pick_single_ally_target(state, u)
    if target is None:
        return
    if state.extra.pop('sunday_tech_pending', False):
        for part in _sunday_linked_targets(target):
            part.buffs.append(TimedBuff(source_id='sunday',
                                        attributes={'DMG_BONUS_ALL': 50.0},
                                        remaining_turns=2, param_id='sunday_tech_buff',
                                        source_name='荣光之秘'))
        state.log.append(f'  星期日秘技: {target.char.name} 增伤50% 2回合')

    # 立即行动（同谐命途不触发）
    if target.char.path != '同谐':
        navs = state.extra.get('navs', {})
        tgt_idx = state.units.index(target)
        if tgt_idx in navs and not _guest_advance_blocked(state, u, target):
            _set_av(state, navs, tgt_idx, state.current_av)
            state.log.append(f'  星期日战技: {target.char.name} 立即行动')
        # 忆灵立即行动
        if target.memsprite_unit and target.memsprite_unit.is_alive:
            ms = target.memsprite_unit
            # v6.9.1: 忆灵行动条键为 ('ms', id(ms))
            if ms is not None:
                _set_av(state, navs, ('ms', id(ms)), state.current_av)
    # 增伤 30%（有召唤物 50%）2回合
    has_ms = bool(target.memsprite_unit and target.memsprite_unit.is_alive)
    bonus = 50.0 if has_ms else 30.0
    for part in _sunday_linked_targets(target):
        part.buffs.append(TimedBuff(source_id='sunday', attributes={'DMG_BONUS_ALL': bonus},
                                    remaining_turns=2, param_id='sunday_skill_dmg',
                                    source_name='纸与仪典的恩赐'))
    state.log.append(f'  星期日战技: {target.char.name} 增伤{bonus:.0f}% 2回合'
                     f'({"有召唤物" if has_ms else ""})')
    # 天赋: CR+20% 3回合（E6 前仅战技）
    for part in _sunday_linked_targets(target):
        _sunday_apply_cr_buff(state, u, part, from_ult=False)
    # 蒙福者: 回1SP
    if target.extra.get('sunday_mentor'):
        _gain_skill_points(state, 1)
        state.log.append('  星期日战技: 蒙福者回1战技点')
    # E1: 目标无视16%防御+召唤物40% 2回合
    if u.eidolon_rank >= 1:
        target.buffs.append(TimedBuff(source_id='sunday', attributes={'DEF_PEN': 16.0},
                                      remaining_turns=2, param_id='sunday_e1_defpen',
                                      source_name='星期日E1'))
        if has_ms and target.memsprite_unit:
            target.memsprite_unit.buffs.append(TimedBuff(
                source_id='sunday', attributes={'DEF_PEN': 40.0},
                remaining_turns=2, param_id='sunday_e1_defpen', source_name='星期日E1'))
        state.log.append('  星期日E1: 目标无视16%防御+召唤物40% 2回合')
    # 行迹3: 净化1负面
    if any(getattr(tr, 'hook_name', '') == 'sunday_trace3' for tr in (u.char.traces or [])):
        removed = [st for st in target.statuses if getattr(st, 'removable', True)]
        if removed:
            target.statuses.remove(removed[0])
            state.log.append(f'  星期日行迹3: 净化{target.char.name}负面效果')


def _sunday_ult(state, u):
    """终结技·轻与伤痕的赞颂: 目标回20%能量上限(行迹1不足40补至40)+【蒙福者】;
    E2: 首终结技+2SP"""
    from engine.core.combat_sim import (_gain_energy, _gain_skill_points,
                                        _pick_single_ally_target)
    target = _pick_single_ally_target(state, u)
    if target is None:
        return
    if state.extra.pop('sunday_tech_pending', False):
        for part in _sunday_linked_targets(target):
            part.buffs.append(TimedBuff(source_id='sunday',
                                        attributes={'DMG_BONUS_ALL': 50.0},
                                        remaining_turns=2, param_id='sunday_tech_buff',
                                        source_name='荣光之秘'))
        state.log.append(f'  星期日秘技: {target.char.name} 增伤50% 2回合')

    gain = target.char.max_energy * 0.20
    # 行迹1: 不足40补至40
    if any(getattr(tr, 'hook_name', '') == 'sunday_trace1' for tr in (u.char.traces or [])):
        if gain < 40.0:
            gain = 40.0
    # v6.9.1: 20%/最低40为固定能量, 不吃 ENERGY_REGEN（Codex P2-1）
    _gain_energy(target, gain, state=state, apply_regen=False)
    state.log.append(f'  星期日终结技: {target.char.name} 回能量{gain:.0f}')
    _sunday_apply_mentor(state, u, target)
    # E2: 首终结技+2SP
    if u.eidolon_rank >= 2 and not u.extra.get('sunday_e2_used'):
        u.extra['sunday_e2_used'] = True
        _gain_skill_points(state, 2)
        state.log.append('  星期日E2: 首终结技+2战技点')


def _sunday_tick(state, u):
    """星期日回合开始: 【蒙福者】持续回合-1（txt: 星期日自身每回合开始时减1,
    挂目标身上的 buff 按星期日回合倒计时）; E4: 回8能量"""
    from engine.core.combat_sim import _gain_energy
    for eu in state.units:
        if not eu.is_alive:
            continue
        for part in _sunday_linked_targets(eu):
            kept = []
            for b in part.buffs:
                if getattr(b, 'param_id', '') == 'sunday_mentor_cd':
                    b.remaining_turns -= 1
                    if b.remaining_turns > 0:
                        kept.append(b)
                    else:
                        part.extra.pop('sunday_mentor', None)
                        state.log.append(f'  【蒙福者】到期: {_sunday_target_name(part)}')
                else:
                    kept.append(b)
            part.buffs = kept
    if u.eidolon_rank >= 4:
        _gain_energy(u, 8.0, state=state)
        state.log.append('  星期日E4: 回合开始回8能量')


def _sunday_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """星期日 AI: 满能量终结技(蒙福者)→战技→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")



# ════════════ v6.9 瓦尔特机制（角色技能介绍/虚无/瓦尔特.txt）════════════

def _welt_apply_slow(state, u, target):
    """战技命中减速10% 2回合（75%基础概率, 走EHR）"""
    from engine.models.enemy import EnemyStatus
    from engine.core.combat_sim import _roll_effect_hit
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if not _roll_effect_hit(u, state, target, '减速', base_chance=0.75):
        return
    target.add_status(EnemyStatus(id='welt_slow', name='减速', category='debuff',
                                  source='welt', remaining_turns=2,
                                  attributes={'spd_down': 0.10}))
    state.log.append(f'  瓦尔特减速: {target.name or target.id} 速度-10% 2回合')


def _welt_apply_shizhong(state, u, target):
    """【失重】2回合: DEF-40%+速度-5%; 行迹1受伤+10%叠10层; E4全抗-30%"""
    from engine.models.enemy import EnemyStatus
    if target is None:
        return
    attrs = {'def_reduction': 0.40, 'spd_down': 0.05, 'welt_trace1_stacks': 0}
    if u.eidolon_rank >= 4:
        attrs['res_down'] = 0.30
    target.add_status(EnemyStatus(id='welt_shizhong', name='失重', category='debuff',
                                  source='welt', remaining_turns=2, attributes=attrs))
    if u.eidolon_rank >= 4:
        state.log.append(f'  瓦尔特E4: {target.name or target.id} 全抗-30%')
    state.log.append(f'  瓦尔特终结技: {target.name or target.id} 【失重】2回合(DEF-40%)')


def _welt_apply_jinggu(state, u, target, delay_ratio=0.12):
    """v6.9.1: 禁锢1回合——走统一控制类别与 EHR 检定; 延后按目标回合值比例计算"""
    from engine.models.enemy import EnemyStatus
    if target is None:
        return
    from engine.core.combat_sim import AV_PER_TURN, _roll_effect_hit, _enemy_eff_spd
    if not _roll_effect_hit(u, state, target, '禁锢', base_chance=1.0):
        return
    delay = AV_PER_TURN / max(_enemy_eff_spd(target), 1.0) * delay_ratio

    target.add_status(EnemyStatus(id='welt_jinggu', name='禁锢', category='control',
                                  source='welt', remaining_turns=1,
                                  attributes={'spd_down': 0.10, 'delay_ratio': delay_ratio,
                                              'delay_amount': delay}))
    state.log.append(f'  瓦尔特禁锢: {target.name or target.id} 行动延后{delay_ratio*100:.0f}%')


def _welt_ult(state, u):
    """终结技: 150%ATK全体(引擎倍率)+禁锢+失重; 行迹3额外5能"""
    from engine.core.combat_sim import _gain_energy
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        _welt_apply_jinggu(state, u, e)
        _welt_apply_shizhong(state, u, e)
    if any(getattr(tr, 'hook_name', '') == 'welt_trace3' for tr in (u.char.traces or [])):
        _gain_energy(u, 5.0, state=state)
        state.log.append('  瓦尔特行迹3: 终结技额外5能量')


def _welt_extra_damage(state, u, skill_key):
    """附加伤害统一结算（伤害循环后）:
    - 天赋: 击中减速目标→100%ATK虚数附加（E2回3能）
    - 行迹2: 普攻/战技附加80%/120%倍率
    - E1: 失重目标被终结技击中→40%终结技倍率附加(每目标每次攻击1次)
    - 失重目标受击行动延后4%（每目标每回合最多8次）"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage,
                                        _gain_energy)
    alive = state.alive_enemies() or state.enemies
    targets = state.extra.get('last_attack_targets', []) or alive
    stats = _build_effective_stats(u, state)
    trace2 = any(getattr(tr, 'hook_name', '') == 'welt_trace2' for tr in (u.char.traces or []))
    trace3 = any(getattr(tr, 'hook_name', '') == 'welt_trace3' for tr in (u.char.traces or []))
    # 行迹3: EHR>40%每超10% ATK+20%上限80%
    if trace3 and stats.EFFECT_HIT_RATE > 0.40:
        extra_atk = min(int((stats.EFFECT_HIT_RATE - 0.40) / 0.10) * 0.20, 0.80)
        stats = copy.deepcopy(stats)
        stats.ATK *= (1.0 + extra_atk)
    # v6.9.1: 天赋逐段消费（txt:54 每击中1次判定; 弹射用重复段序列）;
    # 行迹2/E1 每目标1次（txt 施放技能「1次附加」）
    segs = state.extra.get('last_hit_segments', []) or targets
    seen_t2 = set()
    seen_e1 = set()
    total = 0.0
    for t in segs:
        if t is None or getattr(t, 'HP', 0) <= 0:
            continue
        t_stats = stats
        is_slow = t.has_status(status_id='welt_slow') or t.has_status(name='减速')
        # E6: 施放战技/终结技击中减速目标→本次伤害双暴（主伤害已由伤害循环吃, 附加段同样生效）
        if is_slow and u.eidolon_rank >= 6 and skill_key in ('skill', 'ultimate'):
            t_stats = copy.deepcopy(t_stats)
            t_stats.CRIT_RATE += 0.30
            t_stats.CRIT_DMG += 0.60
        # 战技天赋已经在 _multihit_damage 按逐段先后结算。
        if is_slow and skill_key != 'skill':
            before = t.HP
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, 100.0,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type=skill_key, crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            if u.eidolon_rank >= 2:
                _gain_energy(u, 3.0, state=state)
                state.log.append('  瓦尔特E2: 天赋触发回3能量')
        # 行迹2: 普攻80%/战技86.4%（=72%×1.2, txt:71）; 终结技不触发; 每目标1次
        if trace2 and skill_key in ('basic_attack', 'skill') and id(t) not in seen_t2:
            seen_t2.add(id(t))
            scale = 80.0 if skill_key == 'basic_attack' else 86.4
            before = t.HP
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, scale,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type=skill_key, crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # E1: 战技/终结技击中失重目标→40%终结技倍率附加（每目标每次攻击1次）
        if skill_key in ('skill', 'ultimate') and u.eidolon_rank >= 1 \
                and t.has_status(status_id='welt_shizhong') and id(t) not in seen_e1:
            seen_e1.add(id(t))
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, 60.0,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            # v6.10.3 P1-7: _commit_enemy_damage 内部已统一计杀, 删除手动 _record_enemy_kill（双计）
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            state.log.append(f'  瓦尔特E1: 失重目标附加40%终结技倍率 {d.final_damage:.0f}')
    u.total_damage_dealt += total
    if total > 0:
        state.log.append(f'  瓦尔特附加伤害: {total:.0f}')


def _welt_talent_hit(state, u, target, stats, skill_type):
    """Resolve one Welt talent hit after a skill segment hit an already slowed target."""
    if target is None or target.HP <= 0:
        return 0.0
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, 100.0,
                         'direct', '虚数', 80, stats.CRIT_RATE >= 0.5,
                         skill_type=skill_type, crit_mode='expected')
    _commit_enemy_damage(state, u, target, d.final_damage)
    if u.eidolon_rank >= 2:
        _gain_energy(u, 3.0, state=state)
        state.log.append('  瓦尔特E2: 天赋触发回3能量')
    return d.final_damage


def _welt_skill_slow(state, u):
    """战技逐段命中减速（last_hit_segments 逐段75%概率）"""
    segs = state.extra.get('last_hit_segments', []) or state.extra.get('last_attack_targets', [])
    for t in segs:
        if t is not None and getattr(t, 'HP', 0) > 0:
            _welt_apply_slow(state, u, t)


def _welt_ally_hit_hooks(state, skill_key):
    """v6.9.1（Codex P2-2）: 失重通用受击钩子——任何我方攻击命中失重目标:
    - txt:48 受击行动延后4%（每目标每回合最多8次, 此前仅瓦尔特攻击触发）
    - txt:66 行迹1 易伤+10% 叠层（最多10层, 持续2回合, 此前固定10%）"""
    welt = next((x for x in state.units if x.char.id == 'welt' and x.is_alive), None)
    if welt is None:
        return
    trace1 = any(getattr(tr, 'hook_name', '') == 'welt_trace1' for tr in (welt.char.traces or []))
    for t in state.extra.get('last_attack_targets', []):
        if t is None or getattr(t, 'HP', 0) <= 0:
            continue
        st = next((s for s in t.statuses if s.id == 'welt_shizhong'), None)
        if st is None:
            continue
        # 受击延后4%（每回合最多8次）
        cnt = t.extra.get('welt_shizhong_count', 0)
        if cnt < 8:
            t.extra['welt_shizhong_count'] = cnt + 1
            t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 400.0
            state.log.append(f'  【失重】受击延后4% ({cnt+1}/8)')
        # 行迹1 叠层（最多10层）
        if trace1:
            stacks = min(10, st.attributes.get('welt_trace1_stacks', 0) + 1)
            st.attributes['welt_trace1_stacks'] = stacks
            st.attributes['vulnerability'] = 0.10 * stacks


def _welt_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """瓦尔特 AI: 满能量终结技→战技→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


# ════════════ v6.9 阮·梅机制（角色技能介绍/同谐/阮·梅.txt）════════════

def _ruanmei_field_active(state) -> bool:
    """阮·梅结界激活判断（独立于 realm_owner 境界系统）"""
    return state.extra.get('ruanmei_field_turns', 0) > 0


def _ruanmei_xianyin_apply(state, u):
    """战技【弦外音】3回合: 全队增伤32%+弱点击破效率50%;
    行迹3: BE>120%每超10%增伤额外+6%上限36%"""
    from engine.core.combat_sim import TimedBuff, _build_effective_stats
    bonus = 32.0
    trace3 = any(getattr(tr, 'hook_name', '') == 'ruan_mei_trace3' for tr in (u.char.traces or []))
    if trace3:
        be = _build_effective_stats(u, state).BREAK_EFFECT
        if be > 1.20:
            bonus = min(32.0 + int((be - 1.20) / 0.10) * 6.0, 68.0)  # 32+36=68 上限
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'ruanmei_xianyin']
            eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                      attributes={'DMG_BONUS_ALL': bonus,
                                                  'TOUGHNESS_EFFICIENCY': 50.0},
                                      remaining_turns=3, param_id='ruanmei_xianyin',
                                      source_name='弦外音'))
    u.extra['ruanmei_xianyin_turns'] = 3
    state.log.append(f'  阮·梅战技: 【弦外音】全队增伤{bonus:.0f}%+破韧效率50% 3回合')


def _ruanmei_field_apply(state, u):
    """终结技结界2回合(E6+1): 全队全抗穿透25%+E1无视20%防御; 攻击后挂残梅绽"""
    from engine.core.combat_sim import TimedBuff
    turns = 3 if u.eidolon_rank >= 6 else 2  # E6: 结界+1回合
    state.extra['ruanmei_field_turns'] = turns
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'ruanmei_field']
            attrs = {'RES_PEN_ALL': 25.0}
            if u.eidolon_rank >= 1:
                attrs['DEF_PEN'] = 20.0  # E1: 全队无视20%防御
            eu.buffs.append(TimedBuff(source_id='ruan_mei', attributes=attrs,
                                      remaining_turns=turns, param_id='ruanmei_field',
                                      source_name='阮·梅结界'))
    state.log.append(f'  阮·梅终结技: 展开结界{turns}回合(全抗穿透25%'
                     f'{", E1无视20%防御" if u.eidolon_rank >= 1 else ""})')


def _ruanmei_apply_canmei(state, u, target):
    """结界期攻击后对目标挂【残梅绽】（恢复前不可重复挂）"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if not _ruanmei_field_active(state):
        return
    if target.has_status(status_id='ruanmei_canmei'):
        return
    target.add_status(EnemyStatus(id='ruanmei_canmei', name='残梅绽', category='debuff',
                                  source='ruan_mei', remaining_turns=-1,
                                  attributes={}))
    state.log.append(f'  【残梅绽】: {target.name or target.id}')


def _ruanmei_canmei_trigger(state, u, enemy):
    """敌方从破韧恢复时【残梅绽】触发: 延长破韧+行动延后(20%×BE+10%)+冰击破伤害50%"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage)
    from engine.core.combat_sim import AV_PER_TURN, _set_av

    if not enemy.has_status(status_id='ruanmei_canmei'):
        return False
    enemy.remove_status('ruanmei_canmei')
    stats = _build_effective_stats(u, state)
    delay = stats.BREAK_EFFECT * 0.20 + 0.10
    delay_av = AV_PER_TURN * delay
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i), state.current_av + delay_av)
    # v6.9.1: 残梅绽延后直接重排敌方 AV, 不再写 av_delayed（旧路径早退不消费）
    before = enemy.HP
    d = calculate_damage(stats, _enemy_for_damage(enemy), stats.ATK, 50.0,
                         'break', '冰', 80, False, crit_mode='expected')
    d.final_damage *= (1.0 + stats.BREAK_EFFECT)  # 击破伤害乘区
    _commit_enemy_damage(state, u, enemy, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  【残梅绽】触发: {enemy.name or enemy.id} 延后{delay*100:.0f}%'
                     f'+冰击破{d.final_damage:.0f}')
    return True  # 保持破韧（延长）


def _ruanmei_break_damage(state, u, target):
    """天赋: 我方击破弱点时阮·梅对目标120%冰击破伤害(E6+200%→320%)"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage)
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive), None)
    if ruan is None:
        return
    stats = _build_effective_stats(ruan, state)
    scale = 120.0 if ruan.eidolon_rank < 6 else 320.0  # E6: 天赋击破倍率+200%
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, scale,
                         'break', '冰', 80, False, crit_mode='expected')
    d.final_damage *= (1.0 + stats.BREAK_EFFECT)
    _commit_enemy_damage(state, ruan, target, d.final_damage)
    ruan.total_damage_dealt += d.final_damage
    # E4: 击破时自身击破特攻+100% 3回合
    if ruan.eidolon_rank >= 4:
        ruan.buffs = [b for b in ruan.buffs if getattr(b, 'param_id', '') != 'ruanmei_e4_be']
        from engine.core.combat_sim import TimedBuff
        ruan.buffs.append(TimedBuff(source_id='ruan_mei', attributes={'BREAK_EFFECT': 100.0},
                                    remaining_turns=3, param_id='ruanmei_e4_be',
                                    source_name='阮·梅E4'))
        state.log.append('  阮·梅E4: 击破特攻+100% 3回合')
    state.log.append(f'  阮·梅天赋: 击破冰伤{d.final_damage:.0f}')



def _ruanmei_canmei_trigger_v3(state, u, enemy):
    """v6.9.1: 残梅绽修复——统一冰击破结果×50%（不再重复乘BE）;
    直接重排敌方下一AV=当前+延后, 保持破韧到该时间点。"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _enemy_for_damage, _record_enemy_kill,
                                        AV_PER_TURN, _set_av)
    if not enemy.has_status(status_id='ruanmei_canmei'):
        return False
    enemy.remove_status('ruanmei_canmei')
    stats = _build_effective_stats(u, state)
    delay = stats.BREAK_EFFECT * 0.20 + 0.10
    delay_av = AV_PER_TURN * delay
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i), state.current_av + delay_av)
    before = enemy.HP
    d = calculate_damage(stats, _enemy_for_damage(enemy), 0, 0, 'break', '冰', 80, False)
    d.final_damage *= 0.50
    _commit_enemy_damage(state, u, enemy, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  【残梅绽】触发: {enemy.name or enemy.id} 延后{delay*100:.0f}%'
                     f'+冰击破{d.final_damage:.0f}')
    return True


def _ruanmei_break_damage_v3(state, u, target):
    """v6.9.1: 天赋击破修复——统一冰击破结果×120%（E6 320%）, 不重复乘BE; E4=+100%BE原始数值。"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage, TimedBuff)
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive), None)
    if ruan is None:
        return
    stats = _build_effective_stats(ruan, state)
    scale = 1.20 if ruan.eidolon_rank < 6 else 3.20  # E6: +200%→320%
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), 0, 0, 'break', '冰', 80, False)
    d.final_damage *= scale
    _commit_enemy_damage(state, ruan, target, d.final_damage)
    ruan.total_damage_dealt += d.final_damage
    if ruan.eidolon_rank >= 4:
        ruan.buffs = [b for b in ruan.buffs if getattr(b, 'param_id', '') != 'ruanmei_e4_be']
        ruan.buffs.append(TimedBuff(source_id='ruan_mei', attributes={'BREAK_EFFECT': 100.0},
                                    remaining_turns=3, param_id='ruanmei_e4_be',
                                    source_name='阮·梅E4'))
        state.log.append('  阮·梅E4: 击破特攻+100% 3回合')
    state.log.append(f'  阮·梅天赋: 击破冰伤{d.final_damage:.0f}')


def _ruanmei_tick(state, u):
    """阮·梅回合开始: 弦外音/结界权威倒计时 + 行迹2回能。"""
    from engine.core.combat_sim import _gain_energy
    xianyin_turns = u.extra.get('ruanmei_xianyin_turns', 0)
    if xianyin_turns > 0:
        xianyin_turns -= 1
        u.extra['ruanmei_xianyin_turns'] = xianyin_turns
        if xianyin_turns <= 0:
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'ruanmei_xianyin']
            state.log.append('  阮·梅弦外音: 结束')
    turns = state.extra.get('ruanmei_field_turns', 0)
    if turns > 0:
        state.extra['ruanmei_field_turns'] = turns - 1
        if turns - 1 <= 0:
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'ruanmei_field']
            state.log.append('  阮·梅结界: 结束')
    if any(getattr(tr, 'hook_name', '') == 'ruan_mei_trace2' for tr in (u.char.traces or [])):
        _gain_energy(u, 5.0, state=state)
        state.log.append('  阮·梅行迹2: 回合开始回5能量')


def _ruanmei_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """阮·梅 AI: 满能量终结技(结界)→战技(弦外音)→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")



# ════════════ v6.9 知更鸟机制（角色技能介绍/同谐/知更鸟.txt）════════════

def _robin_concert_active(u) -> bool:
    return bool(u.extra.get('robin_concert'))


def _robin_skill(state, u):
    """战技: 全队增伤50% 3回合(回合开始-1); 行迹3额外回5能"""
    from engine.core.combat_sim import TimedBuff, _gain_energy
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'robin_skill']
            eu.buffs.append(TimedBuff(source_id='robin', attributes={'DMG_BONUS_ALL': 50.0},
                                      remaining_turns=3, param_id='robin_skill',
                                      source_name='翎之咏叹调'))
    u.extra['robin_skill_turns'] = 3
    state.log.append('  知更鸟战技: 全队增伤50% 3回合')
    if any(getattr(tr, 'hook_name', '') == 'robin_trace3' for tr in (u.char.traces or [])):
        _gain_energy(u, 5.0, state=state)
        state.log.append('  知更鸟行迹3: 战技额外回5能量')


def _robin_ult(state, u):
    """终结技【协奏】: 除自身外队友立即行动; 全队ATK+22.8%+200; 附加伤害挂起;
    免疫控制; 协奏期不进入自己回合; 90速倒计时首次行动时结束"""
    from engine.core.combat_sim import TimedBuff, _set_av
    u.extra['robin_concert'] = True
    u.extra['robin_concert_turns'] = 1
    u.extra['robin_e6_count'] = 0
    # v6.9.1: 90速独立倒计时（约111.11AV/圈），协奏期知更鸟不进入自己回合
    sys = _ensure_marker_system(state)
    if u.marker and u.marker.marker_id == 'robin_concert' and u.marker.is_alive:
        sys.despawn(state, u.marker)
    sys.spawn(state, u, 'robin_concert')

    # 除自身外队友立即行动
    navs = state.extra.get('navs', {})
    for idx, eu in enumerate(state.units):
        if eu.is_alive and eu is not u and idx in navs \
                and not _guest_advance_blocked(state, u, eu):
            _set_av(state, navs, idx, state.current_av)
            state.log.append(f'  协奏: {eu.char.name} 立即行动')
    # 协奏期间知更鸟不进入自己的常规回合；倒计时结束时再插回当前AV。
    navs.pop(state.units.index(u), None)
    # 全队 buff: ATK+22.8%+200; E1全抗穿透24%; E2速度+16%
    # v6.9.1: E4 施放终结技解除全队控制; 行迹1 协奏期全队FUA暴伤+25%
    if u.eidolon_rank >= 4:
        for eu in state.units:
            if eu.is_alive:
                eu.statuses = [s for s in eu.statuses
                               if getattr(s, 'category', '') != 'control']
        state.log.append('  知更鸟E4: 施放终结技解除全队控制')

    attrs = {'ATK_percent': 22.8, 'ATK': 200.0}
    # v6.9.1: 行迹1 协奏期全队FUA暴伤+25%; E4 协奏期效果抵抗+50%（Codex P2-4）
    if any(getattr(tr, 'hook_name', '') == 'robin_trace1' for tr in (u.char.traces or [])):
        attrs['CRIT_DMG_ATK_follow_up'] = 25.0
    if u.eidolon_rank >= 4:
        attrs['EFFECT_RES'] = 50.0
    if u.eidolon_rank >= 1:
        attrs['RES_PEN_ALL'] = 24.0
    if u.eidolon_rank >= 2:
        attrs['SPD_PERCENT'] = 16.0
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'robin_concert']
            eu.buffs.append(TimedBuff(source_id='robin', attributes=attrs,
                                      remaining_turns=1, param_id='robin_concert',
                                      source_name='协奏'))
    state.log.append(f'  知更鸟终结技: 进入【协奏】(全队ATK+22.8%+200'
                     f'{", 全抗穿透24%" if u.eidolon_rank >= 1 else ""}'
                     f'{", 速度+16%" if u.eidolon_rank >= 2 else ""})')


def _robin_concert_extra(state, attacker):
    """协奏期: 每次我方攻击后知更鸟附加120%ATK物理伤(CR固定100%/CD固定150%);
    天赋: 我方攻击后回2能(E2+1); E6附加暴伤+450%(8次/终结技重置)"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _enemy_for_damage, _gain_energy,
                                        _record_kill_after_damage)
    robin = next((x for x in state.units
                  if x.char.id == 'robin' and x.is_alive), None)
    if robin is None:
        return
    _gain_energy(robin, 3.0 if robin.eidolon_rank >= 2 else 2.0, state=state)
    if not _robin_concert_active(robin):
        return
    stats = _build_effective_stats(robin, state)
    targets = state.extra.get('last_attack_targets', [])
    if not targets:
        return
    # 固定双暴: CR固定100%/CD固定150%（E6: 附加暴伤额外+450%, 8次/终结技重置）
    cd_fixed = 1.50
    if robin.eidolon_rank >= 6:
        cnt = robin.extra.get('robin_e6_count', 0)
        if cnt < 8:
            robin.extra['robin_e6_count'] = cnt + 1
            cd_fixed += 4.50
    import copy as _copy
    stats = _copy.deepcopy(stats)
    stats.CRIT_RATE = 1.0
    stats.CRIT_DMG = cd_fixed
    total = 0.0
    for t in targets:
        if t is None or getattr(t, 'HP', 0) <= 0:
            continue
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 120.0,
                             'direct', '物理', 80, True, crit_mode='boolean')
        _commit_enemy_damage(state, robin, t, d.final_damage)
        total += d.final_damage
    robin.total_damage_dealt += total
    if total > 0:
        state.log.append(f'  协奏附加: 知更鸟120%ATK物理伤 {total:.0f}')


def _robin_tick(state, u):
    """知更鸟回合开始: 协奏倒计时-1(到期退出+立即行动); 战技buff回合递减由_tick_buffs"""
    if not _robin_concert_active(u):
        return
    turns = u.extra.get('robin_concert_turns', 0) - 1
    u.extra['robin_concert_turns'] = turns
    if turns <= 0:
        u.extra.pop('robin_concert', None)
        for eu in state.units:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'robin_concert']
        from engine.core.combat_sim import _set_av
        navs = state.extra.get('navs', {})
        uidx = state.units.index(u)
        _set_av(state, navs, uidx, state.current_av)
        state.log.append('  协奏结束: 知更鸟退出并立即行动')


def _robin_skill_tick(state, u):
    """知更鸟自身常规回合开始时递减战技持续时间。"""
    turns = u.extra.get('robin_skill_turns', 0)
    if turns <= 0:
        return
    turns -= 1
    u.extra['robin_skill_turns'] = turns
    if turns <= 0:
        for eu in state.units:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'robin_skill']
        state.log.append('  知更鸟战技增伤: 结束')


def _robin_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """知更鸟 AI: 协奏期不行动(跳过); 满能量终结技→战技→普攻"""
    if _robin_concert_active(u):
        state.log.append(f'  知更鸟: 协奏期不进入自己的回合')
        return
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")




# ════════════ v6.11.1 知更鸟·晴歌（记忆, 晴空乐手+Fever倒计时）════════════
# 数据源: 角色技能介绍/记忆/知更鸟·晴歌.txt (用户原稿 v2)
# v7.1.0 项目主澄清: 贝茜/啾米/派丁仅为「晴空乐手」的状态档位, 实机按一只忆灵计算
# 核心循环: 战技召唤晴空乐手(贝茜档) → 攻击/治疗/护盾攒气氛 → 6/12点升档(啾米/派丁登台)
# → 全员登台(3档)进Fever(晴歌离场, 晴空乐手入行动条+140速倒计时扣气氛) → 气氛归零散场

def _qingge_find(state):
    """存活的知更鸟·晴歌"""
    return next((x for x in state.units
                 if x.char.id == 'robin_summeretto' and x.is_alive), None)


def _qingge_ms(state):
    """唯一的「晴空乐手」忆灵实体（贝茜/啾米/派丁是它的状态档位, v7.1.0 合一）"""
    return next((m for m in state.memsprites
                 if m.summoner_id == 'robin_summeretto' and m.is_alive), None)


def _qingge_members(state):
    """晴空乐手成员档位数: 1=贝茜, 2=+啾米, 3=+派丁(全员登台)"""
    ms = _qingge_ms(state)
    return ms.extra.get('qingge_members', 0) if ms is not None else 0


def _qingge_atmo_cap(qg):
    """气氛上限: 50, E2→70"""
    return 70.0 if qg.eidolon_rank >= 2 else 50.0


def _qingge_gain_atmo(state, gain, cause=None):
    """气氛统一入口: 上限截断 + 阈值召唤检查 + Fever检查 + 动态效果刷新"""
    qg = _qingge_find(state)
    if not qg or gain <= 0:
        return 0.0
    old = qg.extra.get('qingge_atmo', 0.0)
    new = min(_qingge_atmo_cap(qg), old + gain)
    qg.extra['qingge_atmo'] = new
    added = new - old
    if added > 0:
        state.log.append(f'  晴歌气氛+{added:.0f} → {new:.0f}/{_qingge_atmo_cap(qg):.0f}'
                         + (f' ({cause})' if cause else ''))
    _qingge_check_variant_spawn(state, qg)
    _qingge_check_fever(state, qg)
    if added > 0:
        _qingge_refresh_fever_effects(state)
    return added


def _qingge_check_variant_spawn(state, qg):
    """天赋: 晴空乐手(贝茜档)在场时, 气氛≥6→啾米登台; ≥12→派丁登台。
    v7.1.0 项目主澄清: 贝茜/啾米/派丁为状态档位——升档只改成员数并刷新易伤档,
    不触发新召唤（+20能量/on_memsprite_summon 仅实体首次被召唤时）。"""
    ms = _qingge_ms(state)
    if ms is None:
        return
    members = ms.extra.get('qingge_members', 0)
    atmo = qg.extra.get('qingge_atmo', 0.0)
    changed = False
    if members < 2 and atmo >= 6:
        members = 2
        changed = True
        state.log.append('  「晴空乐手」啾米登台 (成员2/3)')
    if members < 3 and atmo >= 12:
        members = 3
        changed = True
        state.log.append('  「晴空乐手」派丁登台 (成员3/3)')
    if changed:
        ms.extra['qingge_members'] = members
        _qingge_refresh_fever_effects(state)


def _qingge_check_fever(state, qg):
    """全员登台(成员档位3)→进入【Fever】"""
    if qg.extra.get('qingge_fever'):
        return
    if _qingge_members(state) < 3:
        return
    _qingge_enter_fever(state, qg)


def _qingge_enter_fever(state, qg):
    """全员登台: 解控 + 进入Fever + 展开结界 + 晴空乐手入行动条 + 倒计时入场;
    晴歌离开行动条(Fever结束前不进自己回合)。"""
    qg.extra['qingge_fever'] = True
    state.log.append('  全员登台! 进入【Fever】')
    ms = _qingge_ms(state)
    # 解控: 晴歌与晴空乐手（忆灵无 statuses, 由召唤者侧清控覆盖）
    for h in [qg] + ([ms] if ms is not None else []):
        if hasattr(h, 'statuses'):
            h.statuses = [s for s in h.statuses
                          if getattr(s, 'category', '') != 'control']
    # E4: 立即+12气氛
    if qg.eidolon_rank >= 4:
        _qingge_gain_atmo(state, 12.0, cause='E4')
    # E6: 本场第一次进Fever→回140能量
    if qg.eidolon_rank >= 6 and not qg.extra.get('qingge_e6_fever_energy'):
        qg.extra['qingge_e6_fever_energy'] = True
        _gain_energy(qg, 140.0, state=state)
        state.log.append('  晴歌E6: 首次进入Fever, 回140能量')
    # 晴歌离场: 从行动条摘除, 退出Fever时恢复
    navs = state.extra.get('navs', {})
    uidx = state.units.index(qg)
    qg.extra['qingge_suspended'] = navs.pop(uidx, None)
    # 晴空乐手入行动条(SPD激活) — v7.1.0 单实体一条
    if ms is not None:
        ms.runtime_spd = _qingge_ms_spd(state, qg, ms)
        ms.extra['next_av'] = state.current_av + AV_PER_TURN / max(ms.runtime_spd, 1.0)
        _stamp_av_key(state, ('ms', id(ms)))
        state.log.append(f'  「晴空乐手」登台行动 (SPD={ms.runtime_spd:.0f}, 成员3/3)')
    # 倒计时入场(140速)
    sys = _ensure_marker_system(state)
    if qg.marker and qg.marker.marker_id == 'qingge_countdown' and qg.marker.is_alive:
        sys.despawn(state, qg.marker)
    sys.spawn(state, qg, 'qingge_countdown')
    # 动态效果: 结界无视防御 + Fever伤害加成 + 成员数易伤
    _qingge_refresh_fever_effects(state)
    state.log.append('  Fever: 展开结界(我方伤害无视防御15%+气氛×0.5%), 晴歌&晴空乐手免疫控制')


def _qingge_exit_fever(state, qg):
    """气氛归零: 晴空乐手全部消失 + 退出Fever + 行动提前50%恢复行动条
    v7.0.0 B3: 行动提前基于摘除时快照(qingge_suspended)减半,
    max(current_av, susp-half)兜底; 消除死变量。"""
    qg.extra['qingge_fever'] = False
    state.log.append('  气氛归零: 退出【Fever】')
    rem = state.extra.get('_rem_sys')
    ms = _qingge_ms(state)
    if ms is not None:
        if rem is not None:
            rem.despawn_memsprite(state, qg, ms, reason='Fever结束')
        elif ms in state.memsprites:
            state.memsprites.remove(ms)
    qg.memsprite_unit = None
    # 忆灵天赋·乘上夏夜晚风: 晴歌行动提前50% + 恢复行动条
    navs = state.extra.get('navs', {})
    uidx = state.units.index(qg)
    susp = qg.extra.pop('qingge_suspended', None)
    half = AV_PER_TURN / max(_effective_spd(qg, state), 1.0) * 0.5
    if susp is not None:
        _set_av(state, navs, uidx, max(state.current_av, susp - half))
    else:
        _set_av(state, navs, uidx, state.current_av + half)
    # 倒计时退场
    sys = state.extra.get('_marker_sys')
    if sys and qg.marker and qg.marker.marker_id == 'qingge_countdown' \
            and qg.marker.is_alive:
        sys.despawn(state, qg.marker)
    # 动态效果回退（结界/伤害/易伤归零）
    _qingge_refresh_fever_effects(state)
    state.log.append('  乘上夏夜晚风: 晴歌行动提前50%')


def _qingge_countdown_action(state, marker):
    """Fever倒计时行动(140速): 扣50%气氛(至少12点); 气氛归零→散场"""
    qg = _qingge_find(state)
    sys = state.extra.get('_marker_sys')
    if qg is None or not qg.extra.get('qingge_fever'):
        # 晴歌阵亡/状态异常: 清理残留晴空乐手与倒计时
        rem = state.extra.get('_rem_sys')
        owner = next((x for x in state.units if x.char.id == 'robin_summeretto'), None)
        ms = _qingge_ms(state)
        if ms is not None and rem is not None and owner is not None:
            rem.despawn_memsprite(state, owner, ms, reason='晴歌离场')
        if sys:
            sys.despawn(state, marker)
        return
    # E6: 倒计时回合开始→回140能量
    if qg.eidolon_rank >= 6:
        _gain_energy(qg, 140.0, state=state)
        state.log.append('  晴歌E6: Fever倒计时回合开始, 回140能量')
    atmo = qg.extra.get('qingge_atmo', 0.0)
    deduct = min(atmo, max(int(atmo * 0.5), 12))
    qg.extra['qingge_atmo'] = atmo - deduct
    state.log.append(f'  Fever倒计时: 气氛-{deduct:.0f} → {qg.extra["qingge_atmo"]:.0f}')
    if qg.extra['qingge_atmo'] <= 0:
        _qingge_exit_fever(state, qg)
    else:
        _qingge_refresh_fever_effects(state)


def _qingge_ms_spd(state, qg, ms):
    """晴空乐手行动速度: 晴歌SPD×180%; E4 Fever期×(1+20%+气氛×0.5%)"""
    base = _effective_spd(qg, state) * 1.80
    if qg.eidolon_rank >= 4 and qg.extra.get('qingge_fever'):
        base *= 1.0 + 0.20 + qg.extra.get('qingge_atmo', 0.0) * 0.005
    return base


def _qingge_refresh_fever_effects(state):
    """动态刷新四组数值（先减旧值再加新值, 幂等）:
    1) 结界: 全队含忆灵 DEF_PEN = Fever? (15%+气氛×0.5%)×天赋factor : 0
    2) Fever伤害加成: 晴歌+晴空乐手 DMG_BONUS_ALL = Fever? (60%+气氛×2%)×忆灵天赋factor : 0 (Lv10)
    3) 成员数易伤: 全队含忆灵 VULNERABILITY_APPLIED = 8%/12%/16%×忆灵天赋factor (成员档位1/2/3, Lv10, 在场即生效)
    4) E4速度: Fever期晴空乐手 runtime_spd 跟随气氛
    v7.0.0 A3: E3天赋+2/忆灵天赋+1 → _skill_level_factor/boost 消费(每级+5%惯例)
    v7.1.0: 三忆灵合一——易伤档位按成员档位状态取值, 不再数实体数"""
    qg = _qingge_find(state)
    if not qg:
        return
    atmo = qg.extra.get('qingge_atmo', 0.0)
    fever = bool(qg.extra.get('qingge_fever'))
    ms = _qingge_ms(state)
    ms_list = [ms] if ms is not None else []
    team = [x for x in state.units if x.is_alive] + ms_list

    talent_factor = _skill_level_factor(qg, 'talent')
    ms_talent_factor = 1.0 + 0.05 * (qg.extra.get('skill_level_boost', {}) or {}).get(
        'memsprite_talent', 0)

    pen = ((0.15 + atmo * 0.005) * talent_factor) if fever else 0.0
    old_pen = state.extra.get('qingge_field_pen', 0.0)
    if abs(pen - old_pen) > 1e-9:
        for x in team:
            x.base_stats.DEF_PEN += pen - old_pen
        state.extra['qingge_field_pen'] = pen

    boost = ((0.60 + atmo * 0.02) * ms_talent_factor) if fever else 0.0
    for h in [qg] + ms_list:
        old = h.extra.get('qingge_dmg_boost', 0.0)
        if abs(boost - old) > 1e-9:
            h.base_stats.DMG_BONUS_ALL += boost - old
            h.extra['qingge_dmg_boost'] = boost

    vuln_map = {1: 0.08 * ms_talent_factor, 2: 0.12 * ms_talent_factor,
                3: 0.16 * ms_talent_factor}
    vuln = vuln_map.get(_qingge_members(state), 0.0)
    old_vuln = state.extra.get('qingge_presence_vuln', 0.0)
    if abs(vuln - old_vuln) > 1e-9:
        for x in team:
            x.base_stats.VULNERABILITY_APPLIED += vuln - old_vuln
        state.extra['qingge_presence_vuln'] = vuln

    if fever and ms is not None:
        ms.runtime_spd = _qingge_ms_spd(state, qg, ms)


def _qingge_atmo_from_action(state, cause):
    """其他单位行动使晴歌获得气氛后的统一附加:
    E2(任意目标回合内第一次施放技能使晴歌获得气氛→额外+2) + 律动消耗(行迹2) + 偏离和弦(行迹3)"""
    qg = _qingge_find(state)
    if qg is None:
        return
    first_this_turn = qg.extra.get('qingge_atmo_turn', -1) != state.turn_count
    qg.extra['qingge_atmo_turn'] = state.turn_count
    if first_this_turn and qg.eidolon_rank >= 2:
        _qingge_gain_atmo(state, 2.0, cause='E2额外')
    if first_this_turn:
        _qingge_rhythm_consume(state, qg)
    _qingge_trace3(state, qg, cause)


def _qingge_rhythm_consume(state, qg):
    """行迹2·即兴蓝调: 任意目标回合内第一次获得气氛时, 消耗1层律动→回3能量"""
    if not any(getattr(t, 'hook_name', '') == 'qingge_trace2_rhythm'
               for t in (qg.char.traces or [])):
        return
    if qg.extra.get('qingge_rhythm', 0) <= 0:
        return
    qg.extra['qingge_rhythm'] = qg.extra['qingge_rhythm'] - 1
    _gain_energy(qg, 3.0, state=state)
    state.log.append(f'  即兴蓝调: 消耗1层律动(剩{qg.extra["qingge_rhythm"]}层), 回3能量')


def _qingge_trace3(state, qg, cause):
    """行迹3·偏离和弦: 使我方目标获得气氛时——
    ATK>晴歌→ATK+晴歌HP×(16%+气氛×0.4%); 否则CD+40%+气氛×1.5% (2回合, 数值随当时气氛快照)"""
    if cause is None or cause is qg:
        return
    if not any(getattr(t, 'hook_name', '') == 'qingge_trace3_chord'
               for t in (qg.char.traces or [])):
        return
    atmo = qg.extra.get('qingge_atmo', 0.0)
    if cause.base_stats.ATK > qg.base_stats.ATK:
        amt = qg.base_stats.HP * (0.16 + atmo * 0.004)
        cause.buffs = [b for b in cause.buffs
                       if getattr(b, 'param_id', '') != 'qingge_chord_atk']
        cause.buffs.append(TimedBuff(source_id='robin_summeretto',
                                     attributes={'ATK': amt},
                                     remaining_turns=2, param_id='qingge_chord_atk',
                                     source_name='偏离和弦'))
        state.log.append(f'  偏离和弦: {cause.char.name} ATK+{amt:.0f} (2回合)')
    else:
        cd = 40.0 + atmo * 1.5
        cause.buffs = [b for b in cause.buffs
                       if getattr(b, 'param_id', '') != 'qingge_chord_cd']
        cause.buffs.append(TimedBuff(source_id='robin_summeretto',
                                     attributes={'CRIT_DMG': cd},
                                     remaining_turns=2, param_id='qingge_chord_cd',
                                     source_name='偏离和弦'))
        state.log.append(f'  偏离和弦: {cause.char.name} 暴伤+{cd:.1f}% (2回合)')


def _qingge_on_ally_attack(state, attacker, via_memsprite=False):
    """我方目标施放攻击结算后: 晴歌气氛+1;
    特邀嘉宾持有者及其召唤物攻击→额外+2 (attacker=召唤者, 忆灵攻击同入口);
    然后统一处理 E2/律动/偏离和弦(attacker≠晴歌)。
    v7.0.0 A4: 晴歌自己的忆灵施放忆灵技(via_memsprite=True, attacker=晴歌)时,
    按"我方目标(忆灵)施放技能使晴歌获得气氛"触发E2额外+2与律动消耗;
    行迹3目标=忆灵无增益意义(_qingge_trace3 对 cause=None 直接返回)。"""
    qg = _qingge_find(state)
    if qg is None:
        return
    _qingge_gain_atmo(state, 1.0, cause='攻击')
    if attacker is not None and attacker is not qg \
            and any(getattr(b, 'param_id', '') == 'qingge_guest' for b in attacker.buffs):
        _qingge_gain_atmo(state, 2.0, cause='特邀嘉宾')
    if attacker is not None and attacker is not qg:
        _qingge_atmo_from_action(state, attacker)
    elif via_memsprite:
        _qingge_atmo_from_action(state, None)


def _qingge_notify_attack(state, attacker, dealt=True):
    """v7.1.0 P1: 独立攻击路径(天赋FUA/助战技/内联终结技/0倍率技能等, 不经
    _use_skill 通用循环结尾)的气氛触发入口——每次调用代表一次完整攻击动作,
    与通用循环(total_dmg>0)口径一致。"""
    if not dealt or attacker is None:
        return
    _qingge_on_ally_attack(state, attacker)


def _guest_advance_blocked(state, actor, target):
    """v7.1.0 特邀嘉宾防永动机规则(项目主澄清②): 持有【特邀嘉宾】的角色
    不得使**其他**友方获得行动提前; 自拉条放行(翔鹰4pc/各类自加速均不受影响)。"""
    if actor is None or target is None or target is actor:
        return False
    if not isinstance(actor, SimUnit):
        return False
    if any(getattr(b, 'param_id', '') == 'qingge_guest' for b in actor.buffs):
        state.log.append(f'  【特邀嘉宾】: {actor.char.name}无法使其他友方获得行动提前')
        return True
    return False



def _qingge_on_heal_shield(state, provider=None, targets=None):
    """渠道b + 行迹2·即兴蓝调（治疗侧 on_heal hook 与护盾侧 on_shield 内联共用）:
    队友提供的治疗/护盾作用于晴歌/晴空乐手→【律动】直接满12层(用户确认);
    任意目标回合内第一次提供治疗/护盾→晴歌气氛+1(治疗与护盾共享每回合去重)。"""
    qg = _qingge_find(state)
    if qg is None:
        return
    if provider is not None and provider is not qg:
        qg_ms = _qingge_ms(state)
        if any(t is qg or t is qg_ms for t in (targets or [])):
            qg.extra['qingge_rhythm'] = 12
            state.log.append('  即兴蓝调: 受队友治疗/护盾→律动12层')
    if qg.extra.get('qingge_heal_turn', -1) != state.turn_count:
        qg.extra['qingge_heal_turn'] = state.turn_count
        _qingge_gain_atmo(state, 1.0, cause='治疗/护盾')
        if provider is not None and provider is not qg:
            _qingge_atmo_from_action(state, provider)


def _qingge_ult_target(state, u):
    """终结技目标(用户确认规则): 姬子·启行队→姬子; 遐蝶风堇队→风堇;
    其他队伍暂按主C惯例(希儿)→第一个队友, 具体情况待用户细化。"""
    for cid in ('himeko_nova',):
        t = next((x for x in state.units if x.char.id == cid and x.is_alive), None)
        if t is not None:
            return t
    has_xiadie = any(x.char.id == 'xiadie' and x.is_alive for x in state.units)
    if has_xiadie:
        fj = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
        if fj is not None:
            return fj
    seele = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if seele is not None:
        return seele
    return next((x for x in state.units if x.is_alive and x is not u), u)


def _qingge_ultimate(state, u):
    """终结技·跃入这片蔚蓝狂想: 目标行动提前100% + 固定回20%能量上限 + 【特邀嘉宾】2回合
    v7.0.0 A1: 自身'能量恢复:5'经通用路径消费JSON effects(energy_regen, 见 _use_skill
    终结技分支)——与姬子·启行等26角色同模式, 此处不再内联回能(曾双重回能+10, GLM验收P1);
    v7.0.0 A3: 目标回能×_skill_level_factor(E5终结技+2→每级+5%)"""
    target = _qingge_ult_target(state, u)
    navs = state.extra.get('navs', {})
    t_idx = state.units.index(target) if target in state.units else -1
    if t_idx >= 0 and t_idx in navs:
        _set_av(state, navs, t_idx, state.current_av)  # 行动提前100%
        state.log.append(f'  晴歌终结技: {target.char.name}行动提前100%')
    # 固定恢复20%能量上限(不吃能量恢复效率)
    _gain_energy(target, (target.char.max_energy or 0) * 0.20
                 * _skill_level_factor(u, 'ultimate'), state=state,
                 apply_regen=False)
    target.buffs = [b for b in target.buffs
                    if getattr(b, 'param_id', '') != 'qingge_guest']
    target.buffs.append(TimedBuff(source_id='robin_summeretto', attributes={},
                                  remaining_turns=2, param_id='qingge_guest',
                                  source_name='特邀嘉宾'))
    state.log.append(f'  【特邀嘉宾】→ {target.char.name} (2回合: 攻击时晴歌气氛+2, 无法拉条队友)')


# 晴歌倒计时 marker 延迟注册（MARKER_ACTIONS 在模块前部构建, 函数定义在后方）
MARKER_ACTIONS["qingge_countdown"] = _qingge_countdown_action


def _rise_and_sing_entry(state, u):
    """光锥[你将起身歌唱]: 进战行动提前(叠影档30-40%) + 【新声】2回合全队速度(叠影档20-40%)"""
    adv = _lc_rank_value(u, 0.30, code='rise_and_sing_advance')
    spd = _lc_rank_value(u, 0.20, code='rise_and_sing_spd')
    u.extra['initial_action_advance_ratio'] = max(
        u.extra.get('initial_action_advance_ratio', 0.0), adv)
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'rise_and_sing_newsound']
            eu.buffs.append(TimedBuff(source_id='rise_and_sing',
                                      attributes={'SPD_PERCENT': spd * 100.0},
                                      remaining_turns=2, param_id='rise_and_sing_newsound',
                                      source_name='新声'))
    state.log.append(f'  光锥[你将起身歌唱] 进战: 行动提前{adv * 100:.0f}% + 新声(全队速度+{spd * 100:.0f}%, 2回合)')


# ════════════ v6.9 不死途机制（角色技能介绍/巡猎/不死途.txt）════════════


def _busitu_apply_bait(state, u, target):
    """【饲饵】: 仅最新被施加的目标生效"""
    if target is None:
        return
    for e in state.enemies:
        e.extra.pop('busitu_bait', None)
    target.extra['busitu_bait'] = True
    _busitu_sync_bait_effects(state, u)
    state.log.append(f'  【饲饵】: {target.name or target.id}(仅最新)')


def _busitu_bait_target(state):
    """当前饲饵目标（存活）"""
    for e in state.enemies:
        if e.extra.get('busitu_bait') and getattr(e, 'HP', 0) > 0:
            return e
    return None


def _busitu_sync_bait_effects(state, u):
    """Synchronize bait-scoped DEF and E6 resistance debuffs without base mutation."""
    from engine.models.enemy import EnemyStatus
    active = _busitu_bait_target(state) is not None
    for enemy in state.enemies:
        if not active or enemy.HP <= 0:
            enemy.remove_status('busitu_def_down')
            enemy.remove_status('busitu_e6_res_down')
            continue
        enemy.add_status(EnemyStatus(
            id='busitu_def_down', name='饲饵威慑', category='debuff',
            source='busitu', remaining_turns=-1,
            attributes={'def_reduction': 0.40},
        ))
        if u.eidolon_rank >= 6:
            enemy.add_status(EnemyStatus(
                id='busitu_e6_res_down', name='不死途E6', category='debuff',
                source='busitu', remaining_turns=-1,
                attributes={'res_down': 0.20},
            ))


def _busitu_rebind_bait(state, u):
    """v6.9.1: 当前饲饵死亡/缺失时, 自动选最低HP存活敌继承饲饵。"""
    target = _busitu_bait_target(state)
    if target is not None:
        return target
    alive = state.alive_enemies()
    if not alive:
        _busitu_sync_bait_effects(state, u)
        return None
    target = min(alive, key=lambda e: e.HP)
    _busitu_apply_bait(state, u, target)
    return target



def _busitu_gain_lanhan(u, amount):
    """婪酣叠层（上限12, E2: 18）"""
    cap = 18 if u.eidolon_rank >= 2 else 12
    before = u.extra.get('busitu_lanhan', 0)
    u.extra['busitu_lanhan'] = min(cap, before + amount)
    u.extra['busitu_lanhan_total'] = min(30, u.extra.get('busitu_lanhan_total', 0) + amount)
    return u.extra['busitu_lanhan'] - before


def _busitu_skill(state, u, target):
    """战技: 指定目标成饲饵; 200%ATK+(已是饲饵额外100%引擎双倍率)+回1SP;
    有饲饵全敌DEF-40%; 无饲饵→最低HP敌成饲饵; 行迹1+1婪酣"""
    from engine.core.combat_sim import _gain_skill_points
    was_bait = bool(target is not None and target.extra.get('busitu_bait'))
    u.extra['busitu_skill_was_bait'] = was_bait

    _busitu_apply_bait(state, u, target)
    # 已是饲饵→回1SP（引擎第二倍率即额外100%段）
    if was_bait:
        _gain_skill_points(state, 1)
        state.log.append('  不死途战技: 饲饵目标回1战技点')
    # 行迹1: 战技+1婪酣
    if any(getattr(tr, 'hook_name', '') == 'busitu_trace1' for tr in (u.char.traces or [])):
        gained = _busitu_gain_lanhan(u, 1)
        state.log.append(f'  不死途行迹1: 战技+1婪酣({u.extra["busitu_lanhan"]}层)')
    state.log.append(f'  不死途战技: {target.name or target.id} 成为【饲饵】')


def _busitu_fua(state, u, target, enhanced=False):
    """天赋FUA: 200%ATK雷伤+2层婪酣; 行迹2 FUA伤害+80%+每层+10%;
    enhanced=强化FUA(终结技): 婪酣≥4耗4层额外200%段, 致命→新饲饵连锁"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage)
    stats = _build_effective_stats(u, state)
    trace2 = any(getattr(tr, 'hook_name', '') == 'busitu_trace2' for tr in (u.char.traces or []))
    mult = 1.0
    if trace2:
        mult *= (1.80 + u.extra.get('busitu_lanhan', 0) * 0.10)  # 行迹2: +80%+每层10%
    if u.eidolon_rank >= 6:
        mult *= (1.0 + u.extra.get('busitu_lanhan_total', 0) * 0.04)  # E6: 累计获得过的婪酣, 上限30
    total = 0.0
    # 主段 200%
    if target is not None and getattr(target, 'HP', 0) > 0:
        d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, 200.0 * mult,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='talent', attack_type='follow_up',
                             crit_mode='expected')
        _, killed = _commit_enemy_damage(state, u, target, d.final_damage)
        total += d.final_damage
        if killed:
            if any(getattr(tr, 'hook_name', '') == 'busitu_trace1'
                   for tr in (u.char.traces or [])):
                _busitu_gain_lanhan(u, 1)  # 行迹1: FUA致命+1层
                state.log.append('  不死途行迹1: FUA致命+1婪酣')
    # 强化FUA连锁: 婪酣≥4耗4层额外200%段, 致命→新饲饵继续
    # v6.9.1: 饲饵死亡自动继承最低HP存活敌（Codex P1-4）
    removed_lanhan = 0.0
    if enhanced:
        while u.extra.get('busitu_lanhan', 0) >= 4:
            u.extra['busitu_lanhan'] -= 4
            removed_lanhan += 4
            nxt = _busitu_rebind_bait(state, u)
            if nxt is None:
                break
            d = calculate_damage(stats, _enemy_for_damage(nxt), stats.ATK, 200.0 * mult,
                                 'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='talent', attack_type='follow_up',
                                 crit_mode='expected')
            _, killed = _commit_enemy_damage(state, u, nxt, d.final_damage)
            total += d.final_damage
            state.log.append(f'  强化FUA连锁: {nxt.name or nxt.id} {d.final_damage:.0f}')
            if not killed:
                break  # 未致命停止连锁
    if u.eidolon_rank >= 2 and removed_lanhan > 0:
        refunded = removed_lanhan * 0.35
        _busitu_gain_lanhan(u, refunded)
        state.log.append(f'  不死途E2: 返还婪酣{refunded:g}层')
    # 主段后+2层婪酣（强化FUA也+2）
    _busitu_gain_lanhan(u, 2)
    if _busitu_bait_target(state) is None:
        _busitu_rebind_bait(state, u)
    _busitu_sync_bait_effects(state, u)
    u.total_damage_dealt += total
    u.damage_log.append(('宿怨，切齿奉还', total, 'follow_up'))
    state.log.append(f'  不死途FUA: {total:.0f} (200%ATK{", 强化" if enhanced else ""})'
                     f' 婪酣{u.extra["busitu_lanhan"]}层')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _busitu_ult(state, u, target):
    """终结技: 目标成饲饵; 400%ATK(引擎)+3充能+强化FUA; 行迹1+2婪酣; E4 ATK+40% 3回合"""
    from engine.core.combat_sim import TimedBuff
    _busitu_apply_bait(state, u, target)
    u.extra['busitu_charge'] = min(3, u.extra.get('busitu_charge', 0) + 3)
    state.log.append(f'  不死途终结技: +3充能({u.extra["busitu_charge"]}/3)')
    _busitu_fua(state, u, target, enhanced=True)
    if any(getattr(tr, 'hook_name', '') == 'busitu_trace1' for tr in (u.char.traces or [])):
        _busitu_gain_lanhan(u, 2)
        state.log.append(f'  不死途行迹1: 终结技+2婪酣({u.extra["busitu_lanhan"]}层)')
    if u.eidolon_rank >= 4:
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'busitu_e4_atk']
        u.buffs.append(TimedBuff(source_id='busitu', attributes={'ATK_PERCENT': 40.0},
                                 remaining_turns=3, param_id='busitu_e4_atk',
                                 source_name='不死途E4'))
        state.log.append('  不死途E4: 攻击力+40% 3回合')


def _busitu_on_ally_attack(state, attacker):
    """天赋: 饲饵受其他目标攻击→回8能+耗1充能FUA 200%ATK+2层婪酣"""
    from engine.core.combat_sim import _gain_energy
    if attacker.char.id == 'busitu':
        return
    busitu = next((x for x in state.units
                   if x.char.id == 'busitu' and x.is_alive), None)
    if busitu is None:
        return
    bait = _busitu_bait_target(state)
    if bait is None or bait not in state.extra.get('last_attack_targets', []):
        return
    if busitu.extra.get('busitu_charge', 0) <= 0:
        return
    busitu.extra['busitu_charge'] -= 1
    _gain_energy(busitu, 8.0, state=state)
    _busitu_fua(state, busitu, bait, enhanced=False)
    state.log.append(f'  不死途天赋: 饲饵受击回8能+耗1充能({busitu.extra["busitu_charge"]}/3)')


def _busitu_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """不死途 AI: 满能量终结技→战技(饲饵)→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")



# ════════════ v6.9 千冶·刃机制（角色技能介绍/虚无/千冶·刃.txt）════════════

def _qianye_wrath_active(u) -> bool:
    return bool(u.extra.get('qianye_wrath'))


def _qianye_e6_gain_charge(state, u):
    """E6 grants at most one charge from HP damage/cost in the current turn."""
    if u.eidolon_rank < 6 or not _qianye_wrath_active(u) \
            or u.extra.get('qianye_e6_charge_used'):
        return
    u.extra['qianye_e6_charge_used'] = True
    _qianye_gain_charge(state, u, 1)
    state.log.append('  千冶·刃E6: 受伤/耗血获得1点充能')


def _reset_qianye_e6_charge_gate(state):
    """任意角色或敌方目标回合结束后，允许千冶E6再次获得充能。"""
    for unit in state.units:
        if unit.char.id == 'qianye':
            unit.extra.pop('qianye_e6_charge_used', None)


def _qianye_sync_wrath_enemy_effects(state, u):
    """E1 resistance reduction follows the live wrath field and each wave."""
    from engine.models.enemy import EnemyStatus
    active = _qianye_wrath_active(u) and u.eidolon_rank >= 1
    for enemy in state.enemies:
        if active and enemy.HP > 0:
            enemy.add_status(EnemyStatus(
                id='qianye_e1_res_down', name='千冶·刃E1', category='debuff',
                source='qianye', remaining_turns=-1, attributes={'res_down': 0.20},
            ))
        else:
            enemy.remove_status('qianye_e1_res_down')


def _qianye_apply_shaqizhaoshen(state, u, target, turns=2):
    """【煞火缠身】: DEF-30%+受伤+50% 2回合"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    target.add_status(EnemyStatus(id='qianye_shaqi', name='煞火缠身', category='debuff',
                                  source='qianye', remaining_turns=turns,
                                  attributes={'def_reduction': 0.30,
                                              'vulnerability': 0.50}))
    state.log.append(f'  【煞火缠身】: {target.name or target.id} DEF-30%+受伤+50%')


def _qianye_gain_charge(state, u, amount=1):
    """充能（上限9, E2: 7）: 达上限→回25能+额外战技(视为FUA)"""
    from engine.core.combat_sim import _gain_energy
    cap = 7 if u.eidolon_rank >= 2 else 9
    charge = u.extra.get('qianye_charge', 0) + amount
    if charge >= cap:
        u.extra['qianye_charge'] = 0
        _gain_energy(u, 25.0, state=state)
        # 额外战技（视为追加攻击）
        if u.current_hp > 1:
            _qianye_extra_skill(state, u)
            state.log.append(f'  千冶·刃天赋: 充能{cap}→回25能+额外战技')
        else:
            state.log.append(f'  千冶·刃天赋: 充能{cap}→回25能(生命≤1不施放额外战技)')
    else:
        u.extra['qianye_charge'] = charge


def _qianye_extra_skill(state, u):
    """天赋额外战技: 72%HP全体+4×24%HP弹射（视为追加攻击; E1后倒计时延后15%）"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _enemy_for_damage)
    if u.current_hp <= 1:
        return
    # 耗10%生命上限（不足则降至1）
    u.current_hp = max(1, u.current_hp - u.max_hp * 0.10)
    _qianye_e6_gain_charge(state, u)
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies() or state.enemies
    total = 0.0
    for t in alive:
        d = calculate_damage(stats, _enemy_for_damage(t), stats.HP, 72.0,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='talent', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    import random as _r
    for _ in range(4):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = _r.choice(alive_now)
        d = calculate_damage(stats, _enemy_for_damage(t), stats.HP, 24.0,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='talent', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    state.log.append(f'  千冶·刃额外战技: {total:.0f} (72%HP全体+4×24%, FUA)')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 额外战技(FUA)补气氛
    # E1: 额外战技后无量忿怒倒计时延后15%
    if u.eidolon_rank >= 1 and u.marker and u.marker.marker_id == 'qianye_wrath':
        u.marker.extra['next_av'] += AV_PER_TURN / max(u.marker.action_spd, 1.0) * 0.15
        state.log.append('  千冶·刃E1: 无量忿怒倒计时延后15%')


def _qianye_enter_wrath(state, u):
    """开启结界【无量忿怒】: CR+20%/CD+60%/普攻强化/解放战技/新终结技/70速倒计时"""
    from engine.core.combat_sim import TimedBuff
    u.extra['qianye_wrath'] = True
    if any(getattr(t, 'hook_name', '') == 'qianye_trace2'
           for t in (u.char.traces or [])):
        u.extra['qianye_taunt_mult'] = 3.0
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'qianye_wrath_buff']
    attrs = {'CRIT_RATE': 20.0, 'CRIT_DMG': 60.0}
    if any(getattr(t, 'hook_name', '') == 'qianye_trace2'
           for t in (u.char.traces or [])):
        attrs.update({'DMG_REDUCTION': 50.0, 'HEAL_BONUS': 50.0})
    u.buffs.append(TimedBuff(source_id='qianye', attributes=attrs,
                             remaining_turns=-1, param_id='qianye_wrath_buff',
                             source_name='无量忿怒'))
    sys = _ensure_marker_system(state)
    if u.marker and u.marker.is_alive:
        sys.despawn(state, u.marker)
    sys.spawn(state, u, 'qianye_wrath')
    _qianye_sync_wrath_enemy_effects(state, u)
    state.log.append('  千冶·刃: 展开结界【无量忿怒】(CR+20%/CD+60%, 70速倒计时)')


def _qianye_exit_wrath(state, u, fatal=False):
    """退出无量忿怒: 解除结界; 致命攻击→不死回50%生命上限; 行迹1能量<75%补至75%"""
    from engine.core.combat_sim import _gain_energy
    u.extra.pop('qianye_wrath', None)
    u.extra.pop('qianye_taunt_mult', None)
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'qianye_wrath_buff']
    _qianye_sync_wrath_enemy_effects(state, u)
    marker = u.marker
    if marker and marker.marker_id == 'qianye_wrath' and marker.is_alive:
        sys = state.extra.get('_marker_sys')
        if sys:
            sys.despawn(state, marker)
    if fatal:
        u.current_hp = min(u.max_hp, u.current_hp + u.max_hp * 0.50)
        state.log.append('  千冶·刃: 致命攻击不死, 结界解除回50%生命上限')
    # 行迹1: 结界解除时能量<75%补至75%
    if any(getattr(tr, 'hook_name', '') == 'qianye_trace1' for tr in (u.char.traces or [])):
        target = u.char.max_energy * 0.75
        if u.current_energy < target:
            _gain_energy(u, target - u.current_energy, state=state)
            state.log.append('  千冶·刃行迹1: 结界解除能量补至75%')
    state.log.append('  千冶·刃: 退出【无量忿怒】')


def _qianye_ult(state, u):
    """终结技: 全敌煞火缠身 + 耗20%生命上限开结界无量忿怒"""
    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0:
            _qianye_apply_shaqizhaoshen(state, u, e)
    # 耗20%生命上限（不足降至1）
    u.current_hp = max(1, u.current_hp - u.max_hp * 0.20)
    _qianye_enter_wrath(state, u)
    _qianye_e6_gain_charge(state, u)


def _qianye_new_ult(state, u):
    """无量忿怒新终结技【千冶铸一，万劫烬灭】（E6倍率×150%; 施放清空能量, 行迹1恢复溢出）"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _enemy_for_damage)
    u.current_energy = 0
    stats = _build_effective_stats(u, state)
    mult = 1.50 if u.eidolon_rank >= 6 else 1.0  # E6: 倍率×150%
    alive = state.alive_enemies() or state.enemies
    total = 0.0
    for t in alive:
        d = calculate_damage(stats, _enemy_for_damage(t), stats.HP, 300.0 * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='ultimate', crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    state.log.append(f'  千冶铸一，万劫烬灭: {total:.0f} (300%HP×{mult})')
    # 行迹1·百炼骨: 施放终结技后清空溢出能量并恢复
    overflow = u.extra.pop('qianye_overflow', 0.0)
    if overflow > 0:
        u.current_energy = min(u.char.max_energy, u.current_energy + overflow)
        state.log.append(f'  千冶·刃行迹1: 溢出能量{overflow:.0f}恢复')
    # 新终结技后仍保持无量忿怒（txt: 解放战技并获得全新终结技, 未说消耗结界）


def _qianye_skill(state, u, skill_key):
    """战技: 耗10%生命上限 72%HP全体+4×24%HP弹射（不耗SP; 生命≤1或非无量忿怒不可用）;
    无量忿怒期解放战技(与天赋额外战技同实现)"""
    if not _qianye_wrath_active(u):
        state.log.append('  [WARN] 千冶·刃: 未处于无量忿怒, 战技不可用')
        return
    if u.current_hp <= 1:
        state.log.append('  [WARN] 千冶·刃: 当前生命值≤1, 无法施放战技')
        return
    u.current_hp = max(1, u.current_hp - u.max_hp * 0.10)  # 战技HP消耗, 伤害由 _use_skill 通用管线结算
    _qianye_e6_gain_charge(state, u)
    state.log.append('  千冶·刃解放战技: 耗10%生命上限')


def _qianye_on_ally_attack(state, attacker):
    """天赋: 结界期我方每次攻击→目标煞火缠身+1充能"""
    qianye = next((x for x in state.units
                   if x.char.id == 'qianye' and x.is_alive), None)
    if qianye is None or not _qianye_wrath_active(qianye):
        return
    for t in state.extra.get('last_attack_targets', []):
        if t is not None and getattr(t, 'HP', 0) > 0:
            _qianye_apply_shaqizhaoshen(state, qianye, t)
    _qianye_gain_charge(state, qianye, 1)


def _qianye_tick(state, u):
    """Compatibility hook; dispatch normally remains owned by TimelineMarker."""
    marker = u.marker
    if (_qianye_wrath_active(u) and marker
            and marker.marker_id == 'qianye_wrath'
            and marker.is_alive
            and state.current_av >= marker.extra.get('next_av', float('inf'))):
        _qianye_wrath_marker_action(state, marker)


def _qianye_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """千冶·刃 AI: all active actions use the common skill pipeline."""
    if _qianye_wrath_active(u):
        if u.current_energy >= u.char.max_energy:
            _use_skill(u, state, 'skill_enhanced')
        elif u.extra.get('qianye_charge', 0) >= 5:
            _use_skill(u, state, 'skill')
        else:
            _use_skill(u, state, 'basic_attack_enhanced')
        _qianye_tick(state, u)
        return
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    else:
        _use_skill(u, state, "basic_attack")



# ════════════ v6.10 黄泉机制（角色技能介绍/虚无/黄泉.txt, 特殊能量·残梦9）════════════

def _acheron_gain_dream(state, u, amt=1, jizhen_target=None):
    """残梦+集真赤: 残梦上限9(溢出转四相断我); 集真赤1层"""
    if u.char.id != 'acheron':
        return
    dream = u.extra.get('acheron_dream', 0) + amt
    if dream > 9:
        # 行迹1·赤鬼: 溢出→四相断我（上限3层）
        overflow = dream - 9
        u.extra['acheron_sixiang'] = min(3, u.extra.get('acheron_sixiang', 0) + overflow)
        dream = 9
    u.extra['acheron_dream'] = dream
    if jizhen_target is not None and getattr(jizhen_target, 'HP', 0) > 0:
        jizhen_target.extra['acheron_jizhen'] = jizhen_target.extra.get('acheron_jizhen', 0) + 1
        state.log.append(f'  【集真赤】: {jizhen_target.name or jizhen_target.id} '
                         f'{jizhen_target.extra["acheron_jizhen"]}层')
    state.log.append(f'  黄泉: 残梦+{amt} → {dream}/9'
                     f'{" (四相断我" + str(u.extra.get("acheron_sixiang", 0)) + "层)" if dream >= 9 else ""}')


def _acheron_apply_jizhen(state, u, target, layers=1):
    """为指定目标附上集真赤"""
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    target.extra['acheron_jizhen'] = target.extra.get('acheron_jizhen', 0) + layers
    state.log.append(f'  【集真赤】: {target.name or target.id} {target.extra["acheron_jizhen"]}层')


def _acheron_jizhen_transfer(state):
    """集真赤离场转移: 目标死亡后转移给集真赤最多的存活目标"""
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            layers = e.extra.pop('acheron_jizhen', 0)
            if layers <= 0:
                continue
            alive = [x for x in state.enemies if getattr(x, 'HP', 0) > 0]
            if not alive:
                continue
            top = max(alive, key=lambda x: x.extra.get('acheron_jizhen', 0))
            top.extra['acheron_jizhen'] = top.extra.get('acheron_jizhen', 0) + layers
            state.log.append(f'  集真赤转移: {layers}层 → {top.name or top.id}')


def _acheron_talent_on_debuff(u, state, target, **_kwargs):
    """天赋: 任意单位施放技能期间使敌陷入负面→+1残梦+集真赤1层(每次施放最多1次)"""
    acheron = next((x for x in state.units
                    if x.char.id == 'acheron' and x.is_alive), None)
    if acheron is None:
        return
    if u.extra.get('acheron_talent_triggered'):
        return  # 本次施放已触发
    u.extra['acheron_talent_triggered'] = True
    _acheron_gain_dream(state, acheron, 1)
    _acheron_apply_jizhen(state, acheron, target, 1)


def _acheron_skill(state, u):
    """战技: +1残梦+集真赤1层（战技直接效果, 非负面触发）"""
    alive = state.alive_enemies() or state.enemies
    target = alive[0] if alive else None
    if target is not None:
        _acheron_gain_dream(state, u, 1)
        _acheron_apply_jizhen(state, u, target, 1)


def _acheron_apply_entry_effects(state):
    """Acheron E4: mark each enemy entering the current wave."""
    acheron = next((x for x in state.units
                    if x.char.id == 'acheron' and x.is_alive and x.eidolon_rank >= 4), None)
    if acheron is None:
        return
    from engine.models.enemy import EnemyStatus
    for enemy in state.enemies:
        existing = next((status for status in enemy.statuses
                          if status.id == 'acheron_e4_ultimate_vulnerability'), None)
        if existing is not None:
            existing.remaining_turns = -1
            existing.attributes['vulnerability_ultimate'] = 0.08
            continue
        enemy.add_status(EnemyStatus(
            id='acheron_e4_ultimate_vulnerability',
            name='终结技易伤',
            category='debuff',
            source='acheron',
            remaining_turns=-1,
            attributes={'vulnerability_ultimate': 0.08},
        ))


def _acheron_apply_leixin(u, stacks):
    """Refresh the trace-3 damage buff from the current layer count."""
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'acheron_leixin']
    if stacks <= 0:
        return
    u.buffs.append(TimedBuff(
        source_id='acheron',
        attributes={'DMG_BONUS_ALL': 30.0 * stacks},
        remaining_turns=3,
        param_id='acheron_leixin',
        source_name='黄泉·雷心',
    ))


def _acheron_trace3_damage_multiplier(u) -> float:
    return 1.0 + 0.30 * min(3, u.extra.get('acheron_leixin', 0))


def _acheron_ult(state, u):
    """终结技: 耗9残梦; 3×啼泽雨斩(24%ATK单体, 消最多3层集真赤→全敌15%ATK+每层提升至60%)
    +黄泉返渡(120%ATK全体+清集真赤+行迹3额外6×25%ATK弹射);
    终结技期无视弱点削韧+全抗-20%; 行迹3增伤30%×3层"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _enemy_for_damage,
                                        _apply_toughness_damage)
    import random as _r
    if u.extra.get('acheron_dream', 0) < 9:
        state.log.append('  [WARN] 黄泉: 残梦不足9, 无法施放终结技')
        return
    u.extra['acheron_dream'] = 0
    stats = _build_effective_stats(u, state)
    # 行迹3·雷心: 增伤30%×3层（啼泽雨斩击中集真赤目标时叠加, 3回合）
    trace3 = any(getattr(tr, 'hook_name', '') == 'acheron_trace3' for tr in (u.char.traces or []))
    # 终结技期全抗-20%; E6 adds another 20% ultimate RES PEN.
    stats = copy.deepcopy(stats)
    stats.RES_PEN_ALL += 0.20 + (0.20 if u.eidolon_rank >= 6 else 0.0)
    total = 0.0
    # The displayed ultimate toughness value is the full action total. Apply it
    # once to every target through the shared break lifecycle.
    for enemy in list(state.alive_enemies()):
        before = enemy.HP
        _apply_toughness_damage(state, u, enemy, 20.0, '雷', 'ultimate', stats)
        _record_kill_after_damage(state, u, enemy, before)
    # 啼泽雨斩 ×3（逐次对主目标; 行迹3: 击中集真赤目标→增伤叠层）
    for i in range(3):
        alive_now = state.alive_enemies()
        if not alive_now:
            break
        t = alive_now[0]
        if trace3 and t.extra.get('acheron_jizhen', 0) > 0:
            stacks = min(3, u.extra.get('acheron_leixin', 0) + 1)
            u.extra['acheron_leixin'] = stacks
            _acheron_apply_leixin(u, stacks)
            state.log.append(f'  行迹3·雷心: 增伤30%×{stacks}层')
        # 消集真赤（最多3层）→ 全敌15%ATK+每层提升至60%
        jz = min(3, t.extra.get('acheron_jizhen', 0))
        t.extra['acheron_jizhen'] = t.extra.get('acheron_jizhen', 0) - jz
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t, 'ultimate'), stats.ATK, 24.0,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate', crit_mode='expected')
        d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                           * _acheron_trace3_damage_multiplier(u))
        _commit_enemy_damage(
            state, u, t, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        if jz > 0:
            for e in state.enemies:
                if getattr(e, 'HP', 0) <= 0:
                    continue
                scale = 15.0 + jz * 15.0  # 15%基础+每层15%→最多60%
                before = e.HP
                d2 = calculate_damage(stats, _enemy_for_damage(e, 'ultimate'), stats.ATK, scale,
                                      'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                                      true_dmg_ratio=state.realm_true_dmg,
                                      skill_type='ultimate', crit_mode='expected')
                d2.final_damage *= (_acheron_original_damage_multiplier(u, state)
                                    * _acheron_trace3_damage_multiplier(u))
                _commit_enemy_damage(
                    state, u, e, d2.final_damage,
                    cipher_record_amount=d2.final_damage / (1.0 + state.realm_true_dmg))
                total += d2.final_damage
            state.log.append(f'  啼泽雨斩消{jz}层集真赤: 全敌{15+jz*15:.0f}%ATK')
    # 黄泉返渡: 120%ATK全体+移除所有集真赤+行迹3额外6×25%ATK弹射
    for e in state.alive_enemies():
        before = e.HP
        d = calculate_damage(stats, _enemy_for_damage(e, 'ultimate'), stats.ATK, 120.0,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate', crit_mode='expected')
        d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                           * _acheron_trace3_damage_multiplier(u))
        _commit_enemy_damage(
            state, u, e, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
    for e in state.enemies:
        e.extra.pop('acheron_jizhen', None)
    if trace3:
        for _ in range(6):
            alive_now = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
            if not alive_now:
                break
            t = _r.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t, 'ultimate'), stats.ATK, 25.0,
                                 'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                                 true_dmg_ratio=state.realm_true_dmg,
                                 skill_type='ultimate', crit_mode='expected')
            d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                               * _acheron_trace3_damage_multiplier(u))
            _commit_enemy_damage(
                state, u, t, d.final_damage,
                cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
            total += d.final_damage
        state.log.append('  行迹3: 黄泉返渡额外6×25%ATK')
    sixiang = u.extra.pop('acheron_sixiang', 0)
    if sixiang > 0:
        for _ in range(sixiang):
            alive_now = state.alive_enemies()
            target = _r.choice(alive_now) if alive_now else None
            _acheron_gain_dream(state, u, 1, jizhen_target=target)
        state.log.append(f'  【四相断我】: 终结技后消耗{sixiang}层→残梦+{sixiang}')
    u.total_damage_dealt += total
    u.damage_log.append(('残梦尽染，一刀缭断', total, 'ultimate'))
    state.log.append(f'  黄泉终结技: {total:.0f} (3×啼泽雨斩+返渡)')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 0倍率终结技补气氛


def _acheron_tick(state, u):
    """黄泉回合开始: E2 +1残梦+集真赤(集真赤最多目标)"""
    if u.eidolon_rank >= 2:
        alive = state.alive_enemies() or state.enemies
        if alive:
            top = max(alive, key=lambda x: x.extra.get('acheron_jizhen', 0))
            _acheron_gain_dream(state, u, 1, jizhen_target=top)
            state.log.append('  黄泉E2: 回合开始+1残梦+集真赤')


def _acheron_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """黄泉 AI: 残梦满9→终结技; SP>0→战技; 否则普攻"""
    if u.extra.get('acheron_dream', 0) >= 9:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")



# ════════════ v6.10 飞霄机制（角色技能介绍/巡猎/飞霄.txt, 特殊能量·飞黄12）════════════

def _feixiao_gain_fly(u, amt=1):
    """飞黄（上限12）"""
    u.extra['feixiao_fly'] = min(12, u.extra.get('feixiao_fly', 0) + amt)
    return u.extra['feixiao_fly']


def _feixiao_fua(state, u, target, from_skill=False):
    """天赋FUA: 110%ATK风伤(破韧目标削韧5); 发动时自身增伤60% 2回合;
    E2: 每FUA+1飞黄(每回合最多6次); E6: FUA视为终结技伤害+倍率+140%"""
    from engine.core.combat_sim import (TimedBuff, _build_effective_stats,
                                        _apply_toughness_damage,
                                        _enemy_for_damage,
                                        _record_kill_after_damage,
                                        calculate_damage)
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    stats = _build_effective_stats(u, state)
    mult = 1.0
    if u.eidolon_rank >= 6:
        mult = 2.40  # E6: FUA倍率+140%
        stats = copy.deepcopy(stats)
        stats.RES_PEN_ALL += 0.20
    skill_type = 'ultimate' if u.eidolon_rank >= 6 else 'talent'
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target, skill_type), stats.ATK, 110.0 * mult,
                         'direct', '风', 80, stats.CRIT_RATE >= 0.5,
                         true_dmg_ratio=state.realm_true_dmg,
                         skill_type=skill_type,
                         attack_type='follow_up', crit_mode='expected')
    _commit_enemy_damage(
        state, u, target, d.final_damage,
        cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
    u.total_damage_dealt += d.final_damage
    _record_kill_after_damage(state, u, target, before)
    break_before = target.HP
    _apply_toughness_damage(
        state, u, target,
        10.0 if u.eidolon_rank >= 4 else 5.0,
        '风', 'talent', stats,
    )
    _record_kill_after_damage(state, u, target, break_before)
    # FUA时自身增伤60% 2回合
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'feixiao_fua_buff']
    u.buffs.append(TimedBuff(source_id='feixiao', attributes={'DMG_BONUS_ALL': 60.0},
                             remaining_turns=2, param_id='feixiao_fua_buff',
                             source_name='雷狩'))
    if u.eidolon_rank >= 4:
        u.buffs = [b for b in u.buffs
                   if getattr(b, 'param_id', '') != 'feixiao_e4_speed']
        u.buffs.append(TimedBuff(
            source_id='feixiao',
            attributes={'SPD_PERCENT': 8.0},
            remaining_turns=2,
            param_id='feixiao_e4_speed',
            source_name='飞霄E4·驱飓听冰',
        ))
    u.extra['feixiao_any_fua_this_turn'] = True
    # E2: 每FUA+1飞黄（每回合最多6次）
    if u.eidolon_rank >= 2:
        cnt = u.extra.get('feixiao_e2_count', 0)
        if cnt < 6:
            u.extra['feixiao_e2_count'] = cnt + 1
            _feixiao_gain_fly(u, 1)
            state.log.append(f'  飞霄E2: FUA+1飞黄({u.extra["feixiao_fly"]}/12)')
    _feixiao_count_attack(state, u)
    state.log.append(f'  飞霄FUA: {d.final_damage:.0f} (110%ATK{"×2.4" if u.eidolon_rank >= 6 else ""})')
    _qingge_notify_attack(state, u, dealt=d.final_damage > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _feixiao_count_attack(state, u, is_ult=False):
    """天赋: 我方每2次攻击+1飞黄（终结技不计; 行迹1上回合未FUA计入1次）"""
    if is_ult:
        return
    feixiao = next((x for x in state.units
                    if x.char.id == 'feixiao' and x.is_alive), None)
    if feixiao is None:
        return
    cnt = feixiao.extra.get('feixiao_attack_count', 0) + 1
    feixiao.extra['feixiao_attack_count'] = cnt
    if cnt >= 2:
        feixiao.extra['feixiao_attack_count'] = 0
        _feixiao_gain_fly(feixiao, 1)
        state.log.append(f'  飞霄天赋: 每2次攻击+1飞黄({feixiao.extra["feixiao_fly"]}/12)')


def _feixiao_on_ally_attack(state, attacker):
    """天赋: 队友攻击后立即FUA 110%ATK（每回合最多1次, 飞霄回合开始重置）"""
    if attacker.char.id == 'feixiao':
        return
    feixiao = next((x for x in state.units
                    if x.char.id == 'feixiao' and x.is_alive), None)
    if feixiao is None or feixiao.extra.get('feixiao_fua_used'):
        return
    alive = state.alive_enemies() or state.enemies
    targets = state.extra.get('last_attack_targets', [])
    target = targets[0] if targets else (alive[0] if alive else None)
    if target is None:
        return
    feixiao.extra['feixiao_fua_used'] = True
    _feixiao_fua(state, feixiao, target)


def _feixiao_skill(state, u):
    """战技: 200%ATK(引擎)+立即天赋FUA+行迹3 ATK+48% 3回合"""
    from engine.core.combat_sim import TimedBuff
    if any(getattr(tr, 'hook_name', '') == 'feixiao_trace3' for tr in (u.char.traces or [])):
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'feixiao_trace3']
        u.buffs.append(TimedBuff(source_id='feixiao', attributes={'ATK_PERCENT': 48.0},
                                 remaining_turns=3, param_id='feixiao_trace3',
                                 source_name='行迹·电举'))
        state.log.append('  飞霄行迹3: 战技ATK+48% 3回合')
    alive = state.alive_enemies() or state.enemies
    target = alive[0] if alive else None
    if target is not None:
        _feixiao_fua(state, u, target)


def _feixiao_ult(state, u):
    """终结技: 耗6飞黄; 6×闪裂刃舞/钺贯天冲(60%ATK, 破韧+30%/未破韧+30%)+160%ATK末段;
    无视弱点削韧+未破韧效率+100%; 行迹2视为FUA+FUA暴伤+36%; E1终结技伤害+10%×5层"""
    from engine.core.combat_sim import (_apply_toughness_damage,
                                        _build_effective_stats, calculate_damage,
                                        _enemy_for_damage, _record_kill_after_damage)
    if u.extra.get('feixiao_fly', 0) < 6:
        state.log.append('  [WARN] 飞霄: 飞黄不足6, 无法施放终结技')
        return
    u.extra['feixiao_fly'] -= 6
    stats = _build_effective_stats(u, state)
    if u.eidolon_rank >= 6:
        stats = copy.deepcopy(stats)
        stats.RES_PEN_ALL += 0.20
    alive = state.alive_enemies() or state.enemies
    target = alive[0] if alive else None
    if target is None:
        return
    # E1: 终结技伤害+10%×5层（本次终结技内累计, 行动结束清空）
    e1_stack = 0
    total = 0.0
    for i in range(6):
        if getattr(target, 'HP', 0) <= 0:
            break
        # 闪裂刃舞(破韧+30%)/钺贯天冲(未破韧+30%) 交替
        scale = 60.0
        if target.is_broken:
            scale *= 1.30  # 闪裂刃舞: 破韧目标+30%
        else:
            scale *= 1.30  # 钺贯天冲: 未破韧+30%
        before = target.HP
        d = calculate_damage(stats, _enemy_for_damage(target, 'ultimate'), stats.ATK, scale,
                             'direct', '风', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate',
                             attack_type='follow_up',
                             crit_mode='expected')
        if u.eidolon_rank >= 1:
            d.final_damage *= (1.0 + 0.10 * e1_stack)
        _, killed = _commit_enemy_damage(
            state, u, target, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        _record_kill_after_damage(state, u, target, before)
        break_before = target.HP
        _apply_toughness_damage(
            state, u, target,
            5.0 if target.is_broken else 10.0,
            '风', 'ultimate', stats,
        )
        _record_kill_after_damage(state, u, target, break_before)
        if u.eidolon_rank >= 1:
            e1_stack = min(5, e1_stack + 1)  # E1: 每段后+10%层
    # 末段160%ATK
    if getattr(target, 'HP', 0) > 0:
        before = target.HP
        d = calculate_damage(stats, _enemy_for_damage(target, 'ultimate'), stats.ATK, 160.0,
                             'direct', '风', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate', attack_type='follow_up',
                             crit_mode='expected')
        if u.eidolon_rank >= 1:
            d.final_damage *= (1.0 + 0.10 * e1_stack)
        _commit_enemy_damage(
            state, u, target, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        break_before = target.HP
        _apply_toughness_damage(
            state, u, target,
            5.0 if target.is_broken else 10.0,
            '风', 'ultimate', stats,
        )
        _record_kill_after_damage(state, u, target, break_before)
    u.total_damage_dealt += total
    u.damage_log.append(('凿破大荒', total, 'ultimate'))
    state.log.append(f'  飞霄终结技: {total:.0f} (6段60%×1.3+160%, 飞黄{u.extra["feixiao_fly"]}/12)')
    _qingge_notify_attack(state, u, dealt=total > 0)  # v7.1.0 P1: 0倍率终结技补气氛


def _feixiao_tick(state, u):
    """飞霄回合开始: 重置FUA次数+E2计数; 行迹1上回合未FUA计入1次攻击"""
    previous_turn_fua = bool(u.extra.get('feixiao_any_fua_this_turn', False))
    u.extra['feixiao_last_turn_fua'] = previous_turn_fua
    u.extra.pop('feixiao_any_fua_this_turn', None)
    u.extra['feixiao_fua_used'] = False
    u.extra['feixiao_e2_count'] = 0
    if any(getattr(tr, 'hook_name', '') == 'feixiao_trace1' for tr in (u.char.traces or [])):
        if not previous_turn_fua:
            cnt = u.extra.get('feixiao_attack_count', 0) + 1
            u.extra['feixiao_attack_count'] = cnt
            if cnt >= 2:
                u.extra['feixiao_attack_count'] = 0
                _feixiao_gain_fly(u, 1)
                state.log.append(f'  飞霄行迹1: 上回合未FUA计入1次攻击→+1飞黄({u.extra["feixiao_fly"]}/12)')


def _feixiao_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """飞霄 AI: 飞黄满6→终结技; SP>0→战技(含FUA); 否则普攻"""
    if u.extra.get('feixiao_fly', 0) >= 6:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")
