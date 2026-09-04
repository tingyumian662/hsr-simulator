"""mydei（M4 收官批迁入）"""

import copy
import random
from engine.runtime import _set_av, _tech_enemies
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _apply_enemy_taunt
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _use_skill


def _mydei_blood_debt_tick(u, state, navs=None, uidx=None):
    """万敌天赋·以血还血: 血仇状态机

    - 充能≥100且非血仇 → 进入血仇(消耗100充能+回血+行动提前100%+生命上限50%+防御0)
    - 血仇中回合开始 → 自动施放弑王成王
    - 充能≥150且血仇 → 额外回合+自动弑神登神(消耗150充能)
    """
    charge = u.extra.get('mydei_charge', 0)
    is_debt = u.extra.get('is_blood_debt', False)
    u.extra['charge_locked'] = False  # 回合开始解锁充能积攒

    if not is_debt and charge >= 100:
        # 进入血仇: 消耗100充能 + 回血 + 行动提前100% + 生命上限+50% + 防御0
        # v6.2.1: 快照面板（Harness P1-3, 退出时对称还原, 此前退出不回减→永久漂移）
        u.extra['blood_debt_snapshot'] = {
            'max_hp': u.max_hp,
            'base_HP': u.base_stats.HP,
            'base_DEF': u.base_stats.DEF,
            'e2_defpen': 0.15 if u.eidolon_rank >= 2 else 0.0,
            'e4_critdmg': 0.30 if u.eidolon_rank >= 4 else 0.0,
        }
        u.extra['mydei_charge'] = charge - 100
        u.extra['is_blood_debt'] = True
        heal = u.max_hp * 0.20
        u.current_hp = min(u.max_hp, u.current_hp + heal)
        u.max_hp = u.max_hp * 1.50  # 生命上限+50%
        u.base_stats.HP = u.max_hp
        u.base_stats.DEF = 0
        # E2: 血仇期间无视防御15%
        if u.eidolon_rank >= 2:
            u.base_stats.DEF_PEN += 0.15
            state.log.append('  E2: 无视防御+15%')
        # E4: 血仇期间暴伤+30%
        if u.eidolon_rank >= 4:
            u.base_stats.CRIT_DMG += 0.30
            state.log.append('  E4: 暴击伤害+30%')
        if navs is not None and uidx is not None:
            _set_av(state, navs, uidx, state.current_av)  # 行动提前100%（v6.2.1b: 走统一入口补戳）
        state.log.append(f'  进入【血仇】: 生命上限+50%(={u.max_hp:.0f}), 防御=0, 行动提前100%, 回血{heal:.0f}')
        # 血仇回合开始自动弑王成王
        _use_skill(u, state, 'skill_enhanced')
        return

    if is_debt:
        # 血仇中: 回合开始自动弑王成王
        _use_skill(u, state, 'skill_enhanced')
        # 充能≥阈值(E6:100, 默认150) → 弑神登神入 X 轴队列（额外回合）
        need = u.extra.get('shenshen_cost', 150)
        if u.extra.get('mydei_charge', 0) >= need and not any(
                x is u and k == 'extra' for x, k in state.extra.get('extra_turns', [])):
            state.log.append(f'  充能≥{need}: 弑神登神入额外回合队列')
            state.extra.setdefault('extra_turns', []).append((u, 'extra'))


def _mydei_fatal_recovery(u, state):
    """万敌血仇致命攻击: 不会死亡。水与泥土(3次)不退出血仇；否则清空充能退出+回50%生命"""
    retain = u.extra.get('debt_retain_charges', 0)
    if retain > 0:
        # 水与泥土: 血仇状态受致命攻击不退出
        u.extra['debt_retain_charges'] = retain - 1
        u.current_hp = max(1, u.max_hp * 0.01)
        state.log.append(f'  水与泥土: 致命攻击不退出血仇({retain-1}/3剩余)')
        return
    # 退出: 清空充能 + 对称还原面板 + 回50%生命上限
    # v6.2.1: 先还原面板再回血（实机回血基于退仇后的生命上限）
    snap = u.extra.pop('blood_debt_snapshot', None)
    if snap:
        u.max_hp = snap['max_hp']
        u.base_stats.HP = snap['base_HP']
        u.base_stats.DEF = snap['base_DEF']
        u.base_stats.DEF_PEN -= snap['e2_defpen']
        u.base_stats.CRIT_DMG -= snap['e4_critdmg']
        state.log.append('  血仇面板还原(HP/DEF/无视防御/暴伤)')
    u.extra['is_blood_debt'] = False
    u.extra['mydei_charge'] = 0
    u.current_hp = u.max_hp * 0.50
    state.log.append(f'  致命攻击: 退出【血仇】, 回50%生命({u.current_hp:.0f})')


def mydei_ai(unit, state, *, elation=None, max_av=None, navs=None, uidx=None, **__):
    """万敌AI: 血仇检查→能量满→终结技→战技/普攻"""
    _mydei_blood_debt_tick(unit, state, navs, uidx)
    if unit.current_energy >= unit.char.max_energy:
        _use_skill(unit, state, 'ultimate')
    elif state.skill_points > 0:
        _use_skill(unit, state, 'skill')
    else:
        _use_skill(unit, state, 'basic_attack')


def _mydei_trace1_blood_armor(u, state, **kw):
    """行迹1·血祥罩衫: 生命上限>4000时每超100点→暴击+1.2%(最多4000点)"""
    if u.char.id != 'mydei':
        return
    hp = u.max_hp
    if hp > 4000:
        excess = min(hp - 4000, 4000)
        cr_bonus = (excess // 100) * 0.012
        u.base_stats.CRIT_RATE += cr_bonus
        state.log.append(f'  血祥罩衫: 生命上限{hp:.0f}→暴击率+{cr_bonus*100:.1f}%')


def _mydei_trace2_debt_retain(u, state, **kw):
    """行迹2·水与泥土: 血仇状态受致命攻击不退出(3次)"""
    if u.char.id != 'mydei':
        return
    u.extra['debt_retain_charges'] = 3


def _mydei_trace3_control_immune(u, state, **kw):
    """行迹3·三十僭主: 血仇状态免疫控制类负面状态
    （v5.0 P4 激活: debt_control_immune 由 _apply_player_status 消费）"""
    if u.char.id != 'mydei':
        return
    u.extra['debt_control_immune'] = True


def _eid_mydei_e1(u, state, **kw):
    """万敌E1: 弑神登神主目标+30%且变全体(标记)"""
    u.extra['mydei_e1'] = True


def _eid_mydei_e2(u, state, **kw):
    """万敌E2: 血仇无视防御15% + 治疗转充能(标记)"""
    u.extra['mydei_e2'] = True


def _eid_mydei_e4(u, state, **kw):
    """万敌E4: 血仇暴伤+30% + 受击回10%生命(标记)"""
    u.extra['mydei_e4'] = True


def _eid_mydei_e6(u, state, **kw):
    """万敌E6: 开局立刻进入血仇 + 弑神登神充能需求降至100"""
    if u.char.id != 'mydei':
        return
    u.extra['is_blood_debt'] = True
    u.extra['shenshen_cost'] = 100  # E6: 充能需求降低
    heal = u.max_hp * 0.20
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    u.max_hp = u.max_hp * 1.50
    u.base_stats.HP = u.max_hp
    u.base_stats.DEF = 0
    u.extra['mydei_charge'] = 0
    state.log.append(f'  E6: 开局进入【血仇】(生命上限+50%, 弑神登神需求100)')


def _tech_mydei(state, u, is_opener):
    """万敌: 全敌80%生命上限虚数伤 + 嘲讽1回合 + 充能+50（万敌.txt 秘技·折戟臣服的监牢）"""
    from engine.core.combat_engine import calculate_damage, _apply_enemy_taunt, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.HP, 80.0, 'direct', '虚数', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    _apply_enemy_taunt(state, u, state.enemies, turns=1)
    u.extra['mydei_charge'] = min(200, u.extra.get('mydei_charge', 0) + 50)
    state.log.append('[秘技] 折戟臣服的监牢: 全敌80%HP虚数伤 + 嘲讽1回合 + 充能+50')


CHAR_ID = "mydei"
AI = mydei_ai
TECHNIQUE = _tech_mydei


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _mydei_fatal_tick(u, state):
    # 万敌致命攻击检查
    if u.char.id == 'mydei' and u.current_hp <= 0 and u.extra.get('is_blood_debt'):
        _mydei_fatal_recovery(u, state)


def _mydei_e2_reset_tick(u, state):
    # v5.7: 万敌E2: 任意单位行动后重置可累计的治疗转充能（此前40点上限变整场累计）
    mydei = next((x for x in state.units if x.char.id == 'mydei' and x.is_alive), None)
    if mydei:
        mydei.extra['e2_heal_converted'] = 0.0


TURN_TICKS = {'late': [_mydei_fatal_tick, _mydei_e2_reset_tick]}


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _mydei_ult_cast_resource(u, state, skill):
    """PHASE ult_cast_resource: 终结技积攒20点充能+嘲讽+记录优先目标。"""
    from engine.runtime import _select_targets
    # 万敌终结技: 积攒20点天赋充能
    if not u.extra.get('charge_locked'):
        u.extra['mydei_charge'] = min(200, u.extra.get('mydei_charge', 0) + 20)
        state.log.append(f'  诛天焚骨的王座: 充能+20 → {u.extra["mydei_charge"]:.0f}/200')
        # v5.7: 目标与相邻目标嘲讽2回合; 记录下次弑神登神优先目标(仅最新目标生效)
        _alive = state.alive_enemies() or state.enemies
        tgt = _select_targets(_alive, 'blast')
        if tgt:
            _apply_enemy_taunt(state, u, tgt, turns=2)
            u.extra['mydei_priority_target_id'] = tgt[0].id
    return None


def _mydei_special_resource_cost(u, state, skill, skill_key):
    """PHASE special_resource_cost: 充能门槛 + 弑神登神 E1 变形。

    返回 (abort, new_skill|None)：abort=True 资源不足中止；new_skill 替换技能倍率表。
    """
    charge_cost = skill.cost.get("_mydei_charge", 0)
    if charge_cost <= 0:
        return None
    # 献予「纷争」之诗: 免费施放(不耗充能) — 跳过扣减但保留E1变形
    if not u.extra.get('poem_fenzheng_free'):
        cur = u.extra.get('mydei_charge', 0)
        if cur < charge_cost:
            state.log.append(f'  [WARN] 充能不足({cur:.0f}<{charge_cost})')
            return (True, None)
        u.extra['mydei_charge'] = cur - charge_cost
        u.extra['charge_locked'] = True  # 弑神登神期间无法积攒充能
        state.log.append(f'  充能-{charge_cost} → {cur - charge_cost:.0f}/200 (弑神登神)')
    # E1: 弑神登神主目标倍率+30%，且变成对敌方全体（按主目标倍率）
    # v5.7: deepcopy 防跨战斗污染（原实现直接改 char.skills 会跨模拟叠乘）
    new_skill = None
    if u.eidolon_rank >= 1:
        new_skill = copy.deepcopy(skill)
        main = next((m for m in new_skill.multipliers
                     if m.target in ('single_enemy', None, '')), new_skill.multipliers[0])
        main.scale = main.scale * 1.30
        main.target = 'all_enemies'
        new_skill.multipliers = [main]
        new_skill.target = 'all_enemies'
        state.log.append('  E1: 弑神登神倍率+30%且变全体')
    return (False, new_skill)


def _mydei_self_hp_loss(u, state, lost):
    """PHASE self_hp_loss: 以血还血——每损失1%生命=1充能(最多200)。"""
    # 万敌天赋·以血还血: 每损失1%生命=1充能(最多200)
    if not u.extra.get('charge_locked'):
        pct_lost = lost / max(u.max_hp, 1) * 100.0
        charge = min(200, u.extra.get('mydei_charge', 0) + pct_lost)
        u.extra['mydei_charge'] = charge
        state.log.append(f'  以血还血: 充能+{pct_lost:.0f} → {charge:.0f}/200')
    return None


PHASE_HOOKS = {'ult_cast_resource': _mydei_ult_cast_resource,
               'special_resource_cost': _mydei_special_resource_cost,
               'self_hp_loss': _mydei_self_hp_loss}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _mydei_target_order(u, state, alive, skill_key):
    """PHASE target_order: 弑神登神优先目标置首（→alive|None）。"""
    # v5.7: 万敌"下一次弑神登神优先攻击指定敌方单体"——优先目标存活时置首
    if skill_key == 'skill_shenshen':
        pid = u.extra.get('mydei_priority_target_id')
        if pid and any(e.id == pid for e in alive):
            alive.sort(key=lambda e: 0 if e.id == pid else 1)
            return alive
    return None


PHASE_HOOKS['target_order'] = _mydei_target_order


# ---- M5a 批5b: 治疗/收尾相位处理器（原引擎 内联, verbatim 迁入）----


def _mydei_receive_heal_mod(u, state, amt):
    """PHASE receive_heal_mod: 受疗者=万敌——血祥罩衫受疗提升 + E2 治疗转充能。

    按受疗者派发（_char_phase(state, t, ...)）; 返回修饰后治疗量。
    """
    # 万敌行迹1·血祥罩衫: 受疗提高0.75%/每超4000点100生命（最多计入4000）— v5.7 门槛
    excess_hundreds = min(max(0, u.max_hp - 4000), 4000) // 100
    amt *= 1.0 + 0.0075 * excess_hundreds
    # 万敌E2: 血仇期间接受治疗→40%治疗转充能(累计40点)
    if u.eidolon_rank >= 2 and u.extra.get('is_blood_debt'):
        if not u.extra.get('charge_locked'):
            converted = min(40 - u.extra.get('e2_heal_converted', 0),
                            amt * 0.40)
            if converted > 0:
                u.extra['e2_heal_converted'] = u.extra.get('e2_heal_converted', 0) + converted
                u.extra['mydei_charge'] = min(200, u.extra.get('mydei_charge', 0) + converted)
                state.log.append(f'  E2: 治疗转充能+{converted:.0f} → {u.extra["mydei_charge"]:.0f}/200')
    return amt


PHASE_HOOKS['receive_heal_mod'] = _mydei_receive_heal_mod


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_fenzheng(state, summoner, ms_unit, mydei):
    """献予「纷争」之诗(单次): 解控(简化:清负属性buff); 血仇中→免费弑神登神+暴伤200%; 否则拉条100%"""
    from engine.core.combat_engine import _use_skill
    # 解控（引擎无我方控制系统, 简化清理负属性buff）
    cleared = 0
    for b in list(mydei.buffs):
        if any(v < 0 for v in b.attributes.values()):
            mydei.buffs.remove(b)
            cleared += 1
            break
    if mydei.extra.get('is_blood_debt'):
        old_cd = mydei.base_stats.CRIT_DMG
        mydei.base_stats.CRIT_DMG += 2.0
        mydei.extra['poem_fenzheng_free'] = True  # 免费施放(不耗充能)
        try:
            _use_skill(mydei, state, 'skill_shenshen')
        finally:
            mydei.base_stats.CRIT_DMG = old_cd
            mydei.extra.pop('poem_fenzheng_free', None)
        state.log.append('  献予「纷争」之诗: 血仇→免费弑神登神(暴伤+200%)')
    else:
        from engine.characters.robin_summeretto import _guest_advance_blocked
        navs = state.extra.get('navs', {})
        uidx = state.units.index(mydei)
        if uidx in navs and not _guest_advance_blocked(state, summoner, mydei):
            navs[uidx] = state.current_av
        state.log.append('  献予「纷争」之诗: 万敌行动提前100%')
    if cleared:
        state.log.append('  献予「纷争」之诗: 解除控制(简化)')


POEM = ("纷争", _poem_fenzheng, False)
