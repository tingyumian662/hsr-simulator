"""战斗模拟器 v4 — 通用回合制引擎

命途机制（欢愉/记忆等通用子系统）按队伍组成条件激活；角色专属逻辑全部在
engine/characters/ 各角色模块（相位表单轨注册, activate() 每局注入），引擎本体
只负责调度与通用结算。
"""
import copy
import random
from dataclasses import dataclass, field
from engine.models.character import Character
from engine.models.enemy import Enemy, EnemyStatus
from engine.models.elation import ElationBattleState
from engine.core.attributes import compute_combat_stats, CombatStats
from engine.core.damage import calculate_damage
from engine.core.team_mechanics import TeamMechanics
from engine.hooks.base import HookRegistry
from engine.constants import WEAKNESS_ELEMENTS
from engine.systems.timeline_marker import TimelineMarker, TimelineMarkerSystem
from engine.runtime import (AV_PER_TURN, ENERGY_GAIN, DEFAULT_HP, INITIAL_SP, MAX_SP,
                            TimedBuff, PlayerStatus, SimUnit, SimState, CharacterAsTarget,
                            _next_av, _select_targets, _enemy_for_damage, _set_av, _stamp_av_key)

# ---- 常量 ----


# ---- 战斗单元 ----


def _tick_buffs(unit) -> list:
    """倒计时并清理过期 buff，返回被移除的 buff 列表"""
    expired = []
    for b in unit.buffs:
        # These effects are measured by the applier's turn or an independent
        # timeline marker. Beneficiary turns must not consume their duration.
        if getattr(b, 'param_id', '') in {
                'sunday_mentor_cd', 'ruanmei_xianyin', 'ruanmei_field',
                'robin_skill', 'robin_concert'}:
            continue
        # remaining_turns < 0 表示永久；不得在常规回合边界被递减或移除。
        if b.remaining_turns < 0:
            continue
        b.remaining_turns -= 1
        if b.remaining_turns <= 0:
            expired.append(b)
    for b in expired:
        unit.buffs.remove(b)
        if getattr(b, 'param_id', '') == 'sunday_cr':
            unit.extra.pop('sunday_cr_stacks', None)
    if unit.extra.pop('pioneer_double_pending', False):
        unit.extra['pioneer_double_turns'] = 1
    elif unit.extra.get('pioneer_double_turns', 0) > 0:
        unit.extra['pioneer_double_turns'] -= 1
    return expired


def _gain_energy(u: SimUnit, amt: float, *, state=None, percent: bool = False,
                 apply_regen: bool = True) -> float:
    """统一回能入口（v5.0 P2）: 普通回能吃 ENERGY_REGEN 倍率, 百分比回能不吃。

    percent=True: 按能量上限百分比回（藿藿终结技队友回20%等, 实机不吃充能绳）。
    返回实际到账量（受上限截断; 溢出即浪费为实机语义, 无上限突破机制）。
    v6.10: 特殊能量角色(energy_type=special)无能量系统——残梦/飞黄/火种等走各自进度,
    通用回能对其 no-op（防止白厄秘技全队25能把 max_energy 当上限截断污染进度）。
    """
    if getattr(u.char, 'energy_type', 'regular') == 'special':
        return 0.0
    if percent:
        raw = (u.char.max_energy or 0) * amt
    else:
        er = ((_build_effective_stats(u, state).ENERGY_REGEN
               if state else u.base_stats.ENERGY_REGEN)
              if apply_regen else 1.0)
        raw = amt * er
    cap = u.char.max_energy or 999
    # v6.11.1 晴歌E6: Fever期间终结技可储存2次(能量上限×2, 放一次扣140保留溢出)
    if u.char.id == 'robin_summeretto' and u.eidolon_rank >= 6 \
            and u.extra.get('qingge_fever'):
        cap = cap * 2
    before = u.current_energy
    u.current_energy = min(cap, u.current_energy + raw)
    gained = u.current_energy - before
    # v6.9 千冶·刃行迹1·百炼骨: 溢出能量最多积攒80（终结技后恢复）
    if u.char.id == 'qianye' and raw > gained and state is not None:
        u.extra['qianye_overflow'] = min(80.0, u.extra.get('qianye_overflow', 0.0) + (raw - gained))
    if state is not None and gained > 0:
        # 千冶·刃行迹1: 能量恢复至上限时解除自身所有负面效果。
        if u.char.id == 'qianye' and u.current_energy >= (u.char.max_energy or 0) \
                and any(getattr(t, 'hook_name', '') == 'qianye_trace1'
                        for t in (u.char.traces or [])) and u.statuses:
            removed = list(u.statuses)
            u.statuses.clear()
            for status in removed:
                state.hooks.trigger_all('on_exit_state', u=u, state=state, status=status)
            state.log.append('  千冶·刃行迹1: 能量满, 解除自身所有负面效果')
        # v5.7: 开拓者·记忆天赋——我方全体每累计恢复10点能量→迷迷+1%充能（全渠道统一:
        # 施放技能/受击/光锥/藿藿终结技等回能一律经此, 此前只统计施放者自身技能回能）
        # v7.17.0: 该内联分支迁开拓者·记忆角色模块（观察者相位 energy_gain_bank,
        # 原位置派发→on_energy_change 之前, 时序不变; v7.16.0 验收 P3-2）
        _ensure_phase_tables(state)
        _obs_phase(state, 'energy_gain_bank', u, gained=gained)
        # v5.0 P8: on_energy_change 事件（此前声明零触发）
        state.hooks.trigger_all("on_energy_change", u=u, state=state, amount=gained)
    return gained


def _gain_skill_points(state, amount: int = 1) -> int:
    """Recover team skill points and notify effects that count recovery events.

    Recovery-triggered effects count each requested point even when the shared
    resource is already capped, matching effects whose text explicitly includes
    overflow.
    """
    amount = max(int(amount), 0)
    before = state.skill_points
    state.skill_points = min(state.max_sp, state.skill_points + amount)
    for _ in range(amount):
        for owner in state.units:
            lc = getattr(owner, 'lightcone', None)
            if owner.is_alive and lc and lc.id == 'earthly_escapade' \
                    and lc.path == owner.char.path:
                _lc_masquerade_caiyan(state, owner)
    return state.skill_points - before


# ---- 通用辅助 ----


def _record_lc_attack_target(state, target) -> None:
    """Record each concrete enemy hit by the current attack, preserving hit order."""
    targets = state.extra.setdefault('lc_attack_target_refs', [])
    if target not in targets:
        targets.append(target)


def _lc_attacked_enemies(state) -> list:
    """Return living enemies hit by the current attack, with a single-target fallback."""
    if 'lc_attack_target_refs' in state.extra:
        return [t for t in state.extra.get('lc_attack_target_refs', [])
                if t in state.enemies and t.HP > 0]
    alive = state.alive_enemies() or state.enemies
    return alive[:1]


def _record_enemy_kill(state):
    """v6.6b P1-3: 每局敌方目标击杀累计（跨波, 白厄E1变身速度比例消费）。
    所有击杀检测点（直伤/弹射/忆灵/反击/DOT）必须调用, 统一口径。"""
    state.extra['killed_total'] = state.extra.get('killed_total', 0) + 1

def _record_kill_after_damage(state, u, target, before) -> bool:
    """v6.8.3: 手写伤害路径的统一击杀管线——killed_this_action / killed_total /
    on_kill / 光锥击杀事件。返回是否本次伤害造成击杀。"""

    from engine.characters.acheron import _acheron_jizhen_transfer
    if before <= 0 or getattr(target, 'HP', 0) > 0:
        return False
    if getattr(target, 'extra', {}).get('_kill_recorded', False):
        return False
    state.extra['killed_this_action'] = state.extra.get('killed_this_action', 0) + 1
    if hasattr(target, 'extra'):
        target.extra['_kill_recorded'] = True
    _record_enemy_kill(state)
    state.hooks.trigger(getattr(getattr(u, 'char', None), 'id', ''), "on_kill",
                        u=u, state=state, enemy=target)
    if u is not None:
        _process_lc_effects(u, state, "on_kill")
    if any(x.char.id == 'acheron' and x.is_alive for x in state.units):
        _acheron_jizhen_transfer(state)
    return True


def _commit_enemy_damage(state, u, target, amount, *, record_hit=True,
                         damage_type=None, skill_type=None, attack_type=None,
                         cipher_record_multiplier=1.0,
                         cipher_extra_rate=0.0,
                         cipher_record_amount=None,
                         record_cipher=True):
    """提交一次已计算的敌方伤害，并统一处理命中与击杀事件。

    调用方仍负责把返回值计入自己的动作总伤害；这样可以迁移附加伤害、DOT
    和弹射路径而不改变现有总伤害统计。已死亡目标不会再次触发击杀事件。
    """

    from engine.characters.cipher import _cipher_record
    amount = max(float(amount or 0.0), 0.0)
    if target is None or amount <= 0.0 or getattr(target, 'HP', 0.0) <= 0.0:
        return 0.0, False
    before = target.HP
    target.HP = max(0.0, target.HP - amount)
    actual_damage = before - target.HP
    if state is not None and record_hit:
        _record_lc_attack_target(state, target)
        state.extra.setdefault('last_attack_targets', [])
        if target not in state.extra['last_attack_targets']:
            state.extra['last_attack_targets'].append(target)
    killed = _record_kill_after_damage(state, u, target, before) if state is not None else False
    if state is not None:
        # 旧调用默认是常规伤害；所有真伤入口必须显式标注，避免漏记自定义伤害。
        if (record_cipher and u is not None and actual_damage > 0
                and damage_type not in {'true', 'true_damage'}):
            cp = next((x for x in state.units
                       if x.char.id == 'cipher' and x.is_alive), None)
            if cp is not None:
                record_damage = actual_damage
                if cipher_record_amount is not None:
                    record_damage = min(record_damage,
                                        max(float(cipher_record_amount), 0.0))
                _cipher_record(state, cp, target, record_damage,
                               rate_multiplier=cipher_record_multiplier,
                               extra_rate=cipher_extra_rate)
        # damage 是实际 HP 损失；请求值另由 submitted_damage 提供。
        state.hooks.trigger_all("on_after_damage", u=u, state=state, enemy=target,
                                damage=actual_damage,
                                submitted_damage=amount,
                                damage_type=damage_type,
                                skill_type=skill_type,
                                attack_type=attack_type,
                                killed=killed)
    return actual_damage, killed


def _target_attacker_stats(stats, u, state, target, skill_type=None):
    """Apply target-dependent attacker stats without mutating the base panel."""
    if u is None or target is None:
        return stats
    result = stats
    if u.char.id == 'welt' and u.eidolon_rank >= 6 \
            and skill_type in ('skill', 'ultimate') \
            and (target.has_status(status_id='welt_slow') or target.has_status(name='减速')):
        result = copy.deepcopy(result)
        result.CRIT_RATE += 0.30
        result.CRIT_DMG += 0.60
    if u.char.id == 'acheron' and u.eidolon_rank >= 1 \
            and target.debuff_count() > 0:
        if result is stats:
            result = copy.deepcopy(result)
        result.CRIT_RATE = min(1.0, result.CRIT_RATE + 0.18)
    if u.char.id == 'acheron' and u.eidolon_rank >= 6 \
            and skill_type == 'ultimate':
        if result is stats:
            result = copy.deepcopy(result)
        result.RES_PEN_ALL += 0.20
    if u.char.id == 'anaxa' and getattr(target, 'extra', {}).get('anaxa_revealed'):
        if result is stats:
            result = copy.deepcopy(result)
        result.DMG_BONUS_ALL += 0.30 if u.eidolon_rank >= 6 else 0.18
    if u.char.id == 'anaxa' and u.extra.get('anaxa_trace3'):
        weak_count = len(_enemy_weakness_elements(target))
        if weak_count:
            if result is stats:
                result = copy.deepcopy(result)
            result.DEF_PEN += min(0.28, weak_count * 0.04)
    return result


def _target_scaling_stat(stats, value, stat_name, state, target):
    """Apply target-dependent scaling-stat bonuses for the current hit."""
    if stat_name != 'ATK' or state is None or target is None or not target.is_broken:
        return value
    ruan = next((x for x in state.units
                 if x.char.id == 'ruan_mei' and x.is_alive and x.eidolon_rank >= 2), None)
    if ruan is None:
        return value
    return value + stats._base_ATK * 0.40


def _skill_attack_type(state, skill_type):
    """千冶·刃E2 makes allied ultimate damage count as follow-up damage."""

    from engine.characters.qianye import _qianye_wrath_active
    if skill_type != 'ultimate' or state is None:
        return None
    qianye = next((x for x in state.units
                   if x.char.id == 'qianye' and x.is_alive and x.eidolon_rank >= 2), None)
    return 'follow_up' if qianye is not None and _qianye_wrath_active(qianye) else None


def _multihit_damage(stats: CombatStats, targets: list, scaling_stat: float,
                      scale: float, dmg_type: str, element: str, is_crit: bool,
                      hits: int = 1, skill_type: str = None,
                      true_dmg_ratio: float = 0.0, u=None, state=None,
                      scaling_stat_name: str = None, attack_type: str = None,
                      laugh_n: float = 0.0) -> float:

    from engine.characters.seele import _apply_luandie
    from engine.characters.welt import _welt_apply_slow, _welt_talent_hit
    total = 0.0
    hit_targets = []
    for _ in range(hits):
        # v6.2.1: 每段重新选择存活目标（Codex P1-1: 固定列表会继续命中已死亡目标）
        alive_now = [e for e in targets if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        hit_targets.append(t)
        was_welt_slow = bool(
            u is not None and u.char.id == 'welt'
            and (t.has_status(status_id='welt_slow') or t.has_status(name='减速'))
        )
        if state is not None:
            _record_lc_attack_target(state, t)
        if state is not None and u is not None:
            from engine.characters.himeko_nova import _hn_count_hits
            _hn_count_hits(state, u)  # v6.7 歼破协议: 每段命中+1充能
        hit_stats = _apply_target_relic_modifiers(stats, u, t) if u is not None else stats
        # v6.7b 歼破协议: 战技弹射段暴击伤害额外+100%
        if skill_type == 'skill' and state is not None \
                and state.extra.get('hn_charge_skill_cd'):
            hit_stats = copy.deepcopy(hit_stats)
            hit_stats.CRIT_DMG += 1.0
        # v5.0 P3 补: 弹射路径同样应用目标相关光锥条件（星海巡航 HP≤50% 等）
        if u is not None and state is not None:
            hit_stats = _lc_target_correct(hit_stats, u, state, t)
        # v6.3.0 银狼E6: 目标每有1个负面效果伤害+20%, 最多+100%
        if u is not None and u.char.id == 'silver_wolf' and u.eidolon_rank >= 6:
            n = min(getattr(t, 'debuff_count', lambda: 0)(), 5)
            if n > 0:
                hit_stats = copy.deepcopy(hit_stats)
                hit_stats.DMG_BONUS_ALL += 0.20 * n
        hit_stats = _target_attacker_stats(hit_stats, u, state, t, skill_type)
        hit_scaling_stat = _target_scaling_stat(
            stats, scaling_stat, scaling_stat_name, state, t)
        hit_enemy = _enemy_for_damage(t)
        d = calculate_damage(hit_stats, hit_enemy, hit_scaling_stat, scale, dmg_type, element, 80,
                             hit_stats.CRIT_RATE >= 0.5 if u is not None else is_crit,
                             skill_type=skill_type, true_dmg_ratio=true_dmg_ratio,
                             attack_type=attack_type, laugh_n=laugh_n,
                             crit_mode="expected" if u is not None else "boolean")
        total += d.final_damage
        _, killed_by_hit = _commit_enemy_damage(
            state, u, t, d.final_damage, damage_type=dmg_type,
            skill_type=skill_type, attack_type=attack_type,
            cipher_record_amount=(d.final_damage / (1.0 + true_dmg_ratio)
                                  if true_dmg_ratio > 0 else None))
        if u is not None and state is not None:
            # v6.2.1: 逐段对齐 _use_skill 管线（Codex P1-1）——声援用实际伤害,
            # 乱蝶结算 + 击杀事件/光锥/倒置的火炬（此前弹射全漏, 且死亡目标仍被打）
            from engine.characters.trailblazer_remembrance import _apply_tbr_support
            total += _apply_tbr_support(state, u, t, d.final_damage)
            _apply_luandie(state, t, u)
            if killed_by_hit:
                # v5.1: 遐蝶行迹2·倒置的火炬 — 乌黯击杀→死龙速度+100%/1回合
                if u.char.id == 'xiadie' and u.memsprite_unit and u.memsprite_unit.is_alive:
                    u.memsprite_unit.extra['xiadie_spd_boost'] = 1
                    state.log.append('  倒置的火炬: 死龙速度+100%(1回合)')
            # 瓦尔特战技逐段语义: 当段先判断原有减速触发天赋，
            # 再以75%基础概率施加/刷新减速，故首次命中后从第2段开始触发。
            if u.char.id == 'welt' and skill_type == 'skill' and t.HP > 0:
                if was_welt_slow:
                    total += _welt_talent_hit(state, u, t, hit_stats, skill_type)
                if t.HP > 0:
                    _welt_apply_slow(state, u, t)
    if state is not None:
        state.extra['last_multihit_targets'] = hit_targets
    return total


def _target_damage(stats: CombatStats, targets: list, scaling_stat: float,
                   scale: float, dmg_type: str, element: str, is_crit: bool,
                   skill_type: str = None) -> float:
    return sum(calculate_damage(stats, t, scaling_stat, scale, dmg_type, element, 80, is_crit,
                                skill_type=skill_type).final_damage
               for t in targets)


def _apply_target_relic_modifiers(stats, u, enemy):
    """按当前受击目标应用动态遗器属性，不污染基础面板。"""
    if u is None:
        return stats
    # v5.4 烦恼着，幸福着: 目标【温驯】状态下我方任意攻击者CD+12%/层（最多2层）
    # 温驯 status 只由该光锥施加 → 有温驯即佩戴了光锥, 无条件对所有命中者生效
    wenshun = next((st.attributes.get('wenshun_layers', 0) for st in enemy.statuses
                    if st.id == 'wenshun'), 0)
    if wenshun:
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 0.12 * wenshun
    conditions = getattr(u, '_active_relic_conditions', set())
    if not conditions:
        return stats
    result = copy.deepcopy(stats)
    if 'defpen_per_dot' in conditions:
        result.DEF_PEN += min(enemy.dot_count(), 3) * 0.06
    # v5.2 问题3e: 繁星4pc 量子弱点目标额外10%无视防御（非量子弱点不触发）
    if 'defpen_vs_quantum' in conditions and enemy.element_res.get('量子', 0.2) <= 0:
        result.DEF_PEN += 0.10
    if 'cr_vs_debuff' in conditions and enemy.debuff_count() > 0:
        result.CRIT_RATE = min(1.0, result.CRIT_RATE + 0.10)
        if enemy.has_status(name='禁锢') or enemy.has_status(status_id='break:虚数'):
            result.CRIT_DMG += 0.20
    if 'cd_per_debuff_count' in conditions:
        count = enemy.debuff_count()
        bonus = 0.12 if count >= 3 else (0.08 if count >= 2 else 0.0)
        if u.extra.get('pioneer_double_turns', 0) > 0:
            bonus *= 2
        result.CRIT_DMG += bonus
    return result


# ---- 遐蝶天赋：HP吸收（新蕊/死龙回血） ----


# ---- 遗器条件 ----

def _get_relic_conditions(relics, relic_sets) -> set:
    if not relics or not relic_sets:
        return set()
    set_counts = {}
    for p in relics:
        set_counts[p.set_name] = set_counts.get(p.set_name, 0) + 1
    conds = set()
    for set_name, count in set_counts.items():
        if set_name not in relic_sets:
            continue
        for eff in relic_sets[set_name].effects:
            if count < eff.pieces_required or not eff.condition:
                continue
            # v5.1: 拆解组合码（'a+b' 防数据合并错误导致无法触发）
            for part in eff.condition.split('+'):
                conds.add(part.strip())
    return conds


# ---- Buff 注册表 ----
# 格式: {paramId: {stat: value, ...}}
# value 使用游戏内百分比数值（如 66 = 66%），引擎自动 /100

BUFF_REGISTRY = {
    # 希儿
    "seele_speed_buff": {"SPD_PERCENT": 25.0},
    "seele_buffed_state": {"DMG_BONUS_ALL": 80.0},
    # 布洛妮娅
    "bronya_skill_dmg_buff": {"DMG_BONUS_ALL": 66.0},
    "bronya_ult_buff": {"ATK_PERCENT": 55.0},  # CD部分特殊处理
    "bronya_technique_atk": {"ATK_PERCENT": 15.0},
    # 花火
    "sparkle_cd_buff": {},  # CD=花火CD*0.24+45, 动态计算
    # v6.10.6 C1: 谜诡改为真实3回合状态（_apply_skill_effects 特判挂 TimedBuff）, 不再满层近似DMG+60
    "sparkle_ult_buff": {},
    # 符玄
    "fuxuan_field": {"HP_PERCENT": 6.0, "CRIT_RATE": 12.0},
    "fuxuan_ult_trigger": {},
    "fuxuan_tech_barrier": {},
    # 长夜月
    "changyeyue_ult_state": {"_darkness": 1},  # 特殊标记，空属性触发至暗之谜状态
    "changyeyue_skill_cd": {"CRIT_DMG": 24.0},
    "changyeyue_tech_cd": {"CRIT_DMG": 24.0},
    # 遐蝶
    "xiadie_realm": {"_realm": 1},
    "xiadie_tech": {"_tech": 1},
    # 昔涟
    "xilian_field": {"_field": 1},
    "xilian_ult_ripple": {"_ripple": 1},
    "xilian_tech": {"_tech": 1},
    # 风堇
    "fengjin_ult_state": {"_clear_sky": 1},
    "fengjin_tech": {"_tech": 1},
    # 阿格莱雅
    "aglaea_sovereign": {"_sovereign": 1},
    # 藿藿
    "huohuo_ult_atk": {"ATK_PERCENT": 40.0},  # 终结技队友ATK(行迹控抗精通: ≥160能量队友64%)
    # v5.3 开拓者·同谐
    "tbh_band_dance": {"BREAK_EFFECT": 30.0, "_tbh_super_break": 1},  # 伴舞: 击破特攻+30%+超击破源
    # v5.3 忘归人
    "fugue_foxian": {"BREAK_EFFECT": 30.0, "_foxian": 1},   # 狐祈: 击破特攻+30%+狐祈标记
    "fugue_chizhuo": {"_chizhuo": 1},                       # 炽灼: 强化普攻切换标记
    # v5.3 流萤
    "firefly_combustion": {"_combustion": 1},               # 完全燃烧状态（_apply_skill_effects 特殊分支）
    # v6.7 火花
    "sparxie_live": {"_sparxie_live": 1},                   # 直播连线（下次普攻强化, 一次性）
    "sparxie_e2_cd": {"CRIT_DMG": 10.0},                    # E2: 每消耗1爆点暴伤+10% 2回合(叠4层, 消耗处动态叠加)
    "sparxie_e4_elation": {"ELATION_LEVEL": 36.0},          # E4: 终结技后自身欢愉度+36% 3回合
    # v6.7 大丽花
    "dahlia_field_buff": {"TOUGHNESS_EFFICIENCY": 50.0},    # 结界: 全队弱点击破效率+50%
    # v6.7 姬子·启行
    "himeko_nova_flag": {"DMG_BONUS_ALL": 20.0},            # 领航旗语: 全队伤害+20%
    # v6.11.1 知更鸟·晴歌
    "qingge_guest": {},                                      # 特邀嘉宾: 特殊标记(持有者及其召唤物攻击→晴歌气氛+2; 无法拉条队友)
}

# 命名 paramId 治疗注册表（藿藿等; 数字编码 "hpPct|flat" 走 split 解析回退）
HEAL_REGISTRY = {
    "huohuo_skill_heal_main":     {"hp_pct": 24.0, "flat": 640.0},
    "huohuo_skill_heal_adjacent": {"hp_pct": 19.2, "flat": 512.0},
    # v5.3 灵砂（ATK 基数治疗, 满级档; 行迹2 治疗量提高经 HEAL_BONUS 消费）
    "lingsha_skill_heal":  {"stat": "ATK", "hp_pct": 14.0, "flat": 420.0},
    "lingsha_ult_heal":    {"stat": "ATK", "hp_pct": 12.0, "flat": 360.0},
    "lingsha_fuyuan_heal": {"stat": "ATK", "hp_pct": 12.0, "flat": 360.0},
}

DEBUFF_REGISTRY = {
    # v5.6: base_chance = 实机基础概率（默认1.0=100%）, 由 _roll_effect_hit 检定
    "huohuo_tech_atk_down": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                             "attributes": {"ATK_DOWN": 0.25}},
    "凶星低语": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                "attributes": {"vulnerability": 0.16}},
    # v5.3 灵砂【醇醉】: 受击破伤害+25%（per-type 易伤, damage.py _vuln_mult 消费）
    "lingsha_chunzui": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                        "attributes": {"vulnerability_break": 0.25}},
    "hysilens_vuln": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                       "attributes": {"vulnerability": 0.20}},
    "cipher_weak": {"category": "debuff", "duration": 2, "base_chance": 1.20,
                    "attributes": {"outgoing_dmg_reduction": 0.10}},
    # v5.3 流萤火弱点植入（通用弱点机制: weakness_element → 立即改抗性, 到期恢复）
    # 弱点植入属元素机制不走 EHR 检定（_apply_skill_effects 内跳过）
    "firefly_fire_weakness": {"category": "debuff", "duration": 2, "base_chance": 1.0,
                              "attributes": {"weakness_element": "火", "weakness_old_res": 0.0}},
    # v6.3.0 银狼终结技: 全敌DEF-45% 3回合（满级120%基础概率必中）
    "silver_wolf_def_down": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                             "attributes": {"def_reduction": 0.45}},
    # v6.7 绯英行迹1·行裁断: 狐狸老师施放攻击→全敌易伤12% 3回合
    "evanescia_vuln": {"category": "debuff", "duration": 3, "base_chance": 1.0,
                       "attributes": {"vulnerability": 0.12}},
    # v6.7 大丽花终结技·败谢: 防御-18% + 共舞者属性弱点（弱点动态, _apply_skill_effects 特判）
    "the_dahlia_baisie": {"category": "debuff", "duration": 4, "base_chance": 1.0,
                          "attributes": {"def_reduction": 0.18}},
}

MANUAL_BUFF_PARAM_IDS = {
    'cerydra_jungong',
    'dht_tongpao',
    'res_pen_buff',
    'tribbie_shenqi',
    'yaoguang_field',
}

SPECIAL_DEBUFF_PARAM_IDS = {
    'silver_wolf_all_res_down',
    'silver_wolf_weakness',
}


# ---- v5.3 行动条标记行动分发（marker_id → fn(state, marker)）----

# ════════ v6.3.0 银狼机制（角色技能介绍/银狼.txt）════════
# v6.3.0b P1-7/P1-12: (消费键=小写, 数值, 独立status id, 显示名) ——
# 独立 id 使三类缺陷并存（此前固定 id 互相覆盖, debuff 计数被低估）


def _pick_fire_weak_target(pool):
    """优先选择韧性值>0且有火属性弱点的目标"""
    if not pool:
        return None
    fire_weak = [t for t in pool if t.toughness > 0 and t.element_res.get('火', 0.2) <= 0]
    return random.choice(fire_weak) if fire_weak else random.choice(pool)


def _marker_heal_allies(state, healer, heal_id):
    """行动条标记治疗（HEAL_REGISTRY 命名, ATK/HP 基数, 含 HEAL_BONUS）"""
    named = HEAL_REGISTRY.get(heal_id)
    if not named:
        return 0.0
    stats = _build_effective_stats(healer, state)
    heal_base = stats.ATK if named.get("stat") == "ATK" else stats.HP
    amt = (heal_base * named["hp_pct"] / 100 + named["flat"]) * (1.0 + stats.HEAL_BONUS)
    tgt_list = [x for x in state.units if x.is_alive] + \
               [ms for ms in state.memsprites if ms.is_alive]
    for t in tgt_list:
        t.current_hp = min(t.max_hp, t.current_hp + amt)
    state.hooks.trigger_all("on_heal", u=healer, state=state, healer=healer,
                            targets=tgt_list, heal_amt=amt)
    from engine.characters.fengjin import _fengjin_talent_heal_buff
    _fengjin_talent_heal_buff(state, healer)
    # 行动条标记治疗与角色技能治疗使用同一光锥事件管线。
    state.extra['lc_last_heal_amt'] = amt
    _process_lc_effects(healer, state, "on_heal")
    state.log.append(f'  治疗: {amt:.0f}×{len(tgt_list)}人')
    return amt


def _pick_single_ally_target(state, u):
    """单体友方技能目标解析（v5.6）:
    优先携带 single_ally_priority 标记的存活角色（船长4pc持有者, 需被队友选中叠Help）
    → 默认主C seele → 施放者自身。数据驱动, 未来嘲讽角色复用同标记。"""
    for x in state.units:
        if x.is_alive and x.extra.get('single_ally_priority'):
            return x
    seele = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if seele is not None:
        return seele
    return next((x for x in state.units if x.is_alive and x is not u), u)


def _roll_effect_hit(u, state, enemy, effect_name, base_chance=1.0):
    """玩家→敌方 debuff 命中检定（v5.6）:
    最终命中 = 基础概率 × (1 + 效果命中) × (1 - 敌方效果抵抗)
    enemy.effect_res 默认 0（无数据=必中, 真实敌人数据录入后激活）。
    击破异常不检定（实机弱点击破必中, 见 _apply_break_debuff）。
    防御: 无 base_stats 的 mock 单元按 EHR=0 处理（测试构造）。"""
    ehr = 0.0
    if getattr(u, 'base_stats', None) is not None:
        ehr = _build_effective_stats(u, state).EFFECT_HIT_RATE
    chance = base_chance * (1.0 + ehr) * (1.0 - enemy.effect_res)
    if chance >= 1.0:
        return True
    if random.random() >= chance:
        state.log.append(f'  抵抗: {enemy.name or enemy.id} 抵抗{effect_name} (命中{chance:.0%})')
        return False
    return True


# ---- M5a: 角色包相位派发 ----
# 表由 engine.characters.build_phase_tables 每局注入（simulate 经 activate; 直调入口惰性自举）。
# 处理器体为原引擎内联分支 verbatim, 契约逐相位注明于派发点; 变形类一律 `is None` 收接。


def _ensure_phase_tables(state):
    if not state._phase_tables_ready:
        from engine.characters import build_phase_tables
        build_phase_tables(state)


def _char_phase(state, u, phase, **ctx):
    """actor 相位：只派发给行动者角色自己的处理器，返回其结果或 None。

    对无 .char.id 的单位（忆灵/测试 mock）安全返回 None。"""
    cid = getattr(getattr(u, 'char', None), 'id', None)
    if cid is None:
        return None
    fn = state.char_phases.get(cid, {}).get(phase)
    return fn(u, state, **ctx) if fn else None


def _obs_phase(state, phase, u, **ctx):
    """观察者相位：按注册序派发，返回首个非 None 结果。"""
    for fn in state.observer_phases.get(phase, ()):
        res = fn(u, state, **ctx)
        if res is not None:
            return res
    return None


def _turn_ticks(state, zone, u):
    for fn in state.turn_ticks.get(zone, ()):
        fn(u, state)


def _settle_pipeline(state, u, skill, skill_key, total_dmg):
    """技能后结算管线（v6.6 批1-3; 顺序锁死于 characters.SETTLE_PIPELINE_ORDER）。"""
    for fn in state.settle_pipeline:
        fn(u, state, skill, skill_key, total_dmg)


def _apply_skill_effects(u: SimUnit, state: SimState, skill, skill_key: str):
    """将技能的 effects[] 转化为 TimedBuff 应用到目标"""
    _ensure_phase_tables(state)


    single_ally_target = None
    has_single_ally_target = getattr(skill, 'target', None) == 'single_ally' or any(
        (getattr(eff, 'target', None) if not isinstance(eff, dict)
         else eff.get('target')) == 'single_ally'
        for eff in skill.effects
    )
    if has_single_ally_target:
        single_ally_target = _pick_single_ally_target(state, u)
        u.extra['lc_last_skill_target'] = single_ally_target
        state.hooks.trigger_all("on_ally_skill_targeted", u=u, state=state,
                                target=single_ally_target, skill_key=skill_key)

    for eff in skill.effects:
        etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')

        # 忆灵召唤：通用处理器
        if etype == 'summon_memsprite':
            if u.char.memsprite:
                from engine.systems.remembrance import RemembranceSystem
                if state.extra.get('_rem_sys') is None:
                    state.extra['_rem_sys'] = RemembranceSystem()
                state.extra['_rem_sys'].summon_memsprite(state, u, u.char.memsprite)
            continue

        # v5.3 行动条标记（浮元/完全燃烧倒计时）: 惰性创建系统
        if etype == 'spawn_marker':
            sys = _ensure_marker_system(state)  # v6.3.0b P1-1: 统一惰性创建入口
            param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
            sys.spawn(state, u, param_id)
            continue

        # v5.3 行动提前（标记提前/自身提前, 首次接线）
        if etype == 'action_advance':
            target = eff.target if hasattr(eff, 'target') else eff.get('target', 'self')
            ratio = (eff.value if hasattr(eff, 'value') else eff.get('value', 0)) / 100.0
            if target == 'marker':
                sys = state.extra.get('_marker_sys')
                if sys:
                    sys.advance(state, u, ratio)
            elif target == 'self':
                navs = state.extra.get('navs', {})
                uidx = state.units.index(u) if u in state.units else -1
                if uidx >= 0 and uidx in navs:
                    navs[uidx] = max(0, navs[uidx] - (AV_PER_TURN / _effective_spd(u, state)) * ratio)
                    state.log.append(f'  行动提前: {u.char.name} {ratio*100:.0f}%')
            elif target in ('single_ally', 'ally') and single_ally_target is not None \
                    and single_ally_target is not u:
                # v6.11.1 特邀嘉宾: 持有者无法使其他友方目标获得行动提前
                if any(getattr(b, 'param_id', '') == 'qingge_guest' for b in u.buffs):
                    state.log.append('  【特邀嘉宾】: 无法使其他友方获得行动提前')
                    continue
                navs = state.extra.get('navs', {})
                target_idx = (state.units.index(single_ally_target)
                              if single_ally_target in state.units else -1)
                if target_idx >= 0 and target_idx in navs:
                    advanced_av = max(
                        state.current_av,
                        navs[target_idx]
                        - (AV_PER_TURN / _effective_spd(single_ally_target, state)) * ratio,
                    )
                    _set_av(state, navs, target_idx, advanced_av)
                    state.log.append(
                        f'  行动提前: {single_ally_target.char.name} {ratio*100:.0f}%')
            continue

        if etype == 'debuff':
            param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
            target_type = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
            template = DEBUFF_REGISTRY.get(param_id)
            if template is None and param_id not in SPECIAL_DEBUFF_PARAM_IDS:
                state.log.append(f'  [WARN] 未注册debuff paramId={param_id or "<empty>"}, 按通用减益记录')
                template = {"category": "debuff", "duration": 2,
                            "attributes": {"value": getattr(eff, 'value', 0)}}
            targets = _select_targets(state.alive_enemies() or state.enemies, target_type)
            applied_targets = []
            for target in targets:
                # M5a: 角色专属 debuff 逐目标接管（每局注入; True=已处理并计入施加数）
                debuff_special = state.debuff_takeovers.get(param_id)
                if debuff_special is not None and debuff_special(u, state, target):
                    applied_targets.append(target)
                    continue
                attrs = template["attributes"].copy()
                # v5.6: 命中检定（弱点植入属元素机制不走 EHR, 实机必中）
                if 'weakness_element' not in attrs and not _roll_effect_hit(
                        u, state, target, param_id or skill.name, template.get("base_chance", 1.0)):
                    continue
                # v5.3 弱点植入（流萤强化战技火弱点）: 立即改抗性, 到期由 _begin_enemy_turn 恢复
                if 'weakness_element' in attrs:
                    elem = attrs['weakness_element']
                    status_id = param_id or f'{u.char.id}:{skill_key}:debuff'
                    existing = next((s for s in target.statuses if s.id == status_id), None)
                    e6_res_down = target.extra.get('lingsha_e6_res_down', 0) * 0.20
                    # 刷新同一植入状态时，保留首次施加前的基础抗性快照；
                    # 浮元E6 的全抗降低独立叠加，不能被植入效果覆盖。
                    attrs['weakness_old_res'] = (
                        existing.attributes.get('weakness_old_res', target.get_res(elem))
                        if existing and existing.attributes.get('weakness_element') == elem
                        else target.get_res(elem) + e6_res_down
                    )
                    target.element_res[elem] = min(attrs['weakness_old_res'], -0.2) - e6_res_down
                    state.log.append(f'  {elem}弱点植入 → {target.name or target.id} (2回合)')
                    # v6.7 弱点植入事件（大丽花行迹3消费）
                    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                                            element=elem, target=target)
                target.add_status(EnemyStatus(
                    id=param_id or f'{u.char.id}:{skill_key}:debuff',
                    name=param_id or skill.name,
                    category=template["category"],
                    source=u.char.id,
                    remaining_turns=template["duration"],
                    attributes=attrs,
                ))
                # v6.10 on_debuff_applied 事件（黄泉天赋消费）
                state.hooks.trigger_all("on_debuff_applied", u=u, state=state,
                                        target=target)
                applied_targets.append(target)
            if applied_targets:
                state.log.append(f'  debuff {param_id or skill.name} → {len(applied_targets)}名敌人')
                if 'cd_per_debuff_count' in getattr(u, '_active_relic_conditions', set()):
                    u.extra['pioneer_double_pending'] = True
                # v5.0.1: 施加 debuff 事件（好戏开演·戏法叠层）
                _process_lc_effects(u, state, "on_debuff_apply")
            continue

        # v5.0 P5: 护盾效果（盾值 = eff.value × (1+SHIELD_BONUS)）
        if etype == 'shield':
            t_type = eff.target if hasattr(eff, 'target') else 'self'
            if t_type in ('all_allies', 'all'):
                targets = [x for x in state.units if x.is_alive]
            elif t_type in ('single_ally', 'ally'):
                targets = [x for x in state.units if x.is_alive and x is not u][:1] or [u]
            else:
                targets = [u]
            bonus = _build_effective_stats(u, state).SHIELD_BONUS
            shield_val = float(getattr(eff, 'value', 0) or 0) * (1.0 + bonus)
            for t in targets:
                t.shield += shield_val
                state.hooks.trigger_all("on_shield", u=u, state=state,
                                        targets=[t], shield_amt=shield_val)
                # v6.11.1 晴歌行迹2·即兴蓝调(护盾侧) + 渠道b气氛（与治疗共享每回合去重）
                from engine.characters.robin_summeretto import _qingge_on_heal_shield
                _qingge_on_heal_shield(state, provider=u, targets=[t])
                state.log.append(f'  护盾: {t.char.name}+{shield_val:.0f}')
                # 隐士4pc: 持盾队友→CD+15%（受盾者佩戴时激活）
                if 'shield_ally_cd' in getattr(t, '_active_relic_conditions', set()):
                    t.buffs.append(TimedBuff(source_id='隐士4pc', attributes={'CRIT_DMG': 15.0},
                                             remaining_turns=2, source_name='隐士4pc'))
                    state.log.append(f'  隐士4pc: {t.char.name} CD+15%(持盾)')
            continue
        # v5.0 P4: 净化效果（解除目标 1 个控制/减益状态, 负属性 buff 兜底）
        if etype == 'cleanse':
            t_type = eff.target if hasattr(eff, 'target') else 'all_allies'
            if t_type in ('all_allies', 'all'):
                targets = [x for x in state.units if x.is_alive]
            elif t_type in ('single_ally', 'ally'):
                targets = [single_ally_target] if single_ally_target else []
            else:
                targets = [u]
            cleared = 0
            for t in targets:
                for st in list(getattr(t, 'statuses', [])):
                    if st.category in ('control', 'debuff'):
                        t.statuses.remove(st)
                        state.hooks.trigger_all("on_exit_state", u=t, state=state, status=st)
                        cleared += 1
                        break
                if cleared < 1:
                    for b in list(t.buffs):
                        if any(v < 0 for v in b.attributes.values()):
                            t.buffs.remove(b)
                            cleared += 1
                            break
            if cleared:
                state.log.append(f'  净化: 解除{cleared}个负面效果')
            continue
        if etype != 'buff':
            continue

        param_id = eff.param_id if hasattr(eff, 'param_id') else eff.get('paramId', '')
        if param_id == 'laugh_gain_5':
            amount = eff.value if hasattr(eff, 'value') else eff.get('value', 0.0)
            state.laugh_points += amount
            state.log.append(f'  笑点+{amount:.0f}')
            continue
        if param_id == 'cd_buff':
            if single_ally_target is not None:
                single_ally_target.tb_cd_buff_turns = 3
                state.log.append(
                    f'  暴击伤害+50% → {single_ally_target.char.name} (3回合)')
            continue
        if param_id == 'hidden_score_gain':
            amount = eff.value if hasattr(eff, 'value') else eff.get('value', 0.0)
            elation = state.extra.get('_elation')
            if elation is not None:
                elation.gain_hidden_score(state, u, amount)
            else:
                u.hidden_score = min(300.0, u.hidden_score + amount)
            continue
        if param_id in MANUAL_BUFF_PARAM_IDS:
            continue
        if param_id not in BUFF_REGISTRY:
            state.log.append(f'  [WARN] 未注册buff paramId={param_id or "<empty>"}')
            continue
        attrs = BUFF_REGISTRY[param_id].copy()

        # M5a: 角色专属 effect 处理器（每局注入）。接管=True 消费该 effect;
        # 变形=(attrs, duration) 后走通用挂载。takeover 先于 mutator 派发——
        # 与原内联顺序等价（fugue_foxian 等接管型不会落到变形路径）。
        takeover = state.effect_takeovers.get(param_id)
        if takeover is not None and takeover(u, state, skill, skill_key, eff):
            continue
        duration = 2  # 默认2回合
        mutator = state.effect_mutators.get(param_id)
        if mutator is not None:
            attrs, duration = mutator(u, state, attrs, skill)

        target_type = eff.target if hasattr(eff, 'target') else eff.get('target', 'self')

        # 解析目标
        targets = []
        if target_type == 'self':
            targets = [u]
        elif target_type == 'single_ally':
            main = single_ally_target or _pick_single_ally_target(state, u)
            targets = [main]
        elif target_type == 'all_allies' or target_type == 'all_allies_but_self':
            targets = [x for x in state.units if x.is_alive and x != u] if 'but_self' in target_type else [x for x in state.units if x.is_alive]

        for target in targets:
            # M5a: 挂载前预处理（如希儿加速同 ID 上限滚动）
            pre = state.effect_pre_apply.get(param_id)
            if pre is not None:
                pre(u, state, target)
            tb = TimedBuff(source_id=u.char.id, attributes=attrs, remaining_turns=duration,
                           source_name=skill.name, param_id=param_id)
            target.buffs.append(tb)

        if targets:
            names = ','.join(t.char.name for t in targets[:2])
            if len(targets) > 2: names += f'...(+{len(targets)-2})'
            state.log.append(f'  buff {param_id} → {names} ({duration}回合)')


# ---- 光锥效果处理器 ----

def _lc_team_advance(state, ratio, actor=None):
    """全队行动提前（各自速度）; v7.1.0: actor持【特邀嘉宾】时只拉自己(防永动机)"""
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        from engine.characters.robin_summeretto import _guest_advance_blocked
        if eu.is_alive and i in navs and not _guest_advance_blocked(state, actor, eu):
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * ratio)
    state.log.append(f'  光锥拉条: 全队{ratio*100:.0f}%')


def _lc_ally_buff(state, unit, attrs, duration):
    """给下一个行动队友加buff"""
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if target:
        tb = TimedBuff(source_id=unit.char.id, attributes=attrs, remaining_turns=duration)
        target.buffs.append(tb)
        state.log.append(f'  光锥buff → {target.char.name}({duration}回合)')


def _lc_sp_recovery(state, interval=2):
    """每N次终结技回1SP"""
    c = state.extra.get('lc_sp_counter', 0) + 1
    state.extra['lc_sp_counter'] = c
    if c >= interval:
        _gain_skill_points(state)
        state.extra['lc_sp_counter'] = 0
        state.log.append('  光锥回SP')


def _lc_wave_heal(state, ratio=0.80):
    """波次开始群体回血"""
    for u in state.units:
        if u.is_alive and u.current_hp < u.max_hp:
            lost = u.max_hp - u.current_hp
            heal = lost * ratio
            u.current_hp = min(u.max_hp, u.current_hp + heal)
            if heal > 1:
                state.log.append(f'  波次回血: {u.char.name}+{heal:.0f}HP')


# 光锥触发器注册表: {paramId: (event_type, handler)}
# event_type: "on_ult" / "on_skill" / "on_wave_start"
LC_TRIGGERS = {
    "lc_but_the_battle_isnt_over_sp": ("on_ult", lambda s, u, ctx: _lc_sp_recovery(s, 2)),
    "lc_but_the_battle_isnt_over_dmg": ("on_skill", lambda s, u, ctx: _lc_ally_buff(s, u, {'DMG_BONUS_ALL': 30.0}, 1)),
    "lc_dance_dance_dance_advance": ("on_ult", lambda s, u, ctx: _lc_team_advance(s, 0.24, actor=u)),
    "lc_she_already_shut_her_eyes_heal": ("on_wave_start", lambda s, u, ctx: _lc_wave_heal(s, 0.80)),
}

# ---- 光锥 condition 动态化（v5.0 P3） ----
# 事件型条件码 → 触发事件（_process_lc_effects 的 event_type 命名）
LC_EVENT_CODES = {
    "event_battle_start": "on_battle_start",
    "event_skill_after": "on_skill",
    "event_ult_after": "on_ult",
    "event_basic_after": "on_basic_attack",
    "event_kill": "on_kill",
    "event_wave_start": "on_wave_start",
    "event_followup": "on_followup",
    "event_hit_taken": "on_hit_taken",
    # v5.4 无用效果重审扩展
    "event_self_attack": "on_self_attack",
    "event_attack": "on_attack",
    "event_weakness_break": "on_weakness_break",
    "event_turn_start": "on_self_turn_start",
    "event_heal": "on_heal",
    "event_hp_loss": "on_hp_loss",
    "event_memsprite_attack": "on_memsprite_attack",
    "event_memsprite_despawn": "on_memsprite_despawn",
}

def _lc_rank_value(u, default, code=None):
    """v5.7: 从光锥效果 values（精炼1-5档）按当前叠影等级取值; 无数据回退默认（S1）。
    code: 按 condition_code 区分同光锥多条 values（如生命当付之一炬 弱点增伤/减防双条）"""
    eff = next((e for e in getattr(u.lightcone, 'effects', [])
                if getattr(e, 'values', None) and (code is None or e.condition_code == code)),
               None)
    if not eff or not eff.values:
        return default
    idx = min(max(getattr(u.lightcone, 'rank', 1) - 1, 0), len(eff.values) - 1)
    return eff.values[idx]


# v5.1: 无属性数据的事件型效果（回能/回血/回SP类）动作处理
# 键: (光锥id, 事件令牌) → (state, u) 动作
def _rise_and_sing_entry(state, u):
    """M4批2b delegator: 晴歌光锥【于夜色中】入场效果在角色包。"""
    from engine.characters.robin_summeretto import _rise_and_sing_entry as _entry
    return _entry(state, u)


LC_EVENT_ACTIONS = {
    # 时代铭记: 施放终结技攻击后恢复1个战技点
    ("epoch_etched_in_golden_blood", "on_ult"):
        lambda s, u: (_gain_skill_points(s),
                      s.log.append('  光锥[epoch_etched_in_golden_blood] 终结技回1SP')),
    # 当她决定看见: 每波次开始恢复15能量
    ("when_she_decided_to_see", "on_wave_start"):
        lambda s, u: (_gain_energy(u, 15.0, state=s),
                      s.log.append('  光锥[when_she_decided_to_see] 波次回15能量')),
    # 镜中故我: 每个波次开始时我方全体恢复10点能量
    ("past_self_in_mirror", "on_wave_start"):
        lambda s, u: ([_gain_energy(x, 10.0, state=s) for x in s.units if x.is_alive],
                      s.log.append('  光锥[past_self_in_mirror] 波次全队回10能量')),
    # 镜中故我: 终结技后的全队增伤由事件缓冲器处理；击破特攻达标时额外回1SP。
    ("past_self_in_mirror", "on_ult"):
        lambda s, u: _lc_restore_sp_if_be_threshold(s, u),
    # 孤独的疗愈: 陷入装备者持续伤害效果的敌方目标被消灭时回6能量（近似: 击杀即回）
    ("solitary_healing", "on_kill"):
        lambda s, u: (_gain_energy(u, 6.0, state=s),
                      s.log.append('  光锥[solitary_healing] 击杀回6能量')),
    # 无可取代的东西: 消灭敌方目标或受到攻击后回装备者攻击力8%的生命
    ("something_irreplaceable", "on_kill"):
        lambda s, u: _lc_heal_pct_atk(s, u, 0.08, '击杀回血'),
    ("something_irreplaceable", "on_hit_taken"):
        lambda s, u: _lc_heal_pct_atk(s, u, 0.08, '受击回血'),
    # v5.4 无用效果重审修复（全部为实机有效效果的建模）
    # 等价交换: 回合开始随机为1个能量<50%的队友回16能量
    ("quid_pro_quo", "on_self_turn_start"):
        lambda s, u: _lc_quid_pro_quo(s, u),
    # 致长夜的星光: 忆灵消失时回8能量
    ("starlight_to_the_long_night", "on_memsprite_despawn"):
        lambda s, u: (_gain_energy(u, 8.0, state=s),
                      s.log.append('  光锥[starlight_to_the_long_night] 忆灵消失回8能量')),
    # 回到大地的飞行: 队友对单体施放2次战技或终结技后回1战技点
    ("a_grounded_ascent", "on_skill"):
        lambda s, u: _lc_grounded_ascent_counter(s, u),
    ("a_grounded_ascent", "on_ult"):
        lambda s, u: _lc_grounded_ascent_counter(s, u),
    # 今日亦是和平的一日: 增伤 = 能量上限×0.4%（最多计入160点）
    ("today_is_another_peaceful_day", "on_battle_start"):
        lambda s, u: _lc_energy_cap_dmg_bonus(s, u),
    # 生命当付之一炬: 攻击使目标DEF降低 2回合（v5.7: 按叠影档 12/15/18/21/24%）
    ("life_should_be_cast_to_flames", "on_self_attack"):
        lambda s, u: _lc_apply_enemy_def_down(s, u, _lc_rank_value(u, 12.0, code='event_self_attack') / 100,
                                              2, 'def_reduction', 'life_flames_def_down'),
    # 决心如汗珠般闪耀: 击中时16%基础概率使目标【攻陷】DEF-16% 1回合
    ("resolution_shines_as_pearls_of_sweat", "on_self_attack"):
        lambda s, u: _lc_apply_gongxian(s, u),
    # 梦应归于何处: 造成击破伤害时使目标【溃败】2回合（装备者击破伤害+24%/敌速-20%）
    ("whereabouts_should_dreams_rest", "on_weakness_break"):
        lambda s, u: _lc_apply_kubai(s, u),
    # 烦恼着，幸福着: 施放追加攻击后使目标【温驯】最多2层（我方命中温驯目标CD+12%/层）
    ("worrisome_blissful", "on_followup"):
        lambda s, u: _lc_apply_wenshun(s, u),
    # 时节不居: 治疗记录→攻击后附加伤害36%（每回合最多1次）
    ("time_waits_for_no_one", "on_heal"):
        lambda s, u: _lc_record_heal(s, u),
    ("time_waits_for_no_one", "on_self_attack"):
        lambda s, u: _lc_heal_record_extra_damage(s, u),
    ("time_waits_for_no_one", "on_attack"):
        lambda s, u: _lc_heal_record_extra_damage(s, u),
    # 如泥酣眠: 普攻/战技未造成暴击时CR+36% 1回合（期望模式: 按未暴击概率1-CR触发, 3回合CD）
    ("sleep_like_the_dead", "on_self_attack"):
        lambda s, u: _lc_sleep_like_dead_miss_crit(s, u),
    # v5.4 第二批（用户提供S1数值后实现）
    # 落日时起舞/制胜的瞬间: 受击概率提高500%/200%（×6/×3, 用户确认）
    ("dance_at_sunset", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 6.0),
    ("moment_of_victory", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 3.0),
    # 与行星相会: 我方造成与装备者相同属性伤害时提高12%（v5.7: 按叠影档 values 取）
    ("planetary_rendezvous", "on_battle_start"):
        lambda s, u: _lc_same_element_dmg_bonus(s, u, _lc_rank_value(u, 12.0)),
    # 朗道的选择: 受击概率提高200%（×3, 用户确认）
    ("landaus_choice", "on_battle_start"):
        lambda s, u: _lc_set_taunt_mult(s, u, 3.0),
    # 后会有期: 普攻/战技后对随机受击目标造成48%ATK附加伤害（v5.7: 按叠影档 values 取, /100 转小数）
    ("we_will_meet_again", "on_basic_attack"):
        lambda s, u: _lc_extra_flat_damage(s, u, _lc_rank_value(u, 48.0) / 100),
    ("we_will_meet_again", "on_skill"):
        lambda s, u: _lc_extra_flat_damage(s, u, _lc_rank_value(u, 48.0) / 100),
    # 在火的远处: 单次受击/自耗≥25%生命上限→回15%HP+增伤 2回合（3回合CD; v5.7: 增伤按叠影档）
    ("flames_afar", "on_hp_loss"):
        lambda s, u: _lc_flames_afar(s, u, 0.25, _lc_rank_value(u, 25.0)),
    # 生命当付之一炬: 回合开始回10能量（弱点增伤60%在 _lc_target_correct 消费）
    ("life_should_be_cast_to_flames", "on_self_turn_start"):
        lambda s, u: (_gain_energy(u, 10.0, state=s),
                      s.log.append('  光锥[life_should_be_cast_to_flames] 回合开始回10能量')),
    # 美梦小镇大冒险: 施放技能后全队对应类型增伤（最新类型生效, v5.7: 按叠影档）
    ("dreamville_adventure", "on_basic_attack"):
        lambda s, u: _lc_childlike_mark(s, u, 'basic_attack'),
    ("dreamville_adventure", "on_skill"):
        lambda s, u: _lc_childlike_mark(s, u, 'skill'),
    ("dreamville_adventure", "on_ult"):
        lambda s, u: _lc_childlike_mark(s, u, 'ultimate'),
    # 论剑: 多次击中同一目标叠层/层（5层, 换目标重置; v5.7: 按叠影档）
    ("swordplay", "on_self_attack"):
        lambda s, u: _lc_swordplay_stack(s, u),
    # 爱如此刻永恒: 忆灵技后【空白】/【诗行】（当局永久, 用户确认; 双持×1.6）
    ("this_love_forever", "on_memsprite_attack"):
        lambda s, u: _lc_love_forever(s, u),
    # v6.11.1 你将起身歌唱(晴歌专属): 终结技后回1战技点
    ("rise_and_sing", "on_ult"):
        lambda s, u: (_gain_skill_points(s),
                      s.log.append('  光锥[你将起身歌唱] 终结技回1战技点')),
    # 你将起身歌唱: 进战行动提前(叠影档) + 【新声】2回合全队速度提高(叠影档)
    ("rise_and_sing", "on_battle_start"):
        lambda s, u: _rise_and_sing_entry(s, u),  # M4批2b: 委托角色包（下方 delegator）
}


def _lc_heal_pct_atk(state, u, pct, tag):
    """光锥回血动作: 回装备者攻击力 pct 的生命（不可超上限）"""
    atk = _build_effective_stats(u, state).ATK
    heal = atk * pct
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    state.log.append(f'  光锥[{getattr(u, "lightcone", None).id if getattr(u, "lightcone", None) else "lc"}] {tag}+{heal:.0f}')


def _lc_restore_sp_if_be_threshold(state, u):
    """镜中故我: 终结技后，击破特攻达到150%时恢复1个战技点。"""
    if _build_effective_stats(u, state).BREAK_EFFECT < 1.5:
        return
    _gain_skill_points(state)
    state.log.append('  光锥[past_self_in_mirror] 击破特攻达标→回1SP')


def _lc_apply_event_effect(state, u, event):
    """光锥事件缓冲器: 事件触发时按 condition_code 匹配的 effect 挂 TimedBuff。

    对应属性已在 _apply_lc_condition_corrections 恒负向抵消（事件型属性本不该常驻），
    这里挂上临时 buff 恢复。duration 从 condition 文本提取（'N回合'），默认 2。
    """
    lc = getattr(u, 'lightcone', None)
    if not lc:
        return
    code = next((k for k, v in LC_EVENT_CODES.items() if v == event), None)
    if not code:
        return
    import re
    matched = [eff for eff in lc.effects
               if getattr(eff, 'condition_code', '') == code]
    action = LC_EVENT_ACTIONS.get((lc.id, event))
    if action and any(not (eff.attributes or {}) for eff in matched):
        action(state, u)
    for eff in matched:
        attrs = eff.attributes or {}
        if not attrs:
            # v5.1: 回能/回血/回SP类动作处理（LC_EVENT_ACTIONS）
            if not action:
                state.log.append(f'  [WARN] 光锥[{lc.id}]事件效果无属性数据(未建模): {eff.condition[:40]}…')
            continue
        m = re.search(r'(\d+)\s*回合', eff.condition or '')
        dur = int(m.group(1)) if m else 2
        targets = [x for x in state.units if x.is_alive] if eff.target == 'all_allies' else [u]
        for t in targets:
            t.buffs.append(TimedBuff(source_id=lc.id, attributes=attrs,
                                     remaining_turns=dur,
                                     source_name=f'光锥·{lc.name}'))
            state.log.append(f'  光锥[{lc.id}] {event}: {t.char.name} +{list(attrs.keys())} ({dur}回合)')


def _lc_negative(s, attrs):
    """对面板副本负向抵消属性（与 attributes.py 静态施加同基线）"""
    for k, v in (attrs or {}).items():
        _apply_stat(s, k, -v)


# 状态门槛型条件码求值器: (u, state, target) -> bool（True=条件满足保留属性）
def _lc_state_enemies_ge_3(u, state, target=None):
    return len([e for e in state.enemies if e.HP > 0]) >= 3


def _lc_state_spd_threshold(u, state, target=None):
    """速度>100 阈值（于夜色中: 每超10点普攻战技+6%/终结技暴伤+12%, 各6层）
    手算战斗内速度: base_stats 已含遗器与静态百分比折叠, 战斗 buff/status 的
    SPD_PERCENT 只加字段不折叠——不调 _effective_spd, 避免经
    _build_effective_stats → 光锥条件修正 互相递归"""
    extra_pct = 0.0
    for b in getattr(u, 'buffs', []):
        extra_pct += getattr(b, 'attributes', {}).get('SPD_PERCENT', 0.0)
    for st in getattr(u, 'statuses', []):
        extra_pct += getattr(st, 'attributes', {}).get('SPD_PERCENT', 0.0)
    spd = u.base_stats.SPD + u.base_stats._base_SPD * (extra_pct / 100.0)
    return spd > 100.0


def _lc_state_energy_cap(u, state, target=None):
    return (u.char.max_energy or 0) >= 300


def _lc_state_be_threshold(u, state, target=None):
    return u.base_stats.BREAK_EFFECT >= 1.5


def _lc_state_energy_full(u, state, target=None):
    return u.current_energy >= (u.char.max_energy or 0)


def _lc_state_shield(u, state, target=None):
    return getattr(u, 'shield', 0.0) > 0.0


def _lc_state_elation(u, state, target=None):
    return any(x.char.id == 'trailblazer_elation' for x in state.units if x.is_alive)


def _lc_state_team(u, state, target=None):
    return state.max_sp >= 6


def _lc_state_taunt(u, state, target=None):
    return True  # 嘲讽值+75: 由 _select_enemy_target 读 condition_code 加权


def _lc_state_hp50_target(u, state, target):
    if target is None:
        return True
    bp = state.extra.get('enemy_blueprint')
    if bp and bp.HP > 0:
        return target.HP <= bp.HP * 0.50
    max_hp = max((e.HP for e in state.enemies if e.HP > 0), default=0)
    return max_hp > 0 and target.HP <= max_hp * 0.50


def _lc_state_target_debuff(u, state, target):
    if target is None:
        return True
    return target.debuff_count() > 0


def _lc_state_ehr_ge_80(u, state, target=None):
    """效果命中≥80%（好戏开演 2 段: ATK+36%）
    读静态面板避免 _build_effective_stats 递归（EHR 战斗 buff 少见, 简化）"""
    return u.base_stats.EFFECT_HIT_RATE >= 0.8


LC_STATE_EVALUATORS = {
    "state_enemies_ge_3": _lc_state_enemies_ge_3,
    "state_spd_threshold": _lc_state_spd_threshold,
    "state_energy_cap_ge_300": _lc_state_energy_cap,
    "state_be_threshold": _lc_state_be_threshold,
    "state_energy_full": _lc_state_energy_full,
    "state_shield_related": _lc_state_shield,
    "state_elation_path": _lc_state_elation,
    "state_team_related": _lc_state_team,
    "state_taunt_buff": _lc_state_taunt,
    "state_hp_below_50_target": _lc_state_hp50_target,
    "state_target_debuff_related": _lc_state_target_debuff,
    "state_ehr_ge_80": _lc_state_ehr_ge_80,
}
# 目标相关码: 需伤害循环内按目标求值
LC_TARGET_CODES = ("state_hp_below_50_target", "state_target_debuff_related",
                   "count:target_debuffs")


def _lc_count_evaluate(state, u, source, target=None) -> int:
    """计数型光锥条件: 按计数源返回当前值（v5.0.1）"""
    if source == 'enemies_alive':
        return len([e for e in state.enemies if e.HP > 0])
    if source == 'sp_spent':
        return state.extra.get('lc_sp_spent', 0)
    if source == 'target_debuffs':
        if target is None:
            return 0
        return target.debuff_count()
    return 0


def _lc_scope_targets(state, owner, target_kind):
    """返回光锥效果的玩家侧目标，供叠层的跨单位面板结算复用。"""
    alive = [x for x in state.units if x.is_alive]
    if target_kind == 'all_allies':
        return alive
    if target_kind == 'all_allies_except_self':
        return [x for x in alive if x is not owner]
    if target_kind == 'ally_main':
        return [x for x in alive if x is not owner][:1] or [owner]
    return [owner]


def _apply_lc_condition_corrections(u, state, s, target=None, only_codes=None):
    """光锥条件修正: 对不满足条件的 effect 属性负向抵消（s 为副本）。

    - event_*: 恒负向抵消（属性由事件缓冲器挂 TimedBuff 恢复）
    - state_*: 求值不满足 → 负向抵消; 目标相关码在 target 传入时按目标求值
    - typed_permanent: 限定属性型常驻; unsupported: 保持常驻 + WARN 一次
    - only_codes: v6.2.1 仅处理前缀匹配的码（结算副本只重跑目标码,
      避免 event_*/stack_*/count:* 在 _lc_target_correct 里被二次负向抵消）
    """
    lc = getattr(u, 'lightcone', None)
    if not lc or lc.path != u.char.path:
        return
    if state is None:
        return  # 无战斗上下文（初始AV等）不修正, 保守保留属性
    warned = state.extra.setdefault('lc_warned', set())
    for eff in lc.effects:
        code = getattr(eff, 'condition_code', '')
        attrs = eff.attributes or {}
        if not code or not attrs:
            continue
        if only_codes and not any(code.startswith(c) for c in only_codes):
            continue
        if code.startswith('event_'):
            _lc_negative(s, attrs)
        elif code == 'typed_permanent':
            continue
        elif code == 'unsupported':
            if state and lc.id not in warned:
                warned.add(lc.id)
                state.log.append(f'  [WARN] 光锥[{lc.id}]条件未建模(保持常驻近似): {eff.condition[:36]}…')
        elif code.startswith('stack:') or code.startswith('stack_full:'):
            # v5.0.1 叠层/标记: attrs=满层总量, 按 (max-count)/max 比例抵消
            _, key, max_s = code.split(':')
            # 静态属性只初始化在持有者面板中。若效果目标是另一名队友，
            # 持有者必须移除静态残留，实际加成由团队分发补入。
            if u not in _lc_scope_targets(state, u, getattr(eff, 'target', 'self')):
                _lc_negative(s, attrs)
                continue
            count = u.lc_stacks.get(f'{lc.id}::{key}', 0)
            mx = int(max_s)
            if code.startswith('stack_full:'):
                if count < mx:
                    _lc_negative(s, attrs)  # 未叠满 → 全量抵消
            elif count < mx:
                r = (mx - min(count, mx)) / mx
                _lc_negative(s, {k: v * r for k, v in attrs.items()})
        elif code.startswith('count:'):
            # v5.0.1 计数型（场上敌人数/SP消耗/目标debuff数）
            _, source, max_s = code.split(':')
            # 目标负面数只能在伤害循环拿到具体目标后结算。此处预先抵消会使
            # _lc_target_correct 再抵消一次，造成属性重复扣除。
            if source == 'target_debuffs' and target is None:
                continue
            count = _lc_count_evaluate(state, u, source, target)
            mx = int(max_s)
            if count < mx:
                r = (mx - min(count, mx)) / mx
                _lc_negative(s, {k: v * r for k, v in attrs.items()})
        elif code in LC_STATE_EVALUATORS:
            if not LC_STATE_EVALUATORS[code](u, state, target):
                _lc_negative(s, attrs)


def _apply_team_lc_stack_buffs(u, state, s):
    """将其他角色光锥的叠层型队伍/单体增益应用到目标有效面板。"""
    if state is None:
        return
    for owner in state.units:
        if owner is u or not owner.is_alive:
            continue
        lc = getattr(owner, 'lightcone', None)
        if not lc or lc.path != owner.char.path:
            continue
        for eff in lc.effects:
            code = getattr(eff, 'condition_code', '') or ''
            target_kind = getattr(eff, 'target', 'self')
            if target_kind not in ('all_allies', 'all_allies_except_self', 'ally_main'):
                continue
            if u not in _lc_scope_targets(state, owner, target_kind):
                continue
            attrs = eff.attributes or {}
            if not attrs or not (code.startswith('stack:') or code.startswith('stack_full:')):
                continue
            _, key, max_s = code.split(':')
            mx = int(max_s)
            if mx <= 0:
                continue
            count = min(u.lc_stacks.get(f'{lc.id}::{key}', 0), mx)
            scale = 1.0 if code.startswith('stack_full:') and count >= mx else (
                0.0 if code.startswith('stack_full:') else count / mx)
            for attr, value in attrs.items():
                _apply_stat(s, attr, value * scale)


def _lc_target_correct(stats, u, state, target):
    """伤害循环内按目标求值目标相关光锥条件（返回可能为副本）"""
    lc = getattr(u, 'lightcone', None)
    if not lc:
        return stats
    need = any(any((getattr(e, 'condition_code', '') or '').startswith(c)
                   for c in LC_TARGET_CODES)
               and (e.attributes or {}) for e in lc.effects)
    # v5.4 生命当付之一炬/论剑: 目标相关消费（不受 need 门控, 数值由引擎内联）
    inline_target = lc.id in ('life_should_be_cast_to_flames', 'swordplay')
    if not need and not inline_target:
        return stats
    s = copy.deepcopy(stats)
    # v6.2.1: 结算副本只重跑目标码（Harness P1-1: 此前全量重跑使 event_*/stack_* 二次抵消）
    _apply_lc_condition_corrections(u, state, s, target=target, only_codes=LC_TARGET_CODES)
    # v5.4 生命当付之一炬: 目标拥有装备者添加的弱点 → 装备者伤害提高（v5.7: 按叠影档）
    if lc.id == 'life_should_be_cast_to_flames':
        for st in getattr(target, 'statuses', []):
            if st.source == u.char.id and 'weakness_element' in st.attributes:
                s.DMG_BONUS_ALL += _lc_rank_value(u, 60.0, code='state:enemy_has_weakness') / 100
                break
    # v5.4 论剑: 目标与叠层记录一致 → 伤害×(1+每层%×层)（v5.7: 每层%按叠影档, 最多5层）
    if lc.id == 'swordplay' and u.extra.get('swordplay_tid') == target.id:
        s.DMG_BONUS_ALL += (_lc_rank_value(u, 16.0) / 100) * u.extra.get('swordplay_layers', 0)
    return s


def _lc_maybe_gain_stack(state, unit, eff, event_type, target_count=0):
    """叠层/标记触发分发（v5.0.1）: stack_gain:{event}:{key}:{max}:{target}:{duration}:{op}

    event 支持 '|' 并集; target: self/all_allies/ally_main; duration 0=无限;
    op: ''/turn_end:-1/clear_on:attack/clear_on:self_turn_start/per_target
    """
    parts = eff.condition_code.split(':', 6)
    # 格式: stack_gain / event / key / max / [target] / [duration] / [op]
    # op 可含内部冒号（turn_end:-1 / clear_on:attack）, 用 split(':',6) 保留末段
    events, key = parts[1], parts[2]
    mx = int(parts[3]) if len(parts) > 3 and parts[3] else 1
    target_kind = parts[4] if len(parts) > 4 and parts[4] else 'self'
    duration = int(parts[5]) if len(parts) > 5 and parts[5] else 0
    op = parts[6] if len(parts) > 6 else ''
    # 清除事件与 gain 事件可能不同（烈阳: gain on_ult / clear on_self_turn_start）
    # → 清除判定独立于 gain 事件匹配, 且先于叠层（演算: 攻击后重置再按命中叠）
    lc_id = getattr(unit, 'lightcone', None).id if getattr(unit, 'lightcone', None) else 'lc'
    if 'clear_on:attack' in op and event_type == 'on_self_attack':
        u_stacks, u_turns = [], []
        for t in ([x for x in state.units if x.is_alive] if target_kind == 'all_allies' else [unit]):
            u_stacks.append(t.lc_stacks); u_turns.append(t.lc_stack_turns)
        for st, tn in zip(u_stacks, u_turns):
            st.pop(f'{lc_id}::{key}', None); tn.pop(f'{lc_id}::{key}', None)
    if 'clear_on:self_turn_start' in op and event_type == 'on_self_turn_start':
        for t in ([x for x in state.units if x.is_alive] if target_kind == 'all_allies' else [unit]):
            t.lc_stacks.pop(f'{lc_id}::{key}', None)
            t.lc_stack_turns.pop(f'{lc_id}::{key}', None)
        return  # 纯清除事件（烈阳: gain 是 on_ult, 本事件不叠层）
    if event_type not in events.split('|'):
        return
    gain = 1
    if 'per_target' in op:
        gain = max(target_count, 0)  # 按本次命中敌数叠层
    if target_kind == 'all_allies':
        targets = [x for x in state.units if x.is_alive]
    elif target_kind == 'ally_main':
        targets = [x for x in state.units if x.is_alive and x is not unit][:1] or [unit]
    else:
        targets = [unit]
    for t in targets:
        key_full = f'{lc_id}::{key}'
        cur = t.lc_stacks.get(key_full, 0)
        nxt = min(cur + gain, mx)
        if nxt > cur:
            t.lc_stacks[key_full] = nxt
            state.log.append(f'  光锥[{lc_id}] {key} 叠层 {nxt}/{mx}')
            if duration > 0:
                # v5.6: 每层独立计时——新层各自 append 独立倒计时（不再扁平刷新）
                turns = t.lc_stack_turns.setdefault(key_full, [])
                for _ in range(nxt - cur):
                    turns.append(duration)
        elif duration > 0 and gain > 0 and nxt == cur == mx:
            # v5.6: 满层后再触发 → 替换最旧层（每层独立计时, 实机满层覆盖最旧）
            turns = t.lc_stack_turns.setdefault(key_full, [])
            for _ in range(min(gain, mx)):
                if turns:
                    turns.pop(0)
                turns.append(duration)
            state.log.append(f'  光锥[{lc_id}] {key} 满层刷新 {cur}/{mx}')


def _lc_maybe_remove_stack(state, unit, eff, event_type):
    """叠层移除触发: stack_remove:{event}:{key}:{target}"""
    parts = eff.condition_code.split(':')
    events, key = parts[1], parts[2]
    if event_type not in events.split('|'):
        return
    key_full = f'{getattr(unit, "lightcone", None).id if getattr(unit, "lightcone", None) else "lc"}::{key}'
    for t in ([x for x in state.units if x.is_alive] if len(parts) > 3 and parts[3] == 'all_allies' else [unit]):
        t.lc_stacks.pop(key_full, None)
        t.lc_stack_turns.pop(key_full, None)


def _lc_tick_stacks(state, u):
    """叠层回合倒计时（v5.0.1）: Y 轴常规回合结束调用（X 轴不 tick）
    v5.6: 每层独立计时——列表内每层各自 -1, 归零的层消失, 层数同步 len"""
    for key in list(u.lc_stack_turns):
        raw = u.lc_stack_turns[key]
        turns = [raw] if isinstance(raw, int) else raw
        alive = [t - 1 for t in turns if t - 1 > 0]
        if alive:
            u.lc_stack_turns[key] = alive
            u.lc_stacks[key] = len(alive)
        else:
            u.lc_stack_turns.pop(key, None)
            u.lc_stacks.pop(key, None)
    # turn_end 衰减（i_venture_forth_to_hunt 流光: 回合结束掉1层）
    lc = getattr(u, 'lightcone', None)
    if lc:
        for eff in lc.effects:
            code = eff.condition_code or ''
            if code.startswith('stack_gain:') and code.endswith('turn_end:-1'):
                key = code.split(':', 6)[2]
                key_full = f'{lc.id}::{key}'
                cur = u.lc_stacks.get(key_full, 0)
                if cur > 0:
                    nxt = cur - 1
                    if nxt <= 0:
                        u.lc_stacks.pop(key_full, None)
                        u.lc_stack_turns.pop(key_full, None)
                    else:
                        u.lc_stacks[key_full] = nxt
                        turns = u.lc_stack_turns.get(key_full)
                        if turns:
                            turns.pop(0)  # v5.6: 回合结束移除最旧一层


# ---- v5.4 光锥效果建模 helpers（无用效果重审修复）----

def _lc_quid_pro_quo(s, u):
    """等价交换: 回合开始随机为1个能量<50%的队友回16能量"""
    cands = [x for x in s.units if x.is_alive and x is not u
             and x.char.max_energy > 0
             and x.current_energy < x.char.max_energy * 0.5]
    if cands:
        t = random.choice(cands)
        gained = _gain_energy(t, 16.0, state=s)
        s.log.append(f'  光锥[quid_pro_quo] 等价交换: {t.char.name} 回能+{gained:.0f}')


def _lc_grounded_ascent_counter(s, u):
    """回到大地的飞行: 单体辅助技回能、叠圣咏，每2次回1战技点。"""
    if u.char.max_energy <= 0:
        return  # 无常规能量系统角色（如欢愉）不算
    if u.extra.get('lc_last_skill_target_type') != 'single_ally':
        return

    _gain_energy(u, 6.0, state=s)
    target = u.extra.get('lc_last_skill_target')
    if target not in s.units or not target.is_alive:
        target = next((x for x in s.units if x.char.id == 'seele' and x.is_alive), u)
    # v5.6: 圣咏每层独立 TimedBuff（每层持续3回合各自计时, _build_effective_stats 逐buff累加）;
    # 满3层后再获得 → 替换最旧层（实机每层独立 + 满层覆盖最旧）
    hymns = [b for b in target.buffs
             if getattr(b, 'param_id', '') == 'grounded_ascent_hymn']
    if len(hymns) >= 3:
        target.buffs.remove(hymns.pop(0))
    target.buffs.append(TimedBuff(source_id='a_grounded_ascent',
                                  attributes={'DMG_BONUS_ALL': 15.0},
                                  remaining_turns=3,
                                  source_name='光锥·回到大地的飞行',
                                  param_id='grounded_ascent_hymn'))

    u.extra['lc_grounded_count'] = u.extra.get('lc_grounded_count', 0) + 1
    s.log.append(f'  光锥[a_grounded_ascent] 回能6; {target.char.name}圣咏{len(hymns) + 1}/3')
    if u.extra['lc_grounded_count'] >= 2:
        u.extra['lc_grounded_count'] = 0
        _gain_skill_points(s)
        s.log.append('  光锥[a_grounded_ascent] 回到大地的飞行: 回1战技点')


def _lc_energy_cap_dmg_bonus(s, u):
    """今日亦是和平的一日: 增伤 = min(能量上限,160)×0.4%"""
    dmg = min(u.char.max_energy or 0, 160) * 0.4
    u.buffs.append(TimedBuff(source_id='today_is_another_peaceful_day',
                             attributes={'DMG_BONUS_ALL': dmg},
                             remaining_turns=-1,
                             source_name='光锥·今日亦是和平的一日'))
    s.log.append(f'  光锥[today_is_another_peaceful_day] 增伤+{dmg:.1f}% (能量上限{min(u.char.max_energy or 0, 160):.0f}×0.4%)')


def _lc_apply_enemy_def_down(s, u, amount, turns, attr_key, status_id, base_chance=1.0):
    """通用: 攻击使敌方目标防御力降低（EnemyStatus def_reduction 消费链路）
    v5.6: 接入统一 EHR 检定（文本无概率描述的效果=必中 base_chance=1.0）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, status_id, base_chance):
            continue
        t.add_status(EnemyStatus(id=status_id, name=status_id, category='debuff',
                                 source=u.char.id, remaining_turns=turns,
                                 attributes={attr_key: amount}))
        s.log.append(f'  光锥[{getattr(u.lightcone, "id", "?")}] {t.name or t.id} DEF-{amount*100:.0f}% ({turns}回合)')


def _lc_apply_gongxian(s, u):
    """决心如汗珠般闪耀: 击中使目标【攻陷】DEF降低 1回合
    v5.6: 接入统一 EHR 检定; 精炼公式（用户确认）:
    精1~精5 触发基础概率 60%~100%（每精一层+10%）; 减防 12%~16%（每精一层+1%）"""
    rank = getattr(getattr(u, 'lightcone', None), 'rank', 1) or 1
    base_chance = 0.60 + 0.10 * (rank - 1)
    def_down = 0.12 + 0.01 * (rank - 1)
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, '攻陷', base_chance=base_chance):
            s.log.append(f'  光锥[resolution_shines_as_pearls_of_sweat] {t.name or t.id} 攻陷未命中({base_chance:.0%})')
            continue
        t.add_status(EnemyStatus(id='gongxian', name='攻陷', category='debuff',
                                 source=u.char.id, remaining_turns=1,
                                 attributes={'def_reduction': def_down}))
        s.log.append(f'  光锥[resolution_shines_as_pearls_of_sweat] {t.name or t.id} 攻陷: DEF-{def_down*100:.0f}% (1回合)')


def _lc_apply_kubai(s, u):
    """梦应归于何处: 造成击破伤害时使目标【溃败】2回合（击破伤害+24%在 _apply_toughness_damage 消费）
    v5.6: 文本无概率描述→必中, 统一检定入口（base_chance=1.0）"""
    t = s.extra.get('lc_break_enemy')
    if t is None:
        alive = s.alive_enemies() or s.enemies
        t = alive[0] if alive else None
    if t and _roll_effect_hit(u, s, t, '溃败', base_chance=1.0):
        t.add_status(EnemyStatus(id='kubai', name='溃败', category='debuff',
                                 source=u.char.id, remaining_turns=2,
                                 attributes={'break_dmg_bonus': 0.24, 'spd_down': 0.20}))
        s.log.append(f'  光锥[whereabouts_should_dreams_rest] {t.name or t.id} 溃败 (2回合)')


def _lc_apply_wenshun(s, u):
    """烦恼着，幸福着: 施放追加攻击后使目标【温驯】最多2层
    v5.6: 文本无概率描述→必中, 统一检定入口（base_chance=1.0）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    for t in targets:
        if not _roll_effect_hit(u, s, t, '温驯', base_chance=1.0):
            continue
        existing = next((st for st in t.statuses if st.id == 'wenshun'), None)
        layers = (existing.attributes.get('wenshun_layers', 0) if existing else 0) + 1
        layers = min(layers, 2)
        t.add_status(EnemyStatus(id='wenshun', name='温驯', category='debuff',
                                 source=u.char.id, remaining_turns=1,
                                 attributes={'wenshun_layers': layers}))
        s.log.append(f'  光锥[worrisome_blissful] {t.name or t.id} 温驯{layers}层')


def _lc_record_heal(s, u):
    """时节不居: 记录治疗量（累计, 攻击后按36%附加伤害）"""
    amt = u.extra.get('lc_heal_record', 0.0)
    u.extra['lc_heal_record'] = amt + s.extra.get('lc_last_heal_amt', 0.0)


def _lc_heal_record_extra_damage(s, u):
    """时节不居: 攻击后按记录治疗量36%对随机受击敌附加伤害（每回合最多1次）"""
    if u.extra.get('lc_heal_record_used_this_turn'):
        return
    record = u.extra.get('lc_heal_record', 0.0)
    if record <= 0:
        return
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    u.extra['lc_heal_record_used_this_turn'] = True
    u.extra['lc_heal_record'] = 0.0
    extra = record * 0.36
    t = random.choice(targets)
    _commit_enemy_damage(s, u, t, extra)
    u.total_damage_dealt += extra
    s.log.append(f'  光锥[time_waits_for_no_one] 时节不居: 附加伤害{extra:.0f} (治疗记录{record:.0f}×36%)')


def _lc_sleep_like_dead_miss_crit(s, u):
    """如泥酣眠: 普攻/战技未暴击时CR+36% 1回合（期望模式按未暴击概率1-CR判定, 3回合CD）"""
    if u.extra.get('lc_sleep_cd', 0) > 0:
        return
    skill_key = u.extra.get('lc_last_skill_key', s.extra.get('lc_last_skill_key'))
    if skill_key not in ('basic_attack', 'skill', 'basic_attack_enhanced'):
        return
    stats = _build_effective_stats(u, s)
    if random.random() < (1.0 - min(stats.CRIT_RATE, 1.0)):
        duration = 1 if s.extra.get('action_ctx') == 'extra' else 2
        u.buffs.append(TimedBuff(source_id='sleep_like_the_dead',
                                 attributes={'CRIT_RATE': 36.0},
                                 remaining_turns=duration,
                                 source_name='光锥·如泥酣眠'))
        u.extra['lc_sleep_cd'] = 3
        s.log.append('  光锥[sleep_like_the_dead] 未暴击: CR+36% (1回合)')


def _lc_set_taunt_mult(s, u, mult):
    """受击概率提高（×mult, 常驻）: 落日时起舞×6 / 制胜的瞬间×3"""
    u.extra['taunt_mult'] = mult
    s.log.append(f'  光锥[{getattr(u.lightcone, "id", "?")}] 受击概率×{mult}')


def _lc_same_element_dmg_bonus(s, u, bonus):
    """与行星相会: 我方造成与装备者相同属性的伤害提高%（全队, 永久）"""
    # 战斗层 _apply_stat 的元素增伤键为 DMG_BONUS_{中文元素}
    elem_key = f'DMG_BONUS_{u.char.element}'
    for eu in s.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='planetary_rendezvous',
                                      attributes={elem_key: bonus},
                                      remaining_turns=-1,
                                      source_name='光锥·与行星相会'))
    s.log.append(f'  光锥[planetary_rendezvous] 全队{elem_key}+{bonus}%')


def _lc_extra_flat_damage(s, u, ratio):
    """后会有期: 普攻/战技后对随机受击目标造成ATK×ratio附加伤害（不受加成）"""
    targets = _lc_attacked_enemies(s)
    if not targets:
        return
    t = random.choice(targets)
    extra = _build_effective_stats(u, s).ATK * ratio
    _commit_enemy_damage(s, u, t, extra)
    u.total_damage_dealt += extra
    s.log.append(f'  光锥[we_will_meet_again] 后会有期: 附加伤害{extra:.0f} (48%ATK)')


def _lc_flames_afar(s, u, threshold, bonus):
    """在火的远处: 单次受击/自耗≥25%生命上限→回15%HP+增伤bonus% 2回合（3回合CD; v5.7 bonus按叠影档）"""
    cd = u.extra.get('flames_afar_cd', 0)
    if cd > 0:
        return
    lost = s.extra.get('lc_last_hp_loss', 0.0)
    if lost < u.max_hp * threshold:
        return
    u.current_hp = min(u.max_hp, u.current_hp + u.max_hp * 0.15)
    u.buffs.append(TimedBuff(source_id='flames_afar', attributes={'DMG_BONUS_ALL': bonus},
                             remaining_turns=2, source_name='光锥·在火的远处'))
    u.extra['flames_afar_cd'] = 3
    s.log.append(f'  光锥[flames_afar] 在火的远处: 回15%HP, 增伤+{bonus}% (2回合)')


def _lc_childlike_mark(s, u, skill_type):
    """美梦小镇大冒险: 最新技能类型【童心】→ 全队该类型技能增伤（v5.7: 按叠影档）"""
    u.extra['childlike_type'] = skill_type
    stat_type = {
        'basic_attack': 'DMG_BONUS_BASIC',
        'skill': 'DMG_BONUS_SKILL',
        'ultimate': 'DMG_BONUS_ULTIMATE',
    }[skill_type]
    bonus = _lc_rank_value(u, 12.0)
    # 移除旧童心buff, 挂新（全队, 永久直至下次技能）
    for eu in s.units:
        if not eu.is_alive:
            continue
        eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'childlike']
        eu.buffs.append(TimedBuff(source_id='dreamville_adventure',
                                  attributes={stat_type: bonus},
                                  remaining_turns=-1,
                                  source_name='光锥·美梦小镇大冒险',
                                  param_id='childlike'))
    s.log.append(f'  光锥[dreamville_adventure] 童心: 全队{skill_type}增伤+12%')


def _lc_swordplay_stack(s, u):
    """论剑: 多次击中同一目标叠层16%/层（5层, 换目标重置, S1）"""
    tid = s.extra.get('lc_attack_first_target_id', '')
    if not tid:
        return
    if u.extra.get('swordplay_tid') == tid:
        u.extra['swordplay_layers'] = min(u.extra.get('swordplay_layers', 0) + 1, 5)
    else:
        u.extra['swordplay_tid'] = tid
        u.extra['swordplay_layers'] = 1
    s.log.append(f'  光锥[swordplay] 论剑: 叠层{u.extra["swordplay_layers"]}/5')


def _lc_masquerade_caiyan(s, u):
    """游戏尘寰: 每恢复1战技点(含溢出)→1层【彩焰】, 4层→移除彩焰获【假面】4回合。
    假面的队友增益由既有 stack:jiamian 机制消费（lc_stacks 叠层, 队友+CR10/CD28 排除佩戴者）。"""
    layers = u.extra.get('masquerade_caiyan', 0) + 1
    if layers >= 4:
        u.extra['masquerade_caiyan'] = 0
        # 刷新全队 jiamian 叠层（4回合）→ 队友增益经 _apply_team_lc_stack_buffs 生效
        for eu in s.units:
            if not eu.is_alive:
                continue
            eu.lc_stacks['earthly_escapade::jiamian'] = 1
            eu.lc_stack_turns['earthly_escapade::jiamian'] = [4]  # v5.6: 分层容器（假面为状态刷新语义, 单层）
        s.log.append('  光锥[earthly_escapade] 【彩焰】满4层 → 【假面】4回合')
    else:
        u.extra['masquerade_caiyan'] = layers
        s.log.append(f'  光锥[earthly_escapade] 【彩焰】{layers}/4')


def _lc_refresh_love_blank(s):
    """Apply the strongest active permanent Blank mark to the current enemy wave."""
    blank = max((0.16 if owner.extra.get('love_poem') else 0.10
                 for owner in s.units
                 if owner.is_alive and owner.extra.get('love_blank')), default=0.0)
    for enemy in s.enemies:
        if blank:
            enemy.extra['love_blank_vuln'] = blank
        else:
            enemy.extra.pop('love_blank_vuln', None)


def _lc_love_forever(s, u):
    """爱如此刻永恒: 忆灵技后【空白】(敌方易伤10%)/【诗行】(全队CD+16%), 当局永久; 双持×1.6"""
    tgt_type = s.extra.get('lc_last_memsprite_target', '')
    if tgt_type in ('single_ally', 'ally', 'all_allies'):
        u.extra['love_blank'] = True  # 空白: 敌方全体受伤+10%
    else:
        u.extra['love_poem'] = True  # 诗行: 我方全体CD+16%
    both = u.extra.get('love_blank') and u.extra.get('love_poem')
    poem = 0.16 * (1.6 if both else 1.0)
    # 空白 → 敌方 vulnerability（全队受益）; 诗行 → 全队CD buff
    _lc_refresh_love_blank(s)
    if u.extra.get('love_poem'):
        for eu in s.units:
            if not eu.is_alive:
                continue
            eu.buffs = [b for b in eu.buffs if getattr(b, 'param_id', '') != 'love_poem']
            eu.buffs.append(TimedBuff(source_id='this_love_forever',
                                      attributes={'CRIT_DMG': poem * 100.0},
                                      remaining_turns=-1,
                                      source_name='光锥·爱如此刻永恒【诗行】',
                                      param_id='love_poem'))
    s.log.append(f'  光锥[this_love_forever] 空白:{u.extra.get("love_blank")} 诗行:{u.extra.get("love_poem")}')


def _process_lc_effects(unit, state, event_type):
    """根据事件类型触发匹配的光锥特效（v5.0.1: 含叠层/标记分发）"""
    lc = getattr(unit, 'lightcone', None)
    if not lc or lc.path != unit.char.path:
        return  # getattr 守卫: 忆灵可被 _apply_hit 命中
    if event_type == 'on_self_turn_start':
        for key in ('lc_sleep_cd', 'flames_afar_cd'):
            if unit.extra.get(key, 0) > 0:
                unit.extra[key] -= 1
        unit.extra['lc_heal_record_used_this_turn'] = False
    for eff in lc.effects:
        pid = eff.param_id
        if pid in LC_TRIGGERS:
            evt, handler = LC_TRIGGERS[pid]
            if evt == event_type:
                handler(state, unit, None)
                return
    # v5.0.1: 叠层/标记触发分发
    for eff in lc.effects:
        code = eff.condition_code or ''
        if code.startswith('stack_gain:'):
            _lc_maybe_gain_stack(state, unit, eff, event_type,
                                 target_count=state.extra.get('lc_attack_targets', 0))
        elif code.startswith('stack_remove:'):
            _lc_maybe_remove_stack(state, unit, eff, event_type)
    # v5.0 P3: 通用事件缓冲器（condition_code 匹配, 挂 TimedBuff 恢复被抵消属性）
    _lc_apply_event_effect(state, unit, event_type)


# ---- 技能执行 ----

def _bounce_hits(u, mult, state=None) -> int:
    """弹射段数（v5.3 开拓者·同谐E6: 战技额外伤害次数+2, 1+4→1+6）
    v6.7: 绯英行迹3·瞰众乐 终结技弹射+1/2/4（敌数≥3/2/1）; 火花E6 欢愉技每笑点+1次(上限40)"""
    hits = mult.hits
    if u.char.id == 'trailblazer_harmony' and u.eidolon_rank >= 6:
        hits += 2
    if u.char.id == 'evanescia' and state is not None:
        alive = state.alive_enemies()
        n = len(alive)
        if n >= 3:
            hits += 1
        elif n == 2:
            hits += 2
        elif n == 1:
            hits += 4
    if u.char.id == 'sparxie' and u.eidolon_rank >= 6 and state is not None:
        hits += min(int(state.laugh_points), 40)  # E6: 每1笑点额外伤害次数+1
    return hits


def _toughness_efficiency(u, state, skill_key, stats=None) -> float:
    """削韧效率（面板 × 各角色弱点击破效率源, v5.3 模块化注册）"""
    eff = (stats or _build_effective_stats(u, state)).TOUGHNESS_EFFICIENCY
    if u.char.id == 'fugue' and u.eidolon_rank >= 6:
        eff *= 1.5  # 忘归人E6: 自身弱点击破效率+50%
    if u.char.id == 'lingsha' and u.eidolon_rank >= 1:
        eff *= 1.5  # 灵砂E1: 自身弱点击破效率+50%
    if u.char.id == 'firefly' and u.extra.get('combustion') \
            and skill_key in ('basic_attack_enhanced', 'skill_enhanced'):
        eff *= 1.5  # 完全燃烧: 强化攻击弱点击破效率+50%
        if u.eidolon_rank >= 6:
            eff += 0.5  # 流萤E6: 击破效率+50%（v5.7 用户确认实机加算: 1.5+0.5=2.0）
    if u.extra.get('_foxian'):
        fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
        if fugue and fugue.eidolon_rank >= 1:
            eff *= 1.5  # 忘归人E1: 狐祈者弱点击破效率+50%
    return eff


def _no_weakness_pen(u, skill_key) -> bool:
    """全额无视弱点削韧: 遐蝶E6 / 忘归人终结技"""
    if u.char.id == 'xiadie' and u.eidolon_rank >= 6:
        return True
    if u.char.id == 'fugue' and skill_key == 'ultimate':
        return True
    if u.char.id == 'acheron' and (
            skill_key == 'ultimate'
            or (u.eidolon_rank >= 6 and skill_key in ('basic_attack', 'skill'))):
        return True
    if u.char.id == 'feixiao' and skill_key == 'ultimate':
        return True
    return False


def _super_break_rate(state, u, stats=None) -> float:
    """v5.3 超击破转化率（0=不触发）。源注册表:
    忘归人天赋(全队光环) / 同谐【伴舞】(持有者) / 流萤行迹2(燃烧+BE阈值)。
    多源线性求和（实机多源各触发一次实例）。"""
    rate = 0.0
    fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
    if fugue is not None:
        rate += 1.0  # 忘归人天赋: 我方攻击击破状态敌人→削韧转化为超击破（满级100%）
    if any(getattr(b, 'attributes', {}).get('_tbh_super_break') for b in u.buffs):
        rate += 1.0  # 同谐【伴舞】
    if u.char.id == 'firefly' and u.extra.get('combustion'):
        break_effect = (stats or _build_effective_stats(u, state)).BREAK_EFFECT
        if break_effect >= 3.0:
            rate += 1.5  # 流萤行迹2: BE≥300% → 150%转化
        elif break_effect >= 1.5:
            rate += 1.0  # 流萤行迹2: BE≥150% → 100%转化
    return rate


def _apply_toughness_damage(state, u, t, base_toughness, break_element, skill_key, stats) -> float:
    """v5.3 抽取: 单目标削韧结算（行为与原内联块一致+新增）。
    主韧性削韧→击破效果（伤害/延后/异常/hooks）; 已击破目标→超击破（源收窄）;
    云火昭（忘归人天赋）独立削减, 削至0二次击破。返回本次削韧造成的伤害。"""

    from engine.characters.the_dahlia import _dahlia_field_active, _dahlia_super_break_rate
    efficiency = _toughness_efficiency(u, state, skill_key, stats)
    toughness_dmg = base_toughness * efficiency
    no_weakness_pen = _no_weakness_pen(u, skill_key)
    can_tough = (t.element_res.get(break_element, 0.2) <= 0 or no_weakness_pen)
    # 狐祈者: 无对应弱点目标也可削减（50%效率）——非全额无视, 与既有无视弱点不叠加
    if u.extra.get('_foxian') and not no_weakness_pen \
            and t.element_res.get(break_element, 0.2) > 0:
        toughness_dmg *= 0.5
        can_tough = True
    # v5.0 P7: 铁骑4pc — 击破特攻≥150% → 击破/超击破无视20%防御（本地 DEF_PEN）
    sb_stats = stats
    if ('be_threshold_defpen' in getattr(u, '_active_relic_conditions', set())
            and stats.BREAK_EFFECT >= 1.5):
        sb_stats = copy.deepcopy(stats)
        sb_stats.DEF_PEN += 0.20
    # 流萤E1: 强化战技无视15%防御
    if u.char.id == 'firefly' and u.eidolon_rank >= 1 and skill_key == 'skill_enhanced':
        if sb_stats is stats:
            sb_stats = copy.deepcopy(stats)
        sb_stats.DEF_PEN += 0.15
    # 忘归人E4: 狐祈者造成的击破/超击破伤害+20%
    break_mult = 1.0
    if u.extra.get('_foxian'):
        fugue = next((x for x in state.units if x.char.id == 'fugue' and x.is_alive), None)
        if fugue and fugue.eidolon_rank >= 4:
            break_mult = 1.20
    # v5.3 流萤终结技: 强化攻击使敌方目标受到萨姆造成的击破伤害+20%（满级, 持续至本次攻击结束）
    if u.char.id == 'firefly' and u.extra.get('combustion') \
            and skill_key in ('basic_attack_enhanced', 'skill_enhanced'):
        break_mult *= 1.20
    # v5.4 梦应归于何处: 目标【溃败】状态下装备者对其击破伤害+24%
    if t.has_status(status_id='kubai') and getattr(u.lightcone, 'id', '') == 'whereabouts_should_dreams_rest':
        break_mult *= 1.24
    # 同谐行迹1: 伴舞触发的超击破伤害按敌人数+20%~60%
    tbh_mult = 1.0
    if any(getattr(b, 'attributes', {}).get('_tbh_super_break') for b in u.buffs):
        n = min(len(state.alive_enemies()), 5)
        tbh_mult = {5: 1.2, 4: 1.3, 3: 1.4, 2: 1.5, 1: 1.6}[n]

    added = 0.0
    # 主韧性削韧（仅弱点属性、无视弱点或狐祈者减半无视）
    if t.toughness > 0 and t.max_toughness > 0 and can_tough:
        t.toughness = max(0, t.toughness - toughness_dmg)
        # v6.7 大丽花结界: 未破韧目标承受的削韧值也能转化为超击破（削韧与转化同时发生）
        if _dahlia_field_active(state) and not t.is_broken:
            rate = _super_break_rate(state, u, stats) + _dahlia_super_break_rate(state, u, t)
            if rate > 0:
                sbd = calculate_damage(sb_stats, t, 0, 0, "super_break",
                                       break_element, 80, False,
                                       toughness_dmg=toughness_dmg)
                sbd.final_damage *= rate * break_mult * tbh_mult
                _commit_enemy_damage(state, u, t, sbd.final_damage)
                u.total_damage_dealt += sbd.final_damage
                added += sbd.final_damage
                state.log.append(f'  结界超击破: {t.name or t.id} {sbd.final_damage:.0f}(削韧{toughness_dmg:.0f})')
        if t.toughness <= 0 and not t.is_broken:
            t.is_broken = True
            # 击破瞬间结算击破伤害
            bd = calculate_damage(sb_stats, t, 0, 0, "break", break_element, 80, False)
            bd.final_damage *= break_mult
            _commit_enemy_damage(state, u, t, bd.final_damage)
            u.total_damage_dealt += bd.final_damage
            added += bd.final_damage
            state.log.append(f'  击破弱点! {t.name or t.id} 击破={bd.final_damage:.0f}({break_element})')
            # 强制行动延后25%
            t.extra['av_delayed'] = 2500.0
            # 属性异常DOT
            _apply_break_debuff(t, break_element, u, state)
            state.extra['lc_break_enemy'] = t  # v5.4 光锥击破事件目标（梦应归于何处溃败）
            state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
            _process_lc_effects(u, state, "on_weakness_break")  # v5.0.1
            state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                                    enemy=t, skill_key=skill_key)  # v5.3
    elif (t.is_broken or _dahlia_field_active(state)) and can_tough:
        # v5.3 超击破: 需超击破源（转化率>0）, 伤害=削韧值×(1+击破特攻)×转化率×修饰
        # v6.7 大丽花结界: 未破韧目标削韧也能转化（用户 2026-08-15 确认转化率与天赋同率60%）
        rate = _super_break_rate(state, u, stats) + _dahlia_super_break_rate(state, u, t)
        if rate > 0:
            sbd = calculate_damage(sb_stats, t, 0, 0, "super_break",
                                   break_element, 80, False,
                                   toughness_dmg=toughness_dmg)
            sbd.final_damage *= rate * break_mult * tbh_mult
            _commit_enemy_damage(state, u, t, sbd.final_damage)
            u.total_damage_dealt += sbd.final_damage
            added += sbd.final_damage
            state.log.append(f'  超击破: {t.name or t.id} {sbd.final_damage:.0f}(削韧{toughness_dmg:.0f})')

    # 云火昭（忘归人天赋）: 独立于主韧性, 击破后仍可削减, 削至0二次击破（元素=攻击者元素）
    if can_tough and t.is_broken and t.extra_toughness_max > 0 and t.extra_toughness > 0:
        t.extra_toughness = max(0, t.extra_toughness - toughness_dmg)
        if t.extra_toughness <= 0:
            cf = calculate_damage(sb_stats, t, 0, 0, "break", break_element, 80, False)
            cf.final_damage *= break_mult
            _commit_enemy_damage(state, u, t, cf.final_damage)
            u.total_damage_dealt += cf.final_damage
            added += cf.final_damage
            t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 2500.0
            _apply_break_debuff(t, break_element, u, state)
            state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
            _process_lc_effects(u, state, "on_weakness_break")
            state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                                    enemy=t, skill_key=skill_key)
            state.log.append(f'  云火昭击破! {t.name or t.id} 二次击破={cf.final_damage:.0f}({break_element})')
    return added


def _deduct_skill_point_cost(state, u, sp_cost) -> bool:
    """v6.7 抽取: 战技点扣费统一入口。火花【爆点】优先抵扣（消耗爆点视为消耗战技点,
    爆点抵扣部分不计入 lc_sp_spent 光锥SP消耗计数）。返回是否扣费成功。"""
    state.extra['_last_sp_spent'] = 0
    if sp_cost <= 0:
        return True
    spent_points = 0
    burst = state.extra.get('sparxie_burst_points', 0.0)
    if burst > 0:
        used = min(float(sp_cost), burst)
        state.extra['sparxie_burst_points'] = burst - used
        sp_cost -= int(used)
        spent_points += int(used)
        # 火花E2: 每消耗1爆点自身暴伤+10% 2回合（最多叠4层; v6.7b: 总层数封顶,
        # 此前每次消耗 append 独立 buff 可突破4层上限）
        spx = next((x for x in state.units
                    if x.char.id == 'sparxie' and x.is_alive and x.eidolon_rank >= 2), None)
        if spx and used > 0:
            existing = next((b for b in spx.buffs
                             if getattr(b, 'param_id', '') == 'sparxie_e2_cd'), None)
            cur_stacks = int(round(existing.attributes.get('CRIT_DMG', 0.0) / 10.0))                 if existing is not None else 0
            new_stacks = min(4, cur_stacks + int(used))
            if existing is not None and new_stacks > 0:
                existing.attributes['CRIT_DMG'] = 10.0 * new_stacks
                existing.remaining_turns = 2  # 再消耗刷新持续时间
            elif new_stacks > 0:
                spx.buffs.append(TimedBuff(source_id='sparxie',
                                           attributes={'CRIT_DMG': 10.0 * new_stacks},
                                           remaining_turns=2, param_id='sparxie_e2_cd',
                                           source_name='火花E2爆点暴伤'))
        if sp_cost <= 0:
            state.extra['_last_sp_spent'] = spent_points
            state.log.append(f'  {u.char.name}: 爆点抵扣{used:.0f}战技点')
            silver = next((ally for ally in state.units
                           if ally.char.id == 'yinlang' and ally.is_alive
                           and ally.invincible_active), None)
            if silver:
                from engine.characters.yinlang import silver_blindbox
                for _ in range(spent_points):
                    silver_blindbox(silver, state)
            return True
    if state.skill_points < sp_cost:
        state.log.append(f'  [WARN] {u.char.name} 战技点不足({state.skill_points}<{sp_cost}), 无法使用技能')
        return False
    state.skill_points -= sp_cost
    spent_points += int(sp_cost)
    state.extra['_last_sp_spent'] = spent_points
    # v6.10.6 C2: 花火幻相——每消耗1战技点叠1层(上限3), 每层全敌受伤+4%(2回合刷新);
    # E2 每层额外降防10%; 行迹2 单回合耗≥3→下次战技免SP; 行迹1 持CD buff者耗SP→花火回1能量
    sparkle = next((x for x in state.units
                    if x.char.id == 'sparkle' and x.is_alive), None)
    if sparkle is not None and sp_cost > 0:
        stacks = min(3, sparkle.extra.get('sparkle_huanxiang', 0) + sp_cost)
        sparkle.extra['sparkle_huanxiang'] = stacks
        for e in state.enemies:
            if getattr(e, 'HP', 0) <= 0:
                continue
            e.add_status(EnemyStatus(id='sparkle_huanxiang_vuln', name='受伤提高',
                                     category='debuff', source='sparkle',
                                     remaining_turns=2,
                                     attributes={'vulnerability': stacks * 0.04}))
            if sparkle.eidolon_rank >= 2:
                e.add_status(EnemyStatus(id='sparkle_huanxiang_def', name='防御降低',
                                         category='debuff', source='sparkle',
                                         remaining_turns=2,
                                         attributes={'def_reduction': stacks * 0.10}))
        state.log.append(f'  幻相: {stacks}层 全敌受伤+{stacks * 4}%'
                         + (' 降防+10%/层' if sparkle.eidolon_rank >= 2 else ''))
        turn_spent = state.extra.get('sparkle_turn_sp_spent', 0) + sp_cost
        state.extra['sparkle_turn_sp_spent'] = turn_spent
        if turn_spent >= 3:
            state.extra['sparkle_free_skill'] = True
        if any(getattr(t, 'hook_name', '') == 'sparkle_basic_energy'
               for t in (sparkle.char.traces or []))                 and any(getattr(b, 'param_id', '') == 'sparkle_cd_buff' for b in u.buffs):
            _gain_energy(sparkle, 1.0, state=state)
            state.log.append('  花火行迹1: 持CD buff者耗SP→花火+1能量')
    # v5.0.1: SP 消耗计数（花花世界迷人眼: 每消耗1SP无视5%防御）
    state.extra['lc_sp_spent'] = state.extra.get('lc_sp_spent', 0) + sp_cost
    # 银狼结界监听结界内任意我方目标的实际战技点消耗。
    silver = next((ally for ally in state.units
                   if ally.char.id == 'yinlang' and ally.is_alive
                   and ally.invincible_active), None)
    if silver:
        from engine.characters.yinlang import silver_blindbox
        for _ in range(spent_points):
            silver_blindbox(silver, state)
    return True


def _skill_level_factor(u, skill_key):
    """E3/E5 数据层当前采用的统一成长因子（每额外等级 +5%）。"""
    levels = (u.extra.get('skill_level_boost', {}) or {}).get(skill_key, 0)
    return 1.0 + 0.05 * max(int(levels), 0)


def _use_skill(u: SimUnit, state: SimState, skill_key: str,
               laugh_n_override: float = None):
    """技能施放编排器（M5b 拆段; 段函数按序调用, 行为与拆分前逐位一致）。

    S1 解析与门控 → S2 资源支付 → S3 HP 消耗 → S4 类型钩子 → S5 伤害主循环
    → 日志/迷迷终结技 → 攻击后结算（银狼/结算管线）→ S7 治疗段 → S8 效果挂载与收尾。
    """
    resolved = _us_resolve_skill(u, state, skill_key)
    if resolved is None:
        return
    skill, skill_key, qianye_new_ult, is_ultimate_action, skill_level_factor, \
        debuffs_before = resolved

    paid = _us_pay_costs(u, state, skill, skill_key, is_ultimate_action)
    if paid is None:
        return
    skill, spent_skill_points = paid

    skill = _us_hp_costs(u, state, skill, skill_key)

    skill = _us_skill_hooks(u, state, skill, skill_key, qianye_new_ult)
    if skill is None:
        return

    total_dmg, effects_pre_applied = _us_damage_loop(
        u, state, skill, skill_key, laugh_n_override, qianye_new_ult)

    # 日志
    # M5a 相位 mimi_ult: 迷迷终结技伤害段（→damage|None; 开拓者·记忆 JSON 无 multipliers）
    md = _char_phase(state, u, 'mimi_ult', skill=skill, skill_key=skill_key)
    if md is not None:
        total_dmg += md
    u.damage_log.append((skill.name, total_dmg, skill_key))
    state.log.append(f'[{state.current_av:6.0f}AV] {u.char.name} {skill.name}: {total_dmg:.0f}')
    # 行动计数（轮次统计用）
    state.action_counts[u.char.id] = state.action_counts.get(u.char.id, 0) + 1

    # M5a 相位 attack_aftermath: 银狼攻击后机制（缺陷植入/E1E4终结技结算/弱点转移）
    _char_phase(state, u, 'attack_aftermath', skill_key=skill_key, total_dmg=total_dmg)
    # M5a 观察相位 defect_implant: 我方攻击时银狼E2 给受击目标植入缺陷
    # （处理器内含 u==silver_wolf 跳过守卫——与原 if/elif 分流等价）
    _obs_phase(state, 'defect_implant', u, total_dmg=total_dmg)

    # M5a 结算管线: v6.6 批1-3 角色技能后结算
    # （缇宝/刻律德菈/丹恒·腾荒/海瑟音/那刻夏/赛飞儿/白厄;
    # 顺序与原内联逐位一致, 锁死于 characters.SETTLE_PIPELINE_ORDER；
    # self/observer 守卫均在处理器内保留）
    _settle_pipeline(state, u, skill, skill_key, total_dmg)

    _us_heal_effects(u, state, skill, skill_key, skill_level_factor)
    _us_effects_and_tail(u, state, skill, skill_key, total_dmg,
                         spent_skill_points, qianye_new_ult, is_ultimate_action,
                         debuffs_before, effects_pre_applied)


def _us_resolve_skill(u: SimUnit, state: SimState, skill_key: str):
    """S1 技能解析与前置门控：键改写→负面快照→技能门槛→倍率/等级调整→目标缓存→使用前钩子。

    返回 (skill, skill_key, qianye_new_ult, is_ultimate_action, skill_level_factor,
    debuffs_before)；门槛不满足或 on_before_skill 消费时返回 None。
    """
    # v5.3 忘归人: 炽灼状态普攻强化为冉冉方炽
    _ensure_phase_tables(state)
    # M5a 相位 key_rewrite: 普攻键改写（→新键|None; fugue/sparxie/qianye）
    nk = _char_phase(state, u, 'key_rewrite', skill_key=skill_key)
    if nk is not None:
        skill_key = nk
    # v6.10 黄泉天赋: 每次施放技能最多触发1次——技能开始清施放者标记
    u.extra.pop('acheron_talent_triggered', None)
    # Some character-specific handlers add debuffs directly instead of going
    # through _apply_skill_effects. Keep a structured snapshot so those paths
    # still participate in the shared on_debuff_applied contract.
    debuffs_before = {
        id(enemy): copy.deepcopy([
            status for status in enemy.statuses
            if status.category in ('debuff', 'dot', 'control')
        ])
        for enemy in state.enemies
    }
    skill = u.char.skills.get(skill_key)
    # M5a 相位 new_ult_check: 新式终结技判定（千冶·刃 skill_enhanced; 下方三处消费）
    qianye_new_ult = bool(_char_phase(state, u, 'new_ult_check', skill_key=skill_key))
    # M5a 相位 skill_gate_pre: 技能资源门槛（→True=中止）。千冶·刃门控走统一入口。
    # 新终结技继续执行下方完整技能、伤害、击杀、光锥和 Hook 管线，不再提前返回到手写结算。
    if _char_phase(state, u, 'skill_gate_pre', skill_key=skill_key):
        return None

    if not skill:
        return None
    # M5a 相位 skill_gate: 施放限制（→True=中止; 银狼无敌期）
    if _char_phase(state, u, 'skill_gate', skill_key=skill_key):
        return None
    # M5a 相位 skill_adjust_pre: 倍率/面板预调（→新skill|None; 可含副作用处理器）
    ns = _char_phase(state, u, 'skill_adjust_pre', skill=skill, skill_key=skill_key)
    if ns is not None:
        skill = ns
    # v6.10.3 P1-6: 技能等级覆盖层（E3/E5, 每级+5%）
    skill_level_factor = _skill_level_factor(u, skill_key)
    if skill_level_factor > 1.0:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= skill_level_factor
        for eff in (skill.effects or []):
            if getattr(eff, 'type', '') == 'shield':
                eff.value = getattr(eff, 'value', 0.0) * skill_level_factor
    # v6.10.3 P1-3: 爻光E4（阿哈额外回合全体欢愉技×1.5）/ E6（自身欢愉技倍率×2）
    if skill_key == 'elation_skill' and state.extra.get('yao_e4_aha') and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 1.5
    # M5a 相位 skill_adjust_post: 倍率后调（→新skill|None; 爻光E6 欢愉技×2）
    ns = _char_phase(state, u, 'skill_adjust_post', skill=skill, skill_key=skill_key)
    if ns is not None:
        skill = ns
    is_ultimate_action = skill_key == 'ultimate' or qianye_new_ult

    # v6.8.2: 每次技能动作开始清空上次命中的目标缓存——
    # last_multihit_targets 只在弹射路径写入, 不清会污染下一动作的
    # 缇宝结界/海瑟音天赋/那刻夏天赋等“受击目标”判定。
    state.extra.pop('last_attack_targets', None)
    state.extra.pop('last_multihit_targets', None)
    state.extra.pop('last_hit_segments', None)  # 逐段命中（含重复段, 那刻夏逐段计数用）
    state.extra.pop('cipher_action_main_target', None)
    state.extra.pop('cipher_action_targets', None)
    # M5a 相位 action_targets_setup: 行动目标预备（赛飞儿老主顾锁定）
    _char_phase(state, u, 'action_targets_setup', skill_key=skill_key)


    # v5.7 开拓者·记忆E4: 能量上限为0的我方目标主动施放技能→迷迷+3%充能
    if (u.char.max_energy or 0) == 0:
        _obs_phase(state, 'zero_energy_cast', u)

    # Hook: 技能使用前
    if state.hooks.trigger(u.char.id, "on_before_skill",
                            u=u, state=state, skill_key=skill_key, skill=skill):
        return None
    # 光锥攻击后事件在伤害循环内派发，必须提前记录本次技能而不是读取上次技能。
    u.extra['lc_last_skill_key'] = skill_key
    u.extra['lc_last_skill_target_type'] = skill.target
    has_single_ally_target = skill.target == 'single_ally' or any(
        getattr(effect, 'target', '') == 'single_ally' for effect in skill.effects)
    if has_single_ally_target:
        u.extra['lc_last_skill_target'] = _pick_single_ally_target(state, u)
    else:
        u.extra.pop('lc_last_skill_target', None)
    state.extra['lc_last_skill_key'] = skill_key
    return (skill, skill_key, qianye_new_ult, is_ultimate_action,
            skill_level_factor, debuffs_before)


def _us_pay_costs(u: SimUnit, state: SimState, skill, skill_key: str,
                  is_ultimate_action: bool):
    """S2 资源支付：SP 扣减/恢复→终结技能量与角色结算→非终结技回能→特殊资源→伴生效果。

    返回 (skill, spent_skill_points)；SP 不足、终结技内联结算或特殊资源不足时返回 None。
    """
    # SP 与能量（通用）
    sp_cost = skill.cost.get("skill_points", 0)
    # M5a 相位 sp_cost_override: SP 消耗覆写（→新值|None; 花火人造花/流萤E1）
    res = _char_phase(state, u, 'sp_cost_override', sp_cost=sp_cost, skill_key=skill_key)
    if res is not None:
        sp_cost = res
    if not _deduct_skill_point_cost(state, u, sp_cost):
        return None
    spent_skill_points = int(state.extra.pop('_last_sp_spent', 0))
    if skill_key in ("basic_attack", "basic_attack_enhanced"):
        # v5.3: 强化普攻也恢复1战技点（实机普攻类统一恢复; 忘归人冉冉方炽等）
        # v5.7: 数据驱动例外——阿格莱雅孤锋千吻/昔涟向着爱与明天"无法恢复战技点"(_sp_recover: 0)
        if skill.cost.get("_sp_recover", 1):
            _gain_skill_points(state)
    # 终结技消耗全部能量；其他技能回复能量
    if is_ultimate_action:
        # M5a 相位 ult_energy_override: 返回 True=已自扣能量（晴歌E6 Fever 保留溢出）
        if not _char_phase(state, u, 'ult_energy_override', skill=skill):
            u.current_energy = 0
        # v6.10.6 D: 通用终结技后回能——消费 JSON effects 的 energy_regen（26角色声明的"终结技后恢复5能量"此前被静默忽略）
        for eff in (skill.effects or []):
            if getattr(eff, 'type', '') == 'energy_regen':
                amt = getattr(eff, 'value', 0.0) or 0.0
                if amt > 0:
                    _gain_energy(u, float(amt), state=state)
        # v6.10.6 B2: 藿藿禳命——我方施放终结技时触发回血
        from engine.characters.huohuo import _huohuo_ruming_heal_all
        _huohuo_ruming_heal_all(state, u)
        # M5a 相位 ult_cast_resource: 终结技资源结算（万敌充能+嘲讽/开拓者·记忆史诗+迷迷）
        _char_phase(state, u, 'ult_cast_resource', skill=skill)
        # M5a 相位 ult_skill_scale: 终结技倍率动态改写（→新skill|None; 火花欢愉度倍率）
        ns = _char_phase(state, u, 'ult_skill_scale', skill=skill)
        if ns is not None:
            skill = ns
        # M5a 相位 ult_cast_post: 终结技后段结算（绯英E6 回能）
        _char_phase(state, u, 'ult_cast_post')
        # M5a 相位 ult_inline: 返回 True=终结技已完全内联结算（含收尾）, 引擎直接返回
        if _char_phase(state, u, 'ult_inline', skill=skill):
            return None
        # M5a 相位 ult_skill_split: 终结技倍率均分改写（→新skill|None; 大丽花）
        ns = _char_phase(state, u, 'ult_skill_split', skill=skill)
        if ns is not None:
            skill = ns
        # v6.7 同行协议·裁决: 队友主动终结技计数
        from engine.characters.himeko_nova import _hn_count_ally_ult
        _hn_count_ally_ult(state, u)
    else:
        # v5.3 流萤战技: 固定恢复60%能量上限（取代标准战技回能, 下方特殊分支处理）
        gain = ENERGY_GAIN.get(skill_key, 0)
        # M5a 相位 energy_gain_override: 非终结技回能覆写（→新值|None;
        # 流萤战技走 post_hp_cast/火花战技0·强化普攻40/绯英欢愉技5）
        og = _char_phase(state, u, 'energy_gain_override', skill_key=skill_key)
        if og is not None:
            gain = og
        _gain_energy(u, gain, state=state)  # v5.7: 迷迷充能 bank 已统一迁入 _gain_energy

    # 特殊能量消耗（新蕊/追忆/万敌充能）——M5a 相位 special_resource_cost:
    # 返回 (abort, new_skill|None); abort=True 资源不足中止
    res = _char_phase(state, u, 'special_resource_cost', skill=skill, skill_key=skill_key)
    if res is not None:
        _abort, _ns = res
        if _abort:
            return None
        if _ns is not None:
            skill = _ns

    # M5a 相位 cast_side_effects: 施放伴生效果（迷迷回血/充能、追忆获取、新蕊清零）
    _char_phase(state, u, 'cast_side_effects', skill=skill, skill_key=skill_key)
    return (skill, spent_skill_points)


def _us_hp_costs(u: SimUnit, state: SimState, skill, skill_key: str):
    """S3 HP 消耗：全队%/自身当前%/自身上限%三段扣血与损失事件→流萤战技后段。

    返回（可能被 post_hp_skill_override 替换的）skill。
    """
    # 全队HP消耗（遐蝶战技等，包含使用者自身，含忆灵）
    hp_pct_allies = skill.cost.get("hp_percent_allies", 0)
    if hp_pct_allies > 0:
        total_lost = 0.0
        affected = []
        # 角色
        for eu in state.units:
            if eu.is_alive:
                lost = eu.current_hp * (hp_pct_allies / 100.0)
                eu.current_hp = max(1, eu.current_hp - lost)
                total_lost += lost
                affected.append((eu, lost))
        # 忆灵（我方全体含忆灵；skill_dragon 除外——"除死龙以外"不扣死龙）
        for ms in state.memsprites:
            if not ms.is_alive:
                continue
            if skill_key == 'skill_dragon' and ms is u.memsprite_unit:
                continue  # 死龙不被自身强化战技扣血
            lost = ms.current_hp * (hp_pct_allies / 100.0)
            ms.current_hp = max(1, ms.current_hp - lost)
            total_lost += lost
            affected.append((ms, lost))
        # M5a 相位 allies_hp_loss: 全队 HP 消耗吸收（遐蝶新蕊/死龙回血）
        _char_phase(state, u, 'allies_hp_loss', total_lost=total_lost)
        # Hook: HP损失事件（Layer 1 — 与现有硬编码并行）
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                 total_lost=total_lost, affected=affected,
                                 skill_key=skill_key)
        from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
        _dispatch_changyeyue_hp_loss(state, affected)
        # 光锥 HP 损失事件属于实际失血单位；只触发施法者会漏掉受影响队友。
        for affected_unit, _lost in affected:
            if isinstance(affected_unit, SimUnit):
                state.extra['lc_last_hp_loss'] = _lost  # v5.4 在火的远处: 单次损失判定
                _process_lc_effects(affected_unit, state, "on_hp_loss")

    # 自身HP消耗（万敌战技/弑王成王/长夜月战技: 消耗当前生命%，不足降至1点）
    hp_pct_self = skill.cost.get("hp_percent_self", 0) or skill.cost.get("hp_percent", 0)
    if hp_pct_self > 0:
        lost = u.current_hp * (hp_pct_self / 100.0)
        u.current_hp = max(1, u.current_hp - lost)
        state.log.append(f'  HP消耗: -{lost:.0f} ({hp_pct_self}%当前生命) → {u.current_hp:.0f}')
        # M5a 相位 self_hp_loss: 自身 HP 消耗吸收（万敌以血还血）
        _char_phase(state, u, 'self_hp_loss', lost=lost)
        # 自身扣血也发 on_hp_loss（符玄E6累计/风堇E2等; 当前无注册者时零行为）
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                total_lost=lost, affected=[(u, lost)], skill_key=skill_key)
        state.extra['lc_last_hp_loss'] = lost
        _process_lc_effects(u, state, "on_hp_loss")
        from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
        _dispatch_changyeyue_hp_loss(state, [(u, lost)])

    # 自身HP消耗（按生命上限%: v5.3 流萤战技消耗40%生命上限, 不足降至1点）
    hp_pct_max_self = skill.cost.get("hp_percent_max_self", 0)
    if hp_pct_max_self > 0:
        lost = u.max_hp * (hp_pct_max_self / 100.0)
        u.current_hp = max(1, u.current_hp - lost)
        state.log.append(f'  HP消耗: -{lost:.0f} ({hp_pct_max_self}%生命上限) → {u.current_hp:.0f}')
        state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                                total_lost=lost, affected=[(u, lost)], skill_key=skill_key)
        state.extra['lc_last_hp_loss'] = lost
        _process_lc_effects(u, state, "on_hp_loss")
        from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
        _dispatch_changyeyue_hp_loss(state, [(u, lost)])

    # M5a 相位 post_hp_cast: HP 消耗后段（流萤战技回能+拉条）
    _char_phase(state, u, 'post_hp_cast', skill_key=skill_key)
    # M5a 相位 post_hp_skill_override: HP 消耗后倍率改写（→新skill|None; 流萤BE 倍率）
    ns = _char_phase(state, u, 'post_hp_skill_override', skill=skill, skill_key=skill_key)
    if ns is not None:
        skill = ns
    return skill


def _us_skill_hooks(u: SimUnit, state: SimState, skill, skill_key: str,
                    qianye_new_ult: bool):
    """S4 类型钩子与过滤：技能类型事件→角色前结算→每局技能钩子→倍率表过滤→遗器拉条。

    返回（可能被过滤的）skill；技能钩子返回 True（已完全处理）时返回 None。
    """
    # Hook: 按技能类型触发
    skill_event = {
        "basic_attack": "on_basic_attack", "skill": "on_skill",
        "ultimate": "on_ultimate", "elation_skill": "on_elation_skill",
    }.get(skill_key)
    if qianye_new_ult:
        skill_event = 'on_ultimate'
    if skill_event:
        state.hooks.trigger(u.char.id, skill_event, u=u, state=state,
                             skill_key=skill_key, skill=skill)

    # M5a 相位 pre_hooks_cast: 类型钩子前的角色结算（创世之诗/风堇净化）
    _char_phase(state, u, 'pre_hooks_cast', skill_key=skill_key)

    # 角色钩子（每局注册）
    for hook in state.skill_hooks.get(u.char.id, []):
        result = hook(u, state, skill_key)
        if result is True:  # 钩子返回 True 表示已完全处理，跳过后续伤害
            return None
    # M5a 相位 skill_filter: 伤害循环前倍率表过滤（→新skill|None; 不死途首战技）
    ns = _char_phase(state, u, 'skill_filter', skill=skill, skill_key=skill_key)
    if ns is not None:
        skill = ns


    # 遗器触发
    if skill_key == "ultimate" and "ult_action_advance_25" in getattr(u, '_active_relic_conditions', set()):
        advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.25
        u._pending_action_advance = advance
        state.log.append(f'  翔鹰拉条: +{advance:.0f}AV')
    return skill


def _us_damage_loop(u: SimUnit, state: SimState, skill, skill_key: str,
                    laugh_n_override, qianye_new_ult: bool):
    """S5 伤害主循环：效果预挂→逐倍率逐目标伤害/弹射→命中存档→攻击后结算簇→削韧。

    返回 (total_dmg, effects_pre_applied)。
    """
    from engine.characters.feixiao import _feixiao_count_attack, _feixiao_on_ally_attack
    from engine.characters.qianye import _qianye_on_ally_attack
    from engine.characters.robin import _robin_concert_extra
    from engine.characters.seele import _apply_luandie
    from engine.characters.the_dahlia import _dahlia_on_ally_attack
    from engine.characters.welt import _welt_ally_hit_hooks
    # 伤害计算
    total_dmg = 0.0
    lc_targets_hit = 0  # v5.0.1: 本次攻击命中目标数（per_target 叠层用）
    state.extra['lc_attack_target_refs'] = []
    effects_pre_applied = False
    # M5a 相位 effects_pre_cast: 效果预挂（→True=已预挂; 流萤火弱点须先于伤害生效）
    if _char_phase(state, u, 'effects_pre_cast', skill=skill, skill_key=skill_key):
        effects_pre_applied = True
    if skill.multipliers:
        stats = _build_effective_stats(u, state)
        alive = state.alive_enemies() or state.enemies
        # M5a 相位 target_order: 目标排序（→alive|None; 万敌优先目标置首）
        _alive2 = _char_phase(state, u, 'target_order', alive=alive, skill_key=skill_key)
        if _alive2 is not None:
            alive = _alive2
        st = None
        if skill_key == "basic_attack": st = "basic"
        elif skill_key in ("skill", "ultimate"): st = skill_key
        elif qianye_new_ult: st = 'ultimate'
        # M5a 相位 attack_type_override: 攻击类型覆写（→st|None; 黄泉E6 视为终结技）
        st2 = _char_phase(state, u, 'attack_type_override', st=st, skill_key=skill_key)
        if st2 is not None:
            st = st2
        attack_type = _skill_attack_type(state, st)

        action_targets = []  # v6.3.0b P1-10: 本次攻击实际命中目标（银狼天赋/E1/E4 消费）
        for mult in skill.multipliers:
            sc = stats.ATK if mult.stat == "ATK" else (stats.HP if mult.stat == "HP" else 0)
            is_crit = stats.CRIT_RATE >= 0.5
            laugh_n = (
                (laugh_n_override if laugh_n_override is not None
                 else state.elation_state.get_good_show_total(u.char.id))
                if mult.damage_type == 'elation' else 0.0
            )
            # v5.3: 逐倍率目标（忘归人强化普攻: 主目标/相邻目标分倍率）
            mt = mult.target or skill.target
            targets = _select_targets(alive, mt)
            mult_scale = (mult.scale / len(targets)
                          if mult.split and targets else mult.scale)
            lc_targets_hit += len(targets)

            if mt == "bounce":
                hits = _bounce_hits(u, mult, state)  # v5.3 弹射段数（含同谐E6 +2; v6.7 绯英/火花）
                # v6.2.1: 声援/乱蝶/击杀管线已移入 _multihit_damage 逐段处理（Codex P1-1）
                total_dmg += _multihit_damage(stats, alive, sc, mult_scale,
                                              mult.damage_type, mult.element or u.char.element, is_crit,
                                              hits, st, true_dmg_ratio=state.realm_true_dmg, u=u, state=state,
                                              scaling_stat_name=mult.stat, attack_type=attack_type,
                                              laugh_n=laugh_n)
                # v6.8.2: 弹射命中汇总进 action_targets, 缓存立即清空防跨动作污染;
                # 重复段另存 last_hit_segments 供那刻夏「每击中1次」逐段计数（Harness 补）
                _mh = state.extra.get('last_multihit_targets', [])
                action_targets.extend(_mh)
                state.extra['last_multihit_targets'] = []
                state.extra['last_hit_segments'] = list(_mh)
            else:
                action_targets.extend(targets)
                for t in targets:
                    _record_lc_attack_target(state, t)
                    from engine.characters.himeko_nova import _hn_count_hits
                    _hn_count_hits(state, u)  # v6.7 歼破协议: 每击中1目标+1充能
                    t_stats = _apply_target_relic_modifiers(stats, u, t)
                    # v6.7b 歼破协议: 战技造成的暴击伤害额外+100%
                    if st == 'skill' and state.extra.get('hn_charge_skill_cd'):
                        t_stats = copy.deepcopy(t_stats)
                        t_stats.CRIT_DMG += 1.0
                    # v5.0 P3: 光锥目标相关条件（对HP≤50%目标增伤等）
                    t_stats = _lc_target_correct(t_stats, u, state, t)
                    # M5a 相位 target_stats_mod: 逐目标面板修饰（→t_stats|None; 银狼E6）
                    ts2 = _char_phase(state, u, 'target_stats_mod', t=t, t_stats=t_stats)
                    if ts2 is not None:
                        t_stats = ts2
                    t_stats = _target_attacker_stats(t_stats, u, state, t, st)
                    target_sc = _target_scaling_stat(stats, sc, mult.stat, state, t)
                    t_crit = t_stats.CRIT_RATE >= 0.5
                    # M5a 相位 crit_override: 暴击判定覆写
                    # （→(t_stats|None, force_crit|None); 布洛妮娅号令/希儿斩尽）
                    co = _char_phase(state, u, 'crit_override', t=t, t_stats=t_stats,
                                     skill_key=skill_key)
                    if co is not None:
                        co_stats, co_crit = co
                        if co_stats is not None:
                            t_stats = co_stats
                        if co_crit is not None:
                            t_crit = True
                    d = calculate_damage(t_stats, _enemy_for_damage(t, st), target_sc, mult_scale, mult.damage_type,
                                         mult.element or u.char.element, 80, t_crit,
                                         skill_type=st, true_dmg_ratio=state.realm_true_dmg,
                                         attack_type=attack_type, laugh_n=laugh_n,
                                         crit_mode="expected")
                    # M5a 相位 damage_mod: 伤害结算修饰（→新final|None; 黄泉原初倍率）
                    dm = _char_phase(state, u, 'damage_mod', final=d.final_damage, st=st)
                    if dm is not None:
                        d.final_damage = dm
                    total_dmg += d.final_damage
                    # M5a 相位 damage_bonus_add: 提交前追伤（→bonus|None; 符玄E6 种陵）
                    bonus = _char_phase(state, u, 'damage_bonus_add', skill_key=skill_key)
                    if bonus:
                        d.final_damage += bonus
                        total_dmg += bonus
                    _, killed_by_hit = _commit_enemy_damage(
                        state, u, t, d.final_damage,
                        damage_type=mult.damage_type, skill_type=st,
                        attack_type=attack_type,
                        cipher_record_amount=(
                            d.final_damage / (1.0 + state.realm_true_dmg)
                            if state.realm_true_dmg > 0 else None))
                    # v5.7: 迷迷的声援逐段触发（每造成1次伤害→额外28%真伤, 实机逐段）
                    from engine.characters.trailblazer_remembrance import _apply_tbr_support
                    total_dmg += _apply_tbr_support(state, u, t, d.final_damage)
                    # M5a 相位 post_hit_debuff: 命中后挂负面（希儿乱蝶快照）
                    _char_phase(state, u, 'post_hit_debuff', t=t, dmg=d.final_damage,
                                skill_key=skill_key)
                    # 乱蝶受击追加真伤（先结算本次伤害, 乱蝶真伤致死也计入击杀）
                    _apply_luandie(state, t, u)
                    # 击杀检测（希儿再现/on_kill钩子）
                    if killed_by_hit:
                        # M5a 相位 on_kill_effect: 击杀伴生（遐蝶倒置的火炬）
                        _char_phase(state, u, 'on_kill_effect')
                # 波次中段刷新：全灭后立即补充敌人
                if not state.alive_enemies():
                    _respawn_wave(state)

        # v6.3.0b P1-10: 本次攻击实际命中目标去重存档（含被本次击杀者; 银狼消费）
        _seen = set()
        state.extra['last_attack_targets'] = [
            t for t in action_targets
            if t is not None and not (id(t) in _seen or _seen.add(id(t)))]

        # M5a 相位 post_attack_elation: 攻击后欢愉追加（→extra|None 计入 total; 银狼持好活）
        extra = _char_phase(state, u, 'post_attack_elation', stats=stats, st=st,
                            attack_type=attack_type, skill_key=skill_key)
        if extra is not None:
            total_dmg += extra

        u.total_damage_dealt += total_dmg
        # v5.0.1: 攻击完成事件（自身 + 广播）— 叠层触发（歌咏/织锦/龙吟等）
        state.extra['lc_attack_targets'] = lc_targets_hit
        # v5.4 论剑: 记录本次攻击首个目标（同目标叠层判定）
        _first = state.extra.get('lc_attack_target_refs', [])
        state.extra['lc_attack_first_target_id'] = _first[0].id if _first else ''
        _process_lc_effects(u, state, "on_self_attack")
        for eu in state.units:
            if eu.is_alive:
                _process_lc_effects(eu, state, "on_attack")
        # M5a 相位 post_attack_cleanup: 攻击后清理（符玄E6 种陵累计清空）
        _char_phase(state, u, 'post_attack_cleanup', skill_key=skill_key)
        # 普攻后全队监听（布洛妮娅E4等"队友普攻→追加攻击"类效果）
        if skill_key == 'basic_attack' and state.alive_enemies():
            first_target = state.alive_enemies()[0]
            state.hooks.trigger_all("on_ally_attack", u=u, state=state,
                                    skill_key=skill_key, skill=skill, target=first_target)

        # ---- v6.7 角色追加结算（伤害循环后, 目标信息就绪）----
        # M5a 相位 goodshow_settle: 持好活/强化普攻追加结算（绯英/火花; 守卫逐条保留）
        _char_phase(state, u, 'goodshow_settle', skill_key=skill_key, total_dmg=total_dmg)
        # 大丽花天赋: 共舞者攻击→E1固定削韧; 另一共舞者攻击→FUA 5×30%(每回合最多1次)
        # v6.7b: 大丽花自身攻击也触发 E1（txt 共舞者含自身）
        if total_dmg > 0 and state.alive_enemies():
            _dahlia_on_ally_attack(state, u)
        # M5a 相位 post_attack_extra: 附加伤害结算（瓦尔特减速附加/推条）
        _char_phase(state, u, 'post_attack_extra', skill_key=skill_key, total_dmg=total_dmg)
        # v6.9.1: 失重通用受击钩子——任何我方攻击命中失重目标→行动延后4%(≤8次/回合)
        # + 行迹1 易伤叠层(最多10层, 此前只瓦尔特攻击触发且固定10%)
        _welt_ally_hit_hooks(state, skill_key)
        # M5a 观察相位 field_react: 结界期队友攻击挂残梅绽（阮·梅; 处理器内保留守卫）
        _obs_phase(state, 'field_react', u, total_dmg=total_dmg)
        # v6.9 知更鸟: 协奏期每次我方攻击后附加120%ATK物理伤(固定双暴)
        if total_dmg > 0:
            _robin_concert_extra(state, u)
        # v6.11.1 晴歌: 我方目标施放攻击→晴歌气氛+1(特邀嘉宾持有者额外+2/E2/律动/偏离和弦)
        if total_dmg > 0:
            state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=True)
        # M5a 观察相位 bait_react: 饲饵受其他目标攻击→不死途回能+FUA（内含守卫）
        _obs_phase(state, 'bait_react', u, total_dmg=total_dmg)
        # v6.9 千冶·刃: 结界期我方每次攻击→目标煞火缠身+1充能
        if total_dmg > 0:
            _qianye_on_ally_attack(state, u)
        # v6.10 飞霄: 每2次攻击+1飞黄 + 队友攻击后立即FUA
        if total_dmg > 0:
            _feixiao_count_attack(state, u, is_ult=(skill_key == 'ultimate'))
            _feixiao_on_ally_attack(state, u)
        # M5a 相位 post_attack_taunt: 攻击后嘲讽（千冶·刃煞火缠身）
        _char_phase(state, u, 'post_attack_taunt', skill_key=skill_key, total_dmg=total_dmg)

        # 【迷迷的声援】已单点化于 _apply_tbr_support（v5.7: 逐段伤害触发, 见伤害循环）

        # M5a 相位 post_attack_mark: 攻击后标记（阿格莱雅间隙织线）
        _char_phase(state, u, 'post_attack_mark', skill=skill, total_dmg=total_dmg)

        # 削韧计算（仅在有伤害倍率且实际造成伤害时触发）
        if total_dmg > 0:
            toughness_targets = state.alive_enemies() or state.enemies
            for eff in skill.effects:
                etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
                if etype != 'toughness_reduction':
                    continue
                base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
                eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
                break_element = u.char.element
                if eff_target == 'bounce':
                    # v5.3 弹射削韧: 每跳均分总削韧, 逐跳随机目标（同谐行迹2: 首跳+100%）
                    hits = _bounce_hits(u, skill.multipliers[0], state) if skill.multipliers else 1
                    per_hit = base_toughness / hits
                    first_double = bool(u.extra.pop('tbh_bounce_first_double', False))
                    for i in range(hits):
                        tgt = random.choice(toughness_targets)
                        td = per_hit * (2.0 if (i == 0 and first_double) else 1.0)
                        total_dmg += _apply_toughness_damage(
                            state, u, tgt, td, break_element, skill_key, stats)
                else:
                    tgt_list = _select_targets(toughness_targets, eff_target)
                    for t in tgt_list:
                        total_dmg += _apply_toughness_damage(
                            state, u, t, base_toughness, break_element, skill_key, stats)
    return total_dmg, effects_pre_applied


def _us_heal_effects(u: SimUnit, state: SimState, skill, skill_key: str,
                     skill_level_factor: float):
    """S7 治疗段：逐 heal 效果计算治疗量→目标选择（忆灵/全队/单体/相邻）→挂载与事件。"""
    # 治疗处理
    for eff in skill.effects:
        if eff.type != 'heal':
            continue
        # 治疗量: HP%/ATK% + 固定值。paramId 数字编码 "hpPct|flat"，或 HEAL_REGISTRY 命名注册
        named = HEAL_REGISTRY.get(eff.param_id or '')
        if named:
            heal_pct = named["hp_pct"]
            heal_flat = named["flat"]
            heal_stat = named.get("stat", "HP")  # v5.3: 灵砂治疗为 ATK 基数
        else:
            parts = eff.param_id.split('|') if eff.param_id else []
            heal_pct = float(parts[0]) if len(parts) > 0 else 0
            heal_flat = float(parts[1]) if len(parts) > 1 else 0
            heal_stat = "HP"
        healing_stats = _build_effective_stats(u, state)
        heal_base = healing_stats.ATK if heal_stat == "ATK" else healing_stats.HP
        # v5.3: 治疗量加成消费（灵砂行迹2 治疗量提高 = BE×10% 上限20%）
        heal_bonus = healing_stats.HEAL_BONUS
        heal_amt = ((heal_base * (heal_pct / 100) + heal_flat)
                    * (1.0 + heal_bonus) * skill_level_factor)
        # M5a 相位 heal_amount_mod: 治疗量修饰（→新值|None; 风堇行迹1 SPD）
        ham = _char_phase(state, u, 'heal_amount_mod', heal_amt=heal_amt)
        if ham is not None:
            heal_amt = ham
        if eff.target == 'memsprite' and u.memsprite_unit and u.memsprite_unit.is_alive:
            # 治疗忆灵（阿格莱雅战技：为衣匠回复50%衣匠生命上限; 风堇: 按风堇生命上限, v5.7）
            ms = u.memsprite_unit
            # M5a 相位 memsprite_heal_base: 忆灵治疗基数（→base|None; 风堇按自身上限）
            base2 = _char_phase(state, u, 'memsprite_heal_base', ms=ms)
            base = base2 if base2 is not None else ms.max_hp
            heal_val = ((base * (heal_pct / 100) + heal_flat)
                        * skill_level_factor)
            ms.current_hp = min(ms.max_hp, ms.current_hp + heal_val)
            state.log.append(f'  治疗忆灵: {ms.data.name}+{heal_val:.0f}HP')
            continue
        if eff.target in ('all_allies', 'all_allies_but_memsprite'):
            # 全队治疗: 角色 + 忆灵（忆灵治疗也计入→收容的暗潮转化）
            tgt_list = [eu for eu in state.units if eu.is_alive]
            if eff.target == 'all_allies':
                tgt_list += [ms for ms in state.memsprites if ms.is_alive]
        elif eff.target == 'single_ally':
            # 单目标治疗（藿藿战技主目标）: 主C惯例
            main = u.extra.get('lc_last_skill_target')
            if main not in state.units or not main.is_alive:
                main = _pick_single_ally_target(state, u)
            tgt_list = [main] if main else [u]
        elif eff.target == 'blast':
            # 相邻治疗（藿藿战技相邻目标）: 主C + 相邻1人(简化: 施法者之外的队友)
            # M5a 观察相位 heal_blast_main: 主C拾取（→单位|None; 希儿惯例）
            main = _obs_phase(state, 'heal_blast_main', u)
            if main:
                others = [x for x in state.units if x.is_alive and x != main and x != u]
                adj = others[0] if others else None
                tgt_list = [main] + ([adj] if adj else [])
            else:
                tgt_list = [u]
        else:
            tgt_list = [u]
        for t in tgt_list:
            amt = heal_amt
            # M5a 相位 heal_target_mod: 施疗者侧逐目标修饰（→新值|None; 风堇低血/藿藿E4）
            tm = _char_phase(state, u, 'heal_target_mod', t=t, heal_amt=heal_amt)
            if tm is not None:
                amt = tm
            # M5a 相位 receive_heal_mod: 受疗者侧修饰与转化（按 t 派发; 万敌受疗/E2 转充能）
            rm = _char_phase(state, t, 'receive_heal_mod', amt=amt)
            if rm is not None:
                amt = rm
            t.current_hp = min(t.max_hp, t.current_hp + amt)
        ms = u.memsprite_unit
        if ms and hasattr(ms, 'cumulative_healing'):
            heal_val = heal_amt * len(tgt_list)
            # M5a 相位 memoir_heal_mod: 忆灵之诗治疗计入修饰（→新值|None; 风堇×1.72）
            hv = _char_phase(state, u, 'memoir_heal_mod', heal_val=heal_val)
            if hv is not None:
                heal_val = hv
            ms.cumulative_healing = getattr(ms, 'cumulative_healing', 0) + heal_val
        state.log.append(f'  治疗: {heal_amt:.0f}×{len(tgt_list)}人')
        # M5a 相位 post_heal: 治疗后结算（藿藿禳命获得）
        _char_phase(state, u, 'post_heal', skill_key=skill_key)
        # Hook: 治疗事件 → 遐蝶收容的暗潮(xinrui转化)通过 on_heal 钩子触发
        state.hooks.trigger_all("on_heal", u=u, state=state,
                                 healer=u, targets=tgt_list, heal_amt=heal_amt)
        from engine.characters.fengjin import _fengjin_talent_heal_buff
        _fengjin_talent_heal_buff(state, u)
        # v5.4 光锥治疗事件（时节不居: 记录治疗量）
        state.extra['lc_last_heal_amt'] = heal_amt
        _process_lc_effects(u, state, "on_heal")


def _us_effects_and_tail(u: SimUnit, state: SimState, skill, skill_key: str,
                         total_dmg: float, spent_skill_points: int,
                         qianye_new_ult: bool, is_ultimate_action: bool,
                         debuffs_before: dict, effects_pre_applied: bool):
    """S8 效果挂载与收尾：TimedBuff 挂载→角色后结算→光锥事件→负面差分触发→收尾钩子。"""
    from engine.characters.cipher import _cipher_attack_aftermath, _cipher_ensure_laozhuke
    # 技能效果→TimedBuff
    if not effects_pre_applied:
        _apply_skill_effects(u, state, skill, skill_key)

    # M5a 相位 post_effects: 效果挂载后结算（藿藿终结技·遣神役鬼）
    _char_phase(state, u, 'post_effects', skill_key=skill_key)

    if qianye_new_ult:
        # M5a 相位 new_ult_finalize: 新终结技收尾（千冶·刃溢出回能）
        _char_phase(state, u, 'new_ult_finalize')

    # 光锥特效触发
    lc_event = None
    if is_ultimate_action: lc_event = "on_ult"
    elif skill_key == "skill": lc_event = "on_skill"
    elif skill_key == "basic_attack": lc_event = "on_basic_attack"  # v5.0 P3
    elif skill_key == "elation_skill": lc_event = "on_elation_skill"  # v5.0.1 启航
    if lc_event:
        _process_lc_effects(u, state, lc_event)
    # M5a 相位 post_lc: 光锥结算后角色处理（遐蝶西风的驻足）
    _char_phase(state, u, 'post_lc', skill_key=skill_key)
    # v5.2 问题3c: 星体差分机——首次攻击后移除首击CR加成（本次攻击已吃到）
    if u.extra.get('diff_machine_cr'):
        u.base_stats.CRIT_RATE = max(0.0, u.base_stats.CRIT_RATE - u.extra.pop('diff_machine_cr'))
        state.log.append('  星体差分机: 首击暴击加成已消耗')

    # Hook: 技能使用后
    if not u.extra.get('acheron_talent_triggered'):
        for enemy in state.enemies:
            negative_statuses = [
                status for status in enemy.statuses
                if status.category in ('debuff', 'dot', 'control')
            ]
            if negative_statuses != debuffs_before.get(id(enemy), []):
                state.hooks.trigger_all('on_debuff_applied', u=u, state=state,
                                        target=enemy)
                break
    # v6.10.3 P1-1: 赛飞儿天赋FUA/E2易伤/E4附加——我方攻击命中老主顾后触发（修正反向）
    _cipher_attack_aftermath(state, u, skill_key)
    _cipher_ensure_laozhuke(state)
    # v6.10.3 P1-3: 爻光天赋【大吉大利】——持有好活当赏时我方攻击后额外欢愉伤害
    from engine.characters.yaoguang import _yaoguang_dajidali
    _yaoguang_dajidali(state, u, skill_key,
                        spent_skill_points=spent_skill_points)
    # v6.10.3 P1-4: 开拓者·欢愉战技/天赋/行迹2内联接线
    from engine.characters.trailblazer_elation import _tb_skill_aftermath
    _tb_skill_aftermath(state, u, skill_key)
    state.hooks.trigger(u.char.id, "on_after_skill", u=u, state=state,
                        skill_key=skill_key, skill=skill, total_dmg=total_dmg)
    # 本次技能键已在伤害事件前写入 u.extra；state.extra 保留兼容读取。


# ---- 角色技能钩子注册表 ----
# 格式: {char_id: [hook_fn(unit, state, skill_key), ...]}
# 在 simulate() 中按队伍组成动态注册


# ---- 有效面板（含欢愉 buff） ----

def _build_effective_stats(u: SimUnit, state=None) -> CombatStats:
    """按基础面板、临时 Buff、命途修饰构建当前有效面板。"""

    from engine.characters.cerydra import _cerydra_jungong_target
    from engine.characters.qianye import _qianye_wrath_active
    s = copy.deepcopy(u.base_stats)
    for b in u.buffs:
        for attr, val in b.attributes.items():
            _apply_stat(s, attr, val)
    # 玩家侧减益也可以携带数值属性（如减速 SPD_PERCENT）。它们必须和
    # TimedBuff 一样参与面板计算，直到在常规回合边界过期或被净化。
    for status in getattr(u, 'statuses', []):
        for attr, val in getattr(status, 'attributes', {}).items():
            _apply_stat(s, attr, val)
    # v6.10.3 P1-3: 爻光终结技抗穿已移至下方统一动态段（此前此处+0.20与新增+0.24双算）
    if state is not None and state.extra.get('_elation'):
        s = state.extra['_elation'].eff_stats(u, state, base_stats=s)
    if u.char.id == 'feixiao' and any(
            getattr(trace, 'hook_name', '') == 'feixiao_trace2'
            for trace in (u.char.traces or [])):
        s.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] = \
            s.CRIT_DMG_BY_ATTACK_TYPE.get('follow_up', 0.0) + 0.36
    # v5.0 P3: 光锥条件修正（自身条件; 目标相关条件由 _lc_target_correct 在伤害循环处理）
    _apply_lc_condition_corrections(u, state, s)
    _apply_team_lc_stack_buffs(u, state, s)
    # v5.1: 长夜月E6 — 在场时我方全体全属性抗性穿透+20%
    if state is not None:
        cy6 = next((x for x in state.units
                    if x.char.id == 'changyeyue' and x.is_alive and x.eidolon_rank >= 6), None)
        if cy6:
            s.RES_PEN_ALL += 0.20
        # v6.7 绯英E6: 欢愉伤害增笑15% + 每100好活当赏额外增笑2%（最多计入1000点）
        ev6 = next((x for x in state.units
                    if x.char.id == 'evanescia' and x.is_alive and x.eidolon_rank >= 6), None)
        if ev6 is u:
            gs = state.elation_state.get_good_show_total('evanescia')
            s.LAUGH_BOOST += 0.15 + 0.02 * min(int(gs // 100), 10)
        # v6.6c: 海瑟音行迹3 EHR>60%每10%增伤15%上限90%（E2 对全队）
        hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
        if hs and (hs is u or hs.eidolon_rank >= 2):
            ehr = hs.base_stats.EFFECT_HIT_RATE
            if ehr > 0.60:
                s.DMG_BONUS_ALL += min(0.90, (ehr - 0.60) / 0.10 * 0.15)
        # v6.6c: 刻律德菈行迹1 ATK>2000 每多100点暴伤+18% 上限360%
        ce = next((x for x in state.units if x.char.id == 'cerydra' and x.is_alive), None)
        if ce is u and s.ATK > 2000:
            s.CRIT_DMG += min(3.60, (s.ATK - 2000) // 100 * 0.18)
        # v6.10.3 P1-2: 赛飞儿行迹3 天赋FUA暴伤+100%（动态, 仅追加攻击）
        cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
        if cp is u and any(getattr(t, 'hook_name', '') == 'cipher_trace1'
                           for t in (cp.char.traces or [])):
            cipher_spd = s.SPD + s._base_SPD * s.SPD_PERCENT
            if cipher_spd >= 170:
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.50)
            elif cipher_spd >= 140:
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.25)
        if cp is u and any(getattr(t, 'hook_name', '') == 'cipher_trace3'
                           for t in (cp.char.traces or [])):
            s.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] = \
                s.CRIT_DMG_BY_ATTACK_TYPE.get('follow_up', 0.0) + 1.00
        # v6.10.3 P1-3: 爻光行迹/星魂动态面板（此前行迹1暴伤/行迹3/E2/E6/终结技抗穿均缺失或错位）
        yao = next((x for x in state.units if x.char.id == 'yaoguang' and x.is_alive), None)
        if yao is not None:
            if state.yao_field_active:
                s.ELATION_LEVEL += state.extra.get('yaoguang_field_elation_bonus', 0.0)
            if u is yao and any(getattr(t, 'hook_name', '') == 'yaoguang_cd_and_sp'
                                for t in (yao.char.traces or [])):
                s.CRIT_DMG += 0.60  # 行迹1·神闲意满: 自身暴伤+60%
            if u is yao and any(getattr(t, 'hook_name', '') == 'yaoguang_spd_to_elation'
                                for t in (yao.char.traces or [])) and s.SPD >= 120:
                s.ELATION_LEVEL += 0.30 + min(s.SPD - 120.0, 200.0) * 0.01  # 行迹3·开屏有礼
            if yao.eidolon_rank >= 2 and state.yao_field_active:
                s.SPD_PERCENT += 0.12  # E2: 结界期间全队速度+12%
                s.ELATION_LEVEL += 0.16  # E2: 结界期间全队欢愉度+16%
            if yao.eidolon_rank >= 6:
                s.LAUGH_BOOST += 0.25  # E6: 我方全体欢愉伤害增笑25%
        if u.yao_res_pen_turns > 0:
            s.RES_PEN_ALL += 0.24  # 终结技: 全队全属性抗性穿透+24%（3回合）
            if yao is not None and yao.eidolon_rank >= 1:
                s.DEF_PEN_BY_TYPE['elation'] = \
                    s.DEF_PEN_BY_TYPE.get('elation', 0.0) + 0.20  # E1: 欢愉伤害无视20%防御
        # v6.10.3 P1-4: 开拓者·欢愉行迹1/行迹3动态面板 + 终结技CD buff（非欢愉路径消费）
        if u.char.id == 'trailblazer_elation':
            if any(getattr(t, 'hook_name', '') == 'trailblazer_cr_and_sp'
                   for t in (u.char.traces or [])):
                s.CRIT_RATE = min(1.0, s.CRIT_RATE + 0.15)  # 行迹1·跟你爆了: 自身暴击率+15%
            if any(getattr(t, 'hook_name', '') == 'trailblazer_atk_to_elation'
                   for t in (u.char.traces or [])) and s.ATK > 1000:
                s.ELATION_LEVEL += min(0.60, (s.ATK - 1000.0) // 200.0 * 0.10)  # 行迹3·快哉快哉
        if u.tb_cd_buff_turns > 0:
            s.CRIT_DMG += 0.50
        # v6.10.6 C: 花火行迹3·夜想曲——全队ATK+45%; 持战技CD buff者全抗穿+10%; E1 谜诡持有者ATK+40%
        spk = next((x for x in state.units if x.char.id == 'sparkle' and x.is_alive), None)
        if spk is not None:
            if any(getattr(t, 'hook_name', '') == 'sparkle_team_cd'
                   for t in (spk.char.traces or [])):
                s.ATK += s._base_ATK * 0.45  # 行迹3: 全队攻击力+45%
                if any(getattr(b, 'param_id', '') == 'sparkle_cd_buff' for b in u.buffs):
                    s.RES_PEN_ALL += 0.10
            if spk.eidolon_rank >= 1 and any(getattr(b, 'param_id', '') == 'sparkle_mystery'
                                             for b in u.buffs):
                s.ATK += s._base_ATK * 0.40
        # 刻律德菈军功/爵位与星魂：全部按当前持有者动态消费，避免换目标后残留。
        if ce:
            cery_target = _cerydra_jungong_target(state, ce)
            if u is cery_target:
                if ce.eidolon_rank >= 1:
                    s.DEF_PEN += 0.16
                if u.extra.get('cerydra_juewei'):
                    s.DEF_PEN += 0.20
                if ce.eidolon_rank >= 2:
                    s.DMG_BONUS_ALL += 0.40
                if ce.eidolon_rank >= 6:
                    s.RES_PEN_ALL += 0.20
            elif u is ce and cery_target is not None:
                if ce.eidolon_rank >= 2:
                    s.DMG_BONUS_ALL += 1.60
                if ce.eidolon_rank >= 6:
                    s.RES_PEN_ALL += 0.20
        # 千冶·刃E2: 我方终结技视为追加攻击，并使全队追加攻击伤害+75%。
        qianye = next((x for x in state.units
                       if x.char.id == 'qianye' and x.is_alive), None)
        if qianye and qianye.eidolon_rank >= 2:
            if _qianye_wrath_active(qianye):
                s.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] = \
                    s.DMG_BONUS_BY_ATTACK_TYPE.get('follow_up', 0.0) + 0.75
        # 千冶·刃行迹3/E4: 结界期间团队增伤；虚无队友决定终结技分支。
        if qianye and _qianye_wrath_active(qianye) and any(
                getattr(t, 'hook_name', '') == 'qianye_trace3'
                for t in (qianye.char.traces or [])):
            s.DMG_BONUS_ALL += 0.50 + (0.50 if qianye.eidolon_rank >= 4 else 0.0)
            has_other_nihility = any(
                x is not qianye and x.is_alive and x.char.path == '虚无'
                for x in state.units)
            if has_other_nihility:
                s.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = \
                    s.DMG_BONUS_BY_SKILL_TYPE.get('ultimate', 0.0) + 0.75
            elif u is qianye:
                s.DMG_BONUS_ALL += 0.75
        if u.char.id == 'anaxa' and u.eidolon_rank >= 6:
            s.DAMAGE_MULTIPLIER = 1.30
        if u.char.id == 'phainon' and u.eidolon_rank >= 2 and u.extra.get('kasier'):
            s.RES_PEN['物理'] = s.RES_PEN.get('物理', 0.0) + 0.20
        # 丹恒·腾荒同袍相关效果：动态消费，换同袍后不会残留面板。
        dht = next((x for x in state.units
                    if x.char.id == 'dan_heng_permansor_terrae' and x.is_alive), None)
        if dht and u.extra.get('dht_tongpao'):
            s.ATK += dht.base_stats.ATK * 0.15
            if dht.eidolon_rank >= 4:
                s.DMG_REDUCTION += 0.20
            if dht.eidolon_rank >= 6:
                s.DEF_PEN += 0.12
        # 星期日E6: 仅其天赋层数允许暴击溢出按1%→2%转暴伤。
        sunday_e6 = any(x.char.id == 'sunday' and x.is_alive and x.eidolon_rank >= 6
                        for x in state.units)
        if sunday_e6 and any(getattr(b, 'param_id', '') == 'sunday_cr'
                             for b in getattr(u, 'buffs', [])) and s.CRIT_RATE > 1.0:
            s.CRIT_DMG += (s.CRIT_RATE - 1.0) * 2.0
            s.CRIT_RATE = 1.0
    return s


def _effective_spd(u: SimUnit, state=None) -> float:
    """有效速度: 含 SPD_PERCENT buff 折叠后的行动条速度。

    AV 计算统一走此函数（v5.0 接线: SPD_PERCENT 不再只影响伤害）。
    折叠语义: 静态 SPD_PERCENT 已由 attributes.py:362 折进 base_stats.SPD 且字段残留，
    故此处只叠加战斗 TimedBuff 新增的百分比（_apply_stat 战斗层只加字段不折叠）。
    注意: 忆灵速度走运行时字段 action_spd（v5.2 问题1: 不写 MemSprite 配置）。
    """
    s = _build_effective_stats(u, state)
    buff_pct = s.SPD_PERCENT - u.base_stats.SPD_PERCENT  # 战斗中新增部分
    return max(s.SPD + s._base_SPD * buff_pct, 1.0)


def _memsprite_action_speed(state, ms_unit) -> float:
    """返回忆灵下一次 Y 轴行动使用的速度。"""
    spd = ms_unit.action_spd
    if getattr(ms_unit.data, 'name', '') != '死龙':
        return spd
    xiadie = next((u for u in state.units
                   if u.char.id == 'xiadie' and u.is_alive), None)
    if not xiadie:
        return spd
    # 遐蝶行迹2: 遐蝶自身生命值不低于50%时，死龙速度提高40%。
    if xiadie.current_hp >= xiadie.max_hp * 0.5:
        spd *= 1.4
    if ms_unit.extra.get('xiadie_spd_boost'):
        spd *= 2.0
    return spd


def _apply_stat(stats: CombatStats, stat_type: str, value: float):
    """将单个属性值应用到 CombatStats（不依赖 character 上下文）"""
    pct_types = {"HP_percent": "HP_PERCENT", "ATK_percent": "ATK_PERCENT",
                 "DEF_percent": "DEF_PERCENT", "SPD_percent": "SPD_PERCENT",
                 "SPD_PERCENT": "SPD_PERCENT",
                 "CRIT_RATE": "CRIT_RATE", "CRIT_DMG": "CRIT_DMG",
                 "BREAK_EFFECT": "BREAK_EFFECT", "EFFECT_HIT_RATE": "EFFECT_HIT_RATE",
                 "EFFECT_RES": "EFFECT_RES", "ENERGY_REGEN": "ENERGY_REGEN",
                 "DMG_BONUS_ALL": "DMG_BONUS_ALL", "DMG_BONUS_DOT": "DMG_BONUS_DOT",
                 "HEAL_BONUS": "HEAL_BONUS", "SHIELD_BONUS": "SHIELD_BONUS",
                 "TOUGHNESS_EFFICIENCY": "TOUGHNESS_EFFICIENCY",
                 "ELATION_LEVEL": "ELATION_LEVEL", "LAUGH_BOOST": "LAUGH_BOOST",
                 "VULNERABILITY_APPLIED": "VULNERABILITY_APPLIED",
                 "DMG_REDUCTION": "DMG_REDUCTION", "DEF_PEN": "DEF_PEN",
                 "DEF_REDUCTION": "DEF_REDUCTION", "RES_PEN_ALL": "RES_PEN_ALL",
                 "HP_PERCENT": "HP_PERCENT", "ATK_PERCENT": "ATK_PERCENT",
                 "DEF_PERCENT": "DEF_PERCENT", "SPD_PERCENT": "SPD_PERCENT"}
    field_name = pct_types.get(stat_type, stat_type)
    if stat_type in pct_types or field_name != stat_type:
        setattr(stats, field_name, getattr(stats, field_name, 0) + value / 100.0)
        # 百分比属性折叠进实际面板（用 _base_* 白值）— 否则 ATK/DEF/HP_PERCENT 只设动态属性无人消费
        # SPD_PERCENT 不在此处直接折叠；_effective_spd() 会以白值合并战斗中
        # 新增的百分比，供行动条和所有拉条统一使用。
        pct = value / 100.0
        if field_name == 'ATK_PERCENT':
            stats.ATK += getattr(stats, '_base_ATK', stats.ATK) * pct
        elif field_name == 'DEF_PERCENT':
            stats.DEF += getattr(stats, '_base_DEF', stats.DEF) * pct
        elif field_name == 'HP_PERCENT':
            stats.HP += getattr(stats, '_base_HP', stats.HP) * pct
    elif stat_type.startswith("DMG_BONUS_ATK_"):
        # v5.6: 按攻击类别增伤（火舞·仅追加攻击）, 须在 DMG_BONUS_ 前缀之前
        key = stat_type[len("DMG_BONUS_ATK_"):].lower()
        stats.DMG_BONUS_BY_ATTACK_TYPE[key] = stats.DMG_BONUS_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("CRIT_DMG_ATK_"):
        # v5.6: 按攻击类别暴伤（都蓝王朝·5层FUA暴伤+25%）
        key = stat_type[len("CRIT_DMG_ATK_"):].lower()
        stats.CRIT_DMG_BY_ATTACK_TYPE[key] = stats.CRIT_DMG_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DMG_BONUS_") and stat_type != "DMG_BONUS_ALL" and stat_type != "DMG_BONUS_DOT":
        # v5.4 元素增伤（中文键 DMG_BONUS_虚数 等）优先于技能类型增伤
        if stat_type[len("DMG_BONUS_"):] in ("物理", "火", "冰", "雷", "风", "量子", "虚数"):
            stats.DMG_BONUS[stat_type[len("DMG_BONUS_"):]] += value / 100.0
            return
        # DMG_BONUS_SKILL / DMG_BONUS_BASIC / DMG_BONUS_ULTIMATE
        key = stat_type[len("DMG_BONUS_"):].lower()
        stats.DMG_BONUS_BY_SKILL_TYPE[key] = stats.DMG_BONUS_BY_SKILL_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("VULNERABILITY_APPLIED_"):
        key = stat_type[len("VULNERABILITY_APPLIED_"):].lower()
        stats.VULNERABILITY_APPLIED_BY_TYPE[key] = stats.VULNERABILITY_APPLIED_BY_TYPE.get(key, 0) + value / 100.0
    elif stat_type == "DEF_PEN_MEMSPRITE":
        stats.DEF_PEN_MEMSPRITE += value / 100.0
    elif stat_type == "_base_SPD":
        stats._base_SPD += value
        stats.SPD += value
    elif stat_type.startswith("DEF_PEN_SKILL_"):
        # v5.6: 按技能类型无视防御（流光·仅终结技）, 须在 DEF_PEN_ 前缀之前
        key = stat_type[len("DEF_PEN_SKILL_"):].lower()
        stats.DEF_PEN_BY_SKILL_TYPE[key] = stats.DEF_PEN_BY_SKILL_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DEF_PEN_ATK_"):
        key = stat_type[len("DEF_PEN_ATK_"):].lower()
        stats.DEF_PEN_BY_ATTACK_TYPE[key] = stats.DEF_PEN_BY_ATTACK_TYPE.get(key, 0) + value / 100.0
    elif stat_type.startswith("DEF_PEN_"):
        key = stat_type[len("DEF_PEN_"):].lower()
        stats.DEF_PEN_BY_TYPE[key] = stats.DEF_PEN_BY_TYPE.get(key, 0) + value / 100.0
    else:
        # 含 DMG_BONUS_{ELEMENT} 等
        for elem in ["物理", "火", "冰", "雷", "风", "量子", "虚数"]:
            if stat_type == f"DMG_BONUS_{elem}":
                stats.DMG_BONUS[elem] = stats.DMG_BONUS.get(elem, 0) + value / 100.0
                return
        # fallback: RES_PEN 等
        if stat_type.startswith("RES_PEN_"):
            elem = stat_type[len("RES_PEN_"):]
            if elem != "ALL":
                stats.RES_PEN[elem] = stats.RES_PEN.get(elem, 0) + value / 100.0


# ---- 弱点击破 ----

# 元素击破→DOT系数 (倍率 = 系数 × 击破基础值)
BREAK_DOT_MULTIPLIERS = {
    "物理": {"name": "裂伤", "multiplier": 1.0, "type": "dot"},
    "火":   {"name": "灼烧", "multiplier": 1.0, "type": "dot"},
    "冰":   {"name": "冻结", "multiplier": 0.0, "type": "freeze"},
    "雷":   {"name": "触电", "multiplier": 1.0, "type": "dot"},
    "风":   {"name": "风化", "multiplier": 1.0, "type": "dot_wind"},
    "量子": {"name": "纠缠", "multiplier": 0.0, "type": "entangle"},
    "虚数": {"name": "禁锢", "multiplier": 0.0, "type": "imprison"},
}

def _apply_break_debuff(enemy, element: str, attacker, state=None):
    """击破时对敌人施加属性异常状态（state 传入时对 DOT 类写攻击者面板快照, 供敌方回合跳伤）
    v5.6 说明: 击破异常不做 EHR 检定——实机弱点击破时异常必中（免疫除外）"""
    info = BREAK_DOT_MULTIPLIERS.get(element, {})
    if not info:
        return
    enemy.break_element = element
    enemy.break_debuff_name = info["name"]
    enemy.break_debuff_turns = 2  # 异常持续2回合
    category = 'dot' if info["type"] in ('dot', 'dot_wind') else (
        'control' if info["type"] in ('freeze', 'imprison') else 'debuff'
    )
    status = EnemyStatus(
        id=f'break:{element}', name=info["name"], category=category,
        source=getattr(getattr(attacker, 'char', None), 'id', ''),
        remaining_turns=2, removable=True,
    )
    # DOT 快照: 击破时定格攻击者有效面板（数值确定性, 攻击者死亡/换人/buff衰减不影响）
    if state is not None and hasattr(attacker, 'base_stats'):
        status.attributes['dot_snapshot'] = copy.deepcopy(_build_effective_stats(attacker, state))
        status.attributes['dot_element'] = element
        status.attributes['dot_multiplier'] = info["multiplier"] * 100.0
    enemy.add_status(status)
    if info["type"] == "freeze":
        # 冰冻结：仅标记，50%推条在敌人行动时结算（turn_start处理）
        pass
    elif info["type"] == "imprison":
        # 虚数禁锢：减速20%（记录被减部分, 倒计时结束恢复）
        enemy.SPD += enemy.extra.pop('imprison_speed_reduced', 0.0)  # 还原残留记录, 防重复施加叠加
        enemy.extra['imprison_speed_reduced'] = enemy.SPD * 0.20
        enemy.SPD *= 0.80


# ---- 波次 ----


def _respawn_wave(state):
    """全场敌人死亡后重生（保留 blueprint 在 state.extra 中; v6.5 异构敌人按模板列表逐只重建）"""

    from engine.characters.acheron import _acheron_apply_entry_effects
    from engine.characters.anaxa import _anaxa_apply_entry_effects
    from engine.characters.busitu import _busitu_rebind_bait
    from engine.characters.cipher import _cipher_pick_laozhuke, _cipher_trace3_apply_vuln
    from engine.characters.phainon import _apply_phainon_tech_wave, _phainon_implant_phys_weak
    from engine.characters.qianye import _qianye_sync_wrath_enemy_effects, _qianye_wrath_active
    from engine.characters.the_dahlia import _apply_dahlia_baisie
    bps = state.extra.get('enemy_blueprints')
    bp = state.extra.get('enemy_blueprint')
    n = state.extra.get('num_enemies', 3)
    if not bp and not bps:
        return
    state.extra['wave'] = state.extra.get('wave', 1) + 1
    state.enemies = []
    navs = state.extra.get('navs', {})
    if bps:
        pairs = [(tpl, i) for i, tpl in enumerate(bps)]
    else:
        pairs = [(bp, i) for i in range(n)]
    for tpl, i in pairs:
        # deepcopy: 浅拷贝会让同波敌人共享 extra/statuses（av_delayed 互相污染）
        e = copy.deepcopy(tpl)
        w = state.extra['wave']
        e.id = f'{tpl.id}_w{w}_{i}'
        state.enemies.append(e)
        # 重置敌方行动条 AV（含 stamp）
        _set_av(state, navs, ('e', i), state.current_av + AV_PER_TURN / max(e.SPD, 1.0))
    _acheron_apply_entry_effects(state)
    # v6.10.3 P1-2: 赛飞儿行迹3 新波敌人重建易伤（对称维护）
    cipher = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cipher and any(getattr(t, 'hook_name', '') == 'cipher_trace3'
                      for t in (cipher.char.traces or [])):
        _cipher_trace3_apply_vuln(state)
    if cipher:
        _cipher_pick_laozhuke(state, cipher)
    anaxa = next((x for x in state.units if x.char.id == 'anaxa'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if anaxa:
        _anaxa_apply_entry_effects(state, anaxa)
    # v6.3.0 流萤秘技: 每波次重新施加火弱点+伤害（秘技生效时）
    if state.extra.get('firefly_tech_active'):
        ff = next((x for x in state.units if x.char.id == 'firefly' and x.is_alive), None)
        if ff:
            from engine.characters.firefly import _apply_firefly_tech_wave
            _apply_firefly_tech_wave(state, ff)
    # v6.7 姬子·启行秘技: 每个波次开始时立即施放1次战技（拓星巡航, 进战）
    if state.extra.get('hn_tech_active'):
        hn = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
        if hn:
            _use_skill(hn, state, 'skill')
            state.log.append('  姬子·启行秘技: 本波次开始立即施放战技')
    # v6.9 知更鸟秘技: 每个波次开始回5能量（酣醉序曲, 非进战领域）
    if state.extra.get('robin_tech_active'):
        robin = next((x for x in state.units
                      if x.char.id == 'robin' and x.is_alive), None)
        if robin:
            _gain_energy(robin, 5.0, state=state)
            state.log.append('  知更鸟秘技: 本波次回5能量')
    # v6.10 飞霄秘技: 每波200%ATK必暴风伤(每多1敌+100%上限1000%)+1飞黄（岚身, 进战）
    if state.extra.get('feixiao_tech_active'):
        feixiao = next((x for x in state.units
                        if x.char.id == 'feixiao' and x.is_alive), None)
        if feixiao:
            alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
            if alive:
                stats = _build_effective_stats(feixiao, state)
                scale = 200.0 + min(len(alive) - 1, 8) * 100.0  # 每多1敌+100%, 上限1000%
                for e in alive:
                    d = calculate_damage(stats, e, stats.ATK, scale,
                                         'direct', '风', 80, True, crit_mode='boolean')
                    _commit_enemy_damage(state, feixiao, e, d.final_damage)
                    feixiao.total_damage_dealt += d.final_damage
                state.log.append(f'  飞霄秘技: 每波{scale:.0f}%ATK必暴({len(alive)}敌)')
            state.log.append(f'  飞霄秘技: 新波伤害结算，飞黄保持{feixiao.extra["feixiao_fly"]}/12')
    if state.extra.get('acheron_tech_active'):
        acheron = next((x for x in state.units if x.char.id == 'acheron' and x.is_alive), None)
        if acheron:
            from engine.systems.techniques import _tech_acheron
            _tech_acheron(state, acheron, is_opener=True)
    # v6.8b: 白厄秘技: 每个波次开始全敌200%ATK物理伤（终结之始, 进战）
    if state.extra.get('phainon_tech_active'):
        phn = next((x for x in state.units
                    if x.char.id == 'phainon' and x.is_alive), None)
        if phn:
            _apply_phainon_tech_wave(state, phn)
    # v6.2.1: 至暗之谜期间新波敌人同样获得+30%易伤（Codex P2-7: 此前跨波口径漂移,
    # 退出时 _exit_darkness 对称回减）
    if any(getattr(u, 'is_darkness', False) and u.is_alive for u in state.units):
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
    # v6.3.0b P1-9: 银狼E2「入战受伤+20%」对新波敌人同样生效
    sw_e2 = next((x for x in state.units if x.char.id == 'silver_wolf'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if sw_e2:
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.20
    from engine.characters.yinlang import _silver_wolf_apply_entry_effects
    _silver_wolf_apply_entry_effects(state)
    if state.extra.get('yinlang_tech_active'):
        yinlang = next((x for x in state.units
                        if x.char.id == 'yinlang' and x.is_alive), None)
        if yinlang:
            from engine.characters.yinlang import silver_technique_wave
            silver_technique_wave(yinlang, state)
    # v6.7b: 大丽花E2「敌方目标入场时陷败谢+全抗-20%」对新波敌人同样生效
    dl_e2 = next((x for x in state.units if x.char.id == 'the_dahlia'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if dl_e2:
        for e in state.enemies:
            for elem in list(e.element_res.keys()):
                e.element_res[elem] = e.get_res(elem) - 0.20
            _apply_dahlia_baisie(dl_e2, state, e, turns=3)
        state.log.append('  大丽花E2: 新波敌人全抗-20% + 败谢(3回合)')
    # v6.8.1: 缇宝结界易伤+30% 对新波敌人重建（到期回减由 _tribbie_remove_field 对称）
    if state.extra.get('tribbie_field_turns', 0) > 0:
        trib = next((x for x in state.units if x.char.id == 'tribbie' and x.is_alive), None)
        if trib:
            for e in state.enemies:
                e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
            state.log.append('  缇宝结界: 新波敌人受伤+30%重建')
    # v6.8.1: 海瑟音结界（ATK-15%/DEF-25%/E4全抗-20%）对新波敌人重建
    if state.extra.get('hysilens_field_turns', 0) > 0:
        hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
        if hs:
            for e in state.enemies:
                e.extra['hysilens_field'] = True
                if hs.eidolon_rank >= 4:
                    for elem in list(e.element_res):
                        e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
                    e.extra['hysilens_e4_res'] = True
            state.log.append('  海瑟音结界: 新波敌人重建' + (' + E4全抗-20%' if hs.eidolon_rank >= 4 else ''))
    # v6.6b P1-2: 白厄变身期间新波敌人同样植入物理弱点
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('kasier') and x.is_alive), None)
    if ph:
        _phainon_implant_phys_weak(state)
    busitu = next((x for x in state.units
                   if x.char.id == 'busitu' and x.is_alive), None)
    if busitu:
        _busitu_rebind_bait(state, busitu)
    qianye = next((x for x in state.units
                   if x.char.id == 'qianye' and x.is_alive
                   and _qianye_wrath_active(x)), None)
    if qianye:
        _qianye_sync_wrath_enemy_effects(state, qianye)
    _lc_refresh_love_blank(state)
    w = state.extra.get('wave', 1)
    state.log.append(f'--- 第{w}波 ---')
    # 波次开始：触发所有角色光锥的 on_wave_start 特效
    for u in state.units:
        if u.is_alive:
            _process_lc_effects(u, state, "on_wave_start")
            state.hooks.trigger(u.char.id, "on_wave_start", u=u, state=state)


# ---- 通用AI ----


# ---- 行动条第四象限模型（X轴额外回合队列） ----

def _should_ult_now(u, state) -> bool:
    """判定单位是否可以释放终结技（满资源）"""
    if not isinstance(u, SimUnit) or not u.is_alive:
        return False
    cid = u.char.id
    from engine.characters.himeko_nova import _hn_realm_blocks_ult
    if _hn_realm_blocks_ult(state, u):
        return False  # v7.2.0 裁决A: 拓星视界占境界→遐蝶/白厄永封终结技
    if u.char.path == "欢愉":
        return False  # 欢愉特殊资源，由各自 AI 管理
    if cid == 'firefly' and u.extra.get('combustion'):
        return False  # v5.3 完全燃烧期间无法施放终结技
    if cid == 'phainon':
        # v6.8.1: 白厄能量上限12仅为资源计数器, 普攻/战技回能即截满——按火种≥12判定,
        # 否则每次行动后都排入假终结技空转（刷日志/虚增行动计数）
        return u.extra.get('huozhong', 0) >= 12
    if cid == 'xiadie':
        from engine.characters.xiadie import xiadie_xinrui_cap
        return u.xinrui >= xiadie_xinrui_cap(u) and not (u.memsprite_unit and u.memsprite_unit.is_alive)
    if cid == 'xilian':
        # 用户确认实机: 召唤德谬歌后, 忆灵技(选择释放花与箭/此诗献予)类似终结技——
        # 只要追忆≥12 可随时选择释放; 首次终结技需追忆≥24 且单场1次
        if u.is_ripple:
            return u.zhuiyi >= 12
        return u.zhuiyi >= 24 and not u.extra.get('xilian_ult_used')
    return bool(u.char.max_energy and 0 < u.char.max_energy < 1000
                and u.current_energy >= u.char.max_energy)


def _enqueue_ult(state, u):
    """终结技入 X 轴队列"""
    # v5.0 P4: 冻结期间禁止额外回合（用户实机语义: 冻结下无法触发任何 X 轴回合）
    if any(getattr(st, 'name', '') == '冻结' for st in getattr(u, 'statuses', [])):
        state.log.append(f'  {u.char.name}冻结中: 无法释放终结技')
        return
    if any(x is u for x, k in state.extra.get('extra_turns', [])):
        return  # 已在队列
    state.extra.setdefault('extra_turns', []).append((u, 'ult'))
    state.log.append(f'  >>> 终结技入队: {u.char.name}')


def _sweep_ults(state):
    """行动后：全队满资源→追加 X 轴队尾（替代原 _check_ult_interrupts）"""
    guard = state.extra.setdefault('ult_chain_guard', 0)
    if guard >= 4:
        return
    for u in state.units:
        if _should_ult_now(u, state):
            _enqueue_ult(state, u)
            state.extra['ult_chain_guard'] = guard + 1
            return  # 一次一个


def _ai_ult_check(state, actor):
    """常规回合 phase-1：actor 自己/全队满资源→开大入队+hold actor"""
    guard = state.extra.setdefault('ult_chain_guard', 0)
    if guard >= 4:
        return False
    # actor 自己优先
    if _should_ult_now(actor, state):
        _enqueue_ult(state, actor)
        state.extra['ult_chain_guard'] = guard + 1
        return True
    # 全队（例2: 遐蝶待机时风堇开大）
    for u in state.units:
        if u is actor:
            continue
        if _should_ult_now(u, state):
            _enqueue_ult(state, u)
            state.extra['ult_chain_guard'] = guard + 1
            return True
    return False


def _ensure_marker_system(state):
    """行动条标记系统惰性创建入口（v6.3.0b P1-1: 秘技阶段与技能效果共用）。

    _marker_sys 原只在 spawn_marker 技能效果分支首次创建, 灵砂秘技执行点在其之前
    → sys 为 None 时浮元从未召唤。所有 spawn/advance 路径统一经本函数。
    """
    sys = state.extra.get('_marker_sys')
    if sys is None:
        sys = TimelineMarkerSystem()
        from engine.characters import (marker_actions, marker_despawns,
                                       marker_spawns)
        sys.action_handlers.update(marker_actions(state))
        sys.despawn_handlers.update(marker_despawns(state))
        sys.spawn_handlers.update(marker_spawns(state))
        state.extra['_marker_sys'] = sys
    return sys


def _next_y_actor(state):
    """Y 轴选择：合并角色+SPD>0忆灵，按(av, -stamp)取最小（后到先动）"""
    navs = state.extra.get('navs', {})
    stamps = state.extra.get('av_stamp', {})
    candidates = []  # (av, -stamp, key, unit)
    for i, u in enumerate(state.units):
        if u.is_alive and i in navs:
            candidates.append((navs[i], -stamps.get(i, 0), i, u))
    if state.memsprites:
        for ms in state.memsprites:
            if not ms.is_alive or ms.action_spd <= 0:
                continue  # 界外忆灵(SPD=0)不走Y轴
            key = ('ms', id(ms))
            ms_av = ms.extra.get('next_av')
            if ms_av is None:
                ms_av = state.current_av + AV_PER_TURN / ms.action_spd
            candidates.append((ms_av, -stamps.get(key, 0), key, ms))
    # v5.3: 行动条标记（浮元/倒计时）
    if state.markers:
        for m in state.markers:
            if not m.is_alive:
                continue
            key = ('mk', id(m))
            m_av = m.extra.get('next_av')
            if m_av is None:
                m_av = state.current_av + AV_PER_TURN / max(m.action_spd, 1.0)
            candidates.append((m_av, -stamps.get(key, 0), key, m))
    # 敌方（Y轴行动条, 死亡敌人 HP<=0 天然过滤）
    for i, e in enumerate(state.enemies):
        if e.HP > 0:
            key = ('e', i)
            e_av = navs.get(key, e.av)
            candidates.append((e_av, -stamps.get(key, 0), key, e))
    if not candidates:
        return None, float('inf')
    best = min(candidates, key=lambda c: (c[0], c[1]))
    return best[3], best[0]


# ════════ 敌方攻击系统（Y轴行动条 + 选人 + 受击 + 死亡）════════


def _enemy_attack_stats(enemy) -> CombatStats:
    """敌方攻击方面板（只 ATK; 不暴击/无增伤穿透）
    v6.3.0b P1-7: 攻击力降低缺陷在此消费（此前只复制 enemy.ATK, atk_down 无消费端）
    v6.4: 敌方自增益 ATK_PERCENT 消费（与玩家降攻并存）
    v6.4b: ATK_PERCENT/ATK_percent 别名兼容（与 SPD 的 _enemy_status_sum 对称）
    v6.6c P1: 海瑟音结界敌ATK-15%"""
    s = CombatStats()
    s.ATK = enemy.ATK * (1.0 - enemy.status_attribute('atk_down')) \
        * (1.0 + _enemy_status_sum(enemy, 'ATK_PERCENT', 'ATK_percent')) \
        * (0.85 if enemy.extra.get('hysilens_field') else 1.0)
    s.DMG_REDUCTION = enemy.status_attribute('outgoing_dmg_reduction')
    s.CRIT_RATE = 0.0
    s.CRIT_DMG = 0.0
    return s


def _select_enemy_target(state, attacker=None):
    """敌方选人: 嘲讽加权随机（角色 taunt + 忆灵 max_taunt, 排除 is_backup 后援）
    v5.7: attacker=发起攻击的敌方单位时, 若其带【嘲讽】状态(我方施加)→强制选择施加者"""
    if attacker is not None:
        applier_id = next((st.attributes.get('applier')
                           for st in getattr(attacker, 'statuses', [])
                           if getattr(st, 'name', '') == '嘲讽'), None)
        if applier_id:
            forced = next((u for u in state.units
                           if u.char.id == applier_id and u.is_alive), None)
            if forced:
                return forced
    candidates = []
    for u in state.units:
        if u.is_alive:
            # v5.4 受击概率提高（落日时起舞×6/制胜的瞬间×3: taunt_mult 由 LC 设置）
            taunt = (u.char.taunt * u.extra.get('taunt_mult', 1.0)
                     * u.extra.get('qianye_taunt_mult', 1.0))
            candidates.append((taunt, u))
    for ms in state.memsprites:
        if ms.is_alive and not ms.is_backup:
            candidates.append((ms.data.max_taunt, ms))
    if not candidates:
        return None
    # v5.0 P4: 嘲讽状态强制选中（持【嘲讽】的存活单位）
    taunted = [c for _, c in candidates if isinstance(c, SimUnit)
               and any(getattr(st, 'name', '') == '嘲讽' for st in c.statuses)]
    if taunted:
        return taunted[0]
    weights = [w for w, _ in candidates]
    return random.choices([c for _, c in candidates], weights=weights, k=1)[0]


def _apply_enemy_taunt(state, applier, enemies, turns=2):
    """v5.7 嘲讽通用机制（万敌终结技; 后续受击概率.txt 8 角色复用）:
    对敌方施加【嘲讽】——被嘲讽敌人攻击时强制选择施加者（_select_enemy_target 消费）"""
    from engine.models.enemy import EnemyStatus
    for e in enemies:
        if e is None or getattr(e, 'HP', 0) <= 0:
            continue
        existing = next((s for s in e.statuses if s.id == 'taunt'), None)
        if existing:
            existing.remaining_turns = max(existing.remaining_turns, turns)
        else:
            e.statuses.append(EnemyStatus(id='taunt', name='嘲讽', category='debuff',
                                          source=applier.char.id, remaining_turns=turns,
                                          attributes={'applier': applier.char.id}))
        state.log.append(f'  嘲讽: {e.name or e.id} → 只能攻击{applier.char.name}({turns}回合)')


def _apply_player_status(state, target, status: PlayerStatus) -> bool:
    """敌方 debuff 施加管线（v5.0 P4）: 免疫检查 → EHR 命中检定 → 施加。

    免疫消费: 万敌血仇免疫控制（debt_control_immune）、符玄遁甲星舆次数
    （fuxuan_cc_resist_charges）。命中检定: calc_effect_probability(base_chance, 0, RES)。
    """

    from engine.characters.robin import _robin_concert_active
    from engine.systems.techniques import calc_effect_probability
    if not isinstance(target, SimUnit) or not target.is_alive:
        return False
    # ① 免疫检查（仅控制类; 符玄次数是全队机制, 检查队内符玄）
    if status.category == 'control':
        if target.char.id == 'robin' and _robin_concert_active(target):
            state.log.append(f'  知更鸟协奏: 免疫{status.name}')
            return False
        if target.char.id == 'mydei' and target.extra.get('is_blood_debt') \
                and target.extra.get('debt_control_immune'):
            state.log.append(f'  万敌三十僭主: 免疫{status.name}')
            return False
        # v5.7: 长夜月至暗之谜/忆质≥16 免疫控制类负面状态（长夜月.txt 终结技/天赋）
        if target.char.id == 'changyeyue' and \
                (target.is_darkness or getattr(target, 'yizhi', 0) >= 16):
            state.log.append(f'  长夜月至暗/忆质≥16: 免疫{status.name}')
            return False
        fu = next((x for x in state.units
                   if x.char.id == 'fu_xuan' and x.is_alive), None)
        if fu and fu.extra.get('fuxuan_cc_resist_charges', 0) > 0:
            fu.extra['fuxuan_cc_resist_charges'] -= 1
            state.log.append(f'  符玄遁甲星舆: 抵抗{status.name}(剩余{fu.extra["fuxuan_cc_resist_charges"]}次)')
            return False
    # ② EHR 命中检定（目标 EFFECT_RES 含藿藿控抗/风堇抗性）
    res = _build_effective_stats(target, state).EFFECT_RES
    chance = calc_effect_probability(status.base_chance, 0.0, res)
    if chance < 1.0 and random.random() >= chance:
        state.log.append(f'  抵抗: {target.char.name} 抵抗{status.name} (命中{chance:.0%})')
        return False
    # ③ 施加（同 id 刷新）
    existing = next((s for s in target.statuses if s.id == status.id), None)
    if existing:
        existing.remaining_turns = max(existing.remaining_turns, status.remaining_turns)
        state.log.append(f'  {status.name}刷新: {target.char.name}')
    else:
        target.statuses.append(status)
        state.log.append(f'  {status.name} → {target.char.name} ({status.remaining_turns}回合)')
    state.hooks.trigger_all("on_enter_state", u=target, state=state,
                            status=status)  # v5.0 P8 事件
    return True


def _check_control_status(state, u):
    """Y 轴常规回合开始的状态判定（v5.0 P4）:
    - 冻结: 跳过本回合 + 推条5000 + 倒计时; 期间 X 轴禁止（_enqueue_ult/_exec_extra_turn 拦截）
    - 眩晕: 跳过本回合 + 倒计时
    - 减速型（纠缠/禁锢）: 由 statuses 挂的负 SPD 经 _effective_spd 生效, 不拦截
    返回 True 表示本回合被跳过。
    """
    if not u.statuses:
        return False
    for st in list(u.statuses):
        st.remaining_turns -= 1
        if st.category == 'control' and st.name in ('冻结', '眩晕'):
            if st.name == '冻结':
                navs = state.extra.get('navs', {})
                i = state.units.index(u)
                if i in navs:
                    navs[i] += 5000.0  # 冻结: 跳过回合 + 推条5000（用户实机语义）
            state.log.append(f'  {st.name}: {u.char.name}跳过本回合')
            # v6.2.1 注: turn_count 在跳过路径与 hold/resume 存在双计（Harness P3-6）,
            # 当前仅展示用; 实现"每回合"类机制前必须修正
            state.turn_count += 1
            state.extra['killed_this_action'] = 0
            state.extra['ult_chain_guard'] = 0
            _tick_buffs(u)
            state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
            if st.remaining_turns <= 0:  # 最后一次跳过后移除
                u.statuses.remove(st)
                state.hooks.trigger_all("on_exit_state", u=u, state=state, status=st)
            return True
        # 非跳过型状态在本回合的行动值重算后到期。否则 duration=1 的
        # 减速会保留到下一次行动值重算，额外影响一个完整回合。
        if st.remaining_turns <= 0:
            u.statuses.remove(st)
            state.hooks.trigger_all("on_exit_state", u=u, state=state, status=st)
    return False


def _enemy_attack(state, enemy):
    """敌方攻击: 选技能(priority最高) → 选目标 → 反向伤害结算 → 受击钩子"""
    if not enemy.attacks:
        state.log.append(f'  {enemy.name or enemy.id}行动(无攻击技能)')
        return None
    # v6.4: 蓄力条件选择——requires_buff 未满足的技能跳过
    # v6.4b P1-2: 全部受限且条件未满足 → no-op 回合（此前 or enemy.attacks 回退会释放受限技能,
    # 违反字段语义; AV 推进在 _begin_enemy_turn ⑤ 无条件执行, 无行动条死循环风险）
    eligible = [a for a in enemy.attacks
                if not a.get('requires_buff')
                # v6.4b: 只认 category=='buff' 的自增益（同名玩家 debuff 不得误判满足）
                or any(s.id == a['requires_buff']
                       and getattr(s, 'category', '') == 'buff'
                       for s in enemy.statuses)]
    if not eligible:
        state.log.append(f'  {enemy.name or enemy.id}无可用技能(全部受限且条件未满足): 跳过本回合')
        return None
    atk = max(eligible, key=lambda a: a.get('priority', 0))
    target = _select_enemy_target(state, attacker=enemy)
    if target is None:
        return None
    stats = _enemy_attack_stats(enemy)
    # v6.4: 敌方自增益（attacks[].self_buffs: 蓄力/狂暴等; 施加于自身, 复用 statuses 容器与倒计时）
    for sb in atk.get('self_buffs', []) or []:
        _enemy_apply_self_buff(state, enemy, sb)
    # v5.0 P6: 目标类型扩展（single/all_enemies/blast/bounce）
    t_type = atk.get('target_type', 'single_enemy')
    if t_type == 'all_enemies':
        targets = [u for u in state.units if u.is_alive] + \
                  [ms for ms in state.memsprites if ms.is_alive and not ms.is_backup]
    elif t_type == 'blast':
        others = [x for x in state.units if x.is_alive and x is not target] + \
                 [ms for ms in state.memsprites
                  if ms.is_alive and ms is not target and not ms.is_backup]
        targets = [target] + others[:1]
    elif t_type == 'bounce':
        hits = max(int(atk.get('hits', 3)), 1)
        targets = []
        for _ in range(hits):
            t2 = _select_enemy_target(state, attacker=enemy)
            if t2 is not None:
                targets.append(t2)
    else:
        targets = [target]

    # Each target owns a distinct defensive panel. Reusing the selected target's
    # result would make an area attack deal the same pre-mitigation damage to all.
    damages = []
    for hit_target in targets:
        t_stats = (_build_effective_stats(hit_target, state)
                   if isinstance(hit_target, SimUnit) else hit_target.base_stats)
        view = CharacterAsTarget(hit_target, t_stats)
        d = calculate_damage(stats, view, stats.ATK, atk.get('multiplier', 100.0),
                             atk.get('damage_type', 'direct'), atk.get('element', '物理'),
                             enemy.level, False)
        damages.append((hit_target, d.final_damage))

    # v6.2.1: 带默认值防 StopIteration（Harness P2-3: 弹射未命中初选目标时 damages 无该目标）
    primary_damage = next((dmg for hit_target, dmg in damages if hit_target is target), 0.0)
    target_name = '全体' if len(targets) > 1 else (
        getattr(target, 'name', None) or getattr(getattr(target, 'char', None), 'name', str(target))
    )
    state.log.append(f'[{state.current_av:6.0f}AV] {enemy.name or enemy.id} {atk.get("name", "攻击")}: {primary_damage:.0f} → {target_name}')
    state.hooks.trigger_all("on_enemy_attack", u=target, state=state,
                            attacker=enemy, target=target, damage=primary_damage)
    for hit_target, damage in damages:
        _distribute_damage(state, hit_target, damage, enemy)
    # 受击回能（v5.0 P2, 数据驱动: attacks[].energy_gain, 默认0）
    eg = atk.get('energy_gain', 0) or 0
    if eg > 0:
        for hit_target, _d in damages:
            if isinstance(hit_target, SimUnit):
                gained = _gain_energy(hit_target, eg, state=state)
                if gained > 0:
                    state.log.append(f'  受击回能: {hit_target.char.name} +{gained:.0f}')
    # v5.0 P4: 敌方攻击施加 debuff（数据驱动: attacks[].debuffs, 默认无）
    # v6.4: debuffs[].target 支持 "main"（仅初选目标）/ "all"（全部命中, 默认）
    # v6.4b P2: main 契约=初选目标本身, 不以"实际命中"为前提（弹射可能全程未命中初选目标）
    for deb in atk.get('debuffs', []) or []:
        if deb.get('target', 'all') == 'main':
            deb_targets = [(target, 0.0)] if target is not None and target.is_alive else []
        else:
            deb_targets = damages
        for hit_target, _d in deb_targets:
            _apply_player_status(state, hit_target, PlayerStatus(
                id=deb.get('id', f'enemy:{enemy.id}:{deb.get("name", "debuff")}'),
                name=deb.get('name', '负面状态'),
                category=deb.get('category', 'debuff'),
                source=enemy.id,
                remaining_turns=deb.get('duration', 2),
                attributes=dict(deb.get('attributes', {}) or {}),
                base_chance=deb.get('base_chance', 1.0),
            ))
    return primary_damage


def _apply_hit(state, target, amount, enemy):
    """受击结算: 盾吸收 → 扣血 → 万敌充能/E4 → 符玄自回血 → 钩子 → 死亡检查"""

    from engine.characters.qianye import _qianye_apply_shaqizhaoshen, _qianye_e6_gain_charge, _qianye_gain_charge, _qianye_wrath_active
    if amount <= 0 or not getattr(target, 'is_alive', True):
        return
    # v5.0 P5: 护盾优先吸收（盾吸收部分不算 HP 损失, 不触发 on_hp_loss/万敌充能）
    if getattr(target, 'shield', 0.0) > 0:
        absorbed = min(target.shield, amount)
        target.shield -= absorbed
        amount -= absorbed
        if absorbed > 0:
            t_name = getattr(getattr(target, 'char', None), 'name', '忆灵')
            state.log.append(f'  护盾吸收: {t_name} 盾-{absorbed:.0f}')
    # v6.10.6 A1: 扣血钳制——任何单位 HP 不得为负（不变量）; 实际损失用于后续管线
    before_hp = target.current_hp
    target.current_hp -= amount
    if target.current_hp < 0.0:
        target.current_hp = 0.0
    actual_lost = before_hp - target.current_hp
    if amount <= 0:
        return  # 盾完全吸收: 无实际 HP 损失, 不触发后续受击管线
    # 万敌受击充能（行迹1: 每损失1%生命=1充能; 生命上限每超4000点100→充能比例+2.5%, 最多计入4000; 弑神登神期间锁充能）
    if isinstance(target, SimUnit) and target.char.id == 'mydei' and not target.extra.get('charge_locked'):
        pct = actual_lost / max(target.max_hp, 1) * 100.0
        excess_hundreds = min(max(0, target.max_hp - 4000), 4000) // 100  # v5.7: 行迹1门槛
        charge_mult = 1.0 + 0.025 * excess_hundreds
        target.extra['mydei_charge'] = min(200, target.extra.get('mydei_charge', 0) + pct * charge_mult)
        state.log.append(f'  受击充能: +{pct * charge_mult:.1f} → {target.extra["mydei_charge"]:.0f}/200')
    # 万敌E4: 受击回10%生命上限
    if isinstance(target, SimUnit) and target.char.id == 'mydei' and target.eidolon_rank >= 4:
        heal = target.max_hp * 0.10
        target.current_hp = min(target.max_hp, target.current_hp + heal)
        state.log.append(f'  万敌E4: 受击回10%生命 +{heal:.0f}')
    # 符玄天赋·太一玄机: HP≤50% → 回已损失90%（E1 +1次）
    if isinstance(target, SimUnit) and target.char.id == 'fu_xuan':
        used = target.extra.get('fuxuan_self_heal_used', 0)
        cap = 1 + (1 if target.eidolon_rank >= 1 else 0)
        if used < cap and target.current_hp <= target.max_hp * 0.5:
            heal = (target.max_hp - target.current_hp) * 0.9
            target.current_hp = min(target.max_hp, target.current_hp + heal)
            target.extra['fuxuan_self_heal_used'] = used + 1
            state.log.append(f'  太一玄机: 回血{heal:.0f} ({used + 1}/{cap}次)')
    # v6.10.6 A2: HP损失管线统一广播实际损失（此前广播请求值, 溢出伤害污染记录/充能）
    from engine.characters.xiadie import _xiadie_absorb_hp_loss
    _xiadie_absorb_hp_loss(state, actual_lost, "受击")
    state.hooks.trigger_all("on_hp_loss", u=target, state=state,
                            total_lost=actual_lost, affected=[(target, actual_lost)])
    from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
    _dispatch_changyeyue_hp_loss(state, [(target, actual_lost)])
    # v5.0.1: 光锥 HP 损失事件（此身为剑·月蚀）
    if isinstance(target, SimUnit):
        state.extra['lc_last_hp_loss'] = actual_lost  # v5.4 在火的远处: 单次受击损失
        _process_lc_effects(target, state, "on_hp_loss")
    # 受击钩子（符玄E4 挂这里）
    state.hooks.trigger_all("on_take_damage", u=target, state=state,
                            attacker=enemy, damage=amount)
    if isinstance(target, SimUnit) and target.char.id == 'qianye' \
            and _qianye_wrath_active(target):
        if any(getattr(t, 'hook_name', '') == 'qianye_trace2'
               for t in (target.char.traces or [])) and enemy is not None:
            _qianye_apply_shaqizhaoshen(state, target, enemy)
            _qianye_gain_charge(state, target, 1)
        _qianye_e6_gain_charge(state, target)
    # v6.10.3 P1-1: 赛飞儿天赋已移至我方攻击命中老主顾后触发（_cipher_attack_aftermath）,
    # 原受击路径方向反了（敌人攻击我方才触发）。
    # v5.0.1: 光锥受击事件（moment_of_victory 防御 buff / 此身为剑 月蚀）
    if isinstance(target, SimUnit):
        _process_lc_effects(target, state, "on_hit_taken")
    # 遗器受击叠层（拳王ATK/莳者CR; 攻击侧由 on_after_skill 注册触发）
    if isinstance(target, SimUnit):
        conds = getattr(target, '_active_relic_conditions', set()) or set()
        if 'stack_atk_on_hit' in conds or 'stack_cr_on_hit' in conds:
            from engine.core.relic_conditions import _stack_atk_on_hit, _stack_cr_on_hit
            if 'stack_atk_on_hit' in conds:
                _stack_atk_on_hit(target, state)
            if 'stack_cr_on_hit' in conds:
                _stack_cr_on_hit(target, state)
    _check_fatal(state, target)


def _distribute_damage(state, target, amount, enemy):
    """符玄天赋: 穷观阵激活时全队减伤18% + 承伤65%分配（一次应用防递归; 两次独立结算天然无递归）"""
    fuxuan = next((u for u in state.units if u.char.id == 'fu_xuan' and u.is_alive), None)
    field = fuxuan is not None and state.extra.get('fuxuan_field_turns', 0) > 0
    if field:
        amount *= 0.82  # 全队减伤18%
        state.log.append(f'  穷观阵减伤18%: {amount:.0f}')
    if field and isinstance(target, SimUnit) and target is not fuxuan:
        _apply_hit(state, target, amount * 0.35, enemy)
        _apply_hit(state, fuxuan, amount * 0.65, enemy)
        t_name = getattr(target, 'name', None) or target.char.name
        state.log.append(f'  符玄承伤65%: 符玄-{amount * 0.65:.0f} / {t_name}-{amount * 0.35:.0f}')
    else:
        _apply_hit(state, target, amount, enemy)


def _check_fatal(state, target):
    """死亡管线: 致命保护链（万敌血仇→符玄E2→藿藿E2）→ 真正死亡"""

    from engine.characters.cipher import _cipher_trace3_remove_vuln
    from engine.characters.hysilens import _hysilens_remove_field
    from engine.characters.mydei import _mydei_fatal_recovery
    from engine.characters.qianye import _qianye_exit_wrath
    # v6.10.6 A1: 双保险——任何路径进入死亡检查时 HP 不得为负
    if target.current_hp < 0:
        target.current_hp = 0.0
    if target.current_hp > 0:
        return
    # v6.9.1: 千冶·刃无量忿怒致命保护——不死+退出结界+回50%生命上限
    if isinstance(target, SimUnit) and target.char.id == 'qianye' \
            and target.extra.get('qianye_wrath'):
        target.current_hp = 0.0
        _qianye_exit_wrath(state, target, fatal=True)
        return

    if not isinstance(target, SimUnit):
        # 忆灵也可被敌方选中；其死亡必须从行动列表和召唤者引用中清理。
        target.current_hp = 0.0
        target.is_alive = False
        summoner = next((u for u in state.units
                         if u.char.id == getattr(target, 'summoner_id', None)), None)
        rem = state.extra.get('_rem_sys')
        if rem and summoner:
            rem.despawn_memsprite(state, summoner, target, reason="enemy_attack")
        elif target in state.memsprites:
            state.memsprites.remove(target)
            if summoner and summoner.memsprite_unit is target:
                summoner.memsprite_unit = None
        state.log.append(f'  {getattr(getattr(target, "data", None), "name", "忆灵")}被消灭')
        return
    # ① 万敌血仇致命保护
    if target.char.id == 'mydei' and target.extra.get('is_blood_debt'):
        _mydei_fatal_recovery(target, state)
        return
    # ② 符玄E2·柔兆（单场1次+全队回70%）
    from engine.characters.fu_xuan import _fuxuan_e2_fatal_check
    if _fuxuan_e2_fatal_check(state):
        return
    # ③ 藿藿E2·镇尾锁灵（2次+全队回50%）
    from engine.characters.huohuo import _huohuo_e2_fatal_check
    if _huohuo_e2_fatal_check(state):
        return
    # 真正死亡
    target.is_alive = False
    state.extra.get('navs', {}).pop(state.units.index(target), None)
    if target.char.id == 'cipher':
        _cipher_trace3_remove_vuln(state)
        state.log.append('  赛飞儿阵亡→行迹3敌方易伤解除')
    if target.char.id == 'sparkle':
        bonus = target.extra.pop('sparkle_max_sp_bonus', 0)
        if bonus:
            state.max_sp = max(5, state.max_sp - bonus)
            state.log.append(f'  花火阵亡→战技点上限-{bonus}')
    if target.char.id == 'yinlang':
        target.invincible_active = False
        target.invincible_basics_done = 0
        target.extra.pop('yinlang_e2_next_threshold', None)
        target.extra['yinlang_blindbox_prob'] = 1.0
        for enemy in state.enemies:
            enemy.extra.pop('yinlang_e1_vuln', None)
        state.log.append('  银狼阵亡→无敌玩家结界解除')
    if target.char.id == 'hysilens':
        _hysilens_remove_field(state, target)
        state.log.append('  海瑟音阵亡→结界解除')
    if target.char.id == 'sunday':
        for ally in state.units:
            ally.extra.pop('sunday_mentor', None)
            ally.extra.pop('sunday_cr_stacks', None)
            ally.buffs = [b for b in ally.buffs
                          if getattr(b, 'source_id', '') != 'sunday']
            ms = getattr(ally, 'memsprite_unit', None)
            if ms is not None:
                ms.buffs = [b for b in ms.buffs
                            if getattr(b, 'source_id', '') != 'sunday']
        state.log.append('  星期日阵亡→蒙福者与星期日增益解除')
    dht = next((x for x in state.units
                if x.char.id == 'dan_heng_permansor_terrae'), None)
    tongpao_died = bool(dht and target.char.id == dht.extra.get('dht_tongpao_id'))
    if target.char.id == 'dan_heng_permansor_terrae' or tongpao_died:
        owner = target if target.char.id == 'dan_heng_permansor_terrae' else dht
        if owner:
            owner.extra.pop('dht_tongpao_id', None)
            owner.extra.pop('dht_longling_enhanced', None)
            if owner.marker:
                marker_system = state.extra.get('_marker_sys')
                if marker_system:
                    marker_system.despawn(state, owner.marker)
        for ally in state.units:
            ally.extra.pop('dht_tongpao', None)
        for enemy in state.enemies:
            enemy.extra.pop('dht_tongpao_vuln', None)
        state.log.append('  丹恒·腾荒/同袍阵亡→龙灵与同袍效果解除')
    if target.memsprite_unit and target.memsprite_unit.is_alive:
        rem = state.extra.get('_rem_sys')
        if rem:
            rem.despawn_memsprite(state, target, target.memsprite_unit, reason="summoner_death")
    # v5.3: 行动条标记随召唤者阵亡移除（浮元/完全燃烧倒计时）
    if target.marker:
        sys = state.extra.get('_marker_sys')
        if sys:
            sys.despawn(state, target.marker)
    # v5.3: 阵亡事件（光环失效: 忘归人天赋/同谐E4等）
    state.hooks.trigger_all("on_ally_death", u=target, state=state)
    # v5.7: 昔涟阵亡→结界解除（实机: 当昔涟陷入无法战斗状态时, 结界也会被解除）
    if target.char.id == 'xilian' and state.extra.get('xilian_field_turns'):
        state.extra['xilian_field_turns'] = 0
        state.realm_true_dmg = 0
        state.log.append('  昔涟阵亡→结界解除')
    state.log.append(f'  {target.char.name} 阵亡')


def _tick_break_dot(state, enemy, status):
    """击破 DOT 跳伤（敌方回合结算, 快照面板）: 不吃双暴/元素增伤, 吃DOT增伤+全增伤"""
    snap = status.attributes.get('dot_snapshot')
    if not snap:
        return 0.0
    d = calculate_damage(snap, enemy, snap.ATK, status.attributes.get('dot_multiplier', 100.0),
                         "dot", status.attributes.get('dot_element', '物理'), 80)
    source = next((u for u in state.units
                   if u.char.id == getattr(status, 'source', '')), None)
    _commit_enemy_damage(state, source, enemy, d.final_damage)
    state.log.append(f'  {status.name}: {d.final_damage:.0f} → {enemy.name or enemy.id}')
    return d.final_damage


def _enemy_eff_spd(enemy) -> float:
    """敌方有效速度（v5.4 光锥敌方速降消费: 梦应归于何处【溃败】速度-20%等）
    v6.4: 敌方自增益 SPD_PERCENT 消费（与速降并存）
    v6.4b P1-3: SPD_PERCENT/SPD_percent 别名兼容（AGENTS.md 不变量; 每条状态只取其一, 防双键重复叠加）"""
    spd_pct = _enemy_status_sum(enemy, 'SPD_PERCENT', 'SPD_percent')
    return enemy.SPD * (1.0 - enemy.status_attribute('spd_down')) * (1.0 + spd_pct)


def _enemy_status_sum(enemy, *keys):
    """敌方状态属性别名族求和（v6.4b P1-3）: 同一状态只认第一个命中的别名键。"""
    total = 0.0
    for s in enemy.statuses:
        for k in keys:
            v = s.attributes.get(k)
            if v is not None:
                total += float(v)
                break
    return total


def _enemy_apply_self_buff(state, enemy, sb):
    """敌方自增益施加（v6.4b 子代理 P2/P3）: id 与玩家负面状态撞名时不覆盖;
    同 id 刷新取 max 剩余回合（与 _apply_player_status 的 max 语义一致）;
    id 缺失兜底不崩溃。requires_buff 只认 category=='buff' 状态（见 _enemy_attack）。"""
    sid = sb.get('id') or f'self_buff:{sb.get("name", "unknown")}'
    existing = next((s for s in enemy.statuses if s.id == sid), None)
    if existing is not None and existing.category != 'buff':
        state.log.append(f'  [WARN] 自增益[{sid}]与敌方负面状态同名, 跳过施加')
        return
    duration = sb.get('duration', 1)
    attrs = dict(sb.get('attributes', {}) or {})
    if existing is not None:
        existing.remaining_turns = max(existing.remaining_turns, duration)
        existing.attributes.update(attrs)
    else:
        enemy.add_status(EnemyStatus(id=sid, name=sb.get('name', '强化'),
                                     category='buff', source=enemy.id,
                                     remaining_turns=duration, attributes=attrs))
    state.log.append(f'  {enemy.name or enemy.id} {sb.get("name", "强化")}: '
                     f'自身增益 {duration}回合')


def _tick_enemy_statuses(state, enemy):
    """③ 敌方状态倒计时 + 到期恢复（v6.4c 抽出: 冻结跳过回合共用, 项目主确认冻结回合照算倒计时）"""
    expired_statuses = enemy.tick_statuses()
    # v5.3 弱点植入到期恢复（流萤火弱点: status 带 weakness_element/weakness_old_res）
    # v6.3.0b P1-11: 银狼弱点快照=纯抗性; 与全抗降低同批到期时先还原弱点元素,
    # 全抗恢复跳过这些元素防双重回加。
    expired_weak_elems = {st.attributes.get('weakness_element')
                          for st in expired_statuses
                          if st.id == 'silver_wolf_weakness'}
    for st in expired_statuses:
        if 'weakness_element' in st.attributes:
            elem = st.attributes['weakness_element']
            if st.id == 'silver_wolf_weakness':
                pure = st.attributes.get('weakness_old_res', 0.0)
                still_all_res = enemy.has_status(status_id='silver_wolf_all_res_down')
                enemy.element_res[elem] = pure - (0.13 if still_all_res else 0.0)
            else:
                e6_res_down = enemy.extra.get('lingsha_e6_res_down', 0) * 0.20
                enemy.element_res[elem] = st.attributes.get('weakness_old_res', 0.0) - e6_res_down
            state.log.append(f'  {elem}弱点植入结束, 抗性恢复')
        # v6.3.0 银狼战技全抗降低到期恢复（silver_wolf_all_res_down）
        if st.id == 'silver_wolf_all_res_down':
            for elem in list(enemy.element_res):
                if elem in expired_weak_elems:
                    continue  # 同批到期的弱点已按纯抗性恢复, 防双重回加
                enemy.element_res[elem] = enemy.element_res.get(elem, 0) + 0.13
            state.log.append(f'  银狼全抗-13%结束, 抗性恢复')
    break_status = next((s for s in enemy.statuses if s.id.startswith('break:')), None)
    enemy.break_debuff_turns = break_status.remaining_turns if break_status else 0
    for expired in expired_statuses:
        if not expired.id.startswith('break:'):
            state.log.append(f'  {expired.name}解除 {enemy.name or enemy.id}')
            continue
        name = expired.name
        if name == "禁锢":
            enemy.SPD += enemy.extra.pop('imprison_speed_reduced', 0.0)
            state.log.append(f'  禁锢解除: 速度恢复 {enemy.name or enemy.id}')
        else:
            state.log.append(f'  {name}解除 {enemy.name or enemy.id}')
        enemy.break_debuff_name = ""
    return expired_statuses


def _enemy_exec_action(state, enemy):
    """④ 敌方单次行动（v6.4c 抽出: 精英双动每动复用; 行动后 sweep 让满能玩家大招可插入两动之间）"""
    _enemy_attack(state, enemy)
    # v6.6 白厄: 敌方攻击后叠加弑魂之炽（v6.6b P1-7: 仅存活且在变身中的白厄）
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('shihun_stacks', 0) > 0
               and x.is_alive and x.extra.get('kasier')), None)
    if ph:
        ph.extra['shihun_stacks'] = ph.extra.get('shihun_stacks', 0) + 1
        state.log.append(f'  弑魂之炽叠层: {ph.extra["shihun_stacks"]}层')
    _sweep_ults(state)


def _enemy_turn_end(state, enemy, enemy_attacked: bool = True):
    """④b 纠缠推条 + ⑤ AV 更新（v6.4c 抽出: 一回合多动只在最后一动后执行一次）

    v6.6b P1-7: enemy_attacked=False（冻结跳过）时只推条, 不叠层不反击;
    反击仅限存活且在变身中的白厄。"""

    from engine.characters.phainon import _phainon_shihun_counter
    from engine.characters.qianye import _reset_qianye_e6_charge_gate
    # v6.6 白厄: 敌方行动完毕后立即发动弑魂反击（每层倍率+20%）并解除状态
    ph = next((x for x in state.units if x.char.id == 'phainon'
               and x.extra.get('shihun_stacks', 0) > 0
               and x.is_alive and x.extra.get('kasier')), None)
    if ph and enemy.HP > 0 and enemy_attacked:
        stacks = ph.extra.pop('shihun_stacks', 0)
        _phainon_shihun_counter(state, ph, stacks)
        # v6.6b P1-1: 反击后解除减伤
        ph.extra.pop('shihun_dr', None)
        ph.buffs = [b for b in ph.buffs
                    if getattr(b, 'source_name', '') != '弑魂之炽减伤']
    # ④b 纠缠（v5.0 P7）: 量子击破异常 — 敌方行动时推条20%回合值
    if any(s.name == '纠缠' for s in enemy.statuses):
        push = AV_PER_TURN / max(_enemy_eff_spd(enemy), 1.0) * 0.20
        enemy.extra['av_delayed'] = enemy.extra.get('av_delayed', 0) + push
        state.log.append(f'  纠缠: 推条{push:.0f}')
    # ⑤ AV 更新（av_delayed 三写零读就此闭环: 击破2500/冻结5000）
    delay = enemy.extra.pop('av_delayed', 0.0)
    i = state.enemies.index(enemy)
    _set_av(state, state.extra.get('navs', {}), ('e', i),
            state.current_av + AV_PER_TURN / max(_enemy_eff_spd(enemy), 1.0) + delay)
    _reset_qianye_e6_charge_gate(state)


def _enemy_pending_step(state):
    """②b 精英双动第二行动（v6.4c）: 主循环在 X 轴清空后调用; 返回 True=已处理需 continue。

    破韧打断（口径A, 项目主确认）: 同回合内被击破 → 第二行动取消,
    击破的 2500 推条已在破韧时写入 av_delayed, 回合结束时作用于下一回合。
    """
    epa = state.extra.get('enemy_pending_action')
    if epa is None:
        return False
    enemy = epa
    if enemy.HP > 0 and enemy.extra.get('_actions_left', 0) > 0:
        if enemy.is_broken:
            state.log.append(f'  {enemy.name or enemy.id}破韧打断: 第二行动取消(推条作用于下回合)')
        else:
            _enemy_exec_action(state, enemy)
        enemy.extra['_actions_left'] -= 1
    if enemy.HP <= 0 or enemy.extra['_actions_left'] <= 0:
        if enemy.HP > 0:
            _enemy_turn_end(state, enemy)
        state.extra['enemy_pending_action'] = None
    return True


def _begin_enemy_turn(state, enemy):
    """敌方常规回合（Y轴）: 冻结检查 → 韧性恢复 → DOT结算 → 状态倒计时 → 行动（精英双动）→ av_delayed 消费

    v6.4c 精英双动（项目主确认实机语义）: actions_per_turn>1 时同一回合连续行动多次,
    行动之间主循环①清空 X 轴（玩家终结技/破韧打断窗口）; 第一行动上的 self_buff 第二行动立即吃到。
    """

    from engine.characters.hysilens import _hysilens_dot_trigger_v3, _tick_hysilens_dot
    from engine.characters.ruan_mei import _ruanmei_canmei_trigger_v3
    state.turn_count += 1
    for qianye in state.units:
        if qianye.char.id == 'qianye':
            qianye.extra.pop('qianye_e6_charge_used', None)
    # v6.9.1: 失重受击延后次数每回合重置（txt:48 每目标每回合最多8次）
    enemy.extra['welt_shizhong_count'] = 0
    state.extra['action_ctx'] = 'enemy'  # 敌方回合不 tick 我方 buff（AGENTS.md:17）
    # ⓪ 控制（v5.0 P7 用户实机语义; v6.6c 扩展禁锢）: 即将开始常规回合时跳过该回合 + 推条 + 解除
    # v6.4c 点1: 冻结跳过回合 buff/debuff 同样计算倒计时（DOT 跳伤仍不结算）
    cc = next((s for s in enemy.statuses
               if s.category == 'control' and s.name == '冻结'
               or (s.category == 'control' and s.name == '禁锢'
                   and not s.id.startswith('break:'))), None)  # 击破异常禁锢走③恢复, 不走跳过
    if cc:
        enemy.statuses.remove(cc)
        push = 5000.0 if cc.name == '冻结' else cc.attributes.get('delay_amount', 2500.0)
        enemy.extra['av_delayed'] = enemy.extra.get('av_delayed', 0) + push
        enemy.break_debuff_name = ""
        enemy.break_debuff_turns = 0
        _tick_enemy_statuses(state, enemy)
        state.log.append(f'  {enemy.name or enemy.id} {cc.name}: 跳过行动+推条{push:.0f}, {cc.name}解除')
        _enemy_turn_end(state, enemy, enemy_attacked=False)  # v6.6b P1-7: 未行动不反击
        return
    # ① 韧性恢复（击破后敌人行动时复原）
    if enemy.is_broken:
        # v6.9 阮·梅【残梅绽】: 尝试从破韧恢复时触发——延长破韧(跳过恢复)+推条+冰击破伤害
        ruan = next((x for x in state.units
                     if x.char.id == 'ruan_mei' and x.is_alive), None)
        if ruan is not None and _ruanmei_canmei_trigger_v3(state, ruan, enemy):
            state.log.append(f'  {enemy.name or enemy.id} 残梅绽延长破韧')
            return  # 跳过后续恢复/DOT/行动（破韧保持, 本回合仍不行动）
        enemy.is_broken = False
        enemy.toughness = enemy.max_toughness
        enemy.break_debuff_name = ""
        # v5.3 灵砂E1: 击破期间 DEF-20% 随韧性恢复结束
        enemy.remove_status('lingsha_e1_def_down')
        state.log.append(f'  {enemy.name or enemy.id} 韧性恢复')
    # ② DOT 结算（先跳伤再递减 → 持续2回合恰好2跳）
    # v6.6c P1: 海瑟音DOT（hysilens_*）同样结算; 结界引爆窗口挂在此处（敌受DOT跳→反打）
    # v6.8.1: 海瑟音E1「我方持续伤害×116%」全队 DOT 统一结算乘（含击破DOT）
    hs_e1 = next((x for x in state.units
                  if x.char.id == 'hysilens' and x.is_alive and x.eidolon_rank >= 1), None)
    # v6.8.3: 反打次数上限绑定“敌方回合开始时”边界（此前只在终结技重置, 跨回合累计）
    state.extra['hysilens_trigger_count'] = 0

    for s in list(enemy.statuses):
        if s.category != 'dot':
            continue
        if s.id.startswith('break:'):
            d = _tick_break_dot(state, enemy, s)
            if hs_e1 and d > 0 and enemy.HP > 0:
                _commit_enemy_damage(state, hs_e1, enemy, d * 0.16)
        elif s.id.startswith('hysilens_'):
            d = _tick_hysilens_dot(state, enemy, s)
            if hs_e1 and d > 0 and enemy.HP > 0:
                _commit_enemy_damage(state, hs_e1, enemy, d * 0.16)
            hs = next((x for x in state.units
                       if x.char.id == 'hysilens' and x.is_alive), None)
            if hs and enemy.HP > 0:
                _hysilens_dot_trigger_v3(state, hs, enemy)
    if enemy.HP <= 0:
        enemy.HP = 0.0
        if not enemy.extra.get('_kill_recorded'):
            _record_enemy_kill(state)
        state.log.append(f'  {enemy.name or enemy.id} 被持续伤害击败')
        return
    # ③ 状态倒计时 + 到期恢复
    _tick_enemy_statuses(state, enemy)
    # ④ 第一行动
    _enemy_exec_action(state, enemy)
    # ⑤ 精英双动（v6.4c）: 剩余行动挂 pending, 由主循环 ②b 在 X 轴清空后执行
    acts = max(int(getattr(enemy, 'actions_per_turn', 1) or 1), 1)
    if acts > 1 and enemy.HP > 0:
        enemy.extra['_actions_left'] = acts - 1
        state.extra['enemy_pending_action'] = enemy
    else:
        _enemy_turn_end(state, enemy)


def _exec_extra_turn(state, unit, kind):
    """X 轴额外回合执行（从左往右）"""

    from engine.characters.qianye import _reset_qianye_e6_charge_gate
    state.extra['action_ctx'] = 'extra'
    # v7.2.0 #7: 姬子行迹2额外回合开始→解除防循环标记(此后再使用助战技可再触发)
    if isinstance(unit, SimUnit):
        unit.extra.pop('hn_trace2_pending', None)
    # v5.0 P4: 冻结期间禁止额外回合（含已入队行动, 用户实机语义）
    if isinstance(unit, SimUnit) and any(getattr(st, 'name', '') == '冻结'
                                         for st in unit.statuses):
        state.log.append(f'  {unit.char.name}冻结中: 额外回合被跳过')
        state.turn_count += 1
        # v6.2.1b P3-9（评审确认设计意图, 勿补 _sweep_ults）: 跳过期间无能量事件,
        # 对他人 sweep 是无操作; 对自己 sweep 会把被跳过的冻结终结技立刻重入队→
        # 主循环弹出自转直到 ult_chain_guard 顶满。解冻后常规回合 phase-1 会重新入队, 大招不丢只顺延。
        # v6.2.1: 冻结跳过同样执行再现收尾（Codex P1-4: 残留 seele_in_extra 会永久封死再现）
        if unit.char.id == 'seele':
            unit.extra['seele_in_extra'] = False
            unit.buffs = [b for b in unit.buffs
                          if getattr(b, 'source_name', '') != '再现增幅']
            state.log.append('  再现结束: 增幅解除')
        _reset_qianye_e6_charge_gate(state)
        return
    # 希儿增幅: 击杀瞬间获得的 pending 状态, 在 X 轴首个希儿行动(终结技或再现)时激活
    # (增幅覆盖 X 轴上到增幅回合结束的一切行动; 回到 Y 轴常规回合时已撤销)
    if (isinstance(unit, SimUnit) and unit.char.id == 'seele'
            and unit.extra.get('seele_amplify_pending')):
        if not any(getattr(b, 'source_name', '') == '再现增幅' for b in unit.buffs):
            unit.buffs.append(TimedBuff(source_id='seele', attributes={"DMG_BONUS_ALL": 80.0},
                                        remaining_turns=1, source_name='再现增幅'))
            state.log.append('  再现: 增幅生效(80%增伤)')
        unit.extra['seele_amplify_pending'] = False
    from engine.systems.remembrance import RemembranceSystem
    if kind == 'ult':
        state.log.append(f'  === 额外回合[终结技]: {getattr(unit, "char", unit).name if hasattr(unit, "char") else unit.data.name} ===')
        if hasattr(unit, 'char'):
            if unit.char.id == 'xilian' and unit.is_ripple and unit.zhuiyi >= 12:
                _use_skill(unit, state, "ultimate_ripple")
                # 忆灵技释放（追忆-12 + 德谬歌行动: 花与箭/此诗献予, 用户确认实机）
                if unit.memsprite_unit and unit.memsprite_unit.is_alive:
                    from engine.characters.xilian import _xilian_memsprite_action
                    _xilian_memsprite_action(state, unit, unit.memsprite_unit)
            else:
                _use_skill(unit, state, "ultimate")
            _ult_post(state, unit)
        _sweep_ults(state)
    else:
        _dispatch_extra_action(state, unit)
        # 规则4: 额外回合内满能量→追加队尾（排队）
        if isinstance(unit, SimUnit) and _should_ult_now(unit, state):
            _enqueue_ult(state, unit)
        _sweep_ults(state)
        # 希儿增幅: 额外回合结束解除
        if isinstance(unit, SimUnit) and unit.char.id == 'seele':
            unit.extra['seele_in_extra'] = False
            unit.buffs = [b for b in unit.buffs
                          if getattr(b, 'source_name', '') != '再现增幅']
            state.log.append('  再现结束: 增幅解除')
    _reset_qianye_e6_charge_gate(state)


def _dispatch_extra_action(state, unit):
    """额外回合动作分发（按身份）"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    state.extra['_rem_sys'] = rem
    if isinstance(unit, SimUnit):
        cid = unit.char.id
        if cid == 'seele':
            # 希儿再现: 再行动一次（普攻/战技）; 增幅已在 _exec_extra_turn 开头激活
            state.log.append(f'  === 额外回合[再现]: {unit.char.name} ===')
            if state.skill_points > 0:
                _use_skill(unit, state, "skill")
            else:
                _use_skill(unit, state, "basic_attack")
        elif cid == 'mydei':
            # 万敌弑神登神
            state.log.append(f'  === 额外回合[弑神登神]: {unit.char.name} ===')
            _use_skill(unit, state, "skill_shenshen")
        else:
            state.log.append(f'  === 额外回合: {unit.char.name} ===')
            # v6.7: 统一走 _ai_regular_action（直接 ai_fn(unit, state) 缺 elation/navs 等 kwargs）
            _ai_regular_action(state, unit)
    else:
        # 忆灵额外回合（小伊卡/德谬歌/界外）
        if unit.data.name == '小伊卡':
            # 小伊卡额外回合: 乌云乌云+天赋追加治疗
            summoner = next((x for x in state.units if x.char.id == 'fengjin'), None)
            if summoner and summoner.extra.get('clear_sky_turns', 0) > 0:
                ms = unit
                if ms.cumulative_healing > 0:
                    # 风堇E6: 乌云乌云→全队回复12%累计治疗并清空
                    if summoner.eidolon_rank >= 6:
                        heal_amt = ms.cumulative_healing * 0.12
                        # v6.2.1: 全队含忆灵（Codex P2-6）
                        for eu in [x for x in state.units if x.is_alive] \
                                + [x for x in state.memsprites if x.is_alive]:
                            eu.current_hp = min(eu.max_hp, eu.current_hp + heal_amt)
                        ms.cumulative_healing = 0
                        state.log.append(f'  乌云乌云: 全队回复12%并清空({heal_amt:.0f}HP)')
                    else:
                        # v5.6.1: 乌云乌云是直伤, 与其他直伤一样走伤害管线吃乘区
                        # （防御/抗性/增伤/双暴/易伤）; 倍率=累计治疗×20%（用户确认）
                        base = ms.cumulative_healing * 0.20
                        alive = state.alive_enemies()
                        from engine.systems.remembrance import _ms_effective_stats
                        ms_stats = _ms_effective_stats(ms, state)
                        total = 0.0
                        for t in alive:
                            d = calculate_damage(
                                ms_stats, _enemy_for_damage(t), base, 100.0,
                                "direct", "风", 80, ms_stats.CRIT_RATE >= 0.5,
                                crit_mode="expected")
                            _commit_enemy_damage(state, summoner, t, d.final_damage)
                            total += d.final_damage
                        ms.total_damage_dealt += total
                        summoner.total_damage_dealt += total
                        state.log.append(f'  乌云乌云: {total:.0f}伤害(累计治疗={ms.cumulative_healing:.0f})')
                        state.hooks.trigger_all("on_attack_action", u=summoner, state=state, dealt=total > 0)  # v7.1.0 P1: X轴忆灵直伤分支补气氛
                        ms.cumulative_healing *= 0.50
                # 天赋追加治疗: 2%风堇HP+20 × 全队
                bonus_heal = summoner.base_stats.HP * 0.02 + 20
                # 风堇行迹1·暴风停歇: 每超1点SPD→治疗量+1%(上限200点)
                if summoner.base_stats.SPD > 200:
                    bonus_heal *= 1.0 + min(summoner.base_stats.SPD - 200, 200) / 100.0
                # v6.2.1: 全队含忆灵（Codex P2-6: 天赋治疗/on_heal/累计治疗统一包含忆灵）
                alive_allies = [eu for eu in state.units if eu.is_alive] \
                    + [x for x in state.memsprites if x.is_alive]
                total_healed = 0.0
                for eu in alive_allies:
                    amt = bonus_heal
                    # 风堇行迹2·阴云莞尔: 对HP≤50%目标治疗量+25%
                    if eu.current_hp <= eu.max_hp * 0.50:
                        amt = bonus_heal * 1.25
                    eu.current_hp = min(eu.max_hp, eu.current_hp + amt)
                    total_healed += amt
                # 献予「天空」之诗: 风堇持层时治疗计入小伊卡×1.72
                if summoner.extra.get('poem_tiankong', 0) > 0:
                    total_healed *= 1.72
                ms.cumulative_healing += total_healed
                state.log.append(f'  小伊卡天赋: +{bonus_heal:.0f}HP×{len(alive_allies)}人')
                # 追加治疗也计入新蕊(on_heal hook)
                state.hooks.trigger_all("on_heal", u=summoner, state=state,
                                         healer=summoner, targets=alive_allies, heal_amt=bonus_heal)
                from engine.characters.fengjin import _fengjin_talent_heal_buff
                _fengjin_talent_heal_buff(state, summoner)
            return
        rem.handle_memsprite_action(state, unit, regular_turn=False)


def _apply_team_static_relics(state):
    """v5.2: 队伍上下文静态遗器条件（S1③+S2⑤）

    - 仙舟2pc (spd_threshold_120_team_atk): 佩戴者SPD≥120 → 全队ATK+8%（佩戴者自身已由静态层获得）
    - 龙骨2pc (effect_res_30_team_cd): 佩戴者EFFECT_RES≥30% → 全队CD+10%（同上）
    - 坠星2pc (enter_combat_faction_cd): 有同阵营队友(简化:任意队友) → 佩戴者CD+32%
    幂等: state.extra['team_static_applied'] 去重
    """
    if state.extra.get('team_static_applied'):
        return
    state.extra['team_static_applied'] = True
    for u in state.units:
        conds = getattr(u, '_active_relic_conditions', set()) or set()
        if 'spd_threshold_120_team_atk' in conds and u.base_stats.SPD >= 120:
            for eu in state.units:
                if eu is not u:
                    eu.base_stats.ATK += eu.base_stats._base_ATK * 0.08
            state.log.append('  仙舟2pc: 全队ATK+8%')
        if 'effect_res_30_team_cd' in conds and u.base_stats.EFFECT_RES >= 0.30:
            for eu in state.units:
                if eu is not u:
                    eu.base_stats.CRIT_DMG += 0.10
            state.log.append('  龙骨2pc: 全队CD+10%')
        if 'enter_combat_faction_cd' in conds:
            if any(x is not u and x.is_alive for x in state.units):
                u.base_stats.CRIT_DMG += 0.32
                state.log.append('  坠星2pc: 同阵营队友→CD+32%')


def _begin_regular_turn(state, u):
    """Y 轴常规回合开始"""
    _ensure_phase_tables(state)

    from engine.characters.qianye import _reset_qianye_e6_charge_gate
    from engine.characters.seele import _seele_reproduce_check
    navs = state.extra.get('navs', {})
    unit_idx = state.units.index(u)
    # 更新 AV + stamp（v5.0: 有效速度含 SPD_PERCENT buff）
    next_av = state.current_av + AV_PER_TURN / _effective_spd(u, state)
    if getattr(u, '_pending_action_advance', 0) > 0:
        next_av -= u._pending_action_advance
        u._pending_action_advance = 0
    _set_av(state, navs, unit_idx, next_av)
    state.turn_count += 1
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    # M5a: 常规回合角色 tick（pre 区——含 seele 增幅残留清理/缇宝海瑟音结界倒计时/
    # 赛飞儿刻律德菈到期回减/星期日阮·梅知更鸟黄泉飞霄那刻夏 tick; 顺序与原内联
    # 逐位一致, 锁死于 characters.TURN_TICK_ZONE_ORDER['pre']）
    _turn_ticks(state, 'pre', u)

    # 子系统计时（行动前）
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_turn_start(state, u)
    if state.extra.get('_rem_sys'):
        state.extra['_rem_sys'].tick_turn(state, u)

    # v6.10.6 C2: 花火行迹2 单回合SP消耗计数重置（我方常规回合开始）
    state.extra['sparkle_turn_sp_spent'] = 0
    # Hook: 回合开始
    state.hooks.trigger(u.char.id, "on_turn_start", u=u, state=state)
    # v5.0.1: 光锥自身回合开始事件（烈阳移除等）
    _process_lc_effects(u, state, "on_self_turn_start")
    # v5.0 P4: 控制状态判定（冻结/眩晕跳过本回合）
    if _check_control_status(state, u):
        if state.extra.get('_elation'):
            state.extra['_elation'].tick_good_show_turn(state, u)
        return

    # M5a: 藿藿禳命自身 tick（post_control 区, X轴不tick 仅常规回合: 回合开始先递减）
    _turn_ticks(state, 'post_control', u)
    from engine.characters.huohuo import _huohuo_ruming_heal_all
    _huohuo_ruming_heal_all(state, u)

    # 遐蝶行迹3: 任意单位行动后重置治疗转化计数
    state.extra['xiadie_heal_conv'] = 0.0
    # M5a: late 区 tick（昔涟未来消耗→追忆/万敌致命检查与E2重置/阿格莱雅E2无视防御
    # 层清除; 顺序锁死于 characters.TURN_TICK_ZONE_ORDER['late']）
    _turn_ticks(state, 'late', u)

    # phase-1: 终结技优先决策（规则3: 常规回合释放终结技→自己回合挤到X轴）
    if _ai_ult_check(state, u):
        state.extra['pending_turn'] = u
        state.extra['turn_hold_guard'] = state.extra.get('turn_hold_guard', 0) + 1
        return

    # 普攻/战技结束回合
    _ai_regular_action(state, u)
    _seele_reproduce_check(state, u, 'regular')
    _sweep_ults(state)
    _tick_buffs(u)
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_good_show_turn(state, u)
    _lc_tick_stacks(state, u)  # v5.0.1: 光锥叠层回合倒计时（Y轴回合末）
    state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
    _reset_qianye_e6_charge_gate(state)


def _resume_regular_turn(state, u):
    """回到被挤出的常规回合（规则3: 终结技执行完回自己回合）"""

    from engine.characters.qianye import _reset_qianye_e6_charge_gate
    from engine.characters.seele import _seele_reproduce_check
    state.turn_count += 1
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    # 防死循环: 同一常规回合被hold≥2次→强制结束
    hold = state.extra.get('turn_hold_guard', 0)
    if hold >= 3:
        state.log.append(f'  [WARN] {u.char.name}常规回合被hold过多次, 强制行动')
    _ai_regular_action(state, u)
    _seele_reproduce_check(state, u, 'regular')
    _sweep_ults(state)
    _tick_buffs(u)
    if state.extra.get('_elation'):
        state.extra['_elation'].tick_good_show_turn(state, u)
    _lc_tick_stacks(state, u)  # v5.0.1: 光锥叠层回合倒计时
    state.hooks.trigger(u.char.id, "on_turn_end", u=u, state=state)
    _reset_qianye_e6_charge_gate(state)
    state.extra['turn_hold_guard'] = 0


def _ai_regular_action(state, u):
    """常规行动统一入口（普攻/战技，AI主体）"""
    ai_fn = state.ai_registry.get(u.char.id)
    try:
        if ai_fn:
            ai_fn(u, state, elation=state.extra.get('_elation'),
                  max_av=state.extra.get('_max_av', 1000),
                  navs=state.extra.get('navs', {}),
                  uidx=state.units.index(u))
        else:
            _default_ai(u, state)
        from engine.characters.himeko_nova import _hn_ally_auto_support
        _hn_ally_auto_support(state, u)  # v7.2.0 #8: 队友消费助战技次数呼唤拓星者
    except Exception as e:
        state.log.append(f'  [ERROR] {u.char.name} AI崩溃: {e}')
        import traceback
        state.log.append(f'  {traceback.format_exc()}')
        raise


def _ult_post(state, unit):
    """终结技执行后的附加动作（风堇雨过天晴等）"""
    if hasattr(unit, 'char') and unit.char.id == 'fengjin':
        # 雨过天晴: 3回合，不叠加
        if not unit.extra.get('clear_sky_turns'):
            unit.extra['clear_sky_turns'] = 3
            # v6.2.1: 全队含忆灵（项目主确认: 忆灵算全队, 死龙亦算忆灵; Codex P2-6）
            sky_targets = [eu for eu in state.units if eu.is_alive] \
                + [ms for ms in state.memsprites if ms.is_alive]
            for eu in sky_targets:
                # v5.7: 记录原HP上限, 状态结束回退; 刷新时按原值重算防叠乘（此前永久改写+叠乘）
                if 'clear_sky_orig_maxhp' not in eu.extra:
                    eu.extra['clear_sky_orig_maxhp'] = eu.max_hp
                orig = eu.extra['clear_sky_orig_maxhp']
                # 风堇E1: 雨过天晴HP上限加成+50%（整体×1.5: 30%→45%, 600→900）
                eu.max_hp = orig * (1.45 if unit.eidolon_rank >= 1 else 1.30) \
                    + (900 if unit.eidolon_rank >= 1 else 600)
                eu.current_hp = min(eu.max_hp, eu.current_hp)
            state.log.append('  雨过天晴: 全队HP上限+30%+600 (3回合, 含忆灵)')
        # 小伊卡额外回合入队（例2连锁; v7.15.0 改角色包直呼）
        from engine.characters.fengjin import _fengjin_extra_turn
        _fengjin_extra_turn(state, unit)


# ---- 主模拟 ----
def _setup_battle(configs: list[dict], enemy_template: Enemy, max_av: float,
                  num_enemies: int, enemy_templates: list):
    """装配一局战斗：深拷贝输入→建敌我单位→注册队伍效果→激活角色包/子系统→
    开局效果与初始 AV→秘技→银狼开局被动。返回 (state, elation)。

    v6.5: enemy_templates 非空时逐模板创建异构敌人（每怪独立 HP/韧性/弱点/精英双动）,
    波次重生按模板列表重建; 否则沿用单模板×num_enemies 的旧契约。
    """
    from engine.characters.acheron import _acheron_apply_entry_effects
    from engine.characters.cipher import _cipher_ensure_laozhuke
    from engine.characters.the_dahlia import _dahlia_talent_open

    # v5.2 问题1修复: 配置模型是调用方输入, 记忆系统会为忆灵同步速度/后援状态,
    # 必须在局内副本上运行, 避免污染调用方 Character/LightCone/RelicSet 实例。
    configs = copy.deepcopy(configs)
    enemy_template = copy.deepcopy(enemy_template)
    # 敌人
    enemies = []
    if enemy_templates:
        for i, tpl in enumerate(enemy_templates):
            e = copy.deepcopy(tpl)
            e.id = f'{tpl.id}_{i}'
            enemies.append(e)
        num_enemies = len(enemies)
    else:
        for i in range(num_enemies):
            e = copy.deepcopy(enemy_template)
            e.id = f'{enemy_template.id}_{i}'
            enemies.append(e)
    state = SimState(enemies=enemies)

    # 角色
    units = []
    for cfg in configs:
        char = cfg["char"]
        relics, relic_sets = cfg.get("relics"), cfg.get("relic_sets")
        stats = compute_combat_stats(char, cfg.get("lightcone"), relics, relic_sets)
        u = SimUnit(char=char, base_stats=stats, position=cfg.get("position", 0),
                    lightcone=cfg.get("lightcone"))
        u.max_hp = stats.HP
        u.current_hp = stats.HP
        # v6.10 全局能量规则: 常规能量角色开局默认50%能量(显式initial_energy_pct覆盖);
        # 特殊能量角色(energy_type=special)开局0终结技进度
        if getattr(char, 'energy_type', 'regular') == 'special':
            u.current_energy = 0.0
        else:
            u.current_energy = (char.max_energy or 1) * cfg.get("initial_energy_pct", 50.0) / 100.0
        u.eidolon_rank = cfg.get("eidolon", 0)
        u._active_relic_conditions = _get_relic_conditions(relics, relic_sets)
        # v5.6: 单体技能目标优先级标记——船长4pc持有者需被队友选中叠Help
        # （数据驱动: 条件名来自 data/relics/126_恶海逐波的船长.json 复合条件; 未来嘲讽角色复用同标记）
        if 'help_stack_gain' in u._active_relic_conditions:
            u.extra['single_ally_priority'] = 1
        units.append(u)
    state.units = units

    # ── 效果解析：读取所有角色行迹/光锥/遗器/星魂，注册到 HookRegistry ──
    from engine.core.effect_resolver import register_team_effects
    register_team_effects(configs, state.hooks)

    # 队伍检测：按需激活子系统，构建AI注册表
    mechanics = TeamMechanics(units)
    elation = None
    ai_registry = {}
    # ── 角色包装配（M3: 试点角色 SKILL_HOOKS/AI 注入; 欢愉系角色保持欢愉队门控）──
    from engine.characters import activate
    ai_registry.update(activate(state, {u.char.id for u in units},
                                elation_active=mechanics.has("elation")))

    # ── 欢愉子系统 ──
    if mechanics.has("elation"):
        from engine.systems.elation import ElationSystem
        elation = ElationSystem()

    # ── 记忆子系统 ──
    remembrance = None
    if mechanics.has("remembrance"):
        from engine.systems.remembrance import RemembranceSystem
        remembrance = RemembranceSystem()
    # 通用AI注册：非欢愉角色通过此入口注册

    # 遗器开局效果
    p1 = next((u for u in units if u.position == 1), None)
    for u in units:
        if u.position != 1 and p1 and "not_first_slot_atk_buff" in getattr(u, '_active_relic_conditions', set()):
            p1.base_stats.ATK += p1.base_stats._base_ATK * 0.12
            state.log.append(f'[Init] 露莎卡({u.char.name}->{p1.char.name}): 1号位ATK+12%')

    # 战斗初始化
    if elation:
        elation.init_battle(state, units)
    if remembrance:
        remembrance.init_battle(state, units)

    # v6.7b: 大丽花天赋先于 on_enter_battle——E2 入场败谢/行迹1 需共舞者已绑定
    if any(x.char.id == 'the_dahlia' for x in units):
        _dahlia_talent_open(state)
    # Hook: 进入战斗（所有角色初始化完成后触发，含行迹开局效果）
    for u in units:
        state.hooks.trigger(u.char.id, "on_enter_battle", u=u, state=state)
    _acheron_apply_entry_effects(state)
    # v5.0 P3: 光锥战斗开始事件（event_battle_start 缓冲器）
    for u in units:
        _process_lc_effects(u, state, "on_battle_start")
    # 首波同样是波次开始。此前仅在 _respawn_wave() 派发，导致“每波次开始”
    # 的光锥效果从第二波才生效。
    for u in units:
        if u.is_alive:
            _process_lc_effects(u, state, "on_wave_start")
            state.hooks.trigger(u.char.id, "on_wave_start", u=u, state=state)

    # v5.2: 队伍上下文静态遗器条件（仙舟/龙骨全队增益 + 坠星同阵营CD）
    # 静态层(compute_combat_stats)单角色无队伍信息, 此处补全队传播。幂等。
    _apply_team_static_relics(state)
    # v5.2 问题3c: 星体差分机"首击CR+60%"——记录实际加成量, 首次攻击后扣回
    for u in units:
        conds = getattr(u, '_active_relic_conditions', set()) or set()
        if 'cd_threshold_first_atk_cr' in conds and u.base_stats.CRIT_DMG >= 1.20:
            u.extra['diff_machine_cr'] = min(1.0, u.base_stats.CRIT_RATE + 0.60) \
                - u.base_stats.CRIT_RATE

    # 初始 AV（含达成顺序戳）。战斗开始事件已施加的光锥 Buff 及状态条件
    # 需要战斗上下文参与求值；开局拉条在 navs 尚未创建时由遗器处理器暂存比例。
    unit_next_avs = {}
    for i, u in enumerate(units):
        initial_av = AV_PER_TURN / _effective_spd(u, state)
        advance_ratio = u.extra.pop('initial_action_advance_ratio', 0.0)
        unit_next_avs[i] = max(0.0, initial_av * (1.0 - advance_ratio))
    state.extra['navs'] = unit_next_avs  # 供光锥处理器拉条使用
    state.extra['stamp_counter'] = 0
    state.extra['av_stamp'] = {}
    for i in unit_next_avs:
        state.extra['stamp_counter'] += 1
        state.extra['av_stamp'][i] = state.extra['stamp_counter']
    # X 轴额外回合队列 + 常规回合暂存
    state.extra['extra_turns'] = []
    state.extra['pending_turn'] = None
    state.extra['action_ctx'] = 'regular'
    state.extra['killed_this_action'] = 0
    state.extra['ult_chain_guard'] = 0
    state.extra['turn_hold_guard'] = 0
    if state.enemies:
        state.extra['enemy_blueprint'] = copy.deepcopy(state.enemies[0])
        # v6.5: 异构敌人模板列表（波次重生按列表逐模板重建）
        if len(state.enemies) > 1 and enemy_templates:
            state.extra['enemy_blueprints'] = [copy.deepcopy(e) for e in state.enemies]
        state.extra['num_enemies'] = num_enemies
        state.extra['wave'] = 1
        # 敌方行动条初始 AV（与角色同规则; 角色先打 stamp → 同AV敌方后动）
        for i, e in enumerate(state.enemies):
            _set_av(state, state.extra.get('navs', {}), ('e', i),
                    AV_PER_TURN / max(e.SPD, 1.0))
    # 每局 AI 注册表 + 子系统引用（供 _begin_regular_turn 使用）
    state.ai_registry = dict(ai_registry)
    state.extra['_elation'] = elation
    state.extra['_rem_sys'] = remembrance
    state.extra['_max_av'] = max_av

    # v6.3.0: 秘技系统（support 全开 + battle_start 开怪者; 用户 2026-08-14 确认语义）
    _cipher_ensure_laozhuke(state)
    from engine.systems.techniques import apply_techniques
    apply_techniques(state, units)
    # v6.3.0b P1-8: 银狼行迹1·生成 开局激活（缺陷+1回合常驻; 击破植入走 on_any_weakness_break）
    for u in units:
        if u.char.id == 'silver_wolf' and any(
                getattr(t, 'hook_name', '') == 'silver_wolf_trace1_gen'
                for t in (u.char.traces or [])):
            u.extra['silver_wolf_trace1'] = True
    # v6.3.0b P1-9: 银狼E2「敌方进入战斗受伤+20%」——初始波与重生波统一施加
    sw_e2 = next((x for x in units if x.char.id == 'silver_wolf'
                  and x.is_alive and x.eidolon_rank >= 2), None)
    if sw_e2:
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.20
        state.log.append('  银狼E2: 敌方进入战斗受伤+20%')
    return state, elation


def simulate(configs: list[dict], enemy_template: Enemy, max_av: float = 1000.0,
             num_enemies: int = 1, enemy_templates: list = None) -> SimState:
    """主模拟入口：装配一局（_setup_battle）后运行第四象限主循环至时间窗/终局。"""
    state, elation = _setup_battle(configs, enemy_template, max_av,
                                   num_enemies, enemy_templates)
    units = state.units
    from engine.characters.phainon import _phainon_kasier_act

    # ---- 主循环（第四象限模型）----
    state.log.append(f'[DEBUG] 主循环开始: max_av={max_av}, units={len(units)}, enemies={len(state.enemies)}')
    for i, u in enumerate(units):
        state.log.append(f'[DEBUG]   {u.char.name}({u.char.id}): SPD={u.base_stats.SPD:.0f} HP={u.current_hp:.0f} alive={u.is_alive}')
    while True:
        # ① X轴队列非空 → 执行队头（从左往右）
        if state.extra['extra_turns']:
            unit, kind = state.extra['extra_turns'].pop(0)
            if not (unit.is_alive if hasattr(unit, 'is_alive') else unit.is_alive()):
                continue
            state.turn_count += 1
            _exec_extra_turn(state, unit, kind)
            continue
        # ② X轴清空 → 回到被挤出的常规回合
        pu = state.extra['pending_turn']
        if pu is not None:
            state.extra['pending_turn'] = None
            if pu.is_alive:
                _resume_regular_turn(state, pu)
            continue
        # ②b v6.4c 精英双动第二行动（X轴已排空 = 玩家终结技/破韧打断窗口已过）
        if _enemy_pending_step(state):
            continue
        # ③ 时间窗退出（X队列已排空）
        if state.current_av >= max_av:
            break
        # 全队阵亡检查
        if not any(u.is_alive for u in state.units):
            state.log.append('=== 全队阵亡, 模拟结束 ===')
            break
        # 波次刷新
        if not state.alive_enemies() and state.extra.get('enemy_blueprint'):
            _respawn_wave(state)
        # ④ Y轴: 合并角色+忆灵+敌方的后到先动选择
        actor, actor_av = _next_y_actor(state)
        # v6.6 白厄: 卡厄斯兰那额外回合（均分排程, 优先于其他单位）
        ph = next((x for x in state.units if x.char.id == 'phainon'
                   and x.extra.get('kasier') and x.is_alive), None)
        # v6.6b P1-5: 卡厄斯兰那额外回合不得越过 max_av（此前分支先于时间窗检查）
        if ph and ph.extra.get('kasier_next_av', 1e18) <= actor_av \
                and ph.extra.get('kasier_next_av', 1e18) < max_av:
            state.current_av = ph.extra['kasier_next_av']
            _phainon_kasier_act(state, ph)
            continue
        if actor is None or actor_av >= max_av:
            break
        # 阿哈
        if elation and elation.check_aha(state, actor_av, max_av):
            state.current_av = state.aha_next_av
            state.turn_count += 1
            elation.execute_aha(state)
            continue
        state.current_av = actor_av
        if isinstance(actor, Enemy):
            # 敌方: 常规回合（攻击）
            _begin_enemy_turn(state, actor)
        elif isinstance(actor, SimUnit):
            # 角色: 常规回合
            _begin_regular_turn(state, actor)
        elif isinstance(actor, TimelineMarker):
            # v5.3 行动条标记（浮元/倒计时）: 更新行动条后执行行动
            state.turn_count += 1
            sys = state.extra.get('_marker_sys')
            if sys:
                sys.handle_action(state, actor)
        else:
            # Y 轴忆灵: 更新 AV+stamp 后行动
            state.turn_count += 1
            key = ('ms', id(actor))
            # 遐蝶行迹2·倒置的火炬: 遐蝶HP≥50%时死龙SPD+40%；
            # 焰息击杀后的+100%仅消耗于死龙下一次行动。
            spd = _memsprite_action_speed(state, actor)
            if getattr(actor.data, 'name', '') == '死龙':
                actor.extra.pop('xiadie_spd_boost', None)
            _set_av(state, state.extra.get('navs', {}), key,
                    state.current_av + AV_PER_TURN / max(spd, 1.0))
            rem = state.extra.get('_rem_sys')
            if rem:
                rem.handle_memsprite_action(state, actor, regular_turn=True)

    state.log.append(f'\n=== 模拟结束 AV={state.current_av:.0f} {state.turn_count}回合 ===')
    # 轮次统计: 以队内最慢存活角色行动值为一轮的近似（竞速语义）
    alive_spds = [u.base_stats.SPD for u in state.units if u.is_alive]
    state.cycles = int(state.current_av * min(alive_spds) / AV_PER_TURN) if alive_spds else 0
    return state


def _default_ai(u: SimUnit, state: SimState):
    from engine.characters.himeko_nova import _hn_realm_blocks_ult
    if u.current_energy >= u.char.max_energy and u.char.max_energy > 0 \
            and not _hn_realm_blocks_ult(state, u):  # v7.2.0 裁决A
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


# ════════ v6.6 批1: 缇宝/刻律德菈/丹恒·腾荒（角色技能介绍/同谐、存护）════════

# ── 缇宝（同谐·量子）──


# ── 刻律德菈（同谐·风）──


# ── 丹恒·腾荒（存护·物理）──


# v6.6c P1: 龙灵行动注册（函数定义在文件后部, 追加注册防 NameError）


# ════════ v6.6 批2: 海瑟音/那刻夏/赛飞儿（角色技能介绍/虚无、智识）════════

# ── 海瑟音（虚无·物理）──

HYSILENS_DOTS = [
    ('风化', '风', 25.0), ('灼烧', '火', 25.0), ('触电', '雷', 25.0),
    ('裂伤', '物理', 0.0),  # 裂伤按敌HP 20% 封顶 25%ATK
]


# ── 那刻夏（智识·风）──


def _enemy_weakness_elements(target):
    """Return every live weakness, including natural and implanted sources."""
    elements = {element for element in WEAKNESS_ELEMENTS
                if target.element_res.get(element, 0.20) <= 0.0}
    elements.update(status.attributes.get('weakness_element')
                    for status in getattr(target, 'statuses', [])
                    if status.attributes.get('weakness_element'))
    return elements


# ── 赛飞儿（虚无·量子）──


# ════════ v6.6 批3: 白厄（毁灭·物理, 变身状态机）════════


# ════════════ v6.7 大丽花机制（角色技能介绍/虚无/大丽花.txt）════════════

def _flat_toughness_with_break(state, u, t, amount, element='火', skill_key='talent', stats=None):
    """固定削韧统一入口（v6.7b Harness）: 手写削韧段不再裸改 toughness 绕过击破事件——
    击破时结算击破伤害+25%延后+属性异常+hooks, 与 _apply_toughness_damage 主韧性分支同口径。
    返回击破伤害量。"""
    if t is None or getattr(t, 'HP', 0) <= 0 or t.toughness <= 0 or t.is_broken or t.max_toughness <= 0:
        return 0.0
    t.toughness = max(0, t.toughness - amount)
    if t.toughness > 0 or t.is_broken:
        return 0.0
    t.is_broken = True
    sb_stats = stats or _build_effective_stats(u, state)
    bd = calculate_damage(sb_stats, t, 0, 0, "break", element, 80, False)
    _commit_enemy_damage(state, u, t, bd.final_damage)
    u.total_damage_dealt += bd.final_damage
    t.extra['av_delayed'] = 2500.0
    _apply_break_debuff(t, element, u, state)
    state.hooks.trigger(u.char.id, "on_weakness_break", u=u, state=state)
    _process_lc_effects(u, state, "on_weakness_break")
    state.hooks.trigger_all("on_any_weakness_break", u=u, actor=u, state=state,
                            enemy=t, skill_key=skill_key)
    state.log.append(f'  固定削韧击破! {t.name or t.id} 击破={bd.final_damage:.0f}({element})')
    return bd.final_damage


# ════════════ v6.7 绯英机制（角色技能介绍/欢愉/绯英.txt）════════════


# ════════════ v6.7 火花机制（角色技能介绍/欢愉/火花.txt）════════════


# ════════════ v6.7 姬子·启行机制（角色技能介绍/智识/姬子·启行.txt）════════════

# 开拓同行角色定义（txt: 开拓者(所有命途)/姬子/姬子•启行/三月七(存护/巡猎)/长夜月/
# 丹恒/丹恒•饮月/丹恒•腾荒/瓦尔特/星期日）


# 同行协议·裁决（开拓者/丹恒/星期日）
# 同行协议·歼破（三月七/长夜月/瓦尔特/姬子）


# ════════════ v6.9 星期日机制（角色技能介绍/同谐/星期日.txt）════════════


# ════════════ v6.9 瓦尔特机制（角色技能介绍/虚无/瓦尔特.txt）════════════


# ════════════ v6.9 阮·梅机制（角色技能介绍/同谐/阮·梅.txt）════════════


# ════════════ v6.9 知更鸟机制（角色技能介绍/同谐/知更鸟.txt）════════════


# ════════════ v6.11.1 知更鸟·晴歌（记忆, 晴空乐手+Fever倒计时）════════════
# 数据源: 角色技能介绍/记忆/知更鸟·晴歌.txt (用户原稿 v2)
# v7.1.0 项目主澄清: 贝茜/啾米/派丁仅为「晴空乐手」的状态档位, 实机按一只忆灵计算
# 核心循环: 战技召唤晴空乐手(贝茜档) → 攻击/治疗/护盾攒气氛 → 6/12点升档(啾米/派丁登台)
# → 全员登台(3档)进Fever(晴歌离场, 晴空乐手入行动条+140速倒计时扣气氛) → 气氛归零散场


# 晴歌倒计时 marker 延迟注册（MARKER_ACTIONS 在模块前部构建, 函数定义在后方）


# ════════════ v6.9 不死途机制（角色技能介绍/巡猎/不死途.txt）════════════


# ════════════ v6.9 千冶·刃机制（角色技能介绍/虚无/千冶·刃.txt）════════════


    # 新终结技后仍保持无量忿怒（txt: 解放战技并获得全新终结技, 未说消耗结界）


# ════════════ v6.10 黄泉机制（角色技能介绍/虚无/黄泉.txt, 特殊能量·残梦9）════════════


# ════════════ v6.10 飞霄机制（角色技能介绍/巡猎/飞霄.txt, 特殊能量·飞黄12）════════════


