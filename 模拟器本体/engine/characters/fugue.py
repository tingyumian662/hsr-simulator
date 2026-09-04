"""fugue（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _hook_owner, _tech_enemies
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _effective_spd
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _roll_effect_hit


def _fugue_cloudfire_apply(u, state, **kw):
    """忘归人天赋: 敌方额外40%韧性上限的【云火昭】（击破后仍可削, 削至0二次击破）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    for e in state.enemies:
        if e.extra_toughness_max == 0 and e.max_toughness > 0:
            e.extra_toughness_max = e.max_toughness * 0.4
            e.extra_toughness = e.extra_toughness_max
            state.log.append(f'  云火昭: {e.name or e.id} 额外韧性+{e.extra_toughness_max:.0f}')


def _fugue_cloudfire_death(u, state, **kw):
    """忘归人阵亡 → 云火昭失效（光环语义）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue':
        return
    for e in state.enemies:
        e.extra_toughness_max = 0.0
        e.extra_toughness = 0.0
    state.log.append('  云火昭失效: 忘归人阵亡')


def _fugue_foxian_def_down(u, state, **kw):
    """忘归人天赋: 狐祈者攻击→100%基础概率目标DEF-18% 2回合
    v5.6: 接入统一 EHR 检定（enemy.effect_res 默认0=必中）"""
    if not u.extra.get('_foxian'):
        return
    t = kw.get('target')
    if t is None or t.HP <= 0:
        return
    from engine.core.combat_engine import _roll_effect_hit
    if not _roll_effect_hit(u, state, t, '防御降低', base_chance=1.0):
        return
    from engine.models.enemy import EnemyStatus
    t.add_status(EnemyStatus(id='fugue_def_down', name='防御降低', category='debuff',
                             source='fugue', remaining_turns=2,
                             attributes={'def_reduction': 0.18}))
    state.log.append(f'  狐祈: {t.name or t.id} DEF-18% (2回合)')


def _fugue_trace1_break_delay(u, state, **kw):
    """行迹1·青丘重光: 我方造成弱点击破后敌方行动额外延后15%"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    t = kw.get('enemy')
    if t:
        t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 1500.0
        state.log.append(f'  行迹1·青丘重光: {t.name or t.id}行动延后15%')


def _fugue_trace2_team_be(u, state, **kw):
    """行迹2·玑星太素: 敌方弱点被击破→除自身外队友BE+6%（自身BE≥220%→+18%）, 2回合最多2层"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    bonus = 0.18 if owner.base_stats.BREAK_EFFECT >= 2.20 else 0.06
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu is owner or not eu.is_alive:
            continue
        layers = [b for b in eu.buffs if getattr(b, 'param_id', '') == 'fugue_trace2_be']
        while len(layers) >= 2:
            eu.buffs.remove(layers.pop(0))
        eu.buffs.append(TimedBuff(source_id='fugue',
                                  attributes={'BREAK_EFFECT': bonus * 100.0},
                                  remaining_turns=2, source_name='行迹·玑星太素',
                                  param_id='fugue_trace2_be'))
    state.log.append(f'  行迹2·玑星太素: 队友击破特攻+{bonus*100:.0f}% (2回合)')


def _fugue_trace3_self_be(u, state, **kw):
    """行迹3·涂山玄设: 自身击破特攻+30%"""
    if u.char.id != 'fugue':
        return
    u.base_stats.BREAK_EFFECT += 0.30
    state.log.append('  行迹3·涂山玄设: 击破特攻+30%')


def _fugue_trace3_first_sp(u, state, **kw):
    """行迹3·涂山玄设: 本场首次战技后立即恢复1点战技点"""
    if u.char.id != 'fugue' or u.extra.get('fugue_t3_sp_used'):
        return
    u.extra['fugue_t3_sp_used'] = True
    from engine.core.combat_engine import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  行迹3: 首次战技回1战技点')


def _eid_fugue_e2_energy(u, state, **kw):
    """忘归人E2: 敌方弱点被击破时忘归人恢复3点能量"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    from engine.core.combat_engine import _gain_energy
    gained = _gain_energy(owner, 3, state=state)
    state.log.append(f'  E2: 击破回能+{gained:.0f}')


def _eid_fugue_e2_ult(u, state, **kw):
    """忘归人E2: 施放终结技后我方全体行动提前24%"""
    if u.char.id != 'fugue':
        return
    from engine.core.combat_engine import _effective_spd
    from engine.characters.robin_summeretto import _guest_advance_blocked as _guest_fn
    from engine.runtime import AV_PER_TURN
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu.is_alive and i in navs and not _guest_fn(state, u, eu):
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * 0.24)
    state.log.append('  E2: 终结技后全队行动提前24%')


def _tech_fugue(state, u, is_opener):
    """忘归人: 行动提前40% + 全敌DEF-18% 2回合(100%基础概率走EHR)（忘归人.txt 秘技·炤炤彻旷）"""
    from engine.core.combat_engine import _roll_effect_hit
    from engine.runtime import AV_PER_TURN
    from engine.models.enemy import EnemyStatus
    navs = state.extra.get('navs', {})
    uidx = state.units.index(u)
    if uidx in navs:
        navs[uidx] = max(0, navs[uidx] - AV_PER_TURN / max(u.base_stats.SPD, 1) * 0.40)
    for e in _tech_enemies(state):
        if not _roll_effect_hit(u, state, e, '防御降低', base_chance=1.0):
            continue
        e.add_status(EnemyStatus(id='fugue_def_down', name='防御降低', category='debuff',
                                 source='fugue', remaining_turns=2,
                                 attributes={'def_reduction': 0.18}))
    state.log.append('[秘技] 炤炤彻旷: 行动提前40% + 全敌DEF-18% 2回合')


CHAR_ID = "fugue"
TECHNIQUE = _tech_fugue
BREAK_CONFIG = {'spd_target': 134.0}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _fugue_foxian_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['fugue_foxian']: 狐祈仅最新目标生效; E6炽灼时全队化。"""
    from engine.core.combat_engine import BUFF_REGISTRY
    # v5.3 忘归人狐祈: 仅最新目标生效（先移除全体旧狐祈）; E6炽灼时全队化
    for eu in state.units:
        kept = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'fugue_foxian']
        if len(kept) != len(eu.buffs):
            eu.buffs = kept
        eu.extra.pop('_foxian', None)
    e6_all = (u.eidolon_rank >= 6 and
              any(getattr(b, 'attributes', {}).get('_chizhuo') for b in u.buffs))
    if e6_all:
        tgt = [x for x in state.units if x.is_alive]
        state.log.append('  E6: 炽灼状态狐祈对我方全体生效')
    else:
        # 单目标狐祈: 主C惯例（与 single_ally 通用分支口径一致）
        main = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
        tgt = [main] if main else [u]
    for t in tgt:
        t.buffs.append(TimedBuff(source_id=u.char.id,
                                 attributes=dict(BUFF_REGISTRY['fugue_foxian']),
                                 remaining_turns=3, source_name=skill.name,
                                 param_id='fugue_foxian'))
        t.extra['_foxian'] = True  # 狐祈标记（削韧减半/效率/击破伤害判定用）
        state.log.append(f'  buff 狐祈 → {t.char.name} (3回合)')
    return True


def _fugue_chizhuo_duration(u, state, attrs, skill):
    """EFFECT_MUTATORS['fugue_chizhuo']: 炽灼持续3回合。"""
    return attrs, 3


EFFECT_TAKEOVERS = {'fugue_foxian': _fugue_foxian_takeover}
EFFECT_MUTATORS = {'fugue_chizhuo': _fugue_chizhuo_duration}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _fugue_key_rewrite(u, state, skill_key):
    """PHASE key_rewrite: 炽灼状态普攻强化为冉冉方炽（→新键|None）。"""
    if skill_key == 'basic_attack' and \
            any(getattr(b, 'attributes', {}).get('_chizhuo') for b in u.buffs):
        return 'basic_attack_enhanced'
    return None


PHASE_HOOKS = {'key_rewrite': _fugue_key_rewrite}
