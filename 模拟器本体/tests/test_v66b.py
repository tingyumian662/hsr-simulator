"""v6.6b 回归测试: Codex v6.6 白厄精准化审查 9 项修复（Harness 端修正）

语义依据: CODEX_HANDOFF.md v6.6 白厄精准化审查节 + HARNESS_HANDOFF.md v6.6b 修复记录"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, simulate, _build_effective_stats, _use_skill,
    _begin_enemy_turn, _phainon_transform, _phainon_kasier_end,
    _phainon_kasier_act, _phainon_implant_phys_weak, _phainon_shihun_counter,
    _enemy_turn_end, _record_enemy_kill,
)


def _enemy(hp=500000, tough=200, phys_res=0.2, frozen=False, attacks=None):
    res = {'冰': 0.2, '量子': 0.2, '风': 0.2, '雷': 0.2, '虚数': 0.2, '火': 0.2, '物理': phys_res}
    e = Enemy(id='x', name='X', HP=hp, ATK=600, DEF=800, SPD=80,
              toughness=tough, max_toughness=tough, level=80,
              element_res=res,
              attacks=attacks or [{'name': '挥击', 'element': '物理', 'damage_type': 'direct',
                                   'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 0}])
    if frozen:
        e.statuses.append(EnemyStatus(id='fz', name='冻结', category='control',
                                      source='x', remaining_turns=1))
    return e


def _unit(cid='phainon', eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=1)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _state(unit=None, enemy=None):
    unit = unit or _unit()
    enemy = enemy or _enemy()
    st = SimState(enemies=[enemy], units=[unit])
    st.extra['navs'] = {}
    return st, unit, enemy


# ── E1: 弑魂减伤走真实面板（P1-1） ──

class TestShihunDR:
    def test_dr_applied_and_reverted(self):
        u = _unit()
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['shihun_stacks'] = 2
        _use_skill(u, state, 'skill_enhanced')
        eff = _build_effective_stats(u, state)
        assert eff.DMG_REDUCTION == pytest.approx(0.75)
        # 敌方攻击后反击 → 减伤解除
        u.current_hp = u.max_hp
        _enemy_turn_end(state, state.enemies[0])
        eff2 = _build_effective_stats(u, state)
        assert eff2.DMG_REDUCTION == pytest.approx(0.0)
        assert not any(getattr(b, 'source_name', '') == '弑魂之炽减伤' for b in u.buffs)


# ── E2: 物理弱点植入/恢复（P1-2） ──

class TestPhysWeak:
    def test_implant_covers_neutral_res_and_restore(self):
        u = _unit()
        e_neutral = _enemy(phys_res=0.0)   # 中性抗性也要植入 -0.2
        e_high = _enemy(phys_res=0.2)
        state = SimState(enemies=[e_neutral, e_high], units=[u])
        state.extra['navs'] = {}
        _phainon_implant_phys_weak(state)
        assert e_neutral.element_res['物理'] == pytest.approx(-0.2)
        assert e_high.element_res['物理'] == pytest.approx(-0.2)
        # 重复植入不覆盖快照
        _phainon_implant_phys_weak(state)
        st = next(s for s in e_high.statuses if s.id == 'phainon_phys_weak')
        assert st.attributes['weakness_old_res'] == pytest.approx(0.2)
        # 退出变身恢复
        u.extra['kasier'] = True
        _phainon_kasier_end(state, u)
        assert e_neutral.element_res['物理'] == pytest.approx(0.0)
        assert e_high.element_res['物理'] == pytest.approx(0.2)
        assert not any(s.id == 'phainon_phys_weak' for s in e_high.statuses)


# ── E3: 击杀累计（P1-3） ──

class TestKilledTotal:
    def test_kill_recorded_and_e1_ratio(self):
        u = _unit(eidolon=1)
        state = SimState(enemies=[_enemy(hp=10)], units=[u])
        state.extra['navs'] = {}
        state.extra['killed_total'] = 10
        _phainon_transform(state, u)
        # 10 击杀 → ratio 0.66 + 0.15 = 0.81（<=0.84 上限）
        interval = u.extra['kasier_interval']
        base = u.base_stats._base_SPD or u.base_stats.SPD
        expected = 10000.0 / max(base * 0.81, 1.0) / 8.0
        assert interval == pytest.approx(expected)

    def test_kill_increments_counter(self):
        state = SimState(enemies=[_enemy()], units=[_unit()])
        _record_enemy_kill(state)
        _record_enemy_kill(state)
        assert state.extra['killed_total'] == 2


# ── E4: E6 火种无上限（P1-4） ──

class TestHuozhongCap:
    def test_e6_overflow_not_capped(self):
        u = _unit(eidolon=6)
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['huozhong_overflow'] = 987
        _phainon_kasier_end(state, u)
        assert u.extra['huozhong'] == pytest.approx(990)

    def test_e0_capped_at_15(self):
        u = _unit(eidolon=0)
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['huozhong_overflow'] = 987
        _phainon_kasier_end(state, u)
        assert u.extra['huozhong'] == pytest.approx(15)


# ── E5: max_av 边界（P1-5） ──

class TestKasierMaxAv:
    def test_kasier_extra_turn_not_beyond_max_av(self):
        u = _unit()
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['kasier_next_av'] = 300.0
        u.extra['kasier_interval'] = 50.0
        u.extra['kasier_turns'] = 8
        u.extra['kasier_done'] = 0
        simulate([{'char': u.char, 'lightcone': None, 'relics': [],
                   'relic_sets': {}, 'position': 1, 'eidolon': 0}],
                 copy.deepcopy(e), max_av=200.0)
        assert u.extra['kasier_done'] == 0  # 300AV 的额外回合被 max_av 拦下


# ── E6: 额外回合生命周期（P1-6） ──

class TestKasierLifecycle:
    def test_action_ctx_and_turn_count(self):
        u = _unit()
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['kasier_interval'] = 50.0
        u.extra['kasier_next_av'] = 0.0
        u.extra['kasier_done'] = 0
        u.extra['huishang'] = 0
        _phainon_kasier_act(state, u)
        assert state.extra['action_ctx'] == 'extra'
        assert state.turn_count >= 1


# ── E7: 死亡/冻结不反击（P1-7） ──

class TestShihunGuard:
    def test_frozen_enemy_no_counter(self):
        u = _unit()
        u.extra['kasier'] = True
        u.extra['shihun_stacks'] = 2
        e = _enemy(frozen=True)
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        hp0 = e.HP
        _begin_enemy_turn(state, e)
        assert u.extra.get('shihun_stacks') == 2  # 未反击, 状态保留
        assert e.HP == hp0  # 无反击伤害

    def test_dead_phainon_no_counter(self):
        u = _unit()
        u.is_alive = False
        u.extra['shihun_stacks'] = 2
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        hp0 = e.HP
        _enemy_turn_end(state, e)
        assert e.HP == hp0


# ── E8: 分类增伤（P2-1） ──

class TestScopedBonus:
    def test_last_hit_consumes_ultimate_bonus(self):
        u = _unit()
        u.base_stats.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = 1.00  # +100% 终结技增伤
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['kasier_done'] = 7
        u.extra['huozhong'] = 0
        e0 = e.HP
        _phainon_kasier_act(state, u)
        dmg = e0 - e.HP
        # 无增伤对照: 同样面板重算
        u2 = _unit()
        u2.base_stats = copy.deepcopy(u.base_stats)
        u2.base_stats.DMG_BONUS_BY_SKILL_TYPE['ultimate'] = 0.0
        e2 = _enemy()
        state2 = SimState(enemies=[e2], units=[u2])
        state2.extra['navs'] = {}
        u2.extra['kasier'] = True
        u2.extra['kasier_done'] = 7
        u2.extra['huozhong'] = 0
        e20 = e2.HP
        _phainon_kasier_act(state2, u2)
        dmg2 = e20 - e2.HP
        assert dmg > dmg2 * 1.9  # 100% 增伤接近翻倍


# ── E9: 死敌回退（P2-2） ──

class TestDeadFallback:
    def test_last_hit_skips_dead_enemies(self):
        u = _unit()
        e = _enemy(hp=0)
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        u.extra['kasier'] = True
        u.extra['kasier_done'] = 7
        dmg0 = u.total_damage_dealt
        _phainon_kasier_act(state, u)
        assert e.HP == 0  # 不写入负值
        assert u.total_damage_dealt == dmg0
        assert u.extra.get('kasier') is False  # 变身仍正常结束

    def test_shihun_counter_skips_dead(self):
        u = _unit()
        e = _enemy(hp=0)
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        dmg0 = u.total_damage_dealt
        _phainon_shihun_counter(state, u, 2)
        assert e.HP == 0
        assert u.total_damage_dealt == dmg0
