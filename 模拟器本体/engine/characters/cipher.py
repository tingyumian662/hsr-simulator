"""cipher（M4 收官批迁入）"""

import copy
import random
from engine.runtime import _enemy_for_damage, _tech_enemies
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _roll_effect_hit
from engine.core.combat_engine import _skill_level_factor


def _cipher_set_laozhuke(state, u, target):
    """设置唯一【老主顾】，并同步整场减防诗词的目标倍率。"""
    if target is None:
        return None
    for e in state.enemies:
        e.extra['cipher_laozhuke'] = e is target
    if u.extra.get('poem_guiji'):
        from engine.models.enemy import EnemyStatus
        for e in state.enemies:
            if getattr(e, 'HP', 0) <= 0:
                continue
            val = 0.20 if e is target else 0.12
            st = next((s for s in e.statuses if s.id == 'cipher_guiji_def'), None)
            if st is not None:
                st.attributes['def_reduction'] = val
            else:
                e.add_status(EnemyStatus(id='cipher_guiji_def', name='DEF降低',
                                         category='debuff', source='cipher',
                                         remaining_turns=-1,
                                         attributes={'def_reduction': val}))
    return target


def _cipher_pick_laozhuke(state, u):
    """【老主顾】: 生命上限最高者（仅最新）"""
    alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
    if not alive:
        return None
    target = max(alive, key=lambda e: getattr(e, 'max_hp', e.HP))
    return _cipher_set_laozhuke(state, u, target)


def _cipher_ensure_laozhuke(state):
    cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cp is None:
        return None
    if any(getattr(e, 'HP', 0) > 0 and e.extra.get('cipher_laozhuke')
           for e in state.enemies):
        return None
    return _cipher_pick_laozhuke(state, cp)


def _cipher_record(state, u, target, dmg, is_laozhuke=None, *,
                   rate_multiplier=1.0, extra_rate=0.0):
    """记录伤害: 老主顾12%/其他8%（不含溢出; 行迹1速度档加成）"""
    if dmg <= 0:
        return
    if is_laozhuke is None:
        is_laozhuke = bool(target.extra.get('cipher_laozhuke'))
    rate = 0.12 if is_laozhuke else 0.08
    rate *= _skill_level_factor(u, 'talent')
    effective = _build_effective_stats(u, state)
    spd = effective.SPD + effective._base_SPD * effective.SPD_PERCENT
    if spd >= 170:
        rate *= 2.0
    elif spd >= 140:
        rate *= 1.5
    if u.eidolon_rank >= 1:
        rate *= 1.5
    rate = rate * max(float(rate_multiplier), 0.0) + max(float(extra_rate), 0.0)
    u.extra['cipher_record'] = u.extra.get('cipher_record', 0.0) + dmg * rate


def _cipher_e4_extra(state, cp, target):
    """赛飞儿E4: 【老主顾】受我方攻击→赛飞儿对其50%ATK量子附加伤害"""
    stats = _build_effective_stats(cp, state)
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, 50.0,
                         'additional', '量子', 80, False,
                         crit_mode='expected')
    _commit_enemy_damage(state, cp, target, d.final_damage,
                         damage_type='additional')
    cp.total_damage_dealt += d.final_damage
    state.log.append(f'  赛飞儿E4: 老主顾附加{d.final_damage:.0f}')


def _cipher_attack_aftermath(state, u, skill_key):
    """v6.10.3 P1-1: 赛飞儿攻击后接线（on_after_skill 之前调用）:
    - 天赋: 我方其他目标攻击命中【老主顾】→ 赛飞儿对老主顾 FUA 150%ATK（每回合1次, E6×4.5）
    - E1: 施放天赋FUA时 ATK+80% 持续2回合
    - E2: 赛飞儿击中敌方 → 120%基础概率易伤30% 2回合
    - E4: 【老主顾】受我方目标攻击（含赛飞儿）→ 50%ATK量子附加"""
    hit = [t for t in state.extra.get('last_attack_targets', []) or []
           if t is not None and getattr(t, 'HP', 0) > 0]
    if not hit:
        return
    cp = next((x for x in state.units if x.char.id == 'cipher' and x.is_alive), None)
    if cp is None:
        return
    if u.char.id == 'cipher':
        # E2: 自身攻击命中→易伤（同 ID 状态由 Enemy.add_status 刷新持续时间）
        if cp.eidolon_rank >= 2:
            for t in hit:
                if _roll_effect_hit(cp, state, t, '赛飞儿E2易伤', base_chance=1.20):
                    t.add_status(EnemyStatus(id='cipher_e2_vuln', name='易伤',
                                             category='debuff', source='cipher',
                                             remaining_turns=2,
                                             attributes={'vulnerability': 0.30}))
        # E4: 赛飞儿自己攻击老主顾也触发附加
        if cp.eidolon_rank >= 4:
            for t in hit:
                if t.extra.get('cipher_laozhuke'):
                    _cipher_e4_extra(state, cp, t)
        return
    # 队友攻击老主顾: 天赋FUA + E4附加
    lz = next((t for t in hit if t.extra.get('cipher_laozhuke')), None)
    if lz is None:
        return
    if cp.eidolon_rank >= 4:
        _cipher_e4_extra(state, cp, lz)
    if cp.extra.get('cipher_fua_used'):
        return
    cp.extra['cipher_fua_used'] = True
    # E1: 施放FUA时 ATK+80% 2回合（本次FUA即生效）
    if cp.eidolon_rank >= 1 and not cp.extra.get('cipher_e1_atk_buff'):
        cp.base_stats.ATK += cp.base_stats._base_ATK * 0.80
    cp.extra['cipher_e1_atk_buff'] = 2
    stats = _build_effective_stats(cp, state)
    scale = 150.0 * _skill_level_factor(cp, 'talent')
    if cp.eidolon_rank >= 6:
        scale *= 4.5
    d = calculate_damage(stats, _enemy_for_damage(lz), stats.ATK, scale,
                         'direct', '量子', 80, stats.CRIT_RATE >= 0.5,
                         skill_type='skill', attack_type='follow_up',
                         crit_mode='expected')
    _commit_enemy_damage(state, cp, lz, d.final_damage,
                         damage_type='direct', skill_type='talent',
                         attack_type='follow_up',
                         cipher_extra_rate=0.16 if cp.eidolon_rank >= 6 else 0.0)
    cp.total_damage_dealt += d.final_damage
    state.log.append(f'  猫咪怪盗FUA: {d.final_damage:.0f}(老主顾受击)')


def _cipher_trace3_apply_vuln(state):
    """v6.10.3 P1-2: 赛飞儿行迹3 敌方受伤+40%——对称维护（入场/新波）, 幂等标记防重复叠加"""
    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0 and not e.extra.get('cipher_trace3_vuln'):
            e.vulnerability = getattr(e, 'vulnerability', 0.0) + 0.40
            e.extra['cipher_trace3_vuln'] = True


def _cipher_trace3_remove_vuln(state):
    """v6.10.3 P1-2: 赛飞儿死亡/离场时移除行迹3 易伤（对称回减）"""
    for e in state.enemies:
        if e.extra.pop('cipher_trace3_vuln', False):
            e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.40)


def _trace_cipher_trace3(u, state, **kw):
    """赛飞儿行迹3: FUA暴伤+100%(动态面板) + 在场敌受伤+40%(对称维护)
    v6.10.3 P1-2: 暴伤改 CRIT_DMG_BY_ATTACK_TYPE['follow_up'] 动态消费（此前全局+1.0污染普攻/战技/终结技）;
    易伤改对称维护（入场/新波 apply, 死亡 remove, 幂等标记防重复叠加）"""
    if u.char.id != 'cipher':
        return

    _cipher_trace3_apply_vuln(state)
    state.log.append('  行迹·偷天换日: FUA暴伤+100% + 敌受伤+40%')


def _tech_cipher(state, u, is_opener):
    """赛飞儿: 全敌100%ATK量子伤 + 记录+200%（进战秘技, 赛飞儿.txt 标"（进战）";
    v6.7b 裁决: 进战秘技按队伍位序靠前开怪者释放——非开怪者不生效）"""
    if not is_opener:
        return
    from engine.core.combat_engine import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '量子', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage,
                             damage_type='direct', skill_type='technique',
                             cipher_record_multiplier=3.0)
        u.total_damage_dealt += d.final_damage
    state.log.append('[秘技] 穿靴子的猫: 全敌100%ATK量子伤')


CHAR_ID = "cipher"
TECHNIQUE = _tech_cipher


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _cipher_turn_tick(u, state):
    # v6.6c P1: 赛飞儿 ATK+30% 到期回减 + FUA 回合重置
    if u.char.id == 'cipher':
        t = u.extra.get('cipher_atk_buff', 0)
        if t > 0:
            t -= 1
            if t <= 0:
                u.extra['cipher_atk_buff'] = 0
                u.base_stats.ATK -= u.base_stats._base_ATK * 0.30
                state.log.append('  猫咪怪盗: ATK+30%到期回减')
            else:
                u.extra['cipher_atk_buff'] = t
        # v6.10.3 P1-1: E1 FUA ATK+80% 2回合到期回减
        t1 = u.extra.get('cipher_e1_atk_buff', 0)
        if t1 > 0:
            t1 -= 1
            if t1 <= 0:
                u.extra.pop('cipher_e1_atk_buff', None)
                u.base_stats.ATK -= u.base_stats._base_ATK * 0.80
                state.log.append('  赛飞儿E1: FUA ATK+80%到期回减')
            else:
                u.extra['cipher_e1_atk_buff'] = t1
        u.extra.pop('cipher_fua_used', None)  # 老主顾FUA 1次/回合重置


TURN_TICKS = {'pre': _cipher_turn_tick}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _cipher_action_targets_setup(u, state, skill_key):
    """PHASE action_targets_setup: 战技/终结技开场锁定老主顾。"""
    if skill_key in ('skill', 'ultimate'):
        cipher_alive = state.alive_enemies() or state.enemies
        if cipher_alive:
            main_target = cipher_alive[0]
            _cipher_set_laozhuke(state, u, main_target)
            state.extra['cipher_action_main_target'] = main_target
            state.extra['cipher_action_targets'] = cipher_alive[:min(3, len(cipher_alive))]
    return None


PHASE_HOOKS = {'action_targets_setup': _cipher_action_targets_setup}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _cipher_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 战技ATK叠层防重入; 终结技记录真伤结算。"""
    if u.char.id != 'cipher':
        return None
    if skill_key == 'skill':
        # v6.6c P1: 防重入（此前每次战技+30%ATK永久漂移）; 2回合到期回减（回合开始 tick）
        if not u.extra.get('cipher_atk_buff'):
            u.base_stats.ATK += u.base_stats._base_ATK * 0.30
        u.extra['cipher_atk_buff'] = 2
    if skill_key == 'ultimate':
        rec = u.extra.get('cipher_record', 0.0)
        t0 = state.extra.get('cipher_action_main_target')
        record_targets = state.extra.get('cipher_action_targets', [])
        if t0 and record_targets and rec > 0:
            record_damage = 0.0
            dealt, _ = _commit_enemy_damage(
                state, u, t0, rec * 0.25, damage_type='true_damage',
                skill_type='ultimate', record_cipher=False)
            record_damage += dealt
            shared = rec * 0.75 / len(record_targets)
            for target in record_targets:
                dealt, _ = _commit_enemy_damage(
                    state, u, target, shared, damage_type='true_damage',
                    skill_type='ultimate', record_cipher=False)
                record_damage += dealt
            u.total_damage_dealt += record_damage
            state.log.append(
                f'  猫咪怪盗: 记录真伤25%主目标+75%均分 {record_damage:.0f}')
        keep = rec * 0.20 if u.eidolon_rank >= 6 else 0.0
        u.extra['cipher_record'] = keep
        state.log.append('  记录清空(E6返还%.0f)' % keep)
    return None


SETTLE_HANDLERS = {'settle_self': _cipher_settle_self}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_guiji(state, summoner, ms_unit, cipher):
    """献予「诡计」之诗(整场, 赛飞儿): 伤害+36%; 老主顾DEF-20%/其他-12%"""
    cipher.extra['poem_guiji'] = True
    cipher.base_stats.DMG_BONUS_ALL += 0.36
    state.log.append('  献予「诡计」之诗: 赛飞儿伤害+36%+敌方DEF降低')


POEM = ("诡计", _poem_guiji, True)
