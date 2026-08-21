"""遗器动态条件处理器 — 战斗中随状态变化重新评估的条件

与 _eval_relic_condition 的分工：
  - _eval_relic_condition（attributes.py）：入场前一次性判断
  - 本模块：战斗中动态判断（叠层、敌方状态依赖、buff触发等）

所有处理器通过 HookRegistry 在对应触发点注册。"""
from engine.hooks.base import HookRegistry


# ═══════════════════════════════════════════════════════════════
# 通用辅助
# ═══════════════════════════════════════════════════════════════

def _apply_timed_buff(u, state, stat_key, value, duration, target='self', source='遗器', param_id=''):
    from engine.core.combat_sim import TimedBuff
    targets = [u] if target == 'self' else [x for x in state.units if x.is_alive]
    # source_id 兼容 SimUnit（.char.id）/ MemSpriteUnit（.char=MemSprite, 用 summoner_id）
    src = getattr(u, 'char', None)
    source_id = getattr(src, 'id', None) or getattr(u, 'summoner_id', '')
    for t in targets:
        if param_id:
            # 同源刷新: 先移除旧层再挂新层（持续型效果, 实机文本无叠层描述时）
            t.buffs = [b for b in t.buffs if getattr(b, 'param_id', '') != param_id]
        tb = TimedBuff(source_id=source_id, attributes={stat_key: value},
                       remaining_turns=duration, source_name=source, param_id=param_id)
        t.buffs.append(tb)


def _once_per_battle(u, flag_name, fn, *args):
    """仅在首次触发时执行 fn"""
    flags = getattr(u, 'relic_flags', {}) or {}
    if flags.get(flag_name):
        return None
    flags[flag_name] = True
    u.relic_flags = flags
    return fn(*args)


# ═══════════════════════════════════════════════════════════════
# C类: 战斗中动态变化
# ═══════════════════════════════════════════════════════════════

def _elation_threshold_cd(u, state, **kw):
    """零号关卡朋克洛德: 欢愉度首次达40%/80%→CD+20%/32%"""
    el = u.base_stats.ELATION_LEVEL
    flags = getattr(u, 'relic_flags', {}) or {}
    if el >= 0.80 and not flags.get('elation_80'):
        u.base_stats.CRIT_DMG += 0.32
        flags['elation_80'] = flags['elation_40'] = True
        state.log.append('  零号关卡·欢愉度首次达80%→CD+32%')
    elif el >= 0.40 and not flags.get('elation_40'):
        u.base_stats.CRIT_DMG += 0.20
        flags['elation_40'] = True
        state.log.append('  零号关卡·欢愉度首次达40%→CD+20%')
    u.relic_flags = flags


def _hp_threshold_heal(u, state, **kw):
    """戍卫风雪的铁卫 4pc: 回合开始HP≤50%→回血8%MaxHP+5能量"""
    if u.current_hp / u.max_hp <= 0.50:
        heal = u.max_hp * 0.08
        u.current_hp = min(u.max_hp, u.current_hp + heal)
        u.current_energy = min(u.char.max_energy or 999, u.current_energy + 5)
        state.log.append(f'  铁卫·回血{heal:.0f}+5能量')


# ═══════════════════════════════════════════════════════════════
# D类: 事件触发+TimedBuff
# ═══════════════════════════════════════════════════════════════

def _on_ult_cd_buff(u, state, **kw):
    """密林卧雪的猎人 4pc: 终结技后→CD+25%/2回合"""
    _apply_timed_buff(u, state, 'CRIT_DMG', 25.0, 2, source='猎人4pc')

def _on_skill_atk_buff(u, state, **kw):
    """激奏雷电的乐队 4pc: 战技后→ATK+20%/1回合"""
    _apply_timed_buff(u, state, 'ATK_PERCENT', 20.0, 1, source='乐队4pc')

def _on_ult_team_spd_buff(u, state, **kw):
    """骇域漫游的信使 4pc: 终结技对队友→全队SPD+12%/1回合"""
    _apply_timed_buff(u, state, 'SPD_PERCENT', 12.0, 1, 'all_allies', source='信使4pc')

def _on_ult_team_be_buff(u, state, **kw):
    """机心戏梦的钟表匠 4pc: 终结技对队友→全队BE+30%/2回合"""
    _apply_timed_buff(u, state, 'BREAK_EFFECT', 30.0, 2, 'all_allies', source='钟表匠4pc')

def _on_skill_cd_buff(u, state, **kw):
    """重循苦旅的司铎 4pc: 战技/终结技对单体→目标CD+18%/2回合,叠2层"""
    _apply_timed_buff(u, state, 'CRIT_DMG', 18.0, 2, source='司铎4pc')

def _on_fua_atk_buff(u, state, **kw):
    """千星荟萃之城 2pc: 追加攻击→ATK+24%/2回合"""
    _apply_timed_buff(u, state, 'ATK_PERCENT', 24.0, 2, source='千星2pc')

def _on_ult_fire_dmg(u, state, **kw):
    """熔岩锻铸的火匠 4pc: 终结技后→下次攻击火伤+12%"""
    # 简化：施加1回合火伤buff（实际应为"下一次攻击"而非1回合）
    _apply_timed_buff(u, state, 'DMG_BONUS_FIRE', 12.0, 1, source='火匠4pc')

def _on_ult_next_skill_dmg(u, state, **kw):
    """识海迷坠的学者 4pc: 终结技后→下次战技DMG+25%"""
    # 简化：施加1回合战技增伤buff
    _apply_timed_buff(u, state, 'DMG_BONUS_SKILL', 25.0, 1, source='学者4pc')

def _on_memosprite_atk_cd(u, state, **kw):
    """凯歌祝捷的英豪 4pc: 忆灵攻击时→自身+忆灵CD+30%/2回合
    v5.6.1: 实机文本"lasting for 2 turns"无叠层描述→同源刷新（原实现每次触发叠新层, 面板无限膨胀）;
    实机同时作用于忆灵（"wearer's and memosprite's"）"""
    _apply_timed_buff(u, state, 'CRIT_DMG', 30.0, 2, source='英豪4pc', param_id='yinghao4_cd')
    if u.memsprite_unit:
        _apply_timed_buff(u.memsprite_unit, state, 'CRIT_DMG', 30.0, 2, source='英豪4pc', param_id='yinghao4_cd')


# ═══════════════════════════════════════════════════════════════
# E类: 叠层机制
# ═══════════════════════════════════════════════════════════════

def _stack_atk_on_hit(u, state, **kw):
    """街头出身的拳王 4pc: 攻击/受击→ATK+5%/层,上限5层,战斗永久"""
    st = getattr(u, 'relic_stacks', {}) or {}
    cur = st.get('拳王', 0)
    if cur < 5:
        cur += 1
        u.base_stats.ATK += u.base_stats._base_ATK * 0.05
        st['拳王'] = cur
        u.relic_stacks = st

def _stack_atk_on_fua_hit(u, state, **kw):
    """毁烬焚骨的大公: 追加攻击每段伤害→ATK+6%,上限8层,3回合,下次追加攻击重置"""
    st = getattr(u, 'relic_stacks', {}) or {}
    cur = st.get('大公', 0)
    if cur < 8:
        cur += 1
        u.base_stats.ATK += u.base_stats._base_ATK * 0.06
        st['大公'] = cur
        u.relic_stacks = st

def _stack_cr_on_hit(u, state, **kw):
    """宝命长存的莳者 4pc: 受击/耗血→CR+8%/层,上限2层,每层2回合"""
    _apply_timed_buff(u, state, 'CRIT_RATE', 8.0, 2, source='莳者4pc')

def _stack_merit_on_fua(u, state, char_id=None, **kw):
    """奔狼的都蓝王朝 2pc: 我方角色追加攻击→Merit叠层(上限5),每层FUA+5%,5层时FUA暴伤+25%
    v5.2: 广播事件 u=追加执行者, 持有者由 char_id 定位（问题2 架构修复）
    v5.6: 每层FUA+5%接线(DMG_BONUS_BY_ATTACK_TYPE); 5层CD+25%收窄为FUA限定
    (CRIT_DMG_BY_ATTACK_TYPE), 层数驱动重算, 掉层对称回收;
    实机文本"我方角色"(ally character) 含装备者自身, 故执行者=持有者时也叠层"""
    holder = next((x for x in state.units
                   if x.char.id == char_id and x.is_alive), None)
    if holder is None:
        return
    st = getattr(holder, 'relic_stacks', {}) or {}
    cur = st.get('Merit', 0)
    if cur < 5:
        cur += 1
        st['Merit'] = cur
        holder.relic_stacks = st
        state.log.append(f'  都蓝王朝·Merit {cur}/5')
    # v5.6: 层数驱动的加成重算（幂等; 每层FUA+5% / 5层FUA暴伤+25%）
    holder.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] = 0.05 * cur
    holder.base_stats.CRIT_DMG_BY_ATTACK_TYPE['follow_up'] = 0.25 if cur == 5 else 0.0
    if cur == 5:
        state.log.append('  都蓝王朝·5层Merit→FUA暴伤+25%')

def _on_skill_ult_stack_dmg(u, state, **kw):
    """星如我见的领航员 4pc: 入场/战技→战技终结技+18%叠3层; 回合开始/终结技→掉1层"""
    st = getattr(u, 'relic_stacks', {}) or {}
    cur = st.get('领航员', 0)
    skill_key = kw.get('skill_key', '')
    # 增益：入场(enter_battle)或战技时叠层
    is_gain = (skill_key == 'skill') or (kw.get('trigger_type') == 'enter_battle')
    # 衰减：回合开始(turn_start)或终结技时掉层
    is_loss = (skill_key == 'ultimate') or (kw.get('trigger_type') == 'turn_start')

    if is_gain and cur < 3:
        cur += 1
        u.base_stats.DMG_BONUS_BY_SKILL_TYPE['skill'] = u.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('skill', 0) + 0.18
        u.base_stats.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = u.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('ultimate', 0) + 0.18
        st['领航员'] = cur
        u.relic_stacks = st
        state.log.append(f'  领航员·叠{cur}层→战技/终结技+{cur*18}%')
    elif is_loss and cur > 0:
        u.base_stats.DMG_BONUS_BY_SKILL_TYPE['skill'] = u.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('skill', 0) - 0.18
        u.base_stats.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = u.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('ultimate', 0) - 0.18
        cur -= 1
        st['领航员'] = cur
        u.relic_stacks = st


# ═══════════════════════════════════════════════════════════════
# F类: 敌方状态依赖（在eff_stats或_build_effective_stats中处理）
# ═══════════════════════════════════════════════════════════════

def _defpen_per_dot(u, state, **kw):
    """幽锁深牢的系囚 4pc: 敌方每1个DoT→无视6%DEF,上限3个"""
    # 由 _build_effective_stats 读取 enemy 状态后处理
    pass

def _cd_per_debuff(u, state, **kw):
    """死水深潜的先驱 4pc: 敌方≥2/3debuff→CD+8%/12%,上debuff后翻倍1回合"""
    pass

def _cr_vs_debuff(u, state, **kw):
    """盗匪荒漠的废土客 4pc: 敌方有debuff→CR+10%,禁锢→CD+20%"""
    pass


# ═══════════════════════════════════════════════════════════════
# G类: 出场/入场一次性触发
# ═══════════════════════════════════════════════════════════════

def _enter_combat_sp(u, state, **kw):
    """云无留迹的过客 4pc: 开局回1SP"""
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  过客4pc·开局回1SP')

def _enter_combat_advance(u, state, **kw):
    """生命的翁瓦克: SPD≥120→开局行动提前40%"""
    if u.base_stats.SPD >= 120:
        from engine.core.combat_sim import _effective_spd
        AV_PER_TURN = 10000.0
        advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.40
        navs = state.extra.get('navs', {})
        for i, eu in enumerate(state.units):
            if eu == u and i in navs:
                navs[i] = max(0, navs[i] - advance)
                break
        else:
            # on_enter_battle 在初始行动表建立前触发。保留比例，避免
            # 开局拉条因 navs 尚未存在而静默失效。
            u.extra['initial_action_advance_ratio'] = max(
                u.extra.get('initial_action_advance_ratio', 0.0), 0.40
            )
        state.log.append('  翁瓦克·开局拉条40%')

def _weakness_break_energy(u, state, **kw):
    """流星追迹的怪盗 4pc: 击破弱点→回3能量"""
    u.current_energy = min(u.char.max_energy or 999, u.current_energy + 3)

def _first_elation_skill_buff(u, state, **kw):
    """应天涉远的卜者 4pc: 每场战斗首次欢愉技→全队欢愉度+10%"""
    def _apply():
        for eu in state.units:
            if eu.is_alive:
                eu.base_stats.ELATION_LEVEL += 0.10
        state.log.append('  卜者4pc·首次欢愉技→全队欢愉度+10%')
    _once_per_battle(u, 'first_elation_done', _apply)

def _on_kill_team_cd(u, state, **kw):
    """千星荟萃之城 4pc: 击杀→全队CD+12%(战斗永久,不可叠)"""
    def _apply():
        for eu in state.units:
            if eu.is_alive:
                eu.base_stats.CRIT_DMG += 0.12
        state.log.append('  千星4pc·击杀→全队CD+12%')
    _once_per_battle(u, 'kill_cd_applied', _apply)


# ═══════════════════════════════════════════════════════════════
# H类: 状态/buff依赖
# ═══════════════════════════════════════════════════════════════

def _gentle_rain_on_heal(u, state, **kw):
    """烈阳惊雷的女武神 2pc/4pc: 治疗队友→Gentle Rain(2回合)→SPD+6%+全队CD+15%"""
    # 简化：给自身加Gentle Rain标记buff
    _apply_timed_buff(u, state, 'SPD_PERCENT', 6.0, 2, source='女武神·GentleRain')
    for eu in state.units:
        if eu.is_alive and eu != u:
            _apply_timed_buff(eu, state, 'CRIT_DMG', 15.0, 2, source='女武神·GentleRain')

def _help_gain_on_targeted(u, state, char_id=None, target=None, **kw):
    """恶海逐波的船长 4pc: 被队友单体技能选中→Help叠层(上限2)
    v5.6: on_ally_skill_targeted 广播触发（u=施放者, target=被选中者, char_id=持有者）;
    实机"another ally target's ability"——被选中的必须是持有者, 且施放者≠持有者"""
    if target is None or target is u:
        return  # 无目标引用, 或施放者选中自己（"another ally" 语义不叠）
    if kw.get('skill_key') == 'ultimate':
        return  # 终结技不叠层
    holder = next((x for x in state.units if x.char.id == char_id and x.is_alive), None)
    if holder is None or target is not holder:
        return  # 被选中的不是持有者
    st = getattr(holder, 'relic_stacks', {}) or {}
    cur = st.get('Help', 0)
    if cur < 2:
        st['Help'] = cur + 1
        holder.relic_stacks = st
        state.log.append(f'  船长·被{u.char.name}选中→Help {cur + 1}/2')


def _help_consume_on_ult(u, state, **kw):
    """恶海逐波的船长 4pc: 终结技时2层→消耗全部,ATK+48%/1回合
    on_after_skill per-char 触发（u=装备者自己开大）"""
    if kw.get('skill_key') != 'ultimate':
        return
    st = getattr(u, 'relic_stacks', {}) or {}
    if st.get('Help', 0) >= 2:
        st['Help'] = 0
        u.relic_stacks = st
        _apply_timed_buff(u, state, 'ATK_PERCENT', 48.0, 1, source='船长·Help消费')
        state.log.append('  船长·消费2层Help→ATK+48%')

def _shield_ally_cd(u, state, **kw):
    """自匿星芒的隐士 4pc: 持盾队友→CD+15%
    （v5.0 P5: 由 combat_sim 护盾施加分支内联激活, 本函数废弃保留防断链）"""
    pass

def _comburent_team_dmg(u, state, **kw):
    """叩问天工的名冶 4pc: Comburent→全队DMG+15%"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.DMG_BONUS_ALL += 0.15
    state.log.append('  名冶4pc·Comburent→全队DMG+15%')

def _memosprite_field_buff(u, state, **kw):
    """再创天地的救世主 4pc: 普攻/战技后+忆灵在场→HP+24%+全队DMG+15%,持续至下次普攻/战技"""
    _apply_timed_buff(u, state, 'HP_PERCENT', 24.0, 2, source='救世主4pc')
    for eu in state.units:
        if eu.is_alive:
            _apply_timed_buff(eu, state, 'DMG_BONUS_ALL', 15.0, 2, source='救世主4pc')

def _ally_count_dmg_bonus(u, state, **kw):
    """妖精织梦的乐园 2pc: 队友数≠4→每多1个+9%(上限4层),每少1个+12%(上限3层)"""
    alive = len([x for x in state.units if x.is_alive])
    if alive != 4:
        if alive > 4:
            stacks = min(alive - 4, 4)
            bonus = stacks * 0.09
            u.base_stats.DMG_BONUS_ALL += bonus
        else:
            stacks = min(4 - alive, 3)
            bonus = stacks * 0.12
            u.base_stats.DMG_BONUS_ALL += bonus


# ═══════════════════════════════════════════════════════════════
# 动态条件注册表
# ═══════════════════════════════════════════════════════════════

DYNAMIC_RELIC_REGISTRY = {
    # C类: 动态变化
    "elation_level_40_80_cd":    {"trigger": "on_turn_start",     "action": _elation_threshold_cd,       "source_name": "零号关卡·欢愉阈值CD"},
    "hp_threshold_heal":         {"trigger": "on_turn_start",     "action": _hp_threshold_heal,           "source_name": "铁卫·低血回复"},
    "be_threshold_defpen":       {"trigger": None,                "action": None,                         "source_name": "铁骑·击破DEF穿透"},
    # D类: 事件触发+TimedBuff
    "on_ult_cd_buff":            {"trigger": "on_ultimate",       "action": _on_ult_cd_buff,              "source_name": "猎人·终结技CD"},
    "on_skill_atk_buff":         {"trigger": "on_skill",          "action": _on_skill_atk_buff,           "source_name": "乐队·战技ATK"},
    "on_ult_team_spd_buff":      {"trigger": "on_ultimate",       "action": _on_ult_team_spd_buff,        "source_name": "信使·全队SPD"},
    "on_ult_team_be_buff":       {"trigger": "on_ultimate",       "action": _on_ult_team_be_buff,         "source_name": "钟表匠·全队BE"},
    "on_skill_single_cd_buff":   {"trigger": "on_skill",          "action": _on_skill_cd_buff,            "source_name": "司铎·战技CD"},
    "on_fua_atk_buff":           {"trigger": "on_followup",       "action": _on_fua_atk_buff,             "source_name": "千星2pc·追加ATK（v5.2: 仅真追加攻击触发）"},
    "on_ult_fire_dmg":           {"trigger": "on_ultimate",       "action": _on_ult_fire_dmg,             "source_name": "火匠·终结技火伤"},
    "on_ult_next_skill_dmg":     {"trigger": "on_ultimate",       "action": _on_ult_next_skill_dmg,       "source_name": "学者·终结技→战技"},
    "on_memosprite_atk_cd":      {"trigger": "on_memsprite_attack","action": _on_memosprite_atk_cd,       "source_name": "英豪·忆灵CD（v5.2: 忆灵技能结算触发）"},
    # E类: 叠层
    "stack_atk_on_hit":          {"trigger": None,                "action": None,                         "source_name": "拳王·攻击叠ATK（v4.5 在 _apply_hit 内联）"},
    "stack_atk_on_fua":          {"trigger": "on_followup_hit",   "action": _stack_atk_on_fua_hit,        "source_name": "大公·追加逐段叠ATK"},
    "stack_cr_on_hit":           {"trigger": None,                "action": None,                         "source_name": "莳者·受击叠CR（v4.5 在 _apply_hit 内联）"},
    "stack_merit_on_fua":        {"trigger": "on_followup",       "action": _stack_merit_on_fua,          "source_name": "都蓝王朝·Merit叠层（v5.2广播; v5.6: 含自身FUA+每层FUA增伤接线）"},
    "on_skill_ult_stack_dmg":    {"trigger": "on_after_skill",    "action": _on_skill_ult_stack_dmg,      "source_name": "领航员·战技终结技叠层"},
    # F类: 敌方状态（标记，在eff_stats实现）
    "defpen_per_dot":             {"trigger": None,               "action": None,                          "source_name": "系囚·DoT→DEF穿透"},
    "cd_per_debuff_count":        {"trigger": None,               "action": None,                          "source_name": "先驱·debuff→CD"},
    "cr_vs_debuff":               {"trigger": None,               "action": None,                          "source_name": "废土客·debuff→CR/CD"},
    "fua_dmg_bonus":              {"trigger": None,               "action": None,                          "source_name": "大公2pc·追加增伤"},
    # G类: 一次性触发
    "enter_combat_sp_recovery":   {"trigger": "on_enter_battle",  "action": _enter_combat_sp,              "source_name": "过客·开局回SP"},
    "enter_combat_action_advance":{"trigger": "on_enter_battle",  "action": _enter_combat_advance,         "source_name": "翁瓦克·开局拉条"},
    "on_weakness_break_energy":   {"trigger": "on_weakness_break","action": _weakness_break_energy,         "source_name": "怪盗·击破回能"},
    "first_elation_skill_buff":   {"trigger": "on_elation_skill", "action": _first_elation_skill_buff,     "source_name": "卜者4pc·首次欢愉技"},
    "on_kill_team_cd":            {"trigger": "on_kill",          "action": _on_kill_team_cd,              "source_name": "千星4pc·击杀CD"},
    # H类: 状态/buff依赖
    "gentle_rain_buff":           {"trigger": "on_heal",          "action": _gentle_rain_on_heal,          "source_name": "女武神·GentleRain（v5.2: 治疗结算触发）"},
    "help_stack_gain":            {"trigger": "on_ally_skill_targeted", "action": _help_gain_on_targeted,  "source_name": "船长·Help叠层（v5.6: 被队友单体技能选中触发, 实机语义）"},
    "help_stack_consume":         {"trigger": "on_after_skill",   "action": _help_consume_on_ult,         "source_name": "船长·Help消费（终结技2层→ATK+48%）"},
    "shield_ally_cd":             {"trigger": None,               "action": None,                          "source_name": "隐士4pc·持盾CD（v5.0 P5 盾分支内联）"},
    "shield_effect_bonus":        {"trigger": None,               "action": None,                          "source_name": "隐士2pc·护盾效果（静态属性已在 attributes 层生效）"},
    "comburent_team_dmg":         {"trigger": "on_enter_battle",  "action": _comburent_team_dmg,           "source_name": "名冶·Comburent增伤"},
    "on_basic_skill_memosprite_buff":{"trigger": "on_after_skill","action": _memosprite_field_buff,        "source_name": "救世主·忆灵buff"},
    "ally_count_dmg_bonus":       {"trigger": "on_enter_battle",  "action": _ally_count_dmg_bonus,         "source_name": "乐园·队友数增伤"},
    # 静态条件（_eval_relic_condition处理）
    "spd_threshold_120_atk":      {"trigger": None, "action": None, "source_name": "太空站·SPD→ATK"},
    "spd_threshold_120_team_atk": {"trigger": None, "action": None, "source_name": "仙舟·SPD→团队ATK"},
    "ehr_to_atk_capped":          {"trigger": None, "action": None, "source_name": "商业公司·EHR→ATK"},
    "ehr_threshold_50_def":       {"trigger": None, "action": None, "source_name": "贝洛伯格·EHR→DEF"},
    "cd_threshold_first_atk_cr":  {"trigger": None, "action": None, "source_name": "差分机·CD→首击CR"},
    "enter_combat_faction_cd":    {"trigger": None, "action": None, "source_name": "坠星·阵营CD"},
    "enter_combat_energy_to_dmg": {"trigger": None, "action": None, "source_name": "生研院·能量→DMG"},
    "cr_threshold_basic_skill_dmg":{"trigger": None, "action": None, "source_name": "繁星·CR→普攻战技"},
    "spd_threshold_dmg_bonus":    {"trigger": None, "action": None, "source_name": "格拉默·SPD→DMG"},
}


def register_dynamic_relic_effects(registry: HookRegistry, char_id: str, condition_str: str):
    """将遗器条件注册到 HookRegistry。支持 + 分隔的复合条件。"""
    if not condition_str:
        return
    for cond in condition_str.split('+'):
        cond = cond.strip()
        if not cond or cond not in DYNAMIC_RELIC_REGISTRY:
            continue
        tmpl = DYNAMIC_RELIC_REGISTRY[cond]
        trigger = tmpl.get("trigger")
        action = tmpl.get("action")
        if not trigger or not action:
            continue
        registry.register(
            character_id=char_id, event=trigger,
            action=action, condition=tmpl.get("condition"),
            source="relic", source_name=tmpl.get("source_name", cond),
        )
