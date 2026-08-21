"""效果解析器 — 模拟前读取角色/光锥/遗器/星魂，解析为统一效果列表

管线：
  角色 JSON → traces[]/eidolons[]  ─┐
  光锥 JSON → effects[]             ─┤
  遗器套装  → effects[].condition   ─┼→ resolve_character_effects()
                                      │      ↓
                                    └── list[ResolvedEffect] → HookRegistry
"""
from engine.hooks.base import ResolvedEffect, HookRegistry
from engine.models.character import Character
from engine.models.equipment import LightCone, RelicPiece, RelicSet

# ═══════════════════════════════════════════════════════════════════
# 行迹效果注册表: hook_name → {trigger, action, condition?, source_name}
# （dict 结构; handler 签名统一 (u, state, **kw); trigger+action 都非空才注册）
# ═══════════════════════════════════════════════════════════════════

def _trace_basic_energy_bonus(**kwargs):
    """普攻额外回能 +10（爻光特殊行迹、藿藿等）"""
    u = kwargs['u']
    state = kwargs['state']
    bonus = 10
    u.current_energy = min(u.char.max_energy or 999, u.current_energy + bonus)

def _trace_bronya_basic_crit(**kwargs):
    """布洛妮娅行迹「号令」：普攻暴击率100%。由 damage calc 前通过 stats 处理。"""
    pass  # 在 _use_skill 伤害循环内联 t_crit=True，此处为标记


def _trace_bronya_battle_def(u, state, **kw):
    """布洛妮娅行迹「阵地」: 战斗开始全队DEF+20% 2回合"""
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='bronya', attributes={"DEF_PERCENT": 20.0},
                                      remaining_turns=2, source_name='行迹·阵地'))
    state.log.append('  行迹·阵地: 全队DEF+20% (2回合)')


def _trace_bronya_team_dmg(u, state, **kw):
    """布洛妮娅行迹「军势」: 在场全队伤害+10%"""
    if u.char.id != 'bronya':
        return
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.DMG_BONUS_ALL += 0.10
    state.log.append('  行迹·军势: 全队伤害+10%')


def _trace_phainon_trace1(u, state, **kw):
    """白厄行迹1: 开局+1火种"""
    if u.char.id != 'phainon':
        return
    from engine.core.combat_sim import _phainon_gain_huozhong
    _phainon_gain_huozhong(state, u, 1)


def _trace_phainon_trace3(u, state, **kw):
    """白厄行迹3: 进战ATK+50% 第1层（v6.8.1: 统一走叠层函数, 与变身结束第2层共享上限2）"""
    if u.char.id != 'phainon':
        return
    from engine.core.combat_sim import _phainon_trace3_atk_stack
    _phainon_trace3_atk_stack(state, u)
    state.log.append('  行迹·本色: 进战ATK+50%')


def _trace_hysilens_trace1(u, state, **kw):
    """海瑟音行迹1: 开局展开结界3回合 + 回1SP"""
    if u.char.id != 'hysilens':
        return
    from engine.core.combat_sim import _hysilens_field, _gain_skill_points
    _hysilens_field(state, u, turns=3)
    _gain_skill_points(state, 1)
    state.log.append('  行迹·剑旗: 开局结界3回合+回1SP')


def _trace_tb_cr_and_sp(u, state, **kw):
    """开拓者·欢愉行迹1·跟你爆了: 自身暴击率+15%(动态面板) + 施放终结技后回1SP"""
    if u.char.id != 'trailblazer_elation' or kw.get('skill_key') != 'ultimate':
        return
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state, 1)
    state.log.append('  开拓者行迹1: 终结技后回1SP')


def _trace_hysilens_trace3(u, state, **kw):
    """海瑟音行迹3: EHR>60%每10%增伤15%上限90%
    v6.10.3 P1-5: 改为 _build_effective_stats 动态消费（此前入场永久写 base_stats 与动态面板双算）"""
    if u.char.id != 'hysilens':
        return


def _trace_anaxa_trace2(u, state, **kw):
    """那刻夏行迹2: 智识1名CD+140% 或 2名全队伤害+50%"""
    if u.char.id != 'anaxa':
        return
    from engine.core.character_utils import count_remembrance
    n = sum(1 for x in state.units if getattr(x.char, 'path', '') == '智识')
    if n >= 2 or u.eidolon_rank >= 6:
        for eu in state.units:
            if eu.is_alive:
                eu.base_stats.DMG_BONUS_ALL += 0.50
        state.log.append('  行迹·留白: 智识×2 全队伤害+50%')
    if n < 2 or u.eidolon_rank >= 6:
        u.base_stats.CRIT_DMG += 1.40
        state.log.append('  行迹·留白: 智识×1 暴伤+140%')


def _trace_anaxa_trace3(u, state, **kw):
    """那刻夏行迹3: 每弱点无视4%防御上限28%"""
    if u.char.id != 'anaxa':
        return
    u.extra['anaxa_trace3'] = True


def _trace_anaxa_basic_energy(u, state, **kw):
    """那刻夏行迹1: 普攻额外回10能量。"""
    if u.char.id == 'anaxa':
        from engine.core.combat_sim import _gain_energy
        _gain_energy(u, 10.0, state=state)


def _trace_anaxa_turn_energy(u, state, **kw):
    """那刻夏行迹1: 回合开始且没有质性揭露目标时回30能量。"""
    if u.char.id != 'anaxa':
        return
    if not any(getattr(e, 'extra', {}).get('anaxa_revealed')
               for e in state.enemies if getattr(e, 'HP', 0) > 0):
        from engine.core.combat_sim import _gain_energy
        _gain_energy(u, 30.0, state=state)


def _eid_anaxa_e2(u, state, **kw):
    """那刻夏E2: 敌人进入每个波次时添加弱点并降低全抗20%。"""
    if u.char.id != 'anaxa':
        return
    from engine.core.combat_sim import _anaxa_apply_entry_effects
    _anaxa_apply_entry_effects(state, u)


def _eid_phainon_e6(u, state, **kw):
    if u.char.id == 'phainon':
        from engine.core.combat_sim import _phainon_gain_huozhong
        _phainon_gain_huozhong(state, u, 6)


def _trace_cipher_trace3(u, state, **kw):
    """赛飞儿行迹3: FUA暴伤+100%(动态面板) + 在场敌受伤+40%(对称维护)
    v6.10.3 P1-2: 暴伤改 CRIT_DMG_BY_ATTACK_TYPE['follow_up'] 动态消费（此前全局+1.0污染普攻/战技/终结技）;
    易伤改对称维护（入场/新波 apply, 死亡 remove, 幂等标记防重复叠加）"""
    if u.char.id != 'cipher':
        return
    from engine.core.combat_sim import _cipher_trace3_apply_vuln
    _cipher_trace3_apply_vuln(state)
    state.log.append('  行迹·偷天换日: FUA暴伤+100% + 敌受伤+40%')


def _trace_tribbie_trace1(u, state, **kw):
    """缇宝行迹1: FUA后增伤72%×3层3回合（叠加由 _tribbie_talent_fua 处理）"""
    if u.char.id != 'tribbie':
        return
    u.extra['tribbie_trace1_stack'] = min(3, u.extra.get('tribbie_trace1_stack', 0) + 1)


def _trace_tribbie_trace3(u, state, **kw):
    """缇宝行迹3: 战斗开始回30能量"""
    if u.char.id != 'tribbie':
        return
    from engine.core.combat_sim import _gain_energy
    _gain_energy(u, 30.0, state=state)
    state.log.append('  行迹·小石子: 战斗开始回30能量')


def _trace_cerydra_trace1(u, state, **kw):
    """刻律德菈行迹1: ATK>2000每超100点暴伤+18%上限360%
    v6.10.3 P1-5: 改为 _build_effective_stats 动态消费（此前入场永久写 base_stats 与动态面板双算）"""
    if u.char.id != 'cerydra':
        return


def _trace_cerydra_trace2(u, state, **kw):
    """刻律德菈行迹2: 暴击率+100%"""
    if u.char.id != 'cerydra':
        return
    u.base_stats.CRIT_RATE += 1.0


def _trace_dht_trace2(u, state, **kw):
    """丹恒·腾荒行迹2: 战斗开始行动提前40%"""
    if u.char.id != 'dan_heng_permansor_terrae':
        return
    navs = state.extra.get('navs', {})
    i = state.units.index(u)
    if i in navs:
        remaining = max(0.0, navs[i] - state.current_av)
        navs[i] = state.current_av + remaining * 0.60
    else:
        u.extra['initial_action_advance_ratio'] = \
            u.extra.get('initial_action_advance_ratio', 0.0) + 0.40


def _trace_silver_wolf_gen(u, state, **kw):
    """银狼行迹1·生成: 缺陷持续时间+1回合(植入处统一); 敌弱点被击破时100%概率植入随机缺陷

    v6.3.0b P1-8: on_any_weakness_break 广播事件中 u=击破者; 持有者经 ctx char_id 定位
    （v5.2 问题2 约定: 广播事件 u=事件主体, 持有者由 char_id 区分）"""
    owner = next((x for x in state.units if x.char.id == kw.get('char_id')), None)
    if owner is None:
        return
    owner.extra['silver_wolf_trace1'] = True
    t = kw.get('enemy')
    if t is not None and getattr(t, 'HP', 0) > 0:
        from engine.core.combat_sim import _silver_wolf_implant_defect
        _silver_wolf_implant_defect(state, owner, t)
        state.log.append('  行迹·生成: 击破植入缺陷')


def _trace_silver_wolf_inject_start(u, state, **kw):
    """银狼行迹2·注入: 战斗开始时恢复20点能量"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_sim import _gain_energy
    _gain_energy(u, 20.0, state=state)
    state.log.append('  行迹·注入: 战斗开始回20能量')


def _trace_silver_wolf_inject_turn(u, state, **kw):
    """银狼行迹2·注入: 回合开始时恢复5点能量"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_sim import _gain_energy
    _gain_energy(u, 5.0, state=state)


def _trace_silver_wolf_annotate(u, state, **kw):
    """银狼行迹3·旁注: 每10%效果命中→+10%攻击力, 最高+50%"""
    if u.char.id != 'silver_wolf':
        return
    from engine.core.combat_sim import _build_effective_stats
    ehr = _build_effective_stats(u, state).EFFECT_HIT_RATE
    bonus = min(0.50, int(ehr * 10) * 0.10)
    if bonus > 0:
        u.base_stats.ATK += u.base_stats._base_ATK * bonus
        state.log.append(f'  行迹·旁注: EHR{ehr*100:.0f}%→攻击力+{bonus*100:.0f}%')


def _trace_seele_ripple(u, state, **kw):
    """希儿行迹「涟漪」: 施放普攻后下次行动提前20%"""
    if u.char.id != 'seele':
        return
    from engine.core.combat_sim import AV_PER_TURN
    from engine.core.combat_sim import _effective_spd
    u._pending_action_advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.20
    state.log.append('  行迹·涟漪: 普攻后下次行动提前20%')


def _trace_sparkle_sp_limit(u, state, **kw):
    """花火天赋·叙述性诡计: 战技点上限+2; E4 额外+1（对称维护, 死亡回减）
    v6.10.6 C3"""
    if u.char.id != 'sparkle':
        return
    bonus = 2 + (1 if u.eidolon_rank >= 4 else 0)
    state.max_sp += bonus
    u.extra['sparkle_max_sp_bonus'] = bonus
    state.log.append(f'  花火天赋: 战技点上限+{bonus}')


def _trace_sparkle_turn_end(u, state, **kw):
    """v6.10.6 C3: 我方角色回合结束后, 花火消耗溢出记录补战技点至上限（TXT 花火.txt:39）"""
    from engine.core.combat_sim import _sparkle_turn_end_reserve
    _sparkle_turn_end_reserve(state, u)


def _trace_sparkle_team_cd(u, state, **kw):
    """花火行迹3·夜想曲: 全队ATK+45% + 持战技CD buff者全抗穿+10%
    v6.10.6 C: 改为 _build_effective_stats 动态消费（此前永久写全队暴伤且非TXT口径）"""
    if u.char.id != 'sparkle':
        return


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


def _trace_huohuo_control_resist(u, state, **kw):
    """藿藿行迹「控抗精通」: 抵抗控制+35%
    （v5.0 P4 激活: EFFECT_RES 参与 _apply_player_status 命中检定; 终结技队友ATK+24%在引擎内联）"""
    if u.char.id != 'huohuo':
        return
    u.base_stats.EFFECT_RES += 0.35
    state.log.append('  行迹·控抗精通: 效果抵抗+35%')


def _huohuo_ruming_gain_local(state, u, turns):
    """本地包装: 从 combat_sim 延迟导入（防循环导入）"""
    from engine.core.combat_sim import _huohuo_ruming_gain
    _huohuo_ruming_gain(state, u, turns)


def _trace_huohuo_energy_cycle(u, state, healer=None, targets=None, heal_amt=0, **kw):
    """藿藿行迹3·怯惧应激: 仅禳命治疗触发回1能量
    v6.10.6 B5: 消费点已内联进 combat_sim._huohuo_ruming_heal_all, 本注册改为占位"""
    pass

def _trace_elation_sp_recovery(**kwargs):
    """爻光行迹「暴伤增幅」：施放欢愉技后回复1个战技点"""
    state = kwargs['state']
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  行迹: 欢愉技后回1SP')

def _trace_laugh_gen(**kwargs):
    """欢愉角色行迹：普攻/战技后生成笑点"""
    u = kwargs['u']
    state = kwargs['state']
    skill_key = kwargs.get('skill_key', '')
    if u.char.path != "欢愉" or skill_key not in ("basic_attack", "skill"):
        return
    # 基础笑点 + 角色特定奖励
    bonus = {"yaoguang": 3, "trailblazer_elation": 3}.get(u.char.id, 0)
    if u.char.id == "trailblazer_elation":
        u.current_energy = min(u.char.max_energy, u.current_energy + 10)
    from engine.systems.elation import ElationSystem
    # 好活当赏额外笑点
    gs_total = state.elation_state.get_good_show_total(u.char.id) if hasattr(state, 'elation_state') else 0
    if gs_total > 0:
        bonus += 3
    laugh = 3 + bonus
    state.laugh_points += laugh
    if u.char.id == "yinlang":
        elation = state.extra.get('_elation')
        if elation:
            elation.gain_hidden_score(state, u, laugh)
        else:
            u.hidden_score += laugh

def _trace_yaoguang_field_on_skill(**kwargs):
    """爻光战技展开结界"""
    u = kwargs['u']
    state = kwargs['state']
    if u.char.id != "yaoguang":
        return
    state.yao_field_active, state.yao_field_turns = True, 3

def _trace_yaoguang_energy(**kwargs):
    """爻光普攻额外回能 +10"""
    u = kwargs['u']
    if u.char.id != "yaoguang":
        return
    _trace_basic_energy_bonus(**kwargs)

def _trace_huohuo_energy(**kwargs):
    """藿藿普攻额外回能 +10"""
    u = kwargs['u']
    if u.char.id != "huohuo":
        return
    _trace_basic_energy_bonus(**kwargs)

def _trace_silver_invincible_elation(**kwargs):
    """银狼无敌玩家欢愉技：6×90%弹射"""
    u = kwargs['u']
    state = kwargs['state']
    if u.char.id != "yinlang" or not u.invincible_active:
        return
    from engine.core.damage import calculate_damage
    from engine.core.combat_sim import _build_effective_stats
    total = 0.0
    s = _build_effective_stats(u)
    for _ in range(6):
        d = calculate_damage(s, state.enemy, 0, 90.0, "elation", u.char.element, 80,
                             s.CRIT_RATE >= 0.5, laugh_n=u.hidden_score, crit_mode="expected")
        total += d.final_damage
    u.total_damage_dealt += total
    skill = u.char.skills.get("elation_skill")
    u.damage_log.append(((skill.name + "(无敌)") if skill else "elation", total, "elation_inv"))
    state.log.append(f'[{state.current_av:6.0f}AV] {u.char.name} 欢愉技(无敌): {total:.0f}')
    return True  # 信号：已完全处理，跳过普通伤害


# ── 昔涟专属处理器 ──

def _xilian_trace1_speed_pen(u, state, **kw):
    """行迹1·三相的因果: SPD≥180→全队伤害+20%；每超1点→冰抗穿透+2%(上限60点)
    v5.7: 条件判定改有效面板（含战斗内SPD buff, 此前静态面板）"""
    if u.char.id != 'xilian':
        return
    from engine.core.combat_sim import _build_effective_stats
    spd = _build_effective_stats(u, state).SPD
    if spd < 180:
        return
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.DMG_BONUS_ALL += 0.20
    state.log.append(f'  三相的因果: SPD={spd:.0f}≥180→全队伤害+20%')
    excess = min(spd - 180, 60)
    if excess > 0:
        pen = excess * 0.02
        u.base_stats.RES_PEN['冰'] += pen
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.RES_PEN['冰'] += pen
        state.log.append(f'  三相的因果: 超额{excess:.0f}点→冰抗穿透+{pen*100:.0f}%')


def _xilian_trace2_memsprite_future(u, state, summoner=None, ms_unit=None, **kw):
    """行迹2·记忆的净子: 队友的忆灵被召唤时获得【未来】(忆灵持有的不被消耗)"""
    if not ms_unit or summoner is None:
        return
    if summoner.char.id != 'xilian':
        summoner.has_future = True
        ms_unit.has_future = True  # 忆灵持有的未来不会被消耗(标记位)
        state.log.append(f'  记忆的净子: {summoner.char.name}忆灵被召唤→获得【未来】')


def _xilian_trace3_start_zhuiyi(u, state, **kw):
    """行迹3·岁月的旅人: 队伍中1/2/3+名黄金裔或记忆角色→开局+2/3/6追忆"""
    if u.char.id != 'xilian':
        return
    from engine.core.character_utils import count_gold_or_memory
    count = count_gold_or_memory(state.units, exclude_id='xilian')
    bonus = {1: 2, 2: 3, 3: 6}.get(count, 0) if count >= 1 else 0
    if bonus > 0:
        u.zhuiyi = min(27, u.zhuiyi + bonus)
        state.log.append(f'  岁月的旅人: {count}名黄金裔/记忆→追忆+{bonus} ({u.zhuiyi:.0f}/27)')


# ── 阿格莱雅专属处理器 ──

def _aglaea_trace1_start_energy(u, state, **kw):
    """行迹1·飞驰之阳: 战斗开始时若能量不足50%恢复至50%"""
    if u.char.id != 'aglaea':
        return
    max_e = u.char.max_energy or 0
    if max_e > 0 and u.current_energy < max_e * 0.50:
        u.current_energy = max_e * 0.50
        state.log.append(f'  飞驰之阳: 能量恢复至50% ({u.current_energy:.0f}/{max_e})')


# ── 万敌专属处理器 ──

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


# ── 阿格莱雅/万敌 星魂 ──

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


# ── 开拓者·记忆专属处理器 ──

def _tbr_trace2_scepter(u, state, **kw):
    """行迹2·追念之权杖: 战斗开始时行动提前30%"""
    if u.char.id != 'trailblazer_remembrance':
        return
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu is u and i in navs:
            from engine.core.combat_sim import _effective_spd
            navs[i] = max(0, navs[i] - (10000 / _effective_spd(u, state)) * 0.30)
            state.log.append('  追念之权杖: 行动提前30%')
            break


def _trace_fengjin_t1(u, state, **kw):
    """风堇行迹1·暴风停歇: SPD>200→HP上限+20%（面板改动, 小伊卡召唤时按继承比例自动生效）"""
    if u.char.id == 'fengjin' and u.base_stats.SPD > 200:
        u.base_stats.HP *= 1.20
        state.log.append(f'  行迹1·暴风停歇: HP上限+20% (SPD={u.base_stats.SPD:.0f})')


def _trace_fengjin_t2(u, state, **kw):
    """风堇行迹2·阴云莞尔: 风堇+小伊卡CR+100%（风堇.txt; 小伊卡召唤先于行迹,
    copy 不继承——需显式给小伊卡）"""
    if u.char.id == 'fengjin':
        u.base_stats.CRIT_RATE += 1.00
        if u.memsprite_unit:
            u.memsprite_unit.base_stats.CRIT_RATE += 1.00
        state.log.append('  行迹2·阴云莞尔: 风堇+小伊卡CR+100%')


def _trace_fengjin_t3(u, state, **kw):
    """风堇行迹3·雷雨轻柔: EFFECT_RES+50%（战技/终结技净化由引擎内联处理）"""
    if u.char.id == 'fengjin':
        u.base_stats.EFFECT_RES += 0.50
        state.log.append('  行迹3·雷雨轻柔: 效果抵抗+50%')


# ── v5.3 开拓者·同谐 ──

def _hook_owner(state, char_id, fallback):
    """Resolve the unit that owns a broadcast hook.

    ``trigger_all`` keeps ``u`` as the event subject for compatibility, while
    ``char_id`` identifies the effect owner.  Broadcast handlers must use the
    owner when mutating energy, buffs, or owner-local state.
    """
    if char_id:
        owner = next((unit for unit in getattr(state, 'units', [])
                      if getattr(getattr(unit, 'char', None), 'id', None) == char_id), None)
        if owner is not None:
            return owner
    return fallback


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
    from engine.core.combat_sim import _gain_energy
    _gain_energy(owner, 8.0, state=state)
    owner.extra['tbr_e2_used'] = True
    state.log.append(f'  开拓者·记忆E2: 忆灵行动→+8能量 ({owner.current_energy:.0f})')


def _eid_tbr_e2_reset(u, state, **kw):
    """v5.7 E2 重置: 开拓者回合开始时重置可触发次数"""
    if u.char.id == 'trailblazer_remembrance':
        u.extra['tbr_e2_used'] = False

def _trace_tbh_t2_first_bounce(u, state, **kw):
    """行迹2·随波逐流: 战技第一次伤害削韧+100%（弹射首跳×2, 引擎消费）"""
    if u.char.id != 'trailblazer_harmony' or kw.get('skill_key') != 'skill':
        return
    u.extra['tbh_bounce_first_double'] = True


def _trace_tbh_t3_break_delay(u, state, **kw):
    """行迹3·剧院之帽: 我方造成弱点击破后敌方行动额外延后30%"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'trailblazer_harmony':
        return
    t = kw.get('enemy')
    if t:
        t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 3000.0
        state.log.append(f'  行迹3·剧院之帽: {t.name or t.id}行动延后30%')


def _trace_tbh_talent_energy(u, state, **kw):
    """天赋·全屏段的高空踏歌: 敌方弱点被击破时恢复10能量（满级）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'trailblazer_harmony' or not owner.is_alive:
        return
    from engine.core.combat_sim import _gain_energy
    gained = _gain_energy(owner, 10, state=state)
    state.log.append(f'  天赋·高空踏歌: 击破回能+{gained:.0f}')


def _eid_tbh_e1(u, state, **kw):
    """开拓者·同谐E1: 施放首次战技后立即回复1点战技点"""
    if u.char.id != 'trailblazer_harmony' or u.extra.get('tbh_e1_used'):
        return
    u.extra['tbh_e1_used'] = True
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  E1: 首次战技回1战技点')


def _eid_tbh_e2(u, state, **kw):
    """开拓者·同谐E2: 战斗开始时能量恢复效率+25%，持续3回合"""
    if u.char.id != 'trailblazer_harmony':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='trailblazer_harmony',
                             attributes={'ENERGY_REGEN': 25.0},
                             remaining_turns=3,
                             source_name='开拓者·同谐E2', param_id='tbh_e2_energy'))
    state.log.append('  E2: 能量恢复效率+25% (3回合)')


def _eid_tbh_e4(u, state, **kw):
    """开拓者·同谐E4: 在场时除自身外队友击破特攻 += 开拓者15%击破特攻（光环, 阵亡失效）
    v5.7: 按当前有效面板（含伴舞/光锥等战斗内BE buff）, 回合开始刷新先回退旧值"""
    if u.char.id != 'trailblazer_harmony' or not u.is_alive:
        return
    old = u.extra.get('tbh_e4_bonus', 0.0)
    if old:
        for eu in state.units:
            if eu is not u:
                eu.base_stats.BREAK_EFFECT = max(0.0, eu.base_stats.BREAK_EFFECT - old)
    from engine.core.combat_sim import _build_effective_stats
    bonus = _build_effective_stats(u, state).BREAK_EFFECT * 0.15
    for eu in state.units:
        if eu is not u and eu.is_alive:
            eu.base_stats.BREAK_EFFECT += bonus
    u.extra['tbh_e4_bonus'] = bonus
    state.log.append(f'  E4: 队友击破特攻+{bonus*100:.1f}% (开拓者BE×15%)')


def _eid_tbh_e4_death(u, state, **kw):
    """开拓者·同谐E4 光环失效: 持有者阵亡 → 队友击破特攻回退"""
    if u.char.id != 'trailblazer_harmony':
        return
    bonus = u.extra.get('tbh_e4_bonus', 0.0)
    if bonus:
        for eu in state.units:
            if eu is not u:
                eu.base_stats.BREAK_EFFECT = max(0.0, eu.base_stats.BREAK_EFFECT - bonus)
        state.log.append(f'  E4 光环失效: 队友击破特攻回退{bonus*100:.1f}%')


# ── v5.3 忘归人 ──

def _fugue_cloudfire_apply(u, state, **kw):
    """忘归人天赋: 敌方额外40%韧性上限的【云火昭】（击破后仍可削, 削至0二次击破）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    for e in state.enemies:
        if e.extra_toughness_max == 0 and e.max_toughness > 0:
            e.extra_toughness_max = e.max_toughness * 0.4
            e.extra_toughness = e.extra_toughness_max
            state.log.append(f'  云火昭: {e.name or e.id} 额外韧性+{e.extra_toughness_max:.0f}')


def _fugue_cloudfire_death(u, state, **kw):
    """忘归人阵亡 → 云火昭失效（光环语义）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue':
        return
    for e in state.enemies:
        e.extra_toughness_max = 0.0
        e.extra_toughness = 0.0
    state.log.append('  云火昭失效: 忘归人阵亡')


def _fugue_foxian_def_down(u, state, **kw):
    """忘归人天赋: 狐祈者攻击→100%基础概率目标DEF-18% 2回合
    v5.6: 接入统一 EHR 检定（enemy.effect_res 默认0=必中）"""
    if not u.extra.get('_foxian'):
        return
    t = kw.get('target')
    if t is None or t.HP <= 0:
        return
    from engine.core.combat_sim import _roll_effect_hit
    if not _roll_effect_hit(u, state, t, '防御降低', base_chance=1.0):
        return
    from engine.models.enemy import EnemyStatus
    t.add_status(EnemyStatus(id='fugue_def_down', name='防御降低', category='debuff',
                             source='fugue', remaining_turns=2,
                             attributes={'def_reduction': 0.18}))
    state.log.append(f'  狐祈: {t.name or t.id} DEF-18% (2回合)')


def _fugue_trace1_break_delay(u, state, **kw):
    """行迹1·青丘重光: 我方造成弱点击破后敌方行动额外延后15%"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    t = kw.get('enemy')
    if t:
        t.extra['av_delayed'] = t.extra.get('av_delayed', 0.0) + 1500.0
        state.log.append(f'  行迹1·青丘重光: {t.name or t.id}行动延后15%')


def _fugue_trace2_team_be(u, state, **kw):
    """行迹2·玑星太素: 敌方弱点被击破→除自身外队友BE+6%（自身BE≥220%→+18%）, 2回合最多2层"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    bonus = 0.18 if owner.base_stats.BREAK_EFFECT >= 2.20 else 0.06
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu is owner or not eu.is_alive:
            continue
        layers = [b for b in eu.buffs if getattr(b, 'param_id', '') == 'fugue_trace2_be']
        while len(layers) >= 2:
            eu.buffs.remove(layers.pop(0))
        eu.buffs.append(TimedBuff(source_id='fugue',
                                  attributes={'BREAK_EFFECT': bonus * 100.0},
                                  remaining_turns=2, source_name='行迹·玑星太素',
                                  param_id='fugue_trace2_be'))
    state.log.append(f'  行迹2·玑星太素: 队友击破特攻+{bonus*100:.0f}% (2回合)')


def _fugue_trace3_self_be(u, state, **kw):
    """行迹3·涂山玄设: 自身击破特攻+30%"""
    if u.char.id != 'fugue':
        return
    u.base_stats.BREAK_EFFECT += 0.30
    state.log.append('  行迹3·涂山玄设: 击破特攻+30%')


def _fugue_trace3_first_sp(u, state, **kw):
    """行迹3·涂山玄设: 本场首次战技后立即恢复1点战技点"""
    if u.char.id != 'fugue' or u.extra.get('fugue_t3_sp_used'):
        return
    u.extra['fugue_t3_sp_used'] = True
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  行迹3: 首次战技回1战技点')


def _eid_fugue_e2_energy(u, state, **kw):
    """忘归人E2: 敌方弱点被击破时忘归人恢复3点能量"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'fugue' or not owner.is_alive:
        return
    from engine.core.combat_sim import _gain_energy
    gained = _gain_energy(owner, 3, state=state)
    state.log.append(f'  E2: 击破回能+{gained:.0f}')


def _eid_fugue_e2_ult(u, state, **kw):
    """忘归人E2: 施放终结技后我方全体行动提前24%"""
    if u.char.id != 'fugue':
        return
    from engine.core.combat_sim import _effective_spd, AV_PER_TURN
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu.is_alive and i in navs:
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * 0.24)
    state.log.append('  E2: 终结技后全队行动提前24%')


# ── v5.3 灵砂 ──

def _trace_lingsha_t2_be_to_atk_heal(u, state, **kw):
    """行迹2·朱燎: 攻击力/治疗量提高 = 击破特攻的25%/10%（上限50%/20%）"""
    if u.char.id != 'lingsha':
        return
    be = u.base_stats.BREAK_EFFECT
    atk_bonus = min(be * 0.25, 0.50)
    heal_bonus = min(be * 0.10, 0.20)
    u.base_stats.ATK += u.base_stats._base_ATK * atk_bonus
    u.base_stats.HEAL_BONUS += heal_bonus
    state.log.append(f'  行迹2·朱燎: 攻击力+{atk_bonus*100:.1f}%, 治疗量+{heal_bonus*100:.1f}%')


def _trace_lingsha_t3_pursuit(u, state, **kw):
    """行迹3·遗爇: 浮元在场+队友受伤/耗血且队内有HP%≤60%角色→浮元立即追击
    （不消耗行动次数, 2回合后可再次触发）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'lingsha' or not owner.is_alive:
        return
    if not (owner.marker and owner.marker.is_alive):
        return
    last = owner.extra.get('lingsha_t3_last_turn', -99)
    if state.turn_count - last < 2:
        return
    low = [x for x in state.units if x.is_alive and x.current_hp <= x.max_hp * 0.60]
    if not low:
        return
    owner.extra['lingsha_t3_last_turn'] = state.turn_count
    from engine.core.combat_sim import _lingsha_fuyuan_action
    _lingsha_fuyuan_action(state, owner.marker)
    state.log.append('  行迹3·遗爇: 浮元立即追击(不耗行动次数)')


def _eid_lingsha_e1_break(u, state, **kw):
    """灵砂E1: 敌方弱点被击破时其防御力降低20%（击破期间持续, 韧性恢复时移除）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    if owner.char.id != 'lingsha' or owner.eidolon_rank < 1 or not owner.is_alive:
        return
    t = kw.get('enemy')
    if t:
        from engine.models.enemy import EnemyStatus
        t.add_status(EnemyStatus(id='lingsha_e1_def_down', name='防御降低', category='debuff',
                                 remaining_turns=-1, attributes={'def_reduction': 0.2}))
        state.log.append(f'  E1: {t.name or t.id} DEF-20% (击破期间)')


def _eid_lingsha_e2_ult(u, state, **kw):
    """灵砂E2: 施放终结技时全队击破特攻+40%，持续3回合"""
    if u.char.id != 'lingsha':
        return
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='lingsha',
                                      attributes={'BREAK_EFFECT': 40.0},
                                      remaining_turns=3, source_name='灵砂E2',
                                      param_id='lingsha_e2_be'))
    state.log.append('  E2: 全队击破特攻+40% (3回合)')


# ── v5.3 流萤 ──

def _firefly_refresh_dr(u, state):
    """流萤天赋·茧式源火中枢: HP越低减伤越多（最多40%, HP≤20%满）; 完全燃烧维持最大"""
    # v6.5.1: on_hp_loss 广播 u 可能是忆灵(MemSpriteUnit.char 无 id) → 先过滤
    from engine.core.combat_sim import SimUnit
    if not isinstance(u, SimUnit) or u.char.id != 'firefly' or not u.is_alive:
        return
    if u.extra.get('combustion'):
        dr = 0.40
    else:
        ratio = 1.0 - u.current_hp / max(u.max_hp, 1)
        dr = 0.40 * min(ratio / 0.8, 1.0)
    cur = u.extra.get('firefly_dr_current', 0.0)
    u.base_stats.DMG_REDUCTION += dr - cur
    u.extra['firefly_dr_current'] = dr
    state.log.append(f'  天赋·源火中枢: 减伤{dr*100:.0f}% (HP {u.current_hp:.0f}/{u.max_hp:.0f})')


def _trace_firefly_dr_hp_loss(u, state, **kw):
    """减伤刷新（受击/耗血后）"""
    _firefly_refresh_dr(u, state)


def _trace_firefly_dr_turn(u, state, **kw):
    """减伤刷新（回合开始）"""
    _firefly_refresh_dr(u, state)


def _trace_firefly_talent_start(u, state, **kw):
    """天赋: 战斗开始时能量不足50%则恢复至50%"""
    if u.char.id != 'firefly':
        return
    if u.current_energy < u.char.max_energy * 0.5:
        u.current_energy = u.char.max_energy * 0.5
        state.log.append('  天赋: 战斗开始能量恢复至50%')


# ════════════ v6.7 绯英/火花/大丽花 TRACE handlers ════════════

def _trace_evanescia_energy_convert(u, state, amount=0, **kw):
    """绯英天赋方向1: 获得能量→同步获得等值好活当赏（单次≤100）;
    累计240能量→狐狸老师FUA。锁内(好活→能量转化中)不重复转化。"""
    if u.char.id != 'evanescia' or state.extra.get('_eva_convert_lock'):
        return
    if not state.extra.get('_elation'):
        return
    elation = state.extra['_elation']
    amt = min(float(amount), 100.0)
    if amt > 0:
        elation.grant_good_show(state, 'evanescia', amt, duration=2,
                                source='evanescia_talent')
    # 240累计（FUA触发点, 单次获得最多记240）
    bank = u.extra.get('evanescia_energy_bank', 0.0) + float(amount)
    if bank >= 240.0:
        u.extra['evanescia_energy_bank'] = bank - 240.0
        from engine.core.combat_sim import _evanescia_fox_teacher_fua
        _evanescia_fox_teacher_fua(state, u)
    else:
        u.extra['evanescia_energy_bank'] = bank


def _trace_evanescia_trace3_cr(u, state, **kw):
    """绯英行迹3·瞰众乐: 暴击率+30%（永久）; 弹射次数/好活转移由引擎内联"""
    if u.char.id != 'evanescia':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'CRIT_RATE': 30.0},
                             remaining_turns=-1, source_name='行迹·瞰众乐'))
    state.log.append('  绯英行迹3: 暴击率+30%(永久)')


def _trace_evanescia_talent_elation(u, state, **kw):
    """绯英天赋基础: 获得等同于暴击伤害20%的欢愉度（v6.7b 补, txt 天赋）"""
    if u.char.id != 'evanescia':
        return
    bonus = u.base_stats.CRIT_DMG * 0.20
    u.base_stats.ELATION_LEVEL += bonus
    state.log.append(f'  绯英天赋: 欢愉度+暴伤×20%({bonus*100:.0f}%)')


def _dahlia_trace1_be_bonus(u, state) -> float:
    """行迹1 数值: txt「提高数值等同于24%大丽花的击破特攻+50%」
    = 大丽花BE×24% + 50%（返回百分比原始数值, TimedBuff 口径）。"""
    from engine.core.combat_sim import _build_effective_stats
    be = _build_effective_stats(u, state).BREAK_EFFECT
    return be * 24.0 + 50.0


def _dahlia_trace1_apply(state, u, turns=1):
    """把行迹1 BE 转移施加给其他存活角色（开战1回合 / 受治疗护盾3回合）。"""
    from engine.core.combat_sim import TimedBuff
    bonus = _dahlia_trace1_be_bonus(u, state)
    for eu in state.units:
        if eu.is_alive and eu.char.id != 'the_dahlia':
            eu.buffs.append(TimedBuff(source_id='the_dahlia',
                                      attributes={'BREAK_EFFECT': bonus},
                                      remaining_turns=turns, source_name='又一场葬礼'))
    return bonus


def _trace_dahlia_trace1_open(u, state, **kw):
    """大丽花行迹1·又一场葬礼: 开战其他角色击破特攻+(24%×大丽花BE+50%) 1回合"""
    if u.char.id != 'the_dahlia':
        return
    bonus = _dahlia_trace1_apply(state, u, turns=1)
    state.log.append(f'  大丽花行迹1: 队友击破特攻+{bonus:.1f}%(1回合)')


def _trace_dahlia_trace1_reapply(u, state, targets=None, **kw):
    """大丽花行迹1: 受到队友提供的治疗/护盾→再次触发BE转移, 持续3回合, 单回合1次"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None or dahlia.extra.get('dahlia_trace1_used'):
        return
    if u is not None and u.char.id == 'the_dahlia':
        return  # 仅队友提供的治疗/护盾
    targets = list(targets or [])
    if dahlia not in targets:
        return
    bonus = _dahlia_trace1_apply(state, dahlia, turns=3)
    dahlia.extra['dahlia_trace1_used'] = True
    state.log.append(f'  大丽花行迹1: 受治疗/护盾→队友击破特攻+{bonus:.1f}%(3回合)')


def _trace_dahlia_trace3_implant(u, state, element='', target=None, **kw):
    """大丽花行迹3·弃旧恋新: 我方为敌添加弱点→大丽花速度+30% 2回合;
    火属性添加弱点→+20固定削韧+回10%能量上限"""
    dahlia = next((x for x in state.units
                   if x.char.id == 'the_dahlia' and x.is_alive), None)
    if dahlia is None:
        return
    from engine.core.combat_sim import TimedBuff
    dahlia.buffs.append(TimedBuff(source_id='the_dahlia', attributes={'SPD_PERCENT': 30.0},
                                  remaining_turns=2, source_name='弃旧恋新'))
    # v6.7b: txt 条件=「我方火属性角色施放攻击期间添加过弱点」——火属性角色(非元素)添加弱点
    if target is not None and getattr(u, 'char', None) is not None \
            and u.char.element == '火':
        if target.toughness > 0 and not target.is_broken:
            from engine.core.combat_sim import _flat_toughness_with_break
            _flat_toughness_with_break(state, dahlia, target, 20.0, '火', 'talent')
        # 回10%能量上限的能量, 最多通过此效果恢复至能量上限的50%（v6.7b 补上限）
        cap_half = dahlia.char.max_energy * 0.5
        if dahlia.current_energy < cap_half:
            from engine.core.combat_sim import _gain_energy
            gain = min(dahlia.char.max_energy * 0.10, cap_half - dahlia.current_energy)
            _gain_energy(dahlia, gain, state=state)
        state.log.append(f'  大丽花行迹3: 火属性角色添弱点+20固定削韧 + 回10%能量上限(≤50%)')


def _trace_dahlia_field_tick(u, state, **kw):
    """大丽花结界回合递减（仅大丽花自身回合开始）: 结界-1 + 重置FUA/行迹1回合标记"""
    if u.char.id != 'the_dahlia':
        return
    u.extra['dahlia_fua_used'] = False
    u.extra['dahlia_trace1_used'] = False
    turns = state.extra.get('dahlia_field_turns', 0)
    if turns > 0:
        state.extra['dahlia_field_turns'] = turns - 1
        if turns - 1 <= 0:
            # 移除全队结界buff
            for eu in state.units:
                eu.buffs = [b for b in eu.buffs
                            if getattr(b, 'param_id', '') != 'dahlia_field_buff']
            state.log.append('  大丽花结界: 结束')


# ════════════ v6.7 EIDOLON handlers ════════════

def _eid_evanescia_e1(u, state, **kw):
    """绯英E1: 全属性抗性穿透+20%（永久）; 狐狸老师额外欢愉技在 FUA 内联"""
    if u.char.id != 'evanescia':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'RES_PEN_ALL': 20.0},
                             remaining_turns=-1, source_name='绯英E1'))


def _eid_evanescia_e2(u, state, **kw):
    """绯英E2: 暴击伤害+36%（永久）; 好活获得×1.5/×2 在行迹2/3内联"""
    if u.char.id != 'evanescia':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'CRIT_DMG': 36.0},
                             remaining_turns=-1, source_name='绯英E2'))


def _eid_evanescia_e4(u, state, **kw):
    """绯英E4: 造成的伤害无视15%防御力"""
    if u.char.id != 'evanescia':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='evanescia', attributes={'DEF_PEN': 15.0},
                             remaining_turns=-1, source_name='绯英E4'))


def _eid_sparxie_e6(u, state, **kw):
    """火花E6: 全属性抗性穿透+20%（永久）; 欢愉技额外段数在 _bounce_hits 内联"""
    if u.char.id != 'sparxie':
        return
    from engine.core.combat_sim import TimedBuff
    u.buffs.append(TimedBuff(source_id='sparxie', attributes={'RES_PEN_ALL': 20.0},
                             remaining_turns=-1, source_name='火花E6'))


def _eid_dahlia_e2(u, state, **kw):
    """大丽花E2: 在场全敌全属性抗性-20% + 敌方目标入场即陷【败谢】3回合（初始波,
    重生波由 _respawn_wave 处理）。v6.7b: 败谢补上弱点部分——复用 _apply_dahlia_baisie
    （防-18%+共舞者属性弱点, 与终结技败谢同状态 id 覆盖刷新）。"""
    if u.char.id != 'the_dahlia':
        return
    from engine.core.combat_sim import _apply_dahlia_baisie
    for e in state.enemies:
        for elem in list(e.element_res.keys()):
            e.element_res[elem] = e.get_res(elem) - 0.20
        _apply_dahlia_baisie(u, state, e, turns=3)
    state.log.append('  大丽花E2: 全敌全属性抗性-20% + 败谢(3回合)')


def _eid_dahlia_e6(u, state, **kw):
    """大丽花E6: 共舞者击破特攻+150%（永久）; 行动提前在 FUA 内联"""
    if u.char.id != 'the_dahlia':
        return
    from engine.core.combat_sim import TimedBuff
    for cid in state.extra.get('dahlia_dancers', []):
        partner = next((x for x in state.units if x.char.id == cid), None)
        if partner:
            # v6.7b: TimedBuff 百分比原始数值口径（150=+150%, 此前 1.50 被 /100 成 +1.5%）
            partner.buffs.append(TimedBuff(source_id='the_dahlia',
                                           attributes={'BREAK_EFFECT': 150.0},
                                           remaining_turns=-1, source_name='大丽花E6'))
    state.log.append('  大丽花E6: 共舞者击破特攻+150%')


# ════════════ v6.7 姬子·启行 TRACE/EIDOLON handlers ════════════

def _trace_hn_protocol(u, state, **kw):
    """姬子·启行天赋·同行协议（on_enter_battle）:
    裁决（开拓者/丹恒/星期日）: 姬子伤害+100%+终结技额外+100%+队友终结技计数→免费助战技;
    歼破（三月七/长夜月/瓦尔特/姬子）: 全队暴伤+100%+每击中充能→免费助战技; 两协议可共存"""
    if u.char.id != 'himeko_nova':
        return
    from engine.core.combat_sim import (HIMEKO_NOVA_VERDICT, HIMEKO_NOVA_CHARGE,
                                        TimedBuff)
    ids = {x.char.id for x in state.units}
    if ids & HIMEKO_NOVA_VERDICT:
        state.extra['hn_verdict'] = True
        u.buffs.append(TimedBuff(source_id='himeko_nova',
                                 attributes={'DMG_BONUS_ALL': 100.0,
                                             'DMG_BONUS_ULTIMATE': 100.0},
                                 remaining_turns=-1, source_name='同行协议·裁决'))
        state.log.append('  同行协议·裁决: 姬子伤害+100%+终结技额外+100%')
    if ids & HIMEKO_NOVA_CHARGE:
        state.extra['hn_charge'] = 0
        state.extra['hn_charge_mode'] = True
        # v6.7b: txt「战技造成的暴击伤害额外提高100%」——标记由伤害循环消费
        state.extra['hn_charge_skill_cd'] = True
        for eu in state.units:
            if eu.is_alive:
                eu.buffs.append(TimedBuff(source_id='himeko_nova',
                                          attributes={'CRIT_DMG': 100.0},
                                          remaining_turns=-1, source_name='同行协议·歼破'))
        state.log.append('  同行协议·歼破: 全队暴伤+100% + 战技暴伤额外+100%')


def _trace_hn_flag_regen(u, state, **kw):
    """姬子·启行: 每回合开始——领航旗语期间恢复1次助战技次数; 行迹1(次数=上限时回5能量)"""
    himeko = next((x for x in state.units
                   if x.char.id == 'himeko_nova' and x.is_alive), None)
    if himeko is None:
        return
    from engine.core.combat_sim import _hn_support_cap, _gain_energy
    cap = _hn_support_cap(himeko)
    # 领航旗语（战技buff在身）: 每回合开始恢复1次
    if any(getattr(b, 'param_id', '') == 'himeko_nova_flag' for b in himeko.buffs):
        state.extra['hn_support_uses'] = min(cap, state.extra.get('hn_support_uses', 0) + 1)
        state.log.append('  领航旗语: 助战技次数+1')
    # 行迹1: 回合开始时若使用次数=上限→回5能量
    if state.extra.get('hn_support_uses', 0) >= cap:
        _gain_energy(himeko, 5.0, state=state)
        state.log.append('  姬子行迹1: 助战技次数已满→回5能量')





# ════════════ v6.9 批1 TRACE handlers（星期日/瓦尔特/阮·梅）════════════

def _trace_sunday_trace2(u, state, **kw):
    """星期日行迹2·崇高拂尘: 战斗开始恢复25能量"""
    if u.char.id != 'sunday':
        return
    from engine.core.combat_sim import _gain_energy
    _gain_energy(u, 25.0, state=state)
    state.log.append('  星期日行迹2: 开局回25能量')


def _trace_sunday_tick(u, state, **kw):
    """星期日回合开始: 蒙福者倒计时+E4回8能量"""
    if u.char.id != 'sunday':
        return
    from engine.core.combat_sim import _sunday_tick
    _sunday_tick(state, u)


def _trace_welt_trace1(u, state, **kw):
    """瓦尔特行迹1·惩戒: 战斗开始恢复30能量"""
    if u.char.id != 'welt':
        return
    from engine.core.combat_sim import _gain_energy
    _gain_energy(u, 30.0, state=state)
    state.log.append('  瓦尔特行迹1: 开局回30能量')


def _trace_ruanmei_trace1(u, state, **kw):
    """阮·梅行迹1·物体呼吸中: 全队击破特攻+20%"""
    if u.char.id != 'ruan_mei':
        return
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                      attributes={'BREAK_EFFECT': 20.0},
                                      remaining_turns=-1, source_name='阮·梅行迹1'))
            if eu is not u:
                eu.buffs.append(TimedBuff(source_id='ruan_mei',
                                          attributes={'SPD_PERCENT': 10.0},
                                          remaining_turns=-1, source_name='阮·梅天赋·分型的螺旋'))

    state.log.append('  阮·梅行迹1: 全队击破特攻+20%')


def _trace_ruanmei_tick(u, state, **kw):
    """阮·梅回合开始: 结界-1+行迹2回5能量"""
    if u.char.id != 'ruan_mei':
        return
    from engine.core.combat_sim import _ruanmei_tick
    _ruanmei_tick(state, u)


def _trace_ruanmei_break(u, state, enemy=None, target=None, **kw):
    """阮·梅天赋: 我方击破弱点→对目标120%冰击破伤害(E6+200%)
    on_any_weakness_break 事件传参为 enemy; 兼容 target 别名"""
    from engine.core.combat_sim import _ruanmei_break_damage_v3
    t = enemy if enemy is not None else target
    if t is not None:
        _ruanmei_break_damage_v3(state, None, t)




# ════════════ v6.9 批2 TRACE handlers（知更鸟/不死途）════════════

def _trace_robin_trace2(u, state, **kw):
    """知更鸟行迹2·华彩花腔: 战斗开始自身行动提前25%"""
    if u.char.id != 'robin':
        return
    from engine.core.combat_sim import TimedBuff, _set_av
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='robin', attributes={'CRIT_DMG': 20.0},
                                      remaining_turns=-1, source_name='知更鸟天赋·华彩花腔'))
    state.log.append('  知更鸟天赋: 全队暴伤+20%')

    navs = state.extra.get('navs', {})
    unit_index = next((i for i, unit in enumerate(state.units) if unit is u), None)
    if unit_index is not None and unit_index in navs:
        remaining_av = max(0.0, navs[unit_index] - state.current_av)
        _set_av(state, navs, unit_index, state.current_av + remaining_av * 0.75)
    else:
        u.extra['initial_action_advance_ratio'] = \
            u.extra.get('initial_action_advance_ratio', 0.0) + 0.25
    state.log.append('  知更鸟行迹2: 开局行动提前25%')


def _trace_busitu_trace3(u, state, **kw):
    """不死途行迹3·头狼: 全队暴伤+40%+全队FUA暴伤+80%"""
    if u.char.id != 'busitu':
        return
    from engine.core.combat_sim import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='busitu',
                                      attributes={'CRIT_DMG': 40.0,
                                                  'CRIT_DMG_ATK_follow_up': 80.0},
                                      remaining_turns=-1, source_name='不死途行迹3'))
    state.log.append('  不死途行迹3: 全队暴伤+40%+追加攻击暴伤80%')


def _trace_busitu_e1(u, state, **kw):
    """不死途E1: 在场全敌受伤+24%(≤50%HP时36%)"""
    if u.char.id != 'busitu':
        return
    for e in state.enemies:
        e.extra['busitu_e1_vuln'] = 0.24
        e.extra['busitu_e1_half_vuln'] = 0.36
        e.extra['busitu_e1_max_hp'] = e.HP
    state.log.append('  不死途E1: 全敌受伤+24%(≤50%HP时36%)')


def _trace_qianye_trace1(u, state, **kw):
    """千冶·刃行迹1: 开战时能量不足75%则立即补至75%。"""
    if u.char.id != 'qianye':
        return
    target = (u.char.max_energy or 0) * 0.75
    if u.current_energy < target:
        u.current_energy = target
        state.log.append('  千冶·刃行迹1: 开局能量补至75%')




# ════════════ v6.10 黄泉 TRACE handlers ════════════

def _trace_acheron_trace1(u, state, **kw):
    """行迹1·赤鬼: 开局5残梦+集真赤5层(随机1敌); 溢出→四相断我"""
    if u.char.id != 'acheron':
        return
    from engine.core.combat_sim import (_acheron_gain_dream, _acheron_apply_jizhen)
    import random
    alive = state.alive_enemies() or state.enemies
    _acheron_gain_dream(state, u, 5)
    if alive:
        t = random.choice(alive)
        _acheron_apply_jizhen(state, u, t, 5)
    state.log.append('  黄泉行迹1: 开局5残梦+集真赤5层')


def _trace_acheron_tick(u, state, **kw):
    """黄泉回合开始: E2 +1残梦+集真赤"""
    if u.char.id != 'acheron':
        return
    from engine.core.combat_sim import _acheron_tick
    _acheron_tick(state, u)

def _eid_hn_e6(u, state, **kw):
    """姬子·启行E6: 火属性抗性穿透+20%（永久）; 源能上限/光束额外源能/脉冲额外段 内联
    v6.7b: RES_PEN_FIRE 会落成英文键 'FIRE' 永不消费——直接写中文键 RES_PEN['火']。"""
    if u.char.id != 'himeko_nova':
        return
    u.base_stats.RES_PEN['火'] = u.base_stats.RES_PEN.get('火', 0.0) + 0.20
    state.log.append('  姬子E6: 火属性抗性穿透+20%')


def _trace_firefly_talent_cleanse(u, state, **kw):
    """天赋: 能量恢复至上限时解除自身所有负面效果"""
    if u.char.id != 'firefly':
        return
    if u.current_energy >= u.char.max_energy and u.statuses:
        u.statuses.clear()
        state.log.append('  天赋: 能量满解除自身所有负面')


def _trace_firefly_t1_pull(u, state, **kw):
    """行迹1·偏时迸发: 强化攻击使目标击破时倒计时行动延后10%（每燃烧期最多3次）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    actor = kw.get('actor')
    if actor is not None and actor is not owner:
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    if owner.char.id != 'firefly' or not owner.extra.get('combustion'):
        return
    if owner.extra.get('ff_trace1_pull', 0) >= 3:
        return
    m = owner.marker
    if not (m and m.is_alive):
        return
    m.extra['delay_pending'] = m.extra.get('delay_pending', 0.0) + 10000.0 / 70.0 * 0.10
    owner.extra['ff_trace1_pull'] = owner.extra.get('ff_trace1_pull', 0) + 1
    state.log.append('  行迹1: 完全燃烧倒计时行动延后10%')


def _trace_firefly_t3_atk_to_be(u, state, **kw):
    """行迹3·过载核心: 攻击力>1800时每超10点击破特攻+0.8%"""
    if u.char.id != 'firefly':
        return
    if u.base_stats.ATK > 1800:
        bonus = (u.base_stats.ATK - 1800) / 10.0 * 0.008
        u.base_stats.BREAK_EFFECT += bonus
        state.log.append(f'  行迹3·过载核心: 击破特攻+{bonus*100:.1f}%')


def _eid_firefly_e2_kill(u, state, **kw):
    """流萤E2: 完全燃烧下强化攻击击杀→萨姆额外回合（每回合1次）"""
    if u.char.id != 'firefly' or not u.extra.get('combustion') or u.extra.get('ff_e2_used'):
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    u.extra['ff_e2_used'] = True
    state.extra.setdefault('extra_turns', []).append((u, 'extra'))
    state.log.append('  E2: 击杀→萨姆额外回合')


def _eid_firefly_e2_break(u, state, **kw):
    """流萤E2: 完全燃烧下强化攻击使目标击破→萨姆额外回合（每回合1次）"""
    owner = _hook_owner(state, kw.get('char_id'), u)
    actor = kw.get('actor')
    if actor is not None and actor is not owner:
        return
    if kw.get('skill_key') not in (None, 'basic_attack_enhanced', 'skill_enhanced'):
        return
    if owner.char.id != 'firefly' or not owner.extra.get('combustion') or owner.extra.get('ff_e2_used'):
        return
    owner.extra['ff_e2_used'] = True
    state.extra.setdefault('extra_turns', []).append((owner, 'extra'))
    state.log.append('  E2: 击破→萨姆额外回合')


def _eid_firefly_e2_reset(u, state, **kw):
    """流萤E2: 萨姆回合开始时重置可触发次数"""
    if u.char.id == 'firefly':
        u.extra['ff_e2_used'] = False


# ── 注册表 ──

TRACE_REGISTRY: dict[str, dict] = {
    # 爻光
    "yaoguang_cd_and_sp": {
        "trigger": "on_after_skill",
        "action": _trace_elation_sp_recovery,
        "condition": lambda **kw: kw.get('skill_key') == 'elation_skill' and kw['u'].char.id == 'yaoguang',
        "source_name": "行迹·暴伤增幅",
    },
    "yaoguang_goodshow_extend": {
        # 好活当赏持续+1回合 — 由 ElationSystem.grant_good_show 处理
        "trigger": None, "action": None, "source_name": "行迹·持久喝彩",
    },
    "yaoguang_spd_to_elation": {
        # SPD→欢愉度转化 — 由 ElationSystem 处理
        "trigger": None, "action": None, "source_name": "行迹·速域转化",
    },
    # 银狼
    "yinlang_invincible_hidden": {
        "trigger": None, "action": None, "source_name": "行迹·无敌玩家",
    },
    "yinlang_laugh_to_hidden": {
        "trigger": None, "action": None, "source_name": "行迹·笑点→隐藏分",
    },
    "yinlang_speed_to_elation": {
        "trigger": None, "action": None, "source_name": "行迹·速度转化",
    },
    # 开拓者·欢愉
    "trailblazer_atk_to_elation": {
        "trigger": None, "action": None, "source_name": "行迹·攻击→欢愉度",
    },
    "trailblazer_cr_and_sp": {
        "trigger": "on_after_skill",
        "action": lambda **kw: _trace_tb_cr_and_sp(kw['u'], kw['state'], skill_key=kw.get('skill_key')),
        "source_name": "行迹·暴击+回SP",
    },
    "trailblazer_goodshow_boost": {
        "trigger": None, "action": None, "source_name": "行迹·好活增强",
    },
    # 藿藿
    "huohuo_battle_start": {
        "trigger": "on_enter_battle",
        "action": lambda **kw: (
            setattr(kw['u'], 'current_energy',
                    min(kw['u'].char.max_energy, kw['u'].current_energy + 30)),
            # v6.10.6 B: 行迹2·不敢自专——战斗开始获得禳命持续2回合（TXT 藿藿.txt:57）
            _huohuo_ruming_gain_local(kw['state'], kw['u'], 2),
            kw['state'].log.append('[Init] 藿藿开局+30能量 + 禳命2回合')
        ),
        "source_name": "行迹·战斗开始",
    },
    "huohuo_control_resist_and_atk": {
        "trigger": "on_enter_battle", "action": _trace_huohuo_control_resist, "source_name": "行迹·控制抵抗+攻击",
    },
    "huohuo_energy_cycle": {
        "trigger": None, "action": None, "source_name": "行迹·回合回能(禳命治疗内联)",
    },
    # 布洛妮娅
    "bronya_basic_crit_100": {
        "trigger": "on_basic_attack",
        "action": _trace_bronya_basic_crit,
        "source_name": "行迹·号令",
    },
    "bronya_battle_start_def": {
        "trigger": "on_enter_battle",
        "action": _trace_bronya_battle_def,
        "source_name": "行迹·阵地",
    },
    "bronya_team_dmg_bonus": {
        "trigger": "on_enter_battle", "action": _trace_bronya_team_dmg, "source_name": "行迹·军势",
    },
    # v6.3.0 银狼（普通, silver_wolf）
    # v6.3.0b P1-8: 改 on_any_weakness_break（attacker-only 事件不带 enemy, 队友击破/自身击破均漏触发）
    # v6.6 批1: 缇宝/刻律德菈/丹恒·腾荒
    "phainon_trace1": {
        "trigger": "on_enter_battle",
        "action": _trace_phainon_trace1,
        "source_name": "行迹·终点（开局+1火种）",
    },
    "phainon_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_phainon_trace3,
        "source_name": "行迹·本色（进战ATK+50%）",
    },
    "hysilens_trace1": {
        "trigger": "on_enter_battle",
        "action": _trace_hysilens_trace1,
        "source_name": "行迹·剑旗（开局结界+回SP）",
    },
    "hysilens_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·泡沫（终结技DOT引爆，_use_skill内联）",
    },
    "hysilens_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_hysilens_trace3,
        "source_name": "行迹·琴弦（EHR→增伤）",
    },
    "hysilens_base": {
        "trigger": None, "action": None,
        "source_name": "基础行迹（数据面板属性）",
    },
    "anaxa_trace2": {
        "trigger": "on_enter_battle",
        "action": _trace_anaxa_trace2,
        "source_name": "行迹·留白（智识数量）",
    },
    "anaxa_trace1": {
        "trigger": "on_basic_attack",
        "action": _trace_anaxa_basic_energy,
        "source_name": "行迹·流浪的能指（普攻额外回能）",
    },
    "anaxa_trace1_turn": {
        "trigger": "on_turn_start",
        "action": _trace_anaxa_turn_energy,
        "source_name": "行迹·流浪的能指（无揭露回能）",
    },
    "anaxa_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_anaxa_trace3,
        "source_name": "行迹·嬗变（每弱点无视防御）",
    },
    "cipher_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_cipher_trace3,
        "source_name": "行迹·偷天换日（FUA暴伤+敌受伤）",
    },
    "tribbie_trace1": {
        "trigger": None,
        "action": None,
        "source_name": "行迹·城墙外的羊羔儿（FUA后增伤72%×3层, _tribbie_talent_fua 内联 TimedBuff; v6.8.1 修正: 此前挂 on_enter_battle 开局误触发且层数无消费）",
    },
    "tribbie_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_tribbie_trace3,
        "source_name": "行迹·小石子（开局回30能量）",
    },
    "cerydra_trace1": {
        "trigger": "on_enter_battle",
        "action": _trace_cerydra_trace1,
        "source_name": "行迹·来者（ATK→暴伤）",
    },
    "cerydra_trace2": {
        "trigger": "on_enter_battle",
        "action": _trace_cerydra_trace2,
        "source_name": "行迹·见者（暴击率+100%）",
    },
    "dht_trace2": {
        "trigger": "on_enter_battle",
        "action": _trace_dht_trace2,
        "source_name": "行迹·葳蕤（开局行动提前40%）",
    },
    "anaxa_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "dht_trace1": {"trigger": None, "action": None, "source_name": "行迹·神秀（同袍攻击）"},
    "dht_trace3": {"trigger": None, "action": None, "source_name": "行迹·峥嵘（龙灵行动）"},
    "dht_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "cerydra_trace3": {"trigger": None, "action": None, "source_name": "行迹·征服者（速度与回能）"},
    "cerydra_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "phainon_trace2": {"trigger": None, "action": None, "source_name": "行迹·身承炎炬万千"},
    "phainon_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "silver_wolf_trace1_gen": {
        "trigger": "on_any_weakness_break",
        "action": _trace_silver_wolf_gen,
        "source_name": "行迹·生成（缺陷+1回合, 击破植入）",
    },
    "silver_wolf_trace2_inject": {
        "trigger": "on_enter_battle",
        "action": _trace_silver_wolf_inject_start,
        "source_name": "行迹·注入（战斗开始回20能量）",
    },
    "silver_wolf_trace2_turn": {
        "trigger": "on_turn_start",
        "action": _trace_silver_wolf_inject_turn,
        "source_name": "行迹·注入（回合开始回5能量）",
    },
    "silver_wolf_trace3_annotate": {
        "trigger": "on_enter_battle",
        "action": _trace_silver_wolf_annotate,
        "source_name": "行迹·旁注（EHR→ATK）",
    },
    "silver_wolf_base_trace": {"trigger": None, "action": None, "source_name": "基础行迹（数据面板）"},
    # v6.10.3 P2-2: 补齐完整角色注册缺口（此前卫生测试白名单未覆盖）
    "cipher_trace1": {"trigger": None, "action": None, "source_name": "行迹·神行宝鞋（_cipher_record 速度档内联）"},
    "cipher_trace2": {"trigger": None, "action": None, "source_name": "行迹·三百侠盗（_cipher_record 8%内联）"},
    "cipher_base": {"trigger": None, "action": None, "source_name": "基础行迹（数据面板）"},
    "tribbie_trace2": {"trigger": None, "action": None, "source_name": "行迹·长翅膀的玻璃球！"},
    "tribbie_base": {"trigger": None, "action": None, "source_name": "基础行迹（数据面板）"},
    # 希儿（斩尽/离析效果与E1/E6同文, 在战斗引擎无条件内联; 注册表留文档）
    "seele_crit_and_defpen_vs_lowhp": {
        "trigger": None, "action": None, "source_name": "行迹·低血量暴击(引擎内联)",
    },
    "seele_lysis_butterfly_debuff": {
        "trigger": None, "action": None, "source_name": "行迹·乱蝶(引擎内联)",
    },
    "seele_ripple_action_advance": {
        "trigger": "on_basic_attack",
        "action": _trace_seele_ripple,
        "source_name": "行迹·涟漪",
    },
    # 花火
    "sparkle_basic_energy": {
        "trigger": "on_basic_attack", "action": _trace_basic_energy_bonus, "source_name": "行迹·普攻回能",
    },
    "sparkle_mystery_boost": {
        "trigger": None, "action": None, "source_name": "行迹·谜题强化(折算入终结技buff)",
    },
    "sparkle_team_cd": {
        "trigger": None, "action": None, "source_name": "行迹·夜想曲(动态面板)",
    },
    "sparkle_sp_limit": {
        "trigger": "on_enter_battle",
        "action": _trace_sparkle_sp_limit,
        "source_name": "天赋·叙述性诡计(战技点上限)",
    },
    "sparkle_turn_end": {
        "trigger": "on_turn_end",
        "action": _trace_sparkle_turn_end,
        "source_name": "终结技记录·回合结束补SP",
    },
    # 符玄
    "fuxuan_cc_resist": {
        "trigger": "on_skill", "action": _trace_fuxuan_cc_resist, "source_name": "行迹·控制抵抗",
    },
    "fuxuan_energy_regen": {
        "trigger": "on_turn_start", "action": _trace_fuxuan_energy_regen,
        "condition": lambda **kw: kw['state'].extra.get('fuxuan_field_turns', 0) > 0,
        "source_name": "行迹·能量恢复",
    },
    "fuxuan_ult_heal": {
        "trigger": "on_ultimate",
        "action": _trace_fuxuan_ult_heal,
        "source_name": "行迹·终结技回血",
    },
    # 长夜月
    "changyeyue_trace1_memory_count_cd": {
        "trigger": None, "action": None, "source_name": "行迹·记忆数量→CD",
    },
    "changyeyue_trace2_cr_and_cd": {
        "trigger": "on_after_skill",
        "action": lambda **kw: _changyeyue_trace2(kw['u'], kw['state']),
        "source_name": "行迹·天黑黑月寂寂",
    },
    "changyeyue_trace3_energy_and_yizhi": {
        "trigger": "on_after_skill",
        "action": lambda **kw: _changyeyue_trace3(kw['u'], kw['state'], kw.get('skill_key')),
        "source_name": "行迹·烛火起烛火熄",
    },
    # 遐蝶
    "xiadie_trace1_flame_stack": {
        "trigger": None, "action": None, "source_name": "行迹·西风的驻足",
    },
    "xiadie_trace2_speed": {
        "trigger": None, "action": None, "source_name": "行迹·倒置的火炬",
    },
    "xiadie_trace3_heal_convert": {
        "trigger": "on_heal",
        "action": lambda u, state, healer=None, targets=None, heal_amt=0, **kw: (
            _xiadie_heal_to_xinrui(state, targets, heal_amt)
        ),
        "source_name": "行迹·收容的暗潮",
    },
    # 昔涟
    "xilian_trace1_speed_to_pen": {
        "trigger": "on_enter_battle",
        "action": _xilian_trace1_speed_pen,
        "source_name": "行迹·三相的因果",
    },
    "xilian_trace2_memsprite_future": {
        "trigger": "on_memsprite_summon",
        "action": _xilian_trace2_memsprite_future,
        "source_name": "行迹·记忆的净子",
    },
    "xilian_trace3_start_zhuiyi": {
        "trigger": "on_enter_battle",
        "action": _xilian_trace3_start_zhuiyi,
        "source_name": "行迹·岁月的旅人",
    },
    # 阿格莱雅
    "aglaea_trace1_start_energy": {
        "trigger": "on_enter_battle",
        "action": _aglaea_trace1_start_energy,
        "source_name": "行迹·飞驰之阳",
    },
    "aglaea_trace2_spd_retain": {
        "trigger": None, "action": None, "source_name": "行迹·织运之竭",
    },
    "aglaea_trace3_sovereign_atk": {
        "trigger": None, "action": None, "source_name": "行迹·短视之惩",
    },
    # 万敌
    "mydei_trace1_blood_armor": {
        "trigger": "on_enter_battle",
        "action": _mydei_trace1_blood_armor,
        "source_name": "行迹·血祥罩衫",
    },
    "mydei_trace2_debt_retain": {
        "trigger": "on_enter_battle",
        "action": _mydei_trace2_debt_retain,
        "source_name": "行迹·水与泥土",
    },
    "mydei_trace3_control_immune": {
        "trigger": "on_enter_battle",
        "action": _mydei_trace3_control_immune,
        "source_name": "行迹·三十僭主",
    },
    # 开拓者·记忆
    "tbr_trace2_scepter": {
        "trigger": "on_enter_battle",
        "action": _tbr_trace2_scepter,
        "source_name": "行迹·追念之权杖",
    },
    # v6.2.1: 以下三行迹为引擎内联实现（combat_sim/remembrance 直判），
    # action:None 作文档（Harness P3-7: 防误判"未实现"）
    "tbr_trace1_magnet_chain": {
        "trigger": "on_enter_battle",
        "action": None,
        "source_name": "行迹·磁石与长链（内联: 声援真伤按目标能量上限+2%/10点, 上限+20%）",
    },
    "tbr_trace3_pocket_poem": {
        "trigger": "on_enter_battle",
        "action": None,
        "source_name": "行迹·袖珍的事诗（内联: 坏人麻烦后迷迷+5%充能）",
    },
    "tbr_trace4_epic": {
        "trigger": "on_enter_battle",
        "action": None,
        "source_name": "行迹·未完的尾声（内联: 终结技后+1史诗, 普攻强化）",
    },
    # 风堇
    "fengjin_trace1_spd_heal": {
        "trigger": "on_enter_battle",
        "action": _trace_fengjin_t1,
        "source_name": "行迹·暴风停歇",
    },
    "fengjin_trace2_cr_heal": {
        "trigger": "on_enter_battle",
        "action": _trace_fengjin_t2,
        "source_name": "行迹·阴云莞尔",
    },
    "fengjin_trace3_cleanse": {
        "trigger": "on_enter_battle",
        "action": _trace_fengjin_t3,
        "source_name": "行迹·雷雨轻柔",
    },
    # v5.3 开拓者·同谐
    "tbh_harmony_trace1_ult_mult": {
        # 行迹1·为我起舞: 伴舞超击破按敌人数+20%~60% — 引擎内联(_apply_toughness_damage tbh_mult)
        "trigger": None, "action": None, "source_name": "行迹·为我起舞",
    },
    "tbh_harmony_trace2_skill_break": {
        "trigger": "on_before_skill",
        "action": _trace_tbh_t2_first_bounce,
        "source_name": "行迹·随波逐流",
    },
    "tbh_harmony_trace3_break_delay": {
        "trigger": "on_any_weakness_break",
        "action": _trace_tbh_t3_break_delay,
        "source_name": "行迹·剧院之帽",
    },
    "tbh_harmony_talent_energy": {
        "trigger": "on_any_weakness_break",
        "action": _trace_tbh_talent_energy,
        "source_name": "天赋·全屏段的高空踏歌",
    },
    # v5.3 忘归人
    "fugue_talent_cloudfire": {
        "trigger": "on_enter_battle",
        "action": _fugue_cloudfire_apply,
        "source_name": "天赋·盈后福，德气流布(云火昭)",
    },
    "fugue_talent_cloudfire_wave": {
        "trigger": "on_wave_start",
        "action": _fugue_cloudfire_apply,
        "source_name": "天赋·盈后福，德气流布(云火昭波次重挂)",
    },
    "fugue_talent_cloudfire_death": {
        "trigger": "on_ally_death",
        "action": _fugue_cloudfire_death,
        "source_name": "天赋·盈后福(云火昭失效)",
    },
    "fugue_talent_def_down": {
        "trigger": "on_ally_attack",
        "action": _fugue_foxian_def_down,
        "source_name": "天赋·盈后福，德气流布(狐祈DEF-18%)",
    },
    "fugue_trace1_break_delay": {
        "trigger": "on_any_weakness_break",
        "action": _fugue_trace1_break_delay,
        "source_name": "行迹·青丘重光",
    },
    "fugue_trace2_team_be": {
        "trigger": "on_any_weakness_break",
        "action": _fugue_trace2_team_be,
        "source_name": "行迹·玑星太素",
    },
    "fugue_trace3_self_be": {
        "trigger": "on_enter_battle",
        "action": _fugue_trace3_self_be,
        "source_name": "行迹·涂山玄设",
    },
    "fugue_trace3_first_sp": {
        "trigger": "on_skill",
        "action": _fugue_trace3_first_sp,
        "source_name": "行迹·涂山玄设(首次战技回SP)",
    },
    # v5.3 灵砂
    "lingsha_trace1_basic_energy": {
        "trigger": "on_basic_attack",
        "action": _trace_basic_energy_bonus,
        "source_name": "行迹·兰烟",
    },
    "lingsha_trace2_be_to_atk_heal": {
        "trigger": "on_enter_battle",
        "action": _trace_lingsha_t2_be_to_atk_heal,
        "source_name": "行迹·朱燎",
    },
    "lingsha_trace3_fuyuan_pursuit": {
        "trigger": "on_hp_loss",
        "action": _trace_lingsha_t3_pursuit,
        "source_name": "行迹·遗爇",
    },
    # v5.3 流萤
    "firefly_trace1_combustion_pull": {
        "trigger": "on_any_weakness_break",
        "action": _trace_firefly_t1_pull,
        "source_name": "行迹·偏时迸发(倒计时延后)",
    },
    "firefly_trace2_super_break": {
        # 行迹2·自限装甲: 燃烧下BE≥150%/300%→超击破100%/150% — 引擎内联(_super_break_rate)
        "trigger": None, "action": None, "source_name": "行迹·自限装甲",
    },
    "firefly_trace3_atk_to_be": {
        "trigger": "on_enter_battle",
        "action": _trace_firefly_t3_atk_to_be,
        "source_name": "行迹·过载核心",
    },
    "firefly_talent_start": {
        "trigger": "on_enter_battle",
        "action": _trace_firefly_talent_start,
        "source_name": "天赋·源火中枢(开局能量)",
    },
    "firefly_talent_cleanse": {
        "trigger": "on_energy_change",
        "action": _trace_firefly_talent_cleanse,
        "source_name": "天赋·源火中枢(满能清负面)",
    },
    "firefly_talent_dr_hp_loss": {
        "trigger": "on_hp_loss",
        "action": _trace_firefly_dr_hp_loss,
        "source_name": "天赋·源火中枢(减伤曲线)",
    },
    "firefly_talent_dr_turn": {
        "trigger": "on_turn_start",
        "action": _trace_firefly_dr_turn,
        "source_name": "天赋·源火中枢(减伤曲线回合刷新)",
    },
    # ── v6.7 绯英（角色技能介绍/欢愉/绯英.txt）──
    "evanescia_energy_convert": {
        "trigger": "on_energy_change",
        "action": _trace_evanescia_energy_convert,
        "source_name": "绯英天赋(能量↔好活互转+240累计FUA)",
    },
    "evanescia_trace1": {
        "trigger": None, "action": None,
        "source_name": "行迹·行裁断(狐狸老师易伤, FUA内联)",
    },
    "evanescia_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·开不败(队友好活到期50%转移, tick_turn内联)",
    },
    "evanescia_trace3": {
        "trigger": "on_enter_battle",
        "action": _trace_evanescia_trace3_cr,
        "source_name": "行迹·瞰众乐(暴击率+30%永久; 弹射/转移内联)",
    },
    "evanescia_base": {
        "trigger": "on_enter_battle",
        "action": _trace_evanescia_talent_elation,
        "source_name": "基础行迹(欢愉度=暴伤20%)",
    },
    # ── v6.7 火花（角色技能介绍/欢愉/火花.txt）──
    "sparxie_trace1": {
        "trigger": None, "action": None,
        "source_name": "行迹·人设万花筒(终结技笑点/爆点, ultimate内联)",
    },
    "sparxie_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·真伪调色盘(每笑点全队暴伤, eff_stats内联)",
    },
    "sparxie_trace3": {
        "trigger": None, "action": None,
        "source_name": "行迹·笑点签售会(ATK→欢愉度, eff_stats内联)",
    },
    "sparxie_base": {
        "trigger": None, "action": None, "source_name": "基础行迹",
    },
    # ── v6.7 大丽花（角色技能介绍/虚无/大丽花.txt）──
    "the_dahlia_trace1": {
        "trigger": "on_enter_battle",
        "action": _trace_dahlia_trace1_open,
        "source_name": "行迹·又一场葬礼(开战队友BE转移)",
    },
    "the_dahlia_trace1_heal": {
        "trigger": "on_heal",
        "action": _trace_dahlia_trace1_reapply,
        "source_name": "行迹·又一场葬礼(受治疗再触发3回合)",
    },
    "the_dahlia_trace1_shield": {
        "trigger": "on_shield",
        "action": _trace_dahlia_trace1_reapply,
        "source_name": "行迹·又一场葬礼(受护盾再触发3回合)",
    },
    "the_dahlia_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·致哀故人(FUA每2次回SP, FUA内联)",
    },
    "the_dahlia_trace3": {
        "trigger": "on_weakness_implant",
        "action": _trace_dahlia_trace3_implant,
        "source_name": "行迹·弃旧恋新(添弱点加速+火固定削韧)",
    },
    "the_dahlia_field_tick": {
        "trigger": "on_turn_start",
        "action": _trace_dahlia_field_tick,
        "source_name": "结界回合递减+FUA重置",
    },
    "the_dahlia_base": {
        "trigger": None, "action": None, "source_name": "基础行迹",
    },
    # ── v6.7 姬子·启行（角色技能介绍/智识/姬子·启行.txt）──
    "himeko_nova_protocol": {
        "trigger": "on_enter_battle",
        "action": _trace_hn_protocol,
        "source_name": "天赋·同行协议(裁决/歼破判定)",
    },
    "himeko_nova_trace1": {
        "trigger": None, "action": None,
        "source_name": "行迹·人类该向何处去(助战技不耗次数/回合回能, flag_regen内联)",
    },
    "himeko_nova_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·列车的脉搏在轰鸣(额外回合, support_skill内联)",
    },
    "himeko_nova_trace3": {
        "trigger": None, "action": None,
        "source_name": "行迹·银轨在旷古中静默(终结技+3源能/脉冲强化, ultimate内联)",
    },
    "himeko_nova_flag_regen": {
        "trigger": "on_turn_start",
        "action": _trace_hn_flag_regen,
        "source_name": "领航旗语次数恢复+行迹1回能",
    },
    "himeko_nova_base": {
        "trigger": None, "action": None, "source_name": "基础行迹",
    },
    # ── v6.9 批1 星期日/瓦尔特/阮·梅 ──
    "sunday_trace1": {"trigger": None, "action": None, "source_name": "行迹·主日渴慕(终结技回能补40, ult内联)"},
    "sunday_trace2": {"trigger": "on_enter_battle", "action": _trace_sunday_trace2, "source_name": "行迹·崇高拂尘(开局25能)"},
    "sunday_trace3": {"trigger": None, "action": None, "source_name": "行迹·掌中安港(战技净化, skill内联)"},
    "sunday_trace_tick": {"trigger": "on_turn_start", "action": _trace_sunday_tick, "source_name": "蒙福者倒计时+E4回能"},
    "sunday_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "welt_trace1": {"trigger": "on_enter_battle", "action": _trace_welt_trace1, "source_name": "行迹·惩戒(开局30能)"},
    "welt_trace2": {"trigger": None, "action": None, "source_name": "行迹·审判(普攻/战技附加, 内联)"},
    "welt_trace3": {"trigger": None, "action": None, "source_name": "行迹·裁决(EHR→ATK, 内联)"},
    "welt_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "ruan_mei_trace1": {"trigger": "on_enter_battle", "action": _trace_ruanmei_trace1, "source_name": "行迹·物体呼吸中(全队BE+20%)"},
    "ruan_mei_trace2": {"trigger": None, "action": None, "source_name": "行迹·日消遐思长(回合回5能, tick内联)"},
    "ruan_mei_trace3": {"trigger": None, "action": None, "source_name": "行迹·落烛照水燃(BE阈值增伤, 内联)"},
    "ruan_mei_field_tick": {"trigger": "on_turn_start", "action": _trace_ruanmei_tick, "source_name": "结界递减+行迹2回能"},
    "ruan_mei_break_damage": {"trigger": "on_any_weakness_break", "action": _trace_ruanmei_break, "source_name": "天赋·分型的螺旋(击破冰伤)"},
    "ruan_mei_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.9 批2 知更鸟/不死途 ──
    "robin_trace1": {"trigger": None, "action": None, "source_name": "行迹·即兴装饰(协奏期FUA暴伤, 内联)"},
    "robin_trace2": {"trigger": "on_enter_battle", "action": _trace_robin_trace2, "source_name": "行迹·华彩花腔(开局拉条25%)"},
    "robin_trace3": {"trigger": None, "action": None, "source_name": "行迹·模进乐段(战技额外5能, 内联)"},
    "robin_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "busitu_trace1": {"trigger": None, "action": None, "source_name": "行迹·罪途(婪酣获取, 内联)"},
    "busitu_trace2": {"trigger": None, "action": None, "source_name": "行迹·影肢(FUA增伤, 内联)"},
    "busitu_trace3": {"trigger": "on_enter_battle", "action": _trace_busitu_trace3, "source_name": "行迹·头狼(全队暴伤)"},
    "busitu_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.9 批3 千冶·刃 ──
    "qianye_trace1": {"trigger": "on_enter_battle", "action": _trace_qianye_trace1, "source_name": "行迹·百炼骨(开局75%能量; 溢出/净化内联)"},
    "qianye_trace2": {"trigger": None, "action": None, "source_name": "行迹·千锻魂(受击率/减伤/受击充能, 内联)"},
    "qianye_trace3": {"trigger": None, "action": None, "source_name": "行迹·万淬心(全队伤害/终结技, 内联)"},
    "qianye_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.10 黄泉 ──
    "acheron_trace1": {"trigger": "on_enter_battle", "action": _trace_acheron_trace1, "source_name": "行迹·赤鬼(开局5残梦+集真赤)"},
    "acheron_trace2": {"trigger": None, "action": None, "source_name": "行迹·奈落(虚无队友倍率, 面板守卫)"},
    "acheron_trace3": {"trigger": None, "action": None, "source_name": "行迹·雷心(增伤叠层/返渡额外段, ult内联)"},
    "acheron_trace_tick": {"trigger": "on_turn_start", "action": _trace_acheron_tick, "source_name": "E2回合开始+1残梦"},
    "acheron_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.10 飞霄 ──
    "feixiao_trace1": {"trigger": None, "action": None, "source_name": "行迹·天通(开局3飞黄, simulate内联)"},
    "feixiao_trace2": {"trigger": None, "action": None, "source_name": "行迹·解形(终结技视为FUA, ult内联)"},
    "feixiao_trace3": {"trigger": None, "action": None, "source_name": "行迹·电举(战技ATK+48%, skill内联)"},
    "feixiao_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
}


# ═══════════════════════════════════════════════════════════════════
# 光锥效果注册表: param_id → (trigger, action_fn)
# ═══════════════════════════════════════════════════════════════════

def _lc_sp_recovery(state, interval=2):
    c = state.extra.get('lc_sp_counter', 0) + 1
    state.extra['lc_sp_counter'] = c
    if c >= interval:
        from engine.core.combat_sim import _gain_skill_points
        _gain_skill_points(state)
        state.extra['lc_sp_counter'] = 0
        state.log.append('  光锥回SP')

def _lc_team_advance(state, ratio):
    AV_PER_TURN = 10000.0
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu.is_alive and i in navs:
            from engine.core.combat_sim import _effective_spd
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * ratio)
    state.log.append(f'  光锥拉条: 全队{ratio*100:.0f}%')

def _lc_ally_buff(state, unit, attrs, duration):
    from engine.core.combat_sim import TimedBuff
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if target:
        tb = TimedBuff(source_id=unit.char.id, attributes=attrs, remaining_turns=duration)
        target.buffs.append(tb)
        state.log.append(f'  光锥buff → {target.char.name}({duration}回合)')

def _lc_wave_heal(state, ratio=0.80):
    for u in state.units:
        if u.is_alive and u.current_hp < u.max_hp:
            lost = u.max_hp - u.current_hp
            heal = lost * ratio
            u.current_hp = min(u.max_hp, u.current_hp + heal)
            if heal > 1:
                state.log.append(f'  波次回血: {u.char.name}+{heal:.0f}HP')


LC_EFFECT_REGISTRY: dict[str, dict] = {
    "lc_but_the_battle_isnt_over_sp": {
        "trigger": "on_ultimate",
        "action": lambda **kw: _lc_sp_recovery(kw['state'], 2),
        "source_name": "但战斗还未结束·回SP",
    },
    "lc_but_the_battle_isnt_over_dmg": {
        "trigger": "on_skill",
        "action": lambda **kw: _lc_ally_buff(kw['state'], kw['u'], {'DMG_BONUS_ALL': 30.0}, 1),
        "source_name": "但战斗还未结束·增伤",
    },
    "lc_dance_dance_dance_advance": {
        "trigger": "on_ultimate",
        "action": lambda **kw: _lc_team_advance(kw['state'], 0.24),
        "source_name": "舞！舞！舞！·拉条",
    },
    "lc_she_already_shut_her_eyes_heal": {
        "trigger": "on_wave_start",
        "action": lambda **kw: _lc_wave_heal(kw['state'], 0.80),
        "source_name": "她已闭上双眼·波次回血",
    },
}


# ═══════════════════════════════════════════════════════════════════
# 星魂效果注册表
# ═══════════════════════════════════════════════════════════════════

def _eid_yaoguang_e1(u, state, **kw):
    """爻光E1: 阿哈额外回合固定40笑点 + 全队欢愉伤害无视20%防御"""
    pass  # 已在 _yg_ai 中实现，此处标记使 resolve 读取到 eidolon_rank

def _eid_yaoguang_e2(u, state, **kw):
    """爻光E2: 结界内全队SPD+12%, 欢愉度+16%
    v6.10.3 P1-3: 改为 _build_effective_stats 动态消费（此前 on_turn_start 每次叠 1 回合 TimedBuff 会无限叠层）"""
    pass

def _eid_yaoguang_e4(u, state, **kw):
    """爻光E4: 阿哈额外回合中全体欢愉技伤害×1.5"""
    pass  # 在 execute_aha 中处理

def _eid_yaoguang_e6(u, state, **kw):
    """爻光E6: 全队增笑25% + 自身欢愉技倍率+100%
    v6.10.3 P1-3: 改为动态面板（全队 LAUGH_BOOST）与 _use_skill 倍率内联（此前只给爻光自身且无倍率消费）"""
    pass

def _eid_yinlang_e1(u, state, **kw):
    """银狼E1: 结界内敌方受伤+20% + 退出无敌保留20%隐藏分"""
    from engine.core.combat_sim import _silver_wolf_apply_entry_effects
    _silver_wolf_apply_entry_effects(state)
    state.log.append('  银狼E1: 敌方受伤+20%, 退出无敌保留20%隐藏分')

def _eid_yinlang_e2(u, state, **kw):
    """银狼E2: 无敌内增益+1回合 + 每120隐藏分→额外回合+1强化普攻"""
    pass  # 在 elation.py 银狼逻辑中处理

def _eid_yinlang_e4(u, state, **kw):
    """银狼E4: 崩坏级欢愉伤害×5笑点"""
    pass  # 在 _silver_invincible_elation（崩坏级伤害演示）中处理

def _eid_yinlang_e6(u, state, **kw):
    """银狼E6: 强化普攻欢愉增笑50% + 禁限弱点"""
    from engine.core.combat_sim import _silver_wolf_apply_entry_effects
    _silver_wolf_apply_entry_effects(state)
    state.log.append('  银狼E6: 欢愉增笑+50%, 禁限弱点植入(全属性弱点+抗性归零/-20%)')

def _eid_seele_e4(u, state, **kw):
    """希儿E4: 击杀回15能量"""
    u.current_energy = min(u.char.max_energy or 999, u.current_energy + 15)
    state.log.append('  希儿E4: 击杀回15能量')

# 希儿E1(目标条件)/E2(叠层)/E6(乱蝶) 效果在战斗引擎内联实现(eidolon_rank直判), 注册表保留 None 作文档


def _eid_xilian_e2(u, state, **kw):
    """昔涟E2: 进战+12追忆"""
    u.zhuiyi = min(27, u.zhuiyi + 12)
    state.log.append(f'  昔涟E2: 开局追忆+12 → {u.zhuiyi:.0f}/27')


def _eid_xiadie_e4(u, state, healer=None, targets=None, heal_amt=0, **kw):
    """遐蝶E4: 遐蝶在场时全队受疗+20%（追加治疗, 不参与新蕊转化）"""
    if not targets or heal_amt <= 0:
        return
    if not any(x.char.id == 'xiadie' and x.is_alive for x in state.units):
        return
    for t in targets:
        if getattr(t, 'is_alive', True):
            t.current_hp = min(t.max_hp, t.current_hp + heal_amt * 0.20)
    state.log.append(f'  遐蝶E4: 全队受疗+20% → 追加{heal_amt * 0.20:.0f}HP×{len(targets)}')


def _eid_xiadie_e6(u, state, **kw):
    """遐蝶E6: 量子抗性穿透+20%（死龙召唤时 copy 继承）"""
    u.base_stats.RES_PEN['量子'] = u.base_stats.RES_PEN.get('量子', 0.0) + 0.20
    state.log.append('  遐蝶E6: 量子抗性穿透+20%')


def _eid_fengjin_e1(u, state, **kw):
    """风堇E1: 攻击后回8%HP"""
    heal = u.max_hp * 0.08
    u.current_hp = min(u.max_hp, u.current_hp + heal)
    state.log.append(f'  风堇E1: 攻击后回8%HP +{heal:.0f}')


def _eid_fengjin_e2(u, state, total_lost=0, affected=None, **kw):
    """风堇E2: HP降低→SPD+30% 2回合（刷新不叠加）"""
    from engine.core.combat_sim import TimedBuff
    if not affected:
        return
    for eu, lost in affected:
        # v6.5.1: affected 可能含忆灵(MemSpriteUnit.char 无 id) → isinstance 过滤
        from engine.core.combat_sim import SimUnit
        if isinstance(eu, SimUnit) and eu.char.id == 'fengjin' and eu.is_alive:
            for b in eu.buffs:
                if getattr(b, 'source_name', '') == '风堇E2·翼下':
                    b.remaining_turns = 2
                    break
            else:
                eu.buffs.append(TimedBuff(source_id='fengjin_e2',
                                          attributes={"SPD_PERCENT": 30.0},
                                          remaining_turns=2, source_name='风堇E2·翼下'))
                state.log.append('  风堇E2: HP降低→SPD+30% 2回合')
            break


def _eid_fengjin_e4(u, state, **kw):
    """风堇E4: 行迹1强化—SPD>200每超1点→暴伤+2%（上限200点）"""
    if u.base_stats.SPD > 200:
        bonus = 0.02 * min(u.base_stats.SPD - 200, 200)
        u.base_stats.CRIT_DMG += bonus
        state.log.append(f'  风堇E4: 超速暴伤+{bonus:.2f} (SPD={u.base_stats.SPD:.0f})')


def _eid_fengjin_e6(u, state, **kw):
    """风堇E6: 小伊卡在场→全队RES_PEN+20%（一次性守卫防重复）"""
    if state.extra.get('fengjin_e6_respen'):
        return
    fengjin = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
    if not fengjin or fengjin.eidolon_rank < 6:
        return
    state.extra['fengjin_e6_respen'] = True
    for eu in state.units:
        eu.base_stats.RES_PEN_ALL += 0.20
    state.log.append('  风堇E6: 小伊卡在场→全队RES_PEN+20%')

def _eid_bronya_e1(u, state, **kw):
    """布洛妮娅E1: 战技50%概率回1SP"""
    import random
    if random.random() < 0.50:
        from engine.core.combat_sim import _gain_skill_points
        _gain_skill_points(state)
        state.log.append('  布洛妮娅E1: 战技回1SP')

def _eid_bronya_e2(u, state, **kw):
    """布洛妮娅E2: 战技目标行动后SPD+30% 1回合"""
    from engine.core.combat_sim import TimedBuff
    target = next((x for x in state.units if x.char.id == 'seele' and x.is_alive), None)
    if target:
        tb = TimedBuff(source_id="bronya_e2", attributes={"SPD_PERCENT": 30.0},
                       remaining_turns=1, source_name="布洛妮娅E2·快速行军")
        target.buffs.append(tb)

def _eid_bronya_e6(u, state, **kw):
    """布洛妮娅E6: 战技增伤效果+1回合（duration 在 _apply_skill_effects 内联）"""
    pass  # 引擎内联: bronya_skill_dmg_buff duration 1→2


def _eid_bronya_e4(u, state, target=None, skill_key=None, **kw):
    """布洛妮娅E4·攻其不备: 他角色对风弱点敌普攻后→布洛妮娅追加攻击(普攻伤害80%风伤, 每回合1次)"""
    from engine.core.combat_sim import (_build_effective_stats, calculate_damage,
                                        _commit_enemy_damage, _enemy_for_damage)
    if u.char.id == 'bronya' or skill_key != 'basic_attack' or not target:
        return
    if getattr(target, 'element_res', {}).get('风', 0.2) > 0:
        return  # 非风弱点
    if state.extra.get('bronya_e4_used_turn', -1) == state.turn_count:
        return  # 每回合1次
    bronya = next((x for x in state.units if x.char.id == 'bronya' and x.is_alive), None)
    if not bronya:
        return
    state.extra['bronya_e4_used_turn'] = state.turn_count
    s = _build_effective_stats(bronya, state)
    d = calculate_damage(s, _enemy_for_damage(target), s.ATK, 80.0, "direct", "风", 80,
                         s.CRIT_RATE >= 0.5, crit_mode="expected", attack_type="follow_up")
    _commit_enemy_damage(state, bronya, target, d.final_damage)
    bronya.total_damage_dealt += d.final_damage
    state.log.append(f'  布洛妮娅E4: 追加攻击风伤 {d.final_damage:.0f}')
    # 大公4pc按追加攻击实际造成伤害的段数叠层；本次追加仅有一段。
    state.hooks.trigger_all("on_followup_hit", u=bronya, state=state)
    # v5.0.1: 光锥追加攻击事件（流光/影噬/谕示/火舞等）
    from engine.core.combat_sim import _process_lc_effects
    state.extra['lc_attack_targets'] = 1
    state.extra['lc_attack_target_refs'] = [target]
    state.extra['lc_attack_first_target_id'] = target.id
    _process_lc_effects(bronya, state, "on_followup")
    _process_lc_effects(bronya, state, "on_self_attack")  # 追加攻击也是攻击
    # 动作级追加攻击事件（千星/都蓝王朝——u=执行者=持有者）
    state.hooks.trigger_all("on_followup", u=bronya, state=state)
    # 每累计4次追加攻击（谎言终局·影噬）
    n = state.extra.get('lc_followup_count', 0) + 1
    state.extra['lc_followup_count'] = n
    if n % 4 == 0:
        _process_lc_effects(bronya, state, "on_followup_4th")


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


def _huohuo_e2_fatal_check(state):
    """藿藿E2·镇尾锁灵: 持禳命时我方受致命攻击→不死亡+回50%生命+禳命-1(单场2次)。
    v6.10.6 A3: 补 eidolon>=2 + 藿藿当前持有禳命门控（此前无禳命也触发且负HP存活）"""
    huohuo = next((x for x in state.units
                   if x.char.id == 'huohuo' and x.is_alive), None)
    if huohuo is None or huohuo.eidolon_rank < 2:
        return False
    if huohuo.extra.get('huohuo_ruming_turns', 0) <= 0:
        return False
    charges = state.extra.get('huohuo_e2_charges', 0)
    if charges <= 0:
        return False
    state.extra['huohuo_e2_charges'] = charges - 1
    # v6.10.6 B: E2 触发使禳命持续回合-1（TXT 藿藿.txt:7）
    ruming = huohuo.extra.get('huohuo_ruming_turns', 0) - 1
    if ruming <= 0:
        huohuo.extra.pop('huohuo_ruming_turns', None)
        huohuo.extra.pop('huohuo_ruming_cleanse', None)
    else:
        huohuo.extra['huohuo_ruming_turns'] = ruming
    for eu in state.units:
        if eu.is_alive:
            eu.current_hp = min(eu.max_hp, max(0.0, eu.current_hp) + eu.max_hp * 0.50)
    state.log.append(f'  藿藿E2·镇尾锁灵: 致命保护触发, 回50%生命 ({charges-1}/2次)')
    return True


def _eid_huohuo_e2(u, state, **kw):
    """藿藿E2·镇尾锁灵: 初始化单场2次次数（保护逻辑见 _huohuo_e2_fatal_check, 等受击闭环）"""
    if u.char.id != 'huohuo':
        return
    state.extra['huohuo_e2_charges'] = 2
    state.log.append('  藿藿E2: 致命保护就位(单场2次, 待受击闭环)')

def _eid_sparkle_e1(u, state, **kw):
    """花火E1: 谜诡持有者ATK+40%（动态面板消费）+ 花火自身SPD+15% 2回合
    v6.10.6 C1: 删除硬编码希儿与永久改面板; ATK 部分在 _build_effective_stats 动态消费"""
    from engine.core.combat_sim import TimedBuff
    u.buffs = [b for b in u.buffs if getattr(b, 'param_id', '') != 'sparkle_e1_spd']
    u.buffs.append(TimedBuff(source_id='sparkle', attributes={'SPD_PERCENT': 15.0},
                             remaining_turns=2, param_id='sparkle_e1_spd',
                             source_name='花火E1·悬置怀疑'))
    state.log.append('  花火E1: 自身SPD+15%(2回合)')

def _eid_sparkle_e2(u, state, **kw):
    """花火E2: 天赋每层额外减防10%"""
    pass  # 在花火天赋中处理

def _eid_sparkle_e4(u, state, **kw):
    """花火E4: 终结技回1SP + SP上限+1"""
    state.max_sp += 1
    from engine.core.combat_sim import _gain_skill_points
    _gain_skill_points(state)
    state.log.append('  花火E4: 终结技回1SP, SP上限+1')

def _eid_sparkle_e6(u, state, **kw):
    """花火E6: 战技CD额外+30%花火CD + 谜诡扩散"""
    pass  # 在花火战技buff中处理

def _eid_huohuo_e1(u, state, **kw):
    """藿藿E1: 全队SPD+12% + 自身治疗量+20%（禳命+1回合无禳命计时系统, 占位注释）"""
    for eu in state.units:
        if eu.is_alive:
            eu.base_stats.SPD += eu.base_stats._base_SPD * 0.12
    u.base_stats.HEAL_BONUS += 0.20
    state.log.append('  藿藿E1: 全队SPD+12%, 治疗+20%')

def _eid_huohuo_e6(u, state, healer=None, targets=None, heal_amt=0, **kw):
    """藿藿E6·同休共戚: 藿藿提供治疗时→被治疗目标伤害+50% 2回合（刷新语义）"""
    from engine.core.combat_sim import TimedBuff
    if not targets or heal_amt <= 0:
        return
    if getattr(getattr(healer, 'char', None), 'id', None) != 'huohuo':
        return
    for t in targets:
        if not hasattr(t, 'buffs'):
            continue
        refreshed = False
        for b in t.buffs:
            if getattr(b, 'source_name', '') == '藿藿E6·同休共戚':
                b.remaining_turns = 2
                refreshed = True
                break
        if not refreshed:
            t.buffs.append(TimedBuff(source_id='huohuo_e6', attributes={"DMG_BONUS_ALL": 50.0},
                                     remaining_turns=2, source_name='藿藿E6·同休共戚'))
    state.log.append('  藿藿E6: 治疗→目标伤害+50% 2回合')

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
    from engine.core.combat_sim import TimedBuff
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
    from engine.core.combat_sim import TimedBuff
    u.buffs = [b for b in u.buffs
               if getattr(b, 'param_id', '') != 'tb_e6']
    tb = TimedBuff(source_id="tb_e6", attributes={"CRIT_DMG": 100.0},
                   remaining_turns=3, param_id="tb_e6",
                   source_name="开拓者E6·银河传奇")
    u.buffs.append(tb)

def _eid_skill_levels(u, state, **kw):
    """E3/E5: 技能等级提升——解析角色星魂声明文本构建技能等级覆盖表
    v6.10.3 P1-6: 此前简化"全伤害+6%"且永久改面板, 导致未升级技能也吃加成;
    现在按星魂声明提升对应技能倍率/治疗护盾数值（每级+5%, _use_skill 消费）"""
    import re as _re
    _SKILL_KEY = {'普攻': 'basic_attack', '战技': 'skill', '终结技': 'ultimate',
                  '天赋': 'talent', '欢愉技': 'elation_skill'}
    boost = {}
    for eid in (u.char.eidolons or []):
        hook = getattr(eid, 'hook_name', '') or ''
        if hook.endswith('_e3') and u.eidolon_rank < 3:
            continue
        if hook.endswith('_e5') and u.eidolon_rank < 5:
            continue
        if not (hook.endswith('_e3') or hook.endswith('_e5')):
            continue
        desc = getattr(eid, 'description', '') or ''
        for m in _re.finditer(r'(普攻|战技|终结技|天赋|欢愉技)(?:等级)?\+(\d+)', desc):
            sk = _SKILL_KEY[m.group(1)]
            boost[sk] = boost.get(sk, 0) + int(m.group(2))
    u.extra['skill_level_boost'] = boost


# ── 长夜月专属处理器 ──

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

def _changyeyue_ult(u, state, **kw):
    """终结技: 进入【至暗之谜】"""
    if u.char.id != "changyeyue":
        return
    u.is_darkness = True
    u.darkness_charges = 2
    # 全队易伤+30%
    u.base_stats.VULNERABILITY_APPLIED += 0.30
    # 自身+忆灵伤害+60%
    u.base_stats.DMG_BONUS_ALL += 0.60
    if u.memsprite_unit:
        u.memsprite_unit.base_stats.DMG_BONUS_ALL += 0.60
    state.log.append(f'  进入【至暗之谜】(充能={u.darkness_charges}): 敌方受伤+30%, 自+忆灵伤害+60%')
    # E2: 额外+2充能
    if u.char.eidolons and len(u.char.eidolons) >= 2:
        u.darkness_charges += 2
        state.log.append(f'  长夜月E2: 充能+2→{u.darkness_charges}')


# ── 遐蝶专属处理器 ──

def _xiadie_heal_to_xinrui(state, targets, heal_amt):
    """行迹3·收容的暗潮: 除死龙外队友治疗→100%转化为新蕊。
    死龙在场→转化为死龙HP。每人上限=新蕊上限12%(4080)，任意单位行动后重置。"""
    if not targets:
        return
    total_heal = heal_amt * len(targets)  # 全队治疗总量
    for t in targets:
        if not hasattr(t, 'char') or not hasattr(t.char, 'id') or t.char.id != 'xiadie':
            continue
        # 每人累计转化上限 = 新蕊上限×12%（4080/8160），任意单位行动后重置
        from engine.core.combat_sim import xiadie_xinrui_cap
        cap = xiadie_xinrui_cap(t)
        conv_limit = cap * 0.12
        conv = state.extra.setdefault('xiadie_heal_conv', 0.0)
        if conv >= conv_limit:
            continue
        cap_amt = min(total_heal, conv_limit - conv)
        conv += cap_amt
        state.extra['xiadie_heal_conv'] = conv
        dragon = t.memsprite_unit
        if dragon and dragon.is_alive:
            # 死龙在场: 治疗→死龙HP恢复
            dragon.current_hp = min(dragon.max_hp, dragon.current_hp + cap_amt)
            state.log.append(f'  收容的暗潮: 治疗→死龙回血+{cap_amt:.0f} (HP={dragon.current_hp:.0f}/{dragon.max_hp:.0f})')
        else:
            old_xr = t.xinrui
            t.xinrui = min(cap, t.xinrui + cap_amt)
            if t.xinrui - old_xr > 1:
                state.log.append(f'  新蕊+{t.xinrui - old_xr:.0f} (治疗转化) → {t.xinrui:.0f}/{cap:.0f}')


EIDOLON_REGISTRY: dict[str, dict] = {
    # 爻光
    "yaoguang_e1": {"trigger": "on_enter_battle", "action": _eid_yaoguang_e1, "source_name": "爻光E1"},
    "yaoguang_e2": {"trigger": "on_turn_start",   "action": _eid_yaoguang_e2, "source_name": "爻光E2"},
    "yaoguang_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "爻光E3"},
    "yaoguang_e4": {"trigger": "on_enter_battle", "action": _eid_yaoguang_e4, "source_name": "爻光E4"},
    "yaoguang_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "爻光E5"},
    "yaoguang_e6": {"trigger": "on_enter_battle", "action": _eid_yaoguang_e6, "source_name": "爻光E6"},
    # 银狼
    "yinlang_e1": {"trigger": "on_enter_battle",  "action": _eid_yinlang_e1, "source_name": "银狼E1"},
    "yinlang_e2": {"trigger": "on_enter_battle",  "action": _eid_yinlang_e2, "source_name": "银狼E2"},
    "yinlang_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "银狼E3"},
    "yinlang_e4": {"trigger": "on_enter_battle",  "action": _eid_yinlang_e4, "source_name": "银狼E4"},
    "yinlang_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "银狼E5"},
    "yinlang_e6": {"trigger": "on_enter_battle",  "action": _eid_yinlang_e6, "source_name": "银狼E6"},
    # 海瑟音星魂效果由 combat_sim 的DOT/结界生命周期统一消费，注册表保留
    # 明确入口，避免 JSON hook 名称落空或被重复触发。
    "hysilens_e1": {"trigger": None, "action": None, "source_name": "海瑟音E1（DOT倍率，内联）"},
    "hysilens_e2": {"trigger": None, "action": None, "source_name": "海瑟音E2（行迹3全队，内联）"},
    "hysilens_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "海瑟音E3"},
    "hysilens_e4": {"trigger": None, "action": None, "source_name": "海瑟音E4（结界抗性，内联）"},
    "hysilens_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "海瑟音E5"},
    "hysilens_e6": {"trigger": None, "action": None, "source_name": "海瑟音E6（DOT上限/倍率，内联）"},
    # 希儿
    "seele_e1": {"trigger": "on_enter_battle",   "action": None, "source_name": "希儿E1"},  # 引擎内联(目标条件)
    "seele_e2": {"trigger": "on_enter_battle",   "action": None, "source_name": "希儿E2"},  # 引擎内联(叠层)
    "seele_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "希儿E3"},
    "seele_e4": {"trigger": "on_kill",           "action": _eid_seele_e4, "source_name": "希儿E4"},
    "seele_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "希儿E5"},
    "seele_e6": {"trigger": "on_ultimate",       "action": None, "source_name": "希儿E6"},  # 引擎内联(乱蝶)
    # 布洛妮娅
    "bronya_e1": {"trigger": "on_skill",         "action": _eid_bronya_e1, "source_name": "布洛妮娅E1"},
    "bronya_e2": {"trigger": "on_skill",         "action": _eid_bronya_e2, "source_name": "布洛妮娅E2"},
    "bronya_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "布洛妮娅E3"},
    "bronya_e4": {"trigger": "on_ally_attack",    "action": _eid_bronya_e4, "source_name": "布洛妮娅E4"},
    "bronya_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "布洛妮娅E5"},
    "bronya_e6": {"trigger": "on_skill",         "action": _eid_bronya_e6, "source_name": "布洛妮娅E6"},
    # 花火
    "sparkle_e1": {"trigger": "on_enter_battle",  "action": _eid_sparkle_e1, "source_name": "花火E1"},
    "sparkle_e2": {"trigger": "on_enter_battle",  "action": _eid_sparkle_e2, "source_name": "花火E2"},
    "sparkle_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "花火E3"},
    "sparkle_e4": {"trigger": "on_ultimate",      "action": _eid_sparkle_e4, "source_name": "花火E4"},
    "sparkle_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "花火E5"},
    "sparkle_e6": {"trigger": "on_skill",         "action": _eid_sparkle_e6, "source_name": "花火E6"},
    # 藿藿
    "huohuo_e1": {"trigger": "on_enter_battle",   "action": _eid_huohuo_e1, "source_name": "藿藿E1"},
    "huohuo_e2": {"trigger": "on_enter_battle",   "action": _eid_huohuo_e2, "source_name": "藿藿E2"},
    "huohuo_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "藿藿E3"},
    "huohuo_e4": {"trigger": "on_enter_battle",   "action": None, "source_name": "藿藿E4"},
    "huohuo_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "藿藿E5"},
    "huohuo_e6": {"trigger": "on_heal",           "action": _eid_huohuo_e6, "source_name": "藿藿E6"},
    # 符玄
    "fuxuan_e1": {"trigger": "on_enter_battle",   "action": _eid_fuxuan_e1, "source_name": "符玄E1"},
    "fuxuan_e2": {"trigger": "on_enter_battle",   "action": _eid_fuxuan_e2, "source_name": "符玄E2"},
    "fuxuan_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "符玄E3"},
    "fuxuan_e4": {"trigger": "on_take_damage",    "action": _eid_fuxuan_e4, "source_name": "符玄E4"},
    "fuxuan_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "符玄E5"},
    "fuxuan_e6": {"trigger": "on_hp_loss",        "action": _eid_fuxuan_e6_loss, "source_name": "符玄E6"},
    # 开拓者·欢愉
    "trailblazer_elation_e1": {"trigger": "on_skill",        "action": _eid_tb_elation_e1, "source_name": "开拓者E1"},
    "trailblazer_elation_e2": {"trigger": "on_ultimate",     "action": _eid_tb_elation_e2, "source_name": "开拓者E2"},
    "trailblazer_elation_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者E3"},
    "trailblazer_elation_e4": {"trigger": "on_elation_skill", "action": _eid_tb_elation_e4, "source_name": "开拓者E4"},
    "trailblazer_elation_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者E5"},
    "trailblazer_elation_e6": {"trigger": "on_elation_skill", "action": _eid_tb_elation_e6, "source_name": "开拓者E6"},
    # 开拓者·记忆（v5.7: E1内联于_tbr_support_skill, E2 hook注册, E4/E6内联于_use_skill）
    "tbr_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "开拓者·记忆E1(声援CR+10%/对忆灵生效)"},
    "tbr_e2": {"trigger": "on_memsprite_attack", "action": _eid_tbr_e2, "source_name": "开拓者·记忆E2"},
    "tbr_e2_reset": {"trigger": "on_turn_start", "action": _eid_tbr_e2_reset, "source_name": "开拓者·记忆E2(回合重置)"},
    "tbr_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "开拓者·记忆E3"},
    "tbr_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "开拓者·记忆E4(能量上限0目标施技→迷迷+3%充能, 内联)"},
    "tbr_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "开拓者·记忆E5"},
    "tbr_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "开拓者·记忆E6(终结技暴击率固定100%, 内联)"},
    # 长夜月
    "changyeyue_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "长夜月E1"},
    "changyeyue_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "长夜月E2(暴伤+40%内联于summon_memsprite, 忆质+2统一于_gain_yizhi, v5.7)"},
    "changyeyue_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "长夜月E3"},
    "changyeyue_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "长夜月E4"},
    "changyeyue_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "长夜月E5"},
    "changyeyue_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "长夜月E6"},
    # 遐蝶
    "xiadie_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "遐蝶E1"},  # 引擎内联(死龙条件伤害)
    "xiadie_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "遐蝶E2"},  # 引擎内联(炽意/拉条)
    "xiadie_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "遐蝶E3"},
    "xiadie_e4": {"trigger": "on_heal",          "action": _eid_xiadie_e4, "source_name": "遐蝶E4"},
    "xiadie_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "遐蝶E5"},
    "xiadie_e6": {"trigger": "on_enter_battle", "action": _eid_xiadie_e6, "source_name": "遐蝶E6"},
    # 昔涟
    "xilian_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E1"},  # 引擎内联(弹射)
    "xilian_e2": {"trigger": "on_enter_battle", "action": _eid_xilian_e2, "source_name": "昔涟E2"},
    "xilian_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "昔涟E3"},
    "xilian_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E4"},
    "xilian_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "昔涟E5"},
    "xilian_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E6"},
    # 阿格莱雅
    "aglaea_e1": {"trigger": "on_enter_battle", "action": _eid_aglaea_e1, "source_name": "阿格莱雅E1"},
    "aglaea_e2": {"trigger": "on_enter_battle", "action": _eid_aglaea_e2, "source_name": "阿格莱雅E2"},
    "aglaea_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阿格莱雅E3"},
    "aglaea_e4": {"trigger": "on_enter_battle", "action": _eid_aglaea_e4, "source_name": "阿格莱雅E4"},
    "aglaea_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阿格莱雅E5"},
    "aglaea_e6": {"trigger": "on_enter_battle", "action": _eid_aglaea_e6, "source_name": "阿格莱雅E6"},
    # 万敌
    "mydei_e1": {"trigger": "on_enter_battle", "action": _eid_mydei_e1, "source_name": "万敌E1"},
    "mydei_e2": {"trigger": "on_enter_battle", "action": _eid_mydei_e2, "source_name": "万敌E2"},
    "mydei_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "万敌E3"},
    "mydei_e4": {"trigger": "on_enter_battle", "action": _eid_mydei_e4, "source_name": "万敌E4"},
    "mydei_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "万敌E5"},
    "mydei_e6": {"trigger": "on_enter_battle", "action": _eid_mydei_e6, "source_name": "万敌E6"},
    # 风堇
    "fengjin_e1": {"trigger": "on_after_skill",       "action": _eid_fengjin_e1, "source_name": "风堇E1"},
    "fengjin_e2": {"trigger": "on_hp_loss",           "action": _eid_fengjin_e2, "source_name": "风堇E2"},
    "fengjin_e3": {"trigger": "on_enter_battle",      "action": _eid_skill_levels, "source_name": "风堇E3"},
    "fengjin_e4": {"trigger": "on_enter_battle",      "action": _eid_fengjin_e4, "source_name": "风堇E4"},
    "fengjin_e5": {"trigger": "on_enter_battle",      "action": _eid_skill_levels, "source_name": "风堇E5"},
    "fengjin_e6": {"trigger": "on_memsprite_summon",  "action": _eid_fengjin_e6, "source_name": "风堇E6"},
    # v5.3 开拓者·同谐
    "tbh_harmony_e1": {"trigger": "on_skill",         "action": _eid_tbh_e1, "source_name": "开拓者·同谐E1"},
    "tbh_harmony_e2": {"trigger": "on_enter_battle",  "action": _eid_tbh_e2, "source_name": "开拓者·同谐E2"},
    "tbh_harmony_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者·同谐E3"},
    "tbh_harmony_e4": {"trigger": "on_enter_battle",  "action": _eid_tbh_e4, "source_name": "开拓者·同谐E4"},
    "tbh_harmony_e4_refresh": {"trigger": "on_turn_start", "action": _eid_tbh_e4, "source_name": "开拓者·同谐E4(回合刷新动态BE, v5.7)"},
    "tbh_harmony_e4_death": {"trigger": "on_ally_death", "action": _eid_tbh_e4_death, "source_name": "开拓者·同谐E4(光环失效)"},
    "tbh_harmony_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者·同谐E5"},
    "tbh_harmony_e6": {"trigger": "on_enter_battle",  "action": None, "source_name": "开拓者·同谐E6"},  # 引擎内联(弹射+2)
    # v5.3 忘归人
    "fugue_e1": {"trigger": "on_enter_battle",  "action": None, "source_name": "忘归人E1"},  # 引擎内联(狐祈者击破效率×1.5)
    "fugue_e2": {"trigger": "on_any_weakness_break", "action": _eid_fugue_e2_energy, "source_name": "忘归人E2"},
    "fugue_e2_ult": {"trigger": "on_ultimate", "action": _eid_fugue_e2_ult, "source_name": "忘归人E2(终结技拉条)"},
    "fugue_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "忘归人E3"},
    "fugue_e4": {"trigger": "on_enter_battle",  "action": None, "source_name": "忘归人E4"},  # 引擎内联(狐祈者击破伤害×1.2)
    "fugue_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "忘归人E5"},
    "fugue_e6": {"trigger": "on_enter_battle",  "action": None, "source_name": "忘归人E6"},  # 引擎内联(自身击破效率×1.5/狐祈全队)
    # v5.3 灵砂
    "lingsha_e1": {"trigger": "on_any_weakness_break", "action": _eid_lingsha_e1_break, "source_name": "灵砂E1"},
    "lingsha_e2": {"trigger": "on_ultimate", "action": _eid_lingsha_e2_ult, "source_name": "灵砂E2"},
    "lingsha_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "灵砂E3"},
    "lingsha_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "灵砂E4"},  # marker内联(浮元行动治疗最低HP)
    "lingsha_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "灵砂E5"},
    "lingsha_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "灵砂E6"},  # MARKER_SPAWN内联(全抗-20%/额外4次)
    # v5.3 流萤
    "firefly_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E1"},  # 引擎内联(强化战技不耗SP+无视15%防御)
    "firefly_e2_kill": {"trigger": "on_kill", "action": _eid_firefly_e2_kill, "source_name": "流萤E2(击杀)"},
    "firefly_e2_break": {"trigger": "on_any_weakness_break", "action": _eid_firefly_e2_break, "source_name": "流萤E2(击破)"},
    "firefly_e2_reset": {"trigger": "on_turn_start", "action": _eid_firefly_e2_reset, "source_name": "流萤E2(回合重置)"},
    "firefly_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "流萤E3"},
    "firefly_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E4"},  # 燃烧状态机内联(效果抵抗+50%)
    "firefly_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "流萤E5"},
    "firefly_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E6"},  # 燃烧状态机内联(火抗穿20%/击破效率)
    # ── v6.7 绯英 ──
    "evanescia_e1": {"trigger": "on_enter_battle", "action": _eid_evanescia_e1, "source_name": "绯英E1(全抗穿透20%)"},
    "evanescia_e2": {"trigger": "on_enter_battle", "action": _eid_evanescia_e2, "source_name": "绯英E2(暴伤36%+好活乘区内联)"},
    "evanescia_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "绯英E3"},
    "evanescia_e4": {"trigger": "on_enter_battle", "action": _eid_evanescia_e4, "source_name": "绯英E4(无视15%防御)"},
    "evanescia_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "绯英E5"},
    "evanescia_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "绯英E6"},  # 引擎内联(好活持续+1/欢愉伤害/首终结技回能)
    # ── v6.7 火花 ──
    "sparxie_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E1"},  # eff_stats内联(阿哈+5笑点/每笑点抗穿)
    "sparxie_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E2"},  # 阿哈内联(额外回合+爆点) + 扣费内联(暴伤)
    "sparxie_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "火花E3"},
    "sparxie_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E4"},  # ultimate内联(+5笑点+欢愉度36%)
    "sparxie_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "火花E5"},
    "sparxie_e6": {"trigger": "on_enter_battle", "action": _eid_sparxie_e6, "source_name": "火花E6(抗穿20%+弹射内联)"},
    # ── v6.7 大丽花 ──
    "the_dahlia_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "大丽花E1"},  # 超击破/固定削韧内联
    "the_dahlia_e2": {"trigger": "on_enter_battle", "action": _eid_dahlia_e2, "source_name": "大丽花E2(全抗-20%+败谢)"},
    "the_dahlia_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "大丽花E3"},
    "the_dahlia_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "大丽花E4"},  # FUA内联(+5次+受伤12%)
    "the_dahlia_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "大丽花E5"},
    "the_dahlia_e6": {"trigger": "on_enter_battle", "action": _eid_dahlia_e6, "source_name": "大丽花E6(共舞者BE+150%)"},
    # ── v6.7 姬子·启行 ──
    "himeko_nova_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E1"},  # 内联(裁决-1/歼破-3/弹射+1)
    "himeko_nova_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E2"},  # 内联(上限2/伤害×130%)
    "himeko_nova_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "姬子·启行E3"},
    "himeko_nova_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E4"},  # 内联(助战技全队抗穿)
    "himeko_nova_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "姬子·启行E5"},
    "himeko_nova_e6": {"trigger": "on_enter_battle", "action": _eid_hn_e6, "source_name": "姬子·启行E6(火抗穿20%)"},
    # v6.10.3 赛飞儿：FUA/E2/E4/E6 复杂效果在 combat_sim 内联（_cipher_attack_aftermath 等）,
    # E1 记录×150% 在 _cipher_record 内联; 这里保留完整星魂注册。
    "cipher_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "赛飞儿E1"},  # 内联(记录×150%+FUA ATK+80%)
    "cipher_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "赛飞儿E2"},  # 内联(击中易伤30% 2回合)
    "cipher_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "赛飞儿E3"},
    "cipher_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "赛飞儿E4"},  # 内联(老主顾受击附加50%ATK)
    "cipher_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "赛飞儿E5"},
    "cipher_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "赛飞儿E6"},  # 内联(FUA×4.5+记录+16%+清空返还20%)
    # v6.10.3 P2-2: 银狼/缇宝星魂注册补齐（此前完全缺失, 解析器会静默丢弃TXT星魂）
    "silver_wolf_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "银狼E1"},  # 内联(终结技负面回能)
    "silver_wolf_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "银狼E2"},  # 内联(敌受伤+20%/随机缺陷)
    "silver_wolf_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "银狼E3"},
    "silver_wolf_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "银狼E4"},  # 内联(终结技负面附加)
    "silver_wolf_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "银狼E5"},
    "silver_wolf_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "银狼E6"},  # 内联(每负面+20%上限100%)
    "tribbie_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "缇宝E1"},  # 内联(结界附加真伤)
    "tribbie_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "缇宝E2"},  # 内联(附加×1.2+额外1次)
    "tribbie_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "缇宝E3"},
    "tribbie_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "缇宝E4"},  # 内联(神启期全队无视防御)
    "tribbie_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "缇宝E5"},
    "tribbie_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "缇宝E6"},  # 内联(终结技FUA+729%)
    # v6.10.2 那刻夏/刻律德菈/丹恒·腾荒/白厄：复杂效果在 combat_sim 内联，
    # 这里保留完整星魂注册，避免解析器把 TXT 星魂静默丢弃。
    "anaxa_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "那刻夏E1"},
    "anaxa_e2": {"trigger": "on_enter_battle", "action": _eid_anaxa_e2, "source_name": "那刻夏E2"},
    "anaxa_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "那刻夏E3"},
    "anaxa_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "那刻夏E4"},
    "anaxa_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "那刻夏E5"},
    "anaxa_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "那刻夏E6"},
    "cerydra_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "刻律德菈E1"},
    "cerydra_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "刻律德菈E2"},
    "cerydra_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "刻律德菈E3"},
    "cerydra_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "刻律德菈E4"},
    "cerydra_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "刻律德菈E5"},
    "cerydra_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "刻律德菈E6"},
    "dht_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "丹恒·腾荒E1"},
    "dht_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "丹恒·腾荒E2"},
    "dht_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "丹恒·腾荒E3"},
    "dht_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "丹恒·腾荒E4"},
    "dht_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "丹恒·腾荒E5"},
    "dht_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "丹恒·腾荒E6"},
    "phainon_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "白厄E1"},
    "phainon_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "白厄E2"},
    "phainon_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "白厄E3"},
    "phainon_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "白厄E4"},
    "phainon_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "白厄E5"},
    "phainon_e6": {"trigger": "on_enter_battle", "action": _eid_phainon_e6, "source_name": "白厄E6"},
    # ── v6.9 批1 星期日/瓦尔特/阮·梅 ──
    "sunday_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "星期日E1"},  # skill内联(无视防御)
    "sunday_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "星期日E2"},  # ult内联(首终结技+2SP+蒙福者伤害)
    "sunday_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "星期日E3"},
    "sunday_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "星期日E4"},  # tick内联(回合回8能)
    "sunday_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "星期日E5"},
    "sunday_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "星期日E6"},  # 内联(CR叠层/溢出暴击率转暴伤)
    "welt_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "瓦尔特E1"},  # 附加伤害内联
    "welt_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "瓦尔特E2"},  # 天赋回能内联
    "welt_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "瓦尔特E3"},
    "welt_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "瓦尔特E4"},  # 失重全抗内联
    "welt_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "瓦尔特E5"},
    "welt_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "瓦尔特E6"},  # 减速双暴内联
    "ruan_mei_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "阮·梅E1"},  # 结界期无视防御内联
    "ruan_mei_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "阮·梅E2"},  # 破韧目标ATK+40%(待实现简化)
    "ruan_mei_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阮·梅E3"},
    "ruan_mei_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "阮·梅E4"},  # 击破自身BE+100%内联
    "ruan_mei_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阮·梅E5"},
    "ruan_mei_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "阮·梅E6"},  # 结界+1/天赋击破+200%内联
    # ── v6.9 批2 知更鸟/不死途 ──
    "robin_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "知更鸟E1"},  # 协奏期全抗穿透内联
    "robin_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "知更鸟E2"},  # 协奏期速度/回能内联
    "robin_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "知更鸟E3"},
    "robin_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "知更鸟E4"},  # 终结技解控内联
    "robin_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "知更鸟E5"},
    "robin_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "知更鸟E6"},  # 附加暴伤+450%内联
    "busitu_e1": {"trigger": "on_enter_battle", "action": _trace_busitu_e1, "source_name": "不死途E1(全敌受伤24%)"},
    "busitu_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "不死途E2"},  # 婪酣上限18内联
    "busitu_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "不死途E3"},
    "busitu_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "不死途E4"},  # 终结技ATK+40%内联
    "busitu_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "不死途E5"},
    "busitu_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "不死途E6"},  # 有饲饵全抗-20%+婪酣增伤内联
    # ── v6.9 批3 千冶·刃 ──
    "qianye_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "千冶·刃E1"},  # 结界期全抗-20%+倒计时延后内联
    "qianye_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "千冶·刃E2"},  # 终结技视为FUA+充能上限7内联
    "qianye_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "千冶·刃E3"},
    "qianye_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "千冶·刃E4"},  # 万淬心+50%内联
    "qianye_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "千冶·刃E5"},
    "qianye_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "千冶·刃E6"},  # 受击/耗血充能+倍率×150%内联
    # ── v6.10 黄泉 ──
    "acheron_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "黄泉E1"},  # 负面目标CR+18%内联
    "acheron_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "黄泉E2"},  # 回合开始+1残梦(trace_tick)
    "acheron_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "黄泉E3"},
    "acheron_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "黄泉E4"},  # 入场敌终结技易伤8%内联
    "acheron_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "黄泉E5"},
    "acheron_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "黄泉E6"},  # 抗穿20%+普攻战技视为终结技内联
    # ── v6.10 飞霄 ──
    "feixiao_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "飞霄E1"},  # 终结技伤害+10%×5层内联
    "feixiao_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "飞霄E2"},  # 每FUA+1飞黄内联
    "feixiao_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "飞霄E3"},
    "feixiao_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "飞霄E4"},  # FUA削韧+100%+速度+8%内联
    "feixiao_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "飞霄E5"},
    "feixiao_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "飞霄E6"},  # 抗穿20%+FUA视为终结技+倍率+140%内联
}


# ═══════════════════════════════════════════════════════════════════
# 效果解析主函数
# ═══════════════════════════════════════════════════════════════════

def resolve_character_effects(
    character: Character,
    lightcone: LightCone | None = None,
    relics: list[RelicPiece] | None = None,
    relic_sets: dict[str, RelicSet] | None = None,
    eidolon_rank: int = 0,
    registry: HookRegistry | None = None,
) -> list[ResolvedEffect]:
    """解析角色的所有行迹/星魂/光锥/遗器效果

    Returns:
        list[ResolvedEffect] — 注册到 HookRegistry 的效果列表
    """
    effects: list[ResolvedEffect] = []
    cid = character.id

    # ── 1. 行迹效果 ──
    for trace in character.traces:
        hn = trace.hook_name
        if not hn or hn not in TRACE_REGISTRY:
            continue
        tmpl = TRACE_REGISTRY[hn]
        if tmpl.get("trigger") and tmpl.get("action"):
            effects.append(ResolvedEffect(
                source="trace",
                source_name=tmpl.get("source_name", trace.name),
                char_id=cid,
                trigger=tmpl["trigger"],
                action=tmpl["action"],
                condition=tmpl.get("condition"),
            ))

    # ── 2. 星魂效果 (选定等级及以下) ──
    for eidolon in character.eidolons:
        if eidolon.rank > eidolon_rank:
            continue
        hn = eidolon.hook_name
        if not hn or hn not in EIDOLON_REGISTRY:
            continue
        tmpl = EIDOLON_REGISTRY[hn]
        if tmpl.get("trigger") and tmpl.get("action"):
            effects.append(ResolvedEffect(
                source="eidolon",
                source_name=tmpl.get("source_name", eidolon.name),
                char_id=cid,
                trigger=tmpl["trigger"],
                action=tmpl["action"],
                condition=tmpl.get("condition"),
            ))

    # ── 3. 光锥效果 (仅命途匹配时生效) ──
    if lightcone and lightcone.path == character.path:
        for lc_eff in lightcone.effects:
            pid = lc_eff.param_id
            if not pid or pid not in LC_EFFECT_REGISTRY:
                continue
            tmpl = LC_EFFECT_REGISTRY[pid]
            effects.append(ResolvedEffect(
                source="lightcone",
                source_name=tmpl.get("source_name", lightcone.name),
                char_id=cid,
                trigger=tmpl["trigger"],
                action=tmpl["action"],
            ))

    # ── 4. 遗器动态效果 ──
    if relics and relic_sets:
        from engine.core.relic_conditions import register_dynamic_relic_effects
        set_counts = {}
        for p in relics:
            set_counts[p.set_name] = set_counts.get(p.set_name, 0) + 1
        for set_name, count in set_counts.items():
            if set_name not in relic_sets:
                continue
            for eff in relic_sets[set_name].effects:
                if count < eff.pieces_required:
                    continue
                condition_str = eff.condition
                if not condition_str:
                    continue
                # 委托给动态条件注册表
                if registry is not None:
                    register_dynamic_relic_effects(registry, cid, condition_str)

    return effects


def _relic_eagle_advance(u, state):
    """翔鹰4件套：终结技后行动提前25%"""
    AV_PER_TURN = 10000.0
    from engine.core.combat_sim import _effective_spd
    advance = (AV_PER_TURN / _effective_spd(u, state)) * 0.25
    u._pending_action_advance = advance
    state.log.append(f'  翔鹰拉条: +{advance:.0f}AV')


def register_team_effects(configs: list[dict], registry: HookRegistry) -> None:
    """为整个队伍解析并注册所有效果到 HookRegistry（在 simulate 中调用）"""
    registry.clear()
    for cfg in configs:
        char = cfg["char"]
        lc = cfg.get("lightcone")
        relics = cfg.get("relics")
        relic_sets = cfg.get("relic_sets")
        eidolon_rank = cfg.get("eidolon", 0)
        effects = resolve_character_effects(
            char, lc, relics, relic_sets, eidolon_rank, registry=registry
        )
        for effect in effects:
            registry.register_effect(effect)
