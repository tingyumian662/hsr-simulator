"""真珠——五星·冰·欢愉（v7.26.0 录入, 角色技能介绍/欢愉/真珠.txt）

机制映射（项目主裁决 2026-09-29 五则均落地）:
- 【好活当赏】点池: 一切来源经系统 grant_good_show, goodshow_zz 观察相位把持续
  时间改写为 -1(无限批次, 模型 is_expired 仅 0 判过期); 持有上限50在同步点截断
  (_zz_sync_pool, 阿哈转化等外部授予超上限部分在最近同步点回收); 抵御值消耗走
  models.consume_good_show(从最新批次扣)
- 【深度学习】/【美学底本】: 终结技指定底本(AI=首个非自身欢愉角色, 兜底首个非自身
  队友), 3点充能(秘技2点); 强化普攻 key_rewrite 三选一(临摹断水/绘制末浪/幻造星月
  -底本欢愉时); 按欢愉数拉条 10/15/30%(嘉宾封锁守卫); ≥4欢愉→底本 X 轴正半轴额外
  回合(extra_turns 队列, kind='zz_base'), extra_turn_start/end 观察相位精确给予/
  收回: 底本+30好活+60笑点(E2×2), 笑点经 gain_laugh 实时计入银狼隐藏分, 收回只扣
  发出的份额、不碰隐藏分; 临时好活经 consume 收回
- 欢愉技 rider: 按欢愉数 1/2/3/4+→10/15/20/40%(E4×2)给全队挂"下次攻击后追加对应
  属性欢愉伤害"标记, on_attack_action 广播消费(元素随攻击者)
- 天赋抵御值: _zz_absorb_check(先半血DR30%再耗好活挡60%, 每点=200)挂
  _distribute_damage; E1 致命保命2次挂 _check_fatal 第四环(目标保留50%生命)
- 行迹1 艺术壁垒: 底本(欢愉)下次终结技→真珠固定位+90能量(after_ult 观察相位),
  入场+每次真珠终结技后重新武装
- 行迹2 感知容差: 任意我方常规回合开始+5好活(ally_turn_start 观察相位, 本轮上限
  50, 真珠自身回合开始时重置计数); 持好活→全队 EFFECT_RES+50(base_stats 对称开关);
  强化普攻/战技净化走 JSON cleanse 效果行(引擎通用管线, 逐目标解1)
- 行迹3 洞察万物: eff_stats_zz 相位 DEF≥2400 欢愉度+32%、每超100点+3%(超量封顶
  3600); 同相位 HEAL_BONUS += ELATION_LEVEL×20%(灵砂通道)
- E6: 深度学习期间全队 RES_PEN_ALL+20(base_stats 对称开关); 强化普攻后追加240%
  冰属性欢愉伤害(使用底本属性值计算)
"""
import copy as _copy

from engine.core.combat_engine import (
    _build_effective_stats,
    _commit_enemy_damage,
    _effective_spd,
    _gain_energy,
    _skill_level_factor,
)
from engine.core.damage import calculate_damage
from engine.models.character import SkillMultiplier
from engine.runtime import AV_PER_TURN, _enemy_for_damage, _set_av
from engine.systems.elation import gain_laugh

CHAR_ID = "zhenzhu"
ELEMENT = "冰"
TRACE1 = "zhenzhu_art_barrier"
TRACE2 = "zhenzhu_perception_tolerance"
TRACE3 = "zhenzhu_insight_all"

GOODSHOW_CAP = 50.0        # 天赋: 持有上限
WARD_PER_POINT = 200.0     # 天赋: 每点好活=200抵御值
BLOCK_RATIO = 0.60         # 天赋: 抵挡60%
LOW_HP_DR = 0.30           # 天赋满级: ≤50%生命受伤降低30%
RIDER_SCALES = {1: 10.0, 2: 15.0, 3: 20.0}   # 欢愉数→rider%(4+取40)


def _has_trace(u, hook_name):
    return any(getattr(t, "hook_name", "") == hook_name for t in (u.char.traces or []))


def _me(state):
    return next((x for x in state.units if x.char.id == CHAR_ID and x.is_alive), None)


def _pool(state):
    return state.elation_state.get_good_show_total(CHAR_ID)


def _el_sys(state):
    from engine.systems.elation import ElationSystem
    return state.extra.get("_elation") or ElationSystem()


# ---- 好活点池 ----

def _zz_gain(u, state, n, cause=""):
    """好活点池获得(经系统统一入口, 获得瞬间按上限50截断——裁决9 goodshow_cap)。"""
    n = float(n)
    if n <= 0:
        return
    before = _pool(state)
    _el_sys(state).grant_good_show(state, CHAR_ID, n, duration=-1,
                                   source=cause or CHAR_ID)
    _zz_sync_pool(state)
    after = _pool(state)
    if after > before:
        state.log.append(f"  真珠【好活当赏】+{after - before:g} ({cause}) "
                         f"→ {after:g}/{GOODSHOW_CAP:g}")


def _zz_consume(state, n):
    return state.elation_state.consume_good_show(CHAR_ID, n)


def _zz_sync_pool(state):
    """池同步: 行迹2 持好活→全队EFFECT_RES+50 对称开关(上限截断已由 goodshow_cap
    在获得瞬间完成——裁决9, 不再在此裁减)。"""
    me = _me(state)
    if me is None:
        if state.extra.pop("zz_t2_res_on", False):
            for t in state.units:
                t.base_stats.EFFECT_RES -= 0.50
        return
    want = _has_trace(me, TRACE2) and _pool(state) > 0
    on = state.extra.get("zz_t2_res_on", False)
    if want and not on:
        for t in state.units:
            if t.is_alive:
                t.base_stats.EFFECT_RES += 0.50
        state.extra["zz_t2_res_on"] = True
        state.log.append("  行迹2·感知容差: 持好活→全队效果抵抗+50%")
    elif not want and on:
        for t in state.units:
            t.base_stats.EFFECT_RES -= 0.50
        state.extra["zz_t2_res_on"] = False


# ---- 天赋: 抵御值吸收（_distribute_damage 函数级延迟导入）----

def _zz_absorb_check(state, target, amount):
    """先半血以下DR30%, 再消耗好活抵御值抵挡60%(每点=200, 不足折算)。
    返回折减后伤害; 真珠不在场/无行迹判定外(天赋本体, 行迹2只有RES/好活/净化)。"""
    me = _me(state)
    if me is None:
        return amount
    if target.current_hp / target.max_hp <= 0.50:
        amount *= (1.0 - LOW_HP_DR)
    block = amount * BLOCK_RATIO
    if block <= 0:
        return amount
    pts = min(block / WARD_PER_POINT, _pool(state))
    blocked = pts * WARD_PER_POINT
    if blocked > 0:
        _zz_consume(state, pts)
        _zz_sync_pool(state)
        amount -= blocked
        state.log.append(f"  真珠抵御值: 耗{pts:g}好活挡{blocked:.0f} "
                         f"({target.char.name}受伤→{amount:.0f})")
    return amount


# ---- E1 致命保命（_check_fatal 第四环）----

def _zz_e1_fatal_check(state, target):
    """E1: 致命伤害不死亡, 立即回复50%生命上限, 单场2次。"""
    me = _me(state)
    if me is None or me.eidolon_rank < 1:
        return False
    charges = state.extra.get("zz_e1_charges", 0)
    if charges <= 0:
        return False
    state.extra["zz_e1_charges"] = charges - 1
    target.current_hp = target.max_hp * 0.50
    state.log.append(f"  真珠E1·珍珠藏在海的留白处: {target.char.name}保留50%生命 "
                     f"(剩余{charges - 1}次)")
    return True


# ---- 深度学习 / 美学底本 ----

def _zz_pick_base(state, me):
    cands = [x for x in state.units if x is not me and x.is_alive]
    elation = [x for x in cands if x.char.path == "欢愉"]
    return (elation or cands)[0] if (elation or cands) else None


def _zz_advance(state, target, ratio, label):
    """行动提前(开拓者终结技同型, 特邀嘉宾封锁守卫)。"""
    from engine.characters.robin_summeretto import _guest_advance_blocked
    me = _me(state)
    if me is None or target is None:
        return
    if _guest_advance_blocked(state, me, target):
        return
    navs = state.extra.get("navs", {})
    idx = next((i for i, x in enumerate(state.units) if x is target), None)
    if idx is None or idx not in navs:
        return
    advanced_av = max(
        state.current_av,
        navs[idx] - (AV_PER_TURN / max(_effective_spd(target, state), 1.0)) * ratio,
    )
    _set_av(state, navs, idx, advanced_av)
    state.log.append(f"  真珠{label}→{target.char.name}: 行动提前{ratio * 100:g}%")


def _zz_start_dl(u, state):
    """终结技: 获得20好活 + 指定底本 + 拉条 + ≥4欢愉额外回合 + 行迹1重臂 + E6同步。"""
    base = _zz_pick_base(state, u)
    u.extra["zz_dl"] = True
    u.extra["zz_dl_charges"] = 3
    u.extra["zz_base"] = base
    _zz_gain(u, state, 20, cause="终结技·鉴映灵魂的底色")
    n_el = sum(1 for x in state.units if x.is_alive and x.char.path == "欢愉")
    ratio = {1: 0.10, 2: 0.15}.get(n_el, 0.30)
    if base is not None:
        _zz_advance(state, base, ratio, "终结技")
        if u.eidolon_rank >= 2:
            others = [x for x in state.units if x.is_alive
                      and x.char.path == "欢愉" and x is not u and x is not base]
            for x in others:
                _zz_advance(state, x, ratio, "E2终结技")
    if n_el >= 4 and base is not None:
        queued = any(x is base for x, _k in state.extra.get("extra_turns", []))
        if not queued:
            state.extra.setdefault("extra_turns", []).append((base, "zz_base"))
            state.log.append(f"  真珠终结技: 【美学底本】{base.char.name}获得1个额外回合(4欢愉)")
    u.extra["zz_trace1_armed"] = True
    _zz_sync_e6(state)
    _fire_ally_targeted(state, u, base)


def _fire_ally_targeted(state, u, target):
    """终结技指定单体友方→补发 on_ally_skill_targeted(戏梦点星的伶人4pc等消费;
    通用管线按 single_ally 效果行派发, 真珠底本由模块选定, 故此处带正确目标补发)。"""
    if target is None or target is u:
        return
    state.hooks.trigger_all("on_ally_skill_targeted", u=u, state=state,
                            target=target, skill_key="ultimate")


def _zz_dl_consume(u, state):
    """每次强化普攻消耗1点充能; 归零即结束深度学习(行动后语义等价)。"""
    ch = u.extra.get("zz_dl_charges", 0) - 1
    u.extra["zz_dl_charges"] = ch
    if ch <= 0:
        u.extra["zz_dl"] = False
        _zz_sync_e6(state)
        state.log.append("  【深度学习】: 充能耗尽, 结束")


def _zz_sync_e6(state):
    """E6: 深度学习期间全队 RES_PEN_ALL+20(base_stats 对称开关; 阵亡时回收)。"""
    me = _me(state)
    if me is None:
        if state.extra.pop("zz_e6_on", False):
            for t in state.units:
                t.base_stats.RES_PEN_ALL -= 0.20
            state.log.append("  真珠阵亡: E6 全队抗穿加成回收")
        return
    want = me.eidolon_rank >= 6 and bool(me.extra.get("zz_dl"))
    on = state.extra.get("zz_e6_on", False)
    if want and not on:
        for t in state.units:
            if t.is_alive:
                t.base_stats.RES_PEN_ALL += 0.20
        state.extra["zz_e6_on"] = True
        state.log.append("  E6·用一副躯壳演算生命: 深度学习期间全队全属性抗性穿透+20%")
    elif not want and on:
        for t in state.units:
            t.base_stats.RES_PEN_ALL -= 0.20
        state.extra["zz_e6_on"] = False


# ---- 额外回合临时给予/收回（extra_turn_start/end 观察相位）----

def _zz_extra_turn_start(unit, state, kind=None, **kw):
    if kind != "zz_base" or unit is None or getattr(unit, "char", None) is None:
        return None
    me = _me(state)
    if me is None:
        return None
    mult = 2.0 if me.eidolon_rank >= 2 else 1.0
    gs, lp = 30.0 * mult, 60.0 * mult
    _el_sys(state).grant_good_show(state, unit.char.id, gs, duration=2,
                                   source="zz_extra_turn")
    state.extra["zz_temp_gs"] = (unit.char.id, gs)
    gain_laugh(state, lp)  # 实时叠加: 授予瞬间即计入银狼隐藏分
    state.extra["zz_temp_laugh"] = lp
    state.log.append(f"  【美学底本】{unit.char.name}额外回合开始: "
                     f"+{gs:g}好活+{lp:g}笑点(回合结束收回)")
    return None


def _zz_extra_turn_end(unit, state, kind=None, **kw):
    if kind != "zz_base":
        return None
    rec = state.extra.pop("zz_temp_gs", None)
    if rec is not None:
        state.elation_state.consume_good_show(rec[0], rec[1])
    lp = state.extra.pop("zz_temp_laugh", 0.0)
    if lp > 0:
        # 裁决5: 只扣发出的份额, 池内其余笑点不动; 绝不碰银狼隐藏分
        state.laugh_points = max(0.0, state.laugh_points - lp)
    if rec is not None or lp > 0:
        state.log.append(f"  【美学底本】{getattr(getattr(unit, 'char', None), 'name', '?')}"
                         f"额外回合结束: 收回临时好活/笑点({lp:g}笑点)")
    return None


# ---- 欢愉技 rider ----

def _zz_elation_arm(u, state, skill_key):
    """SKILL_HOOKS: 欢愉技施放→按欢愉数给全队挂"下次攻击后追伤"标记(E4×2)。"""
    if u.char.id != CHAR_ID or skill_key != "elation_skill":
        return
    n_el = sum(1 for x in state.units if x.is_alive and x.char.path == "欢愉")
    scale = RIDER_SCALES.get(n_el, 40.0)
    if u.eidolon_rank >= 4:
        scale *= 2.0
    for t in state.units:
        if t.is_alive:
            t.extra["zz_rider_scale"] = scale
    state.log.append(f"  真珠欢愉技·渲染理性: 全队下次攻击后追加{scale:g}%对应属性欢愉伤害")


def _zz_on_attack(u, state, dealt=True, **ctx):
    """on_attack_action 广播(u=行动者): 消费 rider 标记, 追加对应属性欢愉伤害。"""
    if not dealt:
        return
    scale = u.extra.pop("zz_rider_scale", None)
    if scale is None or getattr(u, "char", None) is None:
        return
    if u not in state.units:
        return
    hits = [t for t in (state.extra.get("last_attack_targets") or [])
            if t is not None and getattr(t, "HP", 0) > 0]
    if not hits:
        hits = state.alive_enemies()
    if not hits:
        return
    stats = _build_effective_stats(u, state)
    laugh_n = state.elation_state.get_good_show_total(u.char.id)
    total = 0.0
    for t in hits:
        d = calculate_damage(stats, _enemy_for_damage(t), 0.0, scale, "elation",
                             u.char.element, 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, crit_mode="expected")
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    if total > 0:
        u.total_damage_dealt += total
        state.log.append(f"  真珠rider: {u.char.name}攻击后追加{total:.0f}"
                         f"{u.char.element}属性欢愉伤害({scale:g}%)")


# ---- 相位处理器 ----

def _zz_key_rewrite(u, state, skill_key):
    """PHASE key_rewrite: 深度学习期间普攻三选一(底本欢愉→幻造星月);
    绘制末浪耗1SP, SP不足时回落普通普攻(否则单人局无行动死锁)。"""
    if u.char.id != CHAR_ID or skill_key != "basic_attack":
        return None
    if not u.extra.get("zz_dl") or u.extra.get("zz_dl_charges", 0) <= 0:
        return None
    base = u.extra.get("zz_base")
    if base is not None and base.is_alive and base.char.path == "欢愉":
        return "zz_basic_dream"
    if state.skill_points >= 1:
        return "basic_attack_enhanced"
    return None


def _zz_energy_override(u, state, skill_key):
    """PHASE energy_gain_override: 强化普攻(两形态)回能30。
    (欢愉技回能5已入 ENERGY_GAIN 通用表, v7.26.2 裁决7)"""
    if u.char.id != CHAR_ID:
        return None
    if skill_key in ("basic_attack_enhanced", "zz_basic_dream"):
        return 30.0
    return None


def _zz_skill_adjust_post(u, state, skill=None, skill_key=None, **kw):
    """PHASE skill_adjust_post: 幻造星月持好活→追加15%冰属性欢愉伤害段。"""
    if skill_key != "zz_basic_dream" or skill is None:
        return None
    if _pool(state) <= 0:
        return None
    new = _copy.deepcopy(skill)
    new.multipliers = list(new.multipliers) + [
        SkillMultiplier(stat="NONE", scale=15.0, damage_type="elation",
                        element=ELEMENT, target="all_enemies")]
    return new


def _zz_extra_lowest_heal(u, state, skill_key):
    """额外治疗: 当前生命百分比最低的我方目标回复同等量(DEF基数+HEAL_BONUS+技能等级)。"""
    named = {"skill": (12.0, 240.0), "basic_attack_enhanced": (8.0, 160.0),
             "zz_basic_dream": (8.0, 160.0)}.get(skill_key)
    if named is None:
        return
    cands = [t for t in state.units if t.is_alive]
    if not cands:
        return
    tgt = min(cands, key=lambda t: t.current_hp / t.max_hp)
    stats = _build_effective_stats(u, state)
    lvl_key = "skill" if skill_key == "skill" else "basic_attack"
    amt = (stats.DEF * named[0] / 100.0 + named[1]) \
        * (1.0 + stats.HEAL_BONUS) * _skill_level_factor(u, lvl_key)
    tgt.current_hp = min(tgt.max_hp, tgt.current_hp + amt)
    state.log.append(f"  真珠·额外治疗: {tgt.char.name}+{amt:.0f}(生命百分比最低)")


def _zz_base_rider(u, state, skill_key):
    """幻造星月攻击后 60% 冰属性欢愉伤害(底本属性值); E6 强化普攻追加240%(底本属性值)。"""
    if skill_key not in ("basic_attack_enhanced", "zz_basic_dream"):
        return
    base = u.extra.get("zz_base")
    if base is None or not base.is_alive:
        return
    scales = []
    if skill_key == "zz_basic_dream" and base.char.path == "欢愉":
        scales.append(60.0)   # 终结技: 攻击后额外60%(美学底本属性值)
    if u.eidolon_rank >= 6:
        scales.append(240.0)  # E6: 强化普攻额外240%(美学底本属性值)
    if not scales:
        return
    bstats = _build_effective_stats(base, state)
    laugh_n = state.elation_state.get_good_show_total(base.char.id)
    total = 0.0
    for sc in scales:
        for t in state.alive_enemies():
            d = calculate_damage(bstats, _enemy_for_damage(t), 0.0, sc, "elation",
                                 ELEMENT, 80, bstats.CRIT_RATE >= 0.5,
                                 laugh_n=laugh_n, crit_mode="expected")
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
    if total > 0:
        u.total_damage_dealt += total
        state.log.append(f"  真珠·底本追伤: {base.char.name}属性值 ×"
                         f"{'/'.join(f'{s:g}%' for s in scales)} → {total:.0f}")


def _zz_post_effects(u, state, skill_key=None, **kw):
    """PHASE post_effects: 战技好活/额外治疗; 强化普攻充能消耗/底本追伤/额外治疗。"""
    if u.char.id != CHAR_ID:
        return None
    if skill_key == "ultimate":
        _zz_start_dl(u, state)
    elif skill_key == "skill":
        _zz_gain(u, state, 15, cause="战技·补缀生命的柔光")
        _zz_extra_lowest_heal(u, state, "skill")
    elif skill_key in ("basic_attack_enhanced", "zz_basic_dream"):
        _zz_extra_lowest_heal(u, state, skill_key)
        _zz_base_rider(u, state, skill_key)
        _zz_dl_consume(u, state)
    return None


def _zz_eff_stats(u, state, s, effective_spd, **kw):
    """PHASE eff_stats_zz(elation.eff_stats 派发): 行迹3·洞察万物。"""
    if u.char.id != CHAR_ID or not _has_trace(u, TRACE3):
        return None
    if s.DEF < 2400.0:
        return None
    over = min(3600.0, s.DEF - 2400.0)
    s.ELATION_LEVEL += 0.32 + over * 0.0003   # 每超100点+3%
    s.HEAL_BONUS += s.ELATION_LEVEL * 0.20    # 欢愉度20%→治疗量加成
    return s


# ---- 观察者相位 ----

def _zz_goodshow_infinite(_u, state, char_id):
    """OBSERVER goodshow_zz: 真珠的好活当赏持续时间无限(负数=永久批次)。"""
    if char_id != CHAR_ID:
        return None
    return -1 if _me(state) is not None else None


def _zz_goodshow_cap(_u, state, char_id):
    """OBSERVER goodshow_cap: 裁决9——真珠好活持有上限50, 获得瞬间截断(全路径)。"""
    return GOODSHOW_CAP if char_id == CHAR_ID else None


def _zz_ally_turn_start(unit, state, **kw):
    """OBSERVER ally_turn_start: 行迹2——任意我方常规回合开始+5好活(本轮上限50,
    真珠自身回合开始时先重置计数再计当次)。u=行动单位。"""
    me = _me(state)
    if me is None:
        # v7.26.2 丙(b)消除: 阵亡时经每回合同步点回收行迹2/E6 两个对称开关
        _zz_sync_pool(state)
        _zz_sync_e6(state)
        return None
    if not _has_trace(me, TRACE2):
        return None
    if getattr(unit, "char", None) is None or not getattr(unit, "is_alive", False):
        return None
    if unit not in state.units:
        return None  # 敌方/忆灵行动不算"我方目标回合"
    if unit is me:
        me.extra["zz_t2_gained"] = 0.0
    gained = me.extra.get("zz_t2_gained", 0.0)
    if gained >= 50.0:
        return None
    add = min(5.0, 50.0 - gained)
    me.extra["zz_t2_gained"] = gained + add
    _zz_gain(me, state, add, cause="行迹2·我方回合开始")
    return None


def _zz_after_ult(unit, state, **kw):
    """OBSERVER after_ult: 行迹1——底本(欢愉)施放终结技→真珠固定位+90能量(单发, 重臂制)。
    u=施放终结技者。"""
    me = _me(state)
    if me is None or not _has_trace(me, TRACE1):
        return None
    if unit is None or unit is not me.extra.get("zz_base"):
        return None
    if not me.extra.get("zz_trace1_armed") or unit.char.path != "欢愉":
        return None
    me.extra["zz_trace1_armed"] = False
    _gain_energy(me, 90.0, state=state, apply_regen=False)
    state.log.append(f"  行迹1·艺术壁垒: 底本{unit.char.name}施放终结技→真珠+90能量")
    return None


# ---- 入场 / 秘技 / AI ----

def _zz_init(state):
    me = _me(state)
    if me is None:
        return
    me.extra.setdefault("zz_t2_gained", 0.0)
    me.extra["zz_trace1_armed"] = True
    n_el = sum(1 for x in state.units if x.is_alive and x.char.path == "欢愉")
    if me.eidolon_rank >= 1:
        state.extra["zz_e1_charges"] = 2
        boost = {2: 0.10, 3: 0.20}.get(n_el, 0.60 if n_el >= 4 else 0.0)
        if boost:
            for t in state.units:
                if t.is_alive:
                    t.base_stats.ELATION_LEVEL += boost
            state.log.append(f"  真珠E1: {n_el}欢愉→全队欢愉度+{boost * 100:g}%")
    if me.eidolon_rank >= 2:
        for t in state.units:
            if t.is_alive:
                t.base_stats.LAUGH_BOOST += 0.15
        state.log.append("  真珠E2: 全队欢愉伤害增笑+15%")
    state.hooks.register(CHAR_ID, "on_attack_action", _zz_on_attack,
                         source_name="真珠·欢愉技rider")
    _zz_sync_pool(state)


def _zz_tech(state, u, is_opener):
    """秘技·凝润珠光，复刻真迹: 开战20好活 + 深度学习持有美学底本(2点充能)。"""
    if u.char.id != CHAR_ID:
        return
    base = _zz_pick_base(state, u)
    u.extra["zz_dl"] = True
    u.extra["zz_dl_charges"] = 2
    u.extra["zz_base"] = base
    _zz_gain(u, state, 20, cause="秘技·凝润珠光")
    _zz_sync_e6(state)
    state.log.append("  [秘技] 凝润珠光，复刻真迹: 20好活 + 深度学习2点充能"
                     f"(底本={base.char.name if base else '无'})")


def _zz_ai(u, state, **__):
    from engine.core.combat_engine import _use_skill  # 函数级导入(monkeypatch 可见性)
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif u.extra.get("zz_dl") and u.extra.get("zz_dl_charges", 0) > 0:
        _use_skill(u, state, "basic_attack")  # key_rewrite→绘制末浪/幻造星月
    elif state.skill_points >= 1:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


ELATION_GATED = True
SKILL_HOOKS = [_zz_elation_arm]
PHASE_HOOKS = {
    "key_rewrite": _zz_key_rewrite,
    "energy_gain_override": _zz_energy_override,
    "skill_adjust_post": _zz_skill_adjust_post,
    "post_effects": _zz_post_effects,
    "eff_stats_zz": _zz_eff_stats,
}
OBSERVER_HOOKS = {
    "goodshow_zz": _zz_goodshow_infinite,
    "goodshow_cap": _zz_goodshow_cap,
    "ally_turn_start": _zz_ally_turn_start,
    "extra_turn_start": _zz_extra_turn_start,
    "extra_turn_end": _zz_extra_turn_end,
    "after_ult": _zz_after_ult,
}
INIT = _zz_init
TECHNIQUE = _zz_tech
AI = _zz_ai
