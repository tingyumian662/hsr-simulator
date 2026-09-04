"""昔涟（M4 批2a；结界/涟漪/选目标, AI 委托 remembrance）"""

import copy
import random
from engine.core.combat_engine import _build_effective_stats


def _select_xilian_target(state):
    """献予目标选择（数据驱动优先级, 不硬编码进昔涟AI）:
    存活非昔涟 → 未获诗黄金裔（整局生效优先→单次生效, 同档按队伍位置）
    → 已获诗黄金裔按位置循环 → 无黄金裔取非黄金裔"""
    from engine.characters import POEMS
    from engine.core.character_utils import has_poem, is_gold_offspring
    allies = [eu for eu in state.units if eu.is_alive and eu.char.id != 'xilian']
    if not allies:
        return None
    gold = [eu for eu in allies if is_gold_offspring(eu)]
    if gold:
        ungifted = [eu for eu in gold if not has_poem(eu)]
        pool = ungifted or gold
        # 整局生效诗篇优先（POEM_PERSISTENT 数据驱动）; 同档按队伍位置稳定
        pool = sorted(pool, key=lambda eu: (not POEMS.get(eu.char.id, ('', None, False))[2], eu.position))
        return pool[0]
    return allies[0]


def _xilian_trace1_speed_pen(u, state, **kw):
    """行迹1·三相的因果: SPD≥180→全队伤害+20%；每超1点→冰抗穿透+2%(上限60点)
    v5.7: 条件判定改有效面板（含战斗内SPD buff, 此前静态面板）"""
    if u.char.id != 'xilian':
        return
    from engine.core.combat_engine import _build_effective_stats
    spd = _build_effective_stats(u, state).SPD
    if spd < 180:
        return
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.DMG_BONUS_ALL += 0.20
    state.log.append(f'  三相的因果: SPD={spd:.0f}≥180→全队伤害+20%')
    excess = min(spd - 180, 60)
    if excess > 0:
        pen = excess * 0.02
        u.base_stats.RES_PEN['冰'] += pen
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.RES_PEN['冰'] += pen
        state.log.append(f'  三相的因果: 超额{excess:.0f}点→冰抗穿透+{pen*100:.0f}%')


def _xilian_trace2_memsprite_future(u, state, summoner=None, ms_unit=None, **kw):
    """行迹2·记忆的净子: 队友的忆灵被召唤时获得【未来】(忆灵持有的不被消耗)"""
    if not ms_unit or summoner is None:
        return
    if summoner.char.id != 'xilian':
        summoner.has_future = True
        ms_unit.has_future = True  # 忆灵持有的未来不会被消耗(标记位)
        state.log.append(f'  记忆的净子: {summoner.char.name}忆灵被召唤→获得【未来】')


def _xilian_trace3_start_zhuiyi(u, state, **kw):
    """行迹3·岁月的旅人: 队伍中1/2/3+名黄金裔或记忆角色→开局+2/3/6追忆"""
    if u.char.id != 'xilian':
        return
    from engine.core.character_utils import count_gold_or_memory
    count = count_gold_or_memory(state.units, exclude_id='xilian')
    bonus = {1: 2, 2: 3, 3: 6}.get(count, 0) if count >= 1 else 0
    if bonus > 0:
        u.zhuiyi = min(27, u.zhuiyi + bonus)
        state.log.append(f'  岁月的旅人: {count}名黄金裔/记忆→追忆+{bonus} ({u.zhuiyi:.0f}/27)')


def _eid_xilian_e2(u, state, **kw):
    """昔涟E2: 进战+12追忆"""
    u.zhuiyi = min(27, u.zhuiyi + 12)
    state.log.append(f'  昔涟E2: 开局追忆+12 → {u.zhuiyi:.0f}/27')


def _tech_xilian(state, u, is_opener):
    """昔涟: 进战展开战技结界（真伤24% 2回合; 不获追忆——秘技非战技）（昔涟.txt 秘技·西风尽头）"""
    from engine.runtime import SimState
    if state.realm_owner and state.realm_owner != 'xilian':
        state.log.append(f'  [WARN] 境界已被{state.realm_owner}占据, 昔涟秘技结界无法展开')
        return
    state.realm_owner = 'xilian'
    state.realm_turns = 2
    state.realm_true_dmg = 0.24
    state.log.append('[秘技] 西风尽头: 展开结界(真伤24% 2回合)')


CHAR_ID = "xilian"
TECHNIQUE = _tech_xilian


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _xilian_turn_tick(u, state):
    # 未来 token 消耗
    if u.char.id != 'xilian' and u.has_future:
        u.has_future = False
        xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
        if xilian:
            xilian.zhuiyi = min(27, xilian.zhuiyi + 1)
            sources = xilian.extra.setdefault('zhuiyi_sources', set())
            sources.add(u.char.id)
            state.log.append(f'  未来消耗→昔涟追忆+1 ({xilian.zhuiyi:.0f}/27) 来源={u.char.name}')
            state.hooks.trigger_all("on_future_consume", u=u, state=state)


TURN_TICKS = {'late': _xilian_turn_tick}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _xilian_field_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['xilian_field']: 战技→结界（我方伤害附加真伤）。"""
    if u.char.id != 'xilian':
        return None
    # 昔涟战技→结界
    # v7.2.0 项目主裁决: 昔涟没有境界技能（她是遐蝶/白厄的售后角色）——
    # 结界不读写 realm_owner, 不参与境界互斥; 独立倒计时存 xilian_field_turns
    state.extra['xilian_field_turns'] = 2
    # 昔涟E2: 结界真伤=基础24% + 每有1名不同角色获得德谬歌忆灵技增益+6%（上限48%）
    # v6.2: 累计口径——已获增益角色记录于 xilian_e2_gifted, 由 _xilian_support_skill 递增
    gifted = state.extra.get('xilian_e2_gifted', set())
    if u.eidolon_rank >= 2:
        state.realm_true_dmg = min(0.48, 0.24 + 0.06 * len(gifted))
        state.log.append(f'  昔涟E2: 结界真伤={state.realm_true_dmg:.2f} ({len(gifted)}名角色获增益)')
    else:
        state.realm_true_dmg = 0.24
    state.log.append('  展开结界(2回合): 我方伤害附加24%真伤')
    u.zhuiyi += 3
    return True


def _xilian_ult_ripple_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['xilian_ult_ripple']: 终结技→涟漪+激活全队终结技（单场1次）。"""
    from engine.systems.remembrance import RemembranceSystem
    from engine.core.combat_engine import _enqueue_ult
    from engine.characters.robin_summeretto import _guest_advance_blocked
    if u.char.id != 'xilian':
        return None
    if u.extra.get('xilian_ult_used'):
        state.log.append('  [WARN] 昔涟终结技单场只能施放1次')
        return True
    u.extra['xilian_ult_used'] = True
    u.is_ripple = True
    u.base_stats.CRIT_RATE += 0.50
    if u.memsprite_unit:
        u.memsprite_unit.base_stats.CRIT_RATE += 0.50
    state.extra['xilian_field_turns'] = -1  # v7.2.0: 结界永久(独立于境界系统)
    u.story_points += 1  # 终结技后+1故事
    state.log.append('  进入【往昔的涟漪】: CR+50%, 结界永久, 普攻强化')
    # 首次终结技第二个效果（用户确认实机）: 选择释放 花与箭/此诗献予 共2次忆灵技
    # （第1次由召唤立即行动完成; 此处补第2次; 每次扣12追忆, 终结技总消耗24）
    if u.memsprite_unit and u.memsprite_unit.is_alive:
        _xilian_memsprite_action(state, u, u.memsprite_unit)
    # 昔涟E6: 首次终结技→全队拉条100%
    if u.eidolon_rank >= 6:
        navs = state.extra.get('navs', {})
        for i, eu in enumerate(state.units):
            if eu.is_alive and i in navs \
                    and not _guest_advance_blocked(state, u, eu):
                navs[i] = state.current_av
        state.log.append('  昔涟E6: 首次终结技→全队拉条100%')
    # 激活全体队友的终结技（入 X 轴队列排队，不消耗回合）
    for eu in state.units:
        if eu is u or not eu.is_alive:
            continue
        if eu.char.id == 'changyeyue':
            continue  # 长夜月终结技为状态技，不在此触发
        if eu.char.max_energy and eu.char.max_energy > 0:
            _enqueue_ult(state, eu)
            state.log.append(f'  >>> 昔涟终结技激活: {eu.char.name} 终结技入队')
    return True  # 已经处理完毕，不需要创建TimedBuff


EFFECT_TAKEOVERS = {'xilian_field': _xilian_field_takeover,
                    'xilian_ult_ripple': _xilian_ult_ripple_takeover}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _xilian_special_resource_cost(u, state, skill, skill_key):
    """PHASE special_resource_cost: 追忆门槛。"""
    zhuiyi_cost = skill.cost.get("_zhuiyi", 0)
    if zhuiyi_cost <= 0:
        return None
    if u.zhuiyi < zhuiyi_cost:
        state.log.append(f'  [WARN] 追忆不足({u.zhuiyi:.0f}<{zhuiyi_cost})')
        return (True, None)
    u.zhuiyi -= zhuiyi_cost
    state.log.append(f'  追忆-{zhuiyi_cost} → {u.zhuiyi:.0f}/27')
    return (False, None)


def _xilian_cast_side_effects(u, state, skill, skill_key):
    """PHASE cast_side_effects: 普攻/强化普攻获取追忆（天赋：众愿啊，汇流如歌）。"""
    # 昔涟普攻/强化普攻获取追忆（天赋：众愿啊，汇流如歌）
    zhuiyi_gain = {"basic_attack": 1, "basic_attack_enhanced": 3}.get(skill_key, 0)
    if zhuiyi_gain > 0:
        u.zhuiyi = min(27, u.zhuiyi + zhuiyi_gain)
        state.log.append(f'  追忆+{zhuiyi_gain} → {u.zhuiyi:.0f}/27')
    return None


PHASE_HOOKS = {'special_resource_cost': _xilian_special_resource_cost,
               'cast_side_effects': _xilian_cast_side_effects}


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def _xilian_sync_memsprite_hp(u):
    """德谬歌HP同步: 昔涟HP%变化→德谬歌HP%同步（等待，在所有的过去）"""
    ms = u.memsprite_unit
    if not ms or not ms.is_alive or u.char.id != 'xilian':
        return
    if u.max_hp <= 0 or ms.max_hp <= 0:
        return
    ms.current_hp = ms.max_hp * (u.current_hp / u.max_hp)


def xilian_ai(u, state, **kw):
    """昔涟AI: 常态→战技(+3追忆),≥24→终结技; 涟漪→强化普攻,≥12→一如初见"""
    from engine.core.combat_engine import _use_skill
    # HP同步: 昔涟HP%变化→德谬歌同步
    _xilian_sync_memsprite_hp(u)
    # 涟漪态（实机: 常规回合只能释放向着爱与明天;
    # 忆灵技释放类似终结技——追忆≥12 随时经 X 轴触发, 不走常规回合）
    if u.is_ripple:
        _use_skill(u, state, "basic_attack_enhanced")
        return
    # 常态: 优先终结技(单场1次)，SP不足时普攻(+1SP)
    if u.zhuiyi >= 24 and not u.extra.get('xilian_ult_used'):
        _use_skill(u, state, "ultimate")
        return
    if state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")
    # 战技后给队友上未来
    for eu in state.units:
        if eu.is_alive and eu.char.id != 'xilian':
            eu.has_future = True


AI = xilian_ai


# ---- v7.16.0: 德谬歌专属（原 remembrance 专属方法, verbatim 迁入）----

def _record_xilian_e2_gift(state, target):
    """仅在德谬歌确实施加可消费增益后记录昔涟E2角色数。"""
    xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
    if not xilian or xilian.eidolon_rank < 2:
        return
    gifted = state.extra.setdefault('xilian_e2_gifted', set())
    if target.char.id in gifted:
        return
    gifted.add(target.char.id)
    if state.extra.get('xilian_field_turns'):  # v7.2.0: 结界独立判定(无境界系统)
        state.realm_true_dmg = min(0.48, 0.24 + 0.06 * len(gifted))
        state.log.append(f'  昔涟E2: 获增益角色+1({target.char.name})→结界真伤{state.realm_true_dmg:.2f}')


def _xilian_support_skill(state, summoner, ms_unit):
    """此诗，献予一切生命: 非黄金裔→伤害+40%/2回合(对忆灵生效)。黄金裔→触发专属献予诗"""
    from engine.runtime import TimedBuff
    from engine.core.character_utils import is_gold_offspring
    from engine.characters import POEMS
    target = _select_xilian_target(state)
    if not target:
        return

    # 黄金裔: 触发专属献予诗（角色包 POEM 元组; 未录入→占位日志）
    if is_gold_offspring(target):
        poem = POEMS.get(target.char.id)
        if poem:
            poem[1](state, summoner, ms_unit, target)
            _record_xilian_e2_gift(state, target)
        else:
            state.log.append(f'  献予「?」之诗: {target.char.name}(占位, 待角色录入)')
        summoner.last_target_id = target.char.id
        return

    # 非黄金裔: 伤害+40%/2回合（该效果对其忆灵也生效）
    tb = TimedBuff(source_id=summoner.char.id, attributes={"DMG_BONUS_ALL": 40.0},
                   remaining_turns=2, source_name="此诗，献予一切生命")
    target.buffs.append(tb)
    if target.memsprite_unit and target.memsprite_unit.is_alive:
        ms_tb = TimedBuff(source_id=summoner.char.id, attributes={"DMG_BONUS_ALL": 40.0},
                          remaining_turns=2, source_name="此诗，献予一切生命")
        target.memsprite_unit.buffs.append(ms_tb)
    _record_xilian_e2_gift(state, target)
    summoner.last_target_id = target.char.id
    state.log.append(f'  此诗献予一切生命: {target.char.name}+40%伤害(2回合)')


def _xilian_memsprite_action(state, summoner, ms_unit):
    """德谬歌行动（实机: 玩家选择释放 花与箭/此诗献予, **每次选择释放扣12点追忆**,
    追忆不足12时玩家可不释放）:
    AI 近似——追忆≥12 时: 诗篇目标存在→【此诗献予】(优先级数据驱动: 未获诗黄金裔
    整局→单次, 见 characters.POEMS/_select_xilian_target), 否则【花与箭】; 扣12追忆。
    此诗献予不硬编码进昔涟AI（不同队伍选择原则有变化, 由诗表数据决定）。
    献予真我之诗: 故事≥3 → 消耗全部→额外回合自动花与箭（不扣追忆, 实机文本）。"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    # 献予真我之诗: 故事≥3 → 额外回合自动花与箭（优先于选择释放, 不扣追忆）
    if summoner.story_points >= 3:
        summoner.story_points = 0
        state.log.append('  献予真我之诗: 故事满3→额外回合+花与箭')
        rem._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")
        return
    if summoner.zhuiyi < 12:
        state.log.append(f'  德谬歌待机: 追忆{summoner.zhuiyi:.0f}<12, 暂不选择释放')
        return
    summoner.zhuiyi -= 12
    state.log.append(f'  追忆-12 → {summoner.zhuiyi:.0f}/27')
    target = _select_xilian_target(state)
    if target is not None:
        rem._use_memsprite_skill(state, summoner, ms_unit, "memsprite_support")
    else:
        rem._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _xl_ms_cast(u, state):
    """PHASE ms_cast_xilian: 德谬歌施放忆灵技→风堇天空层+2（含此诗献予路径）。"""
    fj = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
    if fj and 'poem_tiankong' in fj.extra:
        fj.extra['poem_tiankong'] = fj.extra.get('poem_tiankong', 0) + 2
        state.log.append(f'  献予「天空」之诗: 风堇+2层 ({fj.extra["poem_tiankong"]}层)')
    return None


def _xl_ms_support_cast(u, state, ms_unit, skill):
    """PHASE ms_support_cast: 此诗，献予一切生命（无倍率辅助技）。"""
    from engine.systems.remembrance import _dispatch_memsprite_support_events
    _xilian_support_skill(state, u, ms_unit)
    _dispatch_memsprite_support_events(state, u, skill)
    return True


def _xl_ms_bounce_extra(u, state, ms_unit, ms_stats, alive, skill_key):
    """PHASE ms_bounce_extra: 献予真我之诗弹射（E1 追忆/E4 叠层/E6 DEF-20%+全队拉条）。"""
    from engine.core.damage import calculate_damage
    from engine.core.combat_engine import _commit_enemy_damage, _effective_spd
    from engine.runtime import AV_PER_TURN, _enemy_for_damage
    from engine.characters.seele import _apply_luandie
    from engine.characters.trailblazer_remembrance import _apply_tbr_support
    if skill_key != "memsprite_basic":
        return None
    total = 0.0
    sources = u.extra.get('zhuiyi_sources', set())
    # E1: 真我之诗触发→+6追忆, 弹射次数+12
    if u.eidolon_rank >= 1:
        u.zhuiyi = min(27, u.zhuiyi + 6)
        state.log.append(f'  昔涟E1: 真我之诗+6追忆 → {u.zhuiyi:.0f}/27')
    # E4: 花与箭叠层(0-24), 弹射倍率+6%/层
    if u.eidolon_rank >= 4:
        stacks = min(24, u.extra.get('xilian_e4_stacks', 0) + 1)
        u.extra['xilian_e4_stacks'] = stacks
        state.log.append(f'  昔涟E4: 花与箭叠层+1 → {stacks}/24')
    e4_mult = 60.0 + 6.0 * u.extra.get('xilian_e4_stacks', 0)
    bounce_count = len(sources) + (12 if u.eidolon_rank >= 1 else 0)
    # E6: 献予触发计数→首次敌DEF-20%, 二次全队拉条24%
    if u.eidolon_rank >= 6:
        gift = state.extra.get('xilian_gift_count', 0) + 1
        state.extra['xilian_gift_count'] = gift
        if gift == 1:
            for e in state.enemies:
                e.DEF *= 0.80
            bp = state.extra.get('enemy_blueprint')
            if bp:
                bp.DEF *= 0.80  # 同步蓝图, 防波次重生还原
            state.log.append('  昔涟E6: 献予触发→敌方DEF-20%')
        elif gift == 2:
            from engine.characters.robin_summeretto import _guest_advance_blocked
            navs = state.extra.get('navs', {})
            for i, eu in enumerate(state.units):
                if eu.is_alive and i in navs \
                        and not _guest_advance_blocked(state, u, eu):
                    navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * 0.24)
            state.log.append('  昔涟E6: 献予触发2次→全队拉条24%')
    # v6.2.1: 复用共享逐段管线（Codex P1-2: 此前绕过 _enemy_for_damage/声援/击杀检测）
    for _ in range(bounce_count):
        alive_now = [e for e in alive if e.HP > 0]
        if not alive_now:
            break
        t = random.choice(alive_now)
        d = calculate_damage(
            ms_stats, _enemy_for_damage(t), ms_unit.max_hp, e4_mult,
            "direct", ms_unit.data.element or u.char.element,
            80, ms_stats.CRIT_RATE >= 0.5,
            skill_type="basic",
        crit_mode="expected")
        total += d.final_damage
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += _apply_tbr_support(state, u, t, d.final_damage)
        _apply_luandie(state, t)
        state.log.append(f'  献予真我之诗弹射: {d.final_damage:.0f}({e4_mult:.0f}%HP)')
    return total


def _xl_turn_tick(u, state):
    """PHASE turn_tick_rem: 结界独立倒计时，归零清 realm_true_dmg。"""
    ft = state.extra.get('xilian_field_turns', 0)
    if ft > 0:
        ft -= 1
        state.extra['xilian_field_turns'] = ft
        if ft <= 0:
            state.realm_true_dmg = 0
            state.log.append('  昔涟结界到期解除')
    return None


def _xl_ms_ai(u, state, ms_unit):
    """PHASE ms_ai: 德谬歌选择释放（花与箭/此诗献予, 扣12追忆）。"""
    _xilian_memsprite_action(state, u, ms_unit)
    return True


def _xl_ms_created(u, state, ms_unit):
    """PHASE ms_created: 等待，在所有的过去——双方HP上限+24%; 故事+1; 你好世界净化。"""
    ms_unit.max_hp = ms_unit.max_hp * 1.24
    ms_unit.current_hp = ms_unit.current_hp * 1.24
    ms_unit.base_stats.HP = ms_unit.max_hp
    u.base_stats.HP = u.base_stats.HP * 1.24
    u.max_hp = u.max_hp * 1.24
    u.current_hp = min(u.max_hp, u.current_hp)
    state.log.append(f'  等待，在所有的过去: 昔涟+德谬歌HP上限+24% (德谬歌HP={ms_unit.max_hp:.0f})')
    # 献予真我之诗: 德谬歌被召唤时+1故事
    u.story_points += 1
    state.log.append(f'  献予真我之诗: 德谬歌被召唤→故事+1 ({u.story_points}/3)')
    # v5.7 忆灵天赋·你好世界♪: 德谬歌被召唤时解除我方全体控制类负面状态
    from engine.systems.remembrance import _cleanse_controls
    _cleanse_controls(state)
    return None


PHASE_HOOKS['ms_created'] = _xl_ms_created


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
PHASE_HOOKS['ms_cast_xilian'] = _xl_ms_cast
PHASE_HOOKS['ms_support_cast'] = _xl_ms_support_cast
PHASE_HOOKS['ms_bounce_extra'] = _xl_ms_bounce_extra
PHASE_HOOKS['turn_tick_rem'] = _xl_turn_tick
PHASE_HOOKS['ms_ai'] = _xl_ms_ai
