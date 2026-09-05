"""远坂凛（yuanbanlin）——Fate 联动·智识·量子（v7.21.0 录入, txt 同名）

核心机制:
- 宝石能量（u.extra['rin_gems']）: 开战 20（项目主裁决）; SP 每消耗/恢复 1 点 +1（sp_change
  观察者, 归因造成变动者）; 终结技
  +12（行迹3）/+24（E6）; 秘技 +10; 初值 txt 未给出暂按 0（待复核）
- 天赋宝石魔术: 任意我方消耗/恢复 SP → 该单位暴伤+70%（Lv10 口径）2回合（恢复事件
  引擎无施法者上下文时记凛自身——口径注记）; E4 对凛自身可叠 2 层
- 战技强化: 宝石≥15 或 SP≥7（或 E1 持有影子宝石）→ AI 切键 skill_experiment;
  第二魔法实验: 全体 90% + 每轮耗 3 宝石随机单体 90% 弹射（≤33 轮）; SP>2 时耗至
  2、每点 +2 宝石; E1 影子模式: 弹射耗影子宝石且耗尽, 不耗宝石不转化 SP
- 终结技: 主 600% + 其他 200%; +1SP; 敌全体易伤 20% 3回合; E6 +24 宝石 + 额外回合
- 自在远坂流（Archer 连携）: Archer 施放伪·螺旋剑后, SP≤3 或该回路已 5 次未触发 →
  凛 300% + Archer 300% 全体量子合击 + 4SP; 每凛回合结束复位 1 次
- 行迹1: SP 上限+2; 开战 ATK+150% + 量子抗穿15%（Archer 在队同享）; 行迹2: 开战与
  强化战技后 SPD+20% 3回合; E2: 自身战技伤害+30% + 全队战技伤害×130% 光环;
  E6: 全抗穿+20%
- 秘技转换充能: 下次开战 +10 宝石
"""
import copy
import random

from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _flat_toughness_with_break,
    _gain_skill_points, _skill_level_factor, _use_skill,
)
from engine.models.enemy import EnemyStatus

CHAR_ID = "yuanbanlin"
ELEMENT = "量子"


def _rin_find(state):
    return next((x for x in state.units if x.char.id == CHAR_ID and x.is_alive), None)


def _rin_gems_gain(u, n, state=None, note=''):
    if n <= 0:
        return
    u.extra['rin_gems'] = u.extra.get('rin_gems', 0) + n
    if state is not None:
        state.log.append(f'  宝石能量+{n}{note}({u.extra["rin_gems"]})')


def _alive_enemies(state):
    return [e for e in state.enemies if getattr(e, 'HP', 0.0) > 0]


def _rin_deal(state, u, stats, targets, scale, toughness, skill_type,
              attack_type='active'):
    total = 0.0
    for t in targets:
        if getattr(t, 'HP', 0.0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(t, skill_type), stats.ATK, scale,
                             'direct', ELEMENT, 80, stats.CRIT_RATE >= 0.5,
                             skill_type=skill_type, attack_type=attack_type,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, u, t, toughness, ELEMENT, skill_type, stats)
    return total


def _rin_talent_cd_buff(state, target, rin):
    """天赋: 消耗/恢复SP的单位 → 暴伤+70%(Lv10) 2回合; E4 凛自身可叠2层。"""
    if target is None or not target.is_alive:
        return
    scale = 70.0 * _skill_level_factor(rin, 'talent')
    stacking = (target is rin and rin.eidolon_rank >= 4)
    cur = next((b for b in target.buffs
                if getattr(b, 'param_id', '') == 'rin_talent_cd'), None)
    if stacking and cur is not None and cur.attributes.get('CRIT_DMG', 0.0) < scale * 2:
        cur.attributes['CRIT_DMG'] = cur.attributes.get('CRIT_DMG', 0.0) + scale
        cur.remaining_turns = 2
        return
    if cur is not None:  # 非叠加目标: 同名刷新
        cur.attributes['CRIT_DMG'] = scale
        cur.remaining_turns = 2
        return
    target.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'CRIT_DMG': scale},
                                  remaining_turns=2, param_id='rin_talent_cd',
                                  source_name='宝石魔术'))


def _rin_sp_change(u, state, delta=0, **kw):
    """OBSERVER sp_change: 天赋双触发——暴伤 buff 加在造成SP变动的角色（项目主
    2026-09-06 裁决: 消耗/恢复皆然, 实时刷新≈常驻）+ 凛宝石获取。"""
    rin = _rin_find(state)
    if rin is None:
        return
    n = abs(int(delta))
    if n <= 0:
        return
    _rin_talent_cd_buff(state, u if u is not None else rin, rin)
    _rin_gems_gain(rin, n, state, note='(SP变动)')


def _rin_can_enhance(u, state):
    if u.extra.get('rin_shadow_gems', 0) > 0 and u.eidolon_rank >= 1:
        return True
    return u.extra.get('rin_gems', 0) >= 15 or state.skill_points >= 7


def _rin_skill_experiment_cast(state, u):
    """第二魔法实验: 全体90% + 资源驱动弹射(≤33轮) + SP耗至2转化。"""
    lf = _skill_level_factor(u, 'skill')
    stats = _build_effective_stats(u, state)
    if u.eidolon_rank >= 2:
        stats = copy.deepcopy(stats)
        stats.DMG_BONUS_BY_SKILL_TYPE['skill'] = \
            stats.DMG_BONUS_BY_SKILL_TYPE.get('skill', 0.0) + 0.30
    # SP>2 → 耗至2, 每点+2宝石（E1 影子模式不转化）
    shadow_mode = (u.eidolon_rank >= 1 and u.extra.get('rin_shadow_gems', 0) > 0)
    if not shadow_mode and state.skill_points > 2:
        consumed = state.skill_points - 2
        state.skill_points = 2
        _rin_gems_gain(u, consumed * 2, state, note='(SP转化)')
    alive = _alive_enemies(state)
    total = _rin_deal(state, u, stats, alive, 90.0 * lf, 20.0, 'skill')
    # 弹射: 每轮耗3宝石, ≤33轮
    pool = 'rin_shadow_gems' if shadow_mode else 'rin_gems'
    rounds = 0
    while rounds < 33 and u.extra.get(pool, 0) >= 3 and _alive_enemies(state):
        u.extra[pool] = u.extra.get(pool, 0) - 3
        rounds += 1
        t = random.choice(_alive_enemies(state))
        total += _rin_deal(state, u, stats, [t], 90.0 * lf, 2.0, 'skill')
    if shadow_mode:
        consumed_shadow = u.extra.pop('rin_shadow_gems', 0)
        state.log.append(f'  第二魔法实验(影子模式): 耗尽影子宝石, 弹射{rounds}轮')
    else:
        spent = u.extra.get('rin_gems_spent', 0) + rounds * 3
        u.extra['rin_gems_spent'] = spent
        # E1: 单次强化战技消耗≥30 → 获得等量影子宝石
        if u.eidolon_rank >= 1 and rounds * 3 >= 30:
            u.extra['rin_shadow_gems'] = u.extra.get('rin_shadow_gems', 0) + rounds * 3
            state.log.append(f'  E1·宝石翁的学徒: 获得{rounds * 3}影子宝石')
    u.total_damage_dealt += total
    # 行迹2: 强化战技后 SPD+20% 3回合
    if any(t.hook_name == 'yuanbanlin_trace2_lady' for t in (u.char.traces or [])):
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'rin_t2_spd']
        u.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'SPD_PERCENT': 20.0},
                                 remaining_turns=3, param_id='rin_t2_spd',
                                 source_name='淑女风范'))
    state.log.append(f'  第二魔法实验: 全体+弹射{rounds}轮')


def _rin_ult_cast(state, u):
    """山脉震撼·明星之薪: 主600%+其他200%; +1SP; 全体易伤20% 3回合; 行迹3/E6。"""
    state.log.append('  山脉震撼·明星之薪: 主600% + 其他200%')
    stats = _build_effective_stats(u, state)
    lf = _skill_level_factor(u, 'ultimate')
    alive = _alive_enemies(state)
    tsc = alive[0] if alive else None
    others = [e for e in alive if e is not tsc]
    total = _rin_deal(state, u, stats, [tsc] if tsc else [], 600.0 * lf, 30.0, 'ultimate')
    total += _rin_deal(state, u, stats, others, 200.0 * lf, 20.0, 'ultimate')
    u.total_damage_dealt += total
    _gain_skill_points(state, 1)
    for t in alive:
        if getattr(t, 'HP', 0) <= 0:
            continue
        t.add_status(EnemyStatus(id='rin_ult_vuln', name='受到伤害提高',
                                 category='debuff', source=CHAR_ID,
                                 remaining_turns=3,
                                 attributes={'vulnerability': 0.20 * lf}))
    if any(t.hook_name == 'yuanbanlin_trace3_funding' for t in (u.char.traces or [])):
        _rin_gems_gain(u, 12, state, note='(行迹3·终结技)')
    if u.eidolon_rank >= 6:
        _rin_gems_gain(u, 24, state, note='(E6·终结技)')
        queued = any(x is u for x, k in state.extra.get('extra_turns', []))
        if not queued:
            state.extra.setdefault('extra_turns', []).append((u, 'extra'))
            state.log.append('  E6·这次没掉链子: 获得1个额外回合')


def _rin_joint_attack(state):
    """自在远坂流: 凛300% + Archer300% 全体量子合击 + 4SP（每凛回合1次）。"""
    rin = _rin_find(state)
    archer = next((x for x in state.units if x.char.id == 'archer' and x.is_alive), None)
    if rin is None or archer is None or rin.extra.get('rin_joint_used'):
        return
    rin.extra['rin_joint_used'] = True
    alive = _alive_enemies(state)
    rin_stats = _build_effective_stats(rin, state)
    rin.total_damage_dealt += _rin_deal(state, rin, rin_stats, alive, 300.0, 20.0,
                                        'skill', attack_type='follow_up')
    archer_stats = _build_effective_stats(archer, state)
    for t in alive:
        if getattr(t, 'HP', 0.0) <= 0:
            continue
        d = calculate_damage(archer_stats, _enemy_for_damage(t, 'skill'),
                             archer_stats.ATK, 300.0, 'direct', '量子', 80,
                             archer_stats.CRIT_RATE >= 0.5, skill_type='skill',
                             attack_type='follow_up', crit_mode='expected')
        _commit_enemy_damage(state, archer, t, d.final_damage)
        archer.total_damage_dealt += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, archer, t, 20.0, '量子', 'skill',
                                       archer_stats)
    _gain_skill_points(state, 4)
    state.log.append('  自在远坂流: 凛+Archer 全体合击300%×2, +4战技点')


# ---- 钩子装配 ----

def _hook_experiment(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill_experiment":
        _rin_skill_experiment_cast(state, u)
        return True


def _hook_ult(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _rin_ult_cast(state, u)
        return True


def _rin_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE: Archer 施放伪·螺旋剑后判定自在远坂流。"""
    if u.char.id != 'archer' or skill_key != 'skill':
        return
    rin = _rin_find(state)
    if rin is None:
        return
    circuit_casts = u.extra.get('archer_circuit_casts', 0)
    if state.skill_points <= 3 or circuit_casts >= 5:
        _rin_joint_attack(state)


def _rin_turn_tick(u, state):
    """凛回合结束口径（tick 置位处）: 连携触发次数复位。"""
    if u.char.id != CHAR_ID:
        return
    u.extra.pop('rin_joint_used', None)


def _init_yuanbanlin(state):
    rin = _rin_find(state)
    if rin is None:
        return
    _rin_gems_gain(rin, 20, state, note='(开战·天赋)')  # 项目主 2026-09-06 裁决: 进战20
    if any(t.hook_name == 'yuanbanlin_trace1_elegant' for t in (rin.char.traces or [])):
        state.max_sp += 2
        rin.extra['rin_max_sp_bonus'] = 2
        for eu in (rin, next((x for x in state.units
                              if x.char.id == 'archer' and x.is_alive), None)):
            if eu is None:
                continue
            eu.base_stats.ATK += eu.base_stats._base_ATK * 1.50
            eu.base_stats.RES_PEN[ELEMENT] = eu.base_stats.RES_PEN.get(ELEMENT, 0.0) + 0.15
        state.log.append('  秉持优雅: SP上限+2, ATK+150%+量子抗穿15%(凛/Archer)')
    if any(t.hook_name == 'yuanbanlin_trace2_lady' for t in (rin.char.traces or [])):
        rin.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'SPD_PERCENT': 20.0},
                                   remaining_turns=3, param_id='rin_t2_spd',
                                   source_name='淑女风范'))
    if rin.eidolon_rank >= 2:
        for eu in state.units:  # 全队(含自身经由 cast 内联, 此处队友)战技伤害+30%
            if eu.is_alive and eu is not rin:
                eu.base_stats.DMG_BONUS_BY_SKILL_TYPE['skill'] = \
                    eu.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('skill', 0.0) + 0.30
        state.log.append('  E2·位面的旅行者: 队友战技伤害+30% 光环')
    if rin.eidolon_rank >= 6:
        rin.base_stats.RES_PEN_ALL += 0.20
    if rin.extra.pop('rin_tech_pending', None):
        _rin_gems_gain(rin, 10, state, note='(秘技·转换充能)')


def _tech_yuanbanlin(state, u, is_opener):
    if u.char.id != CHAR_ID:
        return
    u.extra['rin_tech_pending'] = True
    state.log.append('[秘技] 转换充能: 下次开战+10宝石能量')


def yuanbanlin_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """AI: 能量满→终结技; 强化条件→第二魔法实验; 有SP→战技; 否则普攻。"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
    elif _rin_can_enhance(u, state):
        _use_skill(u, state, 'skill_experiment')
    elif state.skill_points > 0:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


AI = yuanbanlin_ai
TECHNIQUE = _tech_yuanbanlin
INIT = _init_yuanbanlin
SKILL_HOOKS = [_hook_experiment, _hook_ult]
PHASE_HOOKS = {}
TURN_TICKS = {'pre': _rin_turn_tick}
OBSERVER_HOOKS = {'sp_change': _rin_sp_change}
SETTLE_HANDLERS = {'settle_self': _rin_settle_self}
