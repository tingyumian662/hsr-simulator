"""姬子·启行（M4 批1 迁入；助战技子系统+同行协议名单+裁决封技）"""

import copy
import random
from engine.runtime import ENERGY_GAIN, SimUnit, TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _flat_toughness_with_break
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _skill_level_factor
from engine.core.combat_engine import _use_skill


def _hn_ally_auto_support(state, u):
    """v7.2.0 #8: 非姬子我方角色行动后自动使用助战技——
    姬子·启行在场且全队共享次数>0时呼唤「拓星者」（不占该角色行动, 消耗1次共享次数）。
    此前助战技次数池只有姬子自己的AI与协议触发在消费, 队友从不使用。"""
    if not isinstance(u, SimUnit) or u.char.id == 'himeko_nova' or not u.is_alive:
        return
    hn = next((x for x in state.units
               if x.char.id == 'himeko_nova' and x.is_alive), None)
    if hn is None or state.extra.get('hn_support_uses', 0) <= 0:
        return
    _hn_support_skill(state, u)


def _hn_realm_blocks_ult(state, u) -> bool:
    """v7.2.0 裁决A: 姬子·启行在场=境界【拓星视界】永久占据境界位——
    遐蝶(遗世冥域)/白厄(卡厄斯兰那)的境界类终结技永久无法施放(与实机相同)。"""
    if not isinstance(u, SimUnit) or u.char.id not in ('xiadie', 'phainon'):
        return False
    return any(x.char.id == 'himeko_nova' and x.is_alive for x in state.units)


HIMEKO_NOVA_COMPANIONS = {
    'trailblazer_destruction', 'trailblazer_elation', 'trailblazer_harmony',
    'trailblazer_preservation', 'trailblazer_remembrance',
    'himeko', 'himeko_nova', 'march_7th', 'march_7th_hunt', 'changyeyue',
    'dan_heng', 'dan_heng_imbibitor_lunae', 'dan_heng_permansor_terrae',
    'welt', 'sunday',
}


HIMEKO_NOVA_VERDICT = {
    'trailblazer_destruction', 'trailblazer_elation', 'trailblazer_harmony',
    'trailblazer_preservation', 'trailblazer_remembrance',
    'dan_heng', 'dan_heng_imbibitor_lunae', 'dan_heng_permansor_terrae', 'sunday',
}


HIMEKO_NOVA_CHARGE = {'march_7th', 'march_7th_hunt', 'changyeyue', 'welt', 'himeko'}


def _hn_support_cap(u) -> int:
    """助战技使用次数上限: 1(天赋) + 1(E2)"""
    return 2 if u.eidolon_rank >= 2 else 1


def _hn_support_skill(state, user, *, no_charge=False):
    """助战技·开拓与你同行: 用姬子·启行面板结算（视为她施放战技, 不调 _use_skill 防递归）。
    全队80%ATK+3×12%弹射（姬子本人200%+4×32%, E1弹射+1）; 姬子使用不耗次数(行迹1)+
    天赋全抗穿透20%/暴伤80%; 非姬子使用者回4能量; 行迹2开拓同行→额外回合;
    E2×130%; E4全队抗穿; E6×175%+姬子+1源能（自用/他用均+1）。
    v6.7b: 削韧无视弱点（视为姬子施放战技）+击破结算; 弹射段击杀计数;
    歼破协议充能计数（no_charge=协议免费助战技不计数）; 姬子自用 E4 抗穿 30%。"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None:
        return
    is_self = user.char.id == 'himeko_nova'
    # 次数检查（姬子使用不消耗=行迹1）
    if not is_self:
        uses = state.extra.get('hn_support_uses', 0)
        if uses <= 0:
            return
        state.extra['hn_support_uses'] = uses - 1
    alive = state.alive_enemies()
    if not alive:
        state.log.append('  助战技·开拓与你同行: 无存活目标, 未施放')
        return
    stats = _build_effective_stats(himeko, state)
    if is_self:
        # 天赋: 姬子使用时全抗穿透20%/30%(E4)+暴伤80%（均不可叠加, 技能级）
        # v7.2.0 #3: E5天赋+2 → 每级+5%惯例消费
        talent_factor = _skill_level_factor(himeko, 'talent')
        stats = copy.deepcopy(stats)
        stats.RES_PEN_ALL += (0.30 if himeko.eidolon_rank >= 4 else 0.20) * talent_factor
        stats.CRIT_DMG += 0.80 * talent_factor
    elif himeko.eidolon_rank >= 4:
        # E4: 非姬子使用→全队全抗穿透（姬子额外+10%）; 百分比按原始数值口径
        for eu in state.units:
            if eu.is_alive:
                extra = 30.0 if eu.char.id == 'himeko_nova' else 20.0
                eu.buffs.append(TimedBuff(source_id='himeko_nova',
                                          attributes={'RES_PEN_ALL': extra},
                                          remaining_turns=1, source_name='姬子E4抗穿'))
    # 歼破协议: 战技造成的暴击伤害额外提高100%（助战技视为战技）
    if state.extra.get('hn_charge_skill_cd'):
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 1.0
    aoe_scale = 200.0 if is_self else 80.0
    bounce_hits = (4 if is_self else 3) + (1 if himeko.eidolon_rank >= 1 else 0)
    bounce_scale = 32.0 if is_self else 12.0
    mult = 1.0
    if himeko.eidolon_rank >= 2:
        mult *= 1.30  # E2: 助战技伤害×130%
    if himeko.eidolon_rank >= 6:
        mult *= 1.75  # E6: 我方用助战技伤害+75%
    total = 0.0
    for t in alive:
        if not no_charge:
            _hn_count_hits(state, user)  # 歼破协议: 每击中1目标+1充能
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, aoe_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, user, t, d.final_damage)
        total += d.final_damage
        # 削韧（群攻10/单攻5）: 视为姬子施放战技→无视弱点; 击破按火属性结算
        if t.toughness > 0:
            _flat_toughness_with_break(state, himeko, t, 10.0, '火', 'support_skill', stats)
    for _ in range(bounce_hits):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        if not no_charge:
            _hn_count_hits(state, user)
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, bounce_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='skill', attack_type='follow_up',
                             crit_mode='expected')
        _commit_enemy_damage(state, user, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, himeko, t, 5.0, '火', 'support_skill', stats)
    user.total_damage_dealt += total
    # 使用助战技视为姬子·启行施放的战技 → 姬子按战技回能
    _gain_energy(himeko, ENERGY_GAIN.get('skill', 30.0), state=state)
    # E6: 我方使用或发动助战技→姬子+1源能（自用/他用一致）
    if himeko.eidolon_rank >= 6:
        himeko.extra['hn_source_energy'] = min(
            6, himeko.extra.get('hn_source_energy', 0) + 1)
    if is_self:
        himeko.damage_log.append(('开拓与你同行(自己)', total, 'support_skill'))
    else:
        # 非姬子使用者: 回4能量 + 行迹2额外回合（可插入施放终结技）
        _gain_energy(user, 4.0, state=state)
        # v7.2.0 #7: 行迹2按次触发（原实现每角色全场仅1次=误读防循环条款）;
        # E2: 非开拓同行角色使用助战技也获得额外回合
        trace2_ok = (user.char.id in HIMEKO_NOVA_COMPANIONS
                     or himeko.eidolon_rank >= 2)
        already_queued = any(x is user for x, k in state.extra.get('extra_turns', []))
        if trace2_ok and not already_queued \
                and not user.extra.get('hn_trace2_pending'):
            user.extra['hn_trace2_pending'] = True  # 额外回合内不再触发(防循环)
            state.extra.setdefault('extra_turns', []).append((user, 'ult'))
            state.log.append(f'  姬子行迹2: {user.char.name}获得额外回合(终结技位)')
    state.log.append(f'  助战技·开拓与你同行: {user.char.name} {total:.0f}'
                     f'({"姬子面板" if is_self else "回4能量"})')
    state.hooks.trigger_all("on_attack_action", u=user, state=state, dealt=total > 0)  # v7.1.0 P1: 助战技(不调_use_skill)补气氛


def _hn_ultimate(state, u):
    """姬子·启行终结技·我们，亦是逐星的巨人（v7.2.0 裁决B 输出手法）:
    行迹3开局+3源能 → 脉冲 → 3×光束 → 脉冲 → 3×光束 → 脉冲 → 最后一击
    脉冲: 消耗当前全部源能——基础10%全体 + 每额外1点1次15%随机单体
          (行迹3当次源能≥3→单体倍率×1.3; E6当次源能≥6→额外160%全体);
    光束: 16%全体 +1源能(E6+2), 上限3(E6:6);
    最后一击: 3×80%随机单体; 任意段清场(无存活敌)→跳过剩余段直接收尾。
    v7.2.0 #3: E3终结技+2 → 全部内联倍率×_skill_level_factor(ultimate)(每级+5%)"""
    stats = _build_effective_stats(u, state)
    alive = state.alive_enemies()
    cap = 6 if u.eidolon_rank >= 6 else 3
    mult = 1.30 if u.eidolon_rank >= 2 else 1.0  # E2: 终结技伤害×130%
    ult_factor = _skill_level_factor(u, 'ultimate')  # v7.2.0 #3: E3终结技+2
    beam_scale = 16.0 * ult_factor
    pulse_scale = 10.0 * ult_factor
    last_scale = 80.0 * ult_factor
    trace3 = any(getattr(tr, 'hook_name', '') == 'himeko_nova_trace3'
                 for tr in (u.char.traces or []))
    # 行迹3: 施放终结技立即+3源能（=手法启动资源）
    if trace3:
        u.extra['hn_source_energy'] = min(cap, u.extra.get('hn_source_energy', 0) + 3)
    total = 0.0
    src_used = 0
    cleared = [False]

    def _alive_now():
        return [e for e in alive if e.HP > 0]

    def _beam_volley(times):
        """超频粒子光束: 每次全体16%+削韧2, 每次+1源能(E6+2)"""
        nonlocal total
        for _ in range(times):
            if not _alive_now():
                cleared[0] = True
                return
            for t in _alive_now():
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, beam_scale * mult,
                                     'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate', crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage
                _flat_toughness_with_break(state, u, t, 2.0, '火', 'ultimate', stats)
            gain = 2 if u.eidolon_rank >= 6 else 1  # E6: 光束额外+1源能
            u.extra['hn_source_energy'] = min(
                cap, u.extra.get('hn_source_energy', 0) + gain)

    def _pulse():
        """轨道歼灭脉冲: 消耗全部源能——10%全体 + 每额外1点1次15%随机单体(行迹3≥3×1.3)"""
        nonlocal total, src_used
        src = u.extra.get('hn_source_energy', 0)
        u.extra['hn_source_energy'] = 0
        src_used += src
        for t in _alive_now():
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, pulse_scale * mult,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            _flat_toughness_with_break(state, u, t, 2.0, '火', 'ultimate', stats)
        bounce_scale = 15.0 * ult_factor
        if trace3 and src >= 3:
            bounce_scale *= 1.3  # 行迹3: 当次源能≥3时脉冲单体倍率+30%
        for _ in range(max(0, src - 1)):  # 每额外1点源能1次随机单体
            if not _alive_now():
                cleared[0] = True
                return
            t = random.choice(_alive_now())
            d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, bounce_scale * mult,
                                 'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # E6: 当次源能≥6→额外160%全体
        if u.eidolon_rank >= 6 and src >= 6:
            for t in _alive_now():
                d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK,
                                     160.0 * ult_factor * mult,
                                     'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                                     skill_type='ultimate', crit_mode='expected')
                _commit_enemy_damage(state, u, t, d.final_damage)
                total += d.final_damage

    # 裁决B 手法: 脉冲-3光束-脉冲-3光束-脉冲-最后一击
    _pulse()
    if not cleared[0]:
        _beam_volley(3)
    if not cleared[0]:
        _pulse()
    if not cleared[0]:
        _beam_volley(3)
    if not cleared[0]:
        _pulse()
    # 最后一击: 3次×80%随机单体
    for _ in range(3):
        if not _alive_now():
            break
        t = random.choice(_alive_now())
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, last_scale * mult,
                             'direct', '火', 80, stats.CRIT_RATE >= 0.5,
                             skill_type='ultimate', crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    u.damage_log.append(('我们，亦是逐星的巨人', total, 'ultimate'))
    # 终结技后: 特殊效果额外助战技次数刷新（2次/E1:3次）
    state.extra['hn_protocol_uses'] = 3 if u.eidolon_rank >= 1 else 2
    state.log.append(f'  姬子·启行终结技: {total:.0f} '
                     f'(脉冲-3光束-脉冲-3光束-脉冲-最后一击, 源能消耗{src_used})')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 内联终结技路径补气氛


def _hn_count_ally_ult(state, u):
    """同行协议·裁决: 队友主动施放终结技→计数, 达阈值(2/E1:1)→无消耗助战技"""
    if u.char.id == 'himeko_nova':
        return
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None or not state.extra.get('hn_verdict'):
        return
    threshold = 1 if himeko.eidolon_rank >= 1 else 2  # E1: 所需终结技次数-1
    cnt = state.extra.get('hn_verdict_ult_count', 0) + 1
    state.extra['hn_verdict_ult_count'] = cnt
    if cnt >= threshold:
        state.extra['hn_verdict_ult_count'] = 0
        _hn_try_protocol_support(state, himeko)
        state.log.append(f'  裁决协议: 队友{cnt}次终结技→无消耗助战技')


def _hn_count_hits(state, u):
    """同行协议·歼破: 每击中1名敌方目标+1充能, 达9点(E1:6)→无消耗助战技(本次不获充能)
    v6.7b: 删除姬子自身排除——txt 未排除姬子; 免费助战技不计数由调用方 no_charge 控制。"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None or not state.extra.get('hn_charge_mode'):
        return
    cap = 6 if himeko.eidolon_rank >= 1 else 9  # E1: 所需充能-3
    charge = state.extra.get('hn_charge', 0) + 1
    if charge >= cap:
        state.extra['hn_charge'] = 0
        _hn_try_protocol_support(state, himeko)
        state.log.append(f'  歼破协议: 充能{charge}点→无消耗助战技(本次不获充能)')
    else:
        state.extra['hn_charge'] = charge


def _hn_try_protocol_support(state, himeko):
    """特殊效果免费助战技（单场最多2次, 姬子终结技后刷新）"""
    uses = state.extra.get('hn_protocol_uses', 0)
    if uses <= 0:
        state.log.append('  同行协议: 特殊效果次数已耗尽')
        return
    state.extra['hn_protocol_uses'] = uses - 1
    _hn_support_skill(state, himeko, no_charge=True)  # txt: 本次助战技无法获得充能


def _hn_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """姬子·启行 AI: 满能量→终结技; 助战技轮转（自身不耗次数但1回合CD, 期间战技维持
    领航旗语+恢复次数）; 战技/普攻兜底
    v7.2.0 #2: cd 置2——置1后同回合末尾减1=无效CD, 导致永远助战技、旗语3回合后
    永久丢失; 置2后实际序列=助战技→战技→助战技→战技(旗语持续维持)"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif u.extra.get('hn_skill_cd', 0) <= 0:
        _hn_support_skill(state, u)
        u.extra['hn_skill_cd'] = 2
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")
    if u.extra.get('hn_skill_cd', 0) > 0:
        u.extra['hn_skill_cd'] -= 1


def _trace_hn_protocol(u, state, **kw):
    """姬子·启行天赋·同行协议（on_enter_battle）:
    裁决（开拓者/丹恒/星期日）: 姬子伤害+100%+终结技额外+100%+队友终结技计数→免费助战技;
    歼破（三月七/长夜月/瓦尔特/姬子）: 全队暴伤+100%+每击中充能→免费助战技; 两协议可共存
    v7.2.0 裁决A: 在场即展开境界【拓星视界】永久占据境界位——
    遐蝶(遗世冥域)/白厄(卡厄斯兰那)的境界类终结技永久无法施放(与实机相同)"""
    if u.char.id != 'himeko_nova':
        return
    from engine.runtime import TimedBuff
    state.realm_owner = 'himeko_nova'
    state.realm_turns = -1
    state.log.append('  展开【拓星视界】(境界, 在场期间永久): 遐蝶/白厄终结技被封锁')
    ids = {x.char.id for x in state.units}
    if ids & HIMEKO_NOVA_VERDICT:
        state.extra['hn_verdict'] = True
        u.buffs.append(TimedBuff(source_id='himeko_nova',
                                 attributes={'DMG_BONUS_ALL': 100.0,
                                             'DMG_BONUS_ULTIMATE': 100.0},
                                 remaining_turns=-1, source_name='同行协议·裁决'))
        state.log.append('  同行协议·裁决: 姬子伤害+100%+终结技额外+100%')
    if ids & HIMEKO_NOVA_CHARGE:
        state.extra['hn_charge'] = 0
        state.extra['hn_charge_mode'] = True
        # v6.7b: txt「战技造成的暴击伤害额外提高100%」——标记由伤害循环消费
        state.extra['hn_charge_skill_cd'] = True
        for eu in state.units:
            if eu.is_alive:
                eu.buffs.append(TimedBuff(source_id='himeko_nova',
                                          attributes={'CRIT_DMG': 100.0},
                                          remaining_turns=-1, source_name='同行协议·歼破'))
        state.log.append('  同行协议·歼破: 全队暴伤+100% + 战技暴伤额外+100%')


def _trace_hn_flag_regen(u, state, **kw):
    """姬子·启行: 每回合开始——领航旗语期间恢复1次助战技次数; 行迹1(次数=上限时回5能量)
    v7.2.0 #5: E2「领航旗语状态下每个回合开始时额外恢复1次」→ 恢复第2次"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None:
        return
    from engine.core.combat_engine import _gain_energy
    cap = _hn_support_cap(himeko)
    # 领航旗语（战技buff在身）: 每回合开始恢复1次
    if any(getattr(b, 'param_id', '') == 'himeko_nova_flag' for b in himeko.buffs):
        state.extra['hn_support_uses'] = min(cap, state.extra.get('hn_support_uses', 0) + 1)
        state.log.append('  领航旗语: 助战技次数+1')
        if himeko.eidolon_rank >= 2:  # v7.2.0 #5: E2 额外恢复1次
            state.extra['hn_support_uses'] = min(cap, state.extra.get('hn_support_uses', 0) + 1)
            state.log.append('  姬子E2: 领航旗语额外助战技次数+1')
    # 行迹1: 回合开始时若使用次数=上限→回5能量
    if state.extra.get('hn_support_uses', 0) >= cap:
        _gain_energy(himeko, 5.0, state=state)
        state.log.append('  姬子行迹1: 助战技次数已满→回5能量')


def _eid_hn_e6(u, state, **kw):
    """姬子·启行E6: 火属性抗性穿透+20%（永久）; 源能上限/光束额外源能/脉冲额外段 内联
    v6.7b: RES_PEN_FIRE 会落成英文键 'FIRE' 永不消费——直接写中文键 RES_PEN['火']。"""
    if u.char.id != 'himeko_nova':
        return
    u.base_stats.RES_PEN['火'] = u.base_stats.RES_PEN.get('火', 0.0) + 0.20
    state.log.append('  姬子E6: 火属性抗性穿透+20%')


def _tech_himeko_nova(state, u, is_opener):
    """姬子·启行: 秘技点上限+3（队伍效果, 无条件） + 每波次开始立即施放1次战技
    （拓星巡航, 进战; v6.7b 落实开怪者门控——战技部分仅开怪者生效;
    普通敌人直接消灭不进入战斗的语义在模拟器内不体现）"""
    state.max_sp += 3
    if not is_opener:
        state.log.append('[秘技] 拓星巡航: 秘技点上限+3')
        return
    state.extra['hn_tech_active'] = True
    from engine.core.combat_engine import _use_skill
    _use_skill(u, state, 'skill')
    state.log.append('[秘技] 拓星巡航: 秘技点上限+3, 首波立即施放战技')


def _init_battle(state):
    hn = next((x for x in state.units if x.char.id == CHAR_ID), None)
    if hn is None:
        return
    state.extra['hn_support_uses'] = 1  # 天赋: 全队1次助战技使用次数
    # 特殊效果免费助战技（单场2次, 终结技后刷新; E1: 3次）
    state.extra['hn_protocol_uses'] = 3 if hn.eidolon_rank >= 1 else 2



CHAR_ID = "himeko_nova"
AI = _hn_ai
INIT = _init_battle
TECHNIQUE = _tech_himeko_nova
# 同行协议判定名单（硬编码清单#1, 随迁角色模块）
__all__ = ["CHAR_ID", "AI", "INIT", "TECHNIQUE"]


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _hn_flag_mutator(u, state, attrs, skill):
    """EFFECT_MUTATORS['himeko_nova_flag']: 领航旗语3回合+按战技等级增伤+恢复助战次数。"""
    # v6.7b: 领航旗语 3回合（此前默认2）+ 立即恢复所有助战技次数（txt）
    # v7.2.0 #3: E5战技+2 → 旗语增伤按战技等级消费(每级+5%, 基准Lv10=20%)
    attrs['DMG_BONUS_ALL'] = 20.0 * _skill_level_factor(u, 'skill')
    if u.char.id == 'himeko_nova':
        state.extra['hn_support_uses'] = _hn_support_cap(u)
        state.log.append('  领航旗语: 立即恢复所有助战技使用次数')
    return attrs, 3


EFFECT_MUTATORS = {'himeko_nova_flag': _hn_flag_mutator}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _hn_ult_inline(u, state, skill):
    """PHASE ult_inline: 姬子·启行终结技双模式内联（→True=完全处理）。"""
    from engine.core.combat_engine import _ult_post
    # v6.7 姬子·启行终结技: 双模式内联（光束/脉冲/最后一击）
    _hn_ultimate(state, u)
    _ult_post(state, u)
    return True  # 伤害已内联结算, 跳过后续通用伤害循环


PHASE_HOOKS = {'ult_inline': _hn_ult_inline}
