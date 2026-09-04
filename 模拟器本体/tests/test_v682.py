"""v6.8.2 回归: HARNESS-R1~R6（v6.8.1 修复后复核发现的残余问题）

语义依据: CODEX_HANDOFF.md「HARNESS PORT REVIEW — v6.8.1 修复后复核」节。"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats, _flat_toughness_with_break
from engine.characters.tribbie import _tribbie_field_extra_damage, _tribbie_talent_fua
from engine.runtime import SimState, SimUnit


def _enemy(hp=500000, toughness=200, name='X'):
    return Enemy(id='x', name=name, HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': -0.2})


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


def _dps(unit, enemy, **kwargs):
    """把敌人在技能调用后的掉血量抓成可直接比较的数值。"""
    from engine.core.combat_engine import _build_effective_stats
    from engine.runtime import _enemy_for_damage
    from engine.core.damage import calculate_damage
    stats = _build_effective_stats(unit, kwargs.pop('state'))
    d = calculate_damage(stats, _enemy_for_damage(enemy), stats.ATK, 100.0,
                         'direct', unit.char.element, 80, stats.CRIT_RATE >= 0.5,
                         crit_mode='expected', **kwargs)
    return d.final_damage


class TestR1TargetCache:
    def test_stale_multihit_targets_cleared_per_action(self):
        """R1: 上一次弹射的 last_multihit_targets 不得污染本次普通攻击。"""
        ax = _unit('anaxa')
        e1, e2 = _enemy(name='A'), _enemy(name='B')
        st = SimState(enemies=[e1, e2], units=[ax])
        st.extra['last_attack_targets'] = [e1]
        st.extra['last_multihit_targets'] = [e2]  # 模拟上一次弹射残留
        _use_skill(ax, st, 'basic_attack')
        # _use_skill 开头已清空缓存；本次只有 e1 被普通攻击命中
        assert not any(s.id.startswith('anaxa_weak') for s in e2.statuses)

    def test_bounce_multihit_cache_cleared_after_aggregate(self):
        """R1: 弹射命中已并入 last_attack_targets, last_multihit_targets 立即清空。"""
        ax = _unit('anaxa')
        st = SimState(enemies=[_enemy(), _enemy()], units=[ax])
        st.skill_points = 3
        _use_skill(ax, st, 'skill')
        assert st.extra.get('last_multihit_targets', []) == []


class TestR2FlatBreakGuard:
    def test_flat_break_skips_dead_target(self):
        """R2: 目标已死亡时不再击破、不再重复计击杀。"""
        u = _unit('himeko_nova')
        e = _enemy(toughness=2)
        e.HP = 0
        st = SimState(enemies=[e], units=[u])
        st.extra['killed_total'] = 7
        _flat_toughness_with_break(st, u, e, 2.0, '火', 'ultimate',
                                   _build_effective_stats(u, st))
        assert e.is_broken is False
        assert st.extra['killed_total'] == 7


class TestR3HysilensE1Immediate:
    def test_ultimate_immediate_dot_settle_multiplied_by_116(self):
        """R3: 泡沫150%立即结算也吃 E1 116%。"""
        e1 = _enemy()
        e0 = _enemy()
        status_attrs = {'element': '火', 'multiplier': 25.0}
        e1.add_status(EnemyStatus(id='hysilens_dot_灼烧', name='灼烧',
                                  category='dot', source='hysilens',
                                  remaining_turns=2, attributes=status_attrs))
        e0.add_status(EnemyStatus(id='hysilens_dot_灼烧', name='灼烧',
                                  category='dot', source='hysilens',
                                  remaining_turns=2, attributes=status_attrs))
        hs1 = _unit('hysilens', eidolon=1)
        hs0 = _unit('hysilens')
        st1 = SimState(enemies=[e1], units=[hs1])
        st0 = SimState(enemies=[e0], units=[hs0])
        hs1.current_energy = hs1.char.max_energy
        hs0.current_energy = hs0.char.max_energy
        hp1 = e1.HP
        hp0 = e0.HP
        _use_skill(hs1, st1, 'ultimate')
        _use_skill(hs0, st0, 'ultimate')
        assert hp1 - e1.HP > hp0 - e0.HP


class TestR4TribbieTraceStackReset:
    def test_stack_counter_resets_after_buff_expiry(self):
        """R4: 行迹1 buff 过期后, 下一次 FUA 从 1 层重新开始。"""
        trib = _unit('tribbie')
        st = SimState(enemies=[_enemy()], units=[trib])
        _tribbie_talent_fua(st, trib)
        _tribbie_talent_fua(st, trib)  # 2层
        trib.buffs = [b for b in trib.buffs
                      if getattr(b, 'param_id', '') != 'tribbie_trace1_stack']
        _tribbie_talent_fua(st, trib)  # 过期后重新叠
        b = next(x for x in trib.buffs if getattr(x, 'param_id', '') == 'tribbie_trace1_stack')
        assert b.attributes['DMG_BONUS_ALL'] == pytest.approx(72.0)


class TestR5TribbieE2FirstHit:
    def test_e2_first_hit_also_120_percent(self):
        """R5: E2 第一段附加也应为 14.4%HP（合计 28.8%HP 基础）。"""
        t0 = _unit('tribbie')
        t2 = _unit('tribbie', eidolon=2)
        e0 = _enemy(hp=500000)
        e2 = _enemy(hp=500000)
        st0 = SimState(enemies=[e0], units=[t0])
        st2 = SimState(enemies=[e2], units=[t2])
        st0.extra['tribbie_field_turns'] = 2
        st2.extra['tribbie_field_turns'] = 2
        _tribbie_field_extra_damage(st0, t0, [e0], total_dmg=0.0)
        d0 = 500000 - e0.HP
        _tribbie_field_extra_damage(st2, t2, [e2], total_dmg=0.0)
        d2 = 500000 - e2.HP
        assert d2 > d0 * 2.3  # 2.4x, 留防御曲线余量


class TestR6PhainonTalentTargets:
    def test_all_allies_but_self_skill_target_triggers(self):
        """R6: all_allies_but_self 技能目标同样触发白厄天赋。"""
        ph = _unit('phainon')
        br = _unit('bronya', position=2)
        st = SimState(enemies=[_enemy()], units=[ph, br])
        st.skill_points = 3
        br.char.skills['skill'] = copy.deepcopy(br.char.skills['skill'])
        br.char.skills['skill'].target = 'all_allies_but_self'
        _use_skill(br, st, 'skill')
        assert ph.extra.get('huozhong', 0) >= 1

    def test_all_allies_ultimate_target_triggers(self):
        """R6: 全队终结技目标同样触发白厄天赋。"""
        ph = _unit('phainon')
        br = _unit('bronya', position=2)
        st = SimState(enemies=[_enemy()], units=[ph, br])
        br.current_energy = br.char.max_energy
        _use_skill(br, st, 'ultimate')
        assert ph.extra.get('huozhong', 0) >= 1
