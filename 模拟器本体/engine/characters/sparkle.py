"""sparkle（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _use_skill


def _sparkle_ult_sp(state):
    """v6.10.6 C3: 花火终结技回6战技点, 溢出记录≤10（TXT 花火.txt:39）"""
    cap = state.max_sp
    before = state.skill_points
    state.skill_points = min(cap, before + 6)
    overflow = max(0, (before + 6) - cap)
    state.extra['sparkle_sp_reserve'] = min(
        10, state.extra.get('sparkle_sp_reserve', 0) + overflow)
    state.log.append(f'  花火终结技: SP {before}→{state.skill_points} '
                     f'(溢出记录{state.extra["sparkle_sp_reserve"]:.0f})')


def _sparkle_turn_end_reserve(state, u):
    """v6.10.6 C3: 我方角色回合结束后, 花火消耗溢出记录补战技点至上限（TXT 花火.txt:39）"""
    reserve = state.extra.get('sparkle_sp_reserve', 0)
    if reserve <= 0 or state.skill_points >= state.max_sp:
        return
    need = state.max_sp - state.skill_points
    use = min(need, reserve)
    state.skill_points += use
    state.extra['sparkle_sp_reserve'] = reserve - use
    state.log.append(f'  花火记录: 回合结束补{use:.0f}SP')


def sparkle_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
    # v6.10.6 E: 删除手工拉条——通用 action_advance（_apply_skill_effects）已处理50%,
    # 此前双重拉条实际接近100%; 目标选择也不再硬编码希儿（战技效果自行选目标）
    if unit.current_energy >= unit.char.max_energy:
        _use_skill(unit, state, 'ultimate')
    elif state.skill_points > 0:
        _use_skill(unit, state, 'skill')
    else:
        _use_skill(unit, state, 'basic_attack')


def _trace_sparkle_sp_limit(u, state, **kw):
    """花火天赋·叙述性诡计: 战技点上限+2; E4 额外+1（对称维护, 死亡回减）
    v6.10.6 C3"""
    if u.char.id != 'sparkle':
        return
    bonus = 2 + (1 if u.eidolon_rank >= 4 else 0)
    state.max_sp += bonus
    u.extra['sparkle_max_sp_bonus'] = bonus
    state.log.append(f'  花火天赋: 战技点上限+{bonus}')


def _trace_sparkle_turn_end(u, state, **kw):
    """v6.10.6 C3: 我方角色回合结束后, 花火消耗溢出记录补战技点至上限（TXT 花火.txt:39）"""

    _sparkle_turn_end_reserve(state, u)


def _trace_sparkle_team_cd(u, state, **kw):
    """花火行迹3·夜想曲: 全队ATK+45% + 持战技CD buff者全抗穿+10%
    v6.10.6 C: 改为 _build_effective_stats 动态消费（此前永久写全队暴伤且非TXT口径）"""
    if u.char.id != 'sparkle':
        return


def _eid_sparkle_e1(u, state, **kw):
    """花火E1: 谜诡持有者ATK+40%（动态面板消费）+ 花火自身SPD+15% 2回合
    v6.10.6 C1: 删除硬编码希儿与永久改面板; ATK 部分在 _build_effective_stats 动态消费"""
    from engine.runtime import TimedBuff
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'sparkle_e1_spd']
    u.buffs.append(TimedBuff(source_id='sparkle', attributes={'SPD_PERCENT': 15.0},
                             remaining_turns=2, param_id='sparkle_e1_spd',
                             source_name='花火E1·悬置怀疑'))
    state.log.append('  花火E1: 自身SPD+15%(2回合)')


def _eid_sparkle_e2(u, state, **kw):
    """花火E2: 天赋每层额外减防10%"""
    pass  # 在花火天赋中处理


def _eid_sparkle_e4(u, state, **kw):
    """花火E4: 终结技回1SP + SP上限+1"""
    state.max_sp += 1
    from engine.core.combat_engine import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  花火E4: 终结技回1SP, SP上限+1')


def _eid_sparkle_e6(u, state, **kw):
    """花火E6: 战技CD额外+30%花火CD + 谜诡扩散"""
    pass  # 在花火战技buff中处理


def _tech_sparkle(state, u, is_opener):
    """花火: 迷误状态期间进战→恢复3战技点 + 花火回20能量（花火.txt 秘技·不可靠叙事者, 非进战）"""
    from engine.core.combat_engine import _gain_skill_points, _gain_energy
    _gain_skill_points(state, 3)
    _gain_energy(u, 20.0, state=state)
    state.log.append('[秘技] 不可靠叙事者: 恢复3战技点 + 花火回20能量')


CHAR_ID = "sparkle"
AI = sparkle_ai
TECHNIQUE = _tech_sparkle


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _sparkle_ult_buff_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['sparkle_ult_buff']: 全体谜诡3回合 + 回6战技点(溢出记录≤10)。"""
    # v6.10.6 C1: 花火终结技——全体谜诡3回合 + 回6战技点(溢出记录≤10)
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'sparkle_mystery']
            eu.buffs.append(TimedBuff(source_id='sparkle', attributes={},
                                      remaining_turns=3, param_id='sparkle_mystery',
                                      source_name='谜诡'))
    _sparkle_ult_sp(state)
    state.log.append('  花火终结技: 全体谜诡(3回合) + 回6战技点')
    return True


def _sparkle_cd_buff_mutator(u, state, attrs, skill):
    """EFFECT_MUTATORS['sparkle_cd_buff']: 花火战技CD buff（E6: 额外+花火暴伤30%）。"""
    # 特殊处理：花火战技CD buff（E6: 额外+花火暴伤30%）
    cd_val = u.base_stats.CRIT_DMG * 0.24 + 0.45
    if u.eidolon_rank >= 6:
        cd_val += u.base_stats.CRIT_DMG * 0.30
    attrs = {'CRIT_DMG': round(cd_val * 100, 1)}
    # v6.10.6 C1: E6 战技CD效果扩散至持有谜诡的队友; E1 施放战技时花火SPD+15%刷新
    if u.eidolon_rank >= 6:
        for eu in state.units:
            if eu.is_alive and any(getattr(b, 'param_id', '') == 'sparkle_mystery'
                                   for b in eu.buffs):
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'sparkle_cd_buff']
                eu.buffs.append(TimedBuff(source_id='sparkle',
                                          attributes=dict(attrs),
                                          remaining_turns=2,
                                          param_id='sparkle_cd_buff',
                                          source_name='花火战技·梦游鱼(E6扩散)'))
    if u.eidolon_rank >= 1:
        u.buffs = [b for b in u.buffs
                   if getattr(b, 'param_id', '') != 'sparkle_e1_spd']
        u.buffs.append(TimedBuff(source_id='sparkle',
                                 attributes={'SPD_PERCENT': 15.0},
                                 remaining_turns=2, param_id='sparkle_e1_spd',
                                 source_name='花火E1·悬置怀疑'))
    return attrs, 2


EFFECT_TAKEOVERS = {'sparkle_ult_buff': _sparkle_ult_buff_takeover}
EFFECT_MUTATORS = {'sparkle_cd_buff': _sparkle_cd_buff_mutator}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _sparkle_sp_cost_override(u, state, sp_cost, skill_key):
    """PHASE sp_cost_override: 人造花——单回合耗SP≥3后下次战技免SP（→新值|None）。"""
    # v6.10.6 C2: 花火行迹2·人造花——单回合耗SP≥3后, 下次战技免SP
    if skill_key == 'skill' and state.extra.get('sparkle_free_skill'):
        state.extra.pop('sparkle_free_skill', None)
        state.log.append('  花火行迹2: 本次战技免SP')
        return 0
    return None


PHASE_HOOKS = {'sp_cost_override': _sparkle_sp_cost_override}
