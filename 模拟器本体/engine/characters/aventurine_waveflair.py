"""砂金•戏浪——五星·量子·欢愉（v7.25.0 录入, 角色技能介绍/欢愉/砂金·戏浪.txt）

机制映射:
- 【热意】个人资源 u.extra['avw_heat']（上限 30 / E2→50）: 战技+4 / 终结技+8 /
  秘技+2 / 队友攻击后+1(天赋) / 行迹2队友技能后+2 / E2 欢愉技后+4
- 天赋阈值（10 / E1 追加 20,30 / E2 追加 40,50）从下方跨过 → 立即施放固定计入
  20 笑点的【举杯！敬炽烈一夏】(laugh_n_override=20, 开拓者同款先例) + 置
  "下一次阿哈强化 All in" 旗标; 重入守卫 avw_casting
- 【All in！敬炽烈一夏】= elation_skill 强化形态（skill_adjust_post）: 同倍率 +
  每消耗 1 点热意追加 1 次弹射 21%; 触发: 阿哈中消费天赋旗标 / E6 施放 2 次后
  常驻; E6 阿哈外不消耗热意
- 行迹1 SPD→欢愉度（140 阈值 +30%, 每超 1 点 +1%, cap 200）——eff_stats_avw 相位
  （爻光 eff_stats_yinlang 同型）
- 行迹2 CD+48 常驻(INIT base_stats) + 队友技能后全队 CD+48 3回合 ≤6次 战技重置
- 行迹3 双分支: 多欢愉队全队欢愉度+20/自身再+80; 单欢愉(追重视为追加攻击未
  建模为攻击类型, 记注) 队友攻击后+2 好活+1 笑点 + 阿哈速度+25(至阿哈结束)
- E1 RES_PEN_ALL+24 / E4 战技全队无视 18% 防御 3 回合 / E6 增笑(LAUGH_BOOST)+25
"""
import copy as _copy

from engine.core.combat_engine import (
    _build_effective_stats,
    _commit_enemy_damage,
    _enemy_for_damage,
    _skill_level_factor,
)
from engine.core.damage import calculate_damage
from engine.models.character import SkillMultiplier
from engine.runtime import TimedBuff

CHAR_ID = "aventurine_waveflair"
ELEMENT = "量子"
HEAT_KEY = "avw_heat"
TRACE1 = "aventurine_waveflair_spd_to_elation"
TRACE2 = "aventurine_waveflair_old_dream"
TRACE3 = "aventurine_waveflair_storm"
_TRACE2_KEYS = ("basic_attack", "basic_attack_enhanced", "skill", "ultimate",
                "follow_up")


def _has_trace(u, hook_name):
    return any(getattr(t, "hook_name", "") == hook_name for t in (u.char.traces or []))


def _heat_cap(u):
    return 50.0 if u.eidolon_rank >= 2 else 30.0


def _heat_thresholds(u):
    ts = [10.0]
    if u.eidolon_rank >= 1:
        ts += [20.0, 30.0]
    if u.eidolon_rank >= 2:
        ts += [40.0, 50.0]
    return ts


def _avw_gain_heat(u, state, n, cause=""):
    """热意获得(带上限); 从下方跨过任一阈值 → 天赋立即施放。"""
    if n <= 0:
        return
    before = u.extra.get(HEAT_KEY, 0.0)
    cap = _heat_cap(u)
    after = min(cap, before + n)
    u.extra[HEAT_KEY] = after
    if cause:
        state.log.append(f"  砂金【热意】+{n:g} ({cause}) → {after:g}/{cap:g}")
    for thr in _heat_thresholds(u):
        if before < thr <= after:
            _avw_threshold_cast(u, state, thr)


def _avw_threshold_cast(u, state, thr):
    """天赋: 热意达阈值 → 立即施放固定 20 笑点【举杯！】+ 置下次阿哈 All in 旗标。"""
    if u.extra.get("avw_casting") or not state.alive_enemies():
        return
    u.extra["avw_casting"] = True
    state.log.append(f"  天赋: 【热意】达到{thr:g} → 立即施放【举杯！敬炽烈一夏】"
                     f"(固定计入20笑点)")
    from engine.core.combat_engine import _use_skill  # 函数级导入(monkeypatch 可见性)
    try:
        _use_skill(u, state, "elation_skill", laugh_n_override=20.0)
    finally:
        u.extra["avw_casting"] = False
    u.extra["avw_next_aha_allin"] = True


# ---- 技能后资源结算（SKILL_HOOKS 签名 fn(u, state, skill_key)）----

def _avw_skill_gains(u, state, skill_key):
    if u.char.id != CHAR_ID or u.char.path != "欢愉":
        return
    if skill_key == "skill":
        state.laugh_points += 4
        u.extra["avw_old_dream_uses"] = 0  # 行迹2: 战技重置可触发次数
        _avw_gain_heat(u, state, 4, cause="战技·绝杀")
    elif skill_key == "ultimate":
        state.laugh_points += 6
        _avw_gain_heat(u, state, 8, cause="终结技·胜局")
    elif skill_key == "elation_skill":
        u.extra["avw_elation_casts"] = u.extra.get("avw_elation_casts", 0) + 1
        if u.eidolon_rank >= 2:
            _avw_gain_heat(u, state, 4, cause="E2·欢愉技")


# ---- All in 形态 / 终结技SPD / E4（PHASE_HOOKS）----

def _avw_skill_adjust_post(u, state, skill=None, skill_key=None, **kw):
    """PHASE skill_adjust_post: 欢愉技 All in 形态——追加"每点热意 1 次弹射 21%"
    段并消耗热意(E6 阿哈外不耗); 记录形态供光锥【潮流】判定。"""
    if skill_key != "elation_skill" or skill is None:
        return None
    in_aha = bool(state.extra.get("aha_running"))
    completed = u.extra.get("avw_elation_casts", 0)  # SKILL_HOOKS 在结算后 → 此为已完成数
    all_in = ((u.eidolon_rank >= 6 and completed >= 2)
              or (in_aha and u.extra.get("avw_next_aha_allin")))
    if in_aha:
        u.extra.pop("avw_next_aha_allin", None)
    u.extra["avw_last_form"] = "all_in" if all_in else "base"
    u.extra["avw_aha_spd_armed"] = False  # 行迹3: 阿哈速度加成重臂(下次队友攻击再+25)
    heat = u.extra.get(HEAT_KEY, 0.0)
    if not all_in or heat <= 0:
        return None
    new = _copy.deepcopy(skill)
    new.multipliers = list(new.multipliers) + [
        SkillMultiplier(stat="NONE", scale=21.0, damage_type="elation",
                        element=ELEMENT, hits=int(heat), target="bounce")]
    if not (u.eidolon_rank >= 6 and not in_aha):  # E6: 阿哈外不消耗
        u.extra[HEAT_KEY] = 0.0
    state.log.append(f"  【All in！敬炽烈一夏】: 消耗{heat:g}热意 → "
                     f"追加{int(heat)}次弹射(21%/次)")
    return new


def _avw_post_effects(u, state, skill_key=None, **kw):
    """PHASE post_effects: 终结技 SPD+30% 4回合; E4 战技全队无视18%防御3回合。"""
    if skill_key == "ultimate":
        u.buffs = [b for b in u.buffs if getattr(b, "param_id", "") != "avw_ult_spd"]
        u.buffs.append(TimedBuff(source_id=CHAR_ID,
                                 attributes={"SPD_PERCENT": 30.0},
                                 remaining_turns=4, param_id="avw_ult_spd",
                                 source_name="终结技·凌越飓浪"))
    elif skill_key == "skill" and u.eidolon_rank >= 4:
        for t in state.units:
            if t.is_alive:
                t.buffs = [b for b in t.buffs
                           if getattr(b, "param_id", "") != "avw_e4_defpen"]
                t.buffs.append(TimedBuff(source_id=CHAR_ID,
                                         attributes={"DEF_PEN": 18.0},
                                         remaining_turns=3, param_id="avw_e4_defpen",
                                         source_name="E4·无需同阳光交易"))
    return None


def _avw_goodshow_settle(u, state, skill_key=None, total_dmg=0.0, **kw):
    """PHASE goodshow_settle: 持【好活当赏】时 战技/终结技 追加 40%/72% 欢愉伤害。"""
    if skill_key not in ("skill", "ultimate"):
        return None
    n = state.elation_state.get_good_show_total(CHAR_ID)
    if n <= 0 or not state.alive_enemies():
        return None
    scale = (40.0 if skill_key == "skill" else 72.0) * _skill_level_factor(u, "talent")
    stats = _build_effective_stats(u, state)
    total = 0.0
    for t in state.alive_enemies():
        d = calculate_damage(stats, _enemy_for_damage(t, "skill"), stats.ATK,
                             scale, "elation", ELEMENT, 80,
                             stats.CRIT_RATE >= 0.5, laugh_n=n,
                             skill_type=skill_key, crit_mode="expected")
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    if total > 0:
        u.total_damage_dealt += total
        state.log.append(f"  天赋·持好活追加: {skill_key}额外{total:.0f}"
                         f"量子欢愉伤害 (好活{n:g}层)")
    return None


def _avw_eff_stats(u, state, s, effective_spd, **kw):
    """PHASE eff_stats_avw(elation.eff_stats 派发): 行迹1·极乐派对——
    SPD≥140 欢愉度+30%, 每超 1 点+1%, 最多计入 200 点超出速度。"""
    if not _has_trace(u, TRACE1) or effective_spd < 140.0:
        return None
    over = min(200.0, effective_spd - 140.0)
    s.ELATION_LEVEL += 0.30 + over * 0.01
    return s


# ---- 好活当赏延长（OBSERVER, elation.grant_good_show 派发点）----

def _avw_goodshow_extend(_u, state, char_id):
    """OBSERVER goodshow_avw: 天赋——获得好活当赏时持续时间+1回合(2→3)。"""
    if char_id != CHAR_ID:
        return None
    me = next((x for x in state.units
               if x.char.id == CHAR_ID and x.is_alive), None)
    return 1 if me is not None else None


# ---- 队友攻击反应（on_attack_action 订阅, 晴歌同型）----

def _avw_on_attack(u, state, dealt=True, **ctx):
    """天赋(+1热意+1笑点) / 行迹3 单欢愉分支(+2好活+1笑点+阿哈速度) /
    行迹2(队友普攻/战技/追加/终结技后 全队CD+48 3回合 ≤6次)。
    u=行动者(队友); HookRegistry 以关键字 u/state 派发。"""
    if not dealt or u.char.id == CHAR_ID:
        return
    me = next((x for x in state.units
               if x.char.id == CHAR_ID and x.is_alive), None)
    if me is None:
        return
    state.laugh_points += 1
    _avw_gain_heat(me, state, 1, cause="天赋·队友攻击")
    if me.extra.get("avw_solo") and _has_trace(me, TRACE3):
        el = state.extra.get("_elation")
        if el is not None:
            el.grant_good_show(state, CHAR_ID, 2, duration=3, source="纵享惊涛")
        state.laugh_points += 1
        if not state.extra.get("avw_aha_spd_armed"):
            state.extra["avw_aha_spd_armed"] = True
            state.aha_speed += 25.0
            state.log.append("  行迹3·纵享惊涛: 阿哈速度+25 (至阿哈时刻结束)")
    if _has_trace(me, TRACE2):
        key = u.extra.get("lc_last_skill_key", "")
        if key in _TRACE2_KEYS:
            used = me.extra.get("avw_old_dream_uses", 0)
            if used < 6:
                me.extra["avw_old_dream_uses"] = used + 1
                for t in state.units:
                    if t.is_alive:
                        t.buffs = [b for b in t.buffs
                                   if getattr(b, "param_id", "") != "avw_old_dream"]
                        t.buffs.append(TimedBuff(
                            source_id=CHAR_ID, attributes={"CRIT_DMG": 48.0},
                            remaining_turns=3, param_id="avw_old_dream",
                            source_name="行迹2·旧梦淘金"))
                _avw_gain_heat(me, state, 2, cause="行迹2·队友技能")


# ---- 入场 / 秘技 ----

def _init_battle(state):
    for u in state.units:
        if u.char.id != CHAR_ID:
            continue
        u.extra.setdefault(HEAT_KEY, 0.0)
        if _has_trace(u, TRACE2):
            u.base_stats.CRIT_DMG += 0.48  # 行迹2无条件段
        if u.eidolon_rank >= 1:
            u.base_stats.RES_PEN_ALL += 0.24
        if u.eidolon_rank >= 6:
            u.base_stats.LAUGH_BOOST += 0.25  # 增笑(独立乘区, 仅星魂)
        if _has_trace(u, TRACE3):
            others = [x for x in state.units
                      if x is not u and x.is_alive and x.char.path == "欢愉"]
            if others:
                for x in state.units:
                    if x.is_alive:
                        x.base_stats.ELATION_LEVEL += 0.20
                u.base_stats.ELATION_LEVEL += 0.80
                state.log.append("  行迹3·纵享惊涛: 全队欢愉度+20%, 砂金额外+80%")
            else:
                u.extra["avw_solo"] = True
        state.hooks.register(CHAR_ID, "on_attack_action", _avw_on_attack,
                             source_name="砂金·队友攻击行动")


def _tech(state, u, is_opener):
    """秘技·于静水掀起风浪: 全体 100%ATK 量子伤害 + 2热意 + 20好活(3回合)。"""
    if u.char.id != CHAR_ID or not state.alive_enemies():
        return
    stats = _build_effective_stats(u, state)
    for t in list(state.alive_enemies()):
        d = calculate_damage(stats, _enemy_for_damage(t, "skill"), stats.ATK,
                             100.0, "direct", ELEMENT, 80,
                             stats.CRIT_RATE >= 0.5, skill_type="skill",
                             crit_mode="expected")
        _commit_enemy_damage(state, u, t, d.final_damage)
        u.total_damage_dealt += d.final_damage
    _avw_gain_heat(u, state, 2, cause="秘技")
    el = state.extra.get("_elation")
    if el is not None:
        el.grant_good_show(state, CHAR_ID, 20, duration=3, source="秘技")
    state.log.append("  [秘技] 于静水掀起风浪: 全体100%ATK量子 + 2热意 + 20好活当赏")


CHAR_ID = CHAR_ID
ELATION_GATED = True
SKILL_HOOKS = [_avw_skill_gains]
PHASE_HOOKS = {
    "skill_adjust_post": _avw_skill_adjust_post,
    "post_effects": _avw_post_effects,
    "goodshow_settle": _avw_goodshow_settle,
    "eff_stats_avw": _avw_eff_stats,
}
OBSERVER_HOOKS = {"goodshow_avw": _avw_goodshow_extend}
INIT = _init_battle
TECHNIQUE = _tech
