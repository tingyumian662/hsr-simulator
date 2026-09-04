"""灵砂（M4 批1 迁入；浮元 marker 三表齐迁, 联动风堇走包内顶层导入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage, _hook_owner
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _apply_toughness_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _ensure_marker_system
from engine.characters.fengjin import _fengjin_cleanse
from engine.characters.fengjin import _fengjin_talent_heal_buff
from engine.core.combat_engine import _marker_heal_allies
from engine.core.combat_engine import _pick_fire_weak_target
from engine.core.combat_engine import _process_lc_effects


def _lingsha_fuyuan_action(state, marker):
    """浮元行动: 全队追击(75%ATK火伤)+随机单体(75%ATK)+削韧(全体每目标10+单体10)
    +净化全员1负面+全队治疗; E4治疗最低HP队友; E6额外4次50%ATK+每次削韧5（用户确认数值）"""
    summoner = next((x for x in state.units if x.char.id == 'lingsha' and x.is_alive), None)
    sys = state.extra.get('_marker_sys')
    if summoner is None:
        if sys:
            sys.despawn(state, marker)  # 召唤者阵亡浮元消失（双保险）
        return
    stats = _build_effective_stats(summoner, state)
    alive = state.alive_enemies() or state.enemies
    if not alive:
        return
    dmg_total = 0.0

    def _fua_damage_hit(t, scale, toughness):
        """浮元单段伤害：大公逐段事件在命中后广播。"""
        nonlocal dmg_total
        d = calculate_damage(stats, _enemy_for_damage(t), stats.ATK, scale, "direct", "火", 80,
                             False, crit_mode="expected", attack_type="follow_up")
        _commit_enemy_damage(state, summoner, t, d.final_damage)
        dmg_total += d.final_damage
        dmg_total += _apply_toughness_damage(state, summoner, t, toughness, "火", "talent", stats)
        state.hooks.trigger_all("on_followup_hit", u=summoner, state=state)

    # ① 全体追击 + 削韧（每目标10）
    for t in list(alive):
        if t.HP <= 0:
            continue
        _fua_damage_hit(t, 75.0, 10.0)
    # ② 额外随机单体（优先韧>0且火弱点）+ 削韧10
    pool = [t for t in alive if t.HP > 0]
    if pool:
        single = _pick_fire_weak_target(pool)
        _fua_damage_hit(single, 75.0, 10.0)
        # ③ E6: 额外4次（50%ATK + 每次削韧5）
        if summoner.eidolon_rank >= 6:
            for _ in range(4):
                tgt = _pick_fire_weak_target([t for t in alive if t.HP > 0])
                if tgt is None:
                    break
                _fua_damage_hit(tgt, 50.0, 5.0)
    summoner.total_damage_dealt += dmg_total
    state.log.append(f'  浮元: 全队追击+单体 {dmg_total:.0f}')
    state.hooks.trigger_all("on_attack_action", u=summoner, state=state, dealt=dmg_total > 0)  # v7.1.0 P1: marker行动攻击补气氛
    # 追加攻击动作完成后仅广播一次：温驯/流光、千星、都蓝等均为动作级效果。
    # 浮元此前遗漏光锥侧 on_followup 处理。
    _process_lc_effects(summoner, state, "on_followup")
    state.hooks.trigger_all("on_followup", u=summoner, state=state)
    # ④ 净化全员1负面
    _fengjin_cleanse(state, summoner)
    # ⑤ 全队治疗（12%ATK+360, 满级; ATK基数）
    _marker_heal_allies(state, summoner, "lingsha_fuyuan_heal")
    # ⑥ E4: 治疗当前HP最低队友 40%ATK
    if summoner.eidolon_rank >= 4:
        alive_units = [x for x in state.units if x.is_alive]
        if alive_units:
            lowest = min(alive_units, key=lambda x: x.current_hp / max(x.max_hp, 1))
            amt = stats.ATK * 0.40 * (1.0 + stats.HEAL_BONUS)
            lowest.current_hp = min(lowest.max_hp, lowest.current_hp + amt)
            state.hooks.trigger_all("on_heal", u=summoner, state=state, healer=summoner,
                                    targets=[lowest], heal_amt=amt)
            _fengjin_talent_heal_buff(state, summoner)
            state.log.append(f'  浮元E4: 治疗{lowest.char.name}+{amt:.0f}')


def _lingsha_fuyuan_spawn_e6(state, marker, summoner):
    """灵砂E6: 浮元在场→敌方全属性抗性-20%（消失时恢复）"""
    if summoner.eidolon_rank >= 6:
        for e in state.enemies:
            for elem in e.element_res:
                e.element_res[elem] -= 0.20
            e.extra['lingsha_e6_res_down'] = e.extra.get('lingsha_e6_res_down', 0) + 1
        state.log.append('  灵砂E6: 敌方全属性抗性-20%')


def _lingsha_fuyuan_despawn(state, marker):
    """灵砂E6: 浮元消失→恢复敌方全属性抗性"""
    for e in state.enemies:
        n = e.extra.get('lingsha_e6_res_down', 0)
        if n > 0:
            for elem in e.element_res:
                e.element_res[elem] += 0.20 * n
            e.extra['lingsha_e6_res_down'] = 0
            state.log.append('  灵砂E6: 敌方全属性抗性恢复')


def _trace_lingsha_t2_be_to_atk_heal(u, state, **kw):
    """行迹2·朱燎: 攻击力/治疗量提高 = 击破特攻的25%/10%（上限50%/20%）"""
    if u.char.id != 'lingsha':
        return
    be = u.base_stats.BREAK_EFFECT
    atk_bonus = min(be * 0.25, 0.50)
    heal_bonus = min(be * 0.10, 0.20)
    u.base_stats.ATK += u.base_stats._base_ATK * atk_bonus
    u.base_stats.HEAL_BONUS += heal_bonus
    state.log.append(f'  行迹2·朱燎: 攻击力+{atk_bonus*100:.1f}%, 治疗量+{heal_bonus*100:.1f}%')


def _trace_lingsha_t3_pursuit(u, state, **kw):
    """行迹3·遗爇: 浮元在场+队友受伤/耗血且队内有HP%≤60%角色→浮元立即追击
    （不消耗行动次数, 2回合后可再次触发）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'lingsha' or not owner.is_alive:
        return
    if not (owner.marker and owner.marker.is_alive):
        return
    last = owner.extra.get('lingsha_t3_last_turn', -99)
    if state.turn_count - last < 2:
        return
    low = [x for x in state.units if x.is_alive and x.current_hp <= x.max_hp * 0.60]
    if not low:
        return
    owner.extra['lingsha_t3_last_turn'] = state.turn_count
    _lingsha_fuyuan_action(state, owner.marker)
    state.log.append('  行迹3·遗爇: 浮元立即追击(不耗行动次数)')


def _eid_lingsha_e1_break(u, state, **kw):
    """灵砂E1: 敌方弱点被击破时其防御力降低20%（击破期间持续, 韧性恢复时移除）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'lingsha' or owner.eidolon_rank < 1 or not owner.is_alive:
        return
    t = kw.get('enemy')
    if t:
        from engine.models.enemy import EnemyStatus
        t.add_status(EnemyStatus(id='lingsha_e1_def_down', name='防御降低', category='debuff',
                                 remaining_turns=-1, attributes={'def_reduction': 0.2}))
        state.log.append(f'  E1: {t.name or t.id} DEF-20% (击破期间)')


def _eid_lingsha_e2_ult(u, state, **kw):
    """灵砂E2: 施放终结技时全队击破特攻+40%，持续3回合"""
    if u.char.id != 'lingsha':
        return
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='lingsha',
                                      attributes={'BREAK_EFFECT': 40.0},
                                      remaining_turns=3, source_name='灵砂E2',
                                      param_id='lingsha_e2_be'))
    state.log.append('  E2: 全队击破特攻+40% (3回合)')


def _tech_lingsha(state, u, is_opener):
    """灵砂: 召唤浮元 + 全敌醇醉2回合（用户 2026-08-14 补录: 秘技·流翠散云, 非进战）"""
    from engine.runtime import _tech_enemies
    # v6.3.0b P1-1: 秘技阶段标记系统未惰性创建, 需先创建再召唤（此前 sys 为 None 只挂醇醉）
    from engine.core.combat_engine import _ensure_marker_system
    sys = _ensure_marker_system(state)
    sys.spawn(state, u, 'lingsha_fuyuan')
    from engine.models.enemy import EnemyStatus
    for e in _tech_enemies(state):
        e.add_status(EnemyStatus(id='lingsha_chunzui', name='醇醉', category='debuff',
                                 source='lingsha', remaining_turns=2,
                                 attributes={'vulnerability_break': 0.25}))
    state.log.append('[秘技] 流翠散云: 召唤浮元 + 全敌醇醉2回合')


CHAR_ID = "lingsha"
TECHNIQUE = _tech_lingsha
MARKERS = {"lingsha_fuyuan": _lingsha_fuyuan_action}
MARKER_DESPAWN = {"lingsha_fuyuan": _lingsha_fuyuan_despawn}
MARKER_SPAWN = {"lingsha_fuyuan": _lingsha_fuyuan_spawn_e6}


# M4 收官批: 击破配装策略随角色（原 relic_optimizer.BREAK_CHAR_CONFIG 条目）
BREAK_CONFIG = {'spd_target': 134.0}
