"""爻光（试点 M3）"""

import copy
import random
from engine.runtime import _enemy_for_damage
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _gain_energy, _skill_level_factor, _use_skill
from engine.core.damage import calculate_damage


def _yaoguang_open_field(state, yao, *, source='skill'):
    """展开或刷新爻光结界；增益持续时间只跟随爻光自身回合。"""
    was_active = state.yao_field_active
    state.yao_field_active = False
    try:
        yao_stats = _build_effective_stats(yao, state)
    finally:
        state.yao_field_active = was_active
    state.yao_field_active = True
    state.yao_field_turns = 3
    state.extra['yaoguang_field_elation_bonus'] = yao_stats.ELATION_LEVEL * 0.20
    # 清理 Harness 旧实现遗留的受益者倒计时 Buff，避免双算或结界结束后残留。
    for unit in state.units:
        unit.buffs = [b for b in unit.buffs
                      if getattr(b, 'param_id', '') != 'yaoguang_field_elation']
    state.log.append(
        f'  爻光结界: 全队欢愉度+{yao_stats.ELATION_LEVEL * 20:.0f}%(3回合,{source})')


def _yaoguang_close_field(state):
    state.yao_field_active = False
    state.yao_field_turns = 0
    state.extra.pop('yaoguang_field_elation_bonus', None)
    for unit in state.units:
        unit.buffs = [b for b in unit.buffs
                      if getattr(b, 'param_id', '') != 'yaoguang_field_elation']


def _yaoguang_dajidali(state, u, skill_key, spent_skill_points=None):
    """v6.10.3 P1-3: 爻光天赋【大吉大利】——爻光持有【好活当赏】时, 我方目标攻击后
    对随机1个击中的目标额外造成1次20%对应属性欢愉伤害; 本次攻击消耗战技点则额外触发1次;
    攻击者欢愉度低于爻光时该次欢愉伤害使用爻光欢愉度计算"""
    yao = next((x for x in state.units if x.char.id == 'yaoguang' and x.is_alive), None)
    if yao is None or state.elation_state.get_good_show_total('yaoguang') <= 0:
        return
    hits = [t for t in state.extra.get('last_attack_targets', []) or []
            if t is not None and getattr(t, 'HP', 0) > 0]
    if not hits:
        return
    stats = _build_effective_stats(u, state)
    yao_elation = _build_effective_stats(yao, state).ELATION_LEVEL
    if stats.ELATION_LEVEL < yao_elation:
        stats = copy.deepcopy(stats)
        stats.ELATION_LEVEL = yao_elation
    laugh_n = state.elation_state.get_good_show_total(u.char.id)
    if spent_skill_points is None:
        skill = u.char.skills.get(skill_key)
        spent_skill_points = ((skill.cost or {}).get('skill_points', 0)
                              if skill is not None else 0)
    times = 2 if spent_skill_points > 0 else 1
    total = 0.0
    for _ in range(times):
        t = random.choice(hits)
        talent_scale = 20.0 * _skill_level_factor(yao, 'talent')
        d = calculate_damage(stats, _enemy_for_damage(t), 0, talent_scale, 'elation',
                             u.char.element, 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, crit_mode='expected')
        _commit_enemy_damage(state, u, t, d.final_damage)
        total += d.final_damage
    u.total_damage_dealt += total
    if total > 0:
        state.log.append(f'  大吉大利: {total:.0f} ({"×2" if times == 2 else "×1"})')


def _eid_yaoguang_e1(u, state, **kw):
    """爻光E1: 阿哈额外回合固定40笑点 + 全队欢愉伤害无视20%防御"""
    pass  # 已在 _yg_ai 中实现，此处标记使 resolve 读取到 eidolon_rank


def _eid_yaoguang_e2(u, state, **kw):
    """爻光E2: 结界内全队SPD+12%, 欢愉度+16%
    v6.10.3 P1-3: 改为 _build_effective_stats 动态消费（此前 on_turn_start 每次叠 1 回合 TimedBuff 会无限叠层）"""
    pass


def _eid_yaoguang_e4(u, state, **kw):
    """爻光E4: 阿哈额外回合中全体欢愉技伤害×1.5"""
    pass  # 在 execute_aha 中处理


def _eid_yaoguang_e6(u, state, **kw):
    """爻光E6: 全队增笑25% + 自身欢愉技倍率+100%
    v6.10.3 P1-3: 改为动态面板（全队 LAUGH_BOOST）与 _use_skill 倍率内联（此前只给爻光自身且无倍率消费）"""
    pass


def _yg_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        # v6.10.3 P1-3 E0/E1分离: 阿哈额外回合固定计入笑点 E0=20 / E1=40;
        # 保存全局笑点→临时替换→主循环处理阿哈→恢复全局笑点（不消耗原笑点）
        fixed = 40 if u.eidolon_rank >= 1 else 20
        saved_laugh = state.laugh_points
        state.laugh_points = fixed
        state.aha_next_av = state.current_av  # 强制阿哈立即行动
        # 全队全抗穿24% (3回合); E1 额外欢愉伤害无视防御20%（面板消费）
        for eu in state.units:
            if eu.is_alive:
                eu.yao_res_pen_turns = 3
        if u.eidolon_rank >= 4:
            state.extra['yao_e4_aha'] = True  # E4: 本次阿哈回合全体欢愉技伤害×1.5
        state.log.append(f'  爻光终结技: 阿哈额外回合固定{fixed}笑点, 全队全抗穿24%(3回合)' +
                         (' +E1无视防御20%' if u.eidolon_rank >= 1 else '') +
                         (' +E4欢愉技×1.5' if u.eidolon_rank >= 4 else ''))
        # 标记：阿哈处理后恢复全局笑点
        state.extra['yao_pending_laugh'] = saved_laugh
    elif state.skill_points > 0 and not state.yao_field_active:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _laugh_gen(u, state, skill_key):
    """笑点生成（yaoguang 形态: +3 早退, 不吃好活加成）"""
    is_tb_elation = (u.char.id == 'trailblazer_elation' and skill_key == 'elation_skill')
    if u.char.path != "欢愉" or (skill_key not in ("basic_attack", "skill") and not is_tb_elation):
        return
    if u.char.id == 'yaoguang':
        state.laugh_points += 3
        return


def _yaoguang_field(u, state, skill_key):
    if u.char.id == "yaoguang" and skill_key == "skill":
        _yaoguang_open_field(state, u)


def _yaoguang_energy(u, state, skill_key):
    if u.char.id == "yaoguang" and skill_key == "basic_attack":
        _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口


CHAR_ID = "yaoguang"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
SKILL_HOOKS = [_laugh_gen, _yaoguang_field, _yaoguang_energy]
AI = _yg_ai


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _yaoguang_skill_adjust_post(u, state, skill, skill_key):
    """PHASE skill_adjust_post: E6 自身欢愉技倍率×2（→新skill|None）。"""
    import copy
    if skill_key == 'elation_skill' and u.eidolon_rank >= 6 and skill.multipliers:
        skill = copy.deepcopy(skill)
        for mult in skill.multipliers:
            mult.scale *= 2.0
        return skill
    return None


PHASE_HOOKS = {'skill_adjust_post': _yaoguang_skill_adjust_post}


OBSERVER_HOOKS = {}


# ---- v7.15.0 相位: 爻光欢愉站点（原 elation 内联, verbatim 迁入）----

def _yao_tech_init(_u, state):
    """OBSERVER init_yaoguang_tech: 秘技——免SP自动战技开结界+3笑点+30能量。"""
    from engine.core.combat_engine import _gain_energy
    yao = next((u for u in state.units if u.char.id == "yaoguang"), None)
    if yao:
        _yaoguang_open_field(state, yao, source='technique')
        state.laugh_points += 3
        _gain_energy(yao, 30.0, state=state)
        state.log.append('[Init] 爻光秘技: 免SP自动战技, +3笑点, +30能量')
    return None


def _yao_goodshow_extend(_u, state, char_id):
    """OBSERVER goodshow_yaoguang: 行迹2·鸿运鳞集——获得好活持续+1回合(2→3)。"""
    if char_id != 'yaoguang':
        return None
    yao = next((x for x in state.units
                if x.char.id == 'yaoguang' and x.is_alive), None)
    if yao and any(getattr(t, 'hook_name', '') == 'yaoguang_goodshow_extend'
                   for t in (yao.char.traces or [])):
        return 1  # duration += 1 由派发点执行
    return None


def _yao_field_tick(unit, state):
    """PHASE field_tick: 结界只在爻光自身行动时递减，归零关闭。"""
    if state.yao_field_active:
        state.yao_field_turns -= 1
        if state.yao_field_turns <= 0:
            _yaoguang_close_field(state)
    return None


OBSERVER_HOOKS['init_yaoguang_tech'] = _yao_tech_init
OBSERVER_HOOKS['goodshow_yaoguang'] = _yao_goodshow_extend
PHASE_HOOKS['field_tick'] = _yao_field_tick
