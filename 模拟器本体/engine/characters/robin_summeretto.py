"""知更鸟·晴歌（M4 批2b；气氛/Fever/三忆灵/特邀嘉宾/事件注册, AI 委托 remembrance）"""

import copy
import random
from engine.runtime import AV_PER_TURN, SimUnit, TimedBuff, _set_av, _stamp_av_key
from engine.core.combat_engine import _effective_spd
from engine.core.combat_engine import _ensure_marker_system
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _lc_rank_value
from engine.core.combat_engine import _skill_level_factor


def _qingge_find(state):
    """存活的知更鸟·晴歌"""
    return next((x for x in state.units
                 if x.char.id == 'robin_summeretto' and x.is_alive), None)


def _qingge_ms(state):
    """唯一的「晴空乐手」忆灵实体（贝茜/啾米/派丁是它的状态档位, v7.1.0 合一）"""
    return next((m for m in state.memsprites
                 if m.summoner_id == 'robin_summeretto' and m.is_alive), None)


def _qingge_members(state):
    """晴空乐手成员档位数: 1=贝茜, 2=+啾米, 3=+派丁(全员登台)"""
    ms = _qingge_ms(state)
    return ms.extra.get('qingge_members', 0) if ms is not None else 0


def _qingge_atmo_cap(qg):
    """气氛上限: 50, E2→70"""
    return 70.0 if qg.eidolon_rank >= 2 else 50.0


def _qingge_gain_atmo(state, gain, cause=None):
    """气氛统一入口: 上限截断 + 阈值召唤检查 + Fever检查 + 动态效果刷新"""
    qg = _qingge_find(state)
    if not qg or gain <= 0:
        return 0.0
    old = qg.extra.get('qingge_atmo', 0.0)
    new = min(_qingge_atmo_cap(qg), old + gain)
    qg.extra['qingge_atmo'] = new
    added = new - old
    if added > 0:
        state.log.append(f'  晴歌气氛+{added:.0f} → {new:.0f}/{_qingge_atmo_cap(qg):.0f}'
                         + (f' ({cause})' if cause else ''))
    _qingge_check_variant_spawn(state, qg)
    _qingge_check_fever(state, qg)
    if added > 0:
        _qingge_refresh_fever_effects(state)
    return added


def _qingge_check_variant_spawn(state, qg):
    """天赋: 晴空乐手(贝茜档)在场时, 气氛≥6→啾米登台; ≥12→派丁登台。
    v7.1.0 项目主澄清: 贝茜/啾米/派丁为状态档位——升档只改成员数并刷新易伤档,
    不触发新召唤（+20能量/on_memsprite_summon 仅实体首次被召唤时）。"""
    ms = _qingge_ms(state)
    if ms is None:
        return
    members = ms.extra.get('qingge_members', 0)
    atmo = qg.extra.get('qingge_atmo', 0.0)
    changed = False
    if members < 2 and atmo >= 6:
        members = 2
        changed = True
        state.log.append('  「晴空乐手」啾米登台 (成员2/3)')
    if members < 3 and atmo >= 12:
        members = 3
        changed = True
        state.log.append('  「晴空乐手」派丁登台 (成员3/3)')
    if changed:
        ms.extra['qingge_members'] = members
        _qingge_refresh_fever_effects(state)


def _qingge_check_fever(state, qg):
    """全员登台(成员档位3)→进入【Fever】"""
    if qg.extra.get('qingge_fever'):
        return
    if _qingge_members(state) < 3:
        return
    _qingge_enter_fever(state, qg)


def _qingge_enter_fever(state, qg):
    """全员登台: 解控 + 进入Fever + 展开结界 + 晴空乐手入行动条 + 倒计时入场;
    晴歌离开行动条(Fever结束前不进自己回合)。"""
    qg.extra['qingge_fever'] = True
    state.log.append('  全员登台! 进入【Fever】')
    ms = _qingge_ms(state)
    # 解控: 晴歌与晴空乐手（忆灵无 statuses, 由召唤者侧清控覆盖）
    for h in [qg] + ([ms] if ms is not None else []):
        if hasattr(h, 'statuses'):
            h.statuses = [s for s in h.statuses
                          if getattr(s, 'category', '') != 'control']
    # E4: 立即+12气氛
    if qg.eidolon_rank >= 4:
        _qingge_gain_atmo(state, 12.0, cause='E4')
    # E6: 本场第一次进Fever→回140能量
    if qg.eidolon_rank >= 6 and not qg.extra.get('qingge_e6_fever_energy'):
        qg.extra['qingge_e6_fever_energy'] = True
        _gain_energy(qg, 140.0, state=state)
        state.log.append('  晴歌E6: 首次进入Fever, 回140能量')
    # 晴歌离场: 从行动条摘除, 退出Fever时恢复
    navs = state.extra.get('navs', {})
    uidx = state.units.index(qg)
    qg.extra['qingge_suspended'] = navs.pop(uidx, None)
    # 晴空乐手入行动条(SPD激活) — v7.1.0 单实体一条
    if ms is not None:
        ms.runtime_spd = _qingge_ms_spd(state, qg, ms)
        ms.extra['next_av'] = state.current_av + AV_PER_TURN / max(ms.runtime_spd, 1.0)
        _stamp_av_key(state, ('ms', id(ms)))
        state.log.append(f'  「晴空乐手」登台行动 (SPD={ms.runtime_spd:.0f}, 成员3/3)')
    # 倒计时入场(140速)
    sys = _ensure_marker_system(state)
    if qg.marker and qg.marker.marker_id == 'qingge_countdown' and qg.marker.is_alive:
        sys.despawn(state, qg.marker)
    sys.spawn(state, qg, 'qingge_countdown')
    # 动态效果: 结界无视防御 + Fever伤害加成 + 成员数易伤
    _qingge_refresh_fever_effects(state)
    state.log.append('  Fever: 展开结界(我方伤害无视防御15%+气氛×0.5%), 晴歌&晴空乐手免疫控制')


def _qingge_exit_fever(state, qg):
    """气氛归零: 晴空乐手全部消失 + 退出Fever + 行动提前50%恢复行动条
    v7.0.0 B3: 行动提前基于摘除时快照(qingge_suspended)减半,
    max(current_av, susp-half)兜底; 消除死变量。"""
    qg.extra['qingge_fever'] = False
    state.log.append('  气氛归零: 退出【Fever】')
    rem = state.extra.get('_rem_sys')
    ms = _qingge_ms(state)
    if ms is not None:
        if rem is not None:
            rem.despawn_memsprite(state, qg, ms, reason='Fever结束')
        elif ms in state.memsprites:
            state.memsprites.remove(ms)
    qg.memsprite_unit = None
    # 忆灵天赋·乘上夏夜晚风: 晴歌行动提前50% + 恢复行动条
    navs = state.extra.get('navs', {})
    uidx = state.units.index(qg)
    susp = qg.extra.pop('qingge_suspended', None)
    half = AV_PER_TURN / max(_effective_spd(qg, state), 1.0) * 0.5
    if susp is not None:
        _set_av(state, navs, uidx, max(state.current_av, susp - half))
    else:
        _set_av(state, navs, uidx, state.current_av + half)
    # 倒计时退场
    sys = state.extra.get('_marker_sys')
    if sys and qg.marker and qg.marker.marker_id == 'qingge_countdown' \
            and qg.marker.is_alive:
        sys.despawn(state, qg.marker)
    # 动态效果回退（结界/伤害/易伤归零）
    _qingge_refresh_fever_effects(state)
    state.log.append('  乘上夏夜晚风: 晴歌行动提前50%')


def _qingge_countdown_action(state, marker):
    """Fever倒计时行动(140速): 扣50%气氛(至少12点); 气氛归零→散场"""
    qg = _qingge_find(state)
    sys = state.extra.get('_marker_sys')
    if qg is None or not qg.extra.get('qingge_fever'):
        # 晴歌阵亡/状态异常: 清理残留晴空乐手与倒计时
        rem = state.extra.get('_rem_sys')
        owner = next((x for x in state.units if x.char.id == 'robin_summeretto'), None)
        ms = _qingge_ms(state)
        if ms is not None and rem is not None and owner is not None:
            rem.despawn_memsprite(state, owner, ms, reason='晴歌离场')
        if sys:
            sys.despawn(state, marker)
        return
    # E6: 倒计时回合开始→回140能量
    if qg.eidolon_rank >= 6:
        _gain_energy(qg, 140.0, state=state)
        state.log.append('  晴歌E6: Fever倒计时回合开始, 回140能量')
    atmo = qg.extra.get('qingge_atmo', 0.0)
    deduct = min(atmo, max(int(atmo * 0.5), 12))
    qg.extra['qingge_atmo'] = atmo - deduct
    state.log.append(f'  Fever倒计时: 气氛-{deduct:.0f} → {qg.extra["qingge_atmo"]:.0f}')
    if qg.extra['qingge_atmo'] <= 0:
        _qingge_exit_fever(state, qg)
    else:
        _qingge_refresh_fever_effects(state)


def _qingge_ms_spd(state, qg, ms):
    """晴空乐手行动速度: 晴歌SPD×180%; E4 Fever期×(1+20%+气氛×0.5%)"""
    base = _effective_spd(qg, state) * 1.80
    if qg.eidolon_rank >= 4 and qg.extra.get('qingge_fever'):
        base *= 1.0 + 0.20 + qg.extra.get('qingge_atmo', 0.0) * 0.005
    return base


def _qingge_refresh_fever_effects(state):
    """动态刷新四组数值（先减旧值再加新值, 幂等）:
    1) 结界: 全队含忆灵 DEF_PEN = Fever? (15%+气氛×0.5%)×天赋factor : 0
    2) Fever伤害加成: 晴歌+晴空乐手 DMG_BONUS_ALL = Fever? (60%+气氛×2%)×忆灵天赋factor : 0 (Lv10)
    3) 成员数易伤: 全队含忆灵 VULNERABILITY_APPLIED = 8%/12%/16%×忆灵天赋factor (成员档位1/2/3, Lv10, 在场即生效)
    4) E4速度: Fever期晴空乐手 runtime_spd 跟随气氛
    v7.0.0 A3: E3天赋+2/忆灵天赋+1 → _skill_level_factor/boost 消费(每级+5%惯例)
    v7.1.0: 三忆灵合一——易伤档位按成员档位状态取值, 不再数实体数"""
    qg = _qingge_find(state)
    if not qg:
        return
    atmo = qg.extra.get('qingge_atmo', 0.0)
    fever = bool(qg.extra.get('qingge_fever'))
    ms = _qingge_ms(state)
    ms_list = [ms] if ms is not None else []
    team = [x for x in state.units if x.is_alive] + ms_list

    talent_factor = _skill_level_factor(qg, 'talent')
    ms_talent_factor = 1.0 + 0.05 * (qg.extra.get('skill_level_boost', {}) or {}).get(
        'memsprite_talent', 0)

    pen = ((0.15 + atmo * 0.005) * talent_factor) if fever else 0.0
    old_pen = state.extra.get('qingge_field_pen', 0.0)
    if abs(pen - old_pen) > 1e-9:
        for x in team:
            x.base_stats.DEF_PEN += pen - old_pen
        state.extra['qingge_field_pen'] = pen

    boost = ((0.60 + atmo * 0.02) * ms_talent_factor) if fever else 0.0
    for h in [qg] + ms_list:
        old = h.extra.get('qingge_dmg_boost', 0.0)
        if abs(boost - old) > 1e-9:
            h.base_stats.DMG_BONUS_ALL += boost - old
            h.extra['qingge_dmg_boost'] = boost

    vuln_map = {1: 0.08 * ms_talent_factor, 2: 0.12 * ms_talent_factor,
                3: 0.16 * ms_talent_factor}
    vuln = vuln_map.get(_qingge_members(state), 0.0)
    old_vuln = state.extra.get('qingge_presence_vuln', 0.0)
    if abs(vuln - old_vuln) > 1e-9:
        for x in team:
            x.base_stats.VULNERABILITY_APPLIED += vuln - old_vuln
        state.extra['qingge_presence_vuln'] = vuln

    if fever and ms is not None:
        ms.runtime_spd = _qingge_ms_spd(state, qg, ms)


def _qingge_atmo_from_action(state, cause):
    """其他单位行动使晴歌获得气氛后的统一附加:
    E2(任意目标回合内第一次施放技能使晴歌获得气氛→额外+2) + 律动消耗(行迹2) + 偏离和弦(行迹3)"""
    qg = _qingge_find(state)
    if qg is None:
        return
    first_this_turn = qg.extra.get('qingge_atmo_turn', -1) != state.turn_count
    qg.extra['qingge_atmo_turn'] = state.turn_count
    if first_this_turn and qg.eidolon_rank >= 2:
        _qingge_gain_atmo(state, 2.0, cause='E2额外')
    if first_this_turn:
        _qingge_rhythm_consume(state, qg)
    _qingge_trace3(state, qg, cause)


def _qingge_rhythm_consume(state, qg):
    """行迹2·即兴蓝调: 任意目标回合内第一次获得气氛时, 消耗1层律动→回3能量"""
    if not any(getattr(t, 'hook_name', '') == 'qingge_trace2_rhythm'
               for t in (qg.char.traces or [])):
        return
    if qg.extra.get('qingge_rhythm', 0) <= 0:
        return
    qg.extra['qingge_rhythm'] = qg.extra['qingge_rhythm'] - 1
    _gain_energy(qg, 3.0, state=state)
    state.log.append(f'  即兴蓝调: 消耗1层律动(剩{qg.extra["qingge_rhythm"]}层), 回3能量')


def _qingge_trace3(state, qg, cause):
    """行迹3·偏离和弦: 使我方目标获得气氛时——
    ATK>晴歌→ATK+晴歌HP×(16%+气氛×0.4%); 否则CD+40%+气氛×1.5% (2回合, 数值随当时气氛快照)"""
    if cause is None or cause is qg:
        return
    if not any(getattr(t, 'hook_name', '') == 'qingge_trace3_chord'
               for t in (qg.char.traces or [])):
        return
    atmo = qg.extra.get('qingge_atmo', 0.0)
    if cause.base_stats.ATK > qg.base_stats.ATK:
        amt = qg.base_stats.HP * (0.16 + atmo * 0.004)
        cause.buffs = [b for b in cause.buffs
                       if getattr(b, 'param_id', '') != 'qingge_chord_atk']
        cause.buffs.append(TimedBuff(source_id='robin_summeretto',
                                     attributes={'ATK': amt},
                                     remaining_turns=2, param_id='qingge_chord_atk',
                                     source_name='偏离和弦'))
        state.log.append(f'  偏离和弦: {cause.char.name} ATK+{amt:.0f} (2回合)')
    else:
        cd = 40.0 + atmo * 1.5
        cause.buffs = [b for b in cause.buffs
                       if getattr(b, 'param_id', '') != 'qingge_chord_cd']
        cause.buffs.append(TimedBuff(source_id='robin_summeretto',
                                     attributes={'CRIT_DMG': cd},
                                     remaining_turns=2, param_id='qingge_chord_cd',
                                     source_name='偏离和弦'))
        state.log.append(f'  偏离和弦: {cause.char.name} 暴伤+{cd:.1f}% (2回合)')


def _qingge_on_ally_attack(state, attacker, via_memsprite=False):
    """我方目标施放攻击结算后: 晴歌气氛+1;
    特邀嘉宾持有者及其召唤物攻击→额外+2 (attacker=召唤者, 忆灵攻击同入口);
    然后统一处理 E2/律动/偏离和弦(attacker≠晴歌)。
    v7.0.0 A4: 晴歌自己的忆灵施放忆灵技(via_memsprite=True, attacker=晴歌)时,
    按"我方目标(忆灵)施放技能使晴歌获得气氛"触发E2额外+2与律动消耗;
    行迹3目标=忆灵无增益意义(_qingge_trace3 对 cause=None 直接返回)。"""
    qg = _qingge_find(state)
    if qg is None:
        return
    _qingge_gain_atmo(state, 1.0, cause='攻击')
    if attacker is not None and attacker is not qg \
            and any(getattr(b, 'param_id', '') == 'qingge_guest' for b in attacker.buffs):
        _qingge_gain_atmo(state, 2.0, cause='特邀嘉宾')
    if attacker is not None and attacker is not qg:
        _qingge_atmo_from_action(state, attacker)
    elif via_memsprite:
        _qingge_atmo_from_action(state, None)


def _guest_advance_blocked(state, actor, target):
    """v7.1.0 特邀嘉宾防永动机规则(项目主澄清②): 持有【特邀嘉宾】的角色
    不得使**其他**友方获得行动提前; 自拉条放行(翔鹰4pc/各类自加速均不受影响)。"""
    if actor is None or target is None or target is actor:
        return False
    if not isinstance(actor, SimUnit):
        return False
    if any(getattr(b, 'param_id', '') == 'qingge_guest' for b in actor.buffs):
        state.log.append(f'  【特邀嘉宾】: {actor.char.name}无法使其他友方获得行动提前')
        return True
    return False


def _qingge_on_heal_shield(state, provider=None, targets=None):
    """渠道b + 行迹2·即兴蓝调（治疗侧 on_heal hook 与护盾侧 on_shield 内联共用）:
    队友提供的治疗/护盾作用于晴歌/晴空乐手→【律动】直接满12层(用户确认);
    任意目标回合内第一次提供治疗/护盾→晴歌气氛+1(治疗与护盾共享每回合去重)。"""
    qg = _qingge_find(state)
    if qg is None:
        return
    if provider is not None and provider is not qg:
        qg_ms = _qingge_ms(state)
        if any(t is qg or t is qg_ms for t in (targets or [])):
            qg.extra['qingge_rhythm'] = 12
            state.log.append('  即兴蓝调: 受队友治疗/护盾→律动12层')
    if qg.extra.get('qingge_heal_turn', -1) != state.turn_count:
        qg.extra['qingge_heal_turn'] = state.turn_count
        _qingge_gain_atmo(state, 1.0, cause='治疗/护盾')
        if provider is not None and provider is not qg:
            _qingge_atmo_from_action(state, provider)


def _qingge_ult_target(state, u):
    """终结技目标(用户确认规则): 姬子·启行队→姬子; 遐蝶风堇队→风堇;
    其他队伍暂按主C惯例(希儿)→第一个队友, 具体情况待用户细化。"""
    for cid in ('himeko_nova',):
        t = next((x for x in state.units if x.char.id == cid and x.is_alive), None)
        if t is not None:
            return t
    has_xiadie = any(x.char.id == 'xiadie' and x.is_alive for x in state.units)
    if has_xiadie:
        fj = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
        if fj is not None:
            return fj
    seele = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if seele is not None:
        return seele
    return next((x for x in state.units if x.is_alive and x is not u), u)


def _qingge_ultimate(state, u):
    """终结技·跃入这片蔚蓝狂想: 目标行动提前100% + 固定回20%能量上限 + 【特邀嘉宾】2回合
    v7.0.0 A1: 自身'能量恢复:5'经通用路径消费JSON effects(energy_regen, 见 _use_skill
    终结技分支)——与姬子·启行等26角色同模式, 此处不再内联回能(曾双重回能+10, GLM验收P1);
    v7.0.0 A3: 目标回能×_skill_level_factor(E5终结技+2→每级+5%)"""
    target = _qingge_ult_target(state, u)
    navs = state.extra.get('navs', {})
    t_idx = state.units.index(target) if target in state.units else -1
    if t_idx >= 0 and t_idx in navs:
        _set_av(state, navs, t_idx, state.current_av)  # 行动提前100%
        state.log.append(f'  晴歌终结技: {target.char.name}行动提前100%')
    # 固定恢复20%能量上限(不吃能量恢复效率)
    _gain_energy(target, (target.char.max_energy or 0) * 0.20
                 * _skill_level_factor(u, 'ultimate'), state=state,
                 apply_regen=False)
    target.buffs = [b for b in target.buffs
                    if getattr(b, 'param_id', '') != 'qingge_guest']
    target.buffs.append(TimedBuff(source_id='robin_summeretto', attributes={},
                                  remaining_turns=2, param_id='qingge_guest',
                                  source_name='特邀嘉宾'))
    state.log.append(f'  【特邀嘉宾】→ {target.char.name} (2回合: 攻击时晴歌气氛+2, 无法拉条队友)')


def _rise_and_sing_entry(state, u):
    """光锥[你将起身歌唱]: 进战行动提前(叠影档30-40%) + 【新声】2回合全队速度(叠影档20-40%)"""
    adv = _lc_rank_value(u, 0.30, code='rise_and_sing_advance')
    spd = _lc_rank_value(u, 0.20, code='rise_and_sing_spd')
    u.extra['initial_action_advance_ratio'] = max(
        u.extra.get('initial_action_advance_ratio', 0.0), adv)
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'rise_and_sing_newsound']
            eu.buffs.append(TimedBuff(source_id='rise_and_sing',
                                      attributes={'SPD_PERCENT': spd * 100.0},
                                      remaining_turns=2, param_id='rise_and_sing_newsound',
                                      source_name='新声'))
    state.log.append(f'  光锥[你将起身歌唱] 进战: 行动提前{adv * 100:.0f}% + 新声(全队速度+{spd * 100:.0f}%, 2回合)')


def _trace_qingge_cr(u, state, **kw):
    """晴歌行迹1·重构谐乐: 晴歌+晴空乐手CR+50%（晴空乐手侧在召唤时加, 见 remembrance._qingge_summon_variant）"""
    if u.char.id == 'robin_summeretto':
        u.base_stats.CRIT_RATE += 0.50
        state.log.append('  行迹·重构谐乐: 晴歌CR+50%')


def _trace_qingge_rhythm(u, state, healer=None, targets=None, **kw):
    """晴歌行迹2·即兴蓝调（治疗侧; 护盾侧由 combat_engine on_shield 内联调用同一逻辑）:
    队友提供的治疗作用于晴歌/晴空乐手→【律动】直接满12层;
    任意目标回合内第一次提供治疗/护盾→晴歌气氛+1（与护盾共享去重）。"""

    _qingge_on_heal_shield(state, provider=healer, targets=targets)


def _eid_qingge_e1_record(u, state, enemy=None, damage=0.0, damage_type='direct', **kw):
    """晴歌E1·夏日离群飞鸟: 「晴空乐手」记录我方目标造成的非真实伤害100%
    （忆灵技施放时消费→真伤, 见 remembrance._use_memsprite_skill_inner 晴歌分支）"""
    qg = next((x for x in state.units
               if x.char.id == 'robin_summeretto' and x.is_alive), None)
    if qg is None or qg.eidolon_rank < 1:
        return
    if damage_type in ('true', 'true_damage'):
        return  # 只记录非真实伤害
    qg.extra['qingge_record'] = qg.extra.get('qingge_record', 0.0) + max(damage, 0.0)


def _eid_qingge_e2(u, state, **kw):
    """晴歌E2·心似一片湖水: 全队全属性抗性穿透+18%（气氛上限+20 与回合首获气氛额外+2
    分别由 combat_engine._qingge_atmo_cap / _qingge_atmo_from_action 内联）"""
    if u.char.id != 'robin_summeretto' or u.eidolon_rank < 2:
        return
    if state.extra.get('qingge_e2_respen'):
        return
    state.extra['qingge_e2_respen'] = True
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.RES_PEN_ALL += 0.18
    state.log.append('  晴歌E2: 全队全属性抗性穿透+18%')


def _tech_qingge(state, u, is_opener):
    """知更鸟·晴歌: 开战行动提前20% + 立即6气氛 + 全队伤害+30% 2回合（进战·我们自成旋律）"""

    from engine.runtime import TimedBuff
    # 开战行动提前20%（navs 尚未创建, 由 initial_action_advance_ratio 暂存机制消费）
    u.extra['initial_action_advance_ratio'] = max(
        u.extra.get('initial_action_advance_ratio', 0.0), 0.20)
    _qingge_gain_atmo(state, 6.0, cause='秘技')
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'qingge_tech']
            eu.buffs.append(TimedBuff(source_id='robin_summeretto',
                                      attributes={'DMG_BONUS_ALL': 30.0},
                                      remaining_turns=2, param_id='qingge_tech',
                                      source_name='我们自成旋律'))
    state.log.append('[秘技] 我们自成旋律: 开战行动提前20% + 6气氛 + 全队伤害+30% 2回合')


def _on_attack_action(u, state, dealt=False, **ctx):
    """on_attack_action 事件处理器（原 _qingge_notify_attack 守卫语义）。"""
    if not dealt or u is None:
        return
    _qingge_on_ally_attack(state, u)


def _init_battle(state):
    state.hooks.register("robin_summeretto", "on_attack_action",
                         _on_attack_action, source_name="晴歌·我方攻击行动")


CHAR_ID = "robin_summeretto"
INIT = _init_battle
TECHNIQUE = _tech_qingge
MARKERS = {"qingge_countdown": _qingge_countdown_action}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _rs_ult_energy_override(u, state, skill):
    """PHASE ult_energy_override: E6 Fever期终结技扣140保留溢出（True=已自扣）。"""
    # v6.11.1 晴歌E6: Fever期终结技扣140保留溢出(储存2次语义)
    if u.eidolon_rank >= 6 and u.extra.get('qingge_fever'):
        u.current_energy = max(0.0, u.current_energy
                               - (skill.cost.get('energy') or u.char.max_energy or 0))
        return True
    return None


def _qingge_ult_inline(u, state, skill):
    """PHASE ult_inline: 晴歌终结技内联（拉条+回能+特邀嘉宾, 无伤害→True=完全处理）。"""
    from engine.core.combat_engine import _process_lc_effects, _ult_post
    from engine.characters.himeko_nova import _hn_count_ally_ult
    # v6.11.1 晴歌终结技: 拉条+回能+特邀嘉宾内联（无伤害, 跳过通用循环）
    _qingge_ultimate(state, u)
    _ult_post(state, u)
    _process_lc_effects(u, state, "on_ult")  # 补通用路径的光锥终结技事件
    _hn_count_ally_ult(state, u)  # v7.2.0 #6: 提前return前补裁决协议计数
    return True


PHASE_HOOKS = {'ult_energy_override': _rs_ult_energy_override,
               'ult_inline': _qingge_ult_inline}


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def qingge_ai(u, state, **kw):
    """晴歌AI: Fever期不进自己回合(行动条已摘除, 保险跳过); 满能量终结技由phase-1拦截;
    SP>0→战技(召唤贝茜/在场回血+气氛), SP=0→普攻。"""
    from engine.core.combat_engine import _use_skill
    if u.extra.get('qingge_fever'):
        return
    if state.skill_points > 0:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


AI = qingge_ai


# ---- v7.16.0: 晴空乐手召唤变体（原 remembrance 专属方法, verbatim 迁入）----

def _qingge_summon_variant(state, summoner, ms_data, name):
    """晴歌专用: 召唤/维护唯一「晴空乐手」忆灵实体。
    战技路径(贝茜档): 实体已在场→回血100%晴空乐手HP上限(Lv10)+晴歌气氛+6, 不重复召唤。
    首次召唤: 创建唯一实体(成员档位1)。
    v7.1.0 项目主澄清: 三只忆灵仅表示角色状态, 实机按一只忆灵计算——
    啾米/派丁登台是成员档位切换(combat_engine._qingge_check_variant_spawn), 不再创建新实体。"""
    import copy as _copy
    from engine.systems.remembrance import MemSpriteUnit
    existing = next((m for m in state.memsprites
                     if m.summoner_id == 'robin_summeretto' and m.is_alive), None)
    if existing is not None:
        t = existing
        # v7.0.0 A3: E3战技+2→Lv12 每级+5%惯例消费
        heal = t.max_hp * 1.0 * _skill_level_factor(summoner, 'skill')
        t.current_hp = min(t.max_hp, t.current_hp + heal)
        _qingge_gain_atmo(state, 6.0, cause='战技·晴空乐手已在场')
        state.log.append(f'  战技: {t.data.name}已在场→回血{heal:.0f} '
                         f'(HP={t.current_hp:.0f}/{t.max_hp:.0f}) + 晴歌气氛+6')
        return t
    data = _copy.deepcopy(ms_data)
    data.name = '晴空乐手'
    ms_stats = _copy.deepcopy(summoner.base_stats)
    ms_stats.HP = summoner.base_stats.HP * 0.70
    ms_stats.SPD = summoner.base_stats.SPD * 1.80
    ms_stats.CRIT_RATE += 0.50  # 行迹1·重构谐乐: 晴空乐手CR+50%
    ms_unit = MemSpriteUnit(
        data=data, summoner_id=summoner.char.id,
        max_hp=ms_stats.HP, current_hp=ms_stats.HP,
        base_stats=ms_stats,
    )
    ms_unit.current_energy = 0
    ms_unit.runtime_spd = 0.0  # Fever前不在行动条(界外), 进Fever时激活
    ms_unit.extra['qingge_members'] = 1  # 成员档位1=贝茜
    state.memsprites.append(ms_unit)
    summoner.memsprite_unit = ms_unit
    # 忆灵天赋·贴近海的心跳: 被召唤→晴歌+20能量
    _gain_energy(summoner, 20.0, state=state)
    state.log.append(f'  召唤「晴空乐手」贝茜 HP={ms_stats.HP:.0f} (晴歌HP×70%)'
                     f' + 贴近海的心跳: 晴歌+20能量')
    state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                            summoner=summoner, ms_unit=ms_unit)
    # 首次入场时已攒的气氛可能已达升档阈值→升档(可能直接全员登台进Fever)
    _qingge_check_variant_spawn(state, summoner)
    _qingge_check_fever(state, summoner)
    # 成员数易伤/Fever动态效果随档位变化刷新
    _qingge_refresh_fever_effects(state)
    return ms_unit


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _rs_ms_build(u, state, ms_data, hp_override):
    """PHASE ms_build: 晴歌专用召唤变体（贝茜/晴空乐手）。"""
    return _qingge_summon_variant(state, u, ms_data, '贝茜')


def _rs_ms_ai(u, state, ms_unit):
    """PHASE ms_ai: 回合开始施放忆灵技·叽叽啾啾四重奏。"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    if "memsprite_basic" in ms_unit.data.skills:
        rem._use_memsprite_skill(state, u, ms_unit, "memsprite_basic")
    return True


def _rs_ms_scale_mod(u, state, skill_key, scale):
    """PHASE ms_scale_mod: E5 忆灵技等级+1(每级+5%), E6 倍率×2。"""
    # v7.0.0 A3: E5改读 skill_level_boost 消除双重来源(解析器统一入口)
    if skill_key == 'memsprite_basic':
        ms_boost = 1.0 + 0.05 * (
            u.extra.get('skill_level_boost', {}) or {}).get(
            'memsprite_skill', 0)
        scale *= ms_boost
        if u.eidolon_rank >= 6:
            scale *= 2.0
        return scale
    return None


def _rs_ms_post_settle(u, state, skill_key):
    """PHASE ms_post_settle: 晴歌+20能量; E1 记录→真伤(HP最高存活敌, 记录减半)。"""
    from engine.core.combat_engine import _commit_enemy_damage
    if skill_key != 'memsprite_basic':
        return None
    _gain_energy(u, 20.0, state=state)
    state.log.append(f'  忆灵技: 晴歌+20能量 ({u.current_energy:.0f})')
    if u.eidolon_rank >= 1:
        record = u.extra.get('qingge_record', 0.0)
        # v7.1.0 P2: 目标取忆灵技AoE结算后的存活敌——结算前快照可能已全体阵亡,
        # 此时本次不触发(记录不减半), 不再对尸体提交真伤
        alive_now = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
        if record > 0 and alive_now:
            atmo = u.extra.get('qingge_atmo', 0.0)
            true_dmg = record * (0.11 + atmo * 0.001)
            hp_top = max(alive_now, key=lambda e: e.HP)
            _commit_enemy_damage(state, u, hp_top, true_dmg,
                                 damage_type='true_damage')
            u.extra['qingge_record'] = record * 0.50
            state.log.append(f'  晴歌E1: 真伤{true_dmg:.0f} → HP最高敌'
                             f'(记录{record:.0f}×11%+气氛{atmo:.0f}×0.1%), 记录减半')
    return None


PHASE_HOOKS['ms_build'] = _rs_ms_build
PHASE_HOOKS['ms_ai'] = _rs_ms_ai
PHASE_HOOKS['ms_scale_mod'] = _rs_ms_scale_mod
PHASE_HOOKS['ms_post_settle'] = _rs_ms_post_settle
