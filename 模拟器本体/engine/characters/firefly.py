"""流萤（试点 M3；完全燃烧入口在引擎 param_id 管线, 出口/倒计时/秘技波次在此）"""

import copy
import random
from engine.runtime import SimUnit, _hook_owner
from engine.core.combat_engine import _apply_toughness_damage, _build_effective_stats, _commit_enemy_damage, _use_skill
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus


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


def _firefly_refresh_dr(u, state):
    """流萤天赋·茧式源火中枢: HP越低减伤越多（最多40%, HP≤20%满）; 完全燃烧维持最大"""
    # v6.5.1: on_hp_loss 广播 u 可能是忆灵(MemSpriteUnit.char 无 id) → 先过滤
    from engine.runtime import SimUnit
    if not isinstance(u, SimUnit) or u.char.id != 'firefly' or not u.is_alive:
        return
    if u.extra.get('combustion'):
        dr = 0.40
    else:
        ratio = 1.0 - u.current_hp / max(u.max_hp, 1)
        dr = 0.40 * min(ratio / 0.8, 1.0)
    cur = u.extra.get('firefly_dr_current', 0.0)
    u.base_stats.DMG_REDUCTION += dr - cur
    u.extra['firefly_dr_current'] = dr
    state.log.append(f'  天赋·源火中枢: 减伤{dr*100:.0f}% (HP {u.current_hp:.0f}/{u.max_hp:.0f})')


def _trace_firefly_dr_hp_loss(u, state, **kw):
    """减伤刷新（受击/耗血后）"""
    _firefly_refresh_dr(u, state)


def _trace_firefly_dr_turn(u, state, **kw):
    """减伤刷新（回合开始）"""
    _firefly_refresh_dr(u, state)


def _trace_firefly_talent_start(u, state, **kw):
    """天赋: 战斗开始时能量不足50%则恢复至50%"""
    if u.char.id != 'firefly':
        return
    if u.current_energy < u.char.max_energy * 0.5:
        u.current_energy = u.char.max_energy * 0.5
        state.log.append('  天赋: 战斗开始能量恢复至50%')


def _trace_firefly_talent_cleanse(u, state, **kw):
    """天赋: 能量恢复至上限时解除自身所有负面效果"""
    if u.char.id != 'firefly':
        return
    if u.current_energy >= u.char.max_energy and u.statuses:
        u.statuses.clear()
        state.log.append('  天赋: 能量满解除自身所有负面')


def _trace_firefly_t1_pull(u, state, **kw):
    """行迹1·偏时迸发: 强化攻击使目标击破时倒计时行动延后10%（每燃烧期最多3次）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    actor = kw.get('actor')
    if actor is not None and actor is not owner:
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    if owner.char.id != 'firefly' or not owner.extra.get('combustion'):
        return
    if owner.extra.get('ff_trace1_pull', 0) >= 3:
        return
    m = owner.marker
    if not (m and m.is_alive):
        return
    m.extra['delay_pending'] = m.extra.get('delay_pending', 0.0) + 10000.0 / 70.0 * 0.10
    owner.extra['ff_trace1_pull'] = owner.extra.get('ff_trace1_pull', 0) + 1
    state.log.append('  行迹1: 完全燃烧倒计时行动延后10%')


def _trace_firefly_t3_atk_to_be(u, state, **kw):
    """行迹3·过载核心: 攻击力>1800时每超10点击破特攻+0.8%"""
    if u.char.id != 'firefly':
        return
    if u.base_stats.ATK > 1800:
        bonus = (u.base_stats.ATK - 1800) / 10.0 * 0.008
        u.base_stats.BREAK_EFFECT += bonus
        state.log.append(f'  行迹3·过载核心: 击破特攻+{bonus*100:.1f}%')


def _eid_firefly_e2_kill(u, state, **kw):
    """流萤E2: 完全燃烧下强化攻击击杀→萨姆额外回合（每回合1次）"""
    if u.char.id != 'firefly' or not u.extra.get('combustion') or u.extra.get('ff_e2_used'):
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    u.extra['ff_e2_used'] = True
    state.extra.setdefault('extra_turns', []).append((u, 'extra'))
    state.log.append('  E2: 击杀→萨姆额外回合')


def _eid_firefly_e2_break(u, state, **kw):
    """流萤E2: 完全燃烧下强化攻击使目标击破→萨姆额外回合（每回合1次）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    actor = kw.get('actor')
    if actor is not None and actor is not owner:
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    if owner.char.id != 'firefly' or not owner.extra.get('combustion') or owner.extra.get('ff_e2_used'):
        return
    owner.extra['ff_e2_used'] = True
    state.extra.setdefault('extra_turns', []).append((owner, 'extra'))
    state.log.append('  E2: 击破→萨姆额外回合')


def _eid_firefly_e2_reset(u, state, **kw):
    """流萤E2: 萨姆回合开始时重置可触发次数"""
    if u.char.id == 'firefly':
        u.extra['ff_e2_used'] = False


def _tech_firefly(state, u, is_opener):
    """流萤: 标记秘技生效——进战首波 + 每波次: 全敌火弱点2回合 + 200%ATK火伤 + 削韧20
    （流萤.txt 秘技·Δ指令-焦土陨击: 每个波次开始时）"""
    state.extra['firefly_tech_active'] = True
    _apply_firefly_tech_wave(state, u)


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


CHAR_ID = "firefly"
AI = firefly_ai
TECHNIQUE = _tech_firefly
MARKERS = {"firefly_countdown": _firefly_countdown_action}


# M4 收官批: 击破配装策略随角色（原 relic_optimizer.BREAK_CHAR_CONFIG 条目）
BREAK_CONFIG = {'spd_target': 145.0, 'exclude_atk': True}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _firefly_combustion_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['firefly_combustion']: 完全燃烧进入。"""
    if u.char.id != 'firefly':
        return None
    # v5.3 流萤完全燃烧状态（进入）
    if u.extra.get('combustion'):
        state.log.append('  [WARN] 已在完全燃烧状态')
        return True
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
    return True


EFFECT_TAKEOVERS = {'firefly_combustion': _firefly_combustion_takeover}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _firefly_sp_cost_override(u, state, sp_cost, skill_key):
    """PHASE sp_cost_override: E1 强化战技不消耗战技点（→新值|None）。"""
    # v5.3 流萤E1: 强化战技不消耗战技点
    if u.eidolon_rank >= 1 and skill_key == 'skill_enhanced':
        return 0
    return None


def _firefly_energy_gain_override(u, state, skill_key):
    """PHASE energy_gain_override: 战技不回能（固定恢复走 post_hp_cast 段）。"""
    if skill_key == 'skill':
        return 0
    return None


def _firefly_post_hp_cast(u, state, skill_key):
    """PHASE post_hp_cast: 战技固定恢复60%能量上限 + 行动提前25%（满级档）。"""
    from engine.runtime import AV_PER_TURN
    from engine.core.combat_engine import _effective_spd, _gain_energy
    # v5.3 流萤战技: 固定恢复60%能量上限 + 自身行动提前25%（满级档）
    if skill_key != 'skill':
        return None
    _gain_energy(u, 0.6, state=state, percent=True)
    navs = state.extra.get('navs', {})
    uidx = state.units.index(u) if u in state.units else -1
    if uidx >= 0 and uidx in navs:
        navs[uidx] = max(0, navs[uidx] - (AV_PER_TURN / _effective_spd(u, state)) * 0.25)
    state.log.append('  战技: 回60%能量上限, 行动提前25%')
    return None


def _firefly_post_hp_skill_override(u, state, skill, skill_key):
    """PHASE post_hp_skill_override: 强化战技倍率=击破特攻依赖（→新skill|None）。"""
    # v5.3 流萤强化战技: 倍率=击破特攻依赖（主(0.2×BE+200)%, 相邻(0.1×BE+100)%, 最多算360%BE）
    if skill_key == 'skill_enhanced' and skill.multipliers:
        be = min(_build_effective_stats(u, state).BREAK_EFFECT, 3.6)
        skill = copy.deepcopy(skill)  # 动态倍率不污染角色数据
        for m in skill.multipliers:
            m.scale = (be * 0.1 * 100 + 100.0) if m.target == 'adjacent' else (be * 0.2 * 100 + 200.0)
        return skill
    return None


PHASE_HOOKS = {'sp_cost_override': _firefly_sp_cost_override,
               'energy_gain_override': _firefly_energy_gain_override,
               'post_hp_cast': _firefly_post_hp_cast,
               'post_hp_skill_override': _firefly_post_hp_skill_override}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _firefly_effects_pre_cast(u, state, skill, skill_key):
    """PHASE effects_pre_cast: 强化战技效果预挂（火弱点先于伤害/削韧生效）。→True=已预挂"""
    from engine.core.combat_engine import _apply_skill_effects
    if skill_key == 'skill_enhanced':
        # 火弱点必须在本次伤害/削韧前生效。
        _apply_skill_effects(u, state, skill, skill_key)
        return True
    return None


PHASE_HOOKS['effects_pre_cast'] = _firefly_effects_pre_cast
