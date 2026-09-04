"""绯英（试点 M3）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _flat_toughness_with_break, _gain_energy, _use_skill
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus


def _evanescia_fox_teacher_fua(state, u):
    """绯英天赋·狐狸老师FUA: 全体100%ATK物理伤害+削韧10(含击破结算)+回10能量;
    持好活时全体追加25%物理欢愉伤害; 行迹1: 全敌易伤12% 3回合;
    E1: 额外触发1次欢愉技+10好活当赏。
    v6.7b: 主段改普通物理直伤（txt 主段是物理属性伤害, 25%欢愉是持好活追加段）。"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    laugh_n = state.elation_state.get_good_show_total('evanescia') \
        if state.extra.get('_elation') else 0
    total = 0.0
    for t in alive:
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 100.0,
                             'direct', '物理', 80, stats.CRIT_RATE >= 0.5,
                             attack_type='follow_up', crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, u, t, 10.0, '物理', 'talent', stats)
        # 持好活: 狐狸老师全体25%物理欢愉伤害（不打尸体）
        if laugh_n > 0 and t.HP > 0:
            before2 = t.HP
            d2 = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 25.0,
                                  'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                  laugh_n=laugh_n, attack_type='follow_up',
                                  crit_mode='expected')
            _commit_enemy_damage(state, u, t, d2.final_damage)
            total += d2.final_damage
    u.total_damage_dealt += total
    _gain_energy(u, 10.0, state=state)  # 回10能量（再触发互转, 正确闭环）
    # 行迹1·行裁断: 全敌易伤12% 3回合
    if any(getattr(tr, 'hook_name', '') == 'evanescia_trace1' for tr in (u.char.traces or [])):
        for t in alive:
            t.add_status(EnemyStatus(id='evanescia_vuln', name='行裁断',
                                     category='debuff', source='evanescia',
                                     remaining_turns=3,
                                     attributes={'vulnerability': 0.12}))
        state.log.append('  狐狸老师: 全敌易伤12% 3回合(行裁断)')
    # E1: 额外触发1次欢愉技 + 10好活当赏
    if u.eidolon_rank >= 1:
        _use_skill(u, state, 'elation_skill')
        elation = state.extra.get('_elation')
        if elation:
            elation.grant_good_show(state, 'evanescia', 10.0, source='evanescia_e1')
        state.log.append('  绯英E1: 额外欢愉技 + 10好活当赏')
    state.log.append(f'  狐狸老师FUA: {total:.0f} (100%ATK全体, 回10能量)')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _evanescia_goodshow_extra(state, u, skill_key):
    """绯英持好活当赏时的追加欢愉伤害（天赋）:
    - 战技: 对受击目标 16% 物理欢愉伤害
    - 终结技: 全体 23% + 随机目标 28%
    - 狐狸老师FUA全体25% 在 _evanescia_fox_teacher_fua 内处理（laugh_n 参与）"""
    if state.elation_state.get_good_show_total('evanescia') <= 0:
        return
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    if not alive:
        return
    # v6.7b: 终结技欢愉伤害至少计入等同于能量上限的好活当赏（txt 天赋）
    laugh_n = max(state.elation_state.get_good_show_total('evanescia'),
                  float(u.char.max_energy or 0))
    total = 0.0
    if skill_key == 'skill':
        # 战技: 对受到攻击的敌方目标（主目标+相邻）各16%欢愉伤害
        for t in alive[:3]:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 16.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='skill',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        state.log.append(f'  绯英持好活: 战技追加16%欢愉伤害 {total:.0f}')
    elif skill_key == 'ultimate':
        for t in alive:
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 23.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='ultimate',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        alive_now = [t for t in alive if t.HP > 0]
        if alive_now:
            t = random.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 28.0,
                                 'elation', '物理', 80, stats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, skill_type='ultimate',
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        state.log.append(f'  绯英持好活: 终结技追加23%全体+28%随机 {total:.0f}')
    u.total_damage_dealt += total


def _trace_evanescia_energy_convert(u, state, amount=0, **kw):
    """绯英天赋方向1: 获得能量→同步获得等值好活当赏（单次≤100）;
    累计240能量→狐狸老师FUA。锁内(好活→能量转化中)不重复转化。"""
    if u.char.id != 'evanescia' or state.extra.get('_eva_convert_lock'):
        return
    if not state.extra.get('_elation'):
        return
    elation = state.extra['_elation']
    amt = min(float(amount), 100.0)
    if amt > 0:
        elation.grant_good_show(state, 'evanescia', amt, duration=2,
                                source='evanescia_talent')
    # 240累计（FUA触发点, 单次获得最多记240）
    bank = u.extra.get('evanescia_energy_bank', 0.0) + float(amount)
    if bank >= 240.0:
        u.extra['evanescia_energy_bank'] = bank - 240.0
        _evanescia_fox_teacher_fua(state, u)
    else:
        u.extra['evanescia_energy_bank'] = bank


def _trace_evanescia_trace3_cr(u, state, **kw):
    """绯英行迹3·瞰众乐: 暴击率+30%（永久）; 弹射次数/好活转移由引擎内联"""
    if u.char.id != 'evanescia':
        return
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'CRIT_RATE': 30.0},
                             remaining_turns=-1, source_name='行迹·瞰众乐'))
    state.log.append('  绯英行迹3: 暴击率+30%(永久)')


def _trace_evanescia_talent_elation(u, state, **kw):
    """绯英天赋基础: 获得等同于暴击伤害20%的欢愉度（v6.7b 补, txt 天赋）"""
    if u.char.id != 'evanescia':
        return
    bonus = u.base_stats.CRIT_DMG * 0.20
    u.base_stats.ELATION_LEVEL += bonus
    state.log.append(f'  绯英天赋: 欢愉度+暴伤×20%({bonus*100:.0f}%)')


def _eid_evanescia_e1(u, state, **kw):
    """绯英E1: 全属性抗性穿透+20%（永久）; 狐狸老师额外欢愉技在 FUA 内联"""
    if u.char.id != 'evanescia':
        return
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'RES_PEN_ALL': 20.0},
                             remaining_turns=-1, source_name='绯英E1'))


def _eid_evanescia_e2(u, state, **kw):
    """绯英E2: 暴击伤害+36%（永久）; 好活获得×1.5/×2 在行迹2/3内联"""
    if u.char.id != 'evanescia':
        return
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'CRIT_DMG': 36.0},
                             remaining_turns=-1, source_name='绯英E2'))


def _eid_evanescia_e4(u, state, **kw):
    """绯英E4: 造成的伤害无视15%防御力"""
    if u.char.id != 'evanescia':
        return
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'DEF_PEN': 15.0},
                             remaining_turns=-1, source_name='绯英E4'))


def _eva_ai(u, state, *, elation, **__):
    """绯英 AI（v6.7）: 能量满→终结技; 好活≥240累计自动触发狐狸老师FUA(引擎hook);
    SP>0→战技(额外+10笑点); 否则普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _tech_evanescia(state, u, is_opener):
    from engine.runtime import _tech_enemies
    """绯英: 全敌100%ATK物理伤+20好活当赏（进战秘技, 绯英.txt 标"（进战）"）
    欢愉角色不入注册表防重复的规则仅限非进战 support（init_battle 无条件全开）;
    进战秘技=主动攻击开怪, v6.7b 落实开怪者门控: 非开怪者不生效。"""
    if not is_opener:
        return
    from engine.core.combat_engine import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '物理', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    elation = state.extra.get('_elation')
    if elation:
        elation.grant_good_show(state, 'evanescia', 20.0, source='technique')
    state.log.append('[秘技] 落英·散者皆忆: 全敌100%ATK物理伤 + 20好活当赏')


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


def _evanescia_skill_laugh(u, state, skill_key):
    """绯英战技: 额外+10笑点; 欢愉技: 额外+5好活当赏（v6.7b 补, txt 欢愉技）"""
    if u.char.id == "evanescia" and skill_key == "skill":
        state.laugh_points += 10
        state.log.append('  绯英战技: 额外+10笑点')
    elif u.char.id == "evanescia" and skill_key == "elation_skill":
        elation = state.extra.get('_elation')
        if elation:
            elation.grant_good_show(state, 'evanescia', 5.0,
                                    source='evanescia_elation_skill')
            state.log.append('  绯英欢愉技: 额外+5好活当赏')


CHAR_ID = "evanescia"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
SKILL_HOOKS = [_laugh_gen, _evanescia_skill_laugh]
AI = _eva_ai
TECHNIQUE = _tech_evanescia


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _evanescia_ult_cast_post(u, state):
    """PHASE ult_cast_post: E6 首终结技回120能量（每再施放4次触发1次）。"""
    # v6.7 绯英E6: 首终结技回120能量（每再施放4次触发1次）
    if u.eidolon_rank >= 6:
        cnt = u.extra.get('evanescia_ult_count', 0) + 1
        u.extra['evanescia_ult_count'] = cnt
        if cnt % 4 == 1:
            _gain_energy(u, 120.0, state=state)
            state.log.append(f'  绯英E6: 首终结技回120能量(第{cnt}次, 每4次触发)')
    return None


def _evanescia_energy_gain_override(u, state, skill_key):
    """PHASE energy_gain_override: 欢愉技能量恢复5（→新值|None, v6.7b 补）。"""
    # txt 欢愉技: 能量恢复5（v6.7b 补）
    if skill_key == 'elation_skill':
        return 5.0
    return None


PHASE_HOOKS = {'ult_cast_post': _evanescia_ult_cast_post,
               'energy_gain_override': _evanescia_energy_gain_override}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _evanescia_goodshow_settle(u, state, skill_key, total_dmg):
    """PHASE goodshow_settle: 持好活当赏——战技对受击目标16%物理欢愉伤等。"""
    # 绯英持好活当赏: 战技对受击目标16%物理欢愉伤害; 终结技全体23%+随机目标28%
    if total_dmg > 0 and state.alive_enemies():
        _evanescia_goodshow_extra(state, u, skill_key)
    return None


PHASE_HOOKS['goodshow_settle'] = _evanescia_goodshow_settle


OBSERVER_HOOKS = {}


# ---- v7.15.0 相位: 绯英好活转移/互转（原 elation 内联, verbatim 迁入）----

def _eva_goodshow(_u, state, char_id, amount, duration):
    """OBSERVER goodshow_eva: 瞰众乐转移 + 方向2 能量互转（→新duration|None）。"""
    from engine.core.combat_engine import _gain_energy
    from engine.systems.elation import ElationSystem
    elation = state.extra.get('_elation') or ElationSystem()  # 装配期实例未挂 state 前兜底
    target = next((x for x in state.units if x.char.id == char_id), None)
    eva = next((x for x in state.units if x.char.id == 'evanescia' and x.is_alive), None)
    # 行迹3·瞰众乐: 队友（参演编号<146 非绯英）获得好活 → 50%转绯英
    if (eva and target and char_id != 'evanescia'
            and (target.char.cast_number or 0) < 146):
        transfer = amount * 0.5
        if eva.eidolon_rank >= 2:
            transfer *= 1.5  # E2: 触发瞰众乐额外+50%
        elation.grant_good_show(state, 'evanescia', transfer, duration=duration,
                                source='evanescia_trace3')
        state.log.append(f'  绯英行迹3·瞰众乐: {char_id}好活{amount:.0f}→绯英+{transfer:.0f}')
    # 天赋方向2: 绯英获得好活→等值能量（单次≤100）
    if char_id == 'evanescia' and eva is not None \
            and not state.extra.get('_eva_convert_lock'):
        state.extra['_eva_convert_lock'] = True
        try:
            _gain_energy(eva, min(float(amount), 100.0), state=state)
        finally:
            state.extra['_eva_convert_lock'] = False
        # E6: 好活当赏持续时间+1回合
        if eva.eidolon_rank >= 6:
            duration += 1
            return duration
    return None


def _eva_goodshow_expire(unit, state, lost):
    """OBSERVER goodshow_expire: 队友好活到期50%（E2 ×2）转绯英；自身不触发。"""
    from engine.systems.elation import ElationSystem
    elation = state.extra.get('_elation') or ElationSystem()  # 直调测试兜底
    if unit.char.id == 'evanescia':
        return None
    eva = next((x for x in state.units
                if x.char.id == 'evanescia' and x.is_alive), None)
    if not eva:
        return None
    transfer = lost * 0.5
    if eva.eidolon_rank >= 2:
        transfer *= 2.0
    elation.grant_good_show(state, 'evanescia', transfer, duration=2,
                            source='evanescia_trace2')
    state.log.append(f'  绯英行迹2·开不败: {unit.char.id}好活到期{lost:.0f}→绯英+{transfer:.0f}')
    return None


OBSERVER_HOOKS['goodshow_eva'] = _eva_goodshow
OBSERVER_HOOKS['goodshow_expire'] = _eva_goodshow_expire
