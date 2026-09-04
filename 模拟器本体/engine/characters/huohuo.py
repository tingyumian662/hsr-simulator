"""藿藿（试点 M3；普攻+10回能闭包经审核确认为未挂载死路径, 已删）"""

import copy
import random
from engine.runtime import TimedBuff
from engine.core.combat_engine import _gain_energy, _use_skill
from engine.models.enemy import EnemyStatus


def _huohuo_ruming_gain(state, u, turns):
    """v6.10.6 B1: 藿藿获得禳命——自身状态, 净化次数刷新6次（TXT 藿藿.txt:41-44）"""
    u.extra['huohuo_ruming_turns'] = turns
    u.extra['huohuo_ruming_cleanse'] = 6
    state.log.append(f'  禳命: 藿藿获得禳命({turns}回合, 净化6次)')


def _huohuo_ruming_tick(state, u):
    """v6.10.6 B1: 藿藿自身常规回合开始——禳命持续回合-1（TXT: 藿藿每回合开始减1）"""
    if u.char.id != 'huohuo':
        return
    t = u.extra.get('huohuo_ruming_turns', 0)
    if t > 0:
        t -= 1
        if t <= 0:
            u.extra.pop('huohuo_ruming_turns', None)
            u.extra.pop('huohuo_ruming_cleanse', None)
            state.log.append('  禳命到期')
        else:
            u.extra['huohuo_ruming_turns'] = t


def _huohuo_ruming_heal_all(state, trigger_unit):
    """v6.10.6 B2/B3/B5: 藿藿持有禳命时, 触发单位回 4.5%藿藿生命上限+120,
    随后全队每个 HP≤50% 目标再回同量; 净化1负面(6次); 行迹3仅禳命治疗回1能量"""
    hh = next((x for x in state.units
               if x.char.id == 'huohuo' and x.is_alive), None)
    if hh is None or hh.extra.get('huohuo_ruming_turns', 0) <= 0:
        return
    heal = hh.max_hp * 0.045 + 120.0
    low = [x for x in state.units
           if x.is_alive and x is not trigger_unit
           and x.current_hp <= x.max_hp * 0.5]
    targets = [trigger_unit] + low if trigger_unit is not None and trigger_unit.is_alive else low
    seen = set()
    for t in targets:
        if id(t) in seen:
            continue
        seen.add(id(t))
        before = t.current_hp
        t.current_hp = min(t.max_hp, t.current_hp + heal)
        amount = t.current_hp - before
        if amount <= 0:
            continue
        # B3: 净化——解除目标1个负面（单次禳命6次, 再获禳命刷新）
        if hh.extra.get('huohuo_ruming_cleanse', 0) > 0:
            neg = [s for s in t.statuses
                   if getattr(s, 'category', '') in ('debuff', 'control')]
            if neg:
                t.statuses.remove(neg[0])
                hh.extra['huohuo_ruming_cleanse'] -= 1
                state.log.append(f'  禳命净化: {t.char.name}解除1负面(余{hh.extra["huohuo_ruming_cleanse"]}次)')
        # on_heal 钩子（藿藿E6/收容的暗潮等统一入口）
        state.hooks.trigger_all("on_heal", u=hh, state=state,
                                healer=hh, targets=[t], heal_amt=amount)
        # B5: 行迹3·怯惧应激——仅禳命治疗触发回1能量
        if any(getattr(tr, 'hook_name', '') == 'huohuo_energy_cycle'
               for tr in (hh.char.traces or [])):
            _gain_energy(hh, 1.0, state=state)
        state.log.append(f'  禳命治疗: {t.char.name}+{amount:.0f}')


def _trace_huohuo_control_resist(u, state, **kw):
    """藿藿行迹「控抗精通」: 抵抗控制+35%
    （v5.0 P4 激活: EFFECT_RES 参与 _apply_player_status 命中检定; 终结技队友ATK+24%在引擎内联）"""
    if u.char.id != 'huohuo':
        return
    u.base_stats.EFFECT_RES += 0.35
    state.log.append('  行迹·控抗精通: 效果抵抗+35%')


def _huohuo_ruming_gain_local(state, u, turns):
    """本地包装: 从 combat_engine 延迟导入（防循环导入）"""
    _huohuo_ruming_gain(state, u, turns)


def _huohuo_e2_fatal_check(state):
    """藿藿E2·镇尾锁灵: 持禳命时我方受致命攻击→不死亡+回50%生命+禳命-1(单场2次)。
    v6.10.6 A3: 补 eidolon>=2 + 藿藿当前持有禳命门控（此前无禳命也触发且负HP存活）"""
    huohuo = next((x for x in state.units
                   if x.char.id == 'huohuo' and x.is_alive), None)
    if huohuo is None or huohuo.eidolon_rank < 2:
        return False
    if huohuo.extra.get('huohuo_ruming_turns', 0) <= 0:
        return False
    charges = state.extra.get('huohuo_e2_charges', 0)
    if charges <= 0:
        return False
    state.extra['huohuo_e2_charges'] = charges - 1
    # v6.10.6 B: E2 触发使禳命持续回合-1（TXT 藿藿.txt:7）
    ruming = huohuo.extra.get('huohuo_ruming_turns', 0) - 1
    if ruming <= 0:
        huohuo.extra.pop('huohuo_ruming_turns', None)
        huohuo.extra.pop('huohuo_ruming_cleanse', None)
    else:
        huohuo.extra['huohuo_ruming_turns'] = ruming
    for eu in state.units:
        if eu.is_alive:
            eu.current_hp = min(eu.max_hp, max(0.0, eu.current_hp) + eu.max_hp * 0.50)
    state.log.append(f'  藿藿E2·镇尾锁灵: 致命保护触发, 回50%生命 ({charges-1}/2次)')
    return True


def _eid_huohuo_e2(u, state, **kw):
    """藿藿E2·镇尾锁灵: 初始化单场2次次数（保护逻辑见 _huohuo_e2_fatal_check, 等受击闭环）"""
    if u.char.id != 'huohuo':
        return
    state.extra['huohuo_e2_charges'] = 2
    state.log.append('  藿藿E2: 致命保护就位(单场2次, 待受击闭环)')


def _eid_huohuo_e1(u, state, **kw):
    """藿藿E1: 全队SPD+12% + 自身治疗量+20%（禳命+1回合无禳命计时系统, 占位注释）"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.SPD += eu.base_stats._base_SPD * 0.12
    u.base_stats.HEAL_BONUS += 0.20
    state.log.append('  藿藿E1: 全队SPD+12%, 治疗+20%')


def _eid_huohuo_e6(u, state, healer=None, targets=None, heal_amt=0, **kw):
    """藿藿E6·同休共戚: 藿藿提供治疗时→被治疗目标伤害+50% 2回合（刷新语义）"""
    from engine.runtime import TimedBuff
    if not targets or heal_amt <= 0:
        return
    if getattr(getattr(healer, 'char', None), 'id', None) != 'huohuo':
        return
    for t in targets:
        if not hasattr(t, 'buffs'):
            continue
        refreshed = False
        for b in t.buffs:
            if getattr(b, 'source_name', '') == '藿藿E6·同休共戚':
                b.remaining_turns = 2
                refreshed = True
                break
        if not refreshed:
            t.buffs.append(TimedBuff(source_id='huohuo_e6', attributes={"DMG_BONUS_ALL": 50.0},
                                     remaining_turns=2, source_name='藿藿E6·同休共戚'))
    state.log.append('  藿藿E6: 治疗→目标伤害+50% 2回合')


def _hh_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        for eu in state.units:
            if eu.is_alive and eu.char.id != "huohuo":
                eu.current_energy = min(eu.char.max_energy,
                                        eu.current_energy + eu.char.max_energy * 0.20)
        state.log.append('  藿藿终结技: 队友回能20%')
    elif state.skill_points >= 2 and any(
        x.current_hp / x.max_hp < 0.5 for x in state.units
        if x.is_alive and x.char.id != "huohuo"
    ):
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _tech_huohuo(state, u, is_opener):
    """藿藿: 与魄散敌人进战→100%基础概率全敌攻击力-25% 2回合（藿藿.txt 秘技·凶煞·劾压鬼物, 非进战）"""
    from engine.runtime import _tech_enemies
    from engine.core.combat_engine import _roll_effect_hit
    from engine.models.enemy import EnemyStatus
    for e in _tech_enemies(state):
        if not _roll_effect_hit(u, state, e, '魄散降攻', base_chance=1.0):
            continue
        e.add_status(EnemyStatus(id='huohuo_tech_atk_down', name='攻击力降低', category='debuff',
                                 source='huohuo', remaining_turns=2,
                                 attributes={'atk_down': 0.25}))  # v6.3.0b P1-7: 小写消费键
    state.log.append('[秘技] 凶煞·劾压鬼物: 全敌攻击力-25% 2回合')


CHAR_ID = "huohuo"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
AI = _hh_ai
TECHNIQUE = _tech_huohuo


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _huohuo_turn_tick(u, state):
    # v6.10.6 B2: 藿藿禳命——藿藿自身状态（X轴不tick, 仅常规回合）:
    # 藿藿回合开始先递减; 藿藿持有时我方目标回合开始回血
    if u.char.id == 'huohuo':
        _huohuo_ruming_tick(state, u)


TURN_TICKS = {'post_control': _huohuo_turn_tick}


PHASE_HOOKS = {}


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _huohuo_heal_target_mod(u, state, t, heal_amt):
    """PHASE heal_target_mod: E4·坐卧不离——目标低血治疗加成(线性插值, 最多+80%)。"""
    # 藿藿E4·坐卧不离: 目标低血治疗加成(线性插值, 最多+80%)
    if u.eidolon_rank >= 4:
        miss = 1.0 - t.current_hp / t.max_hp
        return heal_amt * (1.0 + 0.80 * miss)
    return None


def _huohuo_post_heal(u, state, skill_key):
    """PHASE post_heal: 战技→自身获得禳命3回合（E1延长1回合）。"""
    # v6.10.6 B1: 藿藿战技→藿藿自身获得禳命3回合（E1延长1回合; 此前错误挂在受疗者身上2回合）
    if skill_key == 'skill':
        _huohuo_ruming_gain(state, u, 3 + (1 if u.eidolon_rank >= 1 else 0))
    return None


def _huohuo_post_effects(u, state, skill_key):
    """PHASE post_effects: 终结技·遣神役鬼——队友回20%能量上限+ATK buff。"""
    # 藿藿终结技·尾巴·遣神役鬼: 队友回20%能量上限 + ATK buff 2回合
    # (行迹·控抗精通: 能量上限≥160的队友额外ATK+24% → 40→64)
    if skill_key != 'ultimate':
        return None
    for eu in state.units:
        if eu is u or not eu.is_alive:
            continue
        _gain_energy(eu, 0.20, state=state, percent=True)  # v5.7: 统一入口(迷迷充能bank)
        atk_val = 64.0 if (eu.char.max_energy or 0) >= 160 else 40.0
        eu.buffs.append(TimedBuff(source_id='huohuo', attributes={'ATK_PERCENT': atk_val},
                                  remaining_turns=2, source_name='藿藿终结技',
                                  param_id='huohuo_ult_atk'))
    state.log.append(f'  藿藿终结技: 队友回20%能量上限 + ATK+40/64% 2回合')
    return None


PHASE_HOOKS['heal_target_mod'] = _huohuo_heal_target_mod
PHASE_HOOKS['post_heal'] = _huohuo_post_heal
PHASE_HOOKS['post_effects'] = _huohuo_post_effects
