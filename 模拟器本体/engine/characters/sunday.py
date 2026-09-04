"""sunday（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _set_av
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _pick_single_ally_target
from engine.core.combat_engine import _use_skill


def _sunday_pick_target(state, u):
    """星期日技能目标: 我方单体（含忆灵; 优先存活）"""
    from engine.core.combat_engine import _pick_single_ally_target
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
    from engine.core.combat_engine import TimedBuff, _build_effective_stats
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
    from engine.core.combat_engine import TimedBuff
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
    from engine.core.combat_engine import (TimedBuff, _set_av, _build_effective_stats, _gain_skill_points, _pick_single_ally_target)
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
        from engine.characters.robin_summeretto import _guest_advance_blocked
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
    from engine.core.combat_engine import (_gain_energy, _gain_skill_points, _pick_single_ally_target)
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
    from engine.core.combat_engine import _gain_energy
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


def _trace_sunday_trace2(u, state, **kw):
    """星期日行迹2·崇高拂尘: 战斗开始恢复25能量"""
    if u.char.id != 'sunday':
        return
    from engine.core.combat_engine import _gain_energy
    _gain_energy(u, 25.0, state=state)
    state.log.append('  星期日行迹2: 开局回25能量')


def _trace_sunday_tick(u, state, **kw):
    """星期日回合开始: 蒙福者倒计时+E4回8能量"""
    if u.char.id != 'sunday':
        return

    _sunday_tick(state, u)


def _tech_sunday(state, u, is_opener):
    """星期日: 下次战斗首次技能目标增伤50% 2回合（非进战·荣光之秘）"""
    from engine.runtime import TimedBuff
    state.extra['sunday_tech_pending'] = True
    state.log.append('[秘技] 荣光之秘: 下次战斗首次技能目标增伤50%')


def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _sunday_skill(state, u)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _sunday_ult(state, u)


CHAR_ID = "sunday"
AI = _sunday_ai
TECHNIQUE = _tech_sunday
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _sunday_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'sunday':
        _sunday_tick(state, u)


TURN_TICKS = {'pre': _sunday_turn_tick}
