"""风堇（M4 批1 迁入；忆灵/诗层/AI 属 remembrance, M6 处理）"""

import copy
import random
from engine.runtime import SimUnit, TimedBuff


def _fengjin_cleanse(state, u):
    """风堇行迹3·雷雨轻柔: 战技/终结技→解除全队1个负面效果
    （v5.0 P4: 优先清控制/减益状态, 再清负属性TimedBuff兜底）"""
    cleared = 0
    for eu in state.units:
        if not eu.is_alive:
            continue
        # ① 优先清 PlayerStatus（控制类优先级最高）
        for st in list(eu.statuses):
            if st.category in ('control', 'debuff'):
                eu.statuses.remove(st)
                state.hooks.trigger_all("on_exit_state", u=eu, state=state, status=st)
                cleared += 1
                break
        if cleared < 1:
            # ② 负属性 TimedBuff 兜底
            for b in list(eu.buffs):
                if any(v < 0 for v in b.attributes.values()):
                    eu.buffs.remove(b)
                    cleared += 1
                    break
        if cleared >= 1:
            break
    if cleared:
        state.log.append(f'  行迹3·雷雨轻柔: 净化全队{cleared}个负面效果')


def _fengjin_talent_heal_buff(state, healer):
    """风堇天赋·疗愈世间的晨曦（风堇.txt）: 风堇或小伊卡提供治疗 →
    小伊卡造成的伤害+80%/层（2回合, 最多3层, 每层独立, 满层替换最旧）"""
    fengjin = None
    char = getattr(healer, 'char', None)
    if getattr(char, 'id', None) == 'fengjin':
        fengjin = healer
    elif getattr(healer, 'summoner_id', '') == 'fengjin':
        fengjin = next((x for x in state.units
                        if x.char.id == 'fengjin' and x.is_alive), None)
    if fengjin is None:
        return
    ms = fengjin.memsprite_unit
    if not ms or not ms.is_alive:
        return
    layers = [b for b in ms.buffs if getattr(b, 'param_id', '') == 'fengjin_talent_dmg']
    if len(layers) >= 3:
        ms.buffs.remove(layers.pop(0))
    ms.buffs.append(TimedBuff(source_id='fengjin', attributes={'DMG_BONUS_ALL': 80.0},
                              remaining_turns=2, source_name='疗愈世间的晨曦',
                              param_id='fengjin_talent_dmg'))
    state.log.append(f'  疗愈世间的晨曦: 小伊卡伤害+80% ({min(len(layers) + 1, 3)}/3)')


def _trace_fengjin_t1(u, state, **kw):
    """风堇行迹1·暴风停歇: SPD>200→HP上限+20%（面板改动, 小伊卡召唤时按继承比例自动生效）"""
    if u.char.id == 'fengjin' and u.base_stats.SPD > 200:
        u.base_stats.HP *= 1.20
        state.log.append(f'  行迹1·暴风停歇: HP上限+20% (SPD={u.base_stats.SPD:.0f})')


def _trace_fengjin_t2(u, state, **kw):
    """风堇行迹2·阴云莞尔: 风堇+小伊卡CR+100%（风堇.txt; 小伊卡召唤先于行迹,
    copy 不继承——需显式给小伊卡）"""
    if u.char.id == 'fengjin':
        u.base_stats.CRIT_RATE += 1.00
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.CRIT_RATE += 1.00
        state.log.append('  行迹2·阴云莞尔: 风堇+小伊卡CR+100%')


def _trace_fengjin_t3(u, state, **kw):
    """风堇行迹3·雷雨轻柔: EFFECT_RES+50%（战技/终结技净化由引擎内联处理）"""
    if u.char.id == 'fengjin':
        u.base_stats.EFFECT_RES += 0.50
        state.log.append('  行迹3·雷雨轻柔: 效果抵抗+50%')


def _eid_fengjin_e1(u, state, **kw):
    """风堇E1: 攻击后回8%HP"""
    heal = u.max_hp * 0.08
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    state.log.append(f'  风堇E1: 攻击后回8%HP +{heal:.0f}')


def _eid_fengjin_e2(u, state, total_lost=0, affected=None, **kw):
    """风堇E2: HP降低→SPD+30% 2回合（刷新不叠加）"""
    from engine.runtime import TimedBuff
    if not affected:
        return
    for eu, lost in affected:
        # v6.5.1: affected 可能含忆灵(MemSpriteUnit.char 无 id) → isinstance 过滤
        from engine.runtime import SimUnit
        if isinstance(eu, SimUnit) and eu.char.id == 'fengjin' and eu.is_alive:
            for b in eu.buffs:
                if getattr(b, 'source_name', '') == '风堇E2·翼下':
                    b.remaining_turns = 2
                    break
            else:
                eu.buffs.append(TimedBuff(source_id='fengjin_e2',
                                          attributes={"SPD_PERCENT": 30.0},
                                          remaining_turns=2, source_name='风堇E2·翼下'))
                state.log.append('  风堇E2: HP降低→SPD+30% 2回合')
            break


def _eid_fengjin_e4(u, state, **kw):
    """风堇E4: 行迹1强化—SPD>200每超1点→暴伤+2%（上限200点）"""
    if u.base_stats.SPD > 200:
        bonus = 0.02 * min(u.base_stats.SPD - 200, 200)
        u.base_stats.CRIT_DMG += bonus
        state.log.append(f'  风堇E4: 超速暴伤+{bonus:.2f} (SPD={u.base_stats.SPD:.0f})')


def _eid_fengjin_e6(u, state, **kw):
    """风堇E6: 小伊卡在场→全队RES_PEN+20%（一次性守卫防重复）"""
    if state.extra.get('fengjin_e6_respen'):
        return
    fengjin = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
    if not fengjin or fengjin.eidolon_rank < 6:
        return
    state.extra['fengjin_e6_respen'] = True
    for eu in state.units:
        eu.base_stats.RES_PEN_ALL += 0.20
    state.log.append('  风堇E6: 小伊卡在场→全队RES_PEN+20%')


def _tech_fengjin(state, u, is_opener):
    """风堇: 全队回复30%生命上限+600 + 全队生命上限+20% 2回合（风堇.txt 秘技·天气正好，万物可爱！）
    回退由风堇 tick_turn 的 tech_maxhp_turns 到期执行"""
    heal = u.base_stats.HP * 0.30 + 600
    for eu in state.units:
        if eu.is_alive:
            eu.current_hp = min(eu.max_hp, eu.current_hp + heal)
            if 'tech_orig_maxhp' not in eu.extra:
                eu.extra['tech_orig_maxhp'] = eu.max_hp
            eu.max_hp = eu.max_hp * 1.20
            eu.current_hp = min(eu.max_hp, eu.current_hp)
    u.extra['tech_maxhp_turns'] = 2
    state.log.append(f'[秘技] 天气正好: 全队回复{heal:.0f}HP + 生命上限+20% 2回合')


CHAR_ID = "fengjin"
TECHNIQUE = _tech_fengjin


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _fengjin_ult_state_duration(u, state, attrs, skill):
    """EFFECT_MUTATORS['fengjin_ult_state']: 雨过天晴状态条目持续3回合。"""
    return attrs, 3


EFFECT_MUTATORS = {'fengjin_ult_state': _fengjin_ult_state_duration}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _fengjin_pre_hooks_cast(u, state, skill_key):
    """PHASE pre_hooks_cast: 行迹3·雷雨轻柔——战技/终结技净化全队1负面。"""
    # 风堇行迹3·雷雨轻柔: 战技/终结技→净化全队1个负面效果
    if skill_key in ('skill', 'ultimate'):
        _fengjin_cleanse(state, u)
        # 献予「天空」之诗: 战技/终结技后消耗1层
        layers = u.extra.get('poem_tiankong', 0)
        if layers > 0:
            u.extra['poem_tiankong'] = layers - 1
            state.log.append(f'  献予「天空」之诗: 消耗1层 ({layers-1}层)')
    return None


PHASE_HOOKS = {'pre_hooks_cast': _fengjin_pre_hooks_cast}


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _fengjin_heal_amount_mod(u, state, heal_amt):
    """PHASE heal_amount_mod: 行迹1·暴风停歇——每超1点SPD治疗量+1%（上限200点）。"""
    # 风堇行迹1·暴风停歇: 每超1点SPD→治疗量+1%(上限200点)
    if u.base_stats.SPD > 200:
        return heal_amt * (1.0 + min(u.base_stats.SPD - 200, 200) / 100.0)
    return None


def _fengjin_memsprite_heal_base(u, state, ms):
    """PHASE memsprite_heal_base: 忆灵治疗按风堇自身生命上限（v5.7）。"""
    return u.base_stats.HP


def _fengjin_heal_target_mod(u, state, t, heal_amt):
    """PHASE heal_target_mod: 行迹2·阴云莞尔——HP≤50%目标治疗量+25%。"""
    # 风堇行迹2·阴云莞尔: 对HP≤50%目标治疗量+25%
    if t.current_hp <= t.max_hp * 0.50:
        return heal_amt * 1.25
    return None


def _fengjin_memoir_heal_mod(u, state, heal_val):
    """PHASE memoir_heal_mod: 献予「天空」之诗——持层时治疗计入小伊卡×1.72。"""
    # 献予「天空」之诗: 风堇持层时治疗计入小伊卡×1.72
    if u.extra.get('poem_tiankong', 0) > 0:
        return heal_val * 1.72
    return None


PHASE_HOOKS['heal_amount_mod'] = _fengjin_heal_amount_mod
PHASE_HOOKS['memsprite_heal_base'] = _fengjin_memsprite_heal_base
PHASE_HOOKS['heal_target_mod'] = _fengjin_heal_target_mod
PHASE_HOOKS['memoir_heal_mod'] = _fengjin_memoir_heal_mod


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_tiankong(state, summoner, ms_unit, fj):
    """献予「天空」之诗(持续层): 回24能量(层数由 _use_memsprite_skill 顶部每忆灵技+2统一叠加)"""
    from engine.core.combat_engine import _gain_energy
    # v6.2.1: 统一回能入口（Codex P2-5: 直写绕过 ER/on_energy_change/迷迷充能 bank）
    _gain_energy(fj, 24.0, state=state)
    state.log.append(f'  献予「天空」之诗: 风堇回24能量 (能量={fj.current_energy:.0f})')


POEM = ("天空", _poem_tiankong, True)


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def _fengjin_extra_turn(state, u):
    """雨过天晴: 小伊卡额外回合入 X 轴队列（治疗在 X 轴执行时进行, v6.2.1 拆分防双份）"""
    if u.extra.get('clear_sky_turns', 0) <= 0:
        return
    ms = u.memsprite_unit
    if not ms or not ms.is_alive:
        return
    # 入 X 轴队列（避免重复入队）; 天赋追加治疗由 X 轴执行处统一结算
    # v6.2.1: 此前此处即奶一次 + X 轴执行再奶一次 = 双份（Harness P2-1）
    if not any(x is ms for x, k in state.extra.get('extra_turns', [])):
        state.extra.setdefault('extra_turns', []).append((ms, 'extra'))
        state.log.append(f'  小伊卡额外回合入队')


def fengjin_ai(u, state, **kw):
    """风堇AI: 战技流（用户确认: 实机基本不释放普攻）——SP>0→战技(治疗), SP=0→普攻。
    终结技由 phase-1 拦截入 X 轴队列（雨过天晴在 _ult_post 处理）。
    雨过天晴(3回合): 每次行动后小伊卡额外回合入队→乌云乌云+天赋追加治疗"""
    from engine.core.combat_engine import _use_skill
    if state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")
    _fengjin_extra_turn(state, u)


AI = fengjin_ai


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _fj_ms_created(u, state, ms_unit):
    """PHASE ms_created: 展翼奔向日辉——小伊卡被召唤→风堇+15能量, 首次召唤额外+30。"""
    from engine.core.combat_engine import _gain_energy
    first = not u.extra.get('fengjin_first_summon', False)
    gain = 45 if first else 15
    _gain_energy(u, gain, state=state)
    u.extra['fengjin_first_summon'] = True
    state.log.append(f'  展翼奔向日辉: 风堇+{gain}能量')
    return None


def _fj_ms_despawn(u, state, ms_unit, ms_name):
    """PHASE ms_despawn_settle: 坠落然后飞翔——小伊卡消失→风堇行动提前30%。"""
    from engine.core.combat_engine import _effective_spd
    from engine.runtime import AV_PER_TURN
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu is u and i in navs:
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(u, state)) * 0.30)
            break
    state.log.append('  坠落然后飞翔: 小伊卡消失→风堇行动提前30%')
    return True


def _fj_turn_tick(u, state):
    """PHASE turn_tick_rem: 雨过天晴到期全队(含忆灵)HP上限回退 + 秘技·天气正好回退。"""
    turns = u.extra.get('clear_sky_turns', 0)
    if turns > 0:
        turns -= 1
        u.extra['clear_sky_turns'] = turns
        if turns <= 0:
            u.extra['clear_sky_turns'] = 0
            # v5.7: 退出雨过天晴→全队HP上限回退原值（v6.2.1: 含忆灵）
            for eu in list(state.units) + list(state.memsprites):
                orig = eu.extra.pop('clear_sky_orig_maxhp', None)
                if orig is not None and eu.is_alive:
                    eu.max_hp = orig
                    eu.current_hp = min(orig, eu.current_hp)
            state.log.append('  退出【雨过天晴】(HP上限回退, 含忆灵)')
    # v6.3.0: 秘技·天气正好 HP 上限加成 2 回合到期回退
    tech_turns = u.extra.get('tech_maxhp_turns', 0)
    if tech_turns > 0:
        tech_turns -= 1
        u.extra['tech_maxhp_turns'] = tech_turns
        if tech_turns <= 0:
            for eu in list(state.units) + list(state.memsprites):
                orig = eu.extra.pop('tech_orig_maxhp', None)
                if orig is not None and eu.is_alive:
                    eu.max_hp = orig
                    eu.current_hp = min(eu.max_hp, eu.current_hp)
            state.log.append('  秘技·天气正好: 全队生命上限回退')
    return None




def _ms_buff_tick(u, state, ms_unit):
    """PHASE ms_buff_tick: 技能结算后自身有限持续效果-1（含到期移除）。"""
    if not ms_unit.is_alive:
        return None
    kept = []
    for b in ms_unit.buffs:
        if b.remaining_turns > 0:
            b.remaining_turns -= 1
        # -1 等负数表示永久；0 表示本次结算后到期。
        if b.remaining_turns != 0:
            kept.append(b)
    removed = len(ms_unit.buffs) - len(kept)
    ms_unit.buffs[:] = kept
    if removed:
        state.log.append(f'  {ms_unit.data.name}技能后: 自身持续效果-1({removed}层到期移除)')
    return None

PHASE_HOOKS['ms_buff_tick'] = _ms_buff_tick
PHASE_HOOKS['ms_created'] = _fj_ms_created
PHASE_HOOKS['ms_despawn_settle'] = _fj_ms_despawn
PHASE_HOOKS['turn_tick_rem'] = _fj_turn_tick
