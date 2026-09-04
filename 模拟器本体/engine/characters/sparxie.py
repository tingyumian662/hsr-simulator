"""火花（试点 M3）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _deduct_skill_point_cost, _gain_energy, _gain_skill_points, _use_skill
from engine.core.damage import calculate_damage


def _sparxie_ult_elation_extra(state, u):
    """火花持好活当赏→终结技额外全体48%火属性欢愉伤害（txt 天赋:66, v6.8.1 补）"""
    laugh_n = state.elation_state.get_good_show_total('sparxie')
    if laugh_n <= 0:
        return
    stats = _build_effective_stats(u, state)
    total = 0.0
    for t in state.alive_enemies():
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 48.0,
                             'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, skill_type='ultimate',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    state.log.append(f'  火花持好活: 终结技追加48%欢愉伤害 {total:.0f}')


def _sparxie_enhanced_settle(state, u):
    """火花强化普攻追加结算:
    - 持好活: 天赋 40%主目标+20%相邻 欢愉伤害
    - 互动陷阱(消耗1次): 20%主目标+10%相邻 + 随机礼物
      (红红火火=2笑点2SP / 恍恍惚惚=1笑点) + 天赋每陷阱1次10%欢愉弹射"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    if not alive:
        return
    laugh_n = state.elation_state.get_good_show_total('sparxie')
    main = alive[0]
    adj = alive[1:min(3, len(alive))]
    total = 0.0
    # 持好活: 天赋 40%主+20%相邻
    if laugh_n > 0:
        for t, scale in [(main, 40.0)] + [(t, 20.0) for t in adj]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale,
                                 'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='basic',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
    # 互动陷阱（txt: 消耗战技点1; v6.7b: 补扣费, SP 不足则本次不发动、次数保留）
    traps = u.extra.get('sparxie_trap_uses', 0)
    if traps > 0 and _deduct_skill_point_cost(state, u, 1):
        u.extra['sparxie_trap_uses'] = traps - 1
        for t, scale in [(main, 20.0)] + [(t, 10.0) for t in adj]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # 随机礼物
        if random.random() < 0.5:
            state.laugh_points += 2
            _gain_skill_points(state, 2)
            state.log.append('  互动陷阱: 红红火火(+2笑点+2战技点)')
        else:
            state.laugh_points += 1
            state.log.append('  互动陷阱: 恍恍惚惚(+1笑点)')
        # 天赋: 每发动1次陷阱→强化普攻额外1次10%欢愉弹射
        if laugh_n > 0:
            alive_now = [e for e in alive if e.HP > 0]
            if not alive_now:
                alive_now = alive
            t = random.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 10.0,
                                 'elation', '火', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='basic',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
    if total > 0:
        u.total_damage_dealt += total
        state.log.append(f'  火花强化普攻追加: {total:.0f}')


def _eid_sparxie_e6(u, state, **kw):
    """火花E6: 全属性抗性穿透+20%（永久）; 欢愉技额外段数在 _bounce_hits 内联"""
    if u.char.id != 'sparxie':
        return
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='sparxie', attributes={'RES_PEN_ALL': 20.0},
                             remaining_turns=-1, source_name='火花E6'))


def _spx_ai(u, state, *, elation, **__):
    """火花 AI（v6.7）: 能量满→终结技; 直播连线激活→普攻(消耗连线触发强化普攻);
    SP>0→战技(开连线+陷阱); 否则普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif u.extra.get('sparxie_live'):
        _use_skill(u, state, "basic_attack")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _laugh_gen(u, state, skill_key):
    """笑点生成（通用形态: 3 + 角色加成 + 好活加成）"""
    is_tb_elation = (u.char.id == 'trailblazer_elation' and skill_key == 'elation_skill')
    if u.char.path != "欢愉" or (skill_key not in ("basic_attack", "skill") and not is_tb_elation):
        return
    bonus = {"yaoguang": 3, "trailblazer_elation": 3}.get(u.char.id, 0)
    if bonus and u.char.id == "trailblazer_elation":
        _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口
    if state.elation_state.get_good_show_total(u.char.id) > 0:
        bonus += 3
    laugh = 3 + bonus
    state.laugh_points += laugh


def _sparxie_skill_live(u, state, skill_key):
    """火花战技: 开启直播连线(下次普攻强化, 一次性) + 互动陷阱+1(上限20, v6.7)"""
    if u.char.id == "sparxie" and skill_key == "skill":
        u.extra['sparxie_live'] = True
        used = u.extra.get('sparxie_trap_uses', 0)
        u.extra['sparxie_trap_uses'] = min(20, used + 1)
        state.log.append(f'  火花战技: 直播连线开启(一次性) + 互动陷阱+1({min(20, used+1)}/20)')


def _sparxie_elation_burst(u, state, skill_key):
    """火花欢愉技: 额外+2爆点（v6.7, 弹射段数见 _bounce_hits E6）"""
    if u.char.id == "sparxie" and skill_key == "elation_skill":
        state.extra['sparxie_burst_points'] = \
            state.extra.get('sparxie_burst_points', 0.0) + 2
        state.log.append('  火花欢愉技: +2爆点')


CHAR_ID = "sparxie"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
SKILL_HOOKS = [_laugh_gen, _sparxie_skill_live, _sparxie_elation_burst]
AI = _spx_ai


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _sparxie_key_rewrite(u, state, skill_key):
    """PHASE key_rewrite: 直播连线一次性普攻强化（用户 2026-08-15 确认仅下次普攻强化）。"""
    # v6.7 火花: 直播连线（一次性, 用户 2026-08-15 确认仅下次普攻强化）
    if skill_key == 'basic_attack' and u.extra.get('sparxie_live'):
        u.extra.pop('sparxie_live', None)
        state.log.append('  直播连线: 普攻强化为【百花齐放，胜者独享！】')
        return 'basic_attack_enhanced'
    return None


def _sparxie_ult_skill_scale(u, state, skill):
    """PHASE ult_skill_scale: 终结技倍率=(0.6×欢愉度+50%)ATK + 2笑点 + 行迹1 + E4（→新skill）。"""
    # v6.7b: E4 先结算再取面板——"施放终结技时"欢愉度+36%应计入本次倍率
    if u.eidolon_rank >= 4:
        state.laugh_points += 5
        u.buffs.append(TimedBuff(source_id='sparxie', attributes={'ELATION_LEVEL': 36.0},
                                 remaining_turns=3, param_id='sparxie_e4_elation',
                                 source_name='火花E4·表情管理'))
        state.log.append('  火花E4: +5笑点 + 欢愉度+36%(3回合)')
    spx_stats = _build_effective_stats(u, state)
    skill = copy.deepcopy(skill)
    main = next((m for m in skill.multipliers
                 if m.target in ('all_enemies', None, '')), skill.multipliers[0])
    bonus = spx_stats.ELATION_LEVEL * 60.0  # 0.6×欢愉度(面板小数)×100
    main.scale = main.scale + bonus
    state.log.append(f'  火花终结技: 欢愉度{spx_stats.ELATION_LEVEL*100:.0f}%→倍率+{bonus:.1f}%')
    state.laugh_points += 2
    n_elation = sum(1 for x in state.units if x.char.path == "欢愉")
    extra_laugh, extra_burst = {1: (2, 1), 2: (4, 1), 3: (8, 4)}.get(n_elation, (0, 0))
    state.laugh_points += extra_laugh
    state.extra['sparxie_burst_points'] = \
        state.extra.get('sparxie_burst_points', 0.0) + extra_burst
    state.log.append(f'  火花终结技: +2笑点, 行迹1(欢愉{n_elation})额外+{extra_laugh}笑点+{extra_burst}爆点')
    return skill


def _sparxie_energy_gain_override(u, state, skill_key):
    """PHASE energy_gain_override: 战技无回能; 强化普攻回40（→新值|None）。"""
    # v6.7 火花: 战技无能量恢复（txt 尖叫！火花花连线中无回能行）;
    # 强化普攻【百花齐放】能量恢复40（txt）
    if skill_key == 'skill':
        return 0
    if skill_key == 'basic_attack_enhanced':
        return 40.0
    return None


PHASE_HOOKS = {'key_rewrite': _sparxie_key_rewrite,
               'ult_skill_scale': _sparxie_ult_skill_scale,
               'energy_gain_override': _sparxie_energy_gain_override}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _sparxie_goodshow_settle(u, state, skill_key, total_dmg):
    """PHASE goodshow_settle: 强化普攻互动陷阱结算 + 持好活终结技追加。"""
    # 火花强化普攻: 互动陷阱结算(20%/10%追加+礼物+天赋10%弹射) + 持好活40%/20%欢愉追加
    if skill_key == 'basic_attack_enhanced':
        _sparxie_enhanced_settle(state, u)
    # v6.8.1: 火花持好活当赏→终结技额外全体48%火属性欢愉伤害（txt 天赋:66, 此前整段缺失）
    if skill_key == 'ultimate' \
            and state.elation_state.get_good_show_total('sparxie') > 0 \
            and state.alive_enemies():
        _sparxie_ult_elation_extra(state, u)
    return None


PHASE_HOOKS['goodshow_settle'] = _sparxie_goodshow_settle


OBSERVER_HOOKS = {}


# ---- v7.15.0 相位: 火花欢愉站点（原 elation 内联, verbatim 迁入）----

def _spx_tech_init(_u, state):
    """OBSERVER init_sparxie_tech: 秘技（非进战·流量变现）——全敌50%ATK火伤+回2SP。"""
    spx = next((u for u in state.units if u.char.id == "sparxie"), None)
    if spx:
        stats = spx.base_stats
        for e in state.enemies:
            d = calculate_damage(stats, e, stats.ATK, 50.0, 'direct', '火', 80, False,
                                 crit_mode='expected')
            _commit_enemy_damage(state, spx, e, d.final_damage)
            spx.total_damage_dealt += d.final_damage
        _gain_skill_points(state, 2)
        state.log.append('[Init] 火花秘技: 全敌50%ATK火伤 + 回2战技点')
    return None


def _spx_aha_settle(_u, state, n):
    """OBSERVER aha_sparxie_settle: 阿哈时刻结束——E1 +5笑点; E2 +1额外回合+2爆点。"""
    # v6.7 火花星魂（阿哈时刻结束时触发）
    spx = next((x for x in state.units if x.char.id == 'sparxie' and x.is_alive), None)
    if spx:
        if spx.eidolon_rank >= 1:
            state.laugh_points += 5
            state.log.append('  火花E1: 阿哈时刻结束+5笑点')
        if spx.eidolon_rank >= 2:
            state.extra.setdefault('extra_turns', []).append((spx, 'sparxie_e2'))
            state.extra['sparxie_burst_points'] = \
                state.extra.get('sparxie_burst_points', 0.0) + 2
            state.log.append('  火花E2: 阿哈时刻结束+1额外回合+2爆点')
    return None


def _spx_eff_stats(u, state, s):
    """OBSERVER eff_stats_sparxie: 行迹2 每笑点全队暴伤+8%(上限10层)/E1 抗穿/行迹3 ATK。"""
    spx = next((x for x in state.units
                if x.char.id == 'sparxie' and x.is_alive), None)
    if spx:
        laugh_cap = min(int(state.laugh_points), 10)
        s.CRIT_DMG += 0.08 * laugh_cap  # 行迹2
        if spx.eidolon_rank >= 1:
            s.RES_PEN_ALL += 0.015 * laugh_cap  # E1
        if u.char.id == 'sparxie':
            extra_elation = 0.05 * min(max(int((s.ATK - 2000) / 100), 0), 16)
            if extra_elation > 0:
                s.ELATION_LEVEL += extra_elation  # 行迹3
        return s
    return None


OBSERVER_HOOKS['init_sparxie_tech'] = _spx_tech_init
OBSERVER_HOOKS['aha_sparxie_settle'] = _spx_aha_settle
OBSERVER_HOOKS['eff_stats_sparxie'] = _spx_eff_stats
