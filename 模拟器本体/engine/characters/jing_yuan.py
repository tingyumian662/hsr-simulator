"""jing_yuan（景元, v9.0.0 批3: 空壳→完整）。

机制要点:
- 【神君】走行动条标记(timeline_marker, 浮元先例): 60速入场, 3段初始段数;
  段数上限10(奇缘+20=30), 每段速度+10(高于初始3段的部分), 行动后重置。
  行动时逐段: 随机单体66%ATK雷 + 相邻25%(E1:50%)×主目标倍率;
  E4每段+2能量; E6每段主目标易伤12%≤3层(至本次攻击结束);
  破阵: 段数≥6→该次行动暴伤+25%; E2行动后景元增伤20%×2回合。
- 段数获取: 战技+2/终结技+3(奇缘强化期间额外+6/+12); 奇缘段数满→立即行动。
- 奇缘星魂1(独立开关, 裁定①): 战斗开始及神君每行动1次后→强化(景元伤害
  +240%×4回合 + 强化期间战技/终结技额外段数)。
"""
import copy
import random

from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage
from engine.core.combat_engine import _ensure_marker_system, _use_skill
from engine.core.damage import calculate_damage

CHAR_ID = "jing_yuan"

LL_BASE_SPD = 60.0
LL_BASE_SEG = 3


def _seg_cap(u) -> int:
    """神君段数上限: 10 + 奇缘1 +20。"""
    return 30 if getattr(u, 'fate_bond_rank', 0) >= 1 else 10


def _find_jy(state):
    return next((x for x in state.units
                 if x.char.id == CHAR_ID and x.is_alive), None)


def _ll_marker(state):
    jy = _find_jy(state)
    m = getattr(jy, 'marker', None) if jy else None
    return m if (m and m.is_alive and m.marker_id == 'jingyuan_shenjun') else None


def _empowered(u) -> bool:
    """奇缘强化期间(神君强化 buff 在身)。"""
    return any(getattr(b, 'param_id', '') == 'jy_empower' for b in u.buffs)


def _reschedule_ll(state, marker, seg):
    """段数变化→按剩余比例换算新速度的到达时间; 段数满(奇缘)→立即行动。"""
    old_spd = marker.data.get('base_SPD', LL_BASE_SPD)
    new_spd = LL_BASE_SPD + 10.0 * max(0, seg - LL_BASE_SEG)
    marker.data['base_SPD'] = new_spd
    if new_spd != old_spd:
        remain = max(0.0, marker.extra.get('next_av', state.current_av) - state.current_av)
        frac = remain / (10000.0 / max(old_spd, 1.0)) if old_spd > 0 else 1.0
        marker.extra['next_av'] = state.current_av + frac * (10000.0 / max(new_spd, 1.0))


def _add_segments(state, u, n):
    marker = _ll_marker(state)
    if marker is None:
        return
    seg = min(_seg_cap(u), marker.extra.get('segments', LL_BASE_SEG) + n)
    capped = seg >= _seg_cap(u)
    marker.extra['segments'] = seg
    _reschedule_ll(state, marker, seg)
    state.log.append(f'  【神君】段数: {seg}/{_seg_cap(u)}')
    if capped and getattr(u, 'fate_bond_rank', 0) >= 1:
        marker.extra['next_av'] = state.current_av  # 奇缘: 段数达上限时立即行动
        state.log.append('  【神君】段数已满: 立即行动')


# ---- AI ----

def _jy_ai(u, state, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
    elif state.skill_points > 0:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


# ---- 神君行动 ----

def _shenjun_action(state, marker):
    jy = _find_jy(state)
    sys = state.extra.get('_marker_sys')
    if jy is None:
        if sys:
            sys.despawn(state, marker)
        return
    seg = marker.extra.get('segments', LL_BASE_SEG)
    stats = _build_effective_stats(jy, state)
    # 行迹1·破阵: 段数≥6→该次行动暴伤+25%
    trace1 = any(getattr(t, 'hook_name', '') == 'jy_trace1' for t in (jy.char.traces or []))
    if trace1 and seg >= 6:
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 0.25  # 破阵暴伤+25%(面板小数单位)
        state.log.append('  行迹1·破阵: 神君暴伤+25%')
    adj_ratio = 0.50 if jy.eidolon_rank >= 1 else 0.25  # E1: 相邻25%→50%
    e6 = jy.eidolon_rank >= 6
    vuln = {}  # E6: 主目标易伤层(12%×层≤3, 至本次攻击结束)
    total = 0.0
    for _i in range(seg):
        alive = state.alive_enemies()
        if not alive:
            break
        t = random.choice(alive)
        scale = 66.0
        if e6:
            scale *= 1.0 + 0.12 * min(3, vuln.get(t.id, 0))
            vuln[t.id] = vuln.get(t.id, 0) + 1
        d = calculate_damage(stats, _enemy_for_damage(t, 'skill'), stats.ATK, scale,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             attack_type='follow_up', crit_mode='expected')
        _commit_enemy_damage(state, jy, t, d.final_damage)
        total += d.final_damage
        # 相邻目标: 主目标伤害的 25%(E1: 50%)
        idx = state.enemies.index(t) if t in state.enemies else -1
        for adj in (idx - 1, idx + 1):
            if 0 <= adj < len(state.enemies) and state.enemies[adj].HP > 0:
                a = state.enemies[adj]
                d2 = calculate_damage(stats, _enemy_for_damage(a, 'skill'), stats.ATK,
                                      scale * adj_ratio, 'direct', '雷', 80,
                                      stats.CRIT_RATE >= 0.5,
                                      true_dmg_ratio=state.realm_true_dmg,
                                      attack_type='follow_up', crit_mode='expected')
                _commit_enemy_damage(state, jy, a, d2.final_damage)
                total += d2.final_damage
        if jy.eidolon_rank >= 4:  # E4: 每段攻击后+2能量
            jy.current_energy = min(jy.char.max_energy, jy.current_energy + 2.0)
    jy.total_damage_dealt += total
    jy.damage_log.append(('神君·斩勘神形', total, 'follow_up'))
    state.log.append(f'  【神君】行动: {seg}段 {total:.0f}')
    state.hooks.trigger_all("on_attack_action", u=jy, state=state, dealt=total > 0)
    # 行动后重置(速度/段数回初始; handle_action 已用旧速度排程→显式按60速重排)
    marker.extra['segments'] = LL_BASE_SEG
    marker.data['base_SPD'] = LL_BASE_SPD
    marker.extra['next_av'] = state.current_av + 10000.0 / LL_BASE_SPD
    # E2: 行动后景元增伤20%×2回合
    if jy.eidolon_rank >= 2:
        jy.buffs = [b for b in jy.buffs if getattr(b, 'param_id', '') != 'jy_e2_dmg']
        jy.buffs.append(TimedBuff(source_id=CHAR_ID,
                                  attributes={'DMG_BONUS_ALL': 20.0},
                                  remaining_turns=2, param_id='jy_e2_dmg',
                                  source_name='E2·戎戈动地'))
        state.log.append('  E2: 景元伤害+20%×2回合')
    # 奇缘: 神君每行动1次后→强化(伤害+240%×4回合 + 强化期间额外段数)
    _apply_empower(state, jy)


def _apply_empower(state, jy):
    if getattr(jy, 'fate_bond_rank', 0) < 1:
        return
    jy.buffs = [b for b in jy.buffs if getattr(b, 'param_id', '') != 'jy_empower']
    jy.buffs.append(TimedBuff(source_id=CHAR_ID,
                              attributes={'DMG_BONUS_ALL': 240.0},
                              remaining_turns=4, param_id='jy_empower',
                              source_name='奇缘·万钧入盏'))
    state.log.append('  奇缘·万钧入盏: 神君强化, 景元伤害+240%×4回合')


MARKERS = {"jingyuan_shenjun": _shenjun_action}


# ---- 结算 ----

def _jy_settle_self(u, state, skill, skill_key, total_dmg):
    """settle_self: 战技/终结技→神君段数(奇缘强化期间额外+6/+12); 行迹3遣将。"""
    if u.char.id != CHAR_ID:
        return
    extra = 0
    if skill_key == 'skill':
        extra = 6 if _empowered(u) else 0
    elif skill_key == 'ultimate':
        extra = 12 if _empowered(u) else 0
    if skill_key == 'skill':
        _add_segments(state, u, 2 + extra)
        # 行迹3·遣将: 战技后暴击率+10%×2回合
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'jy_qianjiang']
        u.buffs.append(TimedBuff(source_id=CHAR_ID,
                                 attributes={'CRIT_RATE': 10.0},
                                 remaining_turns=2, param_id='jy_qianjiang',
                                 source_name='行迹·遣将'))
    elif skill_key == 'ultimate':
        _add_segments(state, u, 3 + extra)


SETTLE_HANDLERS = {'settle_self': _jy_settle_self}


# ---- 秘技 / 入场 ----

def _tech_jy(state, u, is_opener):
    """秘技·摄召威灵: 进战神君第1回合段数+3（INIT 已先召唤, 此处直接加段）。"""
    marker = _ll_marker(state)
    if marker is not None:
        _add_segments(state, u, 3)
        state.log.append('[秘技] 摄召威灵: 神君第1回合段数+3')
    else:
        u.extra['jy_tech_seg'] = 3  # 兜底: 标记未出场时由下次调度消费
        state.log.append('[秘技] 摄召威灵: 神君段数+3(待入场)')


def _init_battle(state):
    u = _find_jy(state)
    if u is None:
        return
    u.current_energy = min(u.char.max_energy, u.current_energy + 15.0)  # 行迹2·绸缪
    state.log.append('  行迹2·绸缪: 战斗开始+15能量')
    sys = _ensure_marker_system(state)  # INIT 早于技能效果分支, 惰性创建入口
    marker = sys.spawn(state, u, 'jingyuan_shenjun')
    if marker is not None:
        seg = LL_BASE_SEG + u.extra.pop('jy_tech_seg', 0)
        marker.extra['segments'] = seg
        _reschedule_ll(state, marker, seg)
    _apply_empower(state, u)  # 奇缘: 战斗开始即强化


TECHNIQUE = _tech_jy
INIT = _init_battle
AI = _jy_ai
