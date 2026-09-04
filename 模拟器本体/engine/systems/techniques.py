"""秘技系统 — 战前秘技结算（重构 M2 自 engine/core/combat_utils.py 迁入）

apply_techniques 是唯一生产入口（combat_engine.simulate 初始化尾部按队伍结算）。
TECHNIQUE_EFFECTS 为静态只读映射 char_id → _tech_<cid>(state, u, is_opener)，
全库无写入口（每局隔离安全）。
随迁出删除的零引用死代码：taunt 三函数（真实嘲讽命中在 combat_engine 内联实现）
与 resolve_techniques（全库零调用）——记录见 CODEX_HANDOFF M2 节。
"""

def calc_effect_probability(base_chance: float, effect_hit_rate: float,
                            effect_res: float) -> float:
    """最终命中概率 = 基础概率 × (1 + 效果命中) × (1 - 效果抵抗)"""
    return base_chance * (1.0 + effect_hit_rate) * (1.0 - effect_res)


def _tech_tbh(state, u, is_opener):
    """同谐: 全队击破特攻+30% 2回合（开拓者·同谐.txt 秘技·即刻！独奏团）"""
    from engine.runtime import TimedBuff
    for eu in state.units:
        if eu.is_alive:
            eu.buffs.append(TimedBuff(source_id='trailblazer_harmony',
                                      attributes={'BREAK_EFFECT': 30.0},
                                      remaining_turns=2,
                                      param_id='tbh_tech_be'))
    state.log.append('[秘技] 即刻！独奏团: 全队击破特攻+30% 2回合')


# M3: 试点角色秘技已迁角色包（静态只读引用, 非可变全局）
from engine.runtime import _tech_enemies

from engine.characters import aglaea as _char_aglaea, changyeyue as _char_changyeyue, \
    acheron as _char_acheron, anaxa as _char_anaxa, bronya as _char_bronya, busitu as _char_busitu, cerydra as _char_cerydra, cipher as _char_cipher, dan_heng_permansor_terrae as _char_dan_heng_permansor_terrae, feixiao as _char_feixiao, fu_xuan as _char_fu_xuan, fugue as _char_fugue, hysilens as _char_hysilens, mydei as _char_mydei, phainon as _char_phainon, qianye as _char_qianye, robin as _char_robin, ruan_mei as _char_ruan_mei, seele as _char_seele, sparkle as _char_sparkle, sunday as _char_sunday, the_dahlia as _char_the_dahlia, tribbie as _char_tribbie, welt as _char_welt, \
    robin_summeretto as _char_robin_summeretto, \
    evanescia as _char_evanescia, fengjin as _char_fengjin, \
    trailblazer_remembrance as _char_trailblazer_remembrance, \
    xiadie as _char_xiadie, xilian as _char_xilian, \
    firefly as _char_firefly, himeko_nova as _char_himeko_nova, \
    huohuo as _char_huohuo, lingsha as _char_lingsha, \
    silver_wolf as _char_silver_wolf, yinlang as _char_yinlang
_PILOT_TECHNIQUES = {
    'sunday': _char_sunday.TECHNIQUE,
    'welt': _char_welt.TECHNIQUE,
    'ruan_mei': _char_ruan_mei.TECHNIQUE,
    'robin': _char_robin.TECHNIQUE,
    'busitu': _char_busitu.TECHNIQUE,
    'qianye': _char_qianye.TECHNIQUE,
    'acheron': _char_acheron.TECHNIQUE,
    'feixiao': _char_feixiao.TECHNIQUE,
    'mydei': _char_mydei.TECHNIQUE,
    'sparkle': _char_sparkle.TECHNIQUE,
    'seele': _char_seele.TECHNIQUE,
    'bronya': _char_bronya.TECHNIQUE,
    'fu_xuan': _char_fu_xuan.TECHNIQUE,
    'the_dahlia': _char_the_dahlia.TECHNIQUE,
    'phainon': _char_phainon.TECHNIQUE,
    'hysilens': _char_hysilens.TECHNIQUE,
    'anaxa': _char_anaxa.TECHNIQUE,
    'cipher': _char_cipher.TECHNIQUE,
    'tribbie': _char_tribbie.TECHNIQUE,
    'cerydra': _char_cerydra.TECHNIQUE,
    'dan_heng_permansor_terrae': _char_dan_heng_permansor_terrae.TECHNIQUE,
    'fugue': _char_fugue.TECHNIQUE,

    'robin_summeretto': _char_robin_summeretto.TECHNIQUE,
    'aglaea': _char_aglaea.TECHNIQUE,
    'changyeyue': _char_changyeyue.TECHNIQUE,
    'trailblazer_remembrance': _char_trailblazer_remembrance.TECHNIQUE,
    'xiadie': _char_xiadie.TECHNIQUE,
    'xilian': _char_xilian.TECHNIQUE,

    'firefly': _char_firefly.TECHNIQUE,
    'fengjin': _char_fengjin.TECHNIQUE,
    'himeko_nova': _char_himeko_nova.TECHNIQUE,
    'lingsha': _char_lingsha.TECHNIQUE,
    'silver_wolf': _char_silver_wolf.TECHNIQUE,
    'yinlang': _char_yinlang.TECHNIQUE,
    'huohuo': _char_huohuo.TECHNIQUE,
    'evanescia': _char_evanescia.TECHNIQUE,
}


TECHNIQUE_EFFECTS = {
    'trailblazer_harmony': _tech_tbh,
    # v6.3.0 第二批: 希儿/银狼/布洛妮娅/符玄/藿藿/花火
    # （开拓者·欢愉/爻光 由 elation.init_battle 实现, 不入注册表防重复;
    #   v6.7 例外: 绯英(进战)入注册表——进战受开怪者门控, init_battle 在其之前执行;
    #   火花(非进战)仍由 elation.init_battle 处理）
    # v6.7 绯英（进战, 欢愉角色入注册表破例——见 handler 注释）; 火花秘技由 elation.init_battle 处理
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
        fn = _PILOT_TECHNIQUES.get(u.char.id) or TECHNIQUE_EFFECTS.get(u.char.id)
        if fn:
            fn(state, u, is_opener=False)
    for u in bs_units:
        fn = _PILOT_TECHNIQUES.get(u.char.id) or TECHNIQUE_EFFECTS.get(u.char.id)
        if fn:
            fn(state, u, is_opener=(u is opener_unit))
    return state.extra['opener_id']
