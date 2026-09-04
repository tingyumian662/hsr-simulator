"""phainon（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points
from engine.core.combat_engine import _sweep_ults
from engine.core.combat_engine import _use_skill


def _phainon_gain_huozhong(state, u, amt):
    """白厄火种: 上限12+溢出3(E6无上限); 达到12激活终结技"""
    cap = 15 if u.eidolon_rank < 6 else 999
    u.extra['huozhong'] = min(cap, u.extra.get('huozhong', 0) + amt)
    state.log.append(f'  火种+{amt} → {u.extra["huozhong"]}/12')


def _phainon_gain_huishang(state, u, amt):
    """卡厄斯兰那毁伤"""
    u.extra['huishang'] = min(6, u.extra.get('huishang', 0) + amt)
    state.log.append(f'  毁伤+{amt} → {u.extra["huishang"]}/6')


def _phainon_trace3_atk_stack(state, u):
    """白厄行迹3·照见英雄本色: 进入战斗/变身结束时 ATK+50%, 最多叠加2层
    （v6.8.1: 归位——此前 v6.8b 误装到秘技, txt:99 行迹3）"""
    stacks = u.extra.get('phainon_atk_stacks', 0)
    if stacks >= 2:
        return
    u.extra['phainon_atk_stacks'] = stacks + 1
    u.buffs.append(TimedBuff(source_id='phainon',
                             attributes={'ATK_PERCENT': 50.0},
                             remaining_turns=-1, source_name='终结之始'))
    state.log.append(f'  白厄秘技: 攻击力+50% ({stacks + 1}/2层)')


def _apply_phainon_tech_wave(state, u):
    """白厄秘技·终结之始: 波次开始时全敌200%ATK物理伤害（v6.8b 补, txt 秘技; 仿流萤先例）"""
    stats = _build_effective_stats(u, state)
    total = 0.0
    for e in state.enemies:
        before = e.HP
        if before <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK, 200.0,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
        total += d.final_damage
    state.log.append(f'  白厄秘技: 本波次全敌200%ATK物理伤 {total:.0f}')


def _phainon_transform(state, u):
    """变身为卡厄斯兰那（用户 2026-08-14 精确语义）:
    8 额外回合均分于白厄回合周期——卡厄斯兰那速度=基础速度×0.6(E1 66%~84%),
    每回合间隔 = AV_PER_TURN/(基础×0.6)/8; 队友从进度条离开(非无法战斗, 被动/buff/忆灵照常);
    火种溢出直接计入下一次终结技(无延迟)。"""
    from engine.models.enemy import EnemyStatus
    base_spd = u.base_stats._base_SPD or u.base_stats.SPD
    ratio = 0.60
    if u.eidolon_rank >= 1:
        kills = state.extra.get('killed_total', 0)
        ratio = 0.66 + min(0.18, kills * 0.015)
    interval = AV_PER_TURN / max(base_spd * ratio, 1.0) / 8.0
    # 火种溢出（变身消耗12后的剩余, 退出时直接计入——统一在此扣12）
    cur = u.extra.get('huozhong', 0)
    u.extra['huozhong_overflow'] = max(0, cur - 12)
    u.extra['huozhong'] = min(cur, 12)
    u.extra['kasier'] = True
    u.extra['kasier_interval'] = interval
    u.extra['kasier_next_av'] = state.current_av  # 第1回合立即行动
    u.extra['kasier_turns'] = 8
    u.extra['kasier_done'] = 0
    # 队友离场: 角色从进度条离开(存原值恢复), 忆灵保留; 白厄自身也脱离常规排程
    navs = state.extra.get('navs', {})
    u.extra['kasier_ally_navs'] = {}
    for i, eu in enumerate(state.units):
        if i in navs:
            u.extra['kasier_ally_navs'][i] = navs.pop(i)
    _phainon_implant_phys_weak(state)
    state.log.append(f'  变身【卡厄斯兰那】: 8额外回合(间隔{interval:.0f}AV, 速度=基础×{ratio:.2f}) + 队友离场 + 敌物理弱点')


def _phainon_implant_phys_weak(state):
    """v6.6b P1-2: 变身期间所有敌人统一植入物理弱点（原抗性≤0 也降到 -0.2）;
    重复植入不覆盖快照; 退出变身按快照恢复, 波次重生时重植入。"""
    from engine.models.enemy import EnemyStatus
    for e in state.enemies:
        if any(s.id == 'phainon_phys_weak' for s in e.statuses):
            continue
        old_res = e.get_res('物理')
        e.element_res['物理'] = min(old_res, -0.2)
        e.add_status(EnemyStatus(id='phainon_phys_weak', name='物理弱点', category='debuff',
                                 source='phainon', remaining_turns=-1,
                                 attributes={'weakness_element': '物理', 'weakness_old_res': old_res}))


def _phainon_kasier_end(state, u):
    """退出变身: 恢复队友进度条 + 火种返还(溢出+行迹1的3点, 无延迟) + 清除弑魂/物理弱点"""
    u.extra['kasier'] = False
    navs = state.extra.get('navs', {})
    for i, av in (u.extra.pop('kasier_ally_navs', {}) or {}).items():
        navs[i] = av
    overflow = u.extra.pop('huozhong_overflow', 0)
    bonus = 3  # 行迹1: 变身结束+3火种
    # v6.6b P1-4: E6 火种无上限契约不得被 15 点截断
    cap = 15 if u.eidolon_rank < 6 else 10 ** 9
    u.extra['huozhong'] = min(cap, overflow + bonus)
    # v6.6b P1-2: 物理弱点按快照恢复并移除状态
    for e in state.enemies:
        st = next((s for s in e.statuses if s.id == 'phainon_phys_weak'), None)
        if st is not None:
            e.element_res['物理'] = st.attributes.get('weakness_old_res', e.get_res('物理'))
            e.remove_status('phainon_phys_weak')
    # v6.6b P1-7: 清除弑魂状态（反击/减伤）
    u.extra.pop('shihun_stacks', None)
    u.extra.pop('shihun_dr', None)
    u.buffs = [b for b in u.buffs if getattr(b, 'source_name', '') != '弑魂之炽减伤']
    # v6.8.1: 行迹3「变身结束时 ATK+50%」第二层（最多2层, 无条件——行迹3 非秘技）"""
    _phainon_trace3_atk_stack(state, u)
    state.log.append(f'  退出变身: 队友回归进度条 + 火种返还{overflow + bonus}(溢出{overflow}+行迹1的3) + 物理弱点/弑魂清除')


def _phainon_kasier_act(state, u):
    """卡厄斯兰那额外回合执行: 前7回合AI行动(毁伤≥4死星天裁/毁伤≥1弑魂焚诏/毁伤0普攻),
    第8回合=最后一击(960%ATK全体均分)并结束变身"""
    # v6.6b P1-6: 对齐额外回合生命周期（action_ctx/回合计数/sweep; X 轴不 tick 常规 Buff 不变）
    state.extra['action_ctx'] = 'extra'
    state.turn_count += 1
    done = u.extra.get('kasier_done', 0) + 1
    u.extra['kasier_done'] = done
    # v6.8.1: 额外回合开始时仍持【弑魂之炽】→立即反击并解除（txt:50, 此前缺失）
    if u.extra.get('shihun_stacks', 0) > 0:
        _phainon_shihun_counter(state, u, u.extra['shihun_stacks'])
        u.extra.pop('shihun_stacks', None)
        u.extra.pop('shihun_dr', None)
        u.buffs = [b for b in u.buffs if getattr(b, 'source_name', '') != '弑魂之炽减伤']
        state.log.append('  额外回合开始持弑魂→立即反击并解除')
    if done >= 8:
        stats = _build_effective_stats(u, state)
        # v6.6b P2-2: 无存活敌人则跳过伤害（此前回退打尸体）; 伤害按终结技分类（P2-1）
        alive = state.alive_enemies()
        total = 0.0
        if alive:
            for t in alive:
                before = t.HP
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 960.0 / len(alive),
                                     'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate',
                                     crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
        u.total_damage_dealt += total
        state.log.append(f'  最后一击: {total:.0f}(960%ATK均分)')
        state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 内联最后一击补气氛
        _phainon_kasier_end(state, u)
        _sweep_ults(state)
        return
    # AI: 毁伤≥4 死星天裁; 毁伤≥1 弑魂焚诏; 毁伤0 普攻
    hs = u.extra.get('huishang', 0)
    if hs >= 4:
        _use_skill(u, state, 'skill_shenshen')
    elif hs >= 1:
        _use_skill(u, state, 'skill_enhanced')
    else:
        _use_skill(u, state, 'basic_attack_enhanced')
    u.extra['kasier_next_av'] = u.extra.get('kasier_next_av', state.current_av)         + u.extra.get('kasier_interval', 20.0)
    _sweep_ults(state)  # v6.6b P1-6: 额外回合后统一 sweep


def _phainon_shihun_counter(state, u, stacks):
    """弑魂反击: 40%ATK全体 + 4×30%ATK弹射, 每层+20%"""
    stats = _build_effective_stats(u, state)
    mult = 40.0 * (1 + 0.20 * stacks)
    total = 0.0
    # v6.6b P2-2: 无存活敌人则跳过（此前回退打尸体）; 反击按战技+追加攻击分类（P2-1）
    alive = state.alive_enemies()
    for e in alive:
        before = e.HP
        d = calculate_damage(stats, _enemy_for_damage(e), stats.ATK, mult,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        total += d.final_damage
    for _ in range(4):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 30.0 * (1 + 0.20 * stacks),
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        # v6.8.1: 弹射段击杀统一口径（此前漏计数→白厄E1速度比例漏算）
    u.total_damage_dealt += total
    state.log.append(f'  弑魂反击: {total:.0f} ({stacks}层×20%)')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 反击路径补气氛


def _trace_phainon_trace1(u, state, **kw):
    """白厄行迹1: 开局+1火种"""
    if u.char.id != 'phainon':
        return

    _phainon_gain_huozhong(state, u, 1)


def _trace_phainon_trace3(u, state, **kw):
    """白厄行迹3: 进战ATK+50% 第1层（v6.8.1: 统一走叠层函数, 与变身结束第2层共享上限2）"""
    if u.char.id != 'phainon':
        return

    _phainon_trace3_atk_stack(state, u)
    state.log.append('  行迹·本色: 进战ATK+50%')


def _eid_phainon_e6(u, state, **kw):
    if u.char.id == 'phainon':

        _phainon_gain_huozhong(state, u, 6)


def _tech_phainon(state, u, is_opener):
    """白厄: 秘技点上限+3（txt: 白厄在队伍中时, 队伍效果不门控）+
    开怪者: 全队25能+2毁伤+1SP + 每波200%ATK物理伤（波次hook）。
    （进战秘技; v6.7b 裁决: 进战效果按开怪者门控——v6.8b 拆出队伍效果;
    v6.8.1: ATK+50%×2层归位行迹3, 不再由秘技叠加）"""
    state.max_sp += 3
    if not is_opener:
        state.log.append('[秘技] 终结之始: 秘技点上限+3')
        return
    from engine.core.combat_engine import (_gain_energy, _gain_skill_points)
    state.extra['phainon_tech_active'] = True
    for eu in state.units:
        if eu.is_alive:
            _gain_energy(eu, 25.0, state=state)
    _phainon_gain_huishang(state, u, 2)
    _gain_skill_points(state, 1)
    _apply_phainon_tech_wave(state, u)  # 首波 200%ATK
    state.log.append('[秘技] 终结之始: 全队25能+2毁伤+1SP + 首波200%ATK')


CHAR_ID = "phainon"
TECHNIQUE = _tech_phainon


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _phainon_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 白厄技能/终结技/卡形态/死星天裁状态机。"""
    from engine.runtime import _set_av
    if u.char.id != 'phainon':
        return None
    if skill_key == 'skill':
        _phainon_gain_huozhong(state, u, 2)
    if skill_key == 'ultimate':
        # 耗12火种变身（扣减由 _phainon_transform 统一处理）
        if u.extra.get('huozhong', 0) >= 12:
            _phainon_transform(state, u)
            if u.eidolon_rank >= 1:
                u.buffs = [b for b in u.buffs
                           if getattr(b, 'param_id', '') != 'phainon_e1_ult_cd']
                u.buffs.append(TimedBuff(
                    source_id='phainon', attributes={'CRIT_DMG': 50.0},
                    remaining_turns=3, param_id='phainon_e1_ult_cd',
                    source_name='白厄E1'))
        else:
            state.log.append(f'  [WARN] 火种不足({u.extra.get("huozhong", 0)}<12)')
    # 卡形态: 普攻/战技切换（skill_enhanced=弑魂焚诏, basic_attack_enhanced=血棘渡亡）
    if u.extra.get('kasier'):
        if skill_key in ('basic_attack', 'basic_attack_enhanced'):
            _phainon_gain_huishang(state, u, 2)
        if skill_key in ('skill', 'skill_enhanced'):
            # 弑魂焚诏: 毁伤+敌数 + 弑魂之炽1层(E4+4) + 敌方全体立即行动
            n_enemies = len(state.alive_enemies() or state.enemies)
            _phainon_gain_huishang(state, u, n_enemies)
            stacks = 1 + (4 if u.eidolon_rank >= 4 else 0)
            u.extra['shihun_stacks'] = stacks
            navs = state.extra.get('navs', {})
            for i, e in enumerate(state.enemies):
                _set_av(state, navs, ('e', i), state.current_av)  # 敌立即行动(后到先动)
            # v6.6b P1-1: 减伤走 TimedBuff 实际面板（此前 DMG_REDUCTION_TAKEN 无消费端且累加不还原）
            if not any(getattr(b, 'source_name', '') == '弑魂之炽减伤' for b in u.buffs):
                u.buffs.append(TimedBuff(source_id='phainon_shihun',
                                         attributes={'DMG_REDUCTION': 75.0},
                                         remaining_turns=-1, source_name='弑魂之炽减伤'))
            u.extra['shihun_dr'] = 0.75
            state.log.append(f'  弑魂焚诏: 毁伤+{n_enemies} 弑魂之炽+{stacks}层, 敌立即行动, 减伤75%')
        if skill_key == 'skill_shenshen':
            # 死星天裁: 耗毁伤≤4点, 每点4次45%ATK弹射
            spent = min(u.extra.get('huishang', 0), 4)
            u.extra['huishang'] = u.extra.get('huishang', 0) - spent
            stats = _build_effective_stats(u, state)
            total = 0.0
            # v6.6b P2-2: 无存活敌人跳过; 弹射每段重选存活目标（同 v6.2.1b P1-1 口径）
            alive = state.alive_enemies()
            for _ in range(spent * 4):
                alive_now = [e for e in alive if e.HP > 0]
                if not alive_now:
                    break
                t = random.choice(alive_now)
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 45.0,
                                     'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='skill',
                                     crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
            if spent >= 4 and alive:
                for t in alive:
                    if t.HP <= 0:
                        continue
                    d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 450.0 / len(alive),
                                         'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                                         skill_type='skill',
                                         crit_mode='expected')
                    _commit_enemy_damage(state, u, t, d.final_damage)
                    total += d.final_damage
            if u.eidolon_rank >= 6 and spent > 0:
                highest = max(state.alive_enemies(), key=lambda x: x.HP, default=None)
                if highest is not None:
                    true_dmg = total * 0.36
                    _commit_enemy_damage(state, u, highest, true_dmg,
                                         damage_type='true_damage',
                                         record_cipher=False)
                    total += true_dmg
                    state.log.append(f'  白厄E6: 死星天裁后真伤{true_dmg:.0f}')
            if u.eidolon_rank >= 2 and spent >= 4:
                state.extra.setdefault('extra_turns', []).append((u, 'extra'))
                state.log.append('  白厄E2: 消耗4点毁伤获得额外回合')
            u.total_damage_dealt += total
            state.log.append(f'  死星天裁: {total:.0f} (耗毁伤{spent})')
            state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 0倍率技能补气氛
    return None


def _phainon_settle_named(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_named: 白厄天赋——被点名+1火种（队友点名+暴伤30% 3回合）。"""
    from engine.core.combat_engine import _pick_single_ally_target
    # 白厄天赋: 被点名+1火种（队友点名+暴伤30% 3回合）
    # v6.8.1: 判定白厄是否为技能目标（此前任意队友普攻/战技都触发）;
    # v6.8.2: 覆盖 all_allies_but_self 与全队终结技（txt「成为技能目标时」）;
    # 暴伤改 TimedBuff 刷新（此前裸改 base_stats 永久叠加）
    if u.char.id == 'phainon' or skill_key not in ('skill', 'basic_attack', 'ultimate') \
            or getattr(skill, 'target', '') not in ('single_ally', 'ally', 'all_allies',
                                                    'all_allies_but_self', 'all'):
        return None
    ph = next((x for x in state.units if x.char.id == 'phainon' and x.is_alive), None)
    if ph:
        hit_ph = True
        if getattr(skill, 'target', '') in ('single_ally', 'ally'):
            ally = _pick_single_ally_target(state, u)
            hit_ph = ally is not None and ally.char.id == 'phainon'
        if hit_ph:
            _phainon_gain_huozhong(state, ph, 1)
            ph.buffs = [b for b in ph.buffs
                        if getattr(b, 'param_id', '') != 'phainon_cd_buff']
            ph.buffs.append(TimedBuff(source_id='phainon', attributes={'CRIT_DMG': 30.0},
                                      remaining_turns=3, param_id='phainon_cd_buff',
                                      source_name='被点名'))
            state.log.append(f'  白厄天赋: 被{u.char.name}点名 → +1火种 +暴伤30%(3回合)')
    return None


SETTLE_HANDLERS = {'settle_self': _phainon_settle_self,
                   'settle_named': _phainon_settle_named}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_fushi(state, summoner, ms_unit, phainon):
    """献予「负世」之诗(整场, 白厄): 火种+6 + 变身时永续燃烧(暴伤+72%上限/CR+16%/毁伤+4)"""
    _phainon_gain_huozhong(state, phainon, 6)
    phainon.extra['poem_fushi'] = True
    _phainon_gain_huishang(state, phainon, 4)
    phainon.base_stats.CRIT_DMG += 0.72
    phainon.base_stats.CRIT_RATE += 0.16
    state.log.append('  献予「负世」之诗: 火种+6+毁伤+4+永续燃烧(暴伤72%/CR16%)')


POEM = ("负世", _poem_fushi, True)
