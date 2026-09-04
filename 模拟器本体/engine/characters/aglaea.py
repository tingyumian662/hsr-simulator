"""阿格莱雅（M4 批2a；忆灵/诗层/AI 委托 remembrance, M6）"""

import copy
import random
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_energy


def _aglaea_trace1_start_energy(u, state, **kw):
    """行迹1·飞驰之阳: 战斗开始时若能量不足50%恢复至50%"""
    if u.char.id != 'aglaea':
        return
    max_e = u.char.max_energy or 0
    if max_e > 0 and u.current_energy < max_e * 0.50:
        u.current_energy = max_e * 0.50
        state.log.append(f'  飞驰之阳: 能量恢复至50% ({u.current_energy:.0f}/{max_e})')


def _eid_aglaea_e1(u, state, **kw):
    """阿格莱雅E1: 织线目标受伤+15%(标记)"""
    u.extra['aglaea_e1'] = True


def _eid_aglaea_e2(u, state, **kw):
    """阿格莱雅E2: 行动时无视防御14%×3层(标记)"""
    u.extra['aglaea_e2'] = True


def _eid_aglaea_e4(u, state, **kw):
    """阿格莱雅E4: 速度层上限+1 + 阿格莱雅攻击也能叠层(标记)"""
    u.extra['aglaea_e4'] = True


def _eid_aglaea_e6(u, state, **kw):
    """阿格莱雅E6: 至高之姿时雷抗穿透+20% + 速度阈值连携增伤(标记)"""
    u.extra['aglaea_e6'] = True


def _tech_aglaea(state, u, is_opener):
    """阿格莱雅: 召唤衣匠 + 全敌100%ATK雷伤 + 削韧20 + 能量30 + 随机敌织线
    （阿格莱雅.txt 秘技·披星百裂; 召唤分支自带立即行动≈开怪攻击, 接受该副作用）"""
    from engine.runtime import _tech_enemies
    from engine.core.combat_engine import (calculate_damage, _apply_toughness_damage, _gain_energy, _commit_enemy_damage)
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    state.extra['_rem_sys'] = rem
    if u.char.memsprite and not (u.memsprite_unit and u.memsprite_unit.is_alive):
        rem.summon_memsprite(state, u, u.char.memsprite)
    stats = u.base_stats
    alive = _tech_enemies(state)
    for e in alive:
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '雷', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    if alive:
        import random
        tgt = random.choice(alive)
        for e in state.enemies:
            e.extra['gossamer'] = False
        tgt.extra['gossamer'] = True
        state.log.append(f'  【间隙织线】: {tgt.name or tgt.id}')
    _gain_energy(u, 30.0, state=state)
    state.log.append('[秘技] 披星百裂: 召唤衣匠 + 全敌100%ATK雷伤 + 能量30 + 随机织线')


CHAR_ID = "aglaea"
TECHNIQUE = _tech_aglaea


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _aglaea_e2_clear_tick(u, state):
    # 阿格莱雅E2: 其他单位行动→清除无视防御层
    if u.char.id != 'aglaea':
        aglaea = next((x for x in state.units if x.char.id == 'aglaea'), None)
        if aglaea and aglaea.eidolon_rank >= 2 and aglaea.extra.get('aglaea_e2_stack', 0) > 0:
            stack = aglaea.extra['aglaea_e2_stack']
            aglaea.base_stats.DEF_PEN = max(0, aglaea.base_stats.DEF_PEN - 0.14 * stack)
            if aglaea.memsprite_unit:
                aglaea.memsprite_unit.base_stats.DEF_PEN = max(
                    0, aglaea.memsprite_unit.base_stats.DEF_PEN - 0.14 * stack)
            aglaea.extra['aglaea_e2_stack'] = 0
            state.log.append(f'  E2: {u.char.name}行动→无视防御层清除')


TURN_TICKS = {'late': _aglaea_e2_clear_tick}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _aglaea_sovereign_takeover(u, state, skill, skill_key, eff):
    """EFFECT_TAKEOVERS['aglaea_sovereign']: 终结技→至高之姿。"""
    if u.char.id != 'aglaea':
        return None
    u.is_sovereign = True
    # v5.7: 终结技→自身立即行动（阿格莱雅.txt: 进入【至高之姿】状态并使自身立即行动）
    navs = state.extra.get('navs', {})
    uid = state.units.index(u)
    if uid in navs:
        navs[uid] = state.current_av
        state.log.append('  终结技: 自身立即行动')
    # 献予「浪漫」之诗: 持【浪漫】进入至高→双方伤害+72%无视36%防御
    if u.extra.get('poem_langman') and not u.extra.get('romantic_applied'):
        _romantic_apply(u)
        state.log.append('  献予「浪漫」之诗: 至高之姿增强生效(72%/36%)')
    # 获得衣匠忆灵天赋的速度提高层数（每层15%速度）
    u.extra['sovereign_spd_stack'] = 0
    # 行动序列倒计时（100速度，简化：衣匠回合时检查）
    u.extra['countdown_turns'] = 3
    # 行迹3·短视之惩: 至高之姿时攻击力 += 阿格莱雅速度×720% + 衣匠速度×360%
    spd_bonus_atk = u.base_stats.SPD * 7.20
    ms = u.memsprite_unit
    if ms:
        spd_bonus_atk += ms.base_stats.SPD * 3.60
    u.base_stats.ATK += spd_bonus_atk
    if ms:
        ms.base_stats.ATK += spd_bonus_atk
    u.extra['sovereign_atk_bonus'] = spd_bonus_atk  # v6.2.1: 退出时对称回减
    state.log.append(f'  短视之惩: 攻击力+{spd_bonus_atk:.0f}(速度×720%+衣匠速度×360%)')
    # 至高之姿: 获得衣匠忆灵天赋速度层数(每层自身速度+15%, v5.7 数据驱动)
    if ms:
        stack = ms.extra.get('spd_stack', 0)
        if stack > 0:
            spd_gain = u.base_stats._base_SPD * (u.char.sovereign_spd_pct / 100) * stack
            u.base_stats.SPD += spd_gain
            u.extra['sovereign_spd_bonus'] = spd_gain
            state.log.append(f'  至高之姿: 衣匠{stack}层速度→自身速度+{spd_gain:.0f}')
    # E6: 至高之姿时自身与衣匠雷抗穿透+20%
    if u.eidolon_rank >= 6:
        u.base_stats.RES_PEN['雷'] += 0.20
        if ms:
            ms.base_stats.RES_PEN['雷'] += 0.20
        state.log.append('  E6: 雷属性抗性穿透+20%')
    state.log.append('  进入【至高之姿】: 普攻强化为孤锋千吻, 无法施放战技')
    return True


EFFECT_TAKEOVERS = {'aglaea_sovereign': _aglaea_sovereign_takeover}


PHASE_HOOKS = {}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _aglaea_post_attack_mark(u, state, skill, total_dmg):
    """PHASE post_attack_mark: 天赋·金玫之指——攻击使最新目标陷入【间隙织线】。"""
    from engine.runtime import _select_targets
    # 阿格莱雅天赋·金玫之指: 攻击使最新目标陷入【间隙织线】
    if total_dmg > 0 and skill.multipliers:
        for e in state.enemies:
            e.extra['gossamer'] = False
            e.extra.pop('gossamer_dmg_bonus', None)  # v5.7: 换目标时同步清除易伤
        targets = _select_targets(state.alive_enemies() or state.enemies,
                                  skill.target if skill.target != 'blast' else 'single_enemy')
        if targets:
            targets[0].extra['gossamer'] = True
            # E1: 织线目标受到的伤害提高15%
            if u.eidolon_rank >= 1:
                targets[0].extra['gossamer_dmg_bonus'] = 0.15
            state.log.append(f'  【间隙织线】: {targets[0].name or targets[0].id}')
            # E4: 阿格莱雅攻击后也能使衣匠获得速度层
            if u.eidolon_rank >= 4 and u.memsprite_unit and u.memsprite_unit.is_alive:
                ms = u.memsprite_unit
                stack = ms.extra.get('spd_stack', 0)
                if stack < 7:
                    ms.extra['spd_stack'] = stack + 1
                    state.log.append(f'  E4: 阿格莱雅攻击→衣匠速度层+1 ({stack+1}/7)')
            # 献予「浪漫」之诗: 阿格莱雅攻击后消耗【浪漫】回70能量
            if u.extra.pop('poem_langman', None):
                _gain_energy(u, 70.0, state=state)
                state.log.append(f'  献予「浪漫」之诗: 攻击回70能量 ({u.current_energy:.0f})')
    return None


PHASE_HOOKS['post_attack_mark'] = _aglaea_post_attack_mark


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _romantic_apply(aglaea):
    """浪漫之诗增强: 阿格莱雅+衣匠伤害+72%无视36%防御（至退出至高之姿）"""
    if aglaea.extra.get('romantic_applied'):
        return
    aglaea.base_stats.DMG_BONUS_ALL += 0.72
    aglaea.base_stats.DEF_PEN += 0.36
    ms = aglaea.memsprite_unit
    if ms and ms.is_alive:
        ms.base_stats.DMG_BONUS_ALL += 0.72
        ms.base_stats.DEF_PEN += 0.36
    aglaea.extra['romantic_applied'] = True


def _poem_langman(state, summoner, ms_unit, aglaea):
    """献予「浪漫」之诗(单次): 衣匠速度拉满+【浪漫】token; 攻击后回70能量; 双方72%/36%至退出至高之姿"""
    aglaea.extra['poem_langman'] = 1
    ms = aglaea.memsprite_unit
    if ms and ms.is_alive:
        ms.extra['spd_stack'] = 7 if aglaea.eidolon_rank >= 4 else 6
        state.log.append('  献予「浪漫」之诗: 衣匠速度叠满')
    else:
        aglaea.extra['poem_langman_spd_pending'] = True  # 衣匠不在场→召唤时补
    if aglaea.is_sovereign:
        _romantic_apply(aglaea)
    state.log.append('  献予「浪漫」之诗: 阿格莱雅获得【浪漫】')


POEM = ("浪漫", _poem_langman, False)


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def _aglaea_sync_memsprite(u, state):
    """衣匠速度=阿格莱雅35%(动态同步)"""
    from engine.core.combat_engine import _effective_spd
    ms = u.memsprite_unit
    if not ms or u.char.id != 'aglaea':
        return
    base_spd = _effective_spd(u, state) * 0.35
    # 加上忆灵天赋速度叠层(每层55点)
    stack = ms.extra.get('spd_stack', 0)
    ms.base_stats.SPD = base_spd + stack * 55
    ms.runtime_spd = ms.base_stats.SPD  # v5.2: 写运行时字段, 不污染 MemSprite 配置


def aglaea_ai(u, state, **kw):
    """阿格莱雅AI: 能量满→终结技(至高之姿); 至高之姿→强化普攻(不能战技); SP>0→战技(召唤/回血衣匠); 否则普攻"""
    from engine.core.combat_engine import _use_skill
    # 同步衣匠速度(阿格莱雅速度变化时衣匠跟随)
    _aglaea_sync_memsprite(u, state)
    # E2: 行动时无视防御14%×3层
    if u.eidolon_rank >= 2:
        stack = u.extra.get('aglaea_e2_stack', 0)
        if stack < 3:
            u.extra['aglaea_e2_stack'] = stack + 1
            u.base_stats.DEF_PEN += 0.14
            if u.memsprite_unit:
                u.memsprite_unit.base_stats.DEF_PEN += 0.14
            state.log.append(f'  E2: 无视防御+14% ({stack+1}/3)')
    # 至高之姿: 普攻强化为孤锋千吻，无法施放战技
    if u.is_sovereign:
        _use_skill(u, state, "basic_attack_enhanced")
        return
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        return
    if state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


AI = aglaea_ai


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _ag_ms_reheal_skip(u, state):
    """PHASE ms_reheal_skip: 已在场回血走 JSON heal 效果（跳过通用 50%）。"""
    return True


def _ag_ms_build(u, state, ms_data, hp_override):
    """PHASE ms_build: 衣匠构建（HP×66%+720/SPD35%动态/织运之竭/飞驰之夏/自身立即行动）。"""
    from engine.core.combat_engine import _effective_spd
    from engine.runtime import _set_av, _stamp_av_key
    ms_stats = copy.deepcopy(u.base_stats)
    ms_stats.HP = u.base_stats.HP * 0.66 + 720
    ms_stats.SPD = _effective_spd(u, state) * 0.35
    ms_stats.ATK = u.base_stats.ATK
    from engine.systems.remembrance import MemSpriteUnit
    ms_unit = MemSpriteUnit(
        data=ms_data, summoner_id=u.char.id,
        max_hp=ms_stats.HP, current_hp=ms_stats.HP,
        base_stats=ms_stats,
    )
    ms_unit.current_energy = 0
    # 织运之竭: 上次消失保留的速度层(最多1层)
    retained = u.extra.get('aglaea_retained_spd', 0)
    ms_unit.extra['spd_stack'] = retained
    u.extra['aglaea_retained_spd'] = 0
    state.memsprites.append(ms_unit)
    u.memsprite_unit = ms_unit
    state.log.append(f'  召唤衣匠 HP={ms_stats.HP:.0f} SPD={ms_stats.SPD:.0f} '
                     f'(阿格莱雅HP×66%+720, SPD×35%)'
                     + (f' 织运之竭恢复{retained}层速度' if retained else ''))
    # 忆灵天赋·飞驰之夏: 衣匠被召唤时自身行动提前100%
    ms_unit.extra['next_av'] = state.current_av
    _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
    state.log.append('  飞驰之夏: 衣匠行动提前100%')
    # v5.7: 召唤衣匠（战技/终结技）→阿格莱雅自身立即行动（阿格莱雅.txt 战技/终结技）
    navs = state.extra.get('navs', {})
    uid = state.units.index(u)
    if uid in navs:
        _set_av(state, navs, uid, state.current_av)  # v6.2.1b P3-1: 统一入口补戳
        state.log.append('  召唤衣匠: 自身立即行动')
    state.hooks.trigger_all("on_memsprite_summon", u=u, state=state,
                             summoner=u, ms_unit=ms_unit)
    return ms_unit


def _ag_ms_despawn(u, state, ms_unit, ms_name):
    """PHASE ms_despawn_settle: 退出至高之姿对称回收+枯草之盈+织运之竭保留。"""
    u.is_sovereign = False
    u.extra['countdown_turns'] = 0
    # v6.2.1: 退出至高之姿对称回减（Harness P2-4, 此前永久留存）
    atk_bonus = u.extra.pop('sovereign_atk_bonus', 0.0)
    if atk_bonus > 0:
        u.base_stats.ATK -= atk_bonus
        ms_unit.base_stats.ATK -= atk_bonus
        state.log.append(f'  短视之惩回收: 攻击力-{atk_bonus:.0f}')
    spd_bonus = u.extra.pop('sovereign_spd_bonus', 0.0)
    if spd_bonus > 0:
        u.base_stats.SPD -= spd_bonus
        state.log.append(f'  至高之姿回收: 速度-{spd_bonus:.0f}')
    if u.eidolon_rank >= 6:
        u.base_stats.RES_PEN['雷'] -= 0.20
        ms_unit.base_stats.RES_PEN['雷'] -= 0.20
        state.log.append('  E6回收: 雷抗穿透-20%')
    # 献予「浪漫」之诗: 退出至高→移除双方72%/36%增强(只减自己加的, 不动E2层)
    if u.extra.get('romantic_applied'):
        u.base_stats.DMG_BONUS_ALL -= 0.72
        u.base_stats.DEF_PEN -= 0.36
        ms_unit.base_stats.DMG_BONUS_ALL -= 0.72
        ms_unit.base_stats.DEF_PEN -= 0.36
        u.extra['romantic_applied'] = False
        state.log.append('  献予「浪漫」之诗: 退出至高→增强移除')
    # 枯草之盈: 衣匠消失时阿格莱雅恢复20点能量（v6.2.1: 统一回能入口）
    _gain_energy(u, 20.0, state=state)
    state.log.append(f'  衣匠消失→阿格莱雅退出【至高之姿】, +20能量')
    # 织运之竭: 速度层保留1层，下次召唤恢复
    stack = ms_unit.extra.get('spd_stack', 0)
    if stack > 0:
        u.extra['aglaea_retained_spd'] = 1
        state.log.append('  织运之竭: 速度层保留1层')
    return True


def _ag_ms_default_target(u, state, alive):
    """PHASE ms_default_target: 单体目标优先【间隙织线】敌人。"""
    g = next((e for e in alive if e.extra.get('gossamer')), None)
    if g:
        return [g]
    return None


def _ag_ms_target_hit_bonus(u, state, ms_unit, ms_stats, t, skill_key):
    """PHASE ms_target_hit_bonus: 攻击织线目标→附加雷伤+回能+速度叠层+浪漫诗。"""
    from engine.runtime import _enemy_for_damage
    if not t.extra.get('gossamer'):
        return None
    # E6: 速度>160/240/320 → 连携攻击伤害+10%/30%/60%
    e6_mult = 1.0
    if u.eidolon_rank >= 6:
        spd = u.base_stats.SPD
        if spd > 320:
            e6_mult = 1.60
        elif spd > 240:
            e6_mult = 1.30
        elif spd > 160:
            e6_mult = 1.10
    add_d = calculate_damage(
        ms_stats, _enemy_for_damage(t), ms_stats.ATK, 30.0 * e6_mult,
        "direct", "雷", 80, ms_stats.CRIT_RATE >= 0.5,
        skill_type="basic" if skill_key == "memsprite_basic" else "skill",
    crit_mode="expected")
    # E1: 织线目标受伤+15% 已单点于 _enemy_for_damage（v5.7）; 此处保留回能
    if u.eidolon_rank >= 1:
        _gain_energy(u, 20.0, state=state)
        state.log.append(f'  E1: 攻击织线目标+20能量 ({u.current_energy:.0f})')
    total = add_d.final_damage
    _commit_enemy_damage(state, u, t, add_d.final_damage)
    state.log.append(f'  间隙织线附加: {add_d.final_damage:.0f}(30%ATK×{e6_mult:.2f})')
    # 忆灵天赋·泪水锻造的匠躯: 速度叠层(E4: 上限+1→7)
    stack = ms_unit.extra.get('spd_stack', 0)
    max_stack = 7 if u.eidolon_rank >= 4 else 6
    if stack < max_stack:
        ms_unit.extra['spd_stack'] = stack + 1
        state.log.append(f'  衣匠速度叠层+1 ({stack+1}/{max_stack})')
    # 献予「浪漫」之诗: 衣匠攻击后消耗【浪漫】回70能量（pop 防重复）
    if u.extra.pop('poem_langman', None):
        _gain_energy(u, 70.0, state=state)
        state.log.append(f'  献予「浪漫」之诗: 衣匠攻击回70能量 ({u.current_energy:.0f})')
    return total


def _ag_ms_action(u, state, ms_unit):
    """PHASE ms_action: E2 叠无视防御层 + 至高倒计时(归零→自毁 despawn→True)。"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    # E2: 衣匠行动也叠无视防御层
    if u.eidolon_rank >= 2:
        stack = u.extra.get('aglaea_e2_stack', 0)
        if stack < 3:
            u.extra['aglaea_e2_stack'] = stack + 1
            u.base_stats.DEF_PEN += 0.14
            ms_unit.base_stats.DEF_PEN += 0.14
            state.log.append(f'  E2(衣匠): 无视防御+14% ({stack+1}/3)')
    # 衣匠倒计时: 至高之姿期间回合开始→倒计时减1，归零→衣匠自毁
    if u.is_sovereign:
        countdown = u.extra.get('countdown_turns', 0)
        if countdown > 0:
            countdown -= 1
            u.extra['countdown_turns'] = countdown
            state.log.append(f'  至高之姿倒计时: {countdown}')
            if countdown <= 0:
                state.log.append('  倒计时归零→衣匠自毁')
                rem.despawn_memsprite(state, u, ms_unit, reason="countdown")
                return True
    return None


PHASE_HOOKS['ms_reheal_skip'] = _ag_ms_reheal_skip
PHASE_HOOKS['ms_build'] = _ag_ms_build
PHASE_HOOKS['ms_despawn_settle'] = _ag_ms_despawn
PHASE_HOOKS['ms_default_target'] = _ag_ms_default_target
PHASE_HOOKS['ms_target_hit_bonus'] = _ag_ms_target_hit_bonus
PHASE_HOOKS['ms_action'] = _ag_ms_action
