"""Archer——Fate 联动·巡猎·量子（v7.21.0 录入, 角色技能介绍/巡猎/archer.txt）

核心机制:
- 充能（u.extra['archer_charge'] ≤4）: 终结技+2 / 行迹2 开战+1 / 秘技+1; 心眼追击每
  次消耗 1 点
- 回路连接（u.extra['archer_circuit']）: 战技进入; 战技伤害+100%（Lv10 口径）叠 2 层
  （E6→3）持续至退出; 每次战技后 append X 轴额外回合（"本回合不结束"口径, 不 tick 常规
  Buff）; 主动施放 5 次战技 / SP 不足 2 / 波次敌人更替 → 退出（清层数与计数）
- 心眼（真）: 队友攻击类技能后（SETTLE 广播）→ 消耗 1 充能对主目标追击 200% 量子 +
  回 1 战技点（目标死亡转随机存活）
- 行迹1: SP 上限+2（sparkle 模式, 死亡对称回减）; 行迹2: 开战+1 充能;
  行迹3: SP 获得后 ≥4 → 暴伤+120% 1回合（sp_change 观察者）
- E1: 单回合 3 次战技 → +2 SP; E2: 终结技时敌量子抗性-20%+量子弱点 2回合;
  E4: 终结技伤害+150%; E6: 回合开始+1 SP / 战技叠层上限 3 / 战技无视防 20%
- 秘技千里眼（进战）: 全敌 200% 量子 + 1 充能
"""
import copy
import random

from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _flat_toughness_with_break,
    _gain_energy, _gain_skill_points,
    _skill_level_factor, _use_skill,
)

CHAR_ID = "archer"
ELEMENT = "量子"
CHARGE_CAP = 4


def _archer_find(state):
    return next((x for x in state.units if x.char.id == CHAR_ID and x.is_alive), None)


def _archer_charge_gain(u, n, state=None, note=''):
    if n <= 0:
        return
    u.extra['archer_charge'] = min(CHARGE_CAP, u.extra.get('archer_charge', 0) + n)
    if state is not None:
        state.log.append(f'  充能+{n}{note}({u.extra["archer_charge"]:.0f}/{CHARGE_CAP})')


def _alive_enemies(state):
    return [e for e in state.enemies if getattr(e, 'HP', 0.0) > 0]


def _archer_deal(state, u, stats, target, scale, toughness, skill_type,
                 attack_type='active'):
    if target is None or getattr(target, 'HP', 0.0) <= 0:
        return 0.0
    d = calculate_damage(stats, _enemy_for_damage(target, skill_type), stats.ATK, scale,
                         'direct', ELEMENT, 80, stats.CRIT_RATE >= 0.5,
                         skill_type=skill_type, attack_type=attack_type,
                         crit_mode='expected')
    _commit_enemy_damage(state, u, target, d.final_damage)
    if target.toughness > 0:
        _flat_toughness_with_break(state, u, target, toughness, ELEMENT, skill_type, stats)
    return d.final_damage


def _archer_circuit_stacks_cap(u):
    return 3 if u.eidolon_rank >= 6 else 2


def _archer_exit_circuit(u, state=None):
    u.extra.pop('archer_circuit', None)
    u.extra.pop('archer_circuit_casts', None)
    u.extra.pop('archer_circuit_stacks', None)
    u.extra.pop('archer_circuit_enemy_snapshot', None)
    u.extra.pop('archer_turn_skill_count', None)
    if state is not None:
        state.log.append('  退出【回路连接】')


def _archer_skill_cast(state, u):
    """伪·螺旋剑: 单体 360%; 进入/维持回路连接; 战技后连入 X 轴。"""
    stats = _build_effective_stats(u, state)
    lf = _skill_level_factor(u, 'skill')
    per_stack = 1.00 * lf  # 战技伤害+100%(Lv10口径)/层
    stacks_now = u.extra.get('archer_circuit_stacks', 0)
    if stacks_now > 0 or u.eidolon_rank >= 6:
        stats = copy.deepcopy(stats)
        if stacks_now > 0:
            stats.DMG_BONUS_BY_SKILL_TYPE['skill'] = \
                stats.DMG_BONUS_BY_SKILL_TYPE.get('skill', 0.0) + per_stack * stacks_now
        if u.eidolon_rank >= 6:
            stats.DEF_PEN += 0.20
    tsc = _alive_enemies(state)[0] if _alive_enemies(state) else None
    total = _archer_deal(state, u, stats, tsc, 360.0 * lf, 20.0, 'skill')
    u.total_damage_dealt += total
    # 回路连接: 进入/计数/叠层
    if not u.extra.get('archer_circuit'):
        u.extra['archer_circuit'] = True
        u.extra['archer_circuit_casts'] = 0
        u.extra['archer_circuit_enemy_snapshot'] = tuple(
            id(e) for e in _alive_enemies(state))
        state.log.append('  进入【回路连接】')
    # 波次敌人更替 → 退出（进入时快照对比; 每次战技检查）
    snap = u.extra.get('archer_circuit_enemy_snapshot')
    if snap is not None and tuple(id(e) for e in _alive_enemies(state)) != snap:
        _archer_exit_circuit(u, state)
        return
    u.extra['archer_circuit_casts'] = u.extra.get('archer_circuit_casts', 0) + 1
    u.extra['archer_turn_skill_count'] = u.extra.get('archer_turn_skill_count', 0) + 1
    cap = _archer_circuit_stacks_cap(u)
    cur = u.extra.get('archer_circuit_stacks', 0)
    if cur < cap:
        u.extra['archer_circuit_stacks'] = cur + 1
        state.log.append(f'  回路连接: 战技伤害+{per_stack * 100:.0f}%'
                         f'({u.extra["archer_circuit_stacks"]}/{cap}层)')
    # E1: 单回合3次战技→+2SP
    if u.eidolon_rank >= 1 and u.extra.get('archer_turn_skill_count') == 3:
        _gain_skill_points(state, 2)
        state.log.append('  E1·未曾触及的理想: 单回合3次战技→+2战技点')
    casts = u.extra['archer_circuit_casts']
    if casts >= 5 or state.skill_points < 2:
        _archer_exit_circuit(u, state)
    else:
        # 本回合不结束: 连入 X 轴（不与既有排队重复）
        queued = any(x is u for x, k in state.extra.get('extra_turns', []))
        if not queued:
            state.extra.setdefault('extra_turns', []).append((u, 'extra'))
            state.log.append(f'  回路连接: 战技#{casts}, 本回合继续（X轴）')


def _archer_ult_cast(state, u):
    """无限剑制: 单体 1000%(E4+150%); +2 充能; E2 量子易伤+弱点。"""
    state.log.append('  无限剑制: 单体1000%量子')
    stats = _build_effective_stats(u, state)
    if u.eidolon_rank >= 4:
        stats = copy.deepcopy(stats)
        stats.DMG_BONUS_ALL += 1.50
    tsc = _alive_enemies(state)[0] if _alive_enemies(state) else None
    total = _archer_deal(state, u, stats, tsc, 1000.0, 30.0, 'ultimate')
    u.total_damage_dealt += total
    _archer_charge_gain(u, 2, state, note='(终结技)')
    if u.eidolon_rank >= 2 and tsc is not None and getattr(tsc, 'HP', 0) > 0:
        # 通用弱点植入机制（v5.3 流萤同款）: 抗性-20% + 快照, 到期自动恢复
        from engine.models.enemy import EnemyStatus
        old_res = tsc.get_res(ELEMENT)
        tsc.element_res[ELEMENT] = min(old_res, -0.20)
        tsc.add_status(EnemyStatus(id='archer_e2_weakness', name='量子弱点',
                                   category='debuff', source=CHAR_ID,
                                   remaining_turns=2,
                                   attributes={'weakness_element': ELEMENT,
                                               'weakness_old_res': old_res}))
        state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                                element=ELEMENT, target=tsc)
        state.log.append('  E2·未能实现的幸福: 量子抗性-20% + 量子弱点植入(2回合)')


def _archer_mind_eye(state, attacker, skill_key):
    """心眼（真）: 队友攻击类技能后 → 消耗1充能追击主目标 200% + 回1SP。"""
    archer = _archer_find(state)
    if archer is None or archer is attacker:
        return
    if skill_key not in ('basic_attack', 'basic_attack_enhanced', 'skill', 'ultimate'):
        return
    charge = archer.extra.get('archer_charge', 0)
    if charge < 1:
        return
    archer.extra['archer_charge'] = charge - 1
    targets = state.extra.get('last_attack_targets') or _alive_enemies(state)[:1]
    t = next((e for e in targets if getattr(e, 'HP', 0) > 0), None)
    if t is None:
        alive = _alive_enemies(state)
        if not alive:
            return
        t = random.choice(alive)
    stats = _build_effective_stats(archer, state)
    lf = _skill_level_factor(archer, 'talent')
    d = calculate_damage(stats, _enemy_for_damage(t, 'skill'), stats.ATK, 200.0 * lf,
                         'direct', ELEMENT, 80, stats.CRIT_RATE >= 0.5,
                         skill_type='skill', attack_type='follow_up',
                         crit_mode='expected')
    _commit_enemy_damage(state, archer, t, d.final_damage)
    archer.total_damage_dealt += d.final_damage
    if t.toughness > 0:
        _flat_toughness_with_break(state, archer, t, 10.0, ELEMENT, 'skill', stats)
    _gain_skill_points(state, 1)
    state.log.append(f'  心眼（真）: 追击200%量子 +1SP（充能{archer.extra["archer_charge"]:.0f}）')


def _hook_skill(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _archer_skill_cast(state, u)
        return True


def _hook_ult(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _archer_ult_cast(state, u)
        return True


def _archer_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE: 队友攻击后触发心眼; 回合技术位清理（单回合战技计数）。"""
    _archer_mind_eye(state, u, skill_key)


def _archer_turn_tick(u, state):
    """E6: 回合开始+1SP; 单回合战技计数复位。"""
    if u.char.id != CHAR_ID:
        return
    u.extra.pop('archer_turn_skill_count', None)
    if u.eidolon_rank >= 6:
        _gain_skill_points(state, 1)
        state.log.append('  E6·无尽徘徊的巡礼: 回合开始+1战技点')


def _archer_sp_change(u, state, delta=0, **kw):
    """OBSERVER sp_change: 行迹3 守护者——SP获得后 ≥4 → 暴伤+120% 1回合。"""
    archer = _archer_find(state)
    if archer is None or delta <= 0 or state.skill_points < 4:
        return
    if not any(t.hook_name == 'archer_trace3_guardian'
               for t in (archer.char.traces or [])):
        return
    archer.buffs = [b for b in archer.buffs
                    if getattr(b, 'param_id', '') != 'archer_guardian_cd']
    archer.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'CRIT_DMG': 120.0},
                                  remaining_turns=1, param_id='archer_guardian_cd',
                                  source_name='守护者'))


def _init_archer(state):
    archer = _archer_find(state)
    if archer is None:
        return
    if any(t.hook_name == 'archer_trace1_projection' for t in (archer.char.traces or [])):
        state.max_sp += 2
        archer.extra['archer_max_sp_bonus'] = 2
    if any(t.hook_name == 'archer_trace2_ally' for t in (archer.char.traces or [])):
        _archer_charge_gain(archer, 1, state, note='(行迹2·开战)')
    if archer.extra.pop('archer_tech_pending', None):
        stats = _build_effective_stats(archer, state)
        total = 0.0
        for t in _alive_enemies(state):
            total += _archer_deal(state, archer, stats, t, 200.0, 20.0, 'skill')
        archer.total_damage_dealt += total
        _archer_charge_gain(archer, 1, state, note='(秘技·千里眼)')
        state.log.append('[秘技] 千里眼: 全敌200%量子 +1充能')


def _tech_archer(state, u, is_opener):
    """秘技（进战）: 主动攻击开怪时生效——挂 pending 由 INIT 兑现。"""
    if u.char.id != CHAR_ID:
        return
    u.extra['archer_tech_pending'] = True
    state.log.append('[秘技] 千里眼: 进战全敌200%量子 +1充能')


def archer_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """AI: 回路连接中→战技(SP够); 能量满→终结技; SP≥2→战技; 否则普攻。"""
    if u.extra.get('archer_circuit') and state.skill_points >= 2:
        _use_skill(u, state, 'skill')
    elif u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
    elif state.skill_points >= 2:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


AI = archer_ai
TECHNIQUE = _tech_archer
INIT = _init_archer
SKILL_HOOKS = [_hook_skill, _hook_ult]
PHASE_HOOKS = {}
TURN_TICKS = {'pre': _archer_turn_tick}
OBSERVER_HOOKS = {'sp_change': _archer_sp_change}
SETTLE_HANDLERS = {'settle_self': _archer_settle_self}
