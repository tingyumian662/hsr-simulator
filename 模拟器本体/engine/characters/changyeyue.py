"""长夜月（M4 批2a；_cy_ai 实迁——唯一不依赖忆灵系统的记忆 AI）"""

import copy
import random
from engine.runtime import TimedBuff
from engine.core.combat_engine import _use_skill


def _dispatch_changyeyue_hp_loss(state, affected):
    """长夜月天赋·今夜与我同行（用户提供长夜月.txt）:
    长夜月或忆灵「长夜」生命降低 → 双方暴伤+60% 2回合 + 长夜月+2忆质。
    每目标每次受击最多触发1次——按受击/扣血事件分发, 天然满足。"""
    for af, _lost in affected:
        cy = None
        char = getattr(af, 'char', None)
        cid = getattr(char, 'id', None)
        if cid == 'changyeyue':
            cy = af
        elif getattr(af, 'summoner_id', '') == 'changyeyue':
            cy = next((x for x in state.units
                       if x.char.id == 'changyeyue' and x.is_alive), None)
        if cy is None:
            continue
        from engine.core.relic_conditions import _apply_timed_buff
        _apply_timed_buff(cy, state, 'CRIT_DMG', 60.0, 2, source='天赋·今夜与我同行',
                          param_id='changyeyue_talent_cd')
        if cy.memsprite_unit and cy.memsprite_unit.is_alive:
            _apply_timed_buff(cy.memsprite_unit, state, 'CRIT_DMG', 60.0, 2,
                              source='天赋·今夜与我同行', param_id='changyeyue_talent_cd')
        from engine.systems.remembrance import _gain_yizhi
        _gain_yizhi(state, cy, 2)
        state.log.append(f'  今夜与我同行: 生命降低→忆质+2 ({cy.yizhi}), 双方暴伤+60%')


def _changyeyue_trace2(u, state, **kw):
    """行迹2: 施放技能消耗5%HP→双方CD+15%/2回合
    v5.6.1: 实机"持续2回合"→同源刷新（原实现 base_stats 永久叠加, 战技16次→CD+240%面板失控）"""
    if u.char.id != "changyeyue":
        return
    hp_cost = u.current_hp * 0.05
    u.current_hp -= hp_cost
    from engine.core.relic_conditions import _apply_timed_buff
    _apply_timed_buff(u, state, 'CRIT_DMG', 15.0, 2, source='行迹2·天黑黑月寂寂',
                      param_id='changyeyue_trace2_cd')
    if u.memsprite_unit:
        _apply_timed_buff(u.memsprite_unit, state, 'CRIT_DMG', 15.0, 2, source='行迹2·天黑黑月寂寂',
                          param_id='changyeyue_trace2_cd')


def _changyeyue_trace3(u, state, skill_key=None, **kw):
    """行迹3: 施放技能→+5能量+1忆质。战斗开始→+70能量+1忆质(在remembrance.init_battle处理)
    献予「岁月」之诗: 战技/终结技后额外+1忆质
    战技效果(白昼悄然离去, 用户提供长夜月.txt): 施放时获得2点忆质, 至暗之谜状态额外12点"""
    if u.char.id != "changyeyue":
        return
    from engine.systems.remembrance import _gain_yizhi  # E2: 每获得事件额外+2（Codex 审查第3项补全）
    u.current_energy = min(u.char.max_energy, u.current_energy + 5)
    _gain_yizhi(state, u, 1)
    if skill_key == 'skill':
        extra = 2 + (12 if u.is_darkness else 0)
        _gain_yizhi(state, u, extra)
        state.log.append(f'  战技忆质: +{extra} (至暗={u.is_darkness}) → {u.yizhi}')
    if u.extra.get('poem_suiyue') and skill_key in ('skill', 'ultimate'):
        _gain_yizhi(state, u, 1)
        state.log.append(f'  献予「岁月」之诗: 战技/终结技→忆质+1 ({u.yizhi})')


def _tech_changyeyue(state, u, is_opener):
    """长夜月: 全队忆灵暴伤+24%(与战技同实现) 2回合 + 忆质+1（长夜月.txt 秘技·愿有冷雨落下）"""
    from engine.runtime import TimedBuff
    u.buffs.append(TimedBuff(source_id='changyeyue', attributes={'CRIT_DMG': 24.0},
                             remaining_turns=2, param_id='changyeyue_tech_cd'))
    from engine.systems.remembrance import _gain_yizhi
    _gain_yizhi(state, u, 1)
    state.log.append('[秘技] 愿有冷雨落下: 忆灵暴伤+24%(与战技同) + 忆质+1')


def _cy_ai(unit, state, **ctx):
    if unit.current_energy >= unit.char.max_energy:
        _use_skill(unit, state, "ultimate")
    else:
        _use_skill(unit, state, "skill")


CHAR_ID = "changyeyue"
AI = _cy_ai
TECHNIQUE = _tech_changyeyue


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _changyeyue_ult_state_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['changyeyue_ult_state']: 终结技→至暗之谜。"""
    if u.char.id != 'changyeyue':
        return None
    # 长夜月终结技→至暗之谜
    # v6.2.1: 防重入——重复终结技仅刷新充能, 加成不叠加（Harness P1-2）
    if not u.is_darkness:
        # 至暗之谜: 敌方全体受伤+30%（施加到敌方，全队受益）
        for e in state.enemies:
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.30
        u.base_stats.DMG_BONUS_ALL += 0.60
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.DMG_BONUS_ALL += 0.60
    u.is_darkness = True
    u.darkness_charges = 2
    if u.eidolon_rank >= 2:
        u.darkness_charges += 2
    state.log.append(f'  进入【至暗之谜】(充能={u.darkness_charges}): 敌方受伤+30%, 伤害+60%')
    return True


def _changyeyue_skill_cd_mutator(u, state, attrs, skill):
    """EFFECT_MUTATORS['changyeyue_skill_cd']: 岁月之诗加成战技CD。"""
    # 献予「岁月」之诗: 长夜月战技CD额外+长夜月暴伤12%
    if u.extra.get('poem_suiyue'):
        attrs = {'CRIT_DMG': 24.0 + u.base_stats.CRIT_DMG * 12.0}
    return attrs, 2


EFFECT_TAKEOVERS = {'changyeyue_ult_state': _changyeyue_ult_state_takeover}
EFFECT_MUTATORS = {'changyeyue_skill_cd': _changyeyue_skill_cd_mutator}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_suiyue(state, summoner, ms_unit, cy):
    """献予「岁月」之诗(整场): 迷梦+18%; 战技/终结技后+1忆质; 战技CD额外+长夜月暴伤12%"""
    cy.extra['poem_suiyue'] = True
    state.log.append('  献予「岁月」之诗: 迷梦+18%, 忆质+1, 战技CD强化')


POEM = ("岁月", _poem_suiyue, True)


PHASE_HOOKS = {}


OBSERVER_HOOKS = {}


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _cy_yizhi_gain(u, state, old):
    """PHASE yizhi_gain: 忆质跨16→解控+「长夜」立即行动（每轮一次）。"""
    if not (u.char.id == 'changyeyue' and old < 16 <= u.yizhi):
        return None
    # 解除自身控制类负面状态
    removed = [s for s in u.statuses if getattr(s, 'category', '') == 'control']
    for s in removed:
        u.statuses.remove(s)
        state.log.append(f'  忆质≥16: {u.char.name}解除{s.name}')
    # 「长夜」立即行动（每轮一次; 迷梦释放后长夜消失, 重新召唤时复位）
    if u.memsprite_unit and u.memsprite_unit.is_alive \
            and not u.extra.get('cy_immediate_done') \
            and not state.extra.get('_ms_acting'):
        from engine.systems.remembrance import RemembranceSystem
        rem = state.extra.get('_rem_sys')
        if rem:
            u.extra['cy_immediate_done'] = True
            rem._force_memsprite_action(state, u, u.memsprite_unit)
            state.log.append('  忆质≥16: 「长夜」立即行动!')
    return None


def _cy_rem_init(u, state):
    """PHASE rem_init: 战斗开始自动召唤长夜+70能量+1忆质+双方CR+35%。"""
    from engine.core.combat_engine import _gain_energy
    from engine.systems.remembrance import RemembranceSystem, _gain_yizhi
    rem = state.extra.get('_rem_sys') or RemembranceSystem()  # 装配期实例未挂 state 兜底
    ms_data = u.char.memsprite
    if ms_data:
        rem.summon_memsprite(state, u, ms_data)
        # v6.2.1: 统一回能入口（Codex P2-5 审计: 直写绕过 ER/能量事件/迷迷 bank）
        _gain_energy(u, 70.0, state=state)
        _gain_yizhi(state, u, 1)  # v5.1: E2 每获得忆质+2
        state.log.append(f'[Init] 长夜月天赋: 召唤长夜(SPD={ms_data.base_SPD}), +70能量, +1忆质')
        u.base_stats.CRIT_RATE += 0.35
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.CRIT_RATE += 0.35
    return None


def _cy_ms_stats_premod(u, state, ms_stats):
    """PHASE ms_stats_premod: E2 长夜月与「长夜」暴击伤害+40%（一次性）。"""
    # v5.7: copy 后单点应用, 双方各加一次; init_battle 首次召唤先于 enter_battle hook
    if u.eidolon_rank >= 2 and not u.extra.get('cy_e2_cd_applied'):
        u.base_stats.CRIT_DMG += 0.40
        ms_stats.CRIT_DMG += 0.40
        u.extra['cy_e2_cd_applied'] = True
        state.log.append('  长夜月E2: 双方暴击伤害+40%')
    return None


def _cy_ms_created(u, state, ms_unit):
    """PHASE ms_created: 孤独浮游漆黑——长夜在场双方伤害+50%；立即行动标记复位。"""
    for holder in (u, ms_unit):
        holder.buffs.append(TimedBuff(
            source_id='changyeyue', attributes={'DMG_BONUS_ALL': 50.0},
            remaining_turns=-1, param_id='changyeyue_night_abyss',
            source_name='孤独浮游漆黑'))
    # 忆质≥16 立即行动标记复位（迷梦后重新召唤可再次触发）
    u.extra['cy_immediate_done'] = False
    state.log.append('  孤独浮游漆黑: 长夜在场, 双方伤害+50%')
    return None


def _cy_ms_despawn(u, state, ms_unit, ms_name):
    """PHASE ms_despawn_settle: 与你再见无期 SPD+10%+忆质%；移除孤独浮游漆黑。"""
    # v6.2.1: 忆质用量读清零前快照（迷梦块先清零再 despawn, 直接读=0）
    yizhi_consumed = u.extra.pop('yizhi_consumed_snapshot', u.yizhi)
    spd_bonus = 10 + min(yizhi_consumed, 40)
    # v6.2.1: 加速无法叠加——旧加成先回减再施新（此前永久叠加漂移）
    old_amt = u.extra.pop('night_spd_bonus_amt', 0.0)
    if old_amt > 0:
        u.base_stats.SPD -= old_amt
    amt = u.base_stats._base_SPD * (spd_bonus / 100.0)
    u.base_stats.SPD += amt
    u.extra['night_spd_bonus_amt'] = amt
    state.log.append(f'  {ms_name}消失→长夜月SPD+{spd_bonus}%(下回合移除), 累计忆质={yizhi_consumed}')
    u.extra['night_spd_bonus_turns'] = 1
    # v5.7 孤独浮游漆黑: 「长夜」消失→移除双方+50%伤害
    u.buffs = [b for b in u.buffs
               if getattr(b, 'param_id', '') != 'changyeyue_night_abyss']
    return True


def _cy_turn_tick(u, state):
    """PHASE turn_tick_rem: 至暗之谜倒计时 + 与你再见无期 SPD 到期回减。"""
    from engine.systems.remembrance import _exit_darkness
    # 至暗之谜回合倒计时
    if u.is_darkness and u.darkness_charges <= 0:
        _exit_darkness(state, u)
    # v6.2.1: 与你再见无期 SPD 加成到期回减（此前只减计数器不回减→永久漂移）
    spd_turns = u.extra.get('night_spd_bonus_turns', 0)
    if spd_turns > 0:
        spd_turns -= 1
        if spd_turns <= 0:
            amt = u.extra.pop('night_spd_bonus_amt', 0.0)
            if amt > 0:
                u.base_stats.SPD -= amt
                state.log.append(f'  与你再见无期到期: SPD-{amt:.0f}')
            u.extra['night_spd_bonus_turns'] = 0
        else:
            u.extra['night_spd_bonus_turns'] = spd_turns
    return None


def _cy_ms_cast_tick(u, state):
    """OBSERVER ms_cast_cy_tick: 任意我方忆灵施技→长夜月+5能量+1忆质。"""
    from engine.core.combat_engine import _gain_energy
    from engine.systems.remembrance import _gain_yizhi
    cy = next((x for x in state.units
               if x.char.id == 'changyeyue' and x.is_alive), None)
    if cy:
        _gain_energy(cy, 5.0, state=state)
        _gain_yizhi(state, cy, 1)
    return None


def _cy_ms_stats_mod(u, state, ms_stats):
    """OBSERVER ms_cy_stats_mod: 长夜 cd buff 注入忆灵面板 + 行迹1 暴伤。"""
    t1_cy = next((x for x in state.units
                  if x.char.id == 'changyeyue' and x.is_alive), None)
    cy_cd_buffs = []
    if t1_cy:
        cy_cd_buffs = [
            b for b in t1_cy.buffs
            if getattr(b, 'param_id', '') in ('changyeyue_skill_cd',
                                              'changyeyue_tech_cd')
        ]
    if cy_cd_buffs:
        ms_stats = copy.deepcopy(ms_stats)
        ms_stats.CRIT_DMG += sum(
            b.attributes.get('CRIT_DMG', 0.0) / 100.0
            for b in cy_cd_buffs
        )
    # 长夜月行迹1·天亮了，雨落了: 战技持续期间, 按队伍记忆命途数量给忆灵暴伤加成
    skill_cd_active = any(
        getattr(b, 'param_id', '') == 'changyeyue_skill_cd'
        for b in cy_cd_buffs
    )
    if t1_cy and skill_cd_active:
        from engine.core.character_utils import count_remembrance
        n = max(min(count_remembrance(state.units), 4), 1)
        t1_bonus = {1: 0.05, 2: 0.15, 3: 0.50, 4: 0.65}[n]
        ms_stats.CRIT_DMG += t1_bonus
        state.log.append(f'  天亮了，雨落了: 忆灵暴伤+{t1_bonus * 100:.0f}% (记忆×{n})')
    return ms_stats


def _cy_scale_mult(u, state, alive):
    """OBSERVER ms_scale_cy_mult: E1 敌方数→我方忆灵伤害倍率乘区。"""
    cy_owner = next((x for x in state.units
                     if x.char.id == 'changyeyue' and x.is_alive), None)
    if cy_owner and cy_owner.eidolon_rank >= 1:
        n = max(min(len(alive), 4), 1)
        return {4: 1.2, 3: 1.25, 2: 1.3, 1: 1.5}[n]
    return None


def _cy_scale_mod(u, state, skill_key, scale):
    """PHASE ms_scale_mod: 献予「岁月」之诗——迷梦伤害+18%。"""
    if skill_key == "memsprite_skill" and u.extra.get('poem_suiyue'):
        return scale * 1.18
    return None


def _cy_tough_eff(u, state):
    """OBSERVER ms_tough_eff: E4 在场时我方忆灵削韧效率+25%; 长夜自身再+25%。"""
    cy_owner = next((x for x in state.units
                     if x.char.id == 'changyeyue' and x.is_alive), None)
    if cy_owner and cy_owner.eidolon_rank >= 4:
        mult = 1.25
        if u.char.id == 'changyeyue':
            mult *= 1.25
        return mult
    return None


PHASE_HOOKS['yizhi_gain'] = _cy_yizhi_gain
PHASE_HOOKS['rem_init'] = _cy_rem_init
PHASE_HOOKS['ms_stats_premod'] = _cy_ms_stats_premod
PHASE_HOOKS['ms_created'] = _cy_ms_created
PHASE_HOOKS['ms_despawn_settle'] = _cy_ms_despawn
PHASE_HOOKS['turn_tick_rem'] = _cy_turn_tick
PHASE_HOOKS['ms_scale_mod'] = _cy_scale_mod
OBSERVER_HOOKS['ms_cast_cy_tick'] = _cy_ms_cast_tick
OBSERVER_HOOKS['ms_cy_stats_mod'] = _cy_ms_stats_mod
OBSERVER_HOOKS['ms_scale_cy_mult'] = _cy_scale_mult
OBSERVER_HOOKS['ms_tough_eff'] = _cy_tough_eff
