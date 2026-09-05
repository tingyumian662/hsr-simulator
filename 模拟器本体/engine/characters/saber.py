"""Saber——Fate 联动·毁灭·风（v7.21.0 录入, 角色技能介绍/毁灭/saber.txt）

核心机制:
- 炉心共鸣（u.extra['saber_resonance'], 文本未设上限）: 强化普攻+2 / 战技未触发分支+3 /
  天赋（任意我方终结技, 含自身）+3 / E1（普攻/战技后+1）/ 秘技+2
- 战技条件分支: 持有共鸣且耗尽可回满能量 → 主/邻两段倍率每点+14%（等级因子口径, E2 每点
  再+7）, 攻击后耗尽全部共鸣、每点固定回 8 能（apply_regen=False）; 否则 +3 共鸣
- 湖之祝福（行迹2）: 溢出能量银行 120（E6→200, 引擎 energy_overflow_bank 观察者相位）;
  终结技后清空回能; 开战能量 <60% 补至 60%
- 强化普攻替换: 终结技后 key_rewrite basic_attack→解放的金色王权（一次性, 仅能施放强化）
- 行迹1 魔力放出: 开战/强化普攻施放时获得; 战技触发耗尽分支时消费 → +1 战技点+立即行动
- 行迹3: 战技 → 暴伤+50% 2回合; 每获得 1 点共鸣 → 暴伤+4% 最多 8 层（永久 base_stats）
- E2: 每获得 1 点共鸣 → 无视防御 1% 最多 15 层; E4: 风穿+8% + 终结技后 4%×3 层;
  E6: 终结技风穿+20% / 银行 200 / 首次终结技回 300 能量（每再 3 次可再触发）
- 吉尔伽美什连携「本王允许你进攻」: 由 jierjialameishi 模块计数触发并调用
  _saber_joint_reward(state)——Saber +120 能量、下次终结技伤害 120%
"""
import copy
import random

from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage, _select_targets
from engine.core.damage import calculate_damage
from engine.core.combat_engine import (
    _build_effective_stats, _commit_enemy_damage, _effective_spd,
    _flat_toughness_with_break, _gain_energy, _gain_skill_points,
    _skill_level_factor, _use_skill,
)

CHAR_ID = "saber"
ELEMENT = "风"


def _saber_find(state):
    return next((x for x in state.units if x.char.id == CHAR_ID and x.is_alive), None)


def _saber_res_gain(u, n, state=None):
    """获得 n 点炉心共鸣 + 行迹3/E2 的获得联动（增量法, 各自上限）。"""
    if n <= 0:
        return
    u.extra['saber_resonance'] = u.extra.get('saber_resonance', 0) + n
    total = u.extra['saber_resonance_gained'] = u.extra.get('saber_resonance_gained', 0) + n
    # 行迹3: 每获得1点 → 暴伤+4% 最多8层
    if any(t.hook_name == 'saber_trace3_crown' for t in (u.char.traces or [])):
        new_stacks = min(8, total)
        delta = new_stacks - u.extra.get('saber_t3_cd_stacks', 0)
        if delta > 0:
            u.extra['saber_t3_cd_stacks'] = new_stacks
            u.base_stats.CRIT_DMG += 0.04 * delta
    # E2: 每获得1点 → 无视防御1% 最多15层
    if u.eidolon_rank >= 2:
        new_stacks = min(15, total)
        delta = new_stacks - u.extra.get('saber_e2_defpen_stacks', 0)
        if delta > 0:
            u.extra['saber_e2_defpen_stacks'] = new_stacks
            u.base_stats.DEF_PEN += 0.01 * delta


def _saber_bank_cap(u):
    return 200.0 if u.eidolon_rank >= 6 else 120.0


def _saber_energy_overflow(u, state, overflow=0.0, **kw):
    """OBSERVER energy_overflow_bank: 湖之祝福——溢出能量入银行（行迹2）。"""
    if u.char.id != CHAR_ID or overflow <= 0:
        return
    if not any(t.hook_name == 'saber_trace2_lake' for t in (u.char.traces or [])):
        return
    cap = _saber_bank_cap(u)
    u.extra['saber_bank'] = min(cap, u.extra.get('saber_bank', 0.0) + overflow)


def _saber_joint_reward(state):
    """吉尔伽美什连携「本王允许你进攻」命中后的 Saber 侧收益（供其模块调用）。"""
    saber = _saber_find(state)
    if saber is None:
        return
    _gain_energy(saber, 120.0, state=state, apply_regen=False)
    saber.extra['saber_next_ult_boost'] = 1.20
    state.log.append('  连携·Saber: +120能量, 下次终结技伤害120%')


# ---- 伤害工具 ----

def _saber_deal(state, u, stats, targets, scale, toughness, skill_type):
    """对目标列表结算一段风伤 + 削韧, 返回总伤害。"""
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


def _alive_enemies(state):
    return [e for e in state.enemies if getattr(e, 'HP', 0.0) > 0]


# ---- 技能实现（SKILL_HOOKS 返回 True=全接管; SP/能量支付由引擎 S2 通用处理） ----

def _saber_skill_cast(state, u):
    """风王铁槌: 扩散 150%/75%; 共振耗尽分支倍率提升 + 耗尽回能 / 否则 +3 共振。"""
    stats = _build_effective_stats(u, state)
    lf = _skill_level_factor(u, 'skill')
    res = u.extra.get('saber_resonance', 0)
    boosted = res > 0 and (u.current_energy + res * 8.0) >= u.char.max_energy
    per_point = 14.0 + (7.0 if u.eidolon_rank >= 2 else 0.0)  # 项目主裁决: 固定值, 主/副同额
    main, adj = 150.0 * lf, 75.0 * lf
    if boosted:
        main += res * per_point
        adj += res * per_point
    # 行迹3: 战技 → 暴伤+50% 2回合
    if any(t.hook_name == 'saber_trace3_crown' for t in (u.char.traces or [])):
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'saber_t3_cd']
        u.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'CRIT_DMG': 50.0},
                                 remaining_turns=2, param_id='saber_t3_cd',
                                 source_name='星之冠冕'))
    alive = _alive_enemies(state)
    tsc = alive[0] if alive else None
    main_list = [tsc] if tsc is not None else []
    adj_list = [e for e in alive if e is not tsc][:2]
    total = _saber_deal(state, u, stats, main_list, main, 20.0, 'skill')
    total += _saber_deal(state, u, stats, adj_list, adj, 10.0, 'skill')
    u.total_damage_dealt += total
    if boosted:
        u.extra.pop('saber_resonance', None)
        _gain_energy(u, res * 8.0, state=state, apply_regen=False)
        state.log.append(f'  风王铁槌: 耗尽{res}点炉心共鸣回能{res * 8:.0f}, 倍率+{res * per_point:.0f}%')
        # 行迹1 魔力放出: 触发耗尽分支时消费 → +1战技点 + 立即行动
        if u.extra.pop('saber_magic_release', None):
            _gain_skill_points(state, 1)
            u._pending_action_advance = AV_PER_TURN / max(_effective_spd(u, state), 1.0)
            state.log.append('  魔力放出: +1战技点, Saber立即行动')
    else:
        _saber_res_gain(u, 3)
        state.log.append(f'  风王铁槌: +3炉心共鸣({u.extra.get("saber_resonance", 0)})')
    if u.eidolon_rank >= 1:
        _saber_res_gain(u, 1)


def _saber_basic_enhanced_cast(state, u):
    """解放的金色王权: 全体 150%; 敌方数=2→额外全体150% / =1→220%; +2 共振 + 魔力放出。"""
    stats = _build_effective_stats(u, state)
    lf = _skill_level_factor(u, 'basic_attack')
    alive = _alive_enemies(state)
    total = _saber_deal(state, u, stats, alive, 150.0 * lf, 20.0, 'basic_attack')
    n = len(alive)
    if n == 2:
        total += _saber_deal(state, u, stats, alive, 150.0 * lf, 0.0, 'basic_attack')
    elif n == 1:
        total += _saber_deal(state, u, stats, alive, 220.0 * lf, 0.0, 'basic_attack')
    u.total_damage_dealt += total
    _saber_res_gain(u, 2)
    if any(t.hook_name == 'saber_trace1_knight' for t in (u.char.traces or [])):
        u.extra['saber_magic_release'] = True
    if u.eidolon_rank >= 1:
        _saber_res_gain(u, 1)
    state.log.append(f'  解放的金色王权: 全体伤害, +2炉心共鸣({u.extra.get("saber_resonance", 0)})')


def _saber_ult_cast(state, u):
    """誓约胜利之剑: 全体 280% + 10×110% 随机弹射; 终结技后强化普攻/E4层/E6回能/银行清空。"""
    state.log.append('  誓约胜利之剑: 全体280% + 10段弹射110%')
    stats = _build_effective_stats(u, state)
    if u.eidolon_rank >= 1 or u.eidolon_rank >= 6 or u.extra.get('saber_next_ult_boost'):
        stats = copy.deepcopy(stats)
    if u.eidolon_rank >= 1:
        stats.DMG_BONUS_ALL += 0.60  # E1: 终结技伤害+60%
    if u.eidolon_rank >= 6:
        stats.RES_PEN[ELEMENT] = stats.RES_PEN.get(ELEMENT, 0.0) + 0.20  # E6: 终结技风穿+20%
    if u.extra.pop('saber_next_ult_boost', None):  # 吉尔伽美什连携: 本次终结技120%
        stats.DMG_BONUS_ALL += 0.20
    alive = _alive_enemies(state)
    total = _saber_deal(state, u, stats, alive, 280.0, 40.0, 'ultimate')
    for _ in range(10):
        alive_now = _alive_enemies(state)
        if not alive_now:
            break
        total += _saber_deal(state, u, stats, [random.choice(alive_now)], 110.0, 10.0,
                             'ultimate')
    u.total_damage_dealt += total
    # 终结技后: 下次普攻替换为强化普攻
    u.extra['saber_enhanced_basic_ready'] = True
    # E4: 施放终结技后 风穿+4%（最多3层）
    if u.eidolon_rank >= 4:
        stacks = min(3, u.extra.get('saber_e4_ult_stacks', 0) + 1)
        delta = stacks - u.extra.get('saber_e4_ult_stacks', 0)
        if delta > 0:
            u.extra['saber_e4_ult_stacks'] = stacks
            u.base_stats.RES_PEN[ELEMENT] = u.base_stats.RES_PEN.get(ELEMENT, 0.0) + 0.04 * delta
    # E6: 首次终结技回300能量; 每再施放3次可再触发（第1/4/7…次）
    if u.eidolon_rank >= 6:
        u.extra['saber_ult_count'] = u.extra.get('saber_ult_count', 0) + 1
        cnt = u.extra['saber_ult_count']
        if cnt == 1 or (cnt - 1) % 3 == 0:
            _gain_energy(u, 300.0, state=state, apply_regen=False)
            state.log.append(f'  E6·守护命运长夜: 终结技#{cnt}后回300能量')
    # 行迹2: 终结技后清空溢出银行并回能（回能若再溢出, 由观察者存回银行=实机可积攒语义）
    bank = u.extra.pop('saber_bank', 0.0)
    if bank > 0:
        _gain_energy(u, bank, state=state, apply_regen=False)
        state.log.append(f'  湖之祝福: 银行清空回能{bank:.0f}')


# ---- 钩子装配 ----

def _hook_skill(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "skill":
        _saber_skill_cast(state, u)
        return True


def _hook_enhanced(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "basic_attack_enhanced":
        _saber_basic_enhanced_cast(state, u)
        return True


def _hook_ult(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _saber_ult_cast(state, u)
        return True


def _saber_key_rewrite(u, state, skill_key=None, **kw):
    """终结技后下次普攻 → 解放的金色王权（一次性; 仅能施放强化普攻）。"""
    if u.char.id != CHAR_ID or skill_key != 'basic_attack':
        return None
    if u.extra.pop('saber_enhanced_basic_ready', None):
        return 'basic_attack_enhanced'
    return None


def _saber_post_effects(u, state, skill_key=None, **kw):
    """E1: 普攻后 +1 炉心共鸣（战技侧在手写实现内处理）。"""
    if u.char.id != CHAR_ID or u.eidolon_rank < 1 or skill_key != 'basic_attack':
        return
    _saber_res_gain(u, 1)


def _saber_settle_ally_ult(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_ally_ult: 天赋——任意我方终结技 → 伤害+60%(Lv10) 2回合 + 3 共振。"""
    if skill_key != 'ultimate':
        return
    saber = _saber_find(state)
    if saber is None:
        return
    scale = 60.0 * _skill_level_factor(saber, 'talent')
    saber.buffs = [b for b in saber.buffs if getattr(b, 'param_id', '') != 'saber_talent_dmg']
    saber.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'DMG_BONUS_ALL': scale},
                                 remaining_turns=2, param_id='saber_talent_dmg',
                                 source_name='龙之炉心'))
    _saber_res_gain(saber, 3)
    state.log.append(f'  龙之炉心: 终结技联动 伤害+{scale:.0f}% 2回合, +3炉心共鸣')


def _init_saber(state):
    """每局初始化: 天赋+1共振 / 行迹1 CR+20%+魔力放出 / 行迹2 能量≥60% / E4风穿+8% / 秘技。"""
    saber = _saber_find(state)
    if saber is None:
        return
    _saber_res_gain(saber, 1)  # 天赋: 进入战斗获得1点
    if any(t.hook_name == 'saber_trace1_knight' for t in (saber.char.traces or [])):
        saber.base_stats.CRIT_RATE += 0.20
        saber.extra['saber_magic_release'] = True
    if any(t.hook_name == 'saber_trace2_lake' for t in (saber.char.traces or [])):
        floor = (saber.char.max_energy or 0) * 0.60
        if saber.current_energy < floor:
            saber.current_energy = floor
    if saber.eidolon_rank >= 4:
        saber.base_stats.RES_PEN[ELEMENT] = saber.base_stats.RES_PEN.get(ELEMENT, 0.0) + 0.08
    if saber.extra.pop('saber_tech_pending', None):
        saber.buffs.append(TimedBuff(source_id=CHAR_ID, attributes={'ATK_PERCENT': 35.0},
                                     remaining_turns=2, param_id='saber_technique',
                                     source_name='骑士王的登场'))
        _saber_res_gain(saber, 2)
        state.log.append('[秘技] 骑士王的登场: 攻击力+35% 2回合, +2炉心共鸣')


def _tech_saber(state, u, is_opener):
    """秘技: 下一次战斗开始时生效（非进战）——挂 pending 由 INIT 兑现。"""
    if u.char.id != CHAR_ID:
        return
    u.extra['saber_tech_pending'] = True
    state.log.append('[秘技] 骑士王的登场: 下次战斗开始时攻击力+35% 2回合 +2炉心共鸣')


def saber_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """Saber AI: 强化普攻就绪→仅普攻; 能量满→终结技; 有SP→战技; 否则普攻。"""
    if u.extra.get('saber_enhanced_basic_ready'):
        _use_skill(u, state, 'basic_attack')  # key_rewrite 翻转为强化普攻
    elif u.current_energy >= u.char.max_energy:
        _use_skill(u, state, 'ultimate')
    elif state.skill_points > 0:
        _use_skill(u, state, 'skill')
    else:
        _use_skill(u, state, 'basic_attack')


AI = saber_ai
TECHNIQUE = _tech_saber
INIT = _init_saber
SKILL_HOOKS = [_hook_skill, _hook_enhanced, _hook_ult]
PHASE_HOOKS = {'key_rewrite': _saber_key_rewrite, 'post_effects': _saber_post_effects}
OBSERVER_HOOKS = {'energy_overflow_bank': _saber_energy_overflow}
SETTLE_HANDLERS = {'settle_ally_ult': _saber_settle_ally_ult}
