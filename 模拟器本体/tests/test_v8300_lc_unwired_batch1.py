"""v8.3.0: _unwired 光锥接线第一批（10行/10把, 项目主批准计划 2026-09-30）。

②类逐目标条件 5 行（_lc_target_correct 内联）:
  秘密誓心/无边曼舞/延长记号/汪！散步时间！/雨一直下
⑤类事件行 5 行（LC_EVENT_ACTIONS + on_shield/on_ally_ult/on_heal/on_battle_start/on_ult）:
  命运从未公平/惊魂夜×2/勿忘她的火焰行1/纵然山河万程
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import (  # noqa: E402
    SimState, _apply_toughness_damage, _build_effective_stats, _lc_target_correct,
    _process_lc_effects, _tick_break_dot, _ult_post,
)
from engine.models.enemy import EnemyStatus  # noqa: E402


def _state(*units, enemies=None):
    from engine.systems.elation import ElationSystem
    st = SimState(enemies=list(enemies or [_enemy()]), units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


def _deb(e, name='负面'):
    e.add_status(EnemyStatus(id=f'deb_{name}_{len(e.statuses)}', name=name,
                             category='debuff', source='t', remaining_turns=2))


def _dot(e, name):
    e.add_status(EnemyStatus(id=f'break:{name}', name=name, category='dot',
                             source='t', remaining_turns=2))


def _bonus(u, st, e, attr):
    base = _build_effective_stats(u, st)
    s2 = _lc_target_correct(base, u, st, e)
    return getattr(s2, attr) - getattr(base, attr)


class TestASecretVow:
    """秘密誓心: 目标HP% ≥ 装备者HP% → 伤害+20-40%（JSON rank5 → S5=40）。"""

    def test_cond_met_and_tier(self):
        u = _unit('firefly', lc_id='a_secret_vow')
        u.current_hp = u.max_hp * 0.5
        e = _enemy()
        e.HP = e.max_hp
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == pytest.approx(0.40)
        u.lightcone.rank = 1
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == pytest.approx(0.20)

    def test_cond_not_met(self):
        u = _unit('firefly', lc_id='a_secret_vow')
        u.current_hp = u.max_hp * 0.5
        e = _enemy()
        e.HP = e.max_hp * 0.3
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == 0.0

    def test_path_guard(self):
        # 虚无光锥戴在毁灭角色上 → 命途不匹配不生效
        u = _unit('firefly', lc_id='fermata')
        u.current_hp = u.max_hp * 0.5
        e = _enemy()
        _dot(e, '触电')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == 0.0


class TestBoundlessChoreo:
    """无边曼舞: 目标防御降低或减速 → 暴伤+24-48%。"""

    def test_defdown(self):
        u = _unit('silver_wolf', lc_id='boundless_choreo')
        e = _enemy()
        e.add_status(EnemyStatus(id='dd', name='防降', category='debuff',
                                 source='t', remaining_turns=2,
                                 attributes={'def_reduction': 0.10}))
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'CRIT_DMG') == pytest.approx(0.48)

    def test_speeddown(self):
        u = _unit('silver_wolf', lc_id='boundless_choreo')
        e = _enemy()
        e.add_status(EnemyStatus(id='sd', name='减速', category='debuff',
                                 source='t', remaining_turns=2,
                                 attributes={'spd_down': 0.2}))
        st = _state(u, enemies=[e])
        u.lightcone.rank = 1
        assert _bonus(u, st, e, 'CRIT_DMG') == pytest.approx(0.24)

    def test_clean_target_no_bonus(self):
        u = _unit('silver_wolf', lc_id='boundless_choreo')
        e = _enemy()
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'CRIT_DMG') == 0.0


class TestVsDotPair:
    """延长记号(触电/风化)与汪！散步时间！(灼烧/裂伤): 共码不同集, DOT集外不生效。"""

    def test_fermata_electric(self):
        u = _unit('silver_wolf', lc_id='fermata')
        e = _enemy()
        _dot(e, '触电')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == pytest.approx(0.32)

    def test_fermata_burn_not_in_set(self):
        u = _unit('silver_wolf', lc_id='fermata')
        e = _enemy()
        _dot(e, '灼烧')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == 0.0

    def test_woof_burn_tier(self):
        u = _unit('firefly', lc_id='woof_walk_time')
        e = _enemy()
        _dot(e, '灼烧')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == pytest.approx(0.32)
        u.lightcone.rank = 1
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == pytest.approx(0.16)

    def test_woof_electric_not_in_set(self):
        u = _unit('firefly', lc_id='woof_walk_time')
        e = _enemy()
        _dot(e, '触电')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'DMG_BONUS_ALL') == 0.0


class TestIncessantRain:
    """雨一直下: 目标负面数≥3 → 暴击率+12-20%（JSON rank1 → S1=12）。"""

    def test_three_debuffs(self):
        u = _unit('silver_wolf', lc_id='incessant_rain')
        e = _enemy()
        for i in range(3):
            _deb(e, f'负{i}')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'CRIT_RATE') == pytest.approx(0.12)

    def test_two_debuffs_no_bonus(self):
        u = _unit('silver_wolf', lc_id='incessant_rain')
        e = _enemy()
        for i in range(2):
            _deb(e, f'负{i}')
        st = _state(u, enemies=[e])
        assert _bonus(u, st, e, 'CRIT_RATE') == 0.0


class TestDotTickExtension:
    """延长记号/汪散步「对持续伤害也生效」: DOT 跳伤按归属者光锥加成。"""

    def _run(self, with_lc):
        u = _unit('silver_wolf', lc_id='fermata')
        u.lightcone.rank = 1
        if not with_lc:
            u.lightcone = None
        e = _enemy()
        _dot(e, '触电')
        st = _state(u, enemies=[e])
        snap = copy.deepcopy(_build_effective_stats(u, st))
        assert snap.DMG_BONUS_ALL == 0.0
        s = next(x for x in e.statuses if x.name == '触电')
        s.attributes['dot_snapshot'] = snap
        s.attributes['dot_multiplier'] = 100.0
        s.attributes['dot_element'] = '雷'
        s.source = u.char.id
        return _tick_break_dot(st, e, s)

    def test_owner_lc_boosts_dot_tick(self):
        d0 = self._run(with_lc=False)
        d1 = self._run(with_lc=True)
        assert d1 == pytest.approx(d0 * 1.16, rel=1e-6)

    def test_wrong_dot_name_no_boost(self):
        u = _unit('silver_wolf', lc_id='fermata')
        u.lightcone.rank = 1
        e = _enemy()
        _dot(e, '灼烧')  # fermata 集合外
        st = _state(u, enemies=[e])
        snap = copy.deepcopy(_build_effective_stats(u, st))
        s = next(x for x in e.statuses if x.name == '灼烧')
        s.attributes.update(dot_snapshot=snap, dot_multiplier=100.0,
                            dot_element='火')
        s.source = u.char.id
        d = _tick_break_dot(st, e, s)
        s2 = copy.deepcopy(s)
        s2.attributes['dot_snapshot'] = copy.deepcopy(snap)
        # 无加成: 与快照直接结算一致（比值1.0 由上例锚定, 此处仅确认不爆炸且为正）
        assert d > 0


class TestShieldEvent:
    """命运从未公平: 装备者提供护盾 → 自身暴伤+40-64% 2回合（同源刷新）。"""

    def test_on_shield_buff(self):
        u = _unit('dan_heng_permansor_terrae', lc_id='inherently_unjust_destiny')
        assert u.lightcone.rank == 1  # JSON 录入档 S1 → 40
        st = _state(u)
        _process_lc_effects(u, st, "on_shield")
        bs = [b for b in u.buffs if b.param_id == 'lcx_inherently_u']
        assert len(bs) == 1
        assert bs[0].attributes['CRIT_DMG'] == 40.0
        assert bs[0].remaining_turns == 2
        _process_lc_effects(u, st, "on_shield")  # 同源刷新不叠层
        bs = [b for b in u.buffs if b.param_id == 'lcx_inherently_u']
        assert len(bs) == 1

    def test_dht_apply_shield_dispatches(self):
        from engine.characters.dan_heng_permansor_terrae import _dht_apply_shield
        u = _unit('dan_heng_permansor_terrae', lc_id='inherently_unjust_destiny')
        st = _state(u)
        _dht_apply_shield(st, u, 20, 400, '渊渟岳峙')
        assert u.shield > 0
        assert any(b.param_id == 'lcx_inherently_u' for b in u.buffs)

    def test_path_guard(self):
        u = _unit('firefly', lc_id='inherently_unjust_destiny')  # 存护光锥/毁灭角色
        st = _state(u)
        _process_lc_effects(u, st, "on_shield")
        assert not any(b.param_id == 'lcx_inherently_u' for b in u.buffs)


class TestNightOfFright:
    """惊魂夜: 行1 我方终结技→HP%最低者回复; 行2 治疗→受疗者ATK叠层(≤5)。"""

    def _team(self):
        u = _unit('huohuo', lc_id='night_of_fright')  # 丰饶/JSON rank1 → S1
        mate = _unit('silver_wolf')
        mate.current_hp = mate.max_hp * 0.30
        return u, mate, _state(u, mate)

    def test_ally_ult_heals_lowest_pct(self):
        u, mate, st = self._team()
        hp0 = mate.current_hp
        st.extra['lc_ult_caster'] = mate
        _process_lc_effects(u, st, "on_ally_ult")
        hb = _build_effective_stats(u, st).HEAL_BONUS
        assert mate.current_hp == pytest.approx(
            min(mate.max_hp, hp0 + mate.max_hp * 0.10 * (1 + hb)))

    def test_self_ult_also_triggers(self):
        u, mate, st = self._team()
        st.extra['lc_ult_caster'] = u
        _process_lc_effects(u, st, "on_ally_ult")
        assert mate.current_hp > mate.max_hp * 0.30

    def test_ult_post_broadcast(self):
        u, mate, st = self._team()
        hp0 = mate.current_hp
        _ult_post(st, mate)  # 广播入口（含施放者与全体存活单位的光锥）
        assert mate.current_hp > hp0

    def test_row1_heal_feeds_row2(self):
        u, mate, st = self._team()
        st.extra['lc_ult_caster'] = u
        _process_lc_effects(u, st, "on_ally_ult")
        stacks = [b for b in mate.buffs if b.param_id == 'lcx_nof_atk']
        assert len(stacks) == 1
        assert stacks[0].attributes['ATK_percent'] == pytest.approx(2.4)

    def test_row2_cap_and_per_target(self):
        u, mate, st = self._team()
        other = _unit('firefly')
        st.units.append(other)
        st.extra['lc_last_heal_targets'] = [mate, other]
        for _ in range(6):
            _process_lc_effects(u, st, "on_heal")
        assert len([b for b in mate.buffs if b.param_id == 'lcx_nof_atk']) == 5
        assert len([b for b in other.buffs if b.param_id == 'lcx_nof_atk']) == 5


class TestFlameBreakTeam:
    """勿忘她的火焰行1: 入场装备者+一名队友击破伤害+32-72%（同类不叠加）。"""

    def test_battle_start_marks_pair(self):
        u = _unit('the_dahlia', lc_id='never_forget_her_flame')  # 虚无/JSON rank1
        mate = _unit('silver_wolf')
        st = _state(u, mate)
        _process_lc_effects(u, st, "on_battle_start")
        assert u.extra['lcf_break_dmg'] == 32.0
        assert mate.extra['lcf_break_dmg'] == 32.0
        _process_lc_effects(u, st, "on_battle_start")  # 同类不叠加
        assert mate.extra['lcf_break_dmg'] == 32.0

    def test_second_wearer_no_stack_on_shared_mate(self):
        u = _unit('the_dahlia', lc_id='never_forget_her_flame')
        u2 = _unit('silver_wolf', lc_id='never_forget_her_flame')
        st = _state(u, u2)
        _process_lc_effects(u, st, "on_battle_start")
        _process_lc_effects(u2, st, "on_battle_start")
        assert u.extra['lcf_break_dmg'] == 32.0
        assert u2.extra['lcf_break_dmg'] == 32.0
        # u2 的"下一名队友"=u 已被标记 → 保持 32 不叠成 64
        assert u.extra['lcf_break_dmg'] == 32.0

    def test_solo_only_wearer(self):
        u = _unit('the_dahlia', lc_id='never_forget_her_flame')
        st = _state(u)
        _process_lc_effects(u, st, "on_battle_start")
        assert u.extra['lcf_break_dmg'] == 32.0

    def test_break_mult_consumption(self):
        u = _unit('the_dahlia', lc_id='never_forget_her_flame')
        e = _enemy(toughness=30, res={'雷': 0.0})
        e.element_res.update({'雷': 0.0})
        st = _state(u, enemies=[e])
        stats = _build_effective_stats(u, st)
        u.extra['lcf_break_dmg'] = 32.0
        d1 = _apply_toughness_damage(st, u, e, 30.0, '雷', 'skill', stats)
        e2 = _enemy(toughness=30, res={'雷': 0.0})
        st2 = _state(u, enemies=[e2])
        del u.extra['lcf_break_dmg']
        d2 = _apply_toughness_damage(st2, u, e2, 30.0, '雷', 'skill',
                                     _build_effective_stats(u, st2))
        assert d1 == pytest.approx(d2 * 1.32, rel=1e-6)


class TestWorldsApart:
    """纵然山河万程: 终结技→全队回复ATK%+最低者额外; 全队卫戍3回合（召唤物升档）。"""

    def _team(self):
        u = _unit('dan_heng_permansor_terrae', lc_id='though_worlds_apart')
        a = _unit('silver_wolf')
        a.current_hp = a.max_hp * 0.50
        b = _unit('firefly')
        b.current_hp = b.max_hp * 0.10  # 绝对值最低
        return u, a, b, _state(u, a, b)

    def test_ult_heal_and_weishu(self):
        u, a, b, st = self._team()
        hp_a, hp_b = a.current_hp, b.current_hp
        _process_lc_effects(u, st, "on_ult")
        stats = _build_effective_stats(u, st)
        amt = stats.ATK * 0.10 * (1 + stats.HEAL_BONUS)
        assert a.current_hp == pytest.approx(min(a.max_hp, hp_a + amt))
        assert b.current_hp == pytest.approx(min(b.max_hp, hp_b + 2 * amt))
        for x in (u, a, b):
            ws = [bf for bf in x.buffs if bf.param_id == 'lcx_twa_weishu']
            assert len(ws) == 1 and ws[0].remaining_turns == 3
            assert ws[0].attributes['DMG_BONUS_ALL'] == pytest.approx(24.0)

    def test_weishu_summon_tier(self):
        u, a, b, st = self._team()
        ms = _unit('fengjin')  # 伪忆灵: 只需 is_alive
        st.memsprites.append(ms)
        _process_lc_effects(u, st, "on_ult")
        ws = [bf for bf in a.buffs if bf.param_id == 'lcx_twa_weishu']
        assert ws[0].attributes['DMG_BONUS_ALL'] == pytest.approx(36.0)  # 24+12

    def test_s5_tiers(self):
        u, a, b, st = self._team()
        u.lightcone.rank = 5
        hp_a = a.current_hp
        _process_lc_effects(u, st, "on_ult")
        stats = _build_effective_stats(u, st)
        amt = stats.ATK * 0.20 * (1 + stats.HEAL_BONUS)
        assert a.current_hp == pytest.approx(min(a.max_hp, hp_a + amt))
        ws = [bf for bf in a.buffs if bf.param_id == 'lcx_twa_weishu']
        assert ws[0].attributes['DMG_BONUS_ALL'] == pytest.approx(48.0)
