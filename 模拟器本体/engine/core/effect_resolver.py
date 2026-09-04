"""效果解析器 — 模拟前读取角色/光锥/遗器/星魂，解析为统一效果列表

管线：
  角色 JSON → traces[]/eidolons[]  ─┐
  光锥 JSON → effects[]             ─┤
  遗器套装  → effects[].condition   ─┼→ resolve_character_effects()
                                      │      ↓
                                    └── list[ResolvedEffect] → HookRegistry
"""
from engine.hooks.base import ResolvedEffect, HookRegistry
from engine.characters import (  # M3/M4: 角色包行迹/星魂处理器（按名引用）
    aglaea, changyeyue, evanescia, fengjin, firefly, himeko_nova, huohuo,
    lingsha, robin_summeretto, silver_wolf, sparxie, trailblazer_elation,
    trailblazer_remembrance, acheron, anaxa, bronya, busitu, cerydra, cipher, dan_heng_permansor_terrae,
    feixiao, fu_xuan, fugue, hysilens, mydei, phainon, qianye, robin,
    ruan_mei, seele, sparkle, sunday, the_dahlia, tribbie, welt,
    xiadie, xilian, yaoguang, yinlang,
)
from engine.runtime import _hook_owner  # M3: 自本文件迁出（角色包共用）
from engine.models.character import Character
from engine.models.equipment import LightCone, RelicPiece, RelicSet
from engine.core.combat_engine import _build_effective_stats, _effective_spd, _gain_energy, _gain_skill_points
from engine.runtime import TimedBuff

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


def _trace_elation_sp_recovery(**kwargs):
    """爻光行迹「暴伤增幅」：施放欢愉技后回复1个战技点"""
    state = kwargs['state']
    _gain_skill_points(state)
    state.log.append('  行迹: 欢愉技后回1SP')


# ── 昔涟专属处理器 ──


# ── 阿格莱雅专属处理器 ──


# ── 万敌专属处理器 ──


# ── 阿格莱雅/万敌 星魂 ──


# ── 开拓者·记忆专属处理器 ──


# ── v5.3 开拓者·同谐 ──


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
    gained = _gain_energy(owner, 10, state=state)
    state.log.append(f'  天赋·高空踏歌: 击破回能+{gained:.0f}')


def _eid_tbh_e1(u, state, **kw):
    """开拓者·同谐E1: 施放首次战技后立即回复1点战技点"""
    if u.char.id != 'trailblazer_harmony' or u.extra.get('tbh_e1_used'):
        return
    u.extra['tbh_e1_used'] = True
    _gain_skill_points(state)
    state.log.append('  E1: 首次战技回1战技点')


def _eid_tbh_e2(u, state, **kw):
    """开拓者·同谐E2: 战斗开始时能量恢复效率+25%，持续3回合"""
    if u.char.id != 'trailblazer_harmony':
        return
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


# ── v5.3 灵砂 ──


# ── v5.3 流萤 ──


# ════════════ v6.7 绯英/火花/大丽花 TRACE handlers ════════════


# ════════════ v6.7 EIDOLON handlers ════════════


# ════════════ v6.7 姬子·启行 TRACE/EIDOLON handlers ════════════


# ════════════ v6.9 批1 TRACE handlers（星期日/瓦尔特/阮·梅）════════════


# ════════════ v6.9 批2 TRACE handlers（知更鸟/不死途）════════════


# ════════════ v6.10 黄泉 TRACE handlers ════════════


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
        "action": lambda **kw: trailblazer_elation._trace_tb_cr_and_sp(kw['u'], kw['state'], skill_key=kw.get('skill_key')),
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
            huohuo._huohuo_ruming_gain_local(kw['state'], kw['u'], 2),
            kw['state'].log.append('[Init] 藿藿开局+30能量 + 禳命2回合')
        ),
        "source_name": "行迹·战斗开始",
    },
    "huohuo_control_resist_and_atk": {
        "trigger": "on_enter_battle", "action": huohuo._trace_huohuo_control_resist, "source_name": "行迹·控制抵抗+攻击",
    },
    "huohuo_energy_cycle": {
        "trigger": None, "action": None, "source_name": "行迹·回合回能(禳命治疗内联)",
    },
    # 布洛妮娅
    "bronya_basic_crit_100": {
        "trigger": "on_basic_attack",
        "action": bronya._trace_bronya_basic_crit,
        "source_name": "行迹·号令",
    },
    "bronya_battle_start_def": {
        "trigger": "on_enter_battle",
        "action": bronya._trace_bronya_battle_def,
        "source_name": "行迹·阵地",
    },
    "bronya_team_dmg_bonus": {
        "trigger": "on_enter_battle", "action": bronya._trace_bronya_team_dmg, "source_name": "行迹·军势",
    },
    # v6.3.0 银狼（普通, silver_wolf）
    # v6.3.0b P1-8: 改 on_any_weakness_break（attacker-only 事件不带 enemy, 队友击破/自身击破均漏触发）
    # v6.6 批1: 缇宝/刻律德菈/丹恒·腾荒
    "phainon_trace1": {
        "trigger": "on_enter_battle",
        "action": phainon._trace_phainon_trace1,
        "source_name": "行迹·终点（开局+1火种）",
    },
    "phainon_trace3": {
        "trigger": "on_enter_battle",
        "action": phainon._trace_phainon_trace3,
        "source_name": "行迹·本色（进战ATK+50%）",
    },
    "hysilens_trace1": {
        "trigger": "on_enter_battle",
        "action": hysilens._trace_hysilens_trace1,
        "source_name": "行迹·剑旗（开局结界+回SP）",
    },
    "hysilens_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·泡沫（终结技DOT引爆，_use_skill内联）",
    },
    "hysilens_trace3": {
        "trigger": "on_enter_battle",
        "action": hysilens._trace_hysilens_trace3,
        "source_name": "行迹·琴弦（EHR→增伤）",
    },
    "hysilens_base": {
        "trigger": None, "action": None,
        "source_name": "基础行迹（数据面板属性）",
    },
    "anaxa_trace2": {
        "trigger": "on_enter_battle",
        "action": anaxa._trace_anaxa_trace2,
        "source_name": "行迹·留白（智识数量）",
    },
    "anaxa_trace1": {
        "trigger": "on_basic_attack",
        "action": anaxa._trace_anaxa_basic_energy,
        "source_name": "行迹·流浪的能指（普攻额外回能）",
    },
    "anaxa_trace1_turn": {
        "trigger": "on_turn_start",
        "action": anaxa._trace_anaxa_turn_energy,
        "source_name": "行迹·流浪的能指（无揭露回能）",
    },
    "anaxa_trace3": {
        "trigger": "on_enter_battle",
        "action": anaxa._trace_anaxa_trace3,
        "source_name": "行迹·嬗变（每弱点无视防御）",
    },
    "cipher_trace3": {
        "trigger": "on_enter_battle",
        "action": cipher._trace_cipher_trace3,
        "source_name": "行迹·偷天换日（FUA暴伤+敌受伤）",
    },
    "tribbie_trace1": {
        "trigger": None,
        "action": None,
        "source_name": "行迹·城墙外的羊羔儿（FUA后增伤72%×3层, _tribbie_talent_fua 内联 TimedBuff; v6.8.1 修正: 此前挂 on_enter_battle 开局误触发且层数无消费）",
    },
    "tribbie_trace3": {
        "trigger": "on_enter_battle",
        "action": tribbie._trace_tribbie_trace3,
        "source_name": "行迹·小石子（开局回30能量）",
    },
    "cerydra_trace1": {
        "trigger": "on_enter_battle",
        "action": cerydra._trace_cerydra_trace1,
        "source_name": "行迹·来者（ATK→暴伤）",
    },
    "cerydra_trace2": {
        "trigger": "on_enter_battle",
        "action": cerydra._trace_cerydra_trace2,
        "source_name": "行迹·见者（暴击率+100%）",
    },
    "dht_trace2": {
        "trigger": "on_enter_battle",
        "action": dan_heng_permansor_terrae._trace_dht_trace2,
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
        "action": silver_wolf._trace_silver_wolf_gen,
        "source_name": "行迹·生成（缺陷+1回合, 击破植入）",
    },
    "silver_wolf_trace2_inject": {
        "trigger": "on_enter_battle",
        "action": silver_wolf._trace_silver_wolf_inject_start,
        "source_name": "行迹·注入（战斗开始回20能量）",
    },
    "silver_wolf_trace2_turn": {
        "trigger": "on_turn_start",
        "action": silver_wolf._trace_silver_wolf_inject_turn,
        "source_name": "行迹·注入（回合开始回5能量）",
    },
    "silver_wolf_trace3_annotate": {
        "trigger": "on_enter_battle",
        "action": silver_wolf._trace_silver_wolf_annotate,
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
        "action": seele._trace_seele_ripple,
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
        "action": sparkle._trace_sparkle_sp_limit,
        "source_name": "天赋·叙述性诡计(战技点上限)",
    },
    "sparkle_turn_end": {
        "trigger": "on_turn_end",
        "action": sparkle._trace_sparkle_turn_end,
        "source_name": "终结技记录·回合结束补SP",
    },
    # 符玄
    "fuxuan_cc_resist": {
        "trigger": "on_skill", "action": fu_xuan._trace_fuxuan_cc_resist, "source_name": "行迹·控制抵抗",
    },
    "fuxuan_energy_regen": {
        "trigger": "on_turn_start", "action": fu_xuan._trace_fuxuan_energy_regen,
        "condition": lambda **kw: kw['state'].extra.get('fuxuan_field_turns', 0) > 0,
        "source_name": "行迹·能量恢复",
    },
    "fuxuan_ult_heal": {
        "trigger": "on_ultimate",
        "action": fu_xuan._trace_fuxuan_ult_heal,
        "source_name": "行迹·终结技回血",
    },
    # 长夜月
    "changyeyue_trace1_memory_count_cd": {
        "trigger": None, "action": None, "source_name": "行迹·记忆数量→CD",
    },
    "changyeyue_trace2_cr_and_cd": {
        "trigger": "on_after_skill",
        "action": lambda **kw: changyeyue._changyeyue_trace2(kw['u'], kw['state']),
        "source_name": "行迹·天黑黑月寂寂",
    },
    "changyeyue_trace3_energy_and_yizhi": {
        "trigger": "on_after_skill",
        "action": lambda **kw: changyeyue._changyeyue_trace3(kw['u'], kw['state'], kw.get('skill_key')),
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
            xiadie._xiadie_heal_to_xinrui(state, targets, heal_amt)
        ),
        "source_name": "行迹·收容的暗潮",
    },
    # 昔涟
    "xilian_trace1_speed_to_pen": {
        "trigger": "on_enter_battle",
        "action": xilian._xilian_trace1_speed_pen,
        "source_name": "行迹·三相的因果",
    },
    "xilian_trace2_memsprite_future": {
        "trigger": "on_memsprite_summon",
        "action": xilian._xilian_trace2_memsprite_future,
        "source_name": "行迹·记忆的净子",
    },
    "xilian_trace3_start_zhuiyi": {
        "trigger": "on_enter_battle",
        "action": xilian._xilian_trace3_start_zhuiyi,
        "source_name": "行迹·岁月的旅人",
    },
    # 阿格莱雅
    "aglaea_trace1_start_energy": {
        "trigger": "on_enter_battle",
        "action": aglaea._aglaea_trace1_start_energy,
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
        "action": mydei._mydei_trace1_blood_armor,
        "source_name": "行迹·血祥罩衫",
    },
    "mydei_trace2_debt_retain": {
        "trigger": "on_enter_battle",
        "action": mydei._mydei_trace2_debt_retain,
        "source_name": "行迹·水与泥土",
    },
    "mydei_trace3_control_immune": {
        "trigger": "on_enter_battle",
        "action": mydei._mydei_trace3_control_immune,
        "source_name": "行迹·三十僭主",
    },
    # 开拓者·记忆
    "tbr_trace2_scepter": {
        "trigger": "on_enter_battle",
        "action": trailblazer_remembrance._tbr_trace2_scepter,
        "source_name": "行迹·追念之权杖",
    },
    # v6.2.1: 以下三行迹为引擎内联实现（combat_engine/remembrance 直判），
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
        "action": fengjin._trace_fengjin_t1,
        "source_name": "行迹·暴风停歇",
    },
    "fengjin_trace2_cr_heal": {
        "trigger": "on_enter_battle",
        "action": fengjin._trace_fengjin_t2,
        "source_name": "行迹·阴云莞尔",
    },
    "fengjin_trace3_cleanse": {
        "trigger": "on_enter_battle",
        "action": fengjin._trace_fengjin_t3,
        "source_name": "行迹·雷雨轻柔",
    },
    # v6.11.1 知更鸟·晴歌
    "qingge_trace1_cr": {
        "trigger": "on_enter_battle",
        "action": robin_summeretto._trace_qingge_cr,
        "source_name": "行迹·重构谐乐",
    },
    "qingge_trace2_rhythm": {
        "trigger": "on_heal",
        "action": robin_summeretto._trace_qingge_rhythm,
        "source_name": "行迹·即兴蓝调（护盾侧由 combat_engine on_shield 内联同一处理）",
    },
    "qingge_trace3_chord": {
        "trigger": None, "action": None,
        "source_name": "行迹·偏离和弦（内联: _qingge_atmo_from_action→_qingge_trace3）",
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
        "action": fugue._fugue_cloudfire_apply,
        "source_name": "天赋·盈后福，德气流布(云火昭)",
    },
    "fugue_talent_cloudfire_wave": {
        "trigger": "on_wave_start",
        "action": fugue._fugue_cloudfire_apply,
        "source_name": "天赋·盈后福，德气流布(云火昭波次重挂)",
    },
    "fugue_talent_cloudfire_death": {
        "trigger": "on_ally_death",
        "action": fugue._fugue_cloudfire_death,
        "source_name": "天赋·盈后福(云火昭失效)",
    },
    "fugue_talent_def_down": {
        "trigger": "on_ally_attack",
        "action": fugue._fugue_foxian_def_down,
        "source_name": "天赋·盈后福，德气流布(狐祈DEF-18%)",
    },
    "fugue_trace1_break_delay": {
        "trigger": "on_any_weakness_break",
        "action": fugue._fugue_trace1_break_delay,
        "source_name": "行迹·青丘重光",
    },
    "fugue_trace2_team_be": {
        "trigger": "on_any_weakness_break",
        "action": fugue._fugue_trace2_team_be,
        "source_name": "行迹·玑星太素",
    },
    "fugue_trace3_self_be": {
        "trigger": "on_enter_battle",
        "action": fugue._fugue_trace3_self_be,
        "source_name": "行迹·涂山玄设",
    },
    "fugue_trace3_first_sp": {
        "trigger": "on_skill",
        "action": fugue._fugue_trace3_first_sp,
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
        "action": lingsha._trace_lingsha_t2_be_to_atk_heal,
        "source_name": "行迹·朱燎",
    },
    "lingsha_trace3_fuyuan_pursuit": {
        "trigger": "on_hp_loss",
        "action": lingsha._trace_lingsha_t3_pursuit,
        "source_name": "行迹·遗爇",
    },
    # v5.3 流萤
    "firefly_trace1_combustion_pull": {
        "trigger": "on_any_weakness_break",
        "action": firefly._trace_firefly_t1_pull,
        "source_name": "行迹·偏时迸发(倒计时延后)",
    },
    "firefly_trace2_super_break": {
        # 行迹2·自限装甲: 燃烧下BE≥150%/300%→超击破100%/150% — 引擎内联(_super_break_rate)
        "trigger": None, "action": None, "source_name": "行迹·自限装甲",
    },
    "firefly_trace3_atk_to_be": {
        "trigger": "on_enter_battle",
        "action": firefly._trace_firefly_t3_atk_to_be,
        "source_name": "行迹·过载核心",
    },
    "firefly_talent_start": {
        "trigger": "on_enter_battle",
        "action": firefly._trace_firefly_talent_start,
        "source_name": "天赋·源火中枢(开局能量)",
    },
    "firefly_talent_cleanse": {
        "trigger": "on_energy_change",
        "action": firefly._trace_firefly_talent_cleanse,
        "source_name": "天赋·源火中枢(满能清负面)",
    },
    "firefly_talent_dr_hp_loss": {
        "trigger": "on_hp_loss",
        "action": firefly._trace_firefly_dr_hp_loss,
        "source_name": "天赋·源火中枢(减伤曲线)",
    },
    "firefly_talent_dr_turn": {
        "trigger": "on_turn_start",
        "action": firefly._trace_firefly_dr_turn,
        "source_name": "天赋·源火中枢(减伤曲线回合刷新)",
    },
    # ── v6.7 绯英（角色技能介绍/欢愉/绯英.txt）──
    "evanescia_energy_convert": {
        "trigger": "on_energy_change",
        "action": evanescia._trace_evanescia_energy_convert,
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
        "action": evanescia._trace_evanescia_trace3_cr,
        "source_name": "行迹·瞰众乐(暴击率+30%永久; 弹射/转移内联)",
    },
    "evanescia_base": {
        "trigger": "on_enter_battle",
        "action": evanescia._trace_evanescia_talent_elation,
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
        "action": the_dahlia._trace_dahlia_trace1_open,
        "source_name": "行迹·又一场葬礼(开战队友BE转移)",
    },
    "the_dahlia_trace1_heal": {
        "trigger": "on_heal",
        "action": the_dahlia._trace_dahlia_trace1_reapply,
        "source_name": "行迹·又一场葬礼(受治疗再触发3回合)",
    },
    "the_dahlia_trace1_shield": {
        "trigger": "on_shield",
        "action": the_dahlia._trace_dahlia_trace1_reapply,
        "source_name": "行迹·又一场葬礼(受护盾再触发3回合)",
    },
    "the_dahlia_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·致哀故人(FUA每2次回SP, FUA内联)",
    },
    "the_dahlia_trace3": {
        "trigger": "on_weakness_implant",
        "action": the_dahlia._trace_dahlia_trace3_implant,
        "source_name": "行迹·弃旧恋新(添弱点加速+火固定削韧)",
    },
    "the_dahlia_field_tick": {
        "trigger": "on_turn_start",
        "action": the_dahlia._trace_dahlia_field_tick,
        "source_name": "结界回合递减+FUA重置",
    },
    "the_dahlia_base": {
        "trigger": None, "action": None, "source_name": "基础行迹",
    },
    # ── v6.7 姬子·启行（角色技能介绍/智识/姬子·启行.txt）──
    "himeko_nova_protocol": {
        "trigger": "on_enter_battle",
        "action": himeko_nova._trace_hn_protocol,
        "source_name": "天赋·同行协议(裁决/歼破判定)",
    },
    "himeko_nova_trace1": {
        # v7.2.0 #1: 此前 on_turn_start 处理器挂在 JSON 未注册的 'himeko_nova_flag_regen'
        # 上导致从未生效——现直接挂 trace1(人类该向何处去): 旗语每回合+1次(E2额外+1)
        # + 次数=上限时回5能量
        "trigger": "on_turn_start",
        "action": himeko_nova._trace_hn_flag_regen,
        "source_name": "行迹·人类该向何处去(旗语回合恢复/次数满回5能量)",
    },
    "himeko_nova_trace2": {
        "trigger": None, "action": None,
        "source_name": "行迹·列车的脉搏在轰鸣(额外回合, support_skill内联)",
    },
    "himeko_nova_trace3": {
        "trigger": None, "action": None,
        "source_name": "行迹·银轨在旷古中静默(终结技+3源能/脉冲强化, ultimate内联)",
    },
    "himeko_nova_base": {
        "trigger": None, "action": None, "source_name": "基础行迹",
    },
    # ── v6.9 批1 星期日/瓦尔特/阮·梅 ──
    "sunday_trace1": {"trigger": None, "action": None, "source_name": "行迹·主日渴慕(终结技回能补40, ult内联)"},
    "sunday_trace2": {"trigger": "on_enter_battle", "action": sunday._trace_sunday_trace2, "source_name": "行迹·崇高拂尘(开局25能)"},
    "sunday_trace3": {"trigger": None, "action": None, "source_name": "行迹·掌中安港(战技净化, skill内联)"},
    "sunday_trace_tick": {"trigger": "on_turn_start", "action": sunday._trace_sunday_tick, "source_name": "蒙福者倒计时+E4回能"},
    "sunday_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "welt_trace1": {"trigger": "on_enter_battle", "action": welt._trace_welt_trace1, "source_name": "行迹·惩戒(开局30能)"},
    "welt_trace2": {"trigger": None, "action": None, "source_name": "行迹·审判(普攻/战技附加, 内联)"},
    "welt_trace3": {"trigger": None, "action": None, "source_name": "行迹·裁决(EHR→ATK, 内联)"},
    "welt_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "ruan_mei_trace1": {"trigger": "on_enter_battle", "action": ruan_mei._trace_ruanmei_trace1, "source_name": "行迹·物体呼吸中(全队BE+20%)"},
    "ruan_mei_trace2": {"trigger": None, "action": None, "source_name": "行迹·日消遐思长(回合回5能, tick内联)"},
    "ruan_mei_trace3": {"trigger": None, "action": None, "source_name": "行迹·落烛照水燃(BE阈值增伤, 内联)"},
    "ruan_mei_field_tick": {"trigger": "on_turn_start", "action": ruan_mei._trace_ruanmei_tick, "source_name": "结界递减+行迹2回能"},
    "ruan_mei_break_damage": {"trigger": "on_any_weakness_break", "action": ruan_mei._trace_ruanmei_break, "source_name": "天赋·分型的螺旋(击破冰伤)"},
    "ruan_mei_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.9 批2 知更鸟/不死途 ──
    "robin_trace1": {"trigger": None, "action": None, "source_name": "行迹·即兴装饰(协奏期FUA暴伤, 内联)"},
    "robin_trace2": {"trigger": "on_enter_battle", "action": robin._trace_robin_trace2, "source_name": "行迹·华彩花腔(开局拉条25%)"},
    "robin_trace3": {"trigger": None, "action": None, "source_name": "行迹·模进乐段(战技额外5能, 内联)"},
    "robin_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    "busitu_trace1": {"trigger": None, "action": None, "source_name": "行迹·罪途(婪酣获取, 内联)"},
    "busitu_trace2": {"trigger": None, "action": None, "source_name": "行迹·影肢(FUA增伤, 内联)"},
    "busitu_trace3": {"trigger": "on_enter_battle", "action": busitu._trace_busitu_trace3, "source_name": "行迹·头狼(全队暴伤)"},
    "busitu_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.9 批3 千冶·刃 ──
    "qianye_trace1": {"trigger": "on_enter_battle", "action": qianye._trace_qianye_trace1, "source_name": "行迹·百炼骨(开局75%能量; 溢出/净化内联)"},
    "qianye_trace2": {"trigger": None, "action": None, "source_name": "行迹·千锻魂(受击率/减伤/受击充能, 内联)"},
    "qianye_trace3": {"trigger": None, "action": None, "source_name": "行迹·万淬心(全队伤害/终结技, 内联)"},
    "qianye_base": {"trigger": None, "action": None, "source_name": "基础行迹"},
    # ── v6.10 黄泉 ──
    "acheron_trace1": {"trigger": "on_enter_battle", "action": acheron._trace_acheron_trace1, "source_name": "行迹·赤鬼(开局5残梦+集真赤)"},
    "acheron_trace2": {"trigger": None, "action": None, "source_name": "行迹·奈落(虚无队友倍率, 面板守卫)"},
    "acheron_trace3": {"trigger": None, "action": None, "source_name": "行迹·雷心(增伤叠层/返渡额外段, ult内联)"},
    "acheron_trace_tick": {"trigger": "on_turn_start", "action": acheron._trace_acheron_tick, "source_name": "E2回合开始+1残梦"},
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
        _gain_skill_points(state)
        state.extra['lc_sp_counter'] = 0
        state.log.append('  光锥回SP')

def _lc_team_advance(state, ratio, actor=None):
    AV_PER_TURN = 10000.0
    from engine.characters.robin_summeretto import _guest_advance_blocked
    navs = state.extra.get('navs', {})
    for i, eu in enumerate(state.units):
        if eu.is_alive and i in navs and not robin_summeretto._guest_advance_blocked(state, actor, eu):
            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * ratio)
    state.log.append(f'  光锥拉条: 全队{ratio*100:.0f}%')

def _lc_ally_buff(state, unit, attrs, duration):
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
        "action": lambda **kw: _lc_team_advance(kw['state'], 0.24, actor=kw.get('u')),
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


# 希儿E1(目标条件)/E2(叠层)/E6(乱蝶) 效果在战斗引擎内联实现(eidolon_rank直判), 注册表保留 None 作文档


def _eid_skill_levels(u, state, **kw):
    """E3/E5: 技能等级提升——解析角色星魂声明文本构建技能等级覆盖表
    v6.10.3 P1-6: 此前简化"全伤害+6%"且永久改面板, 导致未升级技能也吃加成;
    现在按星魂声明提升对应技能倍率/治疗护盾数值（每级+5%, _use_skill 消费）"""
    import re as _re
    # v7.0.0 A2: 忆灵天赋/忆灵技排最前(最长优先), 防'忆灵天赋+1'误读为'天赋+1'
    _SKILL_KEY = {'普攻': 'basic_attack', '战技': 'skill', '终结技': 'ultimate',
                  '天赋': 'talent', '欢愉技': 'elation_skill',
                  '忆灵天赋': 'memsprite_talent', '忆灵技': 'memsprite_skill'}
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
        for m in _re.finditer(r'(忆灵天赋|忆灵技|普攻|战技|终结技|天赋|欢愉技)(?:等级)?\+(\d+)', desc):
            sk = _SKILL_KEY[m.group(1)]
            boost[sk] = boost.get(sk, 0) + int(m.group(2))
    u.extra['skill_level_boost'] = boost


# ── 长夜月专属处理器 ──


# ── 遐蝶专属处理器 ──


EIDOLON_REGISTRY: dict[str, dict] = {
    # 爻光
    "yaoguang_e1": {"trigger": "on_enter_battle", "action": yaoguang._eid_yaoguang_e1, "source_name": "爻光E1"},
    "yaoguang_e2": {"trigger": "on_turn_start",   "action": yaoguang._eid_yaoguang_e2, "source_name": "爻光E2"},
    "yaoguang_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "爻光E3"},
    "yaoguang_e4": {"trigger": "on_enter_battle", "action": yaoguang._eid_yaoguang_e4, "source_name": "爻光E4"},
    "yaoguang_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "爻光E5"},
    "yaoguang_e6": {"trigger": "on_enter_battle", "action": yaoguang._eid_yaoguang_e6, "source_name": "爻光E6"},
    # 银狼
    "yinlang_e1": {"trigger": "on_enter_battle",  "action": yinlang._eid_yinlang_e1, "source_name": "银狼E1"},
    "yinlang_e2": {"trigger": "on_enter_battle",  "action": yinlang._eid_yinlang_e2, "source_name": "银狼E2"},
    "yinlang_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "银狼E3"},
    "yinlang_e4": {"trigger": "on_enter_battle",  "action": yinlang._eid_yinlang_e4, "source_name": "银狼E4"},
    "yinlang_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "银狼E5"},
    "yinlang_e6": {"trigger": "on_enter_battle",  "action": yinlang._eid_yinlang_e6, "source_name": "银狼E6"},
    # 海瑟音星魂效果由 combat_engine 的DOT/结界生命周期统一消费，注册表保留
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
    "seele_e4": {"trigger": "on_kill",           "action": seele._eid_seele_e4, "source_name": "希儿E4"},
    "seele_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "希儿E5"},
    "seele_e6": {"trigger": "on_ultimate",       "action": None, "source_name": "希儿E6"},  # 引擎内联(乱蝶)
    # 布洛妮娅
    "bronya_e1": {"trigger": "on_skill",         "action": bronya._eid_bronya_e1, "source_name": "布洛妮娅E1"},
    "bronya_e2": {"trigger": "on_skill",         "action": bronya._eid_bronya_e2, "source_name": "布洛妮娅E2"},
    "bronya_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "布洛妮娅E3"},
    "bronya_e4": {"trigger": "on_ally_attack",    "action": bronya._eid_bronya_e4, "source_name": "布洛妮娅E4"},
    "bronya_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "布洛妮娅E5"},
    "bronya_e6": {"trigger": "on_skill",         "action": bronya._eid_bronya_e6, "source_name": "布洛妮娅E6"},
    # 花火
    "sparkle_e1": {"trigger": "on_enter_battle",  "action": sparkle._eid_sparkle_e1, "source_name": "花火E1"},
    "sparkle_e2": {"trigger": "on_enter_battle",  "action": sparkle._eid_sparkle_e2, "source_name": "花火E2"},
    "sparkle_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "花火E3"},
    "sparkle_e4": {"trigger": "on_ultimate",      "action": sparkle._eid_sparkle_e4, "source_name": "花火E4"},
    "sparkle_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "花火E5"},
    "sparkle_e6": {"trigger": "on_skill",         "action": sparkle._eid_sparkle_e6, "source_name": "花火E6"},
    # 藿藿
    "huohuo_e1": {"trigger": "on_enter_battle",   "action": huohuo._eid_huohuo_e1, "source_name": "藿藿E1"},
    "huohuo_e2": {"trigger": "on_enter_battle",   "action": huohuo._eid_huohuo_e2, "source_name": "藿藿E2"},
    "huohuo_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "藿藿E3"},
    "huohuo_e4": {"trigger": "on_enter_battle",   "action": None, "source_name": "藿藿E4"},
    "huohuo_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "藿藿E5"},
    "huohuo_e6": {"trigger": "on_heal",           "action": huohuo._eid_huohuo_e6, "source_name": "藿藿E6"},
    # 符玄
    "fuxuan_e1": {"trigger": "on_enter_battle",   "action": fu_xuan._eid_fuxuan_e1, "source_name": "符玄E1"},
    "fuxuan_e2": {"trigger": "on_enter_battle",   "action": fu_xuan._eid_fuxuan_e2, "source_name": "符玄E2"},
    "fuxuan_e3": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "符玄E3"},
    "fuxuan_e4": {"trigger": "on_take_damage",    "action": fu_xuan._eid_fuxuan_e4, "source_name": "符玄E4"},
    "fuxuan_e5": {"trigger": "on_enter_battle",   "action": _eid_skill_levels, "source_name": "符玄E5"},
    "fuxuan_e6": {"trigger": "on_hp_loss",        "action": fu_xuan._eid_fuxuan_e6_loss, "source_name": "符玄E6"},
    # 开拓者·欢愉
    "trailblazer_elation_e1": {"trigger": "on_skill",        "action": trailblazer_elation._eid_tb_elation_e1, "source_name": "开拓者E1"},
    "trailblazer_elation_e2": {"trigger": "on_ultimate",     "action": trailblazer_elation._eid_tb_elation_e2, "source_name": "开拓者E2"},
    "trailblazer_elation_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者E3"},
    "trailblazer_elation_e4": {"trigger": "on_elation_skill", "action": trailblazer_elation._eid_tb_elation_e4, "source_name": "开拓者E4"},
    "trailblazer_elation_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "开拓者E5"},
    "trailblazer_elation_e6": {"trigger": "on_elation_skill", "action": trailblazer_elation._eid_tb_elation_e6, "source_name": "开拓者E6"},
    # 开拓者·记忆（v5.7: E1内联于_tbr_support_skill, E2 hook注册, E4/E6内联于_use_skill）
    "tbr_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "开拓者·记忆E1(声援CR+10%/对忆灵生效)"},
    "tbr_e2": {"trigger": "on_memsprite_attack", "action": trailblazer_remembrance._eid_tbr_e2, "source_name": "开拓者·记忆E2"},
    "tbr_e2_reset": {"trigger": "on_turn_start", "action": trailblazer_remembrance._eid_tbr_e2_reset, "source_name": "开拓者·记忆E2(回合重置)"},
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
    "xiadie_e4": {"trigger": "on_heal",          "action": xiadie._eid_xiadie_e4, "source_name": "遐蝶E4"},
    "xiadie_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "遐蝶E5"},
    "xiadie_e6": {"trigger": "on_enter_battle", "action": xiadie._eid_xiadie_e6, "source_name": "遐蝶E6"},
    # 昔涟
    "xilian_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E1"},  # 引擎内联(弹射)
    "xilian_e2": {"trigger": "on_enter_battle", "action": xilian._eid_xilian_e2, "source_name": "昔涟E2"},
    "xilian_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "昔涟E3"},
    "xilian_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E4"},
    "xilian_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "昔涟E5"},
    "xilian_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "昔涟E6"},
    # 阿格莱雅
    "aglaea_e1": {"trigger": "on_enter_battle", "action": aglaea._eid_aglaea_e1, "source_name": "阿格莱雅E1"},
    "aglaea_e2": {"trigger": "on_enter_battle", "action": aglaea._eid_aglaea_e2, "source_name": "阿格莱雅E2"},
    "aglaea_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阿格莱雅E3"},
    "aglaea_e4": {"trigger": "on_enter_battle", "action": aglaea._eid_aglaea_e4, "source_name": "阿格莱雅E4"},
    "aglaea_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "阿格莱雅E5"},
    "aglaea_e6": {"trigger": "on_enter_battle", "action": aglaea._eid_aglaea_e6, "source_name": "阿格莱雅E6"},
    # 万敌
    "mydei_e1": {"trigger": "on_enter_battle", "action": mydei._eid_mydei_e1, "source_name": "万敌E1"},
    "mydei_e2": {"trigger": "on_enter_battle", "action": mydei._eid_mydei_e2, "source_name": "万敌E2"},
    "mydei_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "万敌E3"},
    "mydei_e4": {"trigger": "on_enter_battle", "action": mydei._eid_mydei_e4, "source_name": "万敌E4"},
    "mydei_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "万敌E5"},
    "mydei_e6": {"trigger": "on_enter_battle", "action": mydei._eid_mydei_e6, "source_name": "万敌E6"},
    # 风堇
    "fengjin_e1": {"trigger": "on_after_skill",       "action": fengjin._eid_fengjin_e1, "source_name": "风堇E1"},
    "fengjin_e2": {"trigger": "on_hp_loss",           "action": fengjin._eid_fengjin_e2, "source_name": "风堇E2"},
    "fengjin_e3": {"trigger": "on_enter_battle",      "action": _eid_skill_levels, "source_name": "风堇E3"},
    "fengjin_e4": {"trigger": "on_enter_battle",      "action": fengjin._eid_fengjin_e4, "source_name": "风堇E4"},
    "fengjin_e5": {"trigger": "on_enter_battle",      "action": _eid_skill_levels, "source_name": "风堇E5"},
    "fengjin_e6": {"trigger": "on_memsprite_summon",  "action": fengjin._eid_fengjin_e6, "source_name": "风堇E6"},
    # v6.11.1 知更鸟·晴歌
    "qingge_e1": {"trigger": "on_after_damage",        "action": robin_summeretto._eid_qingge_e1_record, "source_name": "晴歌E1"},
    "qingge_e2": {"trigger": "on_enter_battle",        "action": robin_summeretto._eid_qingge_e2, "source_name": "晴歌E2"},
    "qingge_e3": {"trigger": "on_enter_battle",        "action": _eid_skill_levels, "source_name": "晴歌E3"},
    "qingge_e4": {"trigger": None, "action": None, "source_name": "晴歌E4（内联: 进Fever+12气氛/晴空乐手速度, combat_engine._qingge_enter_fever）"},
    "qingge_e5": {"trigger": "on_enter_battle",        "action": _eid_skill_levels, "source_name": "晴歌E5"},
    "qingge_e6": {"trigger": None, "action": None, "source_name": "晴歌E6（内联: 忆灵技倍率×2/Fever存2次终结技/回140能量）"},
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
    "fugue_e2": {"trigger": "on_any_weakness_break", "action": fugue._eid_fugue_e2_energy, "source_name": "忘归人E2"},
    "fugue_e2_ult": {"trigger": "on_ultimate", "action": fugue._eid_fugue_e2_ult, "source_name": "忘归人E2(终结技拉条)"},
    "fugue_e3": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "忘归人E3"},
    "fugue_e4": {"trigger": "on_enter_battle",  "action": None, "source_name": "忘归人E4"},  # 引擎内联(狐祈者击破伤害×1.2)
    "fugue_e5": {"trigger": "on_enter_battle",  "action": _eid_skill_levels, "source_name": "忘归人E5"},
    "fugue_e6": {"trigger": "on_enter_battle",  "action": None, "source_name": "忘归人E6"},  # 引擎内联(自身击破效率×1.5/狐祈全队)
    # v5.3 灵砂
    "lingsha_e1": {"trigger": "on_any_weakness_break", "action": lingsha._eid_lingsha_e1_break, "source_name": "灵砂E1"},
    "lingsha_e2": {"trigger": "on_ultimate", "action": lingsha._eid_lingsha_e2_ult, "source_name": "灵砂E2"},
    "lingsha_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "灵砂E3"},
    "lingsha_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "灵砂E4"},  # marker内联(浮元行动治疗最低HP)
    "lingsha_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "灵砂E5"},
    "lingsha_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "灵砂E6"},  # MARKER_SPAWN内联(全抗-20%/额外4次)
    # v5.3 流萤
    "firefly_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E1"},  # 引擎内联(强化战技不耗SP+无视15%防御)
    "firefly_e2_kill": {"trigger": "on_kill", "action": firefly._eid_firefly_e2_kill, "source_name": "流萤E2(击杀)"},
    "firefly_e2_break": {"trigger": "on_any_weakness_break", "action": firefly._eid_firefly_e2_break, "source_name": "流萤E2(击破)"},
    "firefly_e2_reset": {"trigger": "on_turn_start", "action": firefly._eid_firefly_e2_reset, "source_name": "流萤E2(回合重置)"},
    "firefly_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "流萤E3"},
    "firefly_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E4"},  # 燃烧状态机内联(效果抵抗+50%)
    "firefly_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "流萤E5"},
    "firefly_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "流萤E6"},  # 燃烧状态机内联(火抗穿20%/击破效率)
    # ── v6.7 绯英 ──
    "evanescia_e1": {"trigger": "on_enter_battle", "action": evanescia._eid_evanescia_e1, "source_name": "绯英E1(全抗穿透20%)"},
    "evanescia_e2": {"trigger": "on_enter_battle", "action": evanescia._eid_evanescia_e2, "source_name": "绯英E2(暴伤36%+好活乘区内联)"},
    "evanescia_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "绯英E3"},
    "evanescia_e4": {"trigger": "on_enter_battle", "action": evanescia._eid_evanescia_e4, "source_name": "绯英E4(无视15%防御)"},
    "evanescia_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "绯英E5"},
    "evanescia_e6": {"trigger": "on_enter_battle", "action": None, "source_name": "绯英E6"},  # 引擎内联(好活持续+1/欢愉伤害/首终结技回能)
    # ── v6.7 火花 ──
    "sparxie_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E1"},  # eff_stats内联(阿哈+5笑点/每笑点抗穿)
    "sparxie_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E2"},  # 阿哈内联(额外回合+爆点) + 扣费内联(暴伤)
    "sparxie_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "火花E3"},
    "sparxie_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "火花E4"},  # ultimate内联(+5笑点+欢愉度36%)
    "sparxie_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "火花E5"},
    "sparxie_e6": {"trigger": "on_enter_battle", "action": sparxie._eid_sparxie_e6, "source_name": "火花E6(抗穿20%+弹射内联)"},
    # ── v6.7 大丽花 ──
    "the_dahlia_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "大丽花E1"},  # 超击破/固定削韧内联
    "the_dahlia_e2": {"trigger": "on_enter_battle", "action": the_dahlia._eid_dahlia_e2, "source_name": "大丽花E2(全抗-20%+败谢)"},
    "the_dahlia_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "大丽花E3"},
    "the_dahlia_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "大丽花E4"},  # FUA内联(+5次+受伤12%)
    "the_dahlia_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "大丽花E5"},
    "the_dahlia_e6": {"trigger": "on_enter_battle", "action": the_dahlia._eid_dahlia_e6, "source_name": "大丽花E6(共舞者BE+150%)"},
    # ── v6.7 姬子·启行 ──
    "himeko_nova_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E1"},  # 内联(裁决-1/歼破-3/弹射+1)
    "himeko_nova_e2": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E2"},  # 内联(上限2/伤害×130%)
    "himeko_nova_e3": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "姬子·启行E3"},
    "himeko_nova_e4": {"trigger": "on_enter_battle", "action": None, "source_name": "姬子·启行E4"},  # 内联(助战技全队抗穿)
    "himeko_nova_e5": {"trigger": "on_enter_battle", "action": _eid_skill_levels, "source_name": "姬子·启行E5"},
    "himeko_nova_e6": {"trigger": "on_enter_battle", "action": himeko_nova._eid_hn_e6, "source_name": "姬子·启行E6(火抗穿20%)"},
    # v6.10.3 赛飞儿：FUA/E2/E4/E6 复杂效果在 combat_engine 内联（_cipher_attack_aftermath 等）,
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
    # v6.10.2 那刻夏/刻律德菈/丹恒·腾荒/白厄：复杂效果在 combat_engine 内联，
    # 这里保留完整星魂注册，避免解析器把 TXT 星魂静默丢弃。
    "anaxa_e1": {"trigger": "on_enter_battle", "action": None, "source_name": "那刻夏E1"},
    "anaxa_e2": {"trigger": "on_enter_battle", "action": anaxa._eid_anaxa_e2, "source_name": "那刻夏E2"},
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
    "phainon_e6": {"trigger": "on_enter_battle", "action": phainon._eid_phainon_e6, "source_name": "白厄E6"},
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
    "busitu_e1": {"trigger": "on_enter_battle", "action": busitu._trace_busitu_e1, "source_name": "不死途E1(全敌受伤24%)"},
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
