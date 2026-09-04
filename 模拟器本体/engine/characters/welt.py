"""welt（M4 收官批迁入）"""

import copy
import random
from engine.runtime import AV_PER_TURN, _enemy_for_damage
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _enemy_eff_spd
from engine.core.combat_engine import _gain_energy
from engine.core.combat_engine import _roll_effect_hit
from engine.core.combat_engine import _use_skill


def _welt_apply_slow(state, u, target):
    """战技命中减速10% 2回合（75%基础概率, 走EHR）"""
    from engine.models.enemy import EnemyStatus
    from engine.core.combat_engine import _roll_effect_hit
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if not _roll_effect_hit(u, state, target, '减速', base_chance=0.75):
        return
    target.add_status(EnemyStatus(id='welt_slow', name='减速', category='debuff',
                                  source='welt', remaining_turns=2,
                                  attributes={'spd_down': 0.10}))
    state.log.append(f'  瓦尔特减速: {target.name or target.id} 速度-10% 2回合')


def _welt_apply_shizhong(state, u, target):
    """【失重】2回合: DEF-40%+速度-5%; 行迹1受伤+10%叠10层; E4全抗-30%"""
    from engine.models.enemy import EnemyStatus
    if target is None:
        return
    attrs = {'def_reduction': 0.40, 'spd_down': 0.05, 'welt_trace1_stacks': 0}
    if u.eidolon_rank >= 4:
        attrs['res_down'] = 0.30
    target.add_status(EnemyStatus(id='welt_shizhong', name='失重', category='debuff',
                                  source='welt', remaining_turns=2, attributes=attrs))
    if u.eidolon_rank >= 4:
        state.log.append(f'  瓦尔特E4: {target.name or target.id} 全抗-30%')
    state.log.append(f'  瓦尔特终结技: {target.name or target.id} 【失重】2回合(DEF-40%)')


def _welt_apply_jinggu(state, u, target, delay_ratio=0.12):
    """v6.9.1: 禁锢1回合——走统一控制类别与 EHR 检定; 延后按目标回合值比例计算"""
    from engine.models.enemy import EnemyStatus
    if target is None:
        return
    from engine.core.combat_engine import AV_PER_TURN, _roll_effect_hit, _enemy_eff_spd
    if not _roll_effect_hit(u, state, target, '禁锢', base_chance=1.0):
        return
    delay = AV_PER_TURN / max(_enemy_eff_spd(target), 1.0) * delay_ratio

    target.add_status(EnemyStatus(id='welt_jinggu', name='禁锢', category='control',
                                  source='welt', remaining_turns=1,
                                  attributes={'spd_down': 0.10, 'delay_ratio': delay_ratio,
                                              'delay_amount': delay}))
    state.log.append(f'  瓦尔特禁锢: {target.name or target.id} 行动延后{delay_ratio*100:.0f}%')


def _welt_ult(state, u):
    """终结技: 150%ATK全体(引擎倍率)+禁锢+失重; 行迹3额外5能"""
    from engine.core.combat_engine import _gain_energy
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        _welt_apply_jinggu(state, u, e)
        _welt_apply_shizhong(state, u, e)
    if any(getattr(tr, 'hook_name', '') == 'welt_trace3' for tr in (u.char.traces or [])):
        _gain_energy(u, 5.0, state=state)
        state.log.append('  瓦尔特行迹3: 终结技额外5能量')


def _welt_extra_damage(state, u, skill_key):
    """附加伤害统一结算（伤害循环后）:
    - 天赋: 击中减速目标→100%ATK虚数附加（E2回3能）
    - 行迹2: 普攻/战技附加80%/120%倍率
    - E1: 失重目标被终结技击中→40%终结技倍率附加(每目标每次攻击1次)
    - 失重目标受击行动延后4%（每目标每回合最多8次）"""
    from engine.core.combat_engine import (_build_effective_stats, calculate_damage, _commit_enemy_damage, _enemy_for_damage, _gain_energy)
    alive = state.alive_enemies() or state.enemies
    targets = state.extra.get('last_attack_targets', []) or alive
    stats = _build_effective_stats(u, state)
    trace2 = any(getattr(tr, 'hook_name', '') == 'welt_trace2' for tr in (u.char.traces or []))
    trace3 = any(getattr(tr, 'hook_name', '') == 'welt_trace3' for tr in (u.char.traces or []))
    # 行迹3: EHR>40%每超10% ATK+20%上限80%
    if trace3 and stats.EFFECT_HIT_RATE > 0.40:
        extra_atk = min(int((stats.EFFECT_HIT_RATE - 0.40) / 0.10) * 0.20, 0.80)
        stats = copy.deepcopy(stats)
        stats.ATK *= (1.0 + extra_atk)
    # v6.9.1: 天赋逐段消费（txt:54 每击中1次判定; 弹射用重复段序列）;
    # 行迹2/E1 每目标1次（txt 施放技能「1次附加」）
    segs = state.extra.get('last_hit_segments', []) or targets
    seen_t2 = set()
    seen_e1 = set()
    total = 0.0
    for t in segs:
        if t is None or getattr(t, 'HP', 0) <= 0:
            continue
        t_stats = stats
        is_slow = t.has_status(status_id='welt_slow') or t.has_status(name='减速')
        # E6: 施放战技/终结技击中减速目标→本次伤害双暴（主伤害已由伤害循环吃, 附加段同样生效）
        if is_slow and u.eidolon_rank >= 6 and skill_key in ('skill', 'ultimate'):
            t_stats = copy.deepcopy(t_stats)
            t_stats.CRIT_RATE += 0.30
            t_stats.CRIT_DMG += 0.60
        # 战技天赋已经在 _multihit_damage 按逐段先后结算。
        if is_slow and skill_key != 'skill':
            before = t.HP
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, 100.0,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type=skill_key, crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            if u.eidolon_rank >= 2:
                _gain_energy(u, 3.0, state=state)
                state.log.append('  瓦尔特E2: 天赋触发回3能量')
        # 行迹2: 普攻80%/战技86.4%（=72%×1.2, txt:71）; 终结技不触发; 每目标1次
        if trace2 and skill_key in ('basic_attack', 'skill') and id(t) not in seen_t2:
            seen_t2.add(id(t))
            scale = 80.0 if skill_key == 'basic_attack' else 86.4
            before = t.HP
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, scale,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type=skill_key, crit_mode='expected')
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
        # E1: 战技/终结技击中失重目标→40%终结技倍率附加（每目标每次攻击1次）
        if skill_key in ('skill', 'ultimate') and u.eidolon_rank >= 1 \
                and t.has_status(status_id='welt_shizhong') and id(t) not in seen_e1:
            seen_e1.add(id(t))
            d = calculate_damage(t_stats, _enemy_for_damage(t), t_stats.ATK, 60.0,
                                 'direct', '虚数', 80, t_stats.CRIT_RATE >= 0.5,
                                 skill_type='ultimate', crit_mode='expected')
            # v6.10.3 P1-7: _commit_enemy_damage 内部已统一计杀, 删除手动 _record_enemy_kill（双计）
            _commit_enemy_damage(state, u, t, d.final_damage)
            total += d.final_damage
            state.log.append(f'  瓦尔特E1: 失重目标附加40%终结技倍率 {d.final_damage:.0f}')
    u.total_damage_dealt += total
    if total > 0:
        state.log.append(f'  瓦尔特附加伤害: {total:.0f}')


def _welt_talent_hit(state, u, target, stats, skill_type):
    """Resolve one Welt talent hit after a skill segment hit an already slowed target."""
    if target is None or target.HP <= 0:
        return 0.0
    before = target.HP
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, 100.0,
                         'direct', '虚数', 80, stats.CRIT_RATE >= 0.5,
                         skill_type=skill_type, crit_mode='expected')
    _commit_enemy_damage(state, u, target, d.final_damage)
    if u.eidolon_rank >= 2:
        _gain_energy(u, 3.0, state=state)
        state.log.append('  瓦尔特E2: 天赋触发回3能量')
    return d.final_damage


def _welt_skill_slow(state, u):
    """战技逐段命中减速（last_hit_segments 逐段75%概率）"""
    segs = state.extra.get('last_hit_segments', []) or state.extra.get('last_attack_targets', [])
    for t in segs:
        if t is not None and getattr(t, 'HP', 0) > 0:
            _welt_apply_slow(state, u, t)


def _welt_ally_hit_hooks(state, skill_key):
    """v6.9.1（Codex P2-2）: 失重通用受击钩子——任何我方攻击命中失重目标:
    - txt:48 受击行动延后4%（每目标每回合最多8次, 此前仅瓦尔特攻击触发）
    - txt:66 行迹1 易伤+10% 叠层（最多10层, 持续2回合, 此前固定10%）"""
    welt = next((x for x in state.units if x.char.id == 'welt' and x.is_alive), None)
    if welt is None:
        return
    trace1 = any(getattr(tr, 'hook_name', '') == 'welt_trace1' for tr in (welt.char.traces or []))
    for t in state.extra.get('last_attack_targets', []):
        if t is None or getattr(t, 'HP', 0) <= 0:
            continue
        st = next((s for s in t.statuses if s.id == 'welt_shizhong'), None)
        if st is None:
            continue
        # 受击延后4%（每回合最多8次）
        cnt = t.extra.get('welt_shizhong_count', 0)
        if cnt < 8:
            t.extra['welt_shizhong_count'] = cnt + 1
            t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 400.0
            state.log.append(f'  【失重】受击延后4% ({cnt+1}/8)')
        # 行迹1 叠层（最多10层）
        if trace1:
            stacks = min(10, st.attributes.get('welt_trace1_stacks', 0) + 1)
            st.attributes['welt_trace1_stacks'] = stacks
            st.attributes['vulnerability'] = 0.10 * stacks


def _welt_ai(u, state, *, elation=None, max_av=1000, navs=None, uidx=0, **__):
    """瓦尔特 AI: 满能量终结技→战技→普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _trace_welt_trace1(u, state, **kw):
    """瓦尔特行迹1·惩戒: 战斗开始恢复30能量"""
    if u.char.id != 'welt':
        return
    from engine.core.combat_engine import _gain_energy
    _gain_energy(u, 30.0, state=state)
    state.log.append('  瓦尔特行迹1: 开局回30能量')


def _tech_welt(state, u, is_opener):
    """瓦尔特: 全敌禁锢1回合(行动延后20%+减速)（非进战·领域, 领域互斥）"""

    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0:
            _welt_apply_jinggu(state, u, e, delay_ratio=0.20)
    state.log.append('[秘技] 画地为牢: 全敌禁锢1回合(延后20%+减速)')


def _skill_hook_0(u, state, skill_key):
    if u.char.id == CHAR_ID and skill_key == "ultimate":
        _welt_ult(state, u)


CHAR_ID = "welt"
AI = _welt_ai
TECHNIQUE = _tech_welt
SKILL_HOOKS = [_skill_hook_0]


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _welt_post_attack_extra(u, state, skill_key, total_dmg):
    """PHASE post_attack_extra: 附加伤害(减速目标/行迹2/E1) + 战技逐段减速 + 失重推条。"""
    # v6.9 瓦尔特: 附加伤害(减速目标100%/行迹2 80-120%/E1 40%) + 战技逐段减速 + 失重推条
    if total_dmg > 0:
        _welt_extra_damage(state, u, skill_key)
    return None


PHASE_HOOKS['post_attack_extra'] = _welt_post_attack_extra
