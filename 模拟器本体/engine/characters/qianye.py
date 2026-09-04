"""qianye（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _apply_enemy_taunt
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _ensure_marker_system
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _use_skill


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
    from engine.core.combat_engine import _gain_energy
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
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _enemy_for_damage)
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
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 额外战技(FUA)补气氛
    # E1: 额外战技后无量忿怒倒计时延后15%
    if u.eidolon_rank >= 1 and u.marker and u.marker.marker_id == 'qianye_wrath':
        u.marker.extra['next_av'] += AV_PER_TURN / max(u.marker.action_spd, 1.0) * 0.15
        state.log.append('  千冶·刃E1: 无量忿怒倒计时延后15%')


def _qianye_enter_wrath(state, u):
    """开启结界【无量忿怒】: CR+20%/CD+60%/普攻强化/解放战技/新终结技/70速倒计时"""
    from engine.core.combat_engine import TimedBuff
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
    from engine.core.combat_engine import _gain_energy
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
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _enemy_for_damage)
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


def _trace_qianye_trace1(u, state, **kw):
    """千冶·刃行迹1: 开战时能量不足75%则立即补至75%。"""
    if u.char.id != 'qianye':
        return
    target = (u.char.max_energy or 0) * 0.75
    if u.current_energy < target:
        u.current_energy = target
        state.log.append('  千冶·刃行迹1: 开局能量补至75%')


def _tech_qianye(state, u, is_opener):
    """千冶·刃: 全敌嘲讽1回合+自身受伤-90% 2回合（进战·十方无赦）"""
    from engine.core.combat_engine import _apply_enemy_taunt
    from engine.runtime import TimedBuff
    _apply_enemy_taunt(state, u, state.enemies, turns=1)
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'qianye_tech_dr']
    u.buffs.append(TimedBuff(source_id='qianye', attributes={'DMG_REDUCTION': 90.0},
                             remaining_turns=2, param_id='qianye_tech_dr',
                             source_name='十方无赦'))
    state.log.append('[秘技] 十方无赦: 全敌嘲讽1回合 + 自身受伤-90% 2回合')


def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _qianye_skill(state, u, skill_key)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _qianye_ult(state, u)


CHAR_ID = "qianye"
AI = _qianye_ai
TECHNIQUE = _tech_qianye
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]
MARKERS = {"qianye_wrath": _qianye_wrath_marker_action}


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _qianye_turn_tick(u, state):
    for qianye in state.units:
        if qianye.char.id == 'qianye':
            qianye.extra.pop('qianye_e6_charge_used', None)


TURN_TICKS = {'pre': _qianye_turn_tick}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _qianye_key_rewrite(u, state, skill_key):
    """PHASE key_rewrite: 无量忿怒期普攻强化为淬锋断魄（→新键|None）。"""
    # v6.9 千冶·刃: 无量忿怒期普攻强化为淬锋断魄
    if skill_key == 'basic_attack' and u.extra.get('qianye_wrath'):
        state.log.append('  无量忿怒: 普攻强化为【淬锋，断魄】')
        return 'basic_attack_enhanced'
    return None


def _qianye_new_ult_check(u, state, skill_key):
    """PHASE new_ult_check: 新式终结技（skill_enhanced）判定（True|None）。"""
    return True if skill_key == 'skill_enhanced' else None


def _qianye_skill_gate_pre(u, state, skill_key):
    """PHASE skill_gate_pre: 战技需无量忿怒; 新终结技需忿怒+满能量（True=中止）。"""
    # 千冶·刃技能门控走统一入口。新终结技继续执行下方完整技能、
    # 伤害、击杀、光锥和 Hook 管线，不再提前返回到手写结算。
    new_ult = skill_key == 'skill_enhanced'
    if skill_key == 'skill' and not _qianye_wrath_active(u):
        state.log.append('  [WARN] 千冶·刃: 未处于无量忿怒, 战技不可用')
        return True
    if new_ult and (not _qianye_wrath_active(u)
                    or u.current_energy < (u.char.max_energy or 0)):
        state.log.append('  [WARN] 千冶·刃: 新终结技需要无量忿怒与满能量')
        return True
    return None


def _qianye_skill_adjust_pre(u, state, skill, skill_key):
    """PHASE skill_adjust_pre: E6 新终结技倍率×1.5（→新skill|None）。"""
    if skill_key == 'skill_enhanced' and u.eidolon_rank >= 6:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 1.50
        return skill
    return None


PHASE_HOOKS = {'key_rewrite': _qianye_key_rewrite,
               'new_ult_check': _qianye_new_ult_check,
               'skill_gate_pre': _qianye_skill_gate_pre,
               'skill_adjust_pre': _qianye_skill_adjust_pre}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _qianye_post_attack_taunt(u, state, skill_key, total_dmg):
    """PHASE post_attack_taunt: 普攻/强化普攻对受击目标嘲讽1回合。"""
    if skill_key in ('basic_attack', 'basic_attack_enhanced') \
            and total_dmg > 0:
        _apply_enemy_taunt(state, u, state.extra.get('last_attack_targets', []), turns=1)
    return None


PHASE_HOOKS['post_attack_taunt'] = _qianye_post_attack_taunt


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _qianye_new_ult_finalize(u, state):
    """PHASE new_ult_finalize: 行迹1——新终结技后溢出能量恢复。"""
    overflow = u.extra.pop('qianye_overflow', 0.0)
    if overflow > 0:
        u.current_energy = min(u.char.max_energy, u.current_energy + overflow)
        state.log.append(f'  千冶·刃行迹1: 溢出能量{overflow:.0f}恢复')
    return None


PHASE_HOOKS['new_ult_finalize'] = _qianye_new_ult_finalize
