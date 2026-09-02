"""战斗通用计算 — 受击概率、秘技释放、效果命中等"""
from typing import Union
from engine.constants import PATH_TAUNT_VALUES
from engine.models.character import Skill


def calc_effect_probability(base_chance: float, effect_hit_rate: float,
                            effect_res: float) -> float:
    """最终命中概率 = 基础概率 × (1 + 效果命中) × (1 - 效果抵抗)"""
    return base_chance * (1.0 + effect_hit_rate) * (1.0 - effect_res)


# ════════ v6.3.0 秘技系统（用户 2026-08-14 确认语义）════════
# 生效策略: 非进战秘技(support)全部默认开启; 进战秘技(battle_start)只开启站位最靠前角色(开怪者)
# 判定标准: 文本以"主动攻击/下落攻击/牵引开怪"为前提=battle_start; "使用秘技后下一次战斗开始时"=support
# 开怪者无battle_start时: 首个属性命中敌方弱点的角色; 无敌方弱点对应角色→队伍第一个角色

def _tech_enemies(state):
    """存活敌人列表（无存活则全部）"""
    return [e for e in state.enemies if getattr(e, 'HP', 0) > 0] or list(state.enemies)


def _tech_tbh(state, u, is_opener):
    """同谐: 全队击破特攻+30% 2回合（开拓者·同谐.txt 秘技·即刻！独奏团）"""
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='trailblazer_harmony',
                                      attributes={'BREAK_EFFECT': 30.0},
                                      remaining_turns=2,
                                      param_id='tbh_tech_be'))
    state.log.append('[秘技] 即刻！独奏团: 全队击破特攻+30% 2回合')


def _tech_fengjin(state, u, is_opener):
    """风堇: 全队回复30%生命上限+600 + 全队生命上限+20% 2回合（风堇.txt 秘技·天气正好，万物可爱！）
    回退由风堇 tick_turn 的 tech_maxhp_turns 到期执行"""
    heal = u.base_stats.HP * 0.30 + 600
    for eu in state.units:
        if eu.is_alive:
            eu.current_hp = min(eu.max_hp, eu.current_hp + heal)
            if 'tech_orig_maxhp' not in eu.extra:
                eu.extra['tech_orig_maxhp'] = eu.max_hp
            eu.max_hp = eu.max_hp * 1.20
            eu.current_hp = min(eu.max_hp, eu.current_hp)
    u.extra['tech_maxhp_turns'] = 2
    state.log.append(f'[秘技] 天气正好: 全队回复{heal:.0f}HP + 生命上限+20% 2回合')


def _tech_changyeyue(state, u, is_opener):
    """长夜月: 全队忆灵暴伤+24%(与战技同实现) 2回合 + 忆质+1（长夜月.txt 秘技·愿有冷雨落下）"""
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='changyeyue', attributes={'CRIT_DMG': 24.0},
                             remaining_turns=2, param_id='changyeyue_tech_cd'))
    from engine.systems.remembrance import _gain_yizhi
    _gain_yizhi(state, u, 1)
    state.log.append('[秘技] 愿有冷雨落下: 忆灵暴伤+24%(与战技同) + 忆质+1')


def _tech_lingsha(state, u, is_opener):
    """灵砂: 召唤浮元 + 全敌醇醉2回合（用户 2026-08-14 补录: 秘技·流翠散云, 非进战）"""
    # v6.3.0b P1-1: 秘技阶段标记系统未惰性创建, 需先创建再召唤（此前 sys 为 None 只挂醇醉）
    from engine.core.combat_sim import _ensure_marker_system
    sys = _ensure_marker_system(state)
    sys.spawn(state, u, 'lingsha_fuyuan')
    from engine.models.enemy import EnemyStatus
    for e in _tech_enemies(state):
        e.add_status(EnemyStatus(id='lingsha_chunzui', name='醇醉', category='debuff',
                                 source='lingsha', remaining_turns=2,
                                 attributes={'vulnerability_break': 0.25}))
    state.log.append('[秘技] 流翠散云: 召唤浮元 + 全敌醇醉2回合')


def _tech_xilian(state, u, is_opener):
    """昔涟: 进战展开战技结界（真伤24% 2回合; 不获追忆——秘技非战技）（昔涟.txt 秘技·西风尽头）"""
    from engine.core.combat_sim import SimState
    if state.realm_owner and state.realm_owner != 'xilian':
        state.log.append(f'  [WARN] 境界已被{state.realm_owner}占据, 昔涟秘技结界无法展开')
        return
    state.realm_owner = 'xilian'
    state.realm_turns = 2
    state.realm_true_dmg = 0.24
    state.log.append('[秘技] 西风尽头: 展开结界(真伤24% 2回合)')


def _tech_mydei(state, u, is_opener):
    """万敌: 全敌80%生命上限虚数伤 + 嘲讽1回合 + 充能+50（万敌.txt 秘技·折戟臣服的监牢）"""
    from engine.core.combat_sim import calculate_damage, _apply_enemy_taunt, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.HP, 80.0, 'direct', '虚数', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    _apply_enemy_taunt(state, u, state.enemies, turns=1)
    u.extra['mydei_charge'] = min(200, u.extra.get('mydei_charge', 0) + 50)
    state.log.append('[秘技] 折戟臣服的监牢: 全敌80%HP虚数伤 + 嘲讽1回合 + 充能+50')


def _tech_fugue(state, u, is_opener):
    """忘归人: 行动提前40% + 全敌DEF-18% 2回合(100%基础概率走EHR)（忘归人.txt 秘技·炤炤彻旷）"""
    from engine.core.combat_sim import AV_PER_TURN, _roll_effect_hit
    from engine.models.enemy import EnemyStatus
    navs = state.extra.get('navs', {})
    uidx = state.units.index(u)
    if uidx in navs:
        navs[uidx] = max(0, navs[uidx] - AV_PER_TURN / max(u.base_stats.SPD, 1) * 0.40)
    for e in _tech_enemies(state):
        if not _roll_effect_hit(u, state, e, '防御降低', base_chance=1.0):
            continue
        e.add_status(EnemyStatus(id='fugue_def_down', name='防御降低', category='debuff',
                                 source='fugue', remaining_turns=2,
                                 attributes={'def_reduction': 0.18}))
    state.log.append('[秘技] 炤炤彻旷: 行动提前40% + 全敌DEF-18% 2回合')


def _tech_firefly(state, u, is_opener):
    """流萤: 标记秘技生效——进战首波 + 每波次: 全敌火弱点2回合 + 200%ATK火伤 + 削韧20
    （流萤.txt 秘技·Δ指令-焦土陨击: 每个波次开始时）"""
    state.extra['firefly_tech_active'] = True
    from engine.core.combat_sim import _apply_firefly_tech_wave
    _apply_firefly_tech_wave(state, u)


def _tech_tbr(state, u, is_opener):
    """开拓者·记忆: 全敌行动延后50% + 100%ATK冰伤（开拓者·记忆.txt 秘技·记忆如往日重现）"""
    from engine.core.combat_sim import AV_PER_TURN, calculate_damage, _set_av, _commit_enemy_damage
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


def _tech_aglaea(state, u, is_opener):
    """阿格莱雅: 召唤衣匠 + 全敌100%ATK雷伤 + 削韧20 + 能量30 + 随机敌织线
    （阿格莱雅.txt 秘技·披星百裂; 召唤分支自带立即行动≈开怪攻击, 接受该副作用）"""
    from engine.core.combat_sim import (calculate_damage, _apply_toughness_damage,
                                        _gain_energy, _commit_enemy_damage)
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


def _tech_xiadie(state, u, is_opener):
    """遐蝶: 开怪→召唤死龙(HP=新蕊上限50%)+行动提前100%+境界+全队40%当前HP消耗;
    非开怪→新蕊+30%上限（用户 2026-08-14 确认: 可选开怪, 一般当开怪判定）"""
    from engine.core.combat_sim import xiadie_xinrui_cap
    from engine.systems.remembrance import RemembranceSystem
    if not is_opener:
        gain = xiadie_xinrui_cap(u) * 0.30
        u.xinrui = min(xiadie_xinrui_cap(u), u.xinrui + gain)
        state.log.append(f'[秘技] 悲鸣: 非开怪→新蕊+30%上限(+{gain:.0f})')
        return
    rem = state.extra.get('_rem_sys') or RemembranceSystem()
    state.extra['_rem_sys'] = rem
    if not (u.memsprite_unit and u.memsprite_unit.is_alive):
        rem.summon_memsprite(state, u, u.char.memsprite, hp_override=xiadie_xinrui_cap(u) * 0.50)
    # v6.3.0b P1-5: 行动提前100%（此前 navs 未动, 立即行动从未生效）
    from engine.core.combat_sim import _set_av
    navs = state.extra.get('navs', {})
    uidx = state.units.index(u)
    if uidx in navs:
        _set_av(state, navs, uidx, state.current_av)
    # 境界: 遗世冥域（敌方全抗-20%; 与昔涟秘技结界不同来源, 用户确认不冲突）
    if not state.realm_owner:
        state.realm_owner = 'xiadie'
        state.realm_turns = 3
        for e in state.enemies:
            for elem in list(e.element_res):
                e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
        state.log.append('  遗世冥域: 敌方全属性抗性-20% (3回合)')
    # v6.3.0b P1-5: 全队40%当前HP消耗走统一管线（角色+忆灵, 死龙除外; 新蕊/on_hp_loss/光锥事件）
    from engine.core.combat_sim import (_xiadie_absorb_hp_loss,
                                          _dispatch_changyeyue_hp_loss,
                                          _process_lc_effects, SimUnit)
    total_lost = 0.0
    affected = []
    for eu in state.units:
        if eu.is_alive:
            lost = eu.current_hp * 0.40
            eu.current_hp = max(1, eu.current_hp - lost)
            total_lost += lost
            affected.append((eu, lost))
    for ms in state.memsprites:
        if not ms.is_alive or ms is u.memsprite_unit:
            continue  # 死龙不参与（用户确认）
        lost = ms.current_hp * 0.40
        ms.current_hp = max(1, ms.current_hp - lost)
        total_lost += lost
        affected.append((ms, lost))
    _xiadie_absorb_hp_loss(state, total_lost, '秘技悲鸣')
    state.hooks.trigger_all("on_hp_loss", u=u, state=state,
                             total_lost=total_lost, affected=affected,
                             skill_key='technique')
    _dispatch_changyeyue_hp_loss(state, affected)
    for affected_unit, _lost in affected:
        if isinstance(affected_unit, SimUnit):
            state.extra['lc_last_hp_loss'] = _lost
            _process_lc_effects(affected_unit, state, "on_hp_loss")
    state.log.append('[秘技] 悲鸣: 召唤死龙(HP=新蕊50%) + 行动提前100% + 境界 + 全队40%当前HP消耗')


def _tech_seele(state, u, is_opener):
    """希儿: 进战立即进入增幅状态（希儿.txt 秘技·幻身, 进战）
    v6.3.0b P1-4: 直接挂增幅 Buff（此前走 seele_amplify_pending, 被常规回合入口无条件清零）"""
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='seele', attributes={'DMG_BONUS_ALL': 80.0},
                             remaining_turns=1, source_name='再现增幅'))
    state.log.append('[秘技] 幻身: 进战立即进入增幅状态(80%增伤1回合)')


def _tech_silver_wolf(state, u, is_opener):
    """银狼: 立即攻击敌人——全敌80%ATK量子伤 + 无视弱点削韧全体, 击破触发量子击破
    （银狼.txt 秘技·强制结束进程, 进战）"""
    from engine.core.combat_sim import calculate_damage, _apply_break_debuff, _commit_enemy_damage
    from engine.models.enemy import EnemyStatus
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 80.0, 'direct', '量子', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
        # 无视弱点削韧（不受弱点门控; 对齐 _no_weakness_pen 语义）
        if e.toughness > 0 and e.max_toughness > 0:
            e.toughness = max(0, e.toughness - 20.0)
            if e.toughness <= 0 and not e.is_broken:
                e.is_broken = True
                bd = calculate_damage(stats, e, 0, 0, 'break', '量子', 80, False)
                _commit_enemy_damage(state, u, e, bd.final_damage)
                u.total_damage_dealt += bd.final_damage
                e.extra['av_delayed'] = 2500.0
                _apply_break_debuff(e, '量子', u, state)
                state.log.append(f'  秘技击破: {e.name or e.id} 击破={bd.final_damage:.0f}(量子)')
    state.log.append('[秘技] 强制结束进程: 全敌80%ATK量子伤 + 无视弱点削韧20')


def _tech_yinlang(state, u, is_opener):
    """银狼Lv.999秘技：开战后每个波次触发一次固定99笑点盲盒。"""
    if not is_opener:
        return
    state.extra['yinlang_tech_active'] = True
    elation = state.extra.get('_elation')
    if elation:
        elation.silver_technique_wave(u, state)
    state.log.append('[秘技] 朋友，这才是T0级秘技: 本波次盲盒已触发(固定99好活)')


def _tech_bronya(state, u, is_opener):
    """布洛妮娅: 全队攻击力+15% 2回合（布洛妮娅.txt 秘技·在旗帜下, 非进战）"""
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='bronya', attributes={'ATK_PERCENT': 15.0},
                                      remaining_turns=2, param_id='bronya_technique_atk'))
    state.log.append('[秘技] 在旗帜下: 全队攻击力+15% 2回合')


def _tech_fuxuan(state, u, is_opener):
    """符玄: 开启穷观阵——全队减伤18%+承伤65%分摊 + 鉴知(生命上限+6%+暴击+12%) 3回合
    （符玄.txt 秘技·太微行棋; 复用 _distribute_damage 承伤管线, fuxuan_field_turns 驱动）"""
    from engine.core.combat_sim import TimedBuff, _gain_energy
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


def _tech_huohuo(state, u, is_opener):
    """藿藿: 与魄散敌人进战→100%基础概率全敌攻击力-25% 2回合（藿藿.txt 秘技·凶煞·劾压鬼物, 非进战）"""
    from engine.core.combat_sim import _roll_effect_hit
    from engine.models.enemy import EnemyStatus
    for e in _tech_enemies(state):
        if not _roll_effect_hit(u, state, e, '魄散降攻', base_chance=1.0):
            continue
        e.add_status(EnemyStatus(id='huohuo_tech_atk_down', name='攻击力降低', category='debuff',
                                 source='huohuo', remaining_turns=2,
                                 attributes={'atk_down': 0.25}))  # v6.3.0b P1-7: 小写消费键
    state.log.append('[秘技] 凶煞·劾压鬼物: 全敌攻击力-25% 2回合')


def _tech_evanescia(state, u, is_opener):
    """绯英: 全敌100%ATK物理伤+20好活当赏（进战秘技, 绯英.txt 标"（进战）"）
    欢愉角色不入注册表防重复的规则仅限非进战 support（init_battle 无条件全开）;
    进战秘技=主动攻击开怪, v6.7b 落实开怪者门控: 非开怪者不生效。"""
    if not is_opener:
        return
    from engine.core.combat_sim import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '物理', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    elation = state.extra.get('_elation')
    if elation:
        elation.grant_good_show(state, 'evanescia', 20.0, source='technique')
    state.log.append('[秘技] 落英·散者皆忆: 全敌100%ATK物理伤 + 20好活当赏')


def _tech_the_dahlia(state, u, is_opener):
    """大丽花: 立即开启战技结界 + 已破韧目标开战削韧转60%超击破（非进战·领域, 领域互斥）。
    开战削韧值（用户 2026-08-15 确认）: 进战秘技开怪=20, 普攻进战=10——
    按 opener 是否 battle_start 秘技持有者判定。"""
    from engine.core.combat_sim import (_dahlia_field_apply, _build_effective_stats,
                                        calculate_damage, _commit_enemy_damage)
    _dahlia_field_apply(state, u)
    opener_id = state.extra.get('opener_id', '')
    opener = next((x for x in state.units if x.char.id == opener_id), None)
    tech = opener.char.skills.get('technique') if opener else None
    is_bs = bool(tech and getattr(tech, 'technique_category', '') == 'battle_start')
    break_amt = 20.0 if is_bs else 10.0
    stats = _build_effective_stats(u, state)
    for e in state.enemies:
        if e.is_broken:
            sb = calculate_damage(stats, e, 0, 0, 'super_break', '火', 80, False,
                                  toughness_dmg=break_amt)
            sb.final_damage *= 0.60
            _commit_enemy_damage(state, u, e, sb.final_damage)
            u.total_damage_dealt += sb.final_damage
    state.log.append(f'[秘技] 心，是最好的坟茔: 开启结界 + 破韧目标60%超击破(开战削韧{break_amt:.0f})')


def _tech_sunday(state, u, is_opener):
    """星期日: 下次战斗首次技能目标增伤50% 2回合（非进战·荣光之秘）"""
    from engine.core.combat_sim import TimedBuff
    state.extra['sunday_tech_pending'] = True
    state.log.append('[秘技] 荣光之秘: 下次战斗首次技能目标增伤50%')


def _tech_welt(state, u, is_opener):
    """瓦尔特: 全敌禁锢1回合(行动延后20%+减速)（非进战·领域, 领域互斥）"""
    from engine.core.combat_sim import _welt_apply_jinggu
    for e in state.enemies:
        if getattr(e, 'HP', 0) > 0:
            _welt_apply_jinggu(state, u, e, delay_ratio=0.20)
    state.log.append('[秘技] 画地为牢: 全敌禁锢1回合(延后20%+减速)')


def _tech_ruanmei(state, u, is_opener):
    """阮·梅: 自动触发1次战技(不耗SP)（非进战·拭琴抚罗袂）"""
    from engine.core.combat_sim import _ruanmei_xianyin_apply
    _ruanmei_xianyin_apply(state, u)
    state.log.append('[秘技] 拭琴抚罗袂: 自动触发1次战技(弦外音)')


def _tech_robin(state, u, is_opener):
    """知更鸟: 每波次开始回5能量（非进战·领域, 领域互斥; _respawn_wave 接线）"""
    state.extra['robin_tech_active'] = True
    state.log.append('[秘技] 酣醉序曲: 每波次开始知更鸟回5能量')


def _tech_qingge(state, u, is_opener):
    """知更鸟·晴歌: 开战行动提前20% + 立即6气氛 + 全队伤害+30% 2回合（进战·我们自成旋律）"""
    from engine.core.combat_sim import TimedBuff, _qingge_gain_atmo
    # 开战行动提前20%（navs 尚未创建, 由 initial_action_advance_ratio 暂存机制消费）
    u.extra['initial_action_advance_ratio'] = max(
        u.extra.get('initial_action_advance_ratio', 0.0), 0.20)
    _qingge_gain_atmo(state, 6.0, cause='秘技')
    for eu in state.units:
        if eu.is_alive:
            eu.buffs = [b for b in eu.buffs
                        if getattr(b, 'param_id', '') != 'qingge_tech']
            eu.buffs.append(TimedBuff(source_id='robin_summeretto',
                                      attributes={'DMG_BONUS_ALL': 30.0},
                                      remaining_turns=2, param_id='qingge_tech',
                                      source_name='我们自成旋律'))
    state.log.append('[秘技] 我们自成旋律: 开战行动提前20% + 6气氛 + 全队伤害+30% 2回合')


def _tech_busitu(state, u, is_opener):
    """不死途: 全敌100%ATK雷伤+1充能（非进战·吃吧，可憎的手）"""
    from engine.core.combat_sim import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '雷', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage)
        u.total_damage_dealt += d.final_damage
    u.extra['busitu_charge'] = min(3, u.extra.get('busitu_charge', 0) + 1)
    state.log.append('[秘技] 吃吧，可憎的手: 全敌100%ATK雷伤 + 1充能')


def _tech_qianye(state, u, is_opener):
    """千冶·刃: 全敌嘲讽1回合+自身受伤-90% 2回合（进战·十方无赦）"""
    from engine.core.combat_sim import _apply_enemy_taunt, TimedBuff
    _apply_enemy_taunt(state, u, state.enemies, turns=1)
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'qianye_tech_dr']
    u.buffs.append(TimedBuff(source_id='qianye', attributes={'DMG_REDUCTION': 90.0},
                             remaining_turns=2, param_id='qianye_tech_dr',
                             source_name='十方无赦'))
    state.log.append('[秘技] 十方无赦: 全敌嘲讽1回合 + 自身受伤-90% 2回合')


def _tech_acheron(state, u, is_opener):
    """黄泉: 每波200%ATK雷伤+无视弱点削韧+四相断我(施放终结技后+1残梦+集真赤)
    （进战·四相断我; _respawn_wave 接线）"""
    if not is_opener:
        return
    from engine.core.combat_sim import (_apply_toughness_damage,
                                        _acheron_apply_entry_effects,
                                        _build_effective_stats,
                                        _commit_enemy_damage,
                                        _enemy_for_damage,
                                        calculate_damage)
    state.extra['acheron_tech_active'] = True
    _acheron_apply_entry_effects(state)
    u.extra['acheron_sixiang'] = min(3, u.extra.get('acheron_sixiang', 0) + 1)
    stats = _build_effective_stats(u, state)
    total = 0.0
    for e in state.enemies:
        if getattr(e, 'HP', 0) <= 0:
            continue
        d = calculate_damage(stats, _enemy_for_damage(e, 'technique'), stats.ATK, 200.0, 'direct', '雷', 80,
                             stats.CRIT_RATE >= 0.5,
                             true_dmg_ratio=state.realm_true_dmg,
                             crit_mode='expected')
        _commit_enemy_damage(
            state, u, e, d.final_damage,
            cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
        total += d.final_damage
        _apply_toughness_damage(state, u, e, 20.0, '雷', 'technique', stats)
    u.total_damage_dealt += total
    state.log.append(f'[秘技] 四相断我: 全敌200%ATK雷伤+无视弱点削韧 {total:.0f}，四相断我+1')


def _tech_feixiao(state, u, is_opener):
    """飞霄: 标记秘技生效——每波200%ATK必暴风伤+1飞黄（进战·岚身; _respawn_wave 接线）"""
    if not is_opener:
        return
    from engine.core.combat_sim import (_feixiao_gain_fly, _build_effective_stats,
                                        _commit_enemy_damage, _enemy_for_damage,
                                        calculate_damage)
    state.extra['feixiao_tech_active'] = True
    alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
    if alive:
        stats = _build_effective_stats(u, state)
        scale = 200.0 + min(len(alive) - 1, 8) * 100.0
        for e in alive:
            d = calculate_damage(stats, _enemy_for_damage(e, 'technique'), stats.ATK, scale,
                                 'direct', '风', 80, True,
                                 true_dmg_ratio=state.realm_true_dmg,
                                 crit_mode='boolean')
            _commit_enemy_damage(
                state, u, e, d.final_damage,
                cipher_record_amount=d.final_damage / (1.0 + state.realm_true_dmg))
            u.total_damage_dealt += d.final_damage
        state.log.append(f'[秘技] 岚身: 每波{scale:.0f}%ATK必暴({len(alive)}敌)')
    _feixiao_gain_fly(u, 1)
    state.log.append('[秘技] 岚身: +1飞黄')


def _tech_himeko_nova(state, u, is_opener):
    """姬子·启行: 秘技点上限+3（队伍效果, 无条件） + 每波次开始立即施放1次战技
    （拓星巡航, 进战; v6.7b 落实开怪者门控——战技部分仅开怪者生效;
    普通敌人直接消灭不进入战斗的语义在模拟器内不体现）"""
    state.max_sp += 3
    if not is_opener:
        state.log.append('[秘技] 拓星巡航: 秘技点上限+3')
        return
    state.extra['hn_tech_active'] = True
    from engine.core.combat_sim import _use_skill
    _use_skill(u, state, 'skill')
    state.log.append('[秘技] 拓星巡航: 秘技点上限+3, 首波立即施放战技')


def _tech_phainon(state, u, is_opener):
    """白厄: 秘技点上限+3（txt: 白厄在队伍中时, 队伍效果不门控）+
    开怪者: 全队25能+2毁伤+1SP + 每波200%ATK物理伤（波次hook）。
    （进战秘技; v6.7b 裁决: 进战效果按开怪者门控——v6.8b 拆出队伍效果;
    v6.8.1: ATK+50%×2层归位行迹3, 不再由秘技叠加）"""
    state.max_sp += 3
    if not is_opener:
        state.log.append('[秘技] 终结之始: 秘技点上限+3')
        return
    from engine.core.combat_sim import (_gain_energy, _gain_skill_points,
                                        _phainon_gain_huishang,
                                        _apply_phainon_tech_wave)
    state.extra['phainon_tech_active'] = True
    for eu in state.units:
        if eu.is_alive:
            _gain_energy(eu, 25.0, state=state)
    _phainon_gain_huishang(state, u, 2)
    _gain_skill_points(state, 1)
    _apply_phainon_tech_wave(state, u)  # 首波 200%ATK
    state.log.append('[秘技] 终结之始: 全队25能+2毁伤+1SP + 首波200%ATK')


def _tech_hysilens(state, u, is_opener):
    """海瑟音: 全敌2种随机DOT（非进战·领域醉心）"""
    from engine.core.combat_sim import _hysilens_apply_dot
    for e in _tech_enemies(state):
        _hysilens_apply_dot(state, u, e, count=2)
    state.log.append('[秘技] 于海的栖息地: 全敌2种DOT')


def _tech_anaxa(state, u, is_opener):
    """那刻夏: 全敌添加攻击者属性弱点3回合（非进战）"""
    from engine.core.combat_sim import _anaxa_add_weakness
    for e in _tech_enemies(state):
        _anaxa_add_weakness(state, u, e)
    state.log.append('[秘技] 瞳扉之彩: 全敌+1弱点')


def _tech_cipher(state, u, is_opener):
    """赛飞儿: 全敌100%ATK量子伤 + 记录+200%（进战秘技, 赛飞儿.txt 标"（进战）";
    v6.7b 裁决: 进战秘技按队伍位序靠前开怪者释放——非开怪者不生效）"""
    if not is_opener:
        return
    from engine.core.combat_sim import calculate_damage, _commit_enemy_damage
    stats = u.base_stats
    for e in _tech_enemies(state):
        d = calculate_damage(stats, e, stats.ATK, 100.0, 'direct', '量子', 80, False,
                             crit_mode='expected')
        _commit_enemy_damage(state, u, e, d.final_damage,
                             damage_type='direct', skill_type='technique',
                             cipher_record_multiplier=3.0)
        u.total_damage_dealt += d.final_damage
    state.log.append('[秘技] 穿靴子的猫: 全敌100%ATK量子伤')


def _tech_tribbie(state, u, is_opener):
    """缇宝: 进战获得【神启】3回合（非进战）"""
    from engine.core.combat_sim import _tribbie_apply_shenqi
    _tribbie_apply_shenqi(u, state, turns=3)


def _tech_cerydra(state, u, is_opener):
    """刻律德菈: 获得【军功】+开战自动对军功者施放1次战技（非进战）"""
    from engine.core.combat_sim import _cerydra_grant_jungong
    ally = min(state.units, key=lambda x: getattr(x, 'position', 99))
    if ally is not u:
        _cerydra_grant_jungong(state, u, ally)
    state.log.append('[秘技] 先手优势: 获得【军功】+开战自动战技')


def _tech_dht(state, u, is_opener):
    """丹恒·腾荒: 获得【同袍】+开战自动对同袍施放1次战技（非进战）
    v6.6c P2: 施放者是丹恒自己（此前误用同袍单位释放其战技）"""
    ally = min(state.units, key=lambda x: getattr(x, 'position', 99))
    if ally is not u:
        u.extra['dht_tongpao_id'] = ally.char.id
        ally.extra['dht_tongpao'] = True
        from engine.core.combat_sim import _use_skill
        _use_skill(u, state, 'skill')
    state.log.append('[秘技] 地坼: 获得【同袍】+自动战技')


def _tech_sparkle(state, u, is_opener):
    """花火: 迷误状态期间进战→恢复3战技点 + 花火回20能量（花火.txt 秘技·不可靠叙事者, 非进战）"""
    from engine.core.combat_sim import _gain_skill_points, _gain_energy
    _gain_skill_points(state, 3)
    _gain_energy(u, 20.0, state=state)
    state.log.append('[秘技] 不可靠叙事者: 恢复3战技点 + 花火回20能量')


TECHNIQUE_EFFECTS = {
    'trailblazer_harmony': _tech_tbh,
    'fengjin': _tech_fengjin,
    'changyeyue': _tech_changyeyue,
    'lingsha': _tech_lingsha,
    'xilian': _tech_xilian,
    'mydei': _tech_mydei,
    'fugue': _tech_fugue,
    'firefly': _tech_firefly,
    'trailblazer_remembrance': _tech_tbr,
    'aglaea': _tech_aglaea,
    'xiadie': _tech_xiadie,
    # v6.3.0 第二批: 希儿/银狼/布洛妮娅/符玄/藿藿/花火
    # （开拓者·欢愉/爻光 由 elation.init_battle 实现, 不入注册表防重复;
    #   v6.7 例外: 绯英(进战)入注册表——进战受开怪者门控, init_battle 在其之前执行;
    #   火花(非进战)仍由 elation.init_battle 处理）
    'seele': _tech_seele,
    'silver_wolf': _tech_silver_wolf,
    'yinlang': _tech_yinlang,
    'bronya': _tech_bronya,
    'fu_xuan': _tech_fuxuan,
    'huohuo': _tech_huohuo,
    'sparkle': _tech_sparkle,
    'tribbie': _tech_tribbie,
    'cerydra': _tech_cerydra,
    'dan_heng_permansor_terrae': _tech_dht,
    'hysilens': _tech_hysilens,
    'anaxa': _tech_anaxa,
    'cipher': _tech_cipher,
    'phainon': _tech_phainon,
    # v6.7 绯英（进战, 欢愉角色入注册表破例——见 handler 注释）; 火花秘技由 elation.init_battle 处理
    'evanescia': _tech_evanescia,
    'the_dahlia': _tech_the_dahlia,
    'himeko_nova': _tech_himeko_nova,
    'sunday': _tech_sunday,      # v6.9
    'welt': _tech_welt,          # v6.9
    'ruan_mei': _tech_ruanmei,   # v6.9
    'robin': _tech_robin,        # v6.9
    'robin_summeretto': _tech_qingge,  # v6.11.1 进战
    'busitu': _tech_busitu,      # v6.9
    'qianye': _tech_qianye,      # v6.9 进战
    'acheron': _tech_acheron,    # v6.10 进战
    'feixiao': _tech_feixiao,    # v6.10 进战
}


def apply_techniques(state, units):
    """模拟开始执行秘技（v6.3.0）: support 全部生效; battle_start 取站位最前1个=开怪者。
    无 battle_start 时: 开怪者=首个属性命中敌方弱点的角色, 否则队伍第一个。
    所有 battle_start 持有者都调 handler（is_opener 区分——遐蝶非开怪→新蕊+30%）。
    返回 opener_id（写入 state.extra['opener_id']）"""
    supports = []
    bs_units = []
    for u in sorted(units, key=lambda x: getattr(x, 'position', 99)):
        tech = u.char.skills.get('technique')
        cat = getattr(tech, 'technique_category', '') if tech else ''
        if cat == 'support':
            supports.append(u)
        elif cat == 'battle_start':
            bs_units.append(u)
    opener_unit = bs_units[0] if bs_units else None
    if opener_unit is None:
        alive = [e for e in state.enemies if getattr(e, 'HP', 0) > 0] or list(state.enemies)
        weak_elems = {elem for e in alive
                      for elem, res in (e.element_res or {}).items() if res <= 0}
        opener_unit = next((u for u in units if u.char.element in weak_elems), units[0])
    state.extra['opener_id'] = opener_unit.char.id
    for u in supports:
        fn = TECHNIQUE_EFFECTS.get(u.char.id)
        if fn:
            fn(state, u, is_opener=False)
    for u in bs_units:
        fn = TECHNIQUE_EFFECTS.get(u.char.id)
        if fn:
            fn(state, u, is_opener=(u is opener_unit))
    return state.extra['opener_id']

def resolve_techniques(team: list[dict]) -> dict:
    """战斗开始时解析秘技释放。

    规则：
    - 辅助秘技(support)：全部生效
    - 开战秘技(battle_start)：按站位 1→4 取最靠前者

    team: 队伍列表，每项为 {"position": int, "skills": {"technique": Skill|None, ...}}
    返回: {"support": [Skill, ...], "battle_start": Skill | None}
    """
    supports = []
    battle_start = None

    sorted_team = sorted(team, key=lambda c: c.get("position", 99))

    for c in sorted_team:
        skills = c.get("skills", {})
        tech = skills.get("technique") if isinstance(skills, dict) else None
        if not tech:
            continue

        cat = getattr(tech, "technique_category", "") or ""
        if cat == "support":
            supports.append(tech)
        elif cat == "battle_start" and battle_start is None:
            battle_start = tech

    return {"support": supports, "battle_start": battle_start}


def get_default_taunt(path: str) -> int:
    """获取命途的默认嘲讽值"""
    return PATH_TAUNT_VALUES.get(path, 100)


def calc_hit_probability(unit_taunt: int, all_allied_taunts: list[int]) -> float:
    """计算受击概率 = 单位嘲讽值 / 全队存活单位嘲讽值总和"""
    total = sum(all_allied_taunts)
    if total == 0:
        return 0.0
    return unit_taunt / total


def calc_team_hit_probabilities(taunts: dict[str, int]) -> dict[str, float]:
    """计算全队受击概率分布。

    taunts: {unit_id: taunt_value}
    返回: {unit_id: hit_probability}
    """
    total = sum(taunts.values())
    if total == 0:
        return {uid: 0.0 for uid in taunts}
    return {uid: t / total for uid, t in taunts.items()}
