"""银狼·欢愉（试点 M3 自 combat_engine/elation/techniques/effect_resolver 迁入）"""

import copy
import random
from engine.runtime import _enemy_for_damage
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _use_skill
from engine.core.damage import calculate_damage


def _silver_wolf_apply_entry_effects(state):
    """银狼E1/E6：对当前波敌人施加入场领域效果。"""
    sw = next((x for x in state.units if x.char.id == 'yinlang'
               and x.is_alive), None)
    e1_active = bool(sw and sw.eidolon_rank >= 1 and sw.invincible_active)
    if e1_active:
        for enemy in state.enemies:
            enemy.extra['yinlang_e1_vuln'] = 0.20
    else:
        for enemy in state.enemies:
            enemy.extra.pop('yinlang_e1_vuln', None)
    if sw is None:
        return
    if sw.eidolon_rank >= 6:
        for enemy in state.enemies:
            if enemy.extra.get('silver_wolf_e6_entry_applied'):
                continue
            for elem, res in list(enemy.element_res.items()):
                enemy.element_res[elem] = -0.20 if res == 0.0 else 0.0
            enemy.extra['silver_wolf_e6_entry_applied'] = True
    if sw.eidolon_rank >= 1 or sw.eidolon_rank >= 6:
        fields = []
        if e1_active:
            fields.append('E1结界易伤')
        if sw.eidolon_rank >= 6:
            fields.append('E6禁限弱点')
        state.log.append(f'  银狼星魂: 当前波敌人入场效果已施加({"、".join(fields) or "无结界"})')


def _eid_yinlang_e1(u, state, **kw):
    """银狼E1: 结界内敌方受伤+20% + 退出无敌保留20%隐藏分"""
    _silver_wolf_apply_entry_effects(state)
    state.log.append('  银狼E1: 敌方受伤+20%, 退出无敌保留20%隐藏分')


def _eid_yinlang_e2(u, state, **kw):
    """银狼E2: 无敌内增益+1回合 + 每120隐藏分→额外回合+1强化普攻"""
    pass  # 在 elation.py 银狼逻辑中处理


def _eid_yinlang_e4(u, state, **kw):
    """银狼E4: 崩坏级欢愉伤害×5笑点"""
    pass  # 在 _silver_invincible_elation（崩坏级伤害演示）中处理


def _eid_yinlang_e6(u, state, **kw):
    """银狼E6: 强化普攻欢愉增笑50% + 禁限弱点"""
    _silver_wolf_apply_entry_effects(state)
    state.log.append('  银狼E6: 欢愉增笑+50%, 禁限弱点植入(全属性弱点+抗性归零/-20%)')


def _yl_ai(u, state, *, elation, max_av, navs, uidx, **__):
    if u.invincible_active:
        silver_enhanced_basic(u, state)
    elif u.hidden_score >= HS_ULT_COST:
        # 动态开大阈值: 开大后剩余HS需≥120才能吃满30%独立乘区
        # 开大消耗60, 行迹返还20, 光锥返还20(如有) → 阈值=60+120-20-光锥返还=160-光锥返还
        lc_refund = HS_LC_GAIN if not u.lc_ult_used else 0
        hs_threshold = HS_ULT_COST + 120 - HS_TALENT_GAIN - lc_refund  # 140 或 160
        remaining = max_av - state.current_av
        # 近结束时允许提前开大(不掉轴)
        can_early = remaining < 350 and u.hidden_score >= HS_ULT_COST + HS_TALENT_GAIN
        if u.hidden_score >= hs_threshold or can_early:
            silver_ult(u, state)
            navs[uidx] = state.current_av
        elif state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _tech_yinlang(state, u, is_opener):
    """银狼Lv.999秘技：开战后每个波次触发一次固定99笑点盲盒。"""
    if not is_opener:
        return
    state.extra['yinlang_tech_active'] = True
    silver_technique_wave(u, state)  # 银狼在场即欢愉队, 系统实例由函数内兜底
    state.log.append('[秘技] 朋友，这才是T0级秘技: 本波次盲盒已触发(固定99好活)')


def _laugh_gen(u, state, skill_key):
    """笑点生成（yinlang 形态: 仅战技+5, 同步隐藏分）"""
    is_tb_elation = (u.char.id == 'trailblazer_elation' and skill_key == 'elation_skill')
    if u.char.path != "欢愉" or (skill_key not in ("basic_attack", "skill") and not is_tb_elation):
        return
    if u.char.id == 'yinlang':
        laugh = 5 if skill_key == 'skill' else 0
        if laugh <= 0:
            return
        state.laugh_points += laugh
        elation = state.extra.get('_elation')
        if elation:
            elation.gain_hidden_score(state, u, laugh)
        else:
            u.hidden_score = min(300.0, u.hidden_score + laugh)


def _silver_invincible_elation(u, state, skill_key):
    """银狼无敌玩家欢愉技：6×90%弹射"""
    if u.char.id == "yinlang" and u.invincible_active and skill_key == "elation_skill":
        total = 0.0
        s = _build_effective_stats(u, state)
        for _ in range(6):
            alive = state.alive_enemies()
            if not alive:
                break
            t = random.choice(alive)
            laugh_n = u.hidden_score
            if u.eidolon_rank >= 4:
                laugh_n += state.laugh_points * 5.0
            d = calculate_damage(s, _enemy_for_damage(t), 0, 90.0, "elation",
                                 u.char.element, 80, s.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, crit_mode="expected")
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        u.total_damage_dealt += total
        skill = u.char.skills.get("elation_skill")
        u.damage_log.append(((skill.name + "(无敌)") if skill else "elation", total, "elation_inv"))
        state.log.append(f'[{state.current_av:6.0f}AV] {u.char.name} 欢愉技(无敌): {total:.0f}')
        u.extra['yinlang_blindbox_prob'] = 1.0
        state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 提前return欢愉技补气氛
        return True


CHAR_ID = "yinlang"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
SKILL_HOOKS = [_silver_invincible_elation, _laugh_gen]
AI = _yl_ai
TECHNIQUE = _tech_yinlang


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _yinlang_skill_gate(u, state, skill_key):
    """PHASE skill_gate: 银狼无敌玩家期间无法施放战技或终结技（True=中止）。"""
    if u.invincible_active and skill_key in ('skill', 'ultimate'):
        state.log.append('  [WARN] 银狼无敌玩家期间无法施放战技或终结技')
        return True
    return None


PHASE_HOOKS = {'skill_gate': _yinlang_skill_gate}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _yinlang_post_attack_elation(u, state, stats, st, attack_type, skill_key):
    """PHASE post_attack_elation: 持好活普攻/战技追加40%虚数欢愉伤害（→extra|None）。"""
    from engine.core.combat_engine import _target_attacker_stats
    from engine.runtime import _enemy_for_damage
    # 银狼Lv.999天赋：持有好活时，普攻/战技对实际受击且仍存活的目标
    # 追加40%虚数欢愉伤害；强化普攻在其专属路径中已直接改为欢愉伤害。
    if skill_key in ('basic_attack', 'skill') \
            and state.elation_state.get_good_show_total('yinlang') > 0:
        laugh_n = state.elation_state.get_good_show_total('yinlang')
        extra_total = 0.0
        for t in state.extra['last_attack_targets']:
            if t.HP <= 0:
                continue
            extra_stats = _target_attacker_stats(stats, u, state, t, st)
            extra = calculate_damage(
                extra_stats, _enemy_for_damage(t, st), 0.0, 40.0,
                'elation', u.char.element, 80,
                extra_stats.CRIT_RATE >= 0.5, laugh_n=laugh_n,
                skill_type=st, attack_type=attack_type,
                crit_mode='expected')
            _commit_enemy_damage(state, u, t, extra.final_damage)
            extra_total += extra.final_damage
        if extra_total > 0:
            state.log.append(f'  银狼持好活: {skill_key}追加40%欢愉伤害 {extra_total:.0f}')
        return extra_total
    return None


PHASE_HOOKS['post_attack_elation'] = _yinlang_post_attack_elation


# ---- v7.15.0: 银狼欢愉机制（原 elation silver_* 方法, verbatim 迁入）----

# 无敌玩家专属常量（原 elation 常量区迁入）
HS_ULT_COST = 60       # 终结技消耗隐藏分
HS_TALENT_GAIN = 20    # 天赋：进无敌+20HS
HS_LC_GAIN = 20        # 光锥：自释终结技+20HS
INVINCIBLE_MAX = 3     # 无敌玩家强化普攻次数


def _elation_sys(state):
    """欢愉系统实例（战斗内必有; 直调测试兜底新实例——系统无状态, 行为等价）。"""
    from engine.systems.elation import ElationSystem
    return state.extra.get('_elation') or ElationSystem()


def silver_ult(u, state):
    elation = _elation_sys(state)
    hs = u.hidden_score
    if hs < HS_ULT_COST:
        state.log.append(f'  [BUG] HS={hs:.0f}时开大被阻止')
        return
    if u.eidolon_rank >= 2:
        threshold = 120.0
        while threshold <= hs:
            state.extra.setdefault('extra_turns', []).append((u, 'yinlang_e2'))
            threshold += 120.0
        u.extra['yinlang_e2_next_threshold'] = threshold
    u.hidden_score = hs - HS_ULT_COST + HS_TALENT_GAIN
    u.invincible_active = True
    u.invincible_basics_done = 0
    u.extra['yinlang_blindbox_prob'] = 1.0
    _silver_wolf_apply_entry_effects(state)
    if u.eidolon_rank >= 2:
        for buff in u.buffs:
            if getattr(buff, 'remaining_turns', -1) >= 0:
                buff.remaining_turns += 1

    lc_bonus = 0
    if not u.lc_ult_used:
        state.laugh_points += HS_LC_GAIN
        elation.gain_hidden_score(state, u, HS_LC_GAIN)
        u.lc_ult_used = True
        lc_bonus = HS_LC_GAIN

    ha = u.hidden_score
    state.log.append(
        f'[{state.current_av:6.0f}AV] 银狼 无敌玩家启动! '
        f'HS={hs:.0f}->扣{HS_ULT_COST}->+天赋{HS_TALENT_GAIN}+LC{lc_bonus}={ha:.0f} '
        f'CR+{elation._hidden_score_cr(ha)*100:.1f}% '
        f'CD+{elation._hidden_score_cd(ha,u.base_stats.CRIT_RATE)*100:.1f}%')


def silver_blindbox(u, state, *, force=False, laugh_n_override=None):
    """Trigger Silver Wolf's good-show blindbox when a skill point is spent."""
    from engine.core.combat_engine import _gain_skill_points
    elation = _elation_sys(state)
    if not force and not u.invincible_active:
        return 0.0
    if not force and state.elation_state.get_good_show_total(u.char.id) <= 0:
        return 0.0
    probability = 1.0 if force else u.extra.get('yinlang_blindbox_prob', 1.0)
    if random.random() > probability:
        return 0.0
    if not force:
        u.extra['yinlang_blindbox_prob'] = probability * 0.20
    alive = state.alive_enemies()
    if not alive:
        return 0.0
    stats = elation.eff_stats(u, state)
    laugh_n = laugh_n_override if laugh_n_override is not None else u.hidden_score
    base_damage = sum(
        calculate_damage(stats, _enemy_for_damage(target), 0, 90.0, 'elation',
                         u.char.element, 80, stats.CRIT_RATE >= 0.5,
                         laugh_n=laugh_n, crit_mode='expected').final_damage
        for target in alive
    )
    total = 0.0
    if base_damage > 0:
        share = base_damage / len(alive)
        for target in list(alive):
            _commit_enemy_damage(state, u, target, share)
        total += base_damage
    effect_roll = random.random()
    if effect_roll < 0.33:
        target = max((target for target in alive if target.HP > 0),
                     key=lambda target: target.HP, default=None)
        if target is not None:
            extra = base_damage * 0.20
            _commit_enemy_damage(state, u, target, extra,
                                 damage_type='true_damage',
                                 record_cipher=False)
            total += extra
            effect = f'大剑(+{extra:.0f}真伤)'
        else:
            effect = '大剑(无存活目标)'
    elif effect_roll < 0.66:
        _gain_skill_points(state, 2)
        effect = '炸弹(+2SP)'
    else:
        state.laugh_points += 3
        elation.gain_hidden_score(state, u, 3)
        effect = '怪味豆(+3笑点)'
    u.total_damage_dealt += total
    next_probability = probability * 0.20 if not force else probability
    state.log.append(f'  银狼头号补给盲盒: {total:.0f} [{effect}] 概率{probability:.0%}->{next_probability:.0%}')
    return total


def silver_technique_wave(u, state):
    """秘技召唤物：每个波次开始固定触发一次盲盒，欢愉计数固定为99。"""
    if not u.is_alive:
        return 0.0
    return silver_blindbox(u, state, force=True, laugh_n_override=99.0)


def silver_enhanced_basic(u, state):
    from engine.core.combat_engine import _gain_skill_points
    elation = _elation_sys(state)
    s = elation.eff_stats(u, state)
    damage_mult = 1.0 + min(int(u.hidden_score / 60), 2) * 0.15
    s.DAMAGE_MULTIPLIER *= damage_mult
    if u.eidolon_rank >= 6:
        s.LAUGH_BOOST += 0.50
    td, hs = 0.0, u.hidden_score
    has_gs = state.elation_state.get_good_show_total(u.char.id) > 0
    is_crit = s.CRIT_RATE >= 0.5

    # 100 段弹射
    for _ in range(100):
        alive = state.alive_enemies()
        if not alive:
            break
        t = random.choice(alive)
        dmg_type = "elation" if has_gs else "direct"
        scaling = 0 if has_gs else s.ATK
        laugh_n = state.elation_state.get_good_show_total(u.char.id) if has_gs else 0
        d = calculate_damage(s, _enemy_for_damage(t), scaling, 2.4, dmg_type,
                             u.char.element, 80, is_crit,
                             laugh_n=laugh_n, crit_mode="expected")
        _commit_enemy_damage(state, u, t, d.final_damage)
        td += d.final_damage

    # 3 次盲盒：成功概率按上次成功后的20%递减，基础伤害由敌方全体均分。
    bb_dmg, bb_parts = 0.0, []
    alive = state.alive_enemies() or state.enemies
    for _ in range(3):
        probability = u.extra.get('yinlang_blindbox_prob', 1.0)
        if random.random() > probability:
            bb_parts.append('未触发盲盒')
            continue
        u.extra['yinlang_blindbox_prob'] = probability * 0.20
        bh = sum(calculate_damage(s, _enemy_for_damage(t), 0, 90.0, "elation",
                                  u.char.element, 80, is_crit,
                                  laugh_n=hs, crit_mode="expected").final_damage
                 for t in alive if t.HP > 0)
        bb_dmg += bh
        live_targets = [t for t in alive if t.HP > 0]
        if live_targets and bh > 0:
            share = bh / len(live_targets)
            for target in live_targets:
                _commit_enemy_damage(state, u, target, share)
        roll = random.random()
        if roll < 0.33:
            td += bh * 0.20
            if live_targets:
                sword_target = max(live_targets, key=lambda target: target.HP)
                _commit_enemy_damage(state, u, sword_target, bh * 0.20,
                                     damage_type='true_damage',
                                     record_cipher=False)
            bb_parts.append(f'大剑(+{bh*0.20:.0f}真伤)')
        elif roll < 0.66:
            _gain_skill_points(state, 2)
            bb_parts.append('炸弹(+2SP)')
        else:
            state.laugh_points += 3
            elation.gain_hidden_score(state, u, 3)
            hs = u.hidden_score
            bb_parts.append('怪味豆(+3笑点)')
    td += bb_dmg

    # 最后一击
    for t in (state.alive_enemies() or state.enemies):
        if t.HP <= 0:
            continue
        dmg_type = "elation" if has_gs else "direct"
        scaling = 0 if has_gs else s.ATK
        laugh_n = state.elation_state.get_good_show_total(u.char.id) if has_gs else 0
        d = calculate_damage(s, _enemy_for_damage(t), scaling, 100.0, dmg_type,
                             u.char.element, 80, is_crit,
                             laugh_n=laugh_n, crit_mode="expected")
        _commit_enemy_damage(state, u, t, d.final_damage)
        td += d.final_damage

    u.total_damage_dealt += td
    u.invincible_basics_done += 1
    n = u.invincible_basics_done
    u.damage_log.append((f"强化普攻#{n}", td, "enhanced_basic"))
    state.log.append(
        f'[{state.current_av:6.0f}AV] {u.char.name} 强化普攻#{n}: {td:.0f} '
        f'(HS={hs:.0f}, x{damage_mult:.2f}) 盲盒伤害={bb_dmg:.0f} [{",".join(bb_parts)}]')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=td > 0)

    if n >= INVINCIBLE_MAX:
        u.invincible_active = False
        u.invincible_basics_done = 0
        retained = u.hidden_score * 0.20 if u.eidolon_rank >= 1 else 0.0
        u.hidden_score = retained
        u.lc_ult_used = False
        u.extra['yinlang_blindbox_prob'] = 1.0
        u.extra.pop('yinlang_e2_next_threshold', None)
        _silver_wolf_apply_entry_effects(state)
        state.log.append(f'  退出无敌玩家，隐藏分保留{retained:.0f}，LC重置')


# ---- v7.15.0 相位: 阿哈/隐藏分/面板的银狼站点 ----

def _yl_aha_trace(u, state, n):
    """PHASE aha_trace: 阿哈时刻银狼特殊行迹（HS 按笑点档位+20/40）。"""
    from engine.systems.elation import (
        HS_TRACE_BONUS, HS_TRACE_THRESHOLD_HIGH, HS_TRACE_THRESHOLD_LOW)
    elation = _elation_sys(state)
    if n >= HS_TRACE_THRESHOLD_LOW:
        bonus = HS_TRACE_BONUS * (2 if n >= HS_TRACE_THRESHOLD_HIGH else 1)
        elation.gain_hidden_score(state, u, bonus)
        state.log.append(f'  银狼特殊行迹: HS+{bonus} (笑点={n:.0f})')
    return None


def _yl_aha_hs_gain(u, state, n):
    """PHASE aha_hs_gain: 阿哈结算银狼隐藏分+笑点数。"""
    _elation_sys(state).gain_hidden_score(state, u, n)
    return None


def _yl_hidden_score_e2(u, state, before):
    """PHASE hidden_score_e2: E2 120分阈值→额外回合+恢复强化普攻次数。"""
    if u.eidolon_rank < 2 or not u.invincible_active:
        return None
    threshold = u.extra.get('yinlang_e2_next_threshold', 120.0)
    while before < threshold <= u.hidden_score:
        state.extra.setdefault('extra_turns', []).append((u, 'yinlang_e2'))
        u.invincible_basics_done = max(0, u.invincible_basics_done - 1)
        threshold += 120.0
        state.log.append('  银狼E2: 隐藏分达到120阈值→额外回合+恢复强化普攻')
    u.extra['yinlang_e2_next_threshold'] = threshold
    return None


def _yl_eff_stats(u, state, s, effective_spd):
    """PHASE eff_stats_yinlang: 行迹1——有效速度160起欢愉度+50%，每超1点再+2%（上限100%）。"""
    if effective_spd >= 160:
        s.ELATION_LEVEL += min(1.0, 0.50 + (effective_spd - 160.0) * 0.02)
        return s
    return None


PHASE_HOOKS['aha_trace'] = _yl_aha_trace
PHASE_HOOKS['aha_hs_gain'] = _yl_aha_hs_gain
PHASE_HOOKS['hidden_score_e2'] = _yl_hidden_score_e2
PHASE_HOOKS['eff_stats_yinlang'] = _yl_eff_stats
