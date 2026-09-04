"""fu_xuan（M4 收官批迁入）"""

import copy
import random
from engine.runtime import TimedBuff
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _use_skill


def fuxuan_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
    # 阵法倒计时
    field_turns = state.extra.get('fuxuan_field_turns', 0)
    if field_turns > 0:
        state.extra['fuxuan_field_turns'] = field_turns - 1
        # v6.3.0b P1-6: 阵法到期→鉴知HP上限快照回退（秘技施加, 角色+忆灵）
        if state.extra['fuxuan_field_turns'] <= 0:
            for eu in [x for x in state.units if x.is_alive] \
                    + [x for x in state.memsprites if x.is_alive]:
                orig = eu.extra.pop('fuxuan_tech_orig_maxhp', None)
                if orig is not None:
                    eu.max_hp = orig
                    eu.current_hp = min(orig, eu.current_hp)
            state.log.append('  穷观阵到期: 鉴知HP上限回退')
    if unit.current_energy >= unit.char.max_energy:
        _use_skill(unit, state, 'ultimate')
    elif state.skill_points > 0 and state.extra.get('fuxuan_field_turns', 0) <= 0:
        state.extra['fuxuan_field_turns'] = 3  # 3回合阵法
        _use_skill(unit, state, 'skill')
        state.log.append('  符玄展开穷观阵(3回合)')
    else:
        _use_skill(unit, state, 'basic_attack')


def _trace_fuxuan_ult_heal(u, state, **kw):
    """符玄行迹「太乙式盘」: 施放终结技回5%生命上限"""
    if u.char.id != 'fu_xuan':
        return
    heal = u.max_hp * 0.05
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    state.log.append(f'  行迹·太乙式盘: 回5%生命 +{heal:.0f}')


def _trace_fuxuan_energy_regen(u, state, **kw):
    """符玄行迹「六壬兆堪」: 穷观阵激活时回合开始+20能量"""
    if u.char.id != 'fu_xuan':
        return
    u.current_energy = min(u.char.max_energy or 999, u.current_energy + 20)
    state.log.append(f'  行迹·六壬兆堪: 回合回20能量 ({u.current_energy:.0f})')


def _trace_fuxuan_cc_resist(u, state, **kw):
    """符玄行迹「遁甲星舆」: 穷观阵展开时重置全队控制抗性1次次数
    （v5.0 P4 激活: charges 由 _apply_player_status 消费, 免疫一次控制）"""
    if u.char.id != 'fu_xuan':
        return
    state.extra['fuxuan_cc_resist_charges'] = 1
    state.log.append('  行迹·遁甲星舆: 穷观阵控制抗性次数重置(待控制系统就位)')


def _eid_fuxuan_e6_loss(u, state, total_lost=0, affected=None, **kw):
    """符玄E6·种陵: 穷观阵激活时累计全队已损失生命(封顶符玄生命上限120%)

    v5.2 修复: 全队累计语义——队友掉血时 affected 里无符玄, 需从 state.units
    找持有者（照抄 _eid_fuxuan_e4 模式）, 不再依赖 u/affected 参数。"""
    fu = next((x for x in state.units
               if x.char.id == 'fu_xuan' and x.is_alive), None)
    if fu is None:
        return
    if state.extra.get('fuxuan_field_turns', 0) <= 0:
        return
    cap = fu.max_hp * 1.20
    cur = state.extra.get('fuxuan_lost_hp_total', 0.0)
    state.extra['fuxuan_lost_hp_total'] = min(cap, cur + total_lost)


def _fuxuan_e2_fatal_check(state):
    """符玄E2·柔兆: 穷观阵开启时我方受致命伤害→不死亡+回70%生命(单场1次)。
    v6.10.6 A4: 补 eidolon>=2 门控（此前 E0 开穷观阵即白嫖保护）"""
    fuxuan = next((x for x in state.units
                   if x.char.id == 'fu_xuan' and x.is_alive), None)
    if fuxuan is None or fuxuan.eidolon_rank < 2:
        return False
    if state.extra.get('fuxuan_e2_used'):
        return False
    if state.extra.get('fuxuan_field_turns', 0) <= 0:
        return False
    state.extra['fuxuan_e2_used'] = True
    for eu in state.units:
        if eu.is_alive:
            eu.current_hp = min(eu.max_hp, eu.current_hp + eu.max_hp * 0.70)
    state.log.append('  符玄E2·柔兆: 致命伤害保护触发, 全队回70%生命')
    return True


def _eid_fuxuan_e2(u, state, **kw):
    """符玄E2·柔兆: 初始化单场1次标记（保护逻辑见 _fuxuan_e2_fatal_check, 等受击闭环）"""
    if u.char.id != 'fu_xuan':
        return
    state.extra['fuxuan_e2_used'] = False
    state.log.append('  符玄E2: 致命保护就位(单场1次, 待受击闭环)')


def _eid_fuxuan_e1(u, state, **kw):
    """符玄E1: 鉴知→全队CD+30%"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.CRIT_DMG += 0.30
    state.log.append('  符玄E1: 鉴知→全队CD+30%')


def _eid_fuxuan_e4(u, state, **kw):
    """符玄E4: 穷观阵内队友受击→符玄回5能量（on_take_damage 闭环落地, 已改挂）"""
    if state.extra.get('fuxuan_field_turns', 0) <= 0:
        return
    fx = next((x for x in state.units if x.char.id == 'fu_xuan' and x.is_alive), None)
    if fx:
        fx.current_energy = min(fx.char.max_energy or 999, fx.current_energy + 5)
        state.log.append(f'  符玄E4: 受击回5能量 ({fx.current_energy:.0f})')


def _tech_fuxuan(state, u, is_opener):
    """符玄: 开启穷观阵——全队减伤18%+承伤65%分摊 + 鉴知(生命上限+6%+暴击+12%) 3回合
    （符玄.txt 秘技·太微行棋; 复用 _distribute_damage 承伤管线, fuxuan_field_turns 驱动）"""
    from engine.core.combat_engine import _gain_energy
    from engine.runtime import TimedBuff
    state.extra['fuxuan_field_turns'] = 3
    # v6.3.0b P1-6: 补能量恢复30（文本要求, 此前缺失）
    _gain_energy(u, 30.0, state=state)
    # v6.3.0b P1-6: 鉴知生命上限按符玄生命上限+6%, 落到实际 max_hp/current_hp
    # （角色+忆灵, 快照回退由 fuxuan_ai 阵法到期执行; HP_PERCENT 仅留暴击段）
    for eu in [x for x in state.units if x.is_alive] \
            + [x for x in state.memsprites if x.is_alive]:
        if 'fuxuan_tech_orig_maxhp' not in eu.extra:
            eu.extra['fuxuan_tech_orig_maxhp'] = eu.max_hp
        delta = u.base_stats.HP * 0.06
        eu.max_hp = eu.extra['fuxuan_tech_orig_maxhp'] + delta
        eu.current_hp = eu.current_hp + delta
        eu.buffs.append(TimedBuff(source_id='fu_xuan',
                                  attributes={'CRIT_RATE': 12.0},
                                  remaining_turns=3, param_id='fuxuan_tech_barrier'))
    state.log.append('[秘技] 太微行棋: 穷观阵3回合(减伤18%+承伤65%) + 鉴知(HP上限+6%按符玄HP/暴击+12%) + 回能30')


CHAR_ID = "fu_xuan"
AI = fuxuan_ai
TECHNIQUE = _tech_fuxuan


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _fuxuan_field_duration(u, state, attrs, skill):
    """EFFECT_MUTATORS['fuxuan_field']: 太卜阵形持续3回合。"""
    return attrs, 3


EFFECT_MUTATORS = {'fuxuan_field': _fuxuan_field_duration}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _fuxuan_damage_bonus_add(u, state, skill_key):
    """PHASE damage_bonus_add: E6 种陵——终结技+累计损失×200%（→bonus|None）。"""
    # 符玄E6·种陵: 终结技伤害+累计损失×200%（累计在 on_hp_loss 封顶符玄生命120%）
    if u.eidolon_rank >= 6 and skill_key == 'ultimate':
        bonus = state.extra.get('fuxuan_lost_hp_total', 0.0) * 2.0
        if bonus > 0:
            state.log.append(f'  E6种陵: 损失增幅+{bonus:.0f}')
            return bonus
    return None


def _fuxuan_post_attack_cleanup(u, state, skill_key):
    """PHASE post_attack_cleanup: E6 种陵——终结技结算后清空累计损失。"""
    # 符玄E6·种陵: 终结技结算后清空累计损失（循环外, 多目标只清一次）
    if u.eidolon_rank >= 6 and skill_key == 'ultimate':
        if state.extra.get('fuxuan_lost_hp_total', 0.0) > 0:
            state.extra['fuxuan_lost_hp_total'] = 0.0
            state.log.append('  E6种陵: 累计损失已清空')
    return None


PHASE_HOOKS['damage_bonus_add'] = _fuxuan_damage_bonus_add
PHASE_HOOKS['post_attack_cleanup'] = _fuxuan_post_attack_cleanup
