"""tribbie（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy


def _tribbie_apply_shenqi(u, state, turns=3):
    """【神启】: 全队全属性抗性穿透+24%（回合开始-1）
    v6.6c P1: 防重入——重复战技只刷新回合, 不再叠加穿透（此前每次+0.24永久漂移）"""
    if u.extra.get('tribbie_shenqi_turns', 0) > 0:
        u.extra['tribbie_shenqi_turns'] = max(u.extra.get('tribbie_shenqi_turns', 0), turns)
        state.log.append(f'  【神启】刷新 ({u.extra["tribbie_shenqi_turns"]}回合)')
        return
    u.extra['tribbie_shenqi_turns'] = turns
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.RES_PEN_ALL = getattr(eu.base_stats, 'RES_PEN_ALL', 0.0) + 0.24
    state.log.append(f'  【神启】: 全队全抗穿透+24% ({turns}回合)')


def _tribbie_remove_shenqi(u, state):
    """神启到期: 回减全队穿透"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.RES_PEN_ALL = max(0.0, getattr(eu.base_stats, 'RES_PEN_ALL', 0.0) - 0.24)
    u.extra['tribbie_shenqi_turns'] = 0
    state.log.append('  【神启】到期: 全队全抗穿透-24%')


def _tribbie_ult_field(u, state):
    """缇宝结界: 敌受伤+30% 2回合 + 受击后对最高HP目标附加12%HP量子伤
    v6.6c P1: 防重入——重复终结技只刷新回合, 不再叠加易伤（此前每次+0.30永久漂移）"""
    if state.extra.get('tribbie_field_turns', 0) > 0:
        state.extra['tribbie_field_turns'] = 2
        state.log.append('  缇宝结界刷新 (2回合)')
        return
    state.extra['tribbie_field_turns'] = 2
    for e in state.enemies:
        e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
    state.log.append('  缇宝结界: 敌受伤+30% (2回合)')


def _tribbie_remove_field(u, state):
    """结界到期: 回减敌易伤"""
    for e in state.enemies:
        e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.30)
    state.extra['tribbie_field_turns'] = 0
    state.log.append('  缇宝结界到期: 敌受伤-30%')


def _tribbie_field_extra_damage(state, u, hit_targets, total_dmg=0.0):
    """结界受击附加: 每有1名目标受到攻击→对被攻击目标中最高HP者1次12%HP量子伤
    （E2: ×1.2+额外1次; E1: 附加目标真伤=本次攻击总伤害24%）
    v6.8.1: 目标限定被攻击集合（此前从全体存活敌选）+按受击目标数结算次数;
    E1 真伤基数改 total_dmg（此前=附加伤害24%, 量级错误）。"""
    if state.extra.get('tribbie_field_turns', 0) <= 0:
        return
    targets = [t for t in (hit_targets or []) if t is not None and t.HP > 0]
    if not targets:
        return
    stats = _build_effective_stats(u, state)
    total = 0.0
    for _ in range(len(targets)):  # txt: 每有1名目标受到攻击→1次
        live = [t for t in targets if t.HP > 0]
        if not live:
            break
        target = max(live, key=lambda e: e.HP)
        d = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0 * (1.2 if u.eidolon_rank >= 2 else 1.0),
                             'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
        _, killed = _commit_enemy_damage(state, u, target, d.final_damage)
        u.total_damage_dealt += d.final_damage
        total += d.final_damage
        if killed:
            continue

        if u.eidolon_rank >= 2:
            before = target.HP

            d2 = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0 * 1.2,
                                  'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
            _, killed = _commit_enemy_damage(state, u, target, d2.final_damage)
            u.total_damage_dealt += d2.final_damage
            total += d2.final_damage
            if killed:
                continue

        # 献予「门径」之诗: 结界附加伤害额外+1次（v6.6c P2 实装消费）
        if u.extra.get('poem_menjing'):
            before = target.HP

            d3 = calculate_damage(stats, _enemy_for_damage(target), stats.HP, 12.0,
                                  'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                                  crit_mode='expected')
            _, killed = _commit_enemy_damage(state, u, target, d3.final_damage)
            u.total_damage_dealt += d3.final_damage
            total += d3.final_damage
            if killed:
                continue

        # E1: 附加目标真伤 = 本次攻击总伤害×24%（txt:5）
        if u.eidolon_rank >= 1:
            before = target.HP

            td = total_dmg * 0.24
            _, killed = _commit_enemy_damage(state, u, target, td,
                                             damage_type='true_damage',
                                             record_cipher=False)
            u.total_damage_dealt += td
            total += td
            if killed:
                continue

    state.log.append(f'  缇宝结界附加: {total:.0f} ({len(targets)}名受击×12%HP)')


def _tribbie_talent_fua(state, u):
    """天赋: 队友终结技→FUA 18%HP群伤（每角色1次/缇宝终结技重置）
    v6.8.1: E6「天赋FUA伤害+729%」实装（常驻×8.29）;
    行迹1「FUA后增伤72%×3层3回合」改 TimedBuff 滚动（此前只写 tribbie_talent_stack 无消费点）"""
    if u.char.id != 'tribbie':
        return
    stats = _build_effective_stats(u, state)
    e6_mult = 8.29 if u.eidolon_rank >= 6 else 1.0  # 1 + 729%
    total = 0.0
    for e in state.alive_enemies() or state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e), stats.HP, 18.0 * e6_mult,
                             'direct', '量子', 80, stats.CRIT_RATE >= 0.5, crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        total += d.final_damage

    u.total_damage_dealt += total
    # 行迹1: 增伤72%×层数 3回合（TimedBuff 滚动, 消费走通用面板）
    # v6.8.2 极简会话: buff 已过期（被回合边界移除）时从 0 层重新叠
    # （Harness 修: 原 if u.is_alive 块缩进错误, 死亡路径 stacks 未定义会 UnboundLocalError）
    active = next((b for b in u.buffs if getattr(b, 'param_id', '') == 'tribbie_trace1_stack'), None)
    stacks = min(3, (u.extra.get('tribbie_talent_stack', 0) if active else 0) + 1)
    u.extra['tribbie_talent_stack'] = stacks
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'tribbie_trace1_stack']
    u.buffs.append(TimedBuff(source_id='tribbie', attributes={'DMG_BONUS_ALL': 72.0 * stacks},
                             remaining_turns=3, param_id='tribbie_trace1_stack',
                             source_name='行迹1·增伤'))
    state.log.append(f'  缇宝天赋FUA: {total:.0f}(18%HP×{e6_mult:.2f}E6) 行迹1增伤{stacks}/3层')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 天赋FUA路径补气氛


def _trace_tribbie_trace1(u, state, **kw):
    """缇宝行迹1: FUA后增伤72%×3层3回合（叠加由 _tribbie_talent_fua 处理）"""
    if u.char.id != 'tribbie':
        return
    u.extra['tribbie_trace1_stack'] = min(3, u.extra.get('tribbie_trace1_stack', 0) + 1)


def _trace_tribbie_trace3(u, state, **kw):
    """缇宝行迹3: 战斗开始回30能量"""
    if u.char.id != 'tribbie':
        return
    from engine.core.combat_engine import _gain_energy
    _gain_energy(u, 30.0, state=state)
    state.log.append('  行迹·小石子: 战斗开始回30能量')


def _tech_tribbie(state, u, is_opener):
    """缇宝: 进战获得【神启】3回合（非进战）"""

    _tribbie_apply_shenqi(u, state, turns=3)


CHAR_ID = "tribbie"
TECHNIQUE = _tech_tribbie


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _tribbie_turn_tick(u, state):
    # v6.6 缇宝: 神启/结界回合开始-1（实机“自身每回合开始时持续回合数减1”）
    if u.char.id == 'tribbie':
        st = u.extra.get('tribbie_shenqi_turns', 0)
        if st > 0:
            st -= 1
            if st <= 0:
                _tribbie_remove_shenqi(u, state)
            else:
                u.extra['tribbie_shenqi_turns'] = st
        ft = state.extra.get('tribbie_field_turns', 0)
        if ft > 0:
            ft -= 1
            if ft <= 0:
                _tribbie_remove_field(u, state)
            else:
                state.extra['tribbie_field_turns'] = ft


TURN_TICKS = {'pre': _tribbie_turn_tick}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _tribbie_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 战技【神启】/终结技结界+重置队友FUA+E6 立即FUA。"""
    if u.char.id != 'tribbie':
        return None
    # 战技: 【神启】3回合
    if skill_key == 'skill':
        _tribbie_apply_shenqi(u, state)
    # 终结技: 结界 + 重置队友FUA次数 + E6 立即FUA
    if skill_key == 'ultimate':
        _tribbie_ult_field(u, state)
        # v6.6c P1: 缇宝终结技重置每个队友的 FUA 次数（此前 tribbie_ult_used 门锁反了——
        # 首次开大后队友 FUA 永远被封死）
        for k in list(u.extra.keys()):
            if k.startswith('tribbie_fua_'):
                del u.extra[k]
        if u.eidolon_rank >= 6:
            _tribbie_talent_fua(state, u)
    return None


def _tribbie_settle_ally_ult(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_ally_ult: 队友终结技→FUA（每角色1次/缇宝终结技重置）。"""
    # 缇宝天赋: 队友终结技→FUA（每角色1次/缇宝终结技重置）
    if u.char.id == 'tribbie' or skill_key != 'ultimate':
        return None
    trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
    if trib and not trib.extra.get(f'tribbie_fua_{u.char.id}'):
        trib.extra[f'tribbie_fua_{u.char.id}'] = True
        _tribbie_talent_fua(state, trib)
    return None


def _tribbie_settle_field(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_field: 结界期任何我方攻击命中后附加伤害。"""
    # 缇宝结界受击附加（任何我方攻击命中后; v6.8.1: 传受击目标集合+本次总伤害）
    if state.extra.get('tribbie_field_turns', 0) <= 0 or total_dmg <= 0:
        return None
    trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
    if trib:
        # v6.8.2 极简会话: 弹射命中已在伤害循环汇总进 last_attack_targets 且随后清空
        # multihit 缓存, 此处只取 last_attack_targets（Harness 修: 删除悬空表达式+续行符）
        hit_targets = list(state.extra.get('last_attack_targets') or [])
        seen = set()
        uniq = []
        for t in hit_targets:
            if t is not None and id(t) not in seen:
                seen.add(id(t))
                uniq.append(t)
        _tribbie_field_extra_damage(state, trib, uniq, total_dmg)
    return None


SETTLE_HANDLERS = {'settle_self': _tribbie_settle_self,
                   'settle_ally_ult': _tribbie_settle_ally_ult,
                   'settle_field': _tribbie_settle_field}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_menjing(state, summoner, ms_unit, tribbie):
    """献予「门径」之诗(整场, 缇宝): 无视12%防御; 结界附加伤害额外+1次"""
    tribbie.extra['poem_menjing'] = True
    tribbie.base_stats.DEF_PEN += 0.12
    state.log.append('  献予「门径」之诗: 缇宝无视12%防御+结界附加+1次')


POEM = ("门径", _poem_menjing, True)
