"""robin（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage, _set_av
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _ensure_marker_system
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _use_skill


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


def _robin_skill(state, u):
    """战技: 全队增伤50% 3回合(回合开始-1); 行迹3额外回5能"""
    from engine.core.combat_engine import TimedBuff, _gain_energy
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
    from engine.core.combat_engine import TimedBuff, _set_av
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
        from engine.characters.robin_summeretto import _guest_advance_blocked
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
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _enemy_for_damage, _gain_energy, _record_kill_after_damage)
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
        from engine.core.combat_engine import _set_av
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


def _robin_concert_active(u) -> bool:
    return bool(u.extra.get('robin_concert'))


def _trace_robin_trace2(u, state, **kw):
    """知更鸟行迹2·华彩花腔: 战斗开始自身行动提前25%"""
    if u.char.id != 'robin':
        return
    from engine.runtime import TimedBuff, _set_av
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='robin', attributes={'CRIT_DMG': 20.0},
                                      remaining_turns=-1, source_name='知更鸟天赋·华彩花腔'))
    state.log.append('  知更鸟天赋: 全队暴伤+20%')

    navs = state.extra.get('navs', {})
    unit_index = next((i for i, unit in enumerate(state.units) if unit is u), None)
    if unit_index is not None and unit_index in navs:
        remaining_av = max(0.0, navs[unit_index] - state.current_av)
        _set_av(state, navs, unit_index, state.current_av + remaining_av * 0.75)
    else:
        u.extra['initial_action_advance_ratio'] = \
            u.extra.get('initial_action_advance_ratio', 0.0) + 0.25
    state.log.append('  知更鸟行迹2: 开局行动提前25%')


def _tech_robin(state, u, is_opener):
    """知更鸟: 每波次开始回5能量（非进战·领域, 领域互斥; _respawn_wave 接线）"""
    state.extra['robin_tech_active'] = True
    state.log.append('[秘技] 酣醉序曲: 每波次开始知更鸟回5能量')


def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _robin_skill(state, u)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _robin_ult(state, u)


CHAR_ID = "robin"
AI = _robin_ai
TECHNIQUE = _tech_robin
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]
MARKERS = {"robin_concert": _robin_concert_marker_action}


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _robin_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'robin':
        _robin_skill_tick(state, u)


TURN_TICKS = {'pre': _robin_turn_tick}
