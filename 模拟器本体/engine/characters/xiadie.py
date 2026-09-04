"""遐蝶（M4 批2a；死龙/新蕊核心函数, AI 委托 remembrance）"""

import copy
import random
from engine.runtime import SimUnit, _set_av
from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
from engine.core.combat_engine import _process_lc_effects


def xiadie_xinrui_cap(u) -> float:
    """遐蝶新蕊上限: 献予「生死」之诗→可溢出至200%（68000）"""
    return 68000.0 if u.extra.get('poem_shengsi') else 34000.0


def _xiadie_absorb_hp_loss(state, lost_amount: float, desc: str = ""):
    """遐蝶天赋·掌心淌过的荒芜: 我方全体(含忆灵)每损失1点HP→1点新蕊。
    死龙在场时无法获得新蕊，改为队友损失→死龙HP恢复。"""
    if lost_amount <= 0:
        return
    xiadie = next((u for u in state.units if u.char.id == 'xiadie' and u.is_alive), None)
    if not xiadie:
        return
    dragon = xiadie.memsprite_unit
    if dragon and dragon.is_alive:
        # 死龙在场: 转化为死龙HP恢复
        dragon.current_hp = min(dragon.max_hp, dragon.current_hp + lost_amount)
        state.log.append(f'  死龙回血+{lost_amount:.0f} (HP损失转化{desc}) HP={dragon.current_hp:.0f}/{dragon.max_hp:.0f}')
    else:
        # 死龙不在场: 1:1转化为新蕊 + 伤害叠层
        old_xr = xiadie.xinrui
        cap = xiadie_xinrui_cap(xiadie)
        xiadie.xinrui = min(cap, xiadie.xinrui + lost_amount)
        if xiadie.xinrui - old_xr > 1:
            state.log.append(f'  新蕊+{xiadie.xinrui - old_xr:.0f} (HP损失{desc}) → {xiadie.xinrui:.0f}/{cap:.0f}')
        # 伤害叠层(上限3层20%)
        st = getattr(xiadie, 'relic_stacks', {}) or {}
        cur = st.get('xiadie_dmg_buff', 0)
        if cur < 3:
            cur += 1
            xiadie.base_stats.DMG_BONUS_ALL += 0.20
            if xiadie.memsprite_unit:
                xiadie.memsprite_unit.base_stats.DMG_BONUS_ALL += 0.20
            st['xiadie_dmg_buff'] = cur
            xiadie.relic_stacks = st


def _eid_xiadie_e4(u, state, healer=None, targets=None, heal_amt=0, **kw):
    """遐蝶E4: 遐蝶在场时全队受疗+20%（追加治疗, 不参与新蕊转化）"""
    if not targets or heal_amt <= 0:
        return
    if not any(x.char.id == 'xiadie' and x.is_alive for x in state.units):
        return
    for t in targets:
        if getattr(t, 'is_alive', True):
            t.current_hp = min(t.max_hp, t.current_hp + heal_amt * 0.20)
    state.log.append(f'  遐蝶E4: 全队受疗+20% → 追加{heal_amt * 0.20:.0f}HP×{len(targets)}')


def _eid_xiadie_e6(u, state, **kw):
    """遐蝶E6: 量子抗性穿透+20%（死龙召唤时 copy 继承）"""
    u.base_stats.RES_PEN['量子'] = u.base_stats.RES_PEN.get('量子', 0.0) + 0.20
    state.log.append('  遐蝶E6: 量子抗性穿透+20%')


def _xiadie_heal_to_xinrui(state, targets, heal_amt):
    """行迹3·收容的暗潮: 除死龙外队友治疗→100%转化为新蕊。
    死龙在场→转化为死龙HP。每人上限=新蕊上限12%(4080)，任意单位行动后重置。"""
    if not targets:
        return
    total_heal = heal_amt * len(targets)  # 全队治疗总量
    for t in targets:
        if not hasattr(t, 'char') or not hasattr(t.char, 'id') or t.char.id != 'xiadie':
            continue
        # 每人累计转化上限 = 新蕊上限×12%（4080/8160），任意单位行动后重置
        cap = xiadie_xinrui_cap(t)
        conv_limit = cap * 0.12
        conv = state.extra.setdefault('xiadie_heal_conv', 0.0)
        if conv >= conv_limit:
            continue
        cap_amt = min(total_heal, conv_limit - conv)
        conv += cap_amt
        state.extra['xiadie_heal_conv'] = conv
        dragon = t.memsprite_unit
        if dragon and dragon.is_alive:
            # 死龙在场: 治疗→死龙HP恢复
            dragon.current_hp = min(dragon.max_hp, dragon.current_hp + cap_amt)
            state.log.append(f'  收容的暗潮: 治疗→死龙回血+{cap_amt:.0f} (HP={dragon.current_hp:.0f}/{dragon.max_hp:.0f})')
        else:
            old_xr = t.xinrui
            t.xinrui = min(cap, t.xinrui + cap_amt)
            if t.xinrui - old_xr > 1:
                state.log.append(f'  新蕊+{t.xinrui - old_xr:.0f} (治疗转化) → {t.xinrui:.0f}/{cap:.0f}')


def _tech_xiadie(state, u, is_opener):
    """遐蝶: 开怪→召唤死龙(HP=新蕊上限50%)+行动提前100%+境界+全队40%当前HP消耗;
    非开怪→新蕊+30%上限（用户 2026-08-14 确认: 可选开怪, 一般当开怪判定）"""
    from engine.systems.remembrance import RemembranceSystem
    if not is_opener:
        gain = xiadie_xinrui_cap(u) * 0.30
        u.xinrui = min(xiadie_xinrui_cap(u), u.xinrui + gain)
        state.log.append(f'[秘技] 悲鸣: 非开怪→新蕊+30%上限(+{gain:.0f})')
        return
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    state.extra['_rem_sys'] = rem
    if not (u.memsprite_unit and u.memsprite_unit.is_alive):
        rem.summon_memsprite(state, u, u.char.memsprite, hp_override=xiadie_xinrui_cap(u) * 0.50)
    # v6.3.0b P1-5: 行动提前100%（此前 navs 未动, 立即行动从未生效）
    from engine.runtime import _set_av
    navs = state.extra.get('navs', {})
    uidx = state.units.index(u)
    if uidx in navs:
        _set_av(state, navs, uidx, state.current_av)
    # 境界: 遗世冥域（敌方全抗-20%; 与昔涟秘技结界不同来源, 用户确认不冲突）
    if not state.realm_owner:
        state.realm_owner = 'xiadie'
        state.realm_turns = 3
        for e in state.enemies:
            for elem in list(e.element_res):
                e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
        state.log.append('  遗世冥域: 敌方全属性抗性-20% (3回合)')
    # v6.3.0b P1-5: 全队40%当前HP消耗走统一管线（角色+忆灵, 死龙除外; 新蕊/on_hp_loss/光锥事件）
    from engine.core.combat_engine import _process_lc_effects
    from engine.characters.changyeyue import _dispatch_changyeyue_hp_loss
    from engine.runtime import SimUnit
    total_lost = 0.0
    affected = []
    for eu in state.units:
        if eu.is_alive:
            lost = eu.current_hp * 0.40
            eu.current_hp = max(1, eu.current_hp - lost)
            total_lost += lost
            affected.append((eu, lost))
    for ms in state.memsprites:
        if not ms.is_alive or ms is u.memsprite_unit:
            continue  # 死龙不参与（用户确认）
        lost = ms.current_hp * 0.40
        ms.current_hp = max(1, ms.current_hp - lost)
        total_lost += lost
        affected.append((ms, lost))
    _xiadie_absorb_hp_loss(state, total_lost, '秘技悲鸣')
    state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                             total_lost=total_lost, affected=affected,
                             skill_key='technique')
    _dispatch_changyeyue_hp_loss(state, affected)
    for affected_unit, _lost in affected:
        if isinstance(affected_unit, SimUnit):
            state.extra['lc_last_hp_loss'] = _lost
            _process_lc_effects(affected_unit, state, "on_hp_loss")
    state.log.append('[秘技] 悲鸣: 召唤死龙(HP=新蕊50%) + 行动提前100% + 境界 + 全队40%当前HP消耗')


CHAR_ID = "xiadie"
TECHNIQUE = _tech_xiadie


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _xiadie_turn_tick(u, state):
    # 西风的驻足按“遐蝶本回合”近似：强化战技后保留到下一个常规回合开始，
    # 让之后的死龙 Y 轴行动能够实际读取该加成。
    if u.char.id == 'xiadie':
        u.extra.pop('xiadie_flame_stack', None)


TURN_TICKS = {'pre': _xiadie_turn_tick}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _xiadie_realm_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['xiadie_realm']: 终结技→遗世冥域。"""
    if u.char.id != 'xiadie':
        return None
    # 遐蝶终结技→遗世冥域（召唤死龙由 summon_memsprite 通用处理器完成）
    if state.realm_owner and state.realm_owner != 'xiadie':
        state.log.append(f'  [WARN] 境界已被{state.realm_owner}占据，无法展开遗世冥域')
        return True
    state.realm_owner = 'xiadie'
    state.realm_turns = 3
    for e in state.enemies:
        for elem in e.element_res:
            e.element_res[elem] -= 0.20
    state.log.append('  展开【遗世冥域】(3回合): 敌方全属性抗性-20%')
    return True


EFFECT_TAKEOVERS = {'xiadie_realm': _xiadie_realm_takeover}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _xiadie_cast_side_effects(u, state, skill, skill_key):
    """PHASE cast_side_effects: 终结技消耗新蕊（清零前捕获溢出）。"""
    # 遐蝶终结技消耗新蕊
    if skill_key == 'ultimate':
        # 献予「生死」之诗: 清零前捕获溢出(34000以上部分), 召唤死龙时消费→强化晦翼
        u.extra['shengsi_overflow'] = max(0.0, u.xinrui - 34000.0)
        u.xinrui = 0
    return None


def _xiadie_allies_hp_loss(u, state, total_lost):
    """PHASE allies_hp_loss: HP损失→新蕊/死龙回血（统一吸收）。"""
    # 遐蝶天赋：HP损失→新蕊/死龙回血（统一吸收）
    _xiadie_absorb_hp_loss(state, total_lost, "全队HP消耗")
    return None


PHASE_HOOKS = {'cast_side_effects': _xiadie_cast_side_effects,
               'allies_hp_loss': _xiadie_allies_hp_loss}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _xiadie_on_kill_effect(u, state):
    """PHASE on_kill_effect: 乌黯击杀→死龙速度+100%/1回合。"""
    # v5.1: 遐蝶行迹2·倒置的火炬 — 乌黯击杀→死龙速度+100%/1回合
    if u.memsprite_unit and u.memsprite_unit.is_alive:
        u.memsprite_unit.extra['xiadie_spd_boost'] = 1
        state.log.append('  倒置的火炬: 死龙速度+100%(1回合)')
    return None


PHASE_HOOKS['on_kill_effect'] = _xiadie_on_kill_effect


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _xiadie_post_lc(u, state, skill_key):
    """PHASE post_lc: 行迹1·西风的驻足——强化战技焰息叠层+1。"""
    # v5.1: 遐蝶行迹1·西风的驻足 — 【乌黯】对应死龙在场时的强化战技。
    if skill_key == 'skill_dragon':
        u.extra['xiadie_flame_stack'] = min(6, u.extra.get('xiadie_flame_stack', 0) + 1)
        state.log.append(f'  西风的驻足: 焰息叠层+1 → {u.extra["xiadie_flame_stack"]}/6')
    return None


PHASE_HOOKS['post_lc'] = _xiadie_post_lc


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_shengsi(state, summoner, ms_unit, xiadie):
    """献予「生死」之诗(整场): 新蕊可溢出至200%（68000 cap, 召唤死龙时消费溢出强化晦翼）"""
    xiadie.extra['poem_shengsi'] = True
    state.log.append('  献予「生死」之诗: 新蕊上限200%')


POEM = ("生死", _poem_shengsi, True)


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def xiadie_ai(u, state, **kw):
    """遐蝶AI：新蕊<上限→战技(HP消耗,不耗SP)；≥上限→终结技(召唤死龙→焰息→引爆)
    v7.2.0 裁决A: 姬子·启行在场(拓星视界占境界)→终结技永封, 回落战技攒新蕊"""
    from engine.core.combat_engine import _use_skill
    from engine.characters.himeko_nova import _hn_realm_blocks_ult
    if u.memsprite_unit and u.memsprite_unit.is_alive:
        # 遐蝶E2: 召唤后的下次强化战技+30%新蕊(一次性)
        if u.extra.pop('xiadie_e2_skill_pending', False):
            cap = xiadie_xinrui_cap(u)
            u.xinrui = min(cap, u.xinrui + cap * 0.30)
            state.log.append(f'  遐蝶E2: 强化战技+30%新蕊 → {u.xinrui:.0f}/{cap:.0f}')
        _use_skill(u, state, "skill_dragon")
        return
    if u.xinrui >= xiadie_xinrui_cap(u) and not _hn_realm_blocks_ult(state, u):
        _use_skill(u, state, "ultimate")
        return
    _use_skill(u, state, "skill")


AI = xiadie_ai


# ---- v7.16.0: 死龙机制群（原 remembrance 专属方法, verbatim 迁入）----

def _xiadie_e1_mult(state, t):
    """遐蝶E1: 敌HP≤80%/50%→死龙伤害120%/140%"""
    bp = state.extra.get('enemy_blueprint') or state.enemies[0]
    ratio = t.HP / bp.HP if bp.HP > 0 else 1.0
    return 1.40 if ratio <= 0.50 else (1.20 if ratio <= 0.80 else 1.0)


def _calc_flame_damage(state, summoner, ms_unit, multiplier):
    """焰息伤害：遐蝶生命上限% × multiplier（用户确认: 死龙伤害倍率全部按遐蝶生命计算,
    非死龙HP=34000; v5.6.1: 忆灵有效面板含暂存 buff）"""
    from engine.core.damage import calculate_damage
    from engine.core.combat_engine import _commit_enemy_damage
    from engine.systems.remembrance import _ms_effective_stats
    alive = [e for e in state.enemies if e.HP > 0] or state.enemies
    total = 0.0
    speed_boost = False
    for t in alive:
        mult = multiplier
        if summoner.eidolon_rank >= 1:
            mult = mult * _xiadie_e1_mult(state, t)
        d = calculate_damage(_ms_effective_stats(ms_unit, state), t, summoner.max_hp, mult,
                            "direct", "量子", 80, summoner.base_stats.CRIT_RATE >= 0.5, crit_mode="expected")
        total += d.final_damage
        _, killed = _commit_enemy_damage(state, summoner, t, d.final_damage)
        if killed:
            speed_boost = True
        elif d.final_damage <= 0:
            # 当前模型中“无法削减生命”只会表现为最终伤害为零。
            speed_boost = True
    return total, speed_boost


def _trigger_dragon_death(state, summoner, ms_unit):
    """死龙消失→灼掠幽墟的晦翼：6次弹射(死龙HP×40%)+全队回血"""
    from engine.core.damage import calculate_damage
    from engine.core.combat_engine import _commit_enemy_damage
    from engine.systems.remembrance import RemembranceSystem, _ms_effective_stats
    alive = state.alive_enemies() or state.enemies
    bounce_count = 9 if summoner.eidolon_rank >= 6 else 6  # E6: 弹射+3
    total = 0.0
    # 献予「生死」之诗: 溢出消费的晦翼倍率加成
    huiyi_bonus = ms_unit.extra.get('huiyi_mult_bonus', 0.0)
    for _ in range(bounce_count):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        mult = 40.0 + huiyi_bonus
        if summoner.eidolon_rank >= 1:
            mult = mult * _xiadie_e1_mult(state, t)
        d = calculate_damage(_ms_effective_stats(ms_unit, state), t, summoner.max_hp, mult,
                            "direct", "量子", 80, summoner.base_stats.CRIT_RATE >= 0.5, crit_mode="expected")
        total += d.final_damage
        _commit_enemy_damage(state, summoner, t, d.final_damage)
    ms_unit.total_damage_dealt += total
    summoner.total_damage_dealt += total
    # 全队回血6%HP+800（自爆期间死龙在场判定→不触发收容的暗潮转化新蕊）
    for eu in state.units:
        if eu.is_alive:
            heal = summoner.base_stats.HP * 0.06 + 800
            eu.current_hp = min(eu.max_hp, eu.current_hp + heal)
    state.log.append(f'  灼掠幽墟的晦翼: {total:.0f} ({bounce_count}次弹射) + 全队回血(不攒新蕊)')
    # 解除境界
    if state.realm_owner == 'xiadie':
        for e in state.enemies:
            for elem in e.element_res:
                e.element_res[elem] += 0.20
        state.realm_owner = ''
        state.realm_turns = 0
        state.log.append('  解除【遗世冥域】')
    # 移除死龙
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    rem.despawn_memsprite(state, summoner, ms_unit)


def _dragon_flame_once(state, summoner, ms_unit):
    """死龙单次喷吐(Y轴行动): 消耗25%生命上限, HP≤25%→主动降至1点→自爆(晦翼)。
    倍率递增: 24→28→34→34(两档后封顶)。只要HP>1就稳定能喷一次。"""
    if not ms_unit.is_alive or ms_unit.current_hp <= 1:
        return
    hp_pct = 25.0
    base_multiplier = ms_unit.extra.get('flame_mult', 24.0)
    multiplier = base_multiplier
    # 行迹1·西风的驻足: 施放战技(乌黯)后焰息伤害+30%/层, 叠6层, 回合末消失
    flame_stack = summoner.extra.get('xiadie_flame_stack', 0)
    if flame_stack > 0:
        multiplier = multiplier * (1.0 + 0.30 * flame_stack)
    will_destruct = ms_unit.current_hp <= ms_unit.max_hp * 0.25
    # 释放焰息(消耗25%生命上限，最低降至1点)
    cost = ms_unit.max_hp * (hp_pct / 100.0)
    # 遐蝶E2炽意: 抵扣焰息HP消耗（不扣HP, 仍正常喷吐与自爆判定）
    if summoner.extra.get('chiyi', 0) > 0:
        summoner.extra['chiyi'] -= 1
        cost = 0
        state.log.append(f'  炽意抵扣: 焰息不消耗HP (剩余{summoner.extra["chiyi"]}层)')
    ms_unit.current_hp = max(1, ms_unit.current_hp - cost)
    dmg, speed_boost = _calc_flame_damage(state, summoner, ms_unit, multiplier)
    if speed_boost:
        ms_unit.extra['xiadie_spd_boost'] = 1
        state.log.append('  倒置的火炬: 死龙速度+100%(下次行动)')
    ms_unit.total_damage_dealt += dmg
    summoner.total_damage_dealt += dmg
    state.log.append(f'  焰息: {dmg:.0f} (倍率{multiplier:.0f}%) HP={ms_unit.current_hp:.0f}/{ms_unit.max_hp:.0f}')
    # 倍率递增: 24→28→34(两档后封顶)。行迹的临时增伤不能写回基础序列。
    ms_unit.extra['flame_mult'] = min(
        34.0, base_multiplier + (6 if base_multiplier >= 28 else 4))
    # HP≤25%的该次喷吐后→已降至1点→触发晦翼自爆
    if will_destruct:
        state.log.append(f'  HP≤25%: 已降至1点, 触发晦翼')
        _trigger_dragon_death(state, summoner, ms_unit)


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _xd_ms_build(u, state, ms_data, hp_override):
    """PHASE ms_build: 死龙构建（HP=新蕊上限34000/秘技减半, E2 炽意拉条, 溢出消费）。"""
    from engine.runtime import _set_av, _stamp_av_key
    dragon_hp = hp_override or 34000.0
    ms_stats = copy.deepcopy(u.base_stats)
    ms_stats.HP = dragon_hp
    from engine.systems.remembrance import MemSpriteUnit
    ms_unit = MemSpriteUnit(
        data=ms_data, summoner_id=u.char.id,
        max_hp=dragon_hp, current_hp=dragon_hp,
        base_stats=ms_stats,
    )
    ms_unit.current_energy = 0
    ms_unit.runtime_is_backup = True  # v5.2: 后援单位（运行时标记, 不写配置）
    ms_unit.extra['flame_mult'] = 24.0  # 焰息倍率递增起点
    state.memsprites.append(ms_unit)
    u.memsprite_unit = ms_unit
    # 遐蝶E2: 召唤→+2炽意(抵扣焰息HP消耗), 行动提前100%, 下次强化战技+30%新蕊
    if u.eidolon_rank >= 2:
        u.extra['chiyi'] = 2
        u.extra['xiadie_e2_skill_pending'] = True
        navs = state.extra.get('navs', {})
        uid = state.units.index(u)
        if uid in navs:
            _set_av(state, navs, uid, state.current_av)  # v6.2.1b P3-1: 统一入口补戳
        state.log.append('  遐蝶E2: +2炽意, 行动提前100%')
    # 献予「生死」之诗: 消费终结技前捕获的溢出新蕊→晦翼倍率加成(每1%→+0.24, ≤2敌→+0.48)
    overflow = u.extra.pop('shengsi_overflow', 0.0)
    if overflow > 0:
        pct = overflow / 34000.0 * 100.0
        n_enemies = len(state.alive_enemies() or state.enemies)
        bonus = pct * (0.48 if n_enemies <= 2 else 0.24)
        ms_unit.extra['huiyi_mult_bonus'] = bonus
        state.log.append(f'  献予「生死」之诗: 消耗溢出{overflow:.0f}→晦翼倍率+{bonus:.1f}%')
    # 死龙0行动值留在Y轴（后到先动→排在最先）。回到遐蝶常规回合→强化战技→之后死龙回合
    ms_unit.extra['next_av'] = state.current_av
    _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳, 同AV并列才能后到先动
    state.log.append(f'  召唤死龙 HP={dragon_hp:.0f} SPD={ms_data.base_SPD} (后援, Y轴行动条)')
    state.hooks.trigger_all("on_memsprite_summon", u=u, state=state,
                             summoner=u, ms_unit=ms_unit)
    return ms_unit


def _xd_ms_despawn_absorb(u, state, lost_hp, ms_name):
    """PHASE ms_despawn_absorb: 忆灵消失剩余HP→新蕊/死龙回血（死龙除外）。"""
    if lost_hp > 0 and ms_name != '死龙':
        _xiadie_absorb_hp_loss(state, lost_hp, f'{ms_name}消失')
    return None


def _xd_realm_expire(u, state):
    """PHASE realm_expire: 遗世冥域到期→全敌元素抗性+20%回退。"""
    for e in state.enemies:
        for elem in e.element_res:
            e.element_res[elem] += 0.20
    return None


def _xd_ms_action(u, state, ms_unit):
    """PHASE ms_action: 死龙Y轴喷吐(完全处理→True)；非死龙 spd 修正+排程+AI(全包→True)。"""
    from engine.runtime import _stamp_av_key
    from engine.systems.remembrance import AV_PER_TURN, RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    # 死龙Y轴行动: 每次行动喷吐一次(倍率递增24→28→34→34), HP≤25%→自爆
    if ms_unit.data.name == '死龙':
        _dragon_flame_once(state, u, ms_unit)
        return True
    # 更新忆灵AV（死龙通常由主循环先更新后行动；保留此分支供直调入口）。
    spd = ms_unit.action_spd
    if u.char.id == 'xiadie':
        if u.current_hp >= u.max_hp * 0.5:
            spd *= 1.4
        if ms_unit.extra.get('xiadie_spd_boost'):
            spd *= 2.0
            ms_unit.extra['xiadie_spd_boost'] = 0  # 1回合后消耗
    ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / max(spd, 1.0)
    _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳（额外回合路径不经主循环 _set_av）
    rem._memsprite_ai(state, u, ms_unit)
    return True


PHASE_HOOKS['ms_build'] = _xd_ms_build
PHASE_HOOKS['ms_despawn_absorb'] = _xd_ms_despawn_absorb
PHASE_HOOKS['realm_expire'] = _xd_realm_expire
PHASE_HOOKS['ms_action'] = _xd_ms_action
