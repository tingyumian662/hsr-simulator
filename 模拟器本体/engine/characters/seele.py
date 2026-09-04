"""seele（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _effective_spd
from engine.core.combat_engine import _use_skill


def _apply_luandie(state, t, source=None):
    """希儿E6乱蝶: 受击后追加30%终结技快照真伤（不递归, 触发次数3→0）
    "持续3回合"简化=3次触发（引擎无敌人回合概念）"""
    if t.extra.get('luandie', 0) > 0:
        dmg = 0.30 * t.extra.get('luandie_ult_dmg', 0.0)
        _commit_enemy_damage(state, source, t, dmg, damage_type='true_damage',
                             record_cipher=False)
        t.extra['luandie'] -= 1
        state.log.append(f'  乱蝶真伤: {dmg:.0f} (剩余{t.extra["luandie"]}次)')


def _seele_reproduce_check(state, u, ctx):
    """希儿【再现】: 常规回合击杀→1个额外回合（不能无限续杯）
    增幅=击杀瞬间获得的战利品(挂 pending 标志, 不进 buffs, 免被本回合末 _tick_buffs 误杀);
    X轴首个希儿行动(终结技或再现)时激活(_exec_extra_turn 开头补施), 增幅回合结束撤销
    ——实机: 战技动画中释放的终结技排在增幅回合前也能吃到增幅"""
    if u.char.id != 'seele' or u.extra.get('seele_in_extra'):
        return
    if state.extra.get('killed_this_action', 0) > 0 and ctx == 'regular':
        u.extra['seele_in_extra'] = True
        u.extra['seele_amplify_pending'] = True
        state.extra.setdefault('extra_turns', []).append((u, 'extra'))
        state.log.append('  【再现】: 击杀→获得额外回合, 进入增幅状态')


def seele_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
    """希儿AI: SP>0→战技，否则普攻。终结技由phase-1拦截，再现由击杀触发"""
    if state.skill_points > 0:
        _use_skill(unit, state, 'skill')
    else:
        _use_skill(unit, state, 'basic_attack')


def _trace_seele_ripple(u, state, **kw):
    """希儿行迹「涟漪」: 施放普攻后下次行动提前20%"""
    if u.char.id != 'seele':
        return
    from engine.runtime import AV_PER_TURN
    from engine.core.combat_engine import _effective_spd
    u._pending_action_advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.20
    state.log.append('  行迹·涟漪: 普攻后下次行动提前20%')


def _eid_seele_e4(u, state, **kw):
    """希儿E4: 击杀回15能量"""
    u.current_energy = min(u.char.max_energy or 999, u.current_energy + 15)
    state.log.append('  希儿E4: 击杀回15能量')


def _tech_seele(state, u, is_opener):
    """希儿: 进战立即进入增幅状态（希儿.txt 秘技·幻身, 进战）
    v6.3.0b P1-4: 直接挂增幅 Buff（此前走 seele_amplify_pending, 被常规回合入口无条件清零）"""
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='seele', attributes={'DMG_BONUS_ALL': 80.0},
                             remaining_turns=1, source_name='再现增幅'))
    state.log.append('[秘技] 幻身: 进战立即进入增幅状态(80%增伤1回合)')


CHAR_ID = "seele"
AI = seele_ai
TECHNIQUE = _tech_seele


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _seele_turn_tick(u, state):
    # 希儿增幅防御: 常规回合开始前 X轴必已清空(增幅回合已撤销), 清异常残留的 pending
    # (M5a 验收 P3-1 注: 原引擎位于 AV 更新前, 现随 pre 区派发在更新后——
    #  已核实 _effective_spd 不读该标记, 读者仅在 X 轴撤销逻辑, 无语义影响)
    if u.char.id == 'seele':
        u.extra['seele_amplify_pending'] = False


TURN_TICKS = {'pre': _seele_turn_tick}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _seele_speed_buff_pre_apply(u, state, target):
    """EFFECT_PRE_APPLY['seele_speed_buff']: 同ID上限1层(rank0)/2层(E2)滚动。"""
    # 希儿战技加速: 同ID buff 上限1层(rank0刷新)/2层(E2), 移除最旧保持滚动
    cap = 2 if u.eidolon_rank >= 2 else 1
    same = [b for b in target.buffs if getattr(b, 'param_id', '') == 'seele_speed_buff']
    while len(same) >= cap:
        target.buffs.remove(same.pop(0))


EFFECT_PRE_APPLY = {'seele_speed_buff': _seele_speed_buff_pre_apply}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _seele_crit_override(u, state, t, t_stats, skill_key):
    """PHASE crit_override: 行迹·斩尽——HP≤80%目标必暴+无视20%防御。"""
    # 希儿行迹·斩尽(与E1同效果, 无条件): 对HP≤80%目标暴击+15%且无视20%防御
    # (v5.2 期望模式: CR+15% 写入面板副本进入期望公式, DEF_PEN 同副本)
    bp = state.extra.get('enemy_blueprint') or state.enemies[0]
    if bp.HP > 0 and t.HP <= bp.HP * 0.80:
        t_stats = copy.deepcopy(t_stats)
        t_stats.CRIT_RATE = min(1.0, t_stats.CRIT_RATE + 0.15)
        t_stats.DEF_PEN += 0.20
        return (t_stats, True)  # 旧布尔模式兼容（期望模式忽略）
    return None


def _seele_post_hit_debuff(u, state, t, dmg, skill_key):
    """PHASE post_hit_debuff: 行迹·离析——终结技命中→乱蝶(真伤30%快照, 3次)。"""
    # 希儿行迹·离析(与E6同效果, 无条件): 终结技命中→目标陷入【乱蝶】
    if skill_key == 'ultimate':
        if t.HP > 0:
            t.extra['luandie'] = 3
            t.extra['luandie_ult_dmg'] = dmg
            state.log.append('  乱蝶: 目标陷入乱蝶(真伤30%×3)')
    return None


PHASE_HOOKS['crit_override'] = _seele_crit_override
PHASE_HOOKS['post_hit_debuff'] = _seele_post_hit_debuff


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _seele_heal_blast_main(u, state):
    """OBSERVER heal_blast_main: 治疗主C拾取（希儿惯例, 不在场返回 None）。"""
    # 相邻治疗（藿藿战技相邻目标）: 主C惯例
    return next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)


OBSERVER_HOOKS = {'heal_blast_main': _seele_heal_blast_main}
