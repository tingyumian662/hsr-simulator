"""acheron（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _apply_toughness_damage
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _record_kill_after_damage
from engine.core.combat_engine import _use_skill


def _acheron_gain_dream(state, u, amt=1, jizhen_target=None):
    """残梦+集真赤: 残梦上限9(溢出转四相断我); 集真赤1层"""
    if u.char.id != 'acheron':
        return
    dream = u.extra.get('acheron_dream', 0) + amt
    if dream > 9:
        # 行迹1·赤鬼: 溢出→四相断我（上限3层）
        overflow = dream - 9
        u.extra['acheron_sixiang'] = min(3, u.extra.get('acheron_sixiang', 0) + overflow)
        dream = 9
    u.extra['acheron_dream'] = dream
    if jizhen_target is not None and getattr(jizhen_target, 'HP', 0) > 0:
        jizhen_target.extra['acheron_jizhen'] = jizhen_target.extra.get('acheron_jizhen', 0) + 1
        state.log.append(f'  【集真赤】: {jizhen_target.name or jizhen_target.id} '
                         f'{jizhen_target.extra["acheron_jizhen"]}层')
    state.log.append(f'  黄泉: 残梦+{amt} → {dream}/9'
                     f'{" (四相断我" + str(u.extra.get("acheron_sixiang", 0)) + "层)" if dream >= 9 else ""}')


def _acheron_apply_jizhen(state, u, target, layers=1):
    """为指定目标附上集真赤"""
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    target.extra['acheron_jizhen'] = target.extra.get('acheron_jizhen', 0) + layers
    state.log.append(f'  【集真赤】: {target.name or target.id} {target.extra["acheron_jizhen"]}层')


def _acheron_jizhen_transfer(state):
    """集真赤离场转移: 目标死亡后转移给集真赤最多的存活目标"""
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            layers = e.extra.pop('acheron_jizhen', 0)
            if layers <= 0:
                continue
            alive = [x for x in state.enemies if getattr(x, 'HP', 0) > 0]
            if not alive:
                continue
            top = max(alive, key=lambda x: x.extra.get('acheron_jizhen', 0))
            top.extra['acheron_jizhen'] = top.extra.get('acheron_jizhen', 0) + layers
            state.log.append(f'  集真赤转移: {layers}层 → {top.name or top.id}')


def _acheron_talent_on_debuff(u, state, target, **_kwargs):
    """天赋: 任意单位施放技能期间使敌陷入负面→+1残梦+集真赤1层(每次施放最多1次)"""
    acheron = next((x for x in state.units
                    if x.char.id == 'acheron' and x.is_alive), None)
    if acheron is None:
        return
    if u.extra.get('acheron_talent_triggered'):
        return  # 本次施放已触发
    u.extra['acheron_talent_triggered'] = True
    _acheron_gain_dream(state, acheron, 1)
    _acheron_apply_jizhen(state, acheron, target, 1)


def _acheron_skill(state, u):
    """战技: +1残梦+集真赤1层（战技直接效果, 非负面触发）"""
    alive = state.alive_enemies() or state.enemies
    target = alive[0] if alive else None
    if target is not None:
        _acheron_gain_dream(state, u, 1)
        _acheron_apply_jizhen(state, u, target, 1)


def _acheron_apply_entry_effects(state):
    """Acheron E4: mark each enemy entering the current wave."""
    acheron = next((x for x in state.units
                    if x.char.id == 'acheron' and x.is_alive and x.eidolon_rank >= 4), None)
    if acheron is None:
        return
    from engine.models.enemy import EnemyStatus
    for enemy in state.enemies:
        existing = next((status for status in enemy.statuses
                          if status.id == 'acheron_e4_ultimate_vulnerability'), None)
        if existing is not None:
            existing.remaining_turns = -1
            existing.attributes['vulnerability_ultimate'] = 0.08
            continue
        enemy.add_status(EnemyStatus(
            id='acheron_e4_ultimate_vulnerability',
            name='终结技易伤',
            category='debuff',
            source='acheron',
            remaining_turns=-1,
            attributes={'vulnerability_ultimate': 0.08},
        ))


def _acheron_apply_leixin(u, stacks):
    """Refresh the trace-3 damage buff from the current layer count."""
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'acheron_leixin']
    if stacks <= 0:
        return
    u.buffs.append(TimedBuff(
        source_id='acheron',
        attributes={'DMG_BONUS_ALL': 30.0 * stacks},
        remaining_turns=3,
        param_id='acheron_leixin',
        source_name='黄泉·雷心',
    ))


def _acheron_trace3_damage_multiplier(u) -> float:
    return 1.0 + 0.30 * min(3, u.extra.get('acheron_leixin', 0))


def _acheron_original_damage_multiplier(u, state) -> float:
    """奈落 is an independent original-damage multiplier, not DMG bonus."""
    if state is None or u.char.id != 'acheron':
        return 1.0
    nihility_allies = sum(
        1 for ally in state.units
        if ally.is_alive and ally is not u and ally.char.path == '虚无'
    )
    max_requirement = 1 if u.eidolon_rank >= 2 else 2
    if nihility_allies >= max_requirement:
        return 1.60
    if nihility_allies >= 1:
        return 1.15
    return 1.0


def _acheron_ult(state, u):
    """终结技: 耗9残梦; 3×啼泽雨斩(24%ATK单体, 消最多3层集真赤→全敌15%ATK+每层提升至60%)
    +黄泉返渡(120%ATK全体+清集真赤+行迹3额外6×25%ATK弹射);
    终结技期无视弱点削韧+全抗-20%; 行迹3增伤30%×3层"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _enemy_for_damage, _apply_toughness_damage)
    import random as _r
    if u.extra.get('acheron_dream', 0) < 9:
        state.log.append('  [WARN] 黄泉: 残梦不足9, 无法施放终结技')
        return
    u.extra['acheron_dream'] = 0
    stats = _build_effective_stats(u, state)
    # 行迹3·雷心: 增伤30%×3层（啼泽雨斩击中集真赤目标时叠加, 3回合）
    trace3 = any(getattr(tr, 'hook_name', '') == 'acheron_trace3' for tr in (u.char.traces or []))
    # 终结技期全抗-20%; E6 adds another 20% ultimate RES PEN.
    stats = copy.deepcopy(stats)
    stats.RES_PEN_ALL += 0.20 + (0.20 if u.eidolon_rank >= 6 else 0.0)
    total = 0.0
    # The displayed ultimate toughness value is the full action total. Apply it
    # once to every target through the shared break lifecycle.
    for enemy in list(state.alive_enemies()):
        before = enemy.HP
        _apply_toughness_damage(state, u, enemy, 20.0, '雷', 'ultimate', stats)
        _record_kill_after_damage(state, u, enemy, before)
    # 啼泽雨斩 ×3（逐次对主目标; 行迹3: 击中集真赤目标→增伤叠层）
    for i in range(3):
        alive_now = state.alive_enemies()
        if not alive_now:
            break
        t = alive_now[0]
        if trace3 and t.extra.get('acheron_jizhen', 0) > 0:
            stacks = min(3, u.extra.get('acheron_leixin', 0) + 1)
            u.extra['acheron_leixin'] = stacks
            _acheron_apply_leixin(u, stacks)
            state.log.append(f'  行迹3·雷心: 增伤30%×{stacks}层')
        # 消集真赤（最多3层）→ 全敌15%ATK+每层提升至60%
        jz = min(3, t.extra.get('acheron_jizhen', 0))
        t.extra['acheron_jizhen'] = t.extra.get('acheron_jizhen', 0) - jz
        before = t.HP
        d = calculate_damage(stats, _enemy_for_damage(t, 'ultimate'), stats.ATK, 24.0,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate', crit_mode='expected')
        d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                           * _acheron_trace3_damage_multiplier(u))
        _commit_enemy_damage(
            state, u, t, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        if jz > 0:
            for e in state.enemies:
                if getattr(e, 'HP', 0) <= 0:
                    continue
                scale = 15.0 + jz * 15.0  # 15%基础+每层15%→最多60%
                before = e.HP
                d2 = calculate_damage(stats, _enemy_for_damage(e, 'ultimate'), stats.ATK, scale,
                                      'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                                      true_dmg_ratio=state.realm_true_dmg,
                                      skill_type='ultimate', crit_mode='expected')
                d2.final_damage *= (_acheron_original_damage_multiplier(u, state)
                                    * _acheron_trace3_damage_multiplier(u))
                _commit_enemy_damage(
                    state, u, e, d2.final_damage,
                    cipher_record_amount=d2.final_damage / (1.0 + state.realm_true_dmg))
                total += d2.final_damage
            state.log.append(f'  啼泽雨斩消{jz}层集真赤: 全敌{15+jz*15:.0f}%ATK')
    # 黄泉返渡: 120%ATK全体+移除所有集真赤+行迹3额外6×25%ATK弹射
    for e in state.alive_enemies():
        before = e.HP
        d = calculate_damage(stats, _enemy_for_damage(e, 'ultimate'), stats.ATK, 120.0,
                             'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             skill_type='ultimate', crit_mode='expected')
        d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                           * _acheron_trace3_damage_multiplier(u))
        _commit_enemy_damage(
            state, u, e, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
    for e in state.enemies:
        e.extra.pop('acheron_jizhen', None)
    if trace3:
        for _ in range(6):
            alive_now = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
            if not alive_now:
                break
            t = _r.choice(alive_now)
            before = t.HP
            d = calculate_damage(stats, _enemy_for_damage(t, 'ultimate'), stats.ATK, 25.0,
                                 'direct', '雷', 80, stats.CRIT_RATE >= 0.5,
                                 true_dmg_ratio=state.realm_true_dmg,
                                 skill_type='ultimate', crit_mode='expected')
            d.final_damage *= (_acheron_original_damage_multiplier(u, state)
                               * _acheron_trace3_damage_multiplier(u))
            _commit_enemy_damage(
                state, u, t, d.final_damage,
                cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
            total += d.final_damage
        state.log.append('  行迹3: 黄泉返渡额外6×25%ATK')
    sixiang = u.extra.pop('acheron_sixiang', 0)
    if sixiang > 0:
        for _ in range(sixiang):
            alive_now = state.alive_enemies()
            target = _r.choice(alive_now) if alive_now else None
            _acheron_gain_dream(state, u, 1, jizhen_target=target)
        state.log.append(f'  【四相断我】: 终结技后消耗{sixiang}层→残梦+{sixiang}')
    u.total_damage_dealt += total
    u.damage_log.append(('残梦尽染，一刀缭断', total, 'ultimate'))
    state.log.append(f'  黄泉终结技: {total:.0f} (3×啼泽雨斩+返渡)')
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=total > 0)  # v7.1.0 P1: 0倍率终结技补气氛


def _acheron_tick(state, u):
    """黄泉回合开始: E2 +1残梦+集真赤(集真赤最多目标)"""
    if u.eidolon_rank >= 2:
        alive = state.alive_enemies() or state.enemies
        if alive:
            top = max(alive, key=lambda x: x.extra.get('acheron_jizhen', 0))
            _acheron_gain_dream(state, u, 1, jizhen_target=top)
            state.log.append('  黄泉E2: 回合开始+1残梦+集真赤')


def _acheron_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """黄泉 AI: 残梦满9→终结技; SP>0→战技; 否则普攻"""
    if u.extra.get('acheron_dream', 0) >= 9:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _trace_acheron_trace1(u, state, **kw):
    """行迹1·赤鬼: 开局5残梦+集真赤5层(随机1敌); 溢出→四相断我"""
    if u.char.id != 'acheron':
        return

    import random
    alive = state.alive_enemies() or state.enemies
    _acheron_gain_dream(state, u, 5)
    if alive:
        t = random.choice(alive)
        _acheron_apply_jizhen(state, u, t, 5)
    state.log.append('  黄泉行迹1: 开局5残梦+集真赤5层')


def _trace_acheron_tick(u, state, **kw):
    """黄泉回合开始: E2 +1残梦+集真赤"""
    if u.char.id != 'acheron':
        return

    _acheron_tick(state, u)


def _tech_acheron(state, u, is_opener):
    """黄泉: 每波200%ATK雷伤+无视弱点削韧+四相断我(施放终结技后+1残梦+集真赤)
    （进战·四相断我; _respawn_wave 接线）"""
    if not is_opener:
        return
    from engine.core.combat_engine import _apply_toughness_damage, _build_effective_stats, _commit_enemy_damage, calculate_damage
    from engine.runtime import _enemy_for_damage
    state.extra['acheron_tech_active'] = True
    _acheron_apply_entry_effects(state)
    u.extra['acheron_sixiang'] = min(3, u.extra.get('acheron_sixiang', 0) + 1)
    stats = _build_effective_stats(u, state)
    total = 0.0
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e, 'technique'), stats.ATK, 200.0, 'direct', '雷', 80,
                             stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             crit_mode='expected')
        _commit_enemy_damage(
            state, u, e, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        _apply_toughness_damage(state, u, e, 20.0, '雷', 'technique', stats)
    u.total_damage_dealt += total
    state.log.append(f'[秘技] 四相断我: 全敌200%ATK雷伤+无视弱点削韧 {total:.0f}，四相断我+1')


def _init_battle(state):
    # 天赋事件: 任意施放者使敌陷入负面→+1残梦+集真赤
    state.hooks.register(CHAR_ID, 'on_debuff_applied',
                         _acheron_talent_on_debuff, source_name='黄泉天赋')



def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _acheron_skill(state, u)

def _skill_hook_1(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _acheron_ult(state, u)


CHAR_ID = "acheron"
AI = _acheron_ai
INIT = _init_battle
TECHNIQUE = _tech_acheron
SKILL_HOOKS = [_skill_hook_0, _skill_hook_1]


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _acheron_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'acheron':
        _acheron_tick(state, u)


TURN_TICKS = {'pre': _acheron_turn_tick}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _acheron_attack_type_override(u, state, st, skill_key):
    """PHASE attack_type_override: E6 普攻/战技视为终结技（→st|None）。"""
    if u.eidolon_rank >= 6 and skill_key in ('basic_attack', 'skill'):
        return 'ultimate'
    return None


def _acheron_damage_mod(u, state, final, st):
    """PHASE damage_mod: 原初姬子倍率修饰（→新final|None）。"""
    if st in ('basic', 'skill', 'ultimate'):
        return final * _acheron_original_damage_multiplier(u, state)
    return None


PHASE_HOOKS['attack_type_override'] = _acheron_attack_type_override
PHASE_HOOKS['damage_mod'] = _acheron_damage_mod
