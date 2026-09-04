"""ruan_mei（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage, _set_av
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _use_skill


def _ruanmei_field_active(state) -> bool:
    """阮·梅结界激活判断（独立于 realm_owner 境界系统）"""
    return state.extra.get('ruanmei_field_turns', 0) > 0


def _ruanmei_xianyin_apply(state, u):
    """战技【弦外音】3回合: 全队增伤32%+弱点击破效率50%;
    行迹3: BE>120%每超10%增伤额外+6%上限36%"""
    from engine.core.combat_engine import TimedBuff, _build_effective_stats
    bonus = 32.0
    trace3 = any(getattr(tr, 'hook_name', '') == 'ruan_mei_trace3' for tr in (u.char.traces or []))
    if trace3:
        be = _build_effective_stats(u, state).BREAK_EFFECT
        if be > 1.20:
            bonus = min(32.0 + int((be - 1.20) / 0.10) * 6.0, 68.0)  # 32+36=68 上限
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'ruanmei_xianyin']
            eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                      attributes={'DMG_BONUS_ALL': bonus,
                                                  'TOUGHNESS_EFFICIENCY': 50.0},
                                      remaining_turns=3, param_id='ruanmei_xianyin',
                                      source_name='弦外音'))
    u.extra['ruanmei_xianyin_turns'] = 3
    state.log.append(f'  阮·梅战技: 【弦外音】全队增伤{bonus:.0f}%+破韧效率50% 3回合')


def _ruanmei_field_apply(state, u):
    """终结技结界2回合(E6+1): 全队全抗穿透25%+E1无视20%防御; 攻击后挂残梅绽"""
    from engine.core.combat_engine import TimedBuff
    turns = 3 if u.eidolon_rank >= 6 else 2  # E6: 结界+1回合
    state.extra['ruanmei_field_turns'] = turns
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'ruanmei_field']
            attrs = {'RES_PEN_ALL': 25.0}
            if u.eidolon_rank >= 1:
                attrs['DEF_PEN'] = 20.0  # E1: 全队无视20%防御
            eu.buffs.append(TimedBuff(source_id='ruan_mei', attributes=attrs,
                                      remaining_turns=turns, param_id='ruanmei_field',
                                      source_name='阮·梅结界'))
    state.log.append(f'  阮·梅终结技: 展开结界{turns}回合(全抗穿透25%'
                     f'{", E1无视20%防御" if u.eidolon_rank >= 1 else ""})')


def _ruanmei_apply_canmei(state, u, target):
    """结界期攻击后对目标挂【残梅绽】（恢复前不可重复挂）"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if not _ruanmei_field_active(state):
        return
    if target.has_status(status_id='ruanmei_canmei'):
        return
    target.add_status(EnemyStatus(id='ruanmei_canmei', name='残梅绽', category='debuff',
                                  source='ruan_mei', remaining_turns=-1,
                                  attributes={}))
    state.log.append(f'  【残梅绽】: {target.name or target.id}')


def _ruanmei_canmei_trigger(state, u, enemy):
    """敌方从破韧恢复时【残梅绽】触发: 延长破韧+行动延后(20%×BE+10%)+冰击破伤害50%"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage, _enemy_for_damage)
    from engine.core.combat_engine import AV_PER_TURN, _set_av

    if not enemy.has_status(status_id='ruanmei_canmei'):
        return False
    enemy.remove_status('ruanmei_canmei')
    stats = _build_effective_stats(u, state)
    delay = stats.BREAK_EFFECT * 0.20 + 0.10
    delay_av = AV_PER_TURN * delay
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i), state.current_av + delay_av)
    # v6.9.1: 残梅绽延后直接重排敌方 AV, 不再写 av_delayed（旧路径早退不消费）
    before = enemy.HP
    d = calculate_damage(stats, _enemy_for_damage(enemy), stats.ATK, 50.0,
                         'break', '冰', 80, False, crit_mode='expected')
    d.final_damage *= (1.0 + stats.BREAK_EFFECT)  # 击破伤害乘区
    _commit_enemy_damage(state, u, enemy, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  【残梅绽】触发: {enemy.name or enemy.id} 延后{delay*100:.0f}%'
                     f'+冰击破{d.final_damage:.0f}')
    return True  # 保持破韧（延长）


def _ruanmei_break_damage(state, u, target):
    """天赋: 我方击破弱点时阮·梅对目标120%冰击破伤害(E6+200%→320%)"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage, _enemy_for_damage)
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive), None)
    if ruan is None:
        return
    stats = _build_effective_stats(ruan, state)
    scale = 120.0 if ruan.eidolon_rank < 6 else 320.0  # E6: 天赋击破倍率+200%
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, scale,
                         'break', '冰', 80, False, crit_mode='expected')
    d.final_damage *= (1.0 + stats.BREAK_EFFECT)
    _commit_enemy_damage(state, ruan, target, d.final_damage)
    ruan.total_damage_dealt += d.final_damage
    # E4: 击破时自身击破特攻+100% 3回合
    if ruan.eidolon_rank >= 4:
        ruan.buffs = [b for b in ruan.buffs if getattr(b, 'param_id', '') != 'ruanmei_e4_be']
        from engine.core.combat_engine import TimedBuff
        ruan.buffs.append(TimedBuff(source_id='ruan_mei', attributes={'BREAK_EFFECT': 100.0},
                                    remaining_turns=3, param_id='ruanmei_e4_be',
                                    source_name='阮·梅E4'))
        state.log.append('  阮·梅E4: 击破特攻+100% 3回合')
    state.log.append(f'  阮·梅天赋: 击破冰伤{d.final_damage:.0f}')


def _ruanmei_canmei_trigger_v3(state, u, enemy):
    """v6.9.1: 残梅绽修复——统一冰击破结果×50%（不再重复乘BE）;
    直接重排敌方下一AV=当前+延后, 保持破韧到该时间点。"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _enemy_for_damage, _record_enemy_kill, AV_PER_TURN, _set_av)
    if not enemy.has_status(status_id='ruanmei_canmei'):
        return False
    enemy.remove_status('ruanmei_canmei')
    stats = _build_effective_stats(u, state)
    delay = stats.BREAK_EFFECT * 0.20 + 0.10
    delay_av = AV_PER_TURN * delay
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i), state.current_av + delay_av)
    before = enemy.HP
    d = calculate_damage(stats, _enemy_for_damage(enemy), 0, 0, 'break', '冰', 80, False)
    d.final_damage *= 0.50
    _commit_enemy_damage(state, u, enemy, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  【残梅绽】触发: {enemy.name or enemy.id} 延后{delay*100:.0f}%'
                     f'+冰击破{d.final_damage:.0f}')
    return True


def _ruanmei_break_damage_v3(state, u, target):
    """v6.9.1: 天赋击破修复——统一冰击破结果×120%（E6 320%）, 不重复乘BE; E4=+100%BE原始数值。"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage, _enemy_for_damage, TimedBuff)
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive), None)
    if ruan is None:
        return
    stats = _build_effective_stats(ruan, state)
    scale = 1.20 if ruan.eidolon_rank < 6 else 3.20  # E6: +200%→320%
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), 0, 0, 'break', '冰', 80, False)
    d.final_damage *= scale
    _commit_enemy_damage(state, ruan, target, d.final_damage)
    ruan.total_damage_dealt += d.final_damage
    if ruan.eidolon_rank >= 4:
        ruan.buffs = [b for b in ruan.buffs if getattr(b, 'param_id', '') != 'ruanmei_e4_be']
        ruan.buffs.append(TimedBuff(source_id='ruan_mei', attributes={'BREAK_EFFECT': 100.0},
                                    remaining_turns=3, param_id='ruanmei_e4_be',
                                    source_name='阮·梅E4'))
        state.log.append('  阮·梅E4: 击破特攻+100% 3回合')
    state.log.append(f'  阮·梅天赋: 击破冰伤{d.final_damage:.0f}')


def _ruanmei_tick(state, u):
    """阮·梅回合开始: 弦外音/结界权威倒计时 + 行迹2回能。"""
    from engine.core.combat_engine import _gain_energy
    xianyin_turns = u.extra.get('ruanmei_xianyin_turns', 0)
    if xianyin_turns > 0:
        xianyin_turns -= 1
        u.extra['ruanmei_xianyin_turns'] = xianyin_turns
        if xianyin_turns <= 0:
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'ruanmei_xianyin']
            state.log.append('  阮·梅弦外音: 结束')
    turns = state.extra.get('ruanmei_field_turns', 0)
    if turns > 0:
        state.extra['ruanmei_field_turns'] = turns - 1
        if turns - 1 <= 0:
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'ruanmei_field']
            state.log.append('  阮·梅结界: 结束')
    if any(getattr(tr, 'hook_name', '') == 'ruan_mei_trace2' for tr in (u.char.traces or [])):
        _gain_energy(u, 5.0, state=state)
        state.log.append('  阮·梅行迹2: 回合开始回5能量')


def _ruanmei_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """阮·梅 AI: 满能量终结技(结界)→战技(弦外音)→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _trace_ruanmei_trace1(u, state, **kw):
    """阮·梅行迹1·物体呼吸中: 全队击破特攻+20%"""
    if u.char.id != 'ruan_mei':
        return
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                      attributes={'BREAK_EFFECT': 20.0},
                                      remaining_turns=-1, source_name='阮·梅行迹1'))
            if eu is not u:
                eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                          attributes={'SPD_PERCENT': 10.0},
                                          remaining_turns=-1, source_name='阮·梅天赋·分型的螺旋'))

    state.log.append('  阮·梅行迹1: 全队击破特攻+20%')


def _trace_ruanmei_tick(u, state, **kw):
    """阮·梅回合开始: 结界-1+行迹2回5能量"""
    if u.char.id != 'ruan_mei':
        return

    _ruanmei_tick(state, u)


def _trace_ruanmei_break(u, state, enemy=None, target=None, **kw):
    """阮·梅天赋: 我方击破弱点→对目标120%冰击破伤害(E6+200%)
    on_any_weakness_break 事件传参为 enemy; 兼容 target 别名"""

    t = enemy if enemy is not None else target
    if t is not None:
        _ruanmei_break_damage_v3(state, None, t)


def _tech_ruanmei(state, u, is_opener):
    """阮·梅: 自动触发1次战技(不耗SP)（非进战·拭琴抚罗袂）"""

    _ruanmei_xianyin_apply(state, u)
    state.log.append('[秘技] 拭琴抚罗袂: 自动触发1次战技(弦外音)')


def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _ruanmei_xianyin_apply(state, u)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _ruanmei_field_apply(state, u)


CHAR_ID = "ruan_mei"
AI = _ruanmei_ai
TECHNIQUE = _tech_ruanmei
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _ruan_mei_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'ruan_mei':
        _ruanmei_tick(state, u)


TURN_TICKS = {'pre': _ruan_mei_turn_tick}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _ruanmei_field_react(u, state, total_dmg):
    """OBSERVER field_react: 结界期队友攻击→受击目标挂【残梅绽】。"""
    # v6.9 阮·梅: 结界期攻击后对受击目标挂【残梅绽】
    if u.char.id != 'ruan_mei' and total_dmg > 0 and _ruanmei_field_active(state):
        for t in state.extra.get('last_attack_targets', []):
            _ruanmei_apply_canmei(state, u, t)
    return None


OBSERVER_HOOKS = {'field_react': _ruanmei_field_react}
