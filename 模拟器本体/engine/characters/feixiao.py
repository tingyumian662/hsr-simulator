"""feixiao（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _apply_toughness_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _record_kill_after_damage
from engine.core.combat_engine import _use_skill


def _feixiao_gain_fly(u, amt=1):
    """飞黄（上限12）"""
    u.extra['feixiao_fly'] = min(12, u.extra.get('feixiao_fly', 0) + amt)
    return u.extra['feixiao_fly']


def _feixiao_fua(state, u, target, from_skill=False):
    """天赋FUA: 110%ATK风伤(破韧目标削韧5); 发动时自身增伤60% 2回合;
    E2: 每FUA+1飞黄(每回合最多6次); E6: FUA视为终结技伤害+倍率+140%"""
    from engine.core.combat_engine import (TimedBuff, _build_effective_stats, _apply_toughness_damage, _enemy_for_damage, _record_kill_after_damage, calculate_damage)
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
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=d.final_damage > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


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
    from engine.core.combat_engine import TimedBuff
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
    from engine.core.combat_engine import (_apply_toughness_damage, _build_effective_stats, calculate_damage, _enemy_for_damage, _record_kill_after_damage)
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
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 0倍率终结技补气氛


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


def _tech_feixiao(state, u, is_opener):
    """飞霄: 标记秘技生效——每波200%ATK必暴风伤+1飞黄（进战·岚身; _respawn_wave 接线）"""
    if not is_opener:
        return
    from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, calculate_damage
    from engine.runtime import _enemy_for_damage
    state.extra['feixiao_tech_active'] = True
    alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
    if alive:
        stats = _build_effective_stats(u, state)
        scale = 200.0 + min(len(alive) - 1, 8) * 100.0
        for e in alive:
            d = calculate_damage(stats, _enemy_for_damage(e, 'technique'), stats.ATK, scale,
                                 'direct', '风', 80, True,
                                 true_dmg_ratio=state.realm_true_dmg,
                                 crit_mode='boolean')
            _commit_enemy_damage(
                state, u, e, d.final_damage,
                cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
            u.total_damage_dealt += d.final_damage
        state.log.append(f'[秘技] 岚身: 每波{scale:.0f}%ATK必暴({len(alive)}敌)')
    _feixiao_gain_fly(u, 1)
    state.log.append('[秘技] 岚身: +1飞黄')


def _init_battle(state):
    for u in state.units:
        if u.char.id == CHAR_ID:
            u.extra['feixiao_fly'] = 3  # 行迹1: 开局3飞黄
            u.extra['feixiao_fua_used'] = False



def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _feixiao_skill(state, u)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _feixiao_ult(state, u)


CHAR_ID = "feixiao"
AI = _feixiao_ai
INIT = _init_battle
TECHNIQUE = _tech_feixiao
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _feixiao_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'feixiao':
        _feixiao_tick(state, u)


TURN_TICKS = {'pre': _feixiao_turn_tick}
