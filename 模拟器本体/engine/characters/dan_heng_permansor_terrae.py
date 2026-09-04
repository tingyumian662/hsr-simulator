"""dan_heng_permansor_terrae（M4 收官批迁入）"""

import copy
import random
from engine.runtime import _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _ensure_marker_system
from engine.core.combat_engine import _skill_level_factor
from engine.core.combat_engine import _use_skill


def _dht_apply_shield(state, u, amount_pct, flat, source):
    """全队护盾: 20%ATK+400（叠加上限=战技护盾量300%）"""
    stats = _build_effective_stats(u, state)
    source_skill = 'skill' if source == '渊渟岳峙' else 'talent'
    amt = ((stats.ATK * amount_pct / 100 + flat)
           * _skill_level_factor(u, source_skill))
    cap = ((stats.ATK * 0.20 + 400)
           * _skill_level_factor(u, 'skill'))  # 战技基础护盾为上限基准
    for eu in state.units:
        if eu.is_alive:
            eu.shield = min(cap * 3.0, getattr(eu, 'shield', 0.0) + amt)
    state.log.append(f'  {source}: 全队护盾+{amt:.0f} (上限{cap*3:.0f})')


def _dht_summon_longling(state, u, target):
    """【龙灵】召唤: 165速行动条标记（行动=解控+护盾10%ATK+200）
    v6.6c P1: 修复 spawn 签名（此前 4 参 TypeError/静默不召唤）"""
    if u.marker and u.marker.is_alive:
        return
    sys = _ensure_marker_system(state)
    sys.spawn(state, u, 'dht_longling')
    state.log.append('  召唤【龙灵】(165速)')


def _dht_longling_action(state, marker):
    """龙灵行动: 解控+护盾10%ATK+200; 强化→追加攻击80%ATK+同袍80%附加
    v6.6c P1: 签名对齐 action_handlers(state, marker); 净化修复（此前 [:] 空列表 no-op）"""
    u = next((x for x in state.units
              if x.char.id == marker.summoner_id and x.is_alive), None)
    if u is None:
        return
    _dht_apply_shield(state, u, 20.0 if u.eidolon_rank >= 2 else 10.0,
                      400 if u.eidolon_rank >= 2 else 200, '龙灵')
    stats = _build_effective_stats(u, state)
    # 行迹3：龙灵行动时给当前护盾最低的存活队友追加护盾。
    if any(getattr(t, 'hook_name', '') == 'dht_trace3' for t in (u.char.traces or [])):
        candidates = [x for x in state.units if x.is_alive]
        if candidates:
            target = min(candidates, key=lambda x: getattr(x, 'shield', 0.0))
            cap = stats.ATK * 0.20 + 400.0
            target.shield = min(cap * 3.0,
                                target.shield + stats.ATK * 0.05 + 100.0)
    # 净化我方负面（控制+负面状态）
    for eu in state.units:
        if eu.is_alive and eu.statuses:
            before = len(eu.statuses)
            eu.statuses = [s for s in eu.statuses
                           if getattr(s, 'category', '') not in ('control', 'debuff')]
            if len(eu.statuses) < before:
                state.log.append(f'  龙灵净化: {eu.char.name} 解除{before - len(eu.statuses)}个负面')
    # 终结技强化: 追加攻击80%ATK + 同袍80%附加（每次行动消耗1层）
    attacked = False  # v7.1.0 P1: marker行动是否构成攻击(供晴歌气氛触发)
    if u.extra.get('dht_longling_enhanced', 0) > 0:
        u.extra['dht_longling_enhanced'] -= 1
        alive = state.alive_enemies()
        total = 0.0
        tong = next((x for x in state.units
                     if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
        tong_stats = _build_effective_stats(tong, state) if tong else None
        for e in alive:
            d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK,
                                 80.0, 'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='skill', attack_type='follow_up',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, e, d.final_damage)
            total += d.final_damage
            if tong_stats is not None and e.HP > 0:
                attached_scale = 160.0 if u.eidolon_rank >= 2 else 80.0
                attached = calculate_damage(
                    tong_stats, _enemy_for_damage(e), tong_stats.ATK, attached_scale,
                    'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                    skill_type='skill', attack_type='follow_up', crit_mode='expected')
                _commit_enemy_damage(state, u, e, attached.final_damage)
                total += attached.final_damage
        # 行迹3：强化龙灵对当前生命最高的敌人追加同袍攻击力40%。
        highest = max((x for x in alive if x.HP > 0), key=lambda x: x.HP, default=None)
        has_trace3 = any(getattr(t, 'hook_name', '') == 'dht_trace3'
                         for t in (u.char.traces or []))
        if has_trace3 and highest is not None and tong_stats is not None:
            extra = calculate_damage(
                tong_stats, _enemy_for_damage(highest), tong_stats.ATK, 40.0,
                'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                skill_type='skill', attack_type='follow_up', crit_mode='expected')
            _commit_enemy_damage(state, u, highest, extra.final_damage)
            total += extra.final_damage
        u.total_damage_dealt += total
        state.log.append(f'  龙灵强化攻击: {total:.0f}(剩余强化{u.extra["dht_longling_enhanced"]}次)')
        attacked = attacked or total > 0
    # 献予「大地」之诗: 龙灵3次攻击附加同袍护盾80%伤害
    if u.extra.get('poem_dadi_attacks', 0) > 0:
        u.extra['poem_dadi_attacks'] -= 1
        tong = next((x for x in state.units
                     if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
        if tong and getattr(tong, 'shield', 0) > 0:
            dmg = tong.shield * 0.80
            t = next(iter(state.alive_enemies() or []), None)
            if t:
                _commit_enemy_damage(state, u, t, dmg)
                u.total_damage_dealt += dmg
                state.log.append(f'  献予「大地」: 龙灵附加{dmg:.0f}(同袍盾80%)')
                attacked = True
    state.log.append('  龙灵行动: 解控+护盾')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=attacked)  # v7.1.0 P1: marker行动攻击补气氛


def _trace_dht_trace2(u, state, **kw):
    """丹恒·腾荒行迹2: 战斗开始行动提前40%"""
    if u.char.id != 'dan_heng_permansor_terrae':
        return
    navs = state.extra.get('navs', {})
    i = state.units.index(u)
    if i in navs:
        remaining = max(0.0, navs[i] - state.current_av)
        navs[i] = state.current_av + remaining * 0.60
    else:
        u.extra['initial_action_advance_ratio'] = \
            u.extra.get('initial_action_advance_ratio', 0.0) + 0.40


def _tech_dht(state, u, is_opener):
    """丹恒·腾荒: 获得【同袍】+开战自动对同袍施放1次战技（非进战）
    v6.6c P2: 施放者是丹恒自己（此前误用同袍单位释放其战技）"""
    ally = min(state.units, key=lambda x: getattr(x, 'position', 99))
    if ally is not u:
        u.extra['dht_tongpao_id'] = ally.char.id
        ally.extra['dht_tongpao'] = True
        from engine.core.combat_engine import _use_skill
        _use_skill(u, state, 'skill')
    state.log.append('[秘技] 地坼: 获得【同袍】+自动战技')


CHAR_ID = "dan_heng_permansor_terrae"
TECHNIQUE = _tech_dht
MARKERS = {"dht_longling": _dht_longling_action}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _dht_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 战技同袍+全队护盾+龙灵; 终结技龙灵强化。"""
    from engine.runtime import TimedBuff
    from engine.core.combat_engine import _gain_skill_points, _pick_single_ally_target
    if u.char.id != 'dan_heng_permansor_terrae':
        return None
    # 战技: 同袍 + 全队护盾 + 龙灵
    if skill_key == 'skill':
        ally = _pick_single_ally_target(state, u)
        if ally:
            if u.extra.get('dht_tongpao_id') and u.extra['dht_tongpao_id'] != ally.char.id:
                ally2 = next((x for x in state.units if x.char.id == u.extra['dht_tongpao_id']), None)
                if ally2:
                    ally2.extra['dht_tongpao'] = False
            for enemy in state.enemies:
                enemy.extra.pop('dht_tongpao_vuln', None)
            u.extra['dht_tongpao_id'] = ally.char.id
            ally.extra['dht_tongpao'] = True
            if u.eidolon_rank >= 6:
                for enemy in state.enemies:
                    enemy.extra['dht_tongpao_vuln'] = 0.20
            _dht_apply_shield(state, u, 20.0, 400, '渊渟岳峙')
            _dht_summon_longling(state, u, ally)
    # 终结技: 龙灵强化
    if skill_key == 'ultimate':
        u.extra['dht_longling_enhanced'] = 2 + (2 if u.eidolon_rank >= 2 else 0)
        if u.eidolon_rank >= 1:
            _gain_skill_points(state, 1)
            tong = next((x for x in state.units
                         if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
            if tong:
                tong.buffs = [b for b in tong.buffs
                              if getattr(b, 'param_id', '') != 'dht_e1_respen']
                tong.buffs.append(TimedBuff(source_id='dht',
                                            attributes={'RES_PEN_ALL': 18.0},
                                            remaining_turns=3, param_id='dht_e1_respen',
                                            source_name='丹恒·腾荒E1'))
        if u.eidolon_rank >= 2:
            sys = state.extra.get('_marker_sys')
            if sys and u.marker:
                sys.advance(state, u, 1.0)
        state.log.append(f'  龙灵强化: {u.extra["dht_longling_enhanced"]}次行动')
        if u.eidolon_rank >= 6:
            tong = next((x for x in state.units
                         if x.char.id == u.extra.get('dht_tongpao_id') and x.is_alive), None)
            if tong:
                tong_stats = _build_effective_stats(tong, state)
                for target in list(state.alive_enemies()):
                    d = calculate_damage(
                        tong_stats, _enemy_for_damage(target), tong_stats.ATK, 330.0,
                        'direct', tong.char.element, 80, tong_stats.CRIT_RATE >= 0.5,
                        skill_type='ultimate', attack_type='follow_up',
                        crit_mode='expected')
                    _commit_enemy_damage(state, u, target, d.final_damage)
                    u.total_damage_dealt += d.final_damage
                state.log.append('  丹恒·腾荒E6: 同袍附加330%ATK群攻')
    return None


def _dht_settle_tongpao(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_tongpao: 同袍目标攻击后回能+标记推进（行迹2）。"""
    from engine.core.combat_engine import _gain_energy
    # 同袍行迹2: 同袍目标攻击→丹恒·腾荒回能+标记推进
    if total_dmg > 0 and u.extra.get('dht_tongpao'):
        dht = next((x for x in state.units
                    if x.char.id == 'dan_heng_permansor_terrae' and x.is_alive), None)
        if dht and any(getattr(t, 'hook_name', '') == 'dht_trace2'
                       for t in (dht.char.traces or [])):
            _gain_energy(dht, 6.0, state=state)
            marker_system = state.extra.get('_marker_sys')
            if marker_system and dht.marker:
                marker_system.advance(state, dht, 0.15)
    return None


SETTLE_HANDLERS = {'settle_self': _dht_settle_self,
                   'settle_tongpao': _dht_settle_tongpao}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_dadi(state, summoner, ms_unit, dht):
    """献予「大地」之诗(丹恒·腾荒): 龙灵3次攻击附加同袍护盾80%伤害; 同袍伤害+24%"""
    dht.extra['poem_dadi'] = True
    dht.extra['poem_dadi_attacks'] = 3
    state.log.append('  献予「大地」之诗: 龙灵附加+同袍伤害+24%')


POEM = ("大地", _poem_dadi, True)
