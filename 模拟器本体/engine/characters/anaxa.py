"""anaxa（M4 收官批迁入）"""

import copy
import random
from engine.runtime import _tech_enemies
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import WEAKNESS_ELEMENTS
from engine.core.combat_engine import _enemy_weakness_elements
from engine.core.combat_engine import _gain_energy


def _anaxa_apply_entry_effects(state, u):
    """那刻夏E2：每个敌人进入波次时添加1个弱点并全抗降低20%。"""
    if u is None or u.char.id != 'anaxa' or u.eidolon_rank < 2:
        return
    from engine.models.enemy import EnemyStatus
    for enemy in state.enemies:
        if getattr(enemy, 'HP', 0) <= 0 or enemy.extra.get('anaxa_e2_applied'):
            continue
        _anaxa_add_weakness(state, u, enemy)
        enemy.add_status(EnemyStatus(
            id='anaxa_e2_res_down', name='那刻夏E2', category='debuff', source='anaxa',
            remaining_turns=-1, attributes={'res_down': 0.20},
        ))
        enemy.extra['anaxa_e2_applied'] = True
    if state.enemies:
        state.log.append('  那刻夏E2: 敌入场添加弱点+全抗-20%')


def _anaxa_add_weakness(state, u, target):
    """天赋: 每击中+1随机弱点（3回合, 优先未有）"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    # 自然弱点参与“质性揭露”和行迹3计数，但天赋的“尚未拥有”标记
    # 仍以那刻夏自身已添加的弱点状态为准，避免天然全弱点目标把每次命中
    # 都刷新到同一个随机状态。
    existing = {status.attributes.get('weakness_element')
                for status in target.statuses if status.id.startswith('anaxa_weak')}
    pool = [el for el in WEAKNESS_ELEMENTS if el not in existing] or WEAKNESS_ELEMENTS
    import random
    elem = random.choice(pool)
    existing_status = next((s for s in target.statuses
                            if s.id == f'anaxa_weak_{elem}'), None)
    old_res = (existing_status.attributes.get('weakness_old_res', target.get_res(elem))
               if existing_status else target.get_res(elem))
    current_res = target.get_res(elem)
    target.element_res[elem] = min(current_res, -0.2) if current_res > 0 else current_res
    target.add_status(EnemyStatus(
        id=f'anaxa_weak_{elem}', name='弱点', category='debuff',
        source='anaxa', remaining_turns=3,
        attributes={'weakness_element': elem, 'weakness_old_res': old_res}))
    state.log.append(f'  那刻夏弱点: {elem} (+1)')
    # v6.7 弱点植入事件（大丽花行迹3消费）
    state.hooks.trigger_all("on_weakness_implant", u=u, state=state,
                            element=elem, target=target)


def _anaxa_reveal_check(state, u, target):
    """≥5不同弱点→【质性揭露】"""
    weaks = _enemy_weakness_elements(target)
    if len(weaks) >= 5:
        target.extra['anaxa_revealed'] = True
        state.log.append(f'  【质性揭露】: {target.name or target.id} ({len(weaks)}弱点)')


def _trace_anaxa_trace2(u, state, **kw):
    """那刻夏行迹2: 智识1名CD+140% 或 2名全队伤害+50%"""
    if u.char.id != 'anaxa':
        return
    from engine.core.character_utils import count_remembrance
    n = sum(1 for x in state.units if getattr(x.char, 'path', '') == '智识')
    if n >= 2 or u.eidolon_rank >= 6:
        for eu in state.units:
            if eu.is_alive:
                eu.base_stats.DMG_BONUS_ALL += 0.50
        state.log.append('  行迹·留白: 智识×2 全队伤害+50%')
    if n < 2 or u.eidolon_rank >= 6:
        u.base_stats.CRIT_DMG += 1.40
        state.log.append('  行迹·留白: 智识×1 暴伤+140%')


def _trace_anaxa_trace3(u, state, **kw):
    """那刻夏行迹3: 每弱点无视4%防御上限28%"""
    if u.char.id != 'anaxa':
        return
    u.extra['anaxa_trace3'] = True


def _trace_anaxa_basic_energy(u, state, **kw):
    """那刻夏行迹1: 普攻额外回10能量。"""
    if u.char.id == 'anaxa':
        from engine.core.combat_engine import _gain_energy
        _gain_energy(u, 10.0, state=state)


def _trace_anaxa_turn_energy(u, state, **kw):
    """那刻夏行迹1: 回合开始且没有质性揭露目标时回30能量。"""
    if u.char.id != 'anaxa':
        return
    if not any(getattr(e, 'extra', {}).get('anaxa_revealed')
               for e in state.enemies if getattr(e, 'HP', 0) > 0):
        from engine.core.combat_engine import _gain_energy
        _gain_energy(u, 30.0, state=state)


def _eid_anaxa_e2(u, state, **kw):
    """那刻夏E2: 敌人进入每个波次时添加弱点并降低全抗20%。"""
    if u.char.id != 'anaxa':
        return

    _anaxa_apply_entry_effects(state, u)


def _tech_anaxa(state, u, is_opener):
    """那刻夏: 全敌添加攻击者属性弱点3回合（非进战）"""

    for e in _tech_enemies(state):
        _anaxa_add_weakness(state, u, e)
    state.log.append('[秘技] 瞳扉之彩: 全敌+1弱点')


CHAR_ID = "anaxa"
TECHNIQUE = _tech_anaxa


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _anaxa_turn_tick(u, state):
    # v6.9.1: 状态机显式派发到角色常规回合边界（JSON 无 tick hook_name, 注册表钩子不会触发）
    if u.char.id == 'anaxa' and any(
            getattr(t, 'hook_name', '') == 'anaxa_trace1'
            for t in (u.char.traces or [])):
        _trace_anaxa_turn_energy(u, state)


TURN_TICKS = {'pre': _anaxa_turn_tick}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _anaxa_skill_adjust_pre(u, state, skill, skill_key):
    """PHASE skill_adjust_pre: E4 战技 ATK 叠层刷新（副作用, 返回 None）。"""
    from engine.runtime import TimedBuff
    if skill_key == 'skill' and u.eidolon_rank >= 4:
        stacks = min(2, u.extra.get('anaxa_e4_stacks', 0) + 1)
        u.extra['anaxa_e4_stacks'] = stacks
        u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'anaxa_e4_atk']
        u.buffs.append(TimedBuff(source_id='anaxa', attributes={'ATK_PERCENT': 30.0 * stacks},
                                 remaining_turns=2, param_id='anaxa_e4_atk',
                                 source_name='那刻夏E4'))
    return None


PHASE_HOOKS = {'skill_adjust_pre': _anaxa_skill_adjust_pre}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _anaxa_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: E1 战技 debuff/逐段弱点/理性之诗/终结技升华。"""
    from engine.core.combat_engine import WEAKNESS_ELEMENTS, _build_effective_stats, _commit_enemy_damage, _gain_skill_points
    from engine.core.damage import calculate_damage
    from engine.runtime import _enemy_for_damage
    if u.char.id != 'anaxa':
        return None
    if skill_key == 'skill' and u.eidolon_rank >= 1:
        if not u.extra.get('anaxa_e1_first_skill_used'):
            u.extra['anaxa_e1_first_skill_used'] = True
            _gain_skill_points(state, 1)
        for target in state.extra.get('last_attack_targets', []):
            if target.HP > 0:
                target.add_status(EnemyStatus(
                    id='anaxa_e1_def_down', name='那刻夏E1', category='debuff',
                    source='anaxa', remaining_turns=2,
                    attributes={'def_reduction': 0.16},
                ))
    if total_dmg > 0:
        # v6.8.1: 每击中1次→为目标添加1个弱点（txt:53, 逐段含弹射重复段;
        # 此前对所有存活敌各加1个）
        # 逐段: 去重目标 + 弹射重复段（last_hit_segments, v6.8.2 防缓存清空丢段信息）
        # v6.8.3: 优先逐段命中（弹射含重复段）, 缺失回退去重目标集
        hit = list(state.extra.get('last_hit_segments') or state.extra.get('last_attack_targets') or [])
        for t in hit:
            if t is not None and t.HP > 0:
                _anaxa_add_weakness(state, u, t)
                _anaxa_reveal_check(state, u, t)
    # v6.6c P2: 献予「理性」——战技伤害次数+3（单次生效, 用后消费）
    if skill_key == 'skill' and u.extra.get('poem_lixing'):
        stats = _build_effective_stats(u, state)
        sk = u.char.skills.get('skill')
        if sk and sk.multipliers:
            m = sk.multipliers[0]
            sc = stats.ATK if m.stat == 'ATK' else (stats.HP if m.stat == 'HP' else 0)
            for _ in range(3):
                for t in (state.alive_enemies() or state.enemies):
                    if getattr(t, 'HP', 0) <= 0:
                        continue
                    d = calculate_damage(stats, _enemy_for_damage(t), sc, m.scale,
                                         m.damage_type, m.element or u.char.element, 80,
                                         stats.CRIT_RATE >= 0.5, skill_type='skill',
                                         crit_mode='expected')
                    _commit_enemy_damage(state, u, t, d.final_damage)
                    u.total_damage_dealt += d.final_damage
        u.extra.pop('poem_lixing', None)
        state.log.append('  献予「理性」: 战技额外3次伤害已结算')
    if skill_key == 'ultimate':
        for e in state.enemies:
            existing_ult = {st.attributes.get('weakness_element')
                            for st in e.statuses if st.id.startswith('anaxa_ult_weak')}
            for el in WEAKNESS_ELEMENTS:
                old = e.get_res(el)
                if old > 0:
                    e.element_res[el] = min(old, -0.2)
                    e.add_status(EnemyStatus(
                        id='anaxa_ult_weak_' + el, name='弱点', category='debuff',
                        source='anaxa', remaining_turns=1,
                        attributes={'weakness_element': el, 'weakness_old_res': old}))
                elif el not in existing_ult:
                    # 已是弱点(天赋添加): 仅挂升华标记(至目标回合开始), 不重复改抗
                    e.add_status(EnemyStatus(
                        id='anaxa_ult_weak_' + el, name='弱点', category='debuff',
                        source='anaxa', remaining_turns=1,
                        attributes={'weakness_element': el, 'weakness_old_res': old}))
        state.log.append('  【升华】: 全7属性弱点+硬控')
        # v6.6c P2: 质性揭露目标受硬控（禁锢 2回合, 敌方回合跳过+推条2500）
        for e in state.enemies:
            if e.extra.get('anaxa_revealed') and getattr(e, 'HP', 0) > 0:
                e.add_status(EnemyStatus(id='anaxa_imprison', name='禁锢',
                                         category='control', source='anaxa',
                                         remaining_turns=2))
                state.log.append(f'  【升华】硬控: {e.name or e.id} 禁锢(2回合)')
    return None


SETTLE_HANDLERS = {'settle_self': _anaxa_settle_self}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_lixing(state, summoner, ms_unit, anaxa):
    """献予「理性」之诗(单次, 那刻夏): 回1SP+立即行动+战技伤害次数+3+真知"""
    from engine.core.combat_engine import _gain_skill_points
    from engine.characters.robin_summeretto import _guest_advance_blocked
    _gain_skill_points(state, 1)
    navs = state.extra.get('navs', {})
    i = state.units.index(anaxa)
    if i in navs and not _guest_advance_blocked(state, summoner, anaxa):
        navs[i] = state.current_av
    anaxa.extra['poem_lixing'] = True
    state.log.append('  献予「理性」之诗: 回1SP+立即行动+战技+3次')


POEM = ("理性", _poem_lixing, False)
