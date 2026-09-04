"""开拓者·欢愉（试点 M3）"""

import copy
import random
from engine.runtime import AV_PER_TURN, TimedBuff, _enemy_for_damage, _set_av
from engine.core.combat_engine import _build_effective_stats, _commit_enemy_damage, _gain_energy, _gain_skill_points, _skill_level_factor, _use_skill
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus


def _tb_skill_aftermath(state, u, skill_key):
    """v6.10.3 P1-4: 开拓者·欢愉战技/天赋/行迹2内联接线:
    - 战技: 获得20好活当赏; 持有好活时战技额外33%雷欢愉伤害(用全队最高好活层数计算)
    - 行迹2·阿哈咬它!: 我方目标施放欢愉技后→开拓者下次战技额外+2好活"""
    tb = next((x for x in state.units
               if x.char.id == 'trailblazer_elation' and x.is_alive), None)
    if tb is None:
        return
    has_trace2 = any(getattr(t, 'hook_name', '') == 'trailblazer_goodshow_boost'
                     for t in (tb.char.traces or []))
    if skill_key == 'elation_skill' and has_trace2:
        state.extra['tb_trace2_pending'] = True
        return
    if u is tb and skill_key == 'skill':
        elation = state.extra.get('_elation')
        if elation is None:
            from engine.systems.elation import ElationSystem
            elation = ElationSystem()
        elation.grant_good_show(state, 'trailblazer_elation', 20.0,
                                duration=2, source='trailblazer_skill')
        if state.extra.get('tb_trace2_pending') and has_trace2:
            state.extra.pop('tb_trace2_pending', None)
            elation.grant_good_show(state, 'trailblazer_elation', 2.0,
                                    duration=2, source='trailblazer_trace2')
            state.log.append('  开拓者行迹2: 下次战技额外+2好活')
        if state.elation_state.get_good_show_total('trailblazer_elation') > 0:
            best = max((state.elation_state.get_good_show_total(x.char.id)
                        for x in state.units if x.is_alive), default=0.0)
            stats = _build_effective_stats(tb, state)
            total = 0.0
            for e in (state.alive_enemies() or state.enemies):
                if getattr(e, 'HP', 0) <= 0:
                    continue
                talent_scale = 30.0 * _skill_level_factor(tb, 'talent')
                d = calculate_damage(stats, _enemy_for_damage(e), 0, talent_scale, 'elation',
                                     '雷', 80, stats.CRIT_RATE >= 0.5,
                                     laugh_n=best, crit_mode='expected')
                _commit_enemy_damage(state, tb, e, d.final_damage,
                                     damage_type='elation', skill_type='talent')
                total += d.final_damage
            tb.total_damage_dealt += total
            state.log.append(f'  开拓者天赋: 战技额外雷欢愉伤害{total:.0f}(最高好活{best:.0f})')


def _trace_tb_cr_and_sp(u, state, **kw):
    """开拓者·欢愉行迹1·跟你爆了: 自身暴击率+15%(动态面板) + 施放终结技后回1SP"""
    if u.char.id != 'trailblazer_elation' or kw.get('skill_key') != 'ultimate':
        return
    from engine.core.combat_engine import _gain_skill_points
    _gain_skill_points(state, 1)
    state.log.append('  开拓者行迹1: 终结技后回1SP')


def _eid_tb_elation_e1(u, state, **kw):
    """开拓者E1: 战技后下次终结技好活+2, 叠3层"""
    st = getattr(u, 'relic_stacks', {}) or {}
    cur = st.get('tb_e1', 0)
    if cur < 3:
        cur += 1
        st['tb_e1'] = cur
        u.relic_stacks = st


def _eid_tb_elation_e2(u, state, **kw):
    """开拓者E2: 终结技指定单体欢愉度+12%, 2回合
    v6.10.3 P1-4: 目标改为终结技实际选择目标（此前硬编码银狼）"""
    from engine.runtime import TimedBuff
    target = u.extra.get('lc_last_skill_target')
    if target is None or not getattr(target, 'is_alive', False):
        target = u
    target.buffs = [b for b in target.buffs
                    if getattr(b, 'param_id', '') != 'tb_e2']
    tb = TimedBuff(source_id="tb_e2", attributes={"ELATION_LEVEL": 12.0},
                   remaining_turns=2, param_id="tb_e2",
                   source_name="开拓者E2·欢愉度")
    target.buffs.append(tb)


def _eid_tb_elation_e4(u, state, **kw):
    """开拓者E4: 欢愉技→敌方受伤+10%, 2回合
    v6.10.3 P1-4: 改为敌方2回合状态（此前永久叠面板且无到期）"""
    from engine.models.enemy import EnemyStatus
    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0:
            e.add_status(EnemyStatus(id='tb_e4_vuln', name='易伤', category='debuff',
                                     source='trailblazer_elation', remaining_turns=2,
                                     attributes={'vulnerability': 0.10}))


def _eid_tb_elation_e6(u, state, **kw):
    """开拓者E6: 欢愉技→自身CD+100%, 3回合"""
    from engine.runtime import TimedBuff
    u.buffs = [b for b in u.buffs
               if getattr(b, 'param_id', '') != 'tb_e6']
    tb = TimedBuff(source_id="tb_e6", attributes={"CRIT_DMG": 100.0},
                   remaining_turns=3, param_id="tb_e6",
                   source_name="开拓者E6·银河传奇")
    u.buffs.append(tb)


def _tb_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        # v6.10.3 P1-4: 通用指定单体分支（此前硬编码银狼）
        target = u.extra.get('lc_last_skill_target')
        if target is None:
            target = next((x for x in state.units if x.is_alive and x is not u), None)
        if target is None:
            return
        # E1: 战技叠层→终结技目标好活+2×层数（最多3层, 消费后清零）
        e1_stacks = (u.relic_stacks or {}).get('tb_e1', 0)
        if u.eidolon_rank >= 1 and e1_stacks > 0:
            elation.grant_good_show(state, target.char.id, 2.0 * e1_stacks, source="tb_e1")
            u.relic_stacks['tb_e1'] = 0
            state.log.append(f'  开拓者E1: 终结技好活+{2.0 * e1_stacks:.0f}')
        if getattr(target.char, 'skills', {}).get('elation_skill'):
            elation.grant_good_show(state, target.char.id, 10, source="tb_ult")
            state.log.append(f'  主角终结技→{target.char.name}: +10好活当赏, 立即欢愉技(固定计入20笑点)')
            _use_skill(target, state, "elation_skill", laugh_n_override=20.0)
        else:
            from engine.core.combat_engine import _effective_spd
            from engine.characters.robin_summeretto import _guest_advance_blocked
            from engine.runtime import AV_PER_TURN, _set_av
            navs = state.extra.get('navs', {})
            idx = next((i for i, x in enumerate(state.units) if x is target), None)
            if idx is not None and idx in navs \
                    and not _guest_advance_blocked(state, u, target):
                advanced_av = max(
                    state.current_av,
                    navs[idx] - (AV_PER_TURN / max(_effective_spd(target, state), 1.0)) * 0.5,
                )
                _set_av(state, navs, idx, advanced_av)
                state.log.append(f'  主角终结技→{target.char.name}: 行动提前50%')
    elif state.skill_points >= 2:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _laugh_gen(u, state, skill_key):
    """笑点生成（通用形态: 3 + 角色加成 + 好活加成）"""
    is_tb_elation = (u.char.id == 'trailblazer_elation' and skill_key == 'elation_skill')
    if u.char.path != "欢愉" or (skill_key not in ("basic_attack", "skill") and not is_tb_elation):
        return
    bonus = {"yaoguang": 3, "trailblazer_elation": 3}.get(u.char.id, 0)
    if bonus and u.char.id == "trailblazer_elation":
        _gain_energy(u, 10.0, state=state)  # v5.7: 统一入口
    if state.elation_state.get_good_show_total(u.char.id) > 0:
        bonus += 3
    laugh = 3 + bonus
    state.laugh_points += laugh


CHAR_ID = "trailblazer_elation"
ELATION_GATED = True  # AI/SKILL_HOOKS 仅欢愉队激活（M3 语义保持）
SKILL_HOOKS = [_laugh_gen]
AI = _tb_ai


OBSERVER_HOOKS = {}


# ---- v7.15.0 相位: 开拓者·欢愉站点（原 elation 内联, verbatim 迁入）----

def _tb_tech_init(_u, state):
    """OBSERVER init_tb_tech: 秘技·燃起来了——随机欢愉度+30%/20%（25%/75%权重）3回合。"""
    import random as _rnd
    from engine.runtime import TimedBuff
    # TXT 未给精确权重；暂用 25%/75% 保持"小概率/大概率"关系。
    val = _rnd.choices([0.30, 0.20], weights=[1, 3], k=1)[0]
    for u in state.units:
        if u.is_alive:
            u.buffs.append(TimedBuff(source_id="trailblazer_elation",
                                     attributes={"ELATION_LEVEL": val * 100.0},
                                     remaining_turns=3,
                                     param_id="tb_tech_elation",
                                     source_name="开拓者秘技·燃起来了"))
    state.log.append(f'[Init] 主角秘技: 全队欢愉度+{val*100:.0f}%(3回合)')
    return None


OBSERVER_HOOKS['init_tb_tech'] = _tb_tech_init
