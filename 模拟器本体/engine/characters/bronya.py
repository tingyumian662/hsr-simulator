"""bronya（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _process_lc_effects
from engine.core.combat_engine import _use_skill


def bronya_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if unit.current_energy >= unit.char.max_energy:
        _use_skill(unit, state, 'ultimate')
    elif state.skill_points > 0 and target:
        _use_skill(unit, state, 'skill')
        # 战技100%拉条（v7.1.0: 持特邀嘉宾时封锁——防永动机, 自拉条不受影响）
        for i, eu in enumerate(state.units):
            from engine.characters.robin_summeretto import _guest_advance_blocked
            if eu == target and i in navs \
                    and not _guest_advance_blocked(state, unit, eu):
                navs[i] = state.current_av
                break
        state.log.append(f'  拉条100% → {target.char.name}')
    else:
        _use_skill(unit, state, 'basic_attack')


def _trace_bronya_basic_crit(**kwargs):
    """布洛妮娅行迹「号令」：普攻暴击率100%。由 damage calc 前通过 stats 处理。"""
    pass  # 在 _use_skill 伤害循环内联 t_crit=True，此处为标记


def _trace_bronya_battle_def(u, state, **kw):
    """布洛妮娅行迹「阵地」: 战斗开始全队DEF+20% 2回合"""
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='bronya', attributes={"DEF_PERCENT": 20.0},
                                      remaining_turns=2, source_name='行迹·阵地'))
    state.log.append('  行迹·阵地: 全队DEF+20% (2回合)')


def _trace_bronya_team_dmg(u, state, **kw):
    """布洛妮娅行迹「军势」: 在场全队伤害+10%"""
    if u.char.id != 'bronya':
        return
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.DMG_BONUS_ALL += 0.10
    state.log.append('  行迹·军势: 全队伤害+10%')


def _eid_bronya_e1(u, state, **kw):
    """布洛妮娅E1: 战技50%概率回1SP"""
    import random
    if random.random() < 0.50:
        from engine.core.combat_engine import _gain_skill_points
        _gain_skill_points(state)
        state.log.append('  布洛妮娅E1: 战技回1SP')


def _eid_bronya_e2(u, state, **kw):
    """布洛妮娅E2: 战技目标行动后SPD+30% 1回合"""
    from engine.runtime import TimedBuff
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if target:
        tb = TimedBuff(source_id="bronya_e2", attributes={"SPD_PERCENT": 30.0},
                       remaining_turns=1, source_name="布洛妮娅E2·快速行军")
        target.buffs.append(tb)


def _eid_bronya_e4(u, state, target=None, skill_key=None, **kw):
    """布洛妮娅E4·攻其不备: 他角色对风弱点敌普攻后→布洛妮娅追加攻击(普攻伤害80%风伤, 每回合1次)"""
    from engine.core.combat_engine import _build_effective_stats, calculate_damage, _commit_enemy_damage
    from engine.runtime import _enemy_for_damage
    if u.char.id == 'bronya' or skill_key != 'basic_attack' or not target:
        return
    if getattr(target, 'element_res', {}).get('风', 0.2) > 0:
        return  # 非风弱点
    if state.extra.get('bronya_e4_used_turn', -1) == state.turn_count:
        return  # 每回合1次
    bronya = next((x for x in state.units if x.char.id == 'bronya' and x.is_alive), None)
    if not bronya:
        return
    state.extra['bronya_e4_used_turn'] = state.turn_count
    s = _build_effective_stats(bronya, state)
    d = calculate_damage(s, _enemy_for_damage(target), s.ATK, 80.0, "direct", "风", 80,
                         s.CRIT_RATE >= 0.5, crit_mode="expected", attack_type="follow_up")
    _commit_enemy_damage(state, bronya, target, d.final_damage)
    bronya.total_damage_dealt += d.final_damage
    state.log.append(f'  布洛妮娅E4: 追加攻击风伤 {d.final_damage:.0f}')
    # 大公4pc按追加攻击实际造成伤害的段数叠层；本次追加仅有一段。
    state.hooks.trigger_all("on_followup_hit", u=bronya, state=state)
    # v5.0.1: 光锥追加攻击事件（流光/影噬/谕示/火舞等）
    from engine.core.combat_engine import _process_lc_effects
    state.extra['lc_attack_targets'] = 1
    state.extra['lc_attack_target_refs'] = [target]
    state.extra['lc_attack_first_target_id'] = target.id
    _process_lc_effects(bronya, state, "on_followup")
    _process_lc_effects(bronya, state, "on_self_attack")  # 追加攻击也是攻击
    # 动作级追加攻击事件（千星/都蓝王朝——u=执行者=持有者）
    state.hooks.trigger_all("on_followup", u=bronya, state=state)
    # 每累计4次追加攻击（谎言终局·影噬）
    n = state.extra.get('lc_followup_count', 0) + 1
    state.extra['lc_followup_count'] = n
    if n % 4 == 0:
        _process_lc_effects(bronya, state, "on_followup_4th")


def _eid_bronya_e6(u, state, **kw):
    """布洛妮娅E6: 战技增伤效果+1回合（duration 在 _apply_skill_effects 内联）"""
    pass  # 引擎内联: bronya_skill_dmg_buff duration 1→2


def _tech_bronya(state, u, is_opener):
    """布洛妮娅: 全队攻击力+15% 2回合（布洛妮娅.txt 秘技·在旗帜下, 非进战）"""
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='bronya', attributes={'ATK_PERCENT': 15.0},
                                      remaining_turns=2, param_id='bronya_technique_atk'))
    state.log.append('[秘技] 在旗帜下: 全队攻击力+15% 2回合')


CHAR_ID = "bronya"
AI = bronya_ai
TECHNIQUE = _tech_bronya


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _bronya_ult_buff_mutator(u, state, attrs, skill):
    """EFFECT_MUTATORS['bronya_ult_buff']: 终结技CD部分（按自身暴伤动态）。"""
    cd_val = u.base_stats.CRIT_DMG * 0.16 + 0.20
    return {'ATK_PERCENT': 55.0, 'CRIT_DMG': round(cd_val * 100, 1)}, 2


def _bronya_skill_dmg_mutator(u, state, attrs, skill):
    """EFFECT_MUTATORS['bronya_skill_dmg_buff']: 战技增伤持续（E6: +1回合）。"""
    # 特殊持续时间（布洛妮娅E6: 战技增伤+1回合）
    return attrs, (2 if u.eidolon_rank >= 6 else 1)


EFFECT_MUTATORS = {'bronya_ult_buff': _bronya_ult_buff_mutator,
                   'bronya_skill_dmg_buff': _bronya_skill_dmg_mutator}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _bronya_crit_override(u, state, t, t_stats, skill_key):
    """PHASE crit_override: 行迹·号令——普攻必暴（→(t_stats|None, force_crit|None)）。"""
    # 布洛妮娅行迹·号令: 普攻必暴
    if skill_key == 'basic_attack':
        return (None, True)
    return None


PHASE_HOOKS['crit_override'] = _bronya_crit_override
