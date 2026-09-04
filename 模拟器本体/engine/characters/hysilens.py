"""hysilens（M4 收官批迁入）"""

import copy
import random
from engine.runtime import _enemy_for_damage, _tech_enemies
from engine.core.damage import calculate_damage
from engine.models.enemy import EnemyStatus
from engine.core.combat_engine import HYSILENS_DOTS
from engine.core.combat_engine import _build_effective_stats
from engine.core.combat_engine import _commit_enemy_damage
from engine.core.combat_engine import _gain_skill_points


def _hysilens_apply_dot(state, u, target, count=1, e1_double=False):
    """海瑟音天赋/秘技: 挂随机DOT（优先不同状态）
    v6.6c: 存快照供敌方回合结算; 海洋诗立即结算60%。
    v6.8.1: E1 116% 改在 DOT 结算统一乘（全队持续伤害口径, 此前挂时乘只覆盖海瑟音自己）;
    E1「额外陷入一次」=天赋路径双挂（e1_double）。"""
    from engine.models.enemy import EnemyStatus
    if target is None or getattr(target, 'HP', 0) <= 0:
        return
    if e1_double and u.eidolon_rank >= 1:
        count = 2
    snap = copy.deepcopy(_build_effective_stats(u, state))
    for _ in range(count):
        import random
        # v6.8.1: pool 随挂载更新（此前循环前算一次, 多挂时同状态覆盖只剩1种）
        existing = {s.name for s in target.statuses if s.id.startswith('hysilens_dot')}
        pool = [d for d in HYSILENS_DOTS if d[0] not in existing] or HYSILENS_DOTS
        name, elem, mult = random.choice(pool)
        target.add_status(EnemyStatus(
            id=f'hysilens_dot_{name}', name=name, category='dot',
            source='hysilens', remaining_turns=2,
            attributes={'element': elem, 'multiplier': mult,
                        'dot_snapshot': snap,
                        'dot_type': 'break' if name == '裂伤' else 'std'}))
        # 献予「海洋」之诗: DOT 立即结算60%
        if u.extra.get('poem_haiyang'):
            d = _hysilens_dot_damage(target, name, elem, mult, snap) * (1.16 if u.eidolon_rank >= 1 else 1.0)
            _commit_enemy_damage(state, u, target, d)
            u.total_damage_dealt += d
            if d > 0:
                state.log.append(f'  献予「海洋」: 立即结算{d:.0f}')
    state.log.append(f'  海瑟音DOT: {target.name or target.id} +{count}种')


def _hysilens_dot_damage(enemy, name, elem, mult, snap):
    """海瑟音单跳DOT伤害（裂伤=敌HP20%封顶25%ATK; 其余=ATK×倍率）"""
    if name == '裂伤':
        return min(enemy.HP * 0.20, snap.ATK * 0.25)
    d = calculate_damage(snap, _enemy_for_damage(enemy), snap.ATK, mult,
                         'dot', elem, 80, False)
    return d.final_damage


def _tick_hysilens_dot(state, enemy, s):
    """v6.6c P1: 海瑟音DOT敌方回合跳伤（此前 hysilens_* 永不结算）"""
    snap = s.attributes.get('dot_snapshot')
    if not snap:
        return 0.0
    dmg = _hysilens_dot_damage(enemy, s.name,
                               s.attributes.get('element', '物理'),
                               s.attributes.get('multiplier', 25.0), snap)
    owner = next((x for x in state.units
                  if x.char.id == s.source and x.is_alive), None)
    _commit_enemy_damage(state, owner, enemy, dmg)
    if owner is not None:
        owner.total_damage_dealt += dmg
    state.log.append(f'  {s.name}: {dmg:.0f} → {enemy.name or enemy.id}')
    return dmg


def _hysilens_field(state, u, turns=3):
    """海瑟音结界: 敌ATK-15%/DEF-25% + DOT引爆
    v6.6c P1: 实装属性消费（_enemy_attack_stats/_enemy_for_damage 读 hysilens_field）;
    E4: 结界期敌全抗-20%"""
    state.extra['hysilens_field_turns'] = turns
    for e in state.enemies:
        e.extra['hysilens_field'] = True
        if u.eidolon_rank >= 4:
            if not e.extra.get('hysilens_e4_res'):
                for elem in list(e.element_res):
                    e.element_res[elem] = e.element_res.get(elem, 0) - 0.20
                e.extra['hysilens_e4_res'] = True
    state.log.append(f'  海瑟音结界: 敌ATK-15%/DEF-25% ({turns}回合)' + (' + E4全抗-20%' if u.eidolon_rank >= 4 else ''))


def _hysilens_remove_field(state, u):
    for e in state.enemies:
        e.extra['hysilens_field'] = False
        if e.extra.pop('hysilens_e4_res', False):
            for elem in list(e.element_res):
                e.element_res[elem] = e.element_res.get(elem, 0) + 0.20
    state.extra['hysilens_field_turns'] = 0


def _hysilens_dot_trigger_v3(state, u, target):
    """v6.8.3: 海瑟音结界反打——立即结算 80%ATK 物理 DOT。
    旧实现写 hysilens_echo 状态但无 dot_snapshot, 下一回合结算恒为 0 且会自触发;
    这里不新增任何状态, 天然不可递归。触发次数由 _begin_enemy_turn 在每个敌方回合开始重置。"""
    if not target.extra.get('hysilens_field') or getattr(target, 'HP', 0) <= 0:
        return 0.0
    cap = 12 if u.eidolon_rank >= 6 else 8
    cnt = state.extra.get('hysilens_trigger_count', 0)
    if cnt >= cap:
        return 0.0
    state.extra['hysilens_trigger_count'] = cnt + 1
    mult = 80.0 * (1.2 if u.eidolon_rank >= 6 else 1.0) * (1.16 if u.eidolon_rank >= 1 else 1.0)
    stats = _build_effective_stats(u, state)
    d = calculate_damage(stats, _enemy_for_damage(target), stats.ATK, mult,
                         'dot', '物理', 80, False)
    _commit_enemy_damage(state, u, target, d.final_damage)
    u.total_damage_dealt += d.final_damage
    state.log.append(f'  噬魂回响: {d.final_damage:.0f} → {target.name or target.id} ({cnt+1}/{cap})')
    return d.final_damage


def _trace_hysilens_trace1(u, state, **kw):
    """海瑟音行迹1: 开局展开结界3回合 + 回1SP"""
    if u.char.id != 'hysilens':
        return
    from engine.core.combat_engine import _gain_skill_points
    _hysilens_field(state, u, turns=3)
    _gain_skill_points(state, 1)
    state.log.append('  行迹·剑旗: 开局结界3回合+回1SP')


def _trace_hysilens_trace3(u, state, **kw):
    """海瑟音行迹3: EHR>60%每10%增伤15%上限90%
    v6.10.3 P1-5: 改为 _build_effective_stats 动态消费（此前入场永久写 base_stats 与动态面板双算）"""
    if u.char.id != 'hysilens':
        return


def _tech_hysilens(state, u, is_opener):
    """海瑟音: 全敌2种随机DOT（非进战·领域醉心）"""

    for e in _tech_enemies(state):
        _hysilens_apply_dot(state, u, e, count=2)
    state.log.append('[秘技] 于海的栖息地: 全敌2种DOT')


CHAR_ID = "hysilens"
TECHNIQUE = _tech_hysilens


# ---- M5a: 常规回合 tick（原引擎 _begin_regular_turn 内联, verbatim 迁入）----

def _hysilens_turn_tick(u, state):
    # v6.6c P1: 海瑟音结界倒计时 + 到期恢复（此前只设不消费=永久）
    if u.char.id == 'hysilens':
        ft = state.extra.get('hysilens_field_turns', 0)
        if ft > 0:
            ft -= 1
            if ft <= 0:
                _hysilens_remove_field(state, u)
            else:
                state.extra['hysilens_field_turns'] = ft


TURN_TICKS = {'pre': _hysilens_turn_tick}


# ---- M5a 批5a: 技能后结算管线处理器（原引擎 v6.6 批1-3 内联, verbatim 迁入）----


def _hysilens_settle_self(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_self: 终结技开启场域+现存DOT立即结算150%。"""
    if u.char.id != 'hysilens' or skill_key != 'ultimate':
        return None
    _hysilens_field(state, u)
    stats = _build_effective_stats(u, state)
    from engine.core.damage import calculate_damage as _cd
    for e in state.enemies:
        for st in list(e.statuses):
            if st.category != 'dot':
                continue
            mult = st.attributes.get('multiplier', 0) or 0
            if mult > 0:
                d = _cd(stats, _enemy_for_damage(e), stats.ATK,
                        mult * 1.5 * (1.16 if u.eidolon_rank >= 1 else 1.0), 'dot',
                        st.attributes.get('element', '物理'), 80, False)
                _commit_enemy_damage(state, u, e, d.final_damage)
                u.total_damage_dealt += d.final_damage
    state.log.append('  行迹·泡沫: 现存DOT立即结算150%')
    state.extra['hysilens_trigger_count'] = 0
    return None


def _hysilens_settle_dot(u, state, skill, skill_key, total_dmg):
    """SETTLE settle_dot: 任意我方攻击→被击中目标陷入海瑟音DOT（E1 双陷）。"""
    # 天赋: 我方攻击时被击中目标陷入DOT
    if total_dmg <= 0:
        return None
    hs = next((x for x in state.units if x.char.id == 'hysilens' and x.is_alive), None)
    if hs:
        # v6.8.1: 仅被击中的目标陷入（txt:56「我方目标攻击时使被击中的敌方目标陷入」,
        # 此前对所有存活敌挂 DOT 且排除海瑟音自己）
        hit = list(state.extra.get('last_attack_targets') or [])
        seen = set()
        for t in hit:
            if t is None or t.HP <= 0 or id(t) in seen:
                continue
            seen.add(id(t))
            _hysilens_apply_dot(state, hs, t, e1_double=True)  # E1: 天赋路径额外陷入一次
    return None


SETTLE_HANDLERS = {'settle_self': _hysilens_settle_self,
                   'settle_dot': _hysilens_settle_dot}


# ---- v7.15.0: 献予诗篇（原 remembrance 内联, verbatim 迁入; POEM=(诗名, 效果, 整场)）----


def _poem_haiyang(state, summoner, ms_unit, hysilens):
    """献予「海洋」之诗(整场, 海瑟音): 暖流+60能; 伤害+120%; DOT立即结算60/80%"""
    from engine.core.combat_engine import _gain_energy
    _gain_energy(hysilens, 60.0, state=state)
    hysilens.extra['poem_haiyang'] = True
    hysilens.base_stats.DMG_BONUS_ALL += 1.20
    state.log.append('  献予「海洋」之诗: 暖流+60能+伤害+120%')


POEM = ("海洋", _poem_haiyang, True)
