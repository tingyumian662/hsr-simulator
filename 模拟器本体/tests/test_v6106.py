"""v6.10.6 regression tests: Codex fourth-round audit fixes (A: death pipeline invariants)."""
import pytest

from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState,
    SimUnit,
    _apply_hit,
    _check_fatal,
)
from engine.core.effect_resolver import _fuxuan_e2_fatal_check
from engine.models.character import load_character
from engine.models.enemy import Enemy


def _enemy():
    return Enemy(id="t", name="T", HP=500000.0, ATK=100, DEF=800,
                 SPD=80, toughness=200, max_toughness=200, level=80,
                 element_res={e: 0.0 for e in ("物理", "火", "冰", "雷", "风", "量子", "虚数")})


def _unit(cid, position=1, eidolon=0):
    char = load_character(cid, "data/characters")
    stats = compute_combat_stats(char, None, None, None)
    unit = SimUnit(char=char, base_stats=stats, position=position)
    unit.max_hp = unit.current_hp = stats.HP
    unit.eidolon_rank = eidolon
    return unit


def _state(*units):
    return SimState(enemies=[_enemy()], units=list(units))


# ── A1 负 HP 不变量 ──

def test_hp_never_negative_on_hit():
    unit = _unit("seele")
    unit.current_hp = 100.0
    state = _state(unit)
    _apply_hit(state, unit, 1000.0, state.enemies[0])
    assert unit.current_hp == 0.0  # 钳制到 0, 不是 -900
    _check_fatal(state, unit)
    assert unit.is_alive is False  # 真正死亡


def test_hp_never_negative_through_fatal_check():
    unit = _unit("seele")
    unit.current_hp = -900.0  # 直接构造负 HP（防御新扣血路径）
    state = _state(unit)
    _check_fatal(state, unit)
    assert unit.current_hp == 0.0


# ── A2 on_hp_loss 实际损失 ──

def test_on_hp_loss_receives_actual_loss():
    unit = _unit("seele")
    unit.current_hp = 100.0
    state = _state(unit)
    seen = []
    state.hooks.register("seele", "on_hp_loss",
                         lambda u, state, total_lost=0.0, **_: seen.append(total_lost))
    _apply_hit(state, unit, 1000.0, state.enemies[0])
    assert seen == [pytest.approx(100.0)]  # 实际损失 100, 非请求值 1000


# ── A4 符玄 E2 门控 ──

def test_fuxuan_e0_no_fatal_protection():
    fuxuan = _unit("fu_xuan", eidolon=0)
    ally = _unit("seele", position=2)
    state = _state(fuxuan, ally)
    state.extra['fuxuan_field_turns'] = 3  # 穷观阵开启

    assert _fuxuan_e2_fatal_check(state) is False  # E0 不触发

    # E0 下队友受致命伤正常死亡
    ally.current_hp = 100.0
    _apply_hit(state, ally, 1000.0, state.enemies[0])
    _check_fatal(state, ally)
    assert ally.is_alive is False


# ── B 藿藿禳命 ──

def _huohuo_state(eidolon=0, with_ally=False):
    hh = _unit("huohuo", eidolon=eidolon)
    units = [hh]
    if with_ally:
        units.append(_unit("seele", position=2))
    state = _state(*units)
    return hh, state


def test_huohuo_ruming_on_self_after_skill():
    from engine.core.combat_sim import _use_skill
    hh, state = _huohuo_state(eidolon=0)
    state.skill_points = 5
    _use_skill(hh, state, 'skill')
    assert hh.extra.get('huohuo_ruming_turns') == 3  # 藿藿自身3回合
    ally = next((u for u in state.units if u.char.id == 'seele'), None)
    if ally is not None:
        assert 'huohuo_ruming_turns' not in ally.extra  # 不再挂受疗者


def test_huohuo_ruming_e1_duration():
    from engine.core.combat_sim import _use_skill
    hh, state = _huohuo_state(eidolon=1)
    state.skill_points = 5
    _use_skill(hh, state, 'skill')
    assert hh.extra.get('huohuo_ruming_turns') == 4  # E1 +1回合


def test_huohuo_ruming_tick_and_heal():
    from engine.core.combat_sim import _huohuo_ruming_tick, _huohuo_ruming_heal_all
    hh, state = _huohuo_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    hh.extra['huohuo_ruming_turns'] = 3
    hh.extra['huohuo_ruming_cleanse'] = 6
    ally.current_hp = ally.max_hp * 0.4  # ≤50% 目标

    _huohuo_ruming_tick(state, hh)  # 藿藿回合开始: 3→2
    assert hh.extra['huohuo_ruming_turns'] == 2
    hp_before = ally.current_hp
    _huohuo_ruming_heal_all(state, hh)  # 藿藿回合开始回血: 触发者+≤50%全队
    assert ally.current_hp > hp_before
    assert hh.current_hp > 0


def test_huohuo_ruming_cleanse_negative_status():
    from engine.core.combat_sim import _huohuo_ruming_heal_all, PlayerStatus
    hh, state = _huohuo_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    hh.extra['huohuo_ruming_turns'] = 3
    hh.extra['huohuo_ruming_cleanse'] = 6
    ally.current_hp = ally.max_hp * 0.4
    ally.statuses.append(PlayerStatus(id='stun', name='眩晕', category='control'))

    _huohuo_ruming_heal_all(state, ally)  # 队友回合开始

    assert len(ally.statuses) == 0  # 净化1负面
    assert hh.extra['huohuo_ruming_cleanse'] == 5


def test_huohuo_e2_requires_ruming():
    from engine.core.effect_resolver import _eid_huohuo_e2, _huohuo_e2_fatal_check
    from engine.core.combat_sim import _check_fatal
    hh, state = _huohuo_state(eidolon=2, with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    _eid_huohuo_e2(hh, state)  # charges=2

    # 无禳命: 不触发保护
    assert _huohuo_e2_fatal_check(state) is False

    # 有禳命: 触发（HP 归零进入死亡检查）
    hh.extra['huohuo_ruming_turns'] = 3
    hh.extra['huohuo_ruming_cleanse'] = 6
    ally.current_hp = 0.0
    _check_fatal(state, ally)
    assert ally.is_alive is True
    assert hh.extra['huohuo_ruming_turns'] == 2  # E2 使禳命-1


# ── C 花火谜诡/幻相/战技点 ──

def _sparkle_state(eidolon=0, with_ally=False):
    sp = _unit("sparkle", eidolon=eidolon)
    units = [sp]
    if with_ally:
        units.append(_unit("seele", position=2))
    state = _state(*units)
    return sp, state


def test_sparkle_ult_grants_mystery_and_6_sp():
    from engine.core.combat_sim import _use_skill
    sp, state = _sparkle_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    sp.current_energy = sp.char.max_energy
    state.skill_points = 0

    _use_skill(sp, state, 'ultimate')

    assert state.skill_points == state.max_sp  # 回6但受上限5截断
    assert state.extra.get('sparkle_sp_reserve', 0) == 1  # 溢出1记录
    assert any(getattr(b, 'param_id', '') == 'sparkle_mystery' for b in sp.buffs)
    assert any(getattr(b, 'param_id', '') == 'sparkle_mystery' for b in ally.buffs)


def test_sparkle_ult_overflow_reserve():
    from engine.core.combat_sim import _use_skill
    sp, state = _sparkle_state()
    sp.current_energy = sp.char.max_energy
    state.skill_points = 3  # 3+6=9 > max_sp(5) → 溢出4记录

    _use_skill(sp, state, 'ultimate')

    assert state.skill_points == state.max_sp
    assert state.extra.get('sparkle_sp_reserve', 0) == 4


def test_sparkle_huanxiang_stacks_on_sp_spend():
    from engine.core.combat_sim import _deduct_skill_point_cost
    sp, state = _sparkle_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    state.skill_points = 5

    _deduct_skill_point_cost(state, ally, 1)

    assert sp.extra.get('sparkle_huanxiang') == 1
    enemy = state.enemies[0]
    assert enemy.status_attribute('vulnerability') == pytest.approx(0.04)  # 1层×4%


def test_sparkle_e2_huanxiang_def_down():
    from engine.core.combat_sim import _deduct_skill_point_cost
    sp, state = _sparkle_state(eidolon=2, with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    state.skill_points = 5

    _deduct_skill_point_cost(state, ally, 1)

    enemy = state.enemies[0]
    assert enemy.status_attribute('def_reduction') == pytest.approx(0.10)  # E2 1层×10%


def test_sparkle_max_sp_bonus_and_e4():
    from engine.core.effect_resolver import _trace_sparkle_sp_limit
    sp0, state0 = _sparkle_state(eidolon=0)
    base_max = state0.max_sp
    _trace_sparkle_sp_limit(sp0, state0)
    assert state0.max_sp == base_max + 2  # 天赋+2

    sp4, state4 = _sparkle_state(eidolon=4)
    _trace_sparkle_sp_limit(sp4, state4)
    assert state4.max_sp == base_max + 3  # 天赋+2 + E4+1


def test_sparkle_trace2_free_skill_after_3_sp():
    from engine.core.combat_sim import _deduct_skill_point_cost, _use_skill
    sp, state = _sparkle_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    state.skill_points = 5

    for _ in range(3):
        _deduct_skill_point_cost(state, ally, 1)

    assert state.extra.get('sparkle_free_skill') is True
    sp.current_energy = 0
    before_sp = state.skill_points
    _use_skill(sp, state, 'skill')
    assert state.skill_points == before_sp  # 免SP


def test_sparkle_turn_end_reserve_refill():
    from engine.core.combat_sim import _sparkle_turn_end_reserve
    sp, state = _sparkle_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    state.extra['sparkle_sp_reserve'] = 10
    state.skill_points = 3
    state.max_sp = 7

    _sparkle_turn_end_reserve(state, ally)

    assert state.skill_points == 7  # 补至上限
    assert state.extra['sparkle_sp_reserve'] == 6  # 10-4


# ── D 通用终结技回5能量 ──

def test_ultimate_regains_5_energy():
    from engine.core.combat_sim import _use_skill
    u = _unit("seele")
    state = _state(u)
    u.current_energy = u.char.max_energy
    state.skill_points = 5

    _use_skill(u, state, 'ultimate')

    assert u.current_energy == pytest.approx(5.0)  # 清零后回5（JSON energy_regen）


# ── E 花火AI双拉条（AI 行为由端到端冒烟覆盖; 此处验证通用拉条仅一次生效） ──

def test_sparkle_action_advance_applied_once():
    from engine.core.combat_sim import _use_skill, AV_PER_TURN, _effective_spd
    sp = _unit("sparkle")
    ally = _unit("seele", position=2)
    state = _state(sp, ally)
    state.extra['navs'] = {0: 200.0, 1: 300.0}
    state.extra['av_stamp'] = {0: 1, 1: 2}
    state.extra['stamp_counter'] = 2
    state.skill_points = 5
    state.current_av = 100.0

    nav0 = state.extra['navs'][1]
    _use_skill(sp, state, 'skill')

    # 通用 action_advance 提前50%（完整行动间隔的一半）
    expected = nav0 - (AV_PER_TURN / _effective_spd(ally, state)) * 0.50
    assert state.extra['navs'][1] == pytest.approx(expected, abs=1e-6)


def test_fuxuan_e2_protects_once():
    fuxuan = _unit("fu_xuan", eidolon=2)
    ally = _unit("seele", position=2)
    state = _state(fuxuan, ally)
    state.extra['fuxuan_field_turns'] = 3
    state.extra['fuxuan_e2_used'] = False
    ally.current_hp = 100.0
    ally.max_hp = 3000.0

    _apply_hit(state, ally, 1000.0, state.enemies[0])
    _check_fatal(state, ally)

    assert ally.is_alive is True  # E2 保护生效
    assert ally.current_hp == pytest.approx(0.0 + 3000.0 * 0.70)
    assert state.extra['fuxuan_e2_used'] is True
