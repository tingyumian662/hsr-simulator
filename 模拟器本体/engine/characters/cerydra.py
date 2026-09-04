"""cerydra（M4 收官批迁入）"""

import copy
import random


def _cerydra_grant_jungong(state, u, target):
    """【军功】: 目标获军功（仅最新）
    v6.8.1: 换目标清除旧军功者状态（标记/爵位属性对称回退/行迹3 SPD 迁移）——
    此前旧目标仍可触发军功 FUA/回能且升变穿透不回退。"""
    old = u.extra.get('cerydra_jungong_id')
    if old and old != target.char.id:
        old_t = next((x for x in state.units if x.char.id == old), None)
        if old_t:
            old_t.extra['cerydra_jungong'] = False
            if old_t.extra.get('cerydra_juewei'):
                _cerydra_qixi(state, u, old_t)  # 对称回减升变属性（含律法暴伤）
            else:
                old_t.extra['cerydra_juewei'] = False
            # 行迹3 SPD buff 迁移到新军功者（buff 生效期内）
            if u.extra.get('cerydra_spd_buff_turns', 0) > 0 \
                    and u.extra.get('cerydra_spd_buff_ally') == old:
                old_t.base_stats.SPD -= 20
                target.base_stats.SPD += 20
                u.extra['cerydra_spd_buff_ally'] = target.char.id
                state.log.append(f'  行迹3: SPD buff 迁移 {old_t.char.name}→{target.char.name}')
        u.extra['cerydra_charge'] = 0  # 换目标充能归零
        state.log.append('  军功换目标: 旧目标状态清除+充能归零')
    u.extra['cerydra_jungong_id'] = target.char.id
    was_juewei = bool(target.extra.get('cerydra_juewei'))

    target.extra['cerydra_jungong'] = True
    target.extra['cerydra_juewei'] = False
    if was_juewei:
        target.extra['cerydra_juewei'] = True

    u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 1)
    state.log.append(f'  【军功】: {target.char.name} +充能{u.extra["cerydra_charge"]}/8')
    _cerydra_check_promote(state, u, target)


def _cerydra_check_promote(state, u, target):
    """充能≥6: 升【爵位】（解控+战技CD+72%+全抗穿透10%）"""
    if target.extra.get('cerydra_juewei') or u.extra.get('cerydra_charge', 0) < 6:
        return
    target.extra['cerydra_juewei'] = True
    # v6.6c P1: 实装升变效果——全抗穿透+10%+暴伤72%。
    target.base_stats.RES_PEN_ALL = getattr(target.base_stats, 'RES_PEN_ALL', 0.0) + 0.10
    target.base_stats.CRIT_DMG += 0.72
    target.extra['cerydra_rank_title_cd'] = True
    if u.extra.get('poem_lvfa'):
        target.base_stats.CRIT_DMG += 0.30  # 献予「律法」: 军功者暴伤+30%
    # 解控（清控制类状态）
    target.statuses = [s for s in target.statuses
                       if getattr(s, 'category', '') != 'control']
    u.extra['cerydra_juewei_target'] = target.char.id
    state.log.append(f'  【爵位】: {target.char.name} 升变(解控+战技CD+72%+全抗穿透10%)')


def _cerydra_qixi(state, u, target):
    """奇袭: 爵位者战技后触发——消耗6充能降回军功（v6.6c: 对称回减穿透/律法暴伤 + 律法充能+1）"""
    if not target.extra.get('cerydra_juewei'):
        return
    u.extra['cerydra_charge'] = max(0, u.extra.get('cerydra_charge', 0) - 6)
    target.extra['cerydra_juewei'] = False
    target.base_stats.RES_PEN_ALL = max(0.0, getattr(target.base_stats, 'RES_PEN_ALL', 0.0) - 0.10)
    if target.extra.pop('cerydra_rank_title_cd', False):
        target.base_stats.CRIT_DMG = max(0.0, target.base_stats.CRIT_DMG - 0.72)
    if u.extra.get('poem_lvfa'):
        target.base_stats.CRIT_DMG -= 0.30
        u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 1)
        state.log.append('  献予「律法」: 奇袭后充能+1')
    u.extra.pop('cerydra_juewei_target', None)
    state.log.append(f'  奇袭结束: 【爵位】→【军功】(充能{u.extra["cerydra_charge"]}/8)')


def _cerydra_jungong_target(state, u):
    """当前军功持有者（仅最新）"""
    jid = u.extra.get('cerydra_jungong_id')
    if jid:
        return next((x for x in state.units if x.char.id == jid and x.is_alive), None)
    return None


def _trace_cerydra_trace1(u, state, **kw):
    """刻律德菈行迹1: ATK>2000每超100点暴伤+18%上限360%
    v6.10.3 P1-5: 改为 _build_effective_stats 动态消费（此前入场永久写 base_stats 与动态面板双算）"""
    if u.char.id != 'cerydra':
        return


def _trace_cerydra_trace2(u, state, **kw):
    """刻律德菈行迹2: 暴击率+100%"""
    if u.char.id != 'cerydra':
        return
    u.base_stats.CRIT_RATE += 1.0


def _tech_cerydra(state, u, is_opener):
    """刻律德菈: 获得【军功】+开战自动对军功者施放1次战技（非进战）"""

    ally = min(state.units, key=lambda x: getattr(x, 'position', 99))
    if ally is not u:
        _cerydra_grant_jungong(state, u, ally)
    state.log.append('[秘技] 先手优势: 获得【军功】+开战自动战技')


CHAR_ID = "cerydra"
TECHNIQUE = _tech_cerydra


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _cerydra_turn_tick(u, state):
    # v6.6 刻律德菈: 军功SPD buff 3回合到期回减（行迹3）
    # v6.6c P1: 回减记录的受buff者（此前回减当前军功者, 换目标后打错对象）
    if u.char.id == 'cerydra' and u.extra.get('cerydra_spd_buff_turns', 0) > 0:
        t = u.extra.get('cerydra_spd_buff_turns', 0) - 1
        if t <= 0:
            u.base_stats.SPD -= 20
            jid = u.extra.pop('cerydra_spd_buff_ally', None)
            jg = next((x for x in state.units if x.char.id == jid and x.is_alive), None) if jid else None
            if jg:
                jg.base_stats.SPD -= 20
            u.extra['cerydra_spd_buff_turns'] = 0
        else:
            u.extra['cerydra_spd_buff_turns'] = t


TURN_TICKS = {'pre': _cerydra_turn_tick}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _cerydra_skill_adjust_pre(u, state, skill, skill_key):
    """PHASE skill_adjust_pre: E4 终结技倍率+240（→新skill|None）。"""
    if skill_key == 'ultimate' and u.eidolon_rank >= 4 and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale += 240.0
        return skill
    return None


PHASE_HOOKS = {'skill_adjust_pre': _cerydra_skill_adjust_pre}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _cerydra_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 战技军功授予+行迹3SPD; 终结技充能+补授+附加重置。"""
    from engine.core.combat_engine import _gain_energy, _pick_single_ally_target
    if u.char.id != 'cerydra':
        return None
    # 战技: 军功授予
    if skill_key == 'skill':
        ally = _pick_single_ally_target(state, u)
        if ally:
            _cerydra_grant_jungong(state, u, ally)
            # 行迹3: 战技后自身+军功者SPD+20 3回合（v6.6c: 防重入 + 记录受buff者, 到期回减正确对象）
            if not u.extra.get('cerydra_spd_buff_turns'):
                u.base_stats.SPD += 20
                ally.base_stats.SPD += 20
                u.extra['cerydra_spd_buff_ally'] = ally.char.id
            u.extra['cerydra_spd_buff_turns'] = 3
            if u.eidolon_rank >= 1:
                _gain_energy(ally, 2.0, state=state)
    # 终结技: 充能+2 + 无军功者→队伍第一 + 附加重置
    if skill_key == 'ultimate':
        u.extra['cerydra_charge'] = min(8, u.extra.get('cerydra_charge', 0) + 2)
        # v6.8.1: 军功者死亡也算无军功者（_cerydra_jungong_target 校验存活）
        if not _cerydra_jungong_target(state, u):
            first = min(state.units, key=lambda x: getattr(x, 'position', 99))
            if first is not u:
                _cerydra_grant_jungong(state, u, first)
        u.extra['cerydra_fua_count'] = 0
    return None


def _cerydra_settle_jungong_attack(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_jungong_attack: 军功者攻击后60%ATK风附加（20次/终结技重置）。"""
    from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _gain_energy
    from engine.core.damage import calculate_damage
    from engine.runtime import _enemy_for_damage
    # 刻律德菈天赋: 军功者攻击后60%ATK风附加（20次/终结技重置）
    if not u.extra.get('cerydra_jungong') or total_dmg <= 0:
        return None
    cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
    if cery:
        cnt = cery.extra.get('cerydra_fua_count', 0)
        if cnt < 20:
            cery.extra['cerydra_fua_count'] = cnt + 1
            _gain_energy(cery, 5.0, state=state)  # v6.6c: 行迹3 军功者攻击回能5点（模块级函数, 勿局部import）
            stats = _build_effective_stats(cery, state)
            for t in (state.alive_enemies() or state.enemies):
                if getattr(t, 'HP', 0) > 0:
                    attached_scale = 60.0 * (3.0 if cery.eidolon_rank >= 6 else 1.0)
                    d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, attached_scale,
                                         'direct', '风', 80, stats.CRIT_RATE >= 0.5,
                                         crit_mode='expected')
                    _commit_enemy_damage(state, cery, t, d.final_damage)
                    cery.total_damage_dealt += d.final_damage
            state.log.append(f'  刻律德菈附加: {attached_scale:.0f}%ATK风 ({cnt+1}/20)')
    return None


def _cerydra_settle_jungong_qixi(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_jungong_qixi: 奇袭——爵位者战技后消耗6充能降回军功。"""
    # v6.6c: 奇袭——爵位者战技后消耗6充能降回军功
    if skill_key == 'skill' and u.extra.get('cerydra_juewei'):
        cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if cery and cery.extra.get('cerydra_juewei_target') == u.char.id:
            _cerydra_qixi(state, cery, u)
    return None


def _cerydra_settle_jungong_ult(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_jungong_ult: 军功者终结技→充能+1（行迹2, 1次/场）。"""
    # 刻律德菈军功者终结技→充能+1（行迹2, 1次/场）
    if u.extra.get('cerydra_jungong') and skill_key == 'ultimate':
        cery = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if cery and not cery.extra.get('cerydra_trace2_used'):
            cery.extra['cerydra_trace2_used'] = True
            cery.extra['cerydra_charge'] = min(8, cery.extra.get('cerydra_charge', 0) + 1)
            _cerydra_check_promote(state, cery, u)
            state.log.append('  行迹·见者: 军功者终结技→充能+1')
    return None


SETTLE_HANDLERS = {'settle_self': _cerydra_settle_self,
                   'settle_jungong_attack': _cerydra_settle_jungong_attack,
                   'settle_jungong_qixi': _cerydra_settle_jungong_qixi,
                   'settle_jungong_ult': _cerydra_settle_jungong_ult}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_lvfa(state, summoner, ms_unit, cerydra):
    """献予「律法」之诗(整场, 刻律德菈): 军功者暴伤+30%; 奇袭结束后充能+1"""
    cerydra.extra['poem_lvfa'] = True
    state.log.append('  献予「律法」之诗: 军功者暴伤+30%+奇袭后充能+1')


POEM = ("律法", _poem_lvfa, True)
