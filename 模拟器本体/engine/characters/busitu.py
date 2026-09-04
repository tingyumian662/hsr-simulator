"""busitu（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _use_skill


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
    from engine.core.combat_engine import _gain_skill_points
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
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage, _enemy_for_damage)
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
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _busitu_ult(state, u, target):
    """终结技: 目标成饲饵; 400%ATK(引擎)+3充能+强化FUA; 行迹1+2婪酣; E4 ATK+40% 3回合"""
    from engine.core.combat_engine import TimedBuff
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
    from engine.core.combat_engine import _gain_energy
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


def _trace_busitu_trace3(u, state, **kw):
    """不死途行迹3·头狼: 全队暴伤+40%+全队FUA暴伤+80%"""
    if u.char.id != 'busitu':
        return
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='busitu',
                                      attributes={'CRIT_DMG': 40.0,
                                                  'CRIT_DMG_ATK_follow_up': 80.0},
                                      remaining_turns=-1, source_name='不死途行迹3'))
    state.log.append('  不死途行迹3: 全队暴伤+40%+追加攻击暴伤80%')


def _trace_busitu_e1(u, state, **kw):
    """不死途E1: 在场全敌受伤+24%(≤50%HP时36%)"""
    if u.char.id != 'busitu':
        return
    for e in state.enemies:
        e.extra['busitu_e1_vuln'] = 0.24
        e.extra['busitu_e1_half_vuln'] = 0.36
        e.extra['busitu_e1_max_hp'] = e.HP
    state.log.append('  不死途E1: 全敌受伤+24%(≤50%HP时36%)')


def _tech_busitu(state, u, is_opener):
    """不死途: 全敌100%ATK雷伤+1充能（非进战·吃吧，可憎的手）"""
    from engine.core.combat_engine import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '雷', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    u.extra['busitu_charge'] = min(3, u.extra.get('busitu_charge', 0) + 1)
    state.log.append('[秘技] 吃吧，可憎的手: 全敌100%ATK雷伤 + 1充能')


def _init_battle(state):
    for u in state.units:
        if u.char.id == CHAR_ID:
            u.extra['busitu_charge'] = 2  # 天赋: 初始2充能



def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        target = state.alive_enemies()[0] if state.alive_enemies() else None
        _busitu_skill(state, u, target)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        target = state.alive_enemies()[0] if state.alive_enemies() else None
        _busitu_ult(state, u, target)


CHAR_ID = "busitu"
AI = _busitu_ai
INIT = _init_battle
TECHNIQUE = _tech_busitu
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _busitu_skill_filter(u, state, skill, skill_key):
    """PHASE skill_filter: 首战技——仅目标已是饲饵时才保留额外100%段（→新skill|None）。"""
    # v6.9.1: 不死途首战技——仅目标已是饲饵时才保留额外100%段
    if skill_key == 'skill' \
            and not u.extra.get('busitu_skill_was_bait') and skill.multipliers:
        skill = copy.deepcopy(skill)
        skill.multipliers = [m for m in skill.multipliers if m.scale != 100.0]
        state.log.append('  不死途战技: 首次施加饲饵, 无额外100%段')
        return skill
    return None


PHASE_HOOKS = {'skill_filter': _busitu_skill_filter}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _busitu_bait_react(u, state, total_dmg):
    """OBSERVER bait_react: 饲饵受其他目标攻击→回8能+耗1充能FUA。"""
    # v6.9 不死途: 饲饵受其他目标攻击→回8能+耗1充能FUA
    if u.char.id != 'busitu' and total_dmg > 0:
        _busitu_on_ally_attack(state, u)
    return None


OBSERVER_HOOKS = {'bait_react': _busitu_bait_react}
