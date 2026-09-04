"""银狼·虚无（M4 批1 迁入；注意与欢愉银狼 yinlang 区分）"""

import copy
import random
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _apply_break_debuff
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy


SILVER_WOLF_DEFECTS = [
    ('atk_down', 0.10, 'silver_wolf_defect_atk', '攻击力降低'),
    ('def_reduction', 0.12, 'silver_wolf_defect_def', '防御力降低'),
    ('spd_down', 0.06, 'silver_wolf_defect_spd', '速度降低'),
]


def _silver_wolf_trace1_active(u):
    return u.extra.get('silver_wolf_trace1', False)


def _silver_wolf_implant_defect(state, u, target):
    """银狼天赋: 攻击后100%基础概率植入1个随机缺陷（3回合; 行迹1+1=4回合）
    缺陷: 攻击力-10%/防御力-12%/速度-6% 三选一（银狼.txt 天赋·等待程序响应…）"""
    if target is None or getattr(target, 'HP', 0) <= 0:
        return False
    from engine.models.enemy import EnemyStatus
    key, val, sid, name = random.choice(SILVER_WOLF_DEFECTS)
    duration = 4 if _silver_wolf_trace1_active(u) else 3
    target.add_status(EnemyStatus(id=sid, name=name, category='debuff',
                                  source='silver_wolf', remaining_turns=duration,
                                  attributes={key: val}))
    state.log.append(f'  银狼缺陷: {target.name or target.id} {name}-{val*100:.0f}% ({duration}回合)')
    return True


def _apply_silver_wolf_weakness(u, state, target):
    """银狼战技: 添加1个队友属性弱点（优先编队第一位角色属性, 抗性-20% 3回合;
    若为原属性弱点不降抗; 仅保留最新1个——status id 固定覆盖）
    v6.3.0b P1-11: 快照记录首次施加前的纯抗性(剔除全抗降低偏移); 同元素刷新保留
    首快照; 换元素先还原旧元素再写新快照; 到期由 _begin_enemy_turn 恢复。"""
    from engine.models.enemy import EnemyStatus
    # 优先编队第一位（position 最小）角色属性
    first = min(state.units, key=lambda x: getattr(x, 'position', 99))
    elem = first.char.element
    existing = next((s for s in target.statuses if s.id == 'silver_wolf_weakness'), None)
    all_res_active = target.has_status(status_id='silver_wolf_all_res_down')
    # 换元素: 先按旧快照还原旧元素（全抗降低仍在时重挂偏移）
    if existing and existing.attributes.get('weakness_element') != elem:
        old_elem = existing.attributes.get('weakness_element')
        old_res = existing.attributes.get('weakness_old_res',
                                          target.get_res(old_elem) + (0.13 if all_res_active else 0.0))
        target.element_res[old_elem] = old_res - (0.13 if all_res_active else 0.0)
        state.log.append(f'  银狼弱点更换: {old_elem}抗性恢复({old_res*100:.0f}%)')
        existing = None
    # 快照: 同元素刷新保留首次快照; 新植入取纯抗性(当前抗性+全抗降低偏移)
    if existing is None:
        old_res = target.get_res(elem) + (0.13 if all_res_active else 0.0)
    else:
        old_res = existing.attributes.get('weakness_old_res',
                                          target.get_res(elem) + (0.13 if all_res_active else 0.0))
    new_res = old_res - 0.20 if old_res > 0 else old_res  # 原属性弱点不降抗
    target.element_res[elem] = (min(new_res, -0.2) if old_res > 0 else new_res) \
        - (0.13 if all_res_active else 0.0)
    # v6.7 弱点植入事件（大丽花行迹3消费）
    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                            element=elem, target=target)
    if existing:
        existing.remaining_turns = 3
        existing.attributes['weakness_element'] = elem
        existing.attributes['weakness_old_res'] = old_res
    else:
        target.add_status(EnemyStatus(id='silver_wolf_weakness', name='弱点植入', category='debuff',
                                      source='silver_wolf', remaining_turns=3,
                                      attributes={'weakness_element': elem,
                                                  'weakness_old_res': old_res}))
    state.log.append(f'  银狼弱点植入: {elem} (抗性{old_res*100:.0f}%→{target.element_res[elem]*100:.0f}%, 3回合)')
    return True


def _apply_silver_wolf_all_res_down(u, state, target):
    """银狼战技: 全属性抗性-13% 2回合（100%基础概率; 到期由 _begin_enemy_turn 恢复）"""
    from engine.models.enemy import EnemyStatus
    existing = next((s for s in target.statuses if s.id == 'silver_wolf_all_res_down'), None)
    if existing:
        existing.remaining_turns = 2
    else:
        target.add_status(EnemyStatus(id='silver_wolf_all_res_down', name='全抗降低', category='debuff',
                                      source='silver_wolf', remaining_turns=2,
                                      attributes={}))
        for elem in list(target.element_res):
            target.element_res[elem] = target.element_res.get(elem, 0) - 0.13
    state.log.append(f'  银狼全抗-13%: {target.name or target.id} (2回合)')
    return True


def _trace_silver_wolf_gen(u, state, **kw):
    """银狼行迹1·生成: 缺陷持续时间+1回合(植入处统一); 敌弱点被击破时100%概率植入随机缺陷

    v6.3.0b P1-8: on_any_weakness_break 广播事件中 u=击破者; 持有者经 ctx char_id 定位
    （v5.2 问题2 约定: 广播事件 u=事件主体, 持有者由 char_id 区分）"""
    owner = next((x for x in state.units if x.char.id == kw.get('char_id')), None)
    if owner is None:
        return
    owner.extra['silver_wolf_trace1'] = True
    t = kw.get('enemy')
    if t is not None and getattr(t, 'HP', 0) > 0:
        _silver_wolf_implant_defect(state, owner, t)
        state.log.append('  行迹·生成: 击破植入缺陷')


def _trace_silver_wolf_inject_start(u, state, **kw):
    """银狼行迹2·注入: 战斗开始时恢复20点能量"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_engine import _gain_energy
    _gain_energy(u, 20.0, state=state)
    state.log.append('  行迹·注入: 战斗开始回20能量')


def _trace_silver_wolf_inject_turn(u, state, **kw):
    """银狼行迹2·注入: 回合开始时恢复5点能量"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_engine import _gain_energy
    _gain_energy(u, 5.0, state=state)


def _trace_silver_wolf_annotate(u, state, **kw):
    """银狼行迹3·旁注: 每10%效果命中→+10%攻击力, 最高+50%"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_engine import _build_effective_stats
    ehr = _build_effective_stats(u, state).EFFECT_HIT_RATE
    bonus = min(0.50, int(ehr * 10) * 0.10)
    if bonus > 0:
        u.base_stats.ATK += u.base_stats._base_ATK * bonus
        state.log.append(f'  行迹·旁注: EHR{ehr*100:.0f}%→攻击力+{bonus*100:.0f}%')


def _tech_silver_wolf(state, u, is_opener):
    from engine.runtime import _tech_enemies
    """银狼: 立即攻击敌人——全敌80%ATK量子伤 + 无视弱点削韧全体, 击破触发量子击破
    （银狼.txt 秘技·强制结束进程, 进战）"""
    from engine.core.combat_engine import calculate_damage, _apply_break_debuff, _commit_enemy_damage
    from engine.models.enemy import EnemyStatus
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 80.0, 'direct', '量子', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
        # 无视弱点削韧（不受弱点门控; 对齐 _no_weakness_pen 语义）
        if e.toughness > 0 and e.max_toughness > 0:
            e.toughness = max(0, e.toughness - 20.0)
            if e.toughness <= 0 and not e.is_broken:
                e.is_broken = True
                bd = calculate_damage(stats, e, 0, 0, 'break', '量子', 80, False)
                _commit_enemy_damage(state, u, e, bd.final_damage)
                u.total_damage_dealt += bd.final_damage
                e.extra['av_delayed'] = 2500.0
                _apply_break_debuff(e, '量子', u, state)
                state.log.append(f'  秘技击破: {e.name or e.id} 击破={bd.final_damage:.0f}(量子)')
    state.log.append('[秘技] 强制结束进程: 全敌80%ATK量子伤 + 无视弱点削韧20')


CHAR_ID = "silver_wolf"
TECHNIQUE = _tech_silver_wolf
# SILVER_WOLF_DEFECTS 为天赋三缺陷表（硬编码清单#2, 随迁）


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _sw_weakness_debuff(u, state, target):
    """DEBUFF_TAKEOVERS['silver_wolf_weakness']: 动态元素弱点植入。"""
    # v6.3.0 银狼战技: 动态元素弱点（队伍第一位角色属性, 非固定元素）
    return _apply_silver_wolf_weakness(u, state, target)


def _sw_all_res_down_debuff(u, state, target):
    """DEBUFF_TAKEOVERS['silver_wolf_all_res_down']: 全属性抗性降低（到期恢复）。"""
    # v6.3.0 银狼战技: 全属性抗性降低（改 element_res, 到期恢复）
    return _apply_silver_wolf_all_res_down(u, state, target)


DEBUFF_TAKEOVERS = {'silver_wolf_weakness': _sw_weakness_debuff,
                    'silver_wolf_all_res_down': _sw_all_res_down_debuff}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _sw_target_stats_mod(u, state, t, t_stats):
    """PHASE target_stats_mod: E6 每负面伤害+20%（最多+100%）（→t_stats|None）。"""
    # v6.3.0 银狼E6: 目标每有1个负面效果伤害+20%, 最多+100%
    if u.eidolon_rank >= 6:
        n = min(getattr(t, 'debuff_count', lambda: 0)(), 5)
        if n > 0:
            t_stats = copy.deepcopy(t_stats)
            t_stats.DMG_BONUS_ALL += 0.20 * n
            return t_stats
    return None


def _sw_attack_aftermath(u, state, skill_key, total_dmg):
    """PHASE attack_aftermath: 攻击后缺陷植入/E1E4终结技结算/弱点转移。"""
    from engine.runtime import _enemy_for_damage
    from engine.core.damage import calculate_damage as _cd
    # v6.3.0 银狼机制（角色技能介绍/银狼.txt）
    if total_dmg > 0:
        # 天赋: 每次施放攻击后 100% 概率给受击目标植入1个随机缺陷
        # v6.3.0b P1-10: 只遍历本次攻击实际命中目标（此前用 alive_enemies 扩大到全部存活敌）
        hit_targets = state.extra.get('last_attack_targets') or []
        for t in hit_targets:
            _silver_wolf_implant_defect(state, u, t)
        # E1/E4: 终结技后每负面回7能量(上限5次) + 每负面附加20%ATK量子伤(每目标上限5次)
        if skill_key == 'ultimate':
            for t in hit_targets:
                if getattr(t, 'HP', 0) <= 0:
                    continue
                n = min(t.debuff_count(), 5)
                if n <= 0:
                    continue
                if u.eidolon_rank >= 1:
                    _gain_energy(u, 7.0 * n, state=state)
                    state.log.append(f'  银狼E1: 每负面回能+{7*n:.0f}')
                if u.eidolon_rank >= 4:
                    stats = _build_effective_stats(u, state)
                    add_d = _cd(stats, _enemy_for_damage(t), stats.ATK, 20.0 * n,
                                'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                                crit_mode='expected')
                    _commit_enemy_damage(state, u, t, add_d.final_damage)
                    u.total_damage_dealt += add_d.final_damage
                    state.log.append(f'  银狼E4: 每负面附加{add_d.final_damage:.0f}(20%×{n})')
        # 天赋: 弱点转移（被消灭目标若带银狼弱点→转移给存活未添加的敌人, 优先精英）
        for t in list(state.enemies):
            if t.HP > 0:
                continue
            st = next((s for s in t.statuses if s.id == 'silver_wolf_weakness'), None)
            if st is None:
                continue
            candidates = [e for e in state.enemies if e.HP > 0
                          and not any(s.id == 'silver_wolf_weakness' for s in e.statuses)]
            if candidates:
                elite = [e for e in candidates if getattr(e, 'is_elite', False)]
                to = (elite or candidates)[0]
                to.add_status(copy.deepcopy(st))
                state.log.append(f'  银狼弱点转移: {t.name or t.id}→{to.name or to.id}')
    return None


def _sw_defect_implant_obs(u, state, total_dmg):
    """OBSERVER defect_implant: 我方攻击时银狼E2 给受击目标植入随机缺陷。"""
    # E2: 我方目标攻击时, 银狼100%概率给受击目标植入随机缺陷
    if u.char.id == 'silver_wolf':
        return None
    sw = next((x for x in state.units if x.char.id == 'silver_wolf'
               and x.is_alive and x.eidolon_rank >= 2), None)
    if sw and total_dmg > 0:
        # v6.3.0b P1-10: 同样只遍历实际命中目标
        for t in (state.extra.get('last_attack_targets') or []):
            if getattr(t, 'HP', 0) > 0:
                _silver_wolf_implant_defect(state, sw, t)
    return None


PHASE_HOOKS['target_stats_mod'] = _sw_target_stats_mod
PHASE_HOOKS['attack_aftermath'] = _sw_attack_aftermath
OBSERVER_HOOKS = {'defect_implant': _sw_defect_implant_obs}
