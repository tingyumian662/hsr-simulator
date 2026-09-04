"""the_dahlia（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage, _set_av
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _effective_spd
from engine.core.combat_engine import _flat_toughness_with_break
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _gain_skill_points


def _dahlia_field_active(state) -> bool:
    """大丽花结界激活判断（战技/秘技共用 dahlia_field_turns, 领域互斥天然满足）"""
    return state.extra.get('dahlia_field_turns', 0) > 0


def _dahlia_ensure_dancers(state):
    """共舞者维持（txt 天赋）: 每当场上不存在另一位共舞者（死亡/未绑定）,
    使自身与击破特攻最高队友共同成为共舞者。"""
    dahlia = next((x for x in state.units if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    dancers = state.extra.get('dahlia_dancers', [])
    partner_ids = [cid for cid in dancers if cid != 'the_dahlia']
    if partner_ids and any(x.char.id == cid and x.is_alive
                           for cid in partner_ids for x in state.units):
        return
    others = [x for x in state.units if x.is_alive and x.char.id != 'the_dahlia']
    if not others:
        state.extra['dahlia_dancers'] = ['the_dahlia']
        return

    def _be(x):
        try:
            return _build_effective_stats(x, state).BREAK_EFFECT
        except Exception:
            return 0.0
    partner = max(others, key=lambda x: (_be(x), -getattr(x, 'position', 99)))
    state.extra['dahlia_dancers'] = ['the_dahlia', partner.char.id]
    state.log.append(f'  大丽花天赋: 共舞者重绑={partner.char.name}')


def _dahlia_super_break_rate(state, u, t) -> float:
    """大丽花超击破转化率源（v6.7, 与 _super_break_rate 线性求和）:
    - 天赋: 共舞者攻击破韧目标 → 60%（满级档 30%/60%）
    - 结界: 未破韧目标削韧也能转化 → 60%（用户 2026-08-15 确认与天赋同率）
    - E1: 超击破倍率全队生效（非共舞者 +0.6; 共舞者再 +0.4 合计 1.0）"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return 0.0
    rate = 0.0
    dancers = state.extra.get('dahlia_dancers', [])
    is_dancer = u.char.id in dancers
    if is_dancer and t.is_broken:
        rate += 0.6  # 天赋: 共舞者攻击破韧目标
    if _dahlia_field_active(state) and not t.is_broken:
        rate += 0.6  # 结界: 未破韧目标
    # v6.7b: E1 只放大"天赋超击破"(攻击破韧目标)——未破韧结界转化不叠加
    if dahlia.eidolon_rank >= 1 and t.is_broken:
        rate += 0.6 if not is_dancer else 0.4  # E1: 全队生效, 共舞者合计1.0
    return rate


def _dahlia_talent_open(state):
    """大丽花天赋·谁在害怕康士坦丝?（simulate 初始化调用）:
    开战回35能量 + 自身与击破特攻最高队友成为【共舞者】"""
    dahlia = next((x for x in state.units if x.char.id == 'the_dahlia'), None)
    if dahlia is None:
        return
    _gain_energy(dahlia, 35.0, state=state)
    others = [x for x in state.units if x.is_alive and x.char.id != 'the_dahlia']
    if not others:
        state.extra['dahlia_dancers'] = ['the_dahlia']
        return
    # 击破特攻最高（tiebreak 按站位靠前）
    def _be(x):
        try:
            return _build_effective_stats(x, state).BREAK_EFFECT
        except Exception:
            return 0.0
    partner = max(others, key=lambda x: (_be(x), -getattr(x, 'position', 99)))
    state.extra['dahlia_dancers'] = ['the_dahlia', partner.char.id]
    state.log.append(f'  大丽花天赋: 回35能量; 共舞者={dahlia.char.name}+{partner.char.name}')


def _dahlia_field_apply(state, u):
    """大丽花战技/秘技结界: 3回合 + 全队弱点击破效率+50%
    v6.7b: 重复施放先移除旧 buff（防 +50% 叠加漂移, 同 v6.6c 缇宝结界口径）"""
    state.extra['dahlia_field_turns'] = 3
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'dahlia_field_buff']
            eu.buffs.append(TimedBuff(source_id='the_dahlia',
                                      attributes={'TOUGHNESS_EFFICIENCY': 50.0},
                                      remaining_turns=3, param_id='dahlia_field_buff',
                                      source_name='大丽花结界'))
    state.log.append('  大丽花结界: 开启(3回合), 全队弱点击破效率+50%')


def _dahlia_fua(state):
    """大丽花天赋追加攻击: 5次×30%ATK随机单体(每段削韧3, 含击破结算), 命中破韧目标→本次削韧值(3)转200%超击破
    E4: 段数5→10 + 每次击中目标受伤+12% 2回合; E6: 共舞者行动提前20%;
    天赋能量恢复2; 行迹2: 每施放2次FUA回1战技点。
    v6.7b: 每段重新选择存活目标(清场回退); 击杀统一口径; 超击破段不打尸体。"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    stats = _build_effective_stats(dahlia, state)
    hits = 10 if dahlia.eidolon_rank >= 4 else 5
    total = 0.0
    for _ in range(hits):
        alive = state.alive_enemies()
        if not alive:
            break
        t = random.choice(alive)
        # 天赋削韧值3（含击破结算; 本次削韧刚击破的目标同样满足"处于弱点击破状态"）
        _flat_toughness_with_break(state, dahlia, t, 3.0, '火', 'talent', stats)
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, 30.0,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='talent', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, dahlia, t, d.final_damage)
        total += d.final_damage
        # E4: 每次击中目标受伤+12% 2回合
        if dahlia.eidolon_rank >= 4 and t.HP > 0:
            t.add_status(EnemyStatus(id='dahlia_e4_vuln', name='花蕊蚀伤',
                                     category='debuff', source='the_dahlia',
                                     remaining_turns=2,
                                     attributes={'vulnerability': 0.12}))
        # 天赋: 对破韧目标造成伤害后→本次削韧值(3)转1次200%超击破（满级 100%/200%; 不打尸体）
        if t.is_broken and t.HP > 0:
            sb = calculate_damage(stats, _enemy_for_damage(t), 0, 0, 'super_break',
                                  '火', 80, False, toughness_dmg=3.0)
            sb.final_damage *= 2.0
            _commit_enemy_damage(state, dahlia, t, sb.final_damage)
            total += sb.final_damage
    dahlia.total_damage_dealt += total
    dahlia.damage_log.append(('天赋追加攻击', total, 'follow_up'))
    _gain_energy(dahlia, 2.0, state=state)  # txt 天赋: 能量恢复2
    # 行迹2·致哀故人: 施放FUA回1战技点, 每施放2次触发1次
    if any(getattr(tr, 'hook_name', '') == 'the_dahlia_trace2' for tr in (dahlia.char.traces or [])):
        cnt = dahlia.extra.get('dahlia_fua_count', 0) + 1
        dahlia.extra['dahlia_fua_count'] = cnt
        if cnt % 2 == 0:
            _gain_skill_points(state, 1)
            state.log.append('  大丽花行迹2: 第2次FUA→回1战技点')
    state.log.append(f'  大丽花天赋FUA: {total:.0f} ({hits}次×30%, 回2能量)')
    # E6: 施放天赋追加攻击时所有共舞者行动提前20%
    if dahlia.eidolon_rank >= 6:
        navs = state.extra.get('navs', {})
        for cid in state.extra.get('dahlia_dancers', []):
            partner = next((x for x in state.units if x.char.id == cid and x.is_alive), None)
            from engine.characters.robin_summeretto import _guest_advance_blocked
            if partner and not _guest_advance_blocked(state, dahlia, partner):
                pidx = state.units.index(partner)
                if pidx in navs:
                    adv = (AV_PER_TURN / _effective_spd(partner, state)) * 0.20
                    _set_av(state, navs, pidx, max(0, navs[pidx] - adv))
        state.log.append('  大丽花E6: 共舞者行动提前20%')
    state.hooks.trigger_all("on_attack_action", u=dahlia, state=state, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _dahlia_e1_flat(state, dahlia):
    """大丽花E1: 共舞者(含大丽花)施放攻击后, 对受到攻击的敌方目标25%韧性上限固定削韧
    （≥10≤300, 每目标1次; v6.7b: 限定受击目标, 此前误对全体存活敌施加）"""
    if dahlia.eidolon_rank < 1 or not state.alive_enemies():
        return
    targets = [t for t in (state.extra.get('last_attack_targets') or []) if t.HP > 0]
    if not targets:
        return
    for t in targets:
        if t.extra.get('dahlia_e1_flat_used'):
            continue
        flat = min(max(0.25 * t.max_toughness, 10.0), 300.0)
        t.extra['dahlia_e1_flat_used'] = True
        if t.toughness > 0 and not t.is_broken:
            _flat_toughness_with_break(state, dahlia, t, flat, '火', 'talent')
            state.log.append(f'  大丽花E1: 固定削韧{flat:.0f}')


def _dahlia_on_ally_attack(state, u):
    """大丽花天赋: 共舞者攻击后 E1固定削韧(含大丽花自身); 敌方目标受到另一位共舞者攻击后
    → FUA（每回合最多1次）"""
    _dahlia_ensure_dancers(state)
    dancers = state.extra.get('dahlia_dancers', [])
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None or u.char.id not in dancers:
        return
    # E1: 共舞者(含大丽花)攻击后固定削韧
    _dahlia_e1_flat(state, dahlia)
    # 天赋FUA: 仅另一位共舞者攻击触发（每回合最多1次, 大丽花回合开始重置）
    if u.char.id == 'the_dahlia':
        return
    if dahlia.extra.get('dahlia_fua_used'):
        return
    dahlia.extra['dahlia_fua_used'] = True
    _dahlia_fua(state)


def _apply_dahlia_baisie(u, state, target, turns=4):
    """大丽花终结技·败谢: 防御-18% + 添加所有共舞者属性弱点（快照恢复）
    v6.7b: 同元素重复施放保留首次快照（此前取当前抗性致快照污染, 到期恢复成-0.2）;
    主状态同 id 覆盖=刷新持续回合, 防减防叠加。"""
    _dahlia_ensure_dancers(state)
    target.add_status(EnemyStatus(id='the_dahlia_baisie', name='败谢',
                                  category='debuff', source='the_dahlia',
                                  remaining_turns=turns,
                                  attributes={'def_reduction': 0.18}))
    elems = ['火']
    for cid in state.extra.get('dahlia_dancers', []):
        partner = next((x for x in state.units if x.char.id == cid), None)
        if partner and partner.char.element not in elems:
            elems.append(partner.char.element)
    for elem in elems:
        existing = next((s for s in target.statuses if s.id == f'dahlia_weak_{elem}'), None)
        if existing is not None:
            old = existing.attributes.get('weakness_old_res', target.get_res(elem))
        else:
            old = target.get_res(elem)
        target.element_res[elem] = min(old, -0.2)
        target.add_status(EnemyStatus(id=f'dahlia_weak_{elem}', name=f'{elem}弱点',
                                      category='debuff', source='the_dahlia',
                                      remaining_turns=turns,
                                      attributes={'weakness_element': elem,
                                                  'weakness_old_res': old}))
        state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                                element=elem, target=target)
    state.log.append(f'  败谢: 防御-18% + 弱点{"/".join(elems)} ({turns}回合)')


def _dahlia_trace1_be_bonus(u, state) -> float:
    """行迹1 数值: txt「提高数值等同于24%大丽花的击破特攻+50%」
    = 大丽花BE×24% + 50%（返回百分比原始数值, TimedBuff 口径）。"""
    from engine.core.combat_engine import _build_effective_stats
    be = _build_effective_stats(u, state).BREAK_EFFECT
    return be * 24.0 + 50.0


def _dahlia_trace1_apply(state, u, turns=1):
    """把行迹1 BE 转移施加给其他存活角色（开战1回合 / 受治疗护盾3回合）。"""
    from engine.runtime import TimedBuff
    bonus = _dahlia_trace1_be_bonus(u, state)
    for eu in state.units:
        if eu.is_alive and eu.char.id != 'the_dahlia':
            eu.buffs.append(TimedBuff(source_id='the_dahlia',
                                      attributes={'BREAK_EFFECT': bonus},
                                      remaining_turns=turns, source_name='又一场葬礼'))
    return bonus


def _trace_dahlia_trace1_open(u, state, **kw):
    """大丽花行迹1·又一场葬礼: 开战其他角色击破特攻+(24%×大丽花BE+50%) 1回合"""
    if u.char.id != 'the_dahlia':
        return
    bonus = _dahlia_trace1_apply(state, u, turns=1)
    state.log.append(f'  大丽花行迹1: 队友击破特攻+{bonus:.1f}%(1回合)')


def _trace_dahlia_trace1_reapply(u, state, targets=None, **kw):
    """大丽花行迹1: 受到队友提供的治疗/护盾→再次触发BE转移, 持续3回合, 单回合1次"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None or dahlia.extra.get('dahlia_trace1_used'):
        return
    if u is not None and u.char.id == 'the_dahlia':
        return  # 仅队友提供的治疗/护盾
    targets = list(targets or [])
    if dahlia not in targets:
        return
    bonus = _dahlia_trace1_apply(state, dahlia, turns=3)
    dahlia.extra['dahlia_trace1_used'] = True
    state.log.append(f'  大丽花行迹1: 受治疗/护盾→队友击破特攻+{bonus:.1f}%(3回合)')


def _trace_dahlia_trace3_implant(u, state, element='', target=None, **kw):
    """大丽花行迹3·弃旧恋新: 我方为敌添加弱点→大丽花速度+30% 2回合;
    火属性添加弱点→+20固定削韧+回10%能量上限"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    from engine.runtime import TimedBuff
    dahlia.buffs.append(TimedBuff(source_id='the_dahlia', attributes={'SPD_PERCENT': 30.0},
                                  remaining_turns=2, source_name='弃旧恋新'))
    # v6.7b: txt 条件=「我方火属性角色施放攻击期间添加过弱点」——火属性角色(非元素)添加弱点
    if target is not None and getattr(u, 'char', None) is not None \
            and u.char.element == '火':
        if target.toughness > 0 and not target.is_broken:
            from engine.core.combat_engine import _flat_toughness_with_break
            _flat_toughness_with_break(state, dahlia, target, 20.0, '火', 'talent')
        # 回10%能量上限的能量, 最多通过此效果恢复至能量上限的50%（v6.7b 补上限）
        cap_half = dahlia.char.max_energy * 0.5
        if dahlia.current_energy < cap_half:
            from engine.core.combat_engine import _gain_energy
            gain = min(dahlia.char.max_energy * 0.10, cap_half - dahlia.current_energy)
            _gain_energy(dahlia, gain, state=state)
        state.log.append(f'  大丽花行迹3: 火属性角色添弱点+20固定削韧 + 回10%能量上限(≤50%)')


def _trace_dahlia_field_tick(u, state, **kw):
    """大丽花结界回合递减（仅大丽花自身回合开始）: 结界-1 + 重置FUA/行迹1回合标记"""
    if u.char.id != 'the_dahlia':
        return
    u.extra['dahlia_fua_used'] = False
    u.extra['dahlia_trace1_used'] = False
    turns = state.extra.get('dahlia_field_turns', 0)
    if turns > 0:
        state.extra['dahlia_field_turns'] = turns - 1
        if turns - 1 <= 0:
            # 移除全队结界buff
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'dahlia_field_buff']
            state.log.append('  大丽花结界: 结束')


def _eid_dahlia_e2(u, state, **kw):
    """大丽花E2: 在场全敌全属性抗性-20% + 敌方目标入场即陷【败谢】3回合（初始波,
    重生波由 _respawn_wave 处理）。v6.7b: 败谢补上弱点部分——复用 _apply_dahlia_baisie
    （防-18%+共舞者属性弱点, 与终结技败谢同状态 id 覆盖刷新）。"""
    if u.char.id != 'the_dahlia':
        return

    for e in state.enemies:
        for elem in list(e.element_res.keys()):
            e.element_res[elem] = e.get_res(elem) - 0.20
        _apply_dahlia_baisie(u, state, e, turns=3)
    state.log.append('  大丽花E2: 全敌全属性抗性-20% + 败谢(3回合)')


def _eid_dahlia_e6(u, state, **kw):
    """大丽花E6: 共舞者击破特攻+150%（永久）; 行动提前在 FUA 内联"""
    if u.char.id != 'the_dahlia':
        return
    from engine.runtime import TimedBuff
    for cid in state.extra.get('dahlia_dancers', []):
        partner = next((x for x in state.units if x.char.id == cid), None)
        if partner:
            # v6.7b: TimedBuff 百分比原始数值口径（150=+150%, 此前 1.50 被 /100 成 +1.5%）
            partner.buffs.append(TimedBuff(source_id='the_dahlia',
                                           attributes={'BREAK_EFFECT': 150.0},
                                           remaining_turns=-1, source_name='大丽花E6'))
    state.log.append('  大丽花E6: 共舞者击破特攻+150%')


def _tech_the_dahlia(state, u, is_opener):
    """大丽花: 立即开启战技结界 + 已破韧目标开战削韧转60%超击破（非进战·领域, 领域互斥）。
    开战削韧值（用户 2026-08-15 确认）: 进战秘技开怪=20, 普攻进战=10——
    按 opener 是否 battle_start 秘技持有者判定。"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage)
    _dahlia_field_apply(state, u)
    opener_id = state.extra.get('opener_id', '')
    opener = next((x for x in state.units if x.char.id == opener_id), None)
    tech = opener.char.skills.get('technique') if opener else None
    is_bs = bool(tech and getattr(tech, 'technique_category', '') == 'battle_start')
    break_amt = 20.0 if is_bs else 10.0
    stats = _build_effective_stats(u, state)
    for e in state.enemies:
        if e.is_broken:
            sb = calculate_damage(stats, e, 0, 0, 'super_break', '火', 80, False,
                                  toughness_dmg=break_amt)
            sb.final_damage *= 0.60
            _commit_enemy_damage(state, u, e, sb.final_damage)
            u.total_damage_dealt += sb.final_damage
    state.log.append(f'[秘技] 心，是最好的坟茔: 开启结界 + 破韧目标60%超击破(开战削韧{break_amt:.0f})')


CHAR_ID = "the_dahlia"
TECHNIQUE = _tech_the_dahlia


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _dahlia_field_buff_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['dahlia_field_buff']: 战技开启结界。"""
    # v6.7 大丽花战技: 开启结界——统一走 _dahlia_field_apply（设置回合数+全队buff,
    # v6.7b: 此前战技只走通用 buff 路径不设 dahlia_field_turns, 未破韧转化核心机制失效）
    _dahlia_field_apply(state, u)
    return True


def _dahlia_baisie_debuff(u, state, target):
    """DEBUFF_TAKEOVERS['the_dahlia_baisie']: 败谢——防御-18%+共舞者弱点。"""
    # v6.7 大丽花终结技·败谢: 防御-18% + 共舞者属性弱点（弱点动态, 特判）
    _apply_dahlia_baisie(u, state, target)
    return True  # 原引擎无条件计入施加目标


EFFECT_TAKEOVERS = {'dahlia_field_buff': _dahlia_field_buff_takeover}
DEBUFF_TAKEOVERS = {'the_dahlia_baisie': _dahlia_baisie_debuff}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _dahlia_ult_skill_split(u, state, skill):
    """PHASE ult_skill_split: 终结技300%ATK由敌方全体均分（→新skill|None）。"""
    # v6.7 大丽花终结技: 300%ATK由敌方全体均分（白厄最后一击先例）
    alive_n = len(state.alive_enemies())
    if alive_n > 0:
        skill = copy.deepcopy(skill)
        for m in skill.multipliers:
            if m.stat == 'ATK':
                m.scale = m.scale / alive_n
        return skill
    return None


PHASE_HOOKS = {'ult_skill_split': _dahlia_ult_skill_split}
