"""开拓者·记忆（M4 批2a；迷迷声援真伤, AI 委托 remembrance）"""

import copy
import random
from engine.runtime import AV_PER_TURN, _hook_owner, _set_av
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _effective_spd
from engine.core.combat_engine import _gain_energy


def _apply_tbr_support(state, u, t, dmg) -> float:
    """v5.7: 迷迷的声援单点——持有者每造成1次伤害→额外28%真伤（逐段触发, 实机语义;
    此前按"本次行动伤害总额"一次性结算, 多段技能少触发）。
    行迹1·磁石与长链: 能量上限>100每超10点→倍率+2%（最高+20%）; E4: 零能量目标+6%。
    对忆灵生效（E1）: 忆灵循环传 _tbr_support 持有者的 buff 检查。"""
    support = next((b for b in u.buffs
                    if getattr(b, 'attributes', {}).get('_tbr_support')), None)
    if support is None and getattr(u, 'memsprite_unit', None):
        support = next((b for b in u.memsprite_unit.buffs
                        if getattr(b, 'attributes', {}).get('_tbr_support')), None)
    if support is None or dmg <= 0:
        return 0.0
    magnet = 0.0
    if (u.char.max_energy or 0) > 100:
        magnet = min(0.02 * ((u.char.max_energy - 100) // 10), 0.20)
    tbr4 = next((x for x in state.units
                 if x.char.id == 'trailblazer_remembrance' and x.eidolon_rank >= 4), None)
    if tbr4 and (u.char.max_energy or 0) == 0:
        magnet += 0.06
    support_dmg = dmg * 0.28 * (1.0 + magnet)
    _commit_enemy_damage(state, u, t, support_dmg, damage_type='true_damage',
                         record_cipher=False)
    state.log.append(f'  迷迷的声援: 真伤+{support_dmg:.0f}(28%×{1+magnet:.2f})')
    return support_dmg


def _tbr_trace2_scepter(u, state, **kw):
    """行迹2·追念之权杖: 战斗开始时行动提前30%"""
    if u.char.id != 'trailblazer_remembrance':
        return
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu is u and i in navs:
            from engine.core.combat_engine import _effective_spd
            navs[i] = max(0, navs[i] - (10000 / _effective_spd(u, state)) * 0.30)
            state.log.append('  追念之权杖: 行动提前30%')
            break


def _eid_tbr_e2(u, state, **kw):
    """v5.7 开拓者·记忆E2: 除迷迷以外的我方忆灵行动时, 开拓者恢复8能量（每回合最多1次）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'trailblazer_remembrance' or not owner.is_alive:
        return
    if owner.extra.get('tbr_e2_used'):
        return
    ms_unit = kw.get('ms_unit')
    if ms_unit is None or getattr(ms_unit, 'summoner_id', '') == 'trailblazer_remembrance':
        return  # 迷迷自己行动不触发
    from engine.core.combat_engine import _gain_energy
    _gain_energy(owner, 8.0, state=state)
    owner.extra['tbr_e2_used'] = True
    state.log.append(f'  开拓者·记忆E2: 忆灵行动→+8能量 ({owner.current_energy:.0f})')


def _eid_tbr_e2_reset(u, state, **kw):
    """v5.7 E2 重置: 开拓者回合开始时重置可触发次数"""
    if u.char.id == 'trailblazer_remembrance':
        u.extra['tbr_e2_used'] = False


def _tech_tbr(state, u, is_opener):
    """开拓者·记忆: 全敌行动延后50% + 100%ATK冰伤（开拓者·记忆.txt 秘技·记忆如往日重现）"""
    from engine.core.combat_engine import calculate_damage, _commit_enemy_damage
    from engine.runtime import AV_PER_TURN, _set_av
    stats = u.base_stats
    navs = state.extra.get('navs', {})
    for i, e in enumerate(state.enemies):
        if getattr(e, 'HP', 0) <= 0:
            continue
        # v6.3.0b P1-2: 延后直接改敌方初始行动条（此前写 av_delayed, 敌方本次攻击后才消费）
        delay = AV_PER_TURN / max(e.SPD, 1) * 0.50
        _set_av(state, navs, ('e', i), navs.get(('e', i), e.av) + delay)
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '冰', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    state.log.append('[秘技] 记忆如往日重现: 全敌行动延后50% + 100%ATK冰伤')


CHAR_ID = "trailblazer_remembrance"
TECHNIQUE = _tech_tbr


def _mimi_charge_gain(state, ms, gain):
    """迷迷充能统一入口: 满后再次获得充能=拉条(行动提前插队)。

    v7.17.0: 自 remembrance.RemembranceSystem 方法 verbatim 迁入——迷迷为开拓者·记忆
    专属忆灵, 非通用记忆系统职责（v7.16.0 验收 P3-2）。"""
    old = ms.extra.get('charge', 0)
    new = min(100, old + gain)
    ms.extra['charge'] = new
    if old >= 100 and gain > 0:
        # 充能已满再获充能: 拉条插队（例3规则）
        key = ('ms', id(ms))
        ms.extra['next_av'] = state.current_av
        state.extra.setdefault('av_stamp', {})
        state.extra['stamp_counter'] = state.extra.get('stamp_counter', 0) + 1
        state.extra['av_stamp'][key] = state.extra['stamp_counter']
        state.log.append(f'  充能已满→迷迷拉条插队!')
    return new


def _tbr_energy_bank(u, state, gained=0):
    """OBSERVER energy_gain_bank: 我方全体每累计恢复10点能量→迷迷+1%充能（v5.7 天赋,
    全渠道统一: 施放技能/受击/光锥/藿藿终结技等回能一律经 _gain_energy, 此前只统计
    施放者自身技能回能）。v7.17.0 自 combat_engine._gain_energy 内联分支 verbatim 迁入
    （原位置派发→on_energy_change 之前, 时序不变）。"""
    tbr = next((x for x in state.units
                if x.char.id == 'trailblazer_remembrance' and x.memsprite_unit
                and x.memsprite_unit.is_alive), None)
    if tbr:
        bank = tbr.extra.setdefault('tbr_energy_bank', 0.0) + gained
        full_pct = int(bank // 10)
        tbr.extra['tbr_energy_bank'] = bank - full_pct * 10
        if full_pct > 0:
            ms = tbr.memsprite_unit
            _mimi_charge_gain(state, ms, full_pct)
    return None


# ---- M5a: 技能相位处理器（原引擎 _use_skill 内联, verbatim 迁入）----

def _tbr_zero_energy_cast(u, state):
    """OBSERVER zero_energy_cast: 零能量单位施技→迷迷+3%充能（开拓者·记忆E4）。"""
    tbr4 = next((x for x in state.units
                 if x.char.id == 'trailblazer_remembrance' and x.eidolon_rank >= 4
                 and x.memsprite_unit and x.memsprite_unit.is_alive), None)
    if tbr4:
        ms = tbr4.memsprite_unit
        ch = _mimi_charge_gain(state, ms, 3)
        state.log.append(f'  开拓者·记忆E4: 零能量单位施技→迷迷充能+3% → {ch:.0f}%')
    return None


def _tbr_ult_cast_resource(u, state, skill):
    """PHASE ult_cast_resource: 终结技——未完的尾声+1史诗(最多2层)；迷迷+40%充能。"""
    # 开拓者·记忆终结技: 未完的尾声+1史诗(最多2层)；迷迷+40%充能
    u.extra['tbr_epic'] = min(2, u.extra.get('tbr_epic', 0) + 1)
    state.log.append(f'  未完的尾声: 史诗+1 → {u.extra["tbr_epic"]}/2')
    if u.memsprite_unit and u.memsprite_unit.is_alive:
        ms = u.memsprite_unit
        ch = _mimi_charge_gain(state, ms, 40)
        state.log.append(f'  终结技: 迷迷充能+40% → {ch:.0f}%')
    return None


def _tbr_special_resource_cost(u, state, skill, skill_key):
    """PHASE special_resource_cost: 强化普攻消耗1层【史诗】。"""
    # 史诗消耗（开拓者·记忆强化普攻: 消耗1层【史诗】）
    epic_cost = skill.cost.get("_epic", 0)
    if epic_cost <= 0:
        return None
    cur_epic = u.extra.get('tbr_epic', 0)
    if cur_epic < epic_cost:
        state.log.append(f'  [WARN] 史诗不足({cur_epic}<{epic_cost})')
        return (True, None)
    u.extra['tbr_epic'] = cur_epic - epic_cost
    state.log.append(f'  史诗-{epic_cost} → {u.extra["tbr_epic"]}/2')
    return (False, None)


def _tbr_cast_side_effects(u, state, skill, skill_key):
    """PHASE cast_side_effects: 战技→迷迷回60%生命+10%充能；强化普攻→迷迷+10%充能。"""
    # 开拓者·记忆: 战技→迷迷回60%生命+10%充能；强化普攻→迷迷+10%充能
    if not (u.memsprite_unit and u.memsprite_unit.is_alive):
        return None
    ms = u.memsprite_unit
    if skill_key == "skill":
        ms.current_hp = min(ms.max_hp, ms.current_hp + ms.max_hp * 0.60)
        ch = _mimi_charge_gain(state, ms, 10)
        state.log.append(f'  战技: 迷迷回血60% + 充能+10% → {ch:.0f}%')
    elif skill_key == "basic_attack_enhanced":
        ch = _mimi_charge_gain(state, ms, 10)
        state.log.append(f'  强化普攻: 迷迷充能+10% → {ch:.0f}%')
    return None


def _tbr_pre_hooks_cast(u, state, skill_key):
    """PHASE pre_hooks_cast: 献予「创世」之诗——强化普攻后德谬歌额外回合。"""
    # 献予「创世」之诗: 开拓者强化普攻后→德谬歌额外回合自动花与箭
    # (德谬歌SPD=0禁止入X轴, 直调 _use_memsprite_skill 防除零)
    if skill_key == 'basic_attack_enhanced' and u.extra.get('poem_chuangshi'):
        xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
        rem = state.extra.get('_rem_sys')
        if xilian and rem and xilian.memsprite_unit and xilian.memsprite_unit.is_alive:
            rem._use_memsprite_skill(state, xilian, xilian.memsprite_unit, "memsprite_basic")
            state.log.append('  献予「创世」之诗: 德谬歌额外回合→花与箭')
    return None


PHASE_HOOKS = {'ult_cast_resource': _tbr_ult_cast_resource,
               'special_resource_cost': _tbr_special_resource_cost,
               'cast_side_effects': _tbr_cast_side_effects,
               'pre_hooks_cast': _tbr_pre_hooks_cast}
OBSERVER_HOOKS = {'zero_energy_cast': _tbr_zero_energy_cast,
                  'energy_gain_bank': _tbr_energy_bank}


# ---- M5a 批4: 伤害循环/攻击后结算相位处理器（原 _use_skill 内联, verbatim 迁入）----


def _tbr_mimi_ult(u, state, skill, skill_key):
    """PHASE mimi_ult: 终结技→迷迷240%ATK全体冰伤（返回计入 total 的伤害量）。"""
    from engine.runtime import _enemy_for_damage, _select_targets
    from engine.systems.remembrance import _ms_effective_stats
    from engine.core.combat_engine import (
        _apply_toughness_damage, _build_effective_stats)
    from engine.characters.seele import _apply_luandie
    # v5.7: 开拓者·记忆终结技→迷迷240%ATK全体冰伤（文档: 使迷迷对敌方全体造成240%攻击力伤害;
    # 伤害由忆灵释放, JSON multipliers 为空, 须在 if skill.multipliers 块外, 此前伤害段缺失）
    if not (skill_key == 'ultimate' and u.memsprite_unit and u.memsprite_unit.is_alive):
        return None
    ms = u.memsprite_unit
    ms_stats = _ms_effective_stats(ms, state)
    # v5.7 E6: 终结技的暴击率固定为100%
    ult_crit = u.eidolon_rank >= 6 or ms_stats.CRIT_RATE >= 0.5
    mimi_damage = 0.0
    for t in (state.alive_enemies() or state.enemies):
        d = calculate_damage(ms_stats, _enemy_for_damage(t), ms_stats.ATK, 240.0,
                             "direct", "冰", 80, ult_crit,
                             skill_type="ultimate", true_dmg_ratio=state.realm_true_dmg,
                             crit_mode="expected")
        mimi_damage += d.final_damage
        _commit_enemy_damage(
            state, u, t, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        _apply_luandie(state, t, u)
    # 削韧20（主削韧块在 if skill.multipliers 内被跳过, 此处按 JSON ultimate effects 结算）
    u_stats = _build_effective_stats(u, state)
    toughness_dmg = 0.0
    for eff in skill.effects:
        etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
        if etype != 'toughness_reduction':
            continue
        base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
        eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
        for t in _select_targets(state.alive_enemies() or state.enemies, eff_target):
            toughness_dmg += _apply_toughness_damage(
                state, u, t, base_toughness, "冰", skill_key, u_stats)
    state.log.append(f'  终结技: 迷迷240%ATK全体冰伤')
    u.total_damage_dealt += mimi_damage
    state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=mimi_damage > 0)  # v7.1.0 P1: 0倍率终结技补气氛
    return mimi_damage + toughness_dmg


PHASE_HOOKS['mimi_ult'] = _tbr_mimi_ult


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_chuangshi(state, summoner, ms_unit, tbr):
    """献予「创世」之诗(整场): ATK+德谬歌HP16%, CR+德谬歌CR72%(迷迷); 强化普攻后→德谬歌花与箭"""
    if tbr.extra.get('poem_chuangshi_applied'):
        return
    ms = summoner.memsprite_unit
    atk_bonus = ms.max_hp * 0.16
    cr_bonus = ms.base_stats.CRIT_RATE * 0.72
    tbr.base_stats.ATK += atk_bonus
    tbr.base_stats.CRIT_RATE += cr_bonus
    if tbr.memsprite_unit and tbr.memsprite_unit.is_alive:
        tbr.memsprite_unit.base_stats.ATK += atk_bonus
        tbr.memsprite_unit.base_stats.CRIT_RATE += cr_bonus
    tbr.extra.update(poem_chuangshi=True, poem_chuangshi_applied=True,
                     poem_chuangshi_atk=atk_bonus, poem_chuangshi_cr=cr_bonus)
    state.log.append(f'  献予「创世」之诗: 开拓者ATK+{atk_bonus:.0f}, CR+{cr_bonus*100:.1f}%')


POEM = ("创世", _poem_chuangshi, True)


# ---- v7.15.0: 角色 AI（原 remembrance 方法, verbatim 迁入; _use_skill 保持函数级导入）----


def tbr_ai(u, state, **kw):
    """开拓者·记忆AI: 能量满→终结技(+1史诗); 史诗+迷迷在场→强化普攻; SP>0→战技; 否则普攻"""
    from engine.core.combat_engine import _use_skill
    # 未完的尾声: 持史诗且迷迷在场→普攻强化为明天一同写下
    epic = u.extra.get('tbr_epic', 0)
    has_mimi = u.memsprite_unit and u.memsprite_unit.is_alive
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        return
    if epic > 0 and has_mimi:
        _use_skill(u, state, "basic_attack_enhanced")
        return
    if state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


AI = tbr_ai


# ---- v7.16.0: 迷迷专属（原 remembrance 专属方法, verbatim 迁入）----

def _tbr_support_skill(state, summoner, ms_unit):
    """我会！帮你！: 指定我方单体行动提前100% + 【迷迷的声援】3回合。
    声援: 每造成1次伤害→额外28%真伤。对自身施放不触发行动提前。"""
    from engine.runtime import TimedBuff
    # 选目标: 优先主C(非自己)，否则自己
    targets = [eu for eu in state.units if eu.is_alive and eu.char.id != 'trailblazer_remembrance']
    if not targets:
        targets = [eu for eu in state.units if eu.is_alive]
    if not targets:
        return
    target = targets[0]
    # 声援3回合
    attrs = {"_tbr_support": 1}
    # v5.7 E1: 持有声援者暴击率+10%, 且声援效果对该目标的忆灵/忆师也生效
    if summoner.eidolon_rank >= 1:
        attrs["CRIT_RATE"] = 10.0
    tb = TimedBuff(source_id=summoner.char.id,
                   attributes=attrs, remaining_turns=3,
                   source_name="迷迷的声援")
    target.buffs.append(tb)
    if summoner.eidolon_rank >= 1 and target.memsprite_unit \
            and target.memsprite_unit.is_alive:
        target.memsprite_unit.buffs.append(TimedBuff(
            source_id=summoner.char.id,
            attributes={"CRIT_RATE": 10.0, "_tbr_support": 1}, remaining_turns=3,
            source_name="迷迷的声援(E1对忆灵)"))
    # 行动提前100%（非自身）
    if target.char.id != 'trailblazer_remembrance':
        from engine.characters.robin_summeretto import _guest_advance_blocked
        navs = state.extra.get('navs', {})
        for i, eu in enumerate(state.units):
            if eu is target and i in navs \
                    and not _guest_advance_blocked(state, summoner, eu):
                navs[i] = state.current_av
                break
    # 充能清零（100%已消耗）
    ms_unit.extra['charge'] = 0
    state.log.append(f'  我会！帮你！: {target.char.name}行动提前100%+【迷迷的声援】3回合')


def _tbr_memsprite_ai(state, summoner, ms_unit):
    """迷迷行动: 充能<100%→坏人麻烦; 充能100%→我会帮你"""
    from engine.systems.remembrance import RemembranceSystem
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    charge = ms_unit.extra.get('charge', 0)
    if charge >= 100:
        rem._use_memsprite_skill(state, summoner, ms_unit, "memsprite_support")
    else:
        rem._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")


# ---- v7.16.0 相位: 记忆生命周期/忆灵管线站点（原 remembrance 内联, verbatim 迁入）----


def _tbr_ms_build(u, state, ms_data, hp_override):
    """PHASE ms_build: 迷迷构建（HP×80%+640/SPD130/创世补挂/充能50%+首召40%/伙伴一起）。"""
    from engine.systems.remembrance import MemSpriteUnit
    ms_stats = copy.deepcopy(u.base_stats)
    ms_stats.HP = u.base_stats.HP * 0.80 + 640
    ms_stats.SPD = 130
    ms_stats.ATK = u.base_stats.ATK
    ms_unit = MemSpriteUnit(
        data=ms_data, summoner_id=u.char.id,
        max_hp=ms_stats.HP, current_hp=ms_stats.HP,
        base_stats=ms_stats,
    )
    ms_unit.current_energy = 0
    ms_unit.extra['charge'] = 0.0  # 迷迷充能 0-100%
    state.memsprites.append(ms_unit)
    u.memsprite_unit = ms_unit
    state.log.append(f'  召唤迷迷 HP={ms_stats.HP:.0f} SPD={ms_stats.SPD:.0f} '
                     f'(开拓者HP×80%+640, SPD=130)')
    # 献予「创世」之诗: 迷迷重召→补挂存量ATK/CR加成
    if u.extra.get('poem_chuangshi'):
        ms_unit.base_stats.ATK += u.extra.get('poem_chuangshi_atk', 0.0)
        ms_unit.base_stats.CRIT_RATE += u.extra.get('poem_chuangshi_cr', 0.0)
    # 忆灵天赋·迷迷加油: 召唤时+50%充能
    ch = _mimi_charge_gain(state, ms_unit, 50)
    state.log.append(f'  迷迷加油: 充能+50% → {ch:.0f}%')
    # 行迹2·追念之权杖: 首次召唤+40%充能
    if not u.extra.get('tbr_summoned'):
        u.extra['tbr_summoned'] = True
        ch = _mimi_charge_gain(state, ms_unit, 40)
        state.log.append(f'  追念之权杖: 首次召唤充能+40% → {ch:.0f}%')
    # 忆灵天赋·伙伴一起: 全队暴伤 += 迷迷12%暴伤 + 24%
    cd_bonus = ms_stats.CRIT_DMG * 0.12 + 0.24
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.CRIT_DMG += cd_bonus
    state.log.append(f'  伙伴一起: 全队暴伤+{cd_bonus*100:.1f}%')
    state.hooks.trigger_all("on_memsprite_summon", u=u, state=state,
                             summoner=u, ms_unit=ms_unit)
    return ms_unit


def _tbr_ms_despawn(u, state, ms_unit, ms_name):
    """PHASE ms_despawn_settle: 遗憾不留——迷迷消失→开拓者行动提前25%。"""
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu is u and i in navs:
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(u, state)) * 0.25)
            break
    state.log.append('  遗憾不留: 迷迷消失→开拓者行动提前25%')
    return True


def _tbr_ms_support_cast(u, state, ms_unit, skill):
    """PHASE ms_support_cast: 我会！帮你！（无倍率辅助技）。"""
    from engine.systems.remembrance import _dispatch_memsprite_support_events
    _tbr_support_skill(state, u, ms_unit)
    _dispatch_memsprite_support_events(state, u, skill)
    return True


def _tbr_ms_after_attack(u, state, ms_unit, skill_key):
    """PHASE ms_after_attack: 袖珍的事诗——坏人麻烦后+5%充能。"""
    if skill_key == "memsprite_basic":
        ch = _mimi_charge_gain(state, ms_unit, 5)
        state.log.append(f'  袖珍的事诗: 充能+5% → {ch:.0f}%')
    return None


def _tbr_ms_ai(u, state, ms_unit):
    """PHASE ms_ai: 迷迷按充能调度。"""
    _tbr_memsprite_ai(state, u, ms_unit)
    return True


PHASE_HOOKS['ms_build'] = _tbr_ms_build
PHASE_HOOKS['ms_despawn_settle'] = _tbr_ms_despawn
PHASE_HOOKS['ms_support_cast'] = _tbr_ms_support_cast
PHASE_HOOKS['ms_after_attack'] = _tbr_ms_after_attack
PHASE_HOOKS['ms_ai'] = _tbr_ms_ai
