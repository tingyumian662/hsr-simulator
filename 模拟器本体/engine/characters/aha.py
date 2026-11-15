"""aha（星神★阿哈, v9.0.0 批4: 全新第94角色, 欢愉/量子）。

机制要点（口径文档 v9.0.0 追加节为权威对照物）:
- 【愿力】: 上限10、8点激活终结技; 施放普攻/战技/欢愉技后+1; 令使目标回合开始+1
  (昔涟"从/未来"点名回填模式, 项目主指定参考); 我方笑点首达40→+1+全队暴伤40%×3回合
  (阿哈回合结束再触发); 行迹1受疗/受盾+1(每终结技1次); E1开局+5。
- 【「欢愉」令使】(战技点名): 指定我方单体获得令使(欢愉度+20%、其回合开始阿哈+1愿力),
  复唱转移旧目标; 追击50%全体量子欢愉伤害。
- 【欢愉万相】(终结技自buff, 3回合): 批5升格体系的触发源。
- 专属天赋·阿哈时刻在: 阿哈时刻基础速度80→阿哈基础速度 + 参演编号0=无视编号最后施放
  (引擎 systems/elation.py 配合)。
- 星神权能·阿哈正传: 我方伤害→目标【欢愉负债】累积(=损失生命25%, ≤10%目标生命上限,
  阶段转换保留; E6 底线20%生命上限)。消耗规则待批末裁定, 本批仅累积层。
- 欢愉技: 60%全体 + 本次阿哈时刻非真伤总和20%真伤均分(E4×1.25)。
"""
import copy

from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _gain_skill_points, _use_skill)
from engine.core.damage import calculate_damage
from engine.systems.elation import gain_laugh

CHAR_ID = "aha"
WISH_CAP = 10.0
WISH_ULT = 8.0


def _find_aha(state):
    return next((x for x in state.units
                 if x.char.id == CHAR_ID and x.is_alive), None)


def _wish_gain(state, u, n):
    cur = u.extra.get('aha_wish', 0.0)
    new = min(WISH_CAP, cur + float(n))
    u.extra['aha_wish'] = new
    state.log.append(f'  【愿力】: {new:.0f}/10')


def _own_laugh_n(u, state) -> float:
    """阿哈哈愉伤害 N: 窗口内用窗口快照; 平时=自身好活(E4: 额外计队友好活总和100%)。"""
    n = (state.extra.get('aha_laugh_n')
         if state.extra.get('aha_running')
         else state.elation_state.get_good_show_total(u.char.id))
    if u.eidolon_rank >= 4 and n is not None:
        n += sum(state.elation_state.get_good_show_total(x.char.id)
                 for x in state.units
                 if x is not u and x.is_alive and x.char.path == '欢愉')
    return n or 0.0


def ascension_active(state) -> bool:
    """v9.0.0 升格强化生效 = 阿哈E2 + 持有【欢愉万相】。
    各欢愉角色模块升格分支的统一守卫（爽点/绝活共赏=升格态的笑点/好活当赏）。"""
    aha = next((x for x in state.units
                if x.char.id == CHAR_ID and x.is_alive and x.eidolon_rank >= 2), None)
    if aha is None:
        return False
    from engine.systems.elation import elation_ascended
    return elation_ascended(state)


# ---- AI ----

def _aha_ai(u, state, **__):
    """阿哈AI: 愿力≥8→终结技; SP>0→战技(令使点名); 否则普攻。"""
    if u.extra.get('aha_wish', 0.0) >= WISH_ULT:
        _use_skill(u, state, 'ultimate')
    elif state.skill_points > 0:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


# ---- 相位 ----

def _aha_energy_gain(u, state, skill_key):
    """energy_gain_override: 愿力驱动, 通用能量回复不适用(txt 模板文案)。"""
    if u.char.id == CHAR_ID:
        return 0.0
    return None


def _aha_laugh_n_bonus(u, state, **kw):
    """PHASE laugh_n_bonus: E4——自身欢愉伤害 N 额外计入队友好活当赏总和的100%。"""
    if u.char.id != CHAR_ID or u.eidolon_rank < 4:
        return None
    return sum(state.elation_state.get_good_show_total(x.char.id)
               for x in state.units
               if x is not u and x.is_alive and x.char.path == '欢愉')


def _aha_eff_stats(u, state, s=None, effective_spd=0.0, **kw):
    """PHASE eff_stats_aha: 行迹3·盒中蕴藏的世界——速度≥125/150→欢愉度+20%/50%
    （项目主裁定 2026-11-14 补数值）; E2·升格自身强化——持有【欢愉万相】期间
    暴击率+20%、造成的伤害无视60%防御(欢愉通道)。"""
    if u.char.id != CHAR_ID:
        return None
    ns = copy.deepcopy(s) if s is not None else None
    if ns is None:
        return None
    if effective_spd >= 150:
        ns.ELATION_LEVEL += 0.50
    elif effective_spd >= 125:
        ns.ELATION_LEVEL += 0.20
    if any(getattr(b, 'param_id', '') == 'aha_wuxiang' for b in u.buffs):
        ns.CRIT_RATE = min(1.0, ns.CRIT_RATE + 0.20)
        ns.DEF_PEN += 0.60  # E2: 造成的伤害无视60%防御力(不限通道, 审核P2-1)
    return ns


def _aha_ascension_refund(u, state, consumed=None, **kw):
    """OBSERVER aha_settle_ascension: 升格——爽点在阿哈时刻结束时返还消耗的10%。"""
    if not ascension_active(state):
        return None
    c = float(consumed or 0.0)
    if c > 0:
        gain_laugh(state, c * 0.10)
        state.log.append(f'  升格·爽点返还: {c:.0f}×10% = {c * 0.10:.0f}')
    return None


# ---- 技能钩子(S4) ----

def _aha_skill_hook(u, state, skill_key):
    if u.char.id != CHAR_ID:
        return
    if skill_key == 'skill':
        _aha_cast_envoy_skill(u, state)
    elif skill_key == 'ultimate':
        _aha_cast_ult(u, state)


def _aha_cast_envoy_skill(u, state):
    """战技·此乃令使: 点名单体获得令使 + 追击50%全体量子欢愉伤害 + 行迹2。"""
    # 令使点名: 转移旧目标, 授予新目标(单体我方优先欢愉, 除自身)
    old = next((x for x in state.units if getattr(x, 'has_envoy', False)), None)
    if old is not None:
        old.has_envoy = False
        old.buffs = [b for b in old.buffs
                     if getattr(b, 'param_id', '') != 'aha_envoy_elation']
    cands = [x for x in state.units if x.is_alive and x is not u]
    elation = [x for x in cands if x.char.path == '欢愉']
    target = (elation or cands)[0] if cands else None
    if target is not None:
        target.has_envoy = True
        target.buffs.append(TimedBuff(
            source_id=CHAR_ID, attributes={'ELATION_LEVEL': 20.0},
            remaining_turns=-1, param_id='aha_envoy_elation',
            source_name='「欢愉」令使'))
        state.log.append(f'  【「欢愉」令使】→ {target.char.name} '
                         f'(其回合开始阿哈+1愿力)')
    # 追击: 50% 全体量子欢愉伤害(施放后+1愿力由 settle 统一处理)
    stats = _build_effective_stats(u, state)
    laugh_n = _own_laugh_n(u, state)
    total = 0.0
    for e in list(state.alive_enemies()):
        d = calculate_damage(stats, _enemy_for_damage(e, 'skill'), 0.0, 50.0,
                             'elation', u.char.element, 80,
                             stats.CRIT_RATE >= 0.5, laugh_n=laugh_n,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage, damage_type='elation')
        total += d.final_damage
    u.total_damage_dealt += total
    u.damage_log.append(('此乃令使·追击', total, 'skill'))
    state.log.append(f'  此乃令使·追击: 50%全体量子欢愉伤害 {total:.0f}')
    # 行迹2·愚弄众神的玩笑: 令使目标对应属性额外欢愉伤害20%(项目主裁定) + 恢复1SP
    trace2 = any(getattr(t, 'hook_name', '') == 'aha_trace2'
                 for t in (u.char.traces or []))
    if trace2 and target is not None:
        extra = 0.0
        for e in list(state.alive_enemies()):
            d = calculate_damage(stats, _enemy_for_damage(e, 'skill'), 0.0, 20.0,
                                 'elation', target.char.element, 80,
                                 stats.CRIT_RATE >= 0.5, laugh_n=laugh_n,
                                 crit_mode='expected')
            _commit_enemy_damage(state, u, e, d.final_damage, damage_type='elation')
            extra += d.final_damage
        u.total_damage_dealt += extra
        _gain_skill_points(state, actor=u)
        state.log.append(f'  行迹2: 令使({target.char.name}属性)额外欢愉伤害20% '
                         f'{extra:.0f} +恢复1SP')


def _aha_cast_ult(u, state):
    """终结技·啊哈！宇宙笑点大爆炸: 耗8愿力 + 欢愉万相3回合 + 行动提前20%
    (200%全体+5×60%弹射走 JSON 倍率)。"""
    if u.extra.get('aha_wish', 0.0) < WISH_ULT:
        state.log.append('  [WARN] 星神★阿哈: 愿力不足8, 无法施放终结技')
        return
    u.extra['aha_wish'] = u.extra.get('aha_wish', 0.0) - WISH_ULT
    state.log.append(f"  【愿力】消耗8 → {u.extra['aha_wish']:.0f}/10")
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'aha_wuxiang']
    u.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={},
                             remaining_turns=3, param_id='aha_wuxiang',
                             source_name='欢愉万相'))
    spd = max(u.base_stats.SPD, 1.0)
    u._pending_action_advance = (10000.0 / spd) * 0.20
    state.log.append('  【欢愉万相】3回合 + 阿哈行动提前20%')
    u.extra['aha_tr1_used'] = False  # 行迹1: 施放终结技后重置
    # E6: 首次施放终结技后 行动提前100% + 100笑点
    if u.eidolon_rank >= 6 and not u.extra.get('aha_e6_first_ult'):
        u.extra['aha_e6_first_ult'] = True
        u._pending_action_advance += (10000.0 / spd) * 1.0
        gain_laugh(state, 100.0)
        state.log.append('  E6: 首次终结技后行动提前100% + 100笑点')


SKILL_HOOKS = [_aha_skill_hook]


# ---- 欢愉技真伤段(结算) ----

def _aha_elation_settle(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 欢愉技真伤均分 + 施放普攻/战技/欢愉技后+1愿力。"""
    if u.char.id != CHAR_ID:
        return
    if skill_key == 'elation_skill':
        ratio = 0.25 if u.eidolon_rank >= 4 else 0.20  # E4: 真伤倍率+25%
        # 开拓者升格E2前置欢愉技: 阿哈真伤改按其伤害的20%计算(由敌方全体均分)
        tb_pre = state.extra.pop('tb_pre_cast_dmg', None)
        if tb_pre is not None:
            true_total = float(tb_pre) * ratio
        else:
            pool = state.extra.get('aha_window_dmg', 0.0)
            true_total = pool * ratio
        alive = state.alive_enemies()
        if true_total > 0 and alive:
            per = true_total / len(alive)
            for e in list(alive):
                _commit_enemy_damage(state, u, e, per, damage_type='true',
                                     record_cipher=False)
            u.total_damage_dealt += true_total
            src = '开拓者前置欢愉技' if tb_pre is not None else '窗口非真伤'
            state.log.append(f'  欢愉技真伤: {src}×{ratio:.0%} '
                             f'= {true_total:.0f} 均分')
    if skill_key in ('basic_attack', 'skill', 'elation_skill'):
        _wish_gain(state, u, 1)  # 天赋: 施放普攻/战技/欢愉技后+1愿力


SETTLE_HANDLERS = {'settle_self': _aha_elation_settle}


# ---- 观察者 ----

def _aha_ally_turn_start(u, state, **kw):
    """OBSERVER ally_turn_start: 令使目标回合开始→阿哈+1愿力(昔涟未来模式)。"""
    aha = _find_aha(state)
    if aha is None or not getattr(u, 'has_envoy', False):
        return None
    _wish_gain(state, aha, 1)
    return None


def _aha_laugh_gain(u, state, n=None, **kw):
    """OBSERVER laugh_gain: 我方笑点首达40→+1愿力+全队暴伤40%×3回合(每自身回合1次)。"""
    aha = _find_aha(state)
    if aha is None or aha.extra.get('aha_talent40_armed') is False:
        return None
    if state.laugh_points < 40:
        return None
    aha.extra['aha_talent40_armed'] = False
    _wish_gain(state, aha, 1)
    for x in state.units:
        if x.is_alive:
            x.buffs.append(TimedBuff(
                source_id=CHAR_ID, attributes={'CRIT_DMG': 40.0},
                remaining_turns=3, param_id='aha_talent_cd',
                source_name='与祂共演'))
    state.log.append('  天赋: 笑点达40 → +1愿力 + 全队暴伤40%×3回合')
    return None


def _aha_on_heal(u, state, healer=None, targets=None, **kw):
    """行迹1: 受到队友治疗→+1愿力(每终结技1次)。"""
    aha = _find_aha(state)
    if aha is None or healer is aha or aha.extra.get('aha_tr1_used'):
        return
    if aha in (targets or []):
        aha.extra['aha_tr1_used'] = True
        _wish_gain(state, aha, 1)
        state.log.append('  行迹1·异想国: 受疗+1愿力(本次终结技周期已用)')


def _aha_on_shield(u, state, targets=None, **kw):
    """行迹1: 受到队友护盾→+1愿力(与治疗共享1次额度)。"""
    _aha_on_heal(u, state, healer=u if u is not None else None, targets=targets)


OBSERVER_HOOKS = {
    'ally_turn_start': _aha_ally_turn_start,
    'laugh_gain': _aha_laugh_gain,
    'aha_settle_ascension': _aha_ascension_refund,
}


# ---- 回合 tick ----

def _aha_turn_tick(u, state):
    """阿哈回合边界: 天赋40点触发器重新武装(txt: 阿哈回合结束后可再次触发)。"""
    if u.char.id == CHAR_ID:
        u.extra['aha_talent40_armed'] = True


TURN_TICKS = {'late': _aha_turn_tick}


def _aha_ascension_end_tick(u, state, **kw):
    """HOOK on_turn_end(阿哈自身回合末, 欢愉万相在 _tick_buffs 后到期):
    裁定③B 2026-11-15——升格结束瞬间, 银狼升格溢出的隐藏分回截300(丢弃超出)。"""
    if u is None or getattr(u, 'char', None) is None or u.char.id != CHAR_ID:
        return
    was = u.extra.get('aha_was_ascended', False)
    now = ascension_active(state)
    if was and not now:
        yl = next((x for x in state.units
                   if x.char.id == 'yinlang' and x.is_alive), None)
        if yl is not None and yl.hidden_score > 300.0:
            yl.hidden_score = 300.0
            state.log.append('  升格结束: 银狼隐藏分回截300(溢出丢弃)')
    u.extra['aha_was_ascended'] = now


def _aha_on_death(u, state, **kw):
    """HOOK on_ally_death: 裁定⑦B 2026-11-15——阿哈阵亡撤除常驻类效果:
    E1 全队增笑-15% + E6 欢愉负债底线清除(范围仅此两项, 星神权能累积通道
    不在本次裁定范围——HARNESS 申报)。"""
    if u is None or getattr(u, 'char', None) is None or u.char.id != CHAR_ID:
        return
    if state.extra.pop('aha_e1_active', None) is not None:
        for x in state.units:
            x.base_stats.LAUGH_BOOST = max(0.0, x.base_stats.LAUGH_BOOST - 0.15)
        state.log.append('  阿哈阵亡: E1增笑撤除(全队-15%)')
    if state.extra.pop('aha_debt_floor_ratio', None) is not None:
        state.log.append('  阿哈阵亡: E6欢愉负债底线清除')


# ---- 秘技 / 入场 ----

def _tech_aha(state, u, is_opener):
    """秘技·ABRACADABRA！: 进战固定20笑点额外回合(不消耗笑点池, 额外阿哈通道)。"""
    elation = state.extra.get('_elation')
    if elation is not None:
        elation.execute_extra_aha(state, 20.0)
        state.log.append('[秘技] ABRACADABRA！: 固定20笑点额外阿哈回合')


def _init_battle(state):
    aha = _find_aha(state)
    if aha is None:
        return
    if aha.eidolon_rank >= 1:
        _wish_gain(state, aha, 5)
        # E1·在场时我方全体欢愉伤害增笑15%（增笑=LAUGH_BOOST 独立乘区, 口径既有定义;
        # 全队按 base_stats 直加, 阵亡随 on_ally_death 撤除——裁定⑦B 2026-11-15）
        for x in state.units:
            if x.is_alive:
                x.base_stats.LAUGH_BOOST += 0.15
        state.extra['aha_e1_active'] = True
        state.log.append('  E1·增笑: 全队LAUGH_BOOST+15%')
    # 行迹3·盒中蕴藏的世界: 开战行动提前30%（项目主裁定 2026-11-14 补数值）
    trace3 = any(getattr(t, 'hook_name', '') == 'aha_trace3'
                 for t in (aha.char.traces or []))
    if trace3:
        spd = max(aha.base_stats.SPD, 1.0)
        aha._pending_action_advance = (10000.0 / spd) * 0.30
        state.log.append('  行迹3: 战斗开始行动提前30%')
    # 行迹1: 受疗/受盾事件注册(队友来源)
    state.hooks.register(CHAR_ID, 'on_heal', _aha_on_heal,
                         source_name='星神★阿哈行迹1')
    state.hooks.register(CHAR_ID, 'on_shield', _aha_on_shield,
                         source_name='星神★阿哈行迹1')
    # 裁定③B/⑦B 2026-11-15: 升格结束回截 + 阵亡撤除
    state.hooks.register(CHAR_ID, 'on_turn_end', _aha_ascension_end_tick,
                         source_name='星神★阿哈·升格结束检测')
    state.hooks.register(CHAR_ID, 'on_ally_death', _aha_on_death,
                         source_name='星神★阿哈·阵亡撤除')
    # 星神权能·阿哈正传: 开启我方伤害→欢愉负债累积通道(_commit_enemy_damage 消费)
    state.extra['aha_debt_on'] = True
    # E6: 敌方始终拥有至少20%生命上限的【欢愉负债】(底线比率, 引擎消费)
    if aha.eidolon_rank >= 6:
        state.extra['aha_debt_floor_ratio'] = 0.20


TECHNIQUE = _tech_aha
INIT = _init_battle
AI = _aha_ai

PHASE_HOOKS = {
    'energy_gain_override': _aha_energy_gain,
    'laugh_n_bonus': _aha_laugh_n_bonus,
    'eff_stats_aha': _aha_eff_stats,
}
