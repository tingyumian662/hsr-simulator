"""角色包（L2）——每角色一文件，与 data/characters/<id>.json 一一对应（重构 M3 试点）。

依赖方向：角色模块可顶层 import combat_engine/runtime；combat_engine 仅经函数内延迟导入
或 activate() 触达角色层，effect_resolver/techniques 以顶层静态导入按名引用（无环）。标准导出（全部可选）：
CHAR_ID / SKILL_HOOKS / AI / TECHNIQUE / MARKERS；行迹与星魂处理器为模块级函数，
由 effect_resolver 注册表按名引用（<模块>.<_trace_*>）。M4 起逐批扩充试点名单。
"""
from engine.characters import (  # noqa: F401
    acheron,
    aglaea,
    boothill,
    bronya,
    busitu,
    cerydra,
    changyeyue,
    cipher,
    dan_heng_permansor_terrae,
    evanescia,
    feixiao,
    fengjin,
    firefly,
    fu_xuan,
    fugue,
    himeko_nova,
    huohuo,
    hysilens,
    lingsha,
    mydei,
    anaxa,
    phainon,
    qianye,
    rappa,
    robin,
    robin_summeretto,
    ruan_mei,
    seele,
    silver_wolf,
    sparkle,
    sparxie,
    sunday,
    the_dahlia,
    trailblazer_elation,
    trailblazer_harmony,
    trailblazer_remembrance,
    tribbie,
    welt,
    xiadie,
    xilian,
    yaoguang,
    yinlang,
)

PILOTS = (acheron,
          aglaea,
          boothill,
          bronya,
          busitu,
          cerydra,
          changyeyue,
          cipher,
          dan_heng_permansor_terrae,
          evanescia,
          feixiao,
          fengjin,
          firefly,
          fu_xuan,
          fugue,
          himeko_nova,
          huohuo,
          hysilens,
          lingsha,
          mydei,
          anaxa,
          phainon,
          qianye,
          rappa,
          robin,
          robin_summeretto,
          ruan_mei,
          seele,
          silver_wolf,
          sparkle,
          sparxie,
          sunday,
          the_dahlia,
          trailblazer_elation,
          trailblazer_harmony,
          trailblazer_remembrance,
          tribbie,
          welt,
          xiadie,
          xilian,
          yaoguang,
          yinlang)
ELATION_PILOT_IDS = ("yinlang", "yaoguang", "trailblazer_elation", "huohuo",
                     "evanescia", "sparxie")


def activate(state, team_ids, elation_active=False):
    """每局按在场角色装配 SKILL_HOOKS（写入 state）并返回 AI 表。

    simulate 无条件调用并把返回值合并进局部 ai_registry（simulate 尾部会用
    局部表整体覆盖 state.ai_registry, 直写会被清掉——M3 实测教训）。
    """
    build_phase_tables(state, team_ids)
    ai_table = {}
    for m in PILOTS:
        if m.CHAR_ID not in team_ids:
            continue
        gated = getattr(m, "ELATION_GATED", False)
        if gated and not elation_active:
            continue  # 欢愉系角色在非欢愉队保持默认 AI/无技能钩子（M3 前语义）
        hooks = getattr(m, "SKILL_HOOKS", None)
        if hooks:
            state.skill_hooks.setdefault(m.CHAR_ID, []).extend(hooks)
        ai = getattr(m, "AI", None)
        if ai is not None:
            ai_table[m.CHAR_ID] = ai
        init = getattr(m, "INIT", None)
        if init is not None:
            init(state)  # 每局初始化（ELATION_GATED skip 时不调用; 门控=在场不含存活筛）
    return ai_table


def register_all_elation_skill_hooks(skill_hooks):
    """等价于原引擎 _register_elation_skill_hooks（测试直调路径的等价替换）。"""
    for m in PILOTS:
        if m.CHAR_ID in ELATION_PILOT_IDS:
            hooks = getattr(m, "SKILL_HOOKS", None)
            if hooks:
                skill_hooks.setdefault(m.CHAR_ID, []).extend(hooks)


def _present_ids(state):
    """在场角色 id 集合（含已死亡——与原模块级注册表语义一致）。"""
    return {u.char.id for u in state.units}


def marker_actions(state):
    """在场角色的行动条标记动作（每局构建, 替代模块级注册）。"""
    ids = _present_ids(state)
    return {k: v for m in PILOTS if m.CHAR_ID in ids
            for k, v in getattr(m, "MARKERS", {}).items()}


def marker_despawns(state):
    ids = _present_ids(state)
    return {k: v for m in PILOTS if m.CHAR_ID in ids
            for k, v in getattr(m, "MARKER_DESPAWN", {}).items()}


def marker_spawns(state):
    ids = _present_ids(state)
    return {k: v for m in PILOTS if m.CHAR_ID in ids
            for k, v in getattr(m, "MARKER_SPAWN", {}).items()}

# 击破配装策略聚合（原 relic_optimizer.BREAK_CHAR_CONFIG/IDS, 硬编码清单#4 收口）
BREAK_CHAR_CONFIG = {}
BREAK_CHAR_IDS = set()
for _m in PILOTS:
    if getattr(_m, 'BREAK_CONFIG', None):
        BREAK_CHAR_CONFIG[_m.CHAR_ID] = _m.BREAK_CONFIG
    if getattr(_m, 'IS_BREAK_CHAR', False):
        BREAK_CHAR_IDS.add(_m.CHAR_ID)

# 献予诗篇聚合（v7.15.0, 原 remembrance POEM 四表; POEM=(诗名, 效果函数, 整场生效)）
POEMS = {m.CHAR_ID: m.POEM for m in PILOTS if getattr(m, 'POEM', None)}


# ---- M5a: 相位表注入 ----

# 常规回合 tick 三区保序表——与原引擎 内联 tick 顺序逐位一致（零漂移锁）。
# zone 'pre' 派发点锚在原 qianye 位（AV/turn_count 更新之后）; 'post_control' 在控制
# 判定后; 'late' 在 xiadie_heal_conv 通用清理之后、终结技决策之前。
TURN_TICK_ZONE_ORDER = {
    'pre': ('seele', 'qianye', 'xiadie', 'tribbie', 'hysilens', 'cipher', 'cerydra',
            'sunday', 'ruan_mei', 'robin', 'acheron', 'feixiao', 'anaxa'),
    'post_control': ('huohuo',),
    'late': ('xilian', 'mydei', 'aglaea'),
}


# 技能后结算管线顺序——与原引擎 v6.6 批1-3 内联顺序逐位一致（零漂移锁）。
# 每个处理器签名统一 fn(u, state, skill, skill_key, total_dmg); self/observer 守卫在处理器内。
SETTLE_PIPELINE_ORDER = (
    ('tribbie', 'settle_self'), ('tribbie', 'settle_ally_ult'), ('tribbie', 'settle_field'),
    ('cerydra', 'settle_self'), ('cerydra', 'settle_jungong_attack'),
    ('cerydra', 'settle_jungong_qixi'), ('cerydra', 'settle_jungong_ult'),
    ('dan_heng_permansor_terrae', 'settle_self'), ('dan_heng_permansor_terrae', 'settle_tongpao'),
    ('hysilens', 'settle_self'), ('hysilens', 'settle_dot'),
    ('anaxa', 'settle_self'), ('cipher', 'settle_self'),
    ('phainon', 'settle_self'), ('phainon', 'settle_named'),
)


def build_phase_tables(state, team_ids=None):
    """每局注入 M5a 派发表（char_phases/observer_phases/turn_ticks/effect_*/debuff_*）。

    与 activate() 其余装配的区别：不跑 INIT/AI/SKILL_HOOKS、不做 ELATION_GATED 门控——
    与旧引擎内联分支语义逐点等价（内联分支本就不依赖这些）。combat_engine 直调入口在
    表未就绪时惰性调用本函数自举（按在场角色构建, 不引入跨局可变全局）。
    模块标准导出（全部可选）：PHASE_HOOKS / OBSERVER_HOOKS / TURN_TICKS /
    EFFECT_TAKEOVERS / EFFECT_MUTATORS / EFFECT_PRE_APPLY / DEBUFF_TAKEOVERS。
    """
    ids = team_ids if team_ids is not None else {
        u.char.id for u in state.units
        if getattr(getattr(u, 'char', None), 'id', None)
    }
    state.char_phases = {}
    state.observer_phases = {}
    state.effect_takeovers = {}
    state.effect_mutators = {}
    state.effect_pre_apply = {}
    state.debuff_takeovers = {}
    for m in PILOTS:
        if m.CHAR_ID not in ids:
            continue
        phases = getattr(m, 'PHASE_HOOKS', None) or {}
        if phases:
            state.char_phases[m.CHAR_ID] = dict(phases)
        for phase, fn in (getattr(m, 'OBSERVER_HOOKS', None) or {}).items():
            state.observer_phases.setdefault(phase, []).append(fn)
        for pid, fn in (getattr(m, 'EFFECT_TAKEOVERS', None) or {}).items():
            state.effect_takeovers[pid] = fn
        for pid, fn in (getattr(m, 'EFFECT_MUTATORS', None) or {}).items():
            state.effect_mutators[pid] = fn
        for pid, fn in (getattr(m, 'EFFECT_PRE_APPLY', None) or {}).items():
            state.effect_pre_apply[pid] = fn
        for pid, fn in (getattr(m, 'DEBUFF_TAKEOVERS', None) or {}).items():
            state.debuff_takeovers[pid] = fn
    by_id = {m.CHAR_ID: m for m in PILOTS}
    pipeline = getattr(state, 'settle_pipeline', None)
    state.settle_pipeline = []
    for cid, attr in SETTLE_PIPELINE_ORDER:
        m = by_id.get(cid)
        if m is None or cid not in ids:
            continue
        fn = (getattr(m, 'SETTLE_HANDLERS', None) or {}).get(attr)
        if fn:
            state.settle_pipeline.append(fn)
    state.turn_ticks = {}
    for zone, order in TURN_TICK_ZONE_ORDER.items():
        bucket = []
        for cid in order:
            m = by_id.get(cid)
            if m is None or cid not in ids:
                continue
            fns = (getattr(m, 'TURN_TICKS', None) or {}).get(zone)
            if fns:
                bucket.extend(fns if isinstance(fns, (list, tuple)) else [fns])
        if bucket:
            state.turn_ticks[zone] = bucket
    state._phase_tables_ready = True
