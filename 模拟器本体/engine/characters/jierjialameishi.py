"""吉尔伽美什（jierjialameishi）——Fate 联动·毁灭·雷（v7.21.0 录入, txt 同名）

核心机制:
- 兴致（u.extra['gil_xingzhi']）: 队友行动+1（天赋）/ 队友终结技+2（行迹1）/ E2 开战+5・
  终结技+5 / E6 黄金律队友终结技另计 / 连携合击+3 / 秘技+3; 每点 SPD+10%（增量改
  base_stats, 战技清空时对称回落）
- 来兴致了: 首次达 10 点进入（整场, extra['gil_high_spirit']）——仅能施放战技且免 SP
  （sp_cost_override→0）, 战技后清空兴致
- 王之财宝（战技）: 扩散 280/140; 自身【王来承认】无视防 30% 3回合（E1 全队生效 +
  自身 ATK60% + 战技回 40 能）; E2 主+100%/邻+50%
- 天地乖离（终结技）: 全体 400% + 10×100% 弹射（E6 弹射 180%）; 耗尽黄金律每点终结技
  暴伤+100%; E2 终结技+5 兴致; 行迹1 终结技+2 兴致
- 天赋「尽情取悦本王吧」: 初始回合开始自动普攻（来兴致了后不再）; 队友行动 +1 兴致;
  队友终结技 → 王来背负（自身终结技伤害+40% 3回合）+ 兴致 +2 + 回能30%×消耗
- 行迹2: 每获得 1 点兴致 → 暴伤+25% 最多 6 层; 行迹3: 全队 ATK/CD+20% + 能量上限
  >140 每点+1%（≤100%）光环（开战一次性, 死亡对称回减）
- 连携「本王允许你进攻」: Gil/Saber 每次技能累计 1 点（按技能次数口径）, 任意单位
  攻击后 ≥8 → 双人全体合击 + Gil+3 兴致 + saber._saber_joint_reward
- 秘技天之锁: 进战全敌 200% 攻击力雷伤 + 3 兴致（领域持续 10 秒为战斗外效果, 不模拟）
"""
import copy
import random

from engine.runtime import TimedBuff, _enemy_for_damage, _select_targets
from engine.core.damage import calculate_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _flat_toughness_with_break,
    _gain_energy, _skill_level_factor, _use_skill,
)

CHAR_ID = "jierjialameishi"
ELEMENT = "雷"


def _gil_find(state):
    return next((x for x in state.units if x.char.id == CHAR_ID and x.is_alive), None)


def _gil_xingzhi_gain(u, n, state=None, note=''):
    """获得 n 点兴致: SPD +10%/点（增量）; 首次≥10 进入来兴致了。"""
    if n <= 0:
        return
    u.extra['gil_xingzhi'] = u.extra.get('gil_xingzhi', 0) + n
    u.base_stats.SPD += u.base_stats._base_SPD * 0.10 * n
    # 行迹2: 每获得1点 → 暴伤+25% 最多6层
    if any(t.hook_name == 'jierjialameishi_trace2_pride' for t in (u.char.traces or [])):
        total = u.extra.get('gil_xingzhi_gained', 0) + n
        u.extra['gil_xingzhi_gained'] = total
        new_stacks = min(6, total)
        delta = new_stacks - u.extra.get('gil_t2_cd_stacks', 0)
        if delta > 0:
            u.extra['gil_t2_cd_stacks'] = new_stacks
            u.base_stats.CRIT_DMG += 0.25 * delta
    if state is not None:
        state.log.append(f'  兴致+{n}{note}({u.extra["gil_xingzhi"]})')
    if u.extra['gil_xingzhi'] >= 10 and not u.extra.get('gil_high_spirit'):
        u.extra['gil_high_spirit'] = True
        if state is not None:
            state.log.append('  【来兴致了!】整场: 仅能施放战技且不耗战技点')


def _gil_xingzhi_clear(u, state=None):
    """清空兴致（战技后）: SPD 对称回落。"""
    cur = u.extra.pop('gil_xingzhi', 0)
    if cur > 0:
        u.base_stats.SPD -= u.base_stats._base_SPD * 0.10 * cur
        if state is not None:
            state.log.append(f'  兴致清空({cur}), 速度回落')


def _alive_enemies(state):
    return [e for e in state.enemies if getattr(e, 'HP', 0.0) > 0]


def _gil_deal(state, u, stats, targets, scale, toughness, skill_type):
    total = 0.0
    for t in targets:
        if getattr(t, 'HP', 0.0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(t, skill_type), stats.ATK, scale,
                             'direct', ELEMENT, 80, stats.CRIT_RATE >= 0.5,
                             skill_type=skill_type, attack_type='active',
                             crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
        if t.toughness > 0:
            _flat_toughness_with_break(state, u, t, toughness, ELEMENT, skill_type, stats)
    return total


def _gil_joint_attack(state):
    """连携「本王允许你进攻」: 双人全体合击 + 兴致/能量收益。"""
    gil = _gil_find(state)
    if gil is None:
        return
    saber = next((x for x in state.units if x.char.id == 'saber' and x.is_alive), None)
    alive = _alive_enemies(state)
    total = 0.0
    gil_stats = _build_effective_stats(gil, state)
    total += _gil_deal(state, gil, gil_stats, alive, 400.0, 20.0, 'skill')
    state.log.append('  本王允许你进攻: 吉尔伽美什 400%全体雷伤')
    if saber is not None:
        saber_stats = _build_effective_stats(saber, state)
        stotal = 0.0
        for t in alive:
            if getattr(t, 'HP', 0.0) <= 0:
                continue
            d = calculate_damage(saber_stats, _enemy_for_damage(t, 'skill'), saber_stats.ATK,
                                 600.0, 'direct', '风', 80, saber_stats.CRIT_RATE >= 0.5,
                                 skill_type='skill', attack_type='follow_up',
                                 crit_mode='expected')
            _commit_enemy_damage(state, saber, t, d.final_damage)
            stotal += d.final_damage
            if t.toughness > 0:
                _flat_toughness_with_break(state, saber, t, 20.0, '风', 'skill', saber_stats)
        saber.total_damage_dealt += stotal
        from engine.characters.saber import _saber_joint_reward
        _saber_joint_reward(state)
        state.log.append('  本王允许你进攻: Saber 600%全体风伤')
    gil.total_damage_dealt += total
    _gil_xingzhi_gain(gil, 3, state, note='(连携)')


# ---- 技能实现 ----

def _gil_skill_cast(state, u):
    """王之财宝: 扩散 280/140(E2 +100/+50); 王来承认; 来兴致了下战后清空兴致。"""
    stats = _build_effective_stats(u, state)
    lf = _skill_level_factor(u, 'skill')
    main, adj = 280.0 * lf, 140.0 * lf
    if u.eidolon_rank >= 2:
        main += 100.0
        adj += 50.0
    has_ack = any(getattr(b, 'param_id', '') == 'gil_acknowledged' for b in u.buffs)
    if has_ack:  # 王来承认持有中: 无视防已入面板 buff, 此处仅日志
        state.log.append('  王之财宝: 持有王来承认')
    alive = _alive_enemies(state)
    tsc = alive[0] if alive else None
    main_list = [tsc] if tsc is not None else []
    adj_list = [e for e in alive if e is not tsc][:2]
    total = _gil_deal(state, u, stats, main_list, main, 20.0, 'skill')
    total += _gil_deal(state, u, stats, adj_list, adj, 10.0, 'skill')
    u.total_damage_dealt += total
    # 王来承认: 自身无视防30% 3回合（E1: 全队 + 自身ATK60%）
    e1 = u.eidolon_rank >= 1
    targets = [x for x in state.units if x.is_alive] if e1 else [u]
    for t in targets:
        t.buffs = [b for b in t.buffs if getattr(b, 'param_id', '') != 'gil_acknowledged']
        t.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'DEF_PEN': 30.0},
                                 remaining_turns=3, param_id='gil_acknowledged',
                                 source_name='王来承认'))
    if e1:
        u.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'ATK_PERCENT': 60.0},
                                 remaining_turns=3, param_id='gil_ack_atk',
                                 source_name='见证一切之人'))
        _gain_energy(u, 40.0, state=state, apply_regen=False)
        state.log.append('  E1·见证一切之人: 王来承认全队化 + ATK60% + 回40能量')
    # 来兴致了: 战技后清空兴致
    if u.extra.get('gil_high_spirit'):
        _gil_xingzhi_clear(u, state)
    state.log.append('  王之财宝: 扩散雷伤')


def _gil_ult_cast(state, u):
    """天地乖离·开辟之星: 全体 400% + 10×100%(E6→180%) 弹射; 黄金律耗尽暴伤加成。"""
    state.log.append('  天地乖离·开辟之星: 全体400% + 10段弹射')
    stats = _build_effective_stats(u, state)
    bounce_scale = 180.0 if u.eidolon_rank >= 6 else 100.0
    golden = u.extra.pop('gil_golden_law', 0)
    if golden > 0:
        stats = copy.deepcopy(stats)
        stats.CRIT_DMG += 1.0 * golden
        state.log.append(f'  黄金律耗尽{golden}点: 终结技暴伤+{100 * golden:.0f}%')
    alive = _alive_enemies(state)
    total = _gil_deal(state, u, stats, alive, 400.0, 40.0, 'ultimate')
    for _ in range(10):
        alive_now = _alive_enemies(state)
        if not alive_now:
            break
        total += _gil_deal(state, u, stats, [random.choice(alive_now)], bounce_scale,
                           10.0, 'ultimate')
    u.total_damage_dealt += total
    _gil_xingzhi_gain(u, 2, state, note='(行迹1·终结技)')
    if u.eidolon_rank >= 2:
        _gil_xingzhi_gain(u, 5, state, note='(E2·终结技)')


# ---- 钩子装配 ----

def _hook_skill(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _gil_skill_cast(state, u)
        return True


def _hook_ult(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _gil_ult_cast(state, u)
        return True


def _gil_sp_cost_override(u, state, sp_cost=None, skill_key=None, **kw):
    """来兴致了: 战技不消耗战技点。"""
    if u.char.id != CHAR_ID or skill_key != 'skill' or not u.extra.get('gil_high_spirit'):
        return None
    return 0


def _gil_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE: 连携计数——Gil/Saber 每次技能累计1点; 任意单位攻击后≥8触发合击。"""
    gil = _gil_find(state)
    if gil is None:
        return
    if u.char.id in (CHAR_ID, 'saber') and skill_key in (
            'basic_attack', 'basic_attack_enhanced', 'skill', 'ultimate'):
        gil.extra['gil_joint_count'] = gil.extra.get('gil_joint_count', 0) + 1
        if gil.extra['gil_joint_count'] >= 8:
            gil.extra['gil_joint_count'] = 0
            _gil_joint_attack(state)


def _gil_settle_ally_ult(u, state, skill, skill_key, total_dmg):
    """SETTLE: 队友终结技 → 王来背负(自身终结技伤害+40% 3回合) + 兴致+2(行迹1) +
    回能30%×消耗(常规口径=能量上限) + E6 黄金律+1(≤3)。"""
    if skill_key != 'ultimate' or u.char.id == CHAR_ID:
        return
    gil = _gil_find(state)
    if gil is None:
        return
    lf = _skill_level_factor(gil, 'talent')
    gil.buffs = [b for b in gil.buffs if getattr(b, 'param_id', '') != 'gil_burden']
    gil.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'DMG_BONUS_ALL': 40.0 * lf},
                               remaining_turns=3, param_id='gil_burden',
                               source_name='王来背负'))
    _gil_xingzhi_gain(gil, 2, state, note='(行迹1·队友终结技)')
    _gain_energy(gil, (u.char.max_energy or 0) * 0.30, state=state, apply_regen=False)
    if gil.eidolon_rank >= 6:
        gil.extra['gil_golden_law'] = min(3, gil.extra.get('gil_golden_law', 0) + 1)
    state.log.append('  王来背负: 终结技伤害+40% 3回合, 兴致+2, 回能30%')


def _gil_turn_tick(u, state):
    """天赋: 初始（未来兴致了）自身回合开始时自动施放普攻; AI 本轮不再行动。"""
    if u.char.id != CHAR_ID or u.extra.get('gil_high_spirit'):
        return
    u.extra['gil_auto_basic_done'] = True
    _use_skill(u, state, 'basic_attack')
    state.log.append('  天赋·自动普攻: 漫不经心')


def _init_jierjialameishi(state):
    """每局初始化: E2 开战+5兴致 / E4 ER+20% / E6 全队全抗穿+20% / 行迹3光环。"""
    gil = _gil_find(state)
    if gil is None:
        return
    if gil.eidolon_rank >= 2:
        _gil_xingzhi_gain(gil, 5, state, note='(E2·开战)')
    if gil.eidolon_rank >= 4:
        gil.base_stats.ENERGY_REGEN += 0.20
    if any(t.hook_name == 'jierjialameishi_trace3_kingly' for t in (gil.char.traces or [])):
        for eu in state.units:
            if not eu.is_alive:
                continue
            bonus = 20.0 + min(100.0, max(0.0, (eu.char.max_energy or 0) - 140) * 1.0)
            eu.buffs.append(TimedBuff(source_id=CHAR_ID,
                                      attributes={'ATK_PERCENT': bonus, 'CRIT_DMG': bonus},
                                      remaining_turns=-1, param_id='gil_trace3_aura',
                                      source_name='王霸的竞逐'))
        state.log.append('  王霸的竞逐: 全队ATK/暴伤+20%(+能量上限加成) 光环')
    if gil.eidolon_rank >= 6:
        for eu in state.units:
            if eu.is_alive:
                eu.buffs.append(TimedBuff(source_id=CHAR_ID,
                                          attributes={'RES_PEN_ALL': 20.0},
                                          remaining_turns=-1, param_id='gil_e6_aura',
                                          source_name='挚友淬锻之魂'))
    if gil.extra.pop('gil_tech_pending', None):
        stats = _build_effective_stats(gil, state)
        total = 0.0
        for t in _alive_enemies(state):
            total += _gil_deal(state, gil, stats, [t], 200.0, 20.0, 'skill')
        gil.total_damage_dealt += total
        _gil_xingzhi_gain(gil, 3, state, note='(秘技·天之锁)')
        state.log.append('[秘技] 天之锁: 进战全敌200%雷伤 +3兴致')


def _tech_jierjialameishi(state, u, is_opener):
    if u.char.id != CHAR_ID:
        return
    u.extra['gil_tech_pending'] = True
    state.log.append('[秘技] 天之锁: 下次进战+3兴致')


def jierjialameishi_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """AI: 自动普攻已出→本轮不行动; 来兴致了→战技(免SP); 能量满→终结技; 否则普攻。"""
    if u.extra.pop('gil_auto_basic_done', None):
        return  # 天赋自动普攻即本轮行动
    if u.extra.get('gil_high_spirit'):
        _use_skill(u, state, 'skill')
    elif u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
    else:
        _use_skill(u, state, 'basic_attack')


AI = jierjialameishi_ai
TECHNIQUE = _tech_jierjialameishi
INIT = _init_jierjialameishi
SKILL_HOOKS = [_hook_skill, _hook_ult]
PHASE_HOOKS = {'sp_cost_override': _gil_sp_cost_override}
TURN_TICKS = {'pre': _gil_turn_tick}
SETTLE_HANDLERS = {'settle_self': _gil_settle_self, 'settle_ally_ult': _gil_settle_ally_ult}
