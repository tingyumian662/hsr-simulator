"""dan_heng_imbibitor_lunae（丹恒•饮月, v9.0.0 批2: 空壳→完整）。

机制要点:
- 强化普攻三级链(泽芝→瞬华→天矢阴→盘拏耀跃): AI 按 SP+逆鳞 可付档直选
  最高档强化普攻键(skill_hua/tian/pan); 强化普攻视为普攻攻击类型且不恢复SP;
  战技【龙力自在】为站姿文档条目, 不单独施放(施放本技能不视为使用战技)。
- 逆鳞: 上限3(奇缘+1=4), 抵扣强化普攻战技点消耗(优先逆鳞); 消耗逆鳞视为
  消耗战技点——计入 state.extra['lc_sp_spent'] 队内SP消耗口径(饮月奇缘5SP
  触发/花花世界类光锥); 花火/银狼SP监听暂不联动(v9.0.0 批末裁定清单项)。
- 亢心/叱咤逐段爬坡: 折算倍率的多段伤害在 S4 钩子按等差数列预计算平均爬坡
  系数(第k段享 k-1 层等), damage_mod 生效; 行动后满层入 TimedBuff
  (至自身回合结束, E4 叱咤延长一回合)——本行动内爬坡与后续行动满层不双算。
- 奇缘星魂1(独立开关, 裁定①): 我方每消耗5个战技点→追击(6段×500%全体
  虚数, 视为普攻伤害; 自身伤害+240%×3回合; +1逆鳞)。
"""
import copy

from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _enemy_weakness_elements,
    _use_skill)
from engine.core.damage import calculate_damage

CHAR_ID = "dan_heng_imbibitor_lunae"

# 强化普攻键 → 能量恢复(txt); 逆鳞可抵扣的SP档
ENHANCED = {'skill_hua': 30.0, 'skill_tian': 35.0, 'skill_pan': 40.0}


def _scale_cap(u) -> int:
    """逆鳞上限: 3 + 奇缘星魂1 +1。"""
    return 4 if getattr(u, 'fate_bond_rank', 0) >= 1 else 3


def _gain_scales(state, u, n):
    cap = _scale_cap(u)
    cur = u.extra.get('dhil_scales', 0)
    new = min(cap, cur + n)
    u.extra['dhil_scales'] = new
    if new != cur:
        state.log.append(f'  【逆鳞】: {new}/{cap}')


def _skill_hits(u, skill_key) -> int:
    """技能段数 = 主倍率 _hits（相邻目标由同段命中, 不是额外段; adjacent _hits 只是折算存档）。"""
    sk = u.char.get_skill(skill_key)
    if sk is None or not sk.multipliers:
        return 0
    return max(int(getattr(m, 'hits', 1) or 1) for m in sk.multipliers)


def _apply_stacked_buff(u, param_id, name, attrs, turns):
    """同源刷新叠层 buff（雷心先例）。"""
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != param_id]
    u.buffs.append(TimedBuff(source_id=CHAR_ID, attributes=attrs,
                             remaining_turns=turns, param_id=param_id,
                             source_name=name))


# ---- AI ----

def _dhil_ai(u, state, **__):
    """饮月AI: 能量满→终结技; 按SP+逆鳞可付档直选最高档强化普攻; 否则泽芝。"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
        return
    budget = state.skill_points + u.extra.get('dhil_scales', 0)
    for key, cost in (('skill_pan', 3), ('skill_tian', 2), ('skill_hua', 1)):
        if budget >= cost:
            _use_skill(u, state, key)
            return
    _use_skill(u, state, 'basic_attack')


# ---- 相位 ----

def _dhil_sp_cost(u, state, sp_cost, skill_key):
    """sp_cost_override: 逆鳞优先抵扣强化普攻SP; 抵扣量计入队内SP消耗口径。"""
    if skill_key not in ENHANCED or sp_cost is None or sp_cost <= 0:
        return None
    scales = u.extra.get('dhil_scales', 0)
    if scales <= 0:
        return None
    used = min(scales, int(sp_cost))
    u.extra['dhil_scales'] = scales - used
    # 消耗【逆鳞】视为消耗战技点(txt): 计入 lc_sp_spent 队内口径
    state.extra['lc_sp_spent'] = state.extra.get('lc_sp_spent', 0) + used
    state.log.append(f'  【逆鳞】抵扣SP: -{used} (剩{u.extra["dhil_scales"]})')
    return sp_cost - used


def _dhil_attack_type(u, state, st, skill_key):
    """attack_type_override: 强化普攻视为普攻(吃普攻类增伤/触发器)。"""
    if skill_key in ENHANCED:
        return 'basic'
    return None


def _dhil_energy_gain(u, state, skill_key):
    """energy_gain_override: 强化普攻回能 30/35/40(txt)。"""
    return ENHANCED.get(skill_key)


def _dhil_target_stats(u, state, t, t_stats):
    """target_stats_mod: 行迹3·起蛰——对虚数弱点目标暴伤+24%。"""
    if t is None or '虚数' not in _enemy_weakness_elements(t):
        return None
    ns = copy.deepcopy(t_stats)
    ns.CRIT_DMG += 0.24  # 起蛰暴伤+24%(面板小数单位)
    return ns


def _dhil_damage_mod(u, state, final, st):
    """damage_mod: 本行动亢心/叱咤等差爬坡系数(S4 预计算)。"""
    if u.char.id != CHAR_ID or st is None:
        return None
    ramp = u.extra.get('dhil_ramp')
    if ramp and ramp > 1.0:
        return final * ramp
    return None


# ---- 技能钩子(S4, 伤害循环前) ----

def _dhil_skill_hook(u, state, skill_key):
    if u.char.id != CHAR_ID:
        return
    hits = _skill_hits(u, skill_key)
    if hits <= 0:
        return
    # E6·见谯: 盘拏耀跃消耗全部抗穿层(TimedBuff RES_PEN_ALL, 本行动生效)
    if skill_key == 'skill_pan':
        pen = u.extra.pop('dhil_e6', 0)
        if u.eidolon_rank >= 6 and pen > 0:
            _apply_stacked_buff(u, 'dhil_e6_pen', '见谯',
                                {'RES_PEN_ALL': 20.0 * pen}, 1)
            state.log.append(f'  E6·见谯: 盘拏耀跃虚数抗穿+{20 * pen}%×本次')
    # 亢心: 每段攻击后+1层(E1每段2层), 满级10%/层; 第k段享 min(cap,(k-1)*xl) 层
    xl = 2 if u.eidolon_rank >= 1 else 1
    layers_cap = 10 if u.eidolon_rank >= 1 else 6
    kang_pct = sum(min(layers_cap, (k - 1) * xl) * 10.0
                   for k in range(1, hits + 1)) / hits
    # 叱咤: 天矢阴/盘拏耀跃第4段起每段攻击前+1层(≤4), 每层暴伤12%
    chi_pct = 0.0
    if skill_key in ('skill_tian', 'skill_pan'):
        chi_pct = sum(max(0, k - 3) * 12.0 for k in range(1, hits + 1)) / hits
    u.extra['dhil_ramp'] = 1.0 + kang_pct / 100.0 + chi_pct / 100.0


def _dhil_ult_hook(u, state, skill_key):
    if u.char.id != CHAR_ID or skill_key != 'ultimate':
        return
    _gain_scales(state, u, 2)
    if u.eidolon_rank >= 2:
        _gain_scales(state, u, 1)
        spd = max(u.base_stats.SPD, 1.0)
        u._pending_action_advance = (10000.0 / spd) * 1.0
        state.log.append('  E2·九斿: 终结技后行动提前100%')


SKILL_HOOKS = [_dhil_skill_hook, _dhil_ult_hook]


# ---- 结算管线 ----

def _dhil_settle_self(u, state, skill, skill_key, total_dmg):
    """settle_self: 行动后亢心/叱咤满层入 buff(至自身回合结束); 清爬坡系数。"""
    if u.char.id != CHAR_ID:
        return
    u.extra.pop('dhil_ramp', None)
    hits = _skill_hits(u, skill_key)
    if hits <= 0 or total_dmg <= 0:
        return
    xl = 2 if u.eidolon_rank >= 1 else 1
    layers = min(10 if u.eidolon_rank >= 1 else 6, hits * xl)
    if layers > 0:
        _apply_stacked_buff(u, 'dhil_kangxin', '亢心',
                            {'DMG_BONUS_ALL': 10.0 * layers}, 1)
        state.log.append(f'  【亢心】{layers}层: 伤害+{10.0 * layers:.0f}%')
    if skill_key in ('skill_tian', 'skill_pan'):
        cl = min(4, hits - 3)
        if cl > 0:
            turns = 2 if u.eidolon_rank >= 4 else 1  # E4·嘲风: 叱咤延至下一自身回合结束
            _apply_stacked_buff(u, 'dhil_chizha', '叱咤',
                                {'CRIT_DMG': 12.0 * cl}, turns)
            state.log.append(f'  【叱咤】{cl}层: 暴伤+{12.0 * cl:.0f}%')


def _dhil_bond_fua(state, u):
    """奇缘星魂1·沧溟间，月落潮平: 6段×500%ATK虚数全体(视为普攻)+伤害240%×3回合+1逆鳞。"""
    _apply_stacked_buff(u, 'dhil_bond', '沧溟间，月落潮平',
                        {'DMG_BONUS_ALL': 240.0}, 3)
    stats = _build_effective_stats(u, state)
    total = 0.0
    for _seg in range(6):
        for e in list(state.alive_enemies()):
            d = calculate_damage(stats, _enemy_for_damage(e, 'basic'), stats.ATK, 500.0,
                                 'direct', '虚数', 80, stats.CRIT_RATE >= 0.5,
                                 true_dmg_ratio=state.realm_true_dmg,
                                 skill_type='basic', attack_type='basic',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, e, d.final_damage)
            total += d.final_damage
    _gain_scales(state, u, 1)
    u.total_damage_dealt += total
    u.damage_log.append(('沧溟间，月落潮平', total, 'follow_up'))
    state.log.append(f'  奇缘星魂1·沧溟间: 追击6段×500%全体 {total:.0f} '
                     f'+伤害240%×3回合 +1逆鳞')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)


def _dhil_settle_sp_watch(u_actor, state, skill, skill_key, total_dmg):
    """settle_sp_watch(观察者): 我方每消耗5个战技点(逆鳞抵扣同计)→饮月追击。"""
    dhil = next((x for x in state.units
                 if x.char.id == CHAR_ID and x.is_alive), None)
    if dhil is None or getattr(dhil, 'fate_bond_rank', 0) < 1:
        return
    mark = state.extra.get('dhil_sp_mark', 0)
    spent = state.extra.get('lc_sp_spent', 0) - mark
    fired = 0
    while spent >= 5:
        mark += 5
        spent -= 5
        _dhil_bond_fua(state, dhil)
        fired += 1
    state.extra['dhil_sp_mark'] = mark


def _dhil_settle_ally_ult(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_ally_ult: E6·见谯——我方其他角色施放终结技后→盘拏耀跃抗穿层+1(≤3)。"""
    if skill_key != 'ultimate':
        return
    dhil = next((x for x in state.units
                 if x.char.id == CHAR_ID and x.is_alive), None)
    if dhil is None or dhil.eidolon_rank < 6 or u is dhil:
        return  # txt: 我方"其他"角色
    dhil.extra['dhil_e6'] = min(3, dhil.extra.get('dhil_e6', 0) + 1)
    state.log.append(f"  E6·见谯: 抗穿层 {dhil.extra['dhil_e6']}/3")


SETTLE_HANDLERS = {'settle_self': _dhil_settle_self,
                   'settle_sp_watch': _dhil_settle_sp_watch,
                   'settle_ally_ult': _dhil_settle_ally_ult}


def _tech_dhil(state, u, is_opener):
    """秘技·掣空如虹: 进战120%ATK虚数全体+1逆鳞。"""
    if not is_opener:
        return
    stats = _build_effective_stats(u, state)
    total = 0.0
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e, 'technique'), stats.ATK, 120.0,
                             'direct', '虚数', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        total += d.final_damage
    _gain_scales(state, u, 1)
    u.total_damage_dealt += total
    state.log.append(f'[秘技] 掣空如虹: 全敌120%ATK虚数 {total:.0f} +1逆鳞')


def _init_battle(state):
    u = next((x for x in state.units if x.char.id == CHAR_ID), None)
    if u is None:
        return
    u.current_energy = min(u.char.max_energy, u.current_energy + 15.0)  # 行迹1·伏辰
    state.log.append('  行迹1·伏辰: 战斗开始+15能量')


CHAR_ID = "dan_heng_imbibitor_lunae"
AI = _dhil_ai
INIT = _init_battle
TECHNIQUE = _tech_dhil
SKILL_HOOKS = SKILL_HOOKS

PHASE_HOOKS = {
    'sp_cost_override': _dhil_sp_cost,
    'attack_type_override': _dhil_attack_type,
    'energy_gain_override': _dhil_energy_gain,
    'target_stats_mod': _dhil_target_stats,
    'damage_mod': _dhil_damage_mod,
}
