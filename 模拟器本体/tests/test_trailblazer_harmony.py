"""v5.3: 开拓者·同谐（击破辅助）测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _apply_toughness_damage, _super_break_rate
from engine.runtime import SimUnit, SimState


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _sim_tbh(max_av=800, **cfg):
    c = load_character('trailblazer_harmony', 'data/characters')
    return simulate_like(max_av, c, **cfg)


def simulate_like(max_av, c, **cfg):
    from engine.core.combat_engine import simulate
    return simulate([{'char': c, 'position': 1, **cfg}], _enemy(), max_av=max_av)


class TestBounceSkill:
    def test_bounce_hits_5(self):
        """战技弹射 5 次×50%ATK（_hits 解析）; 无行迹2时削韧均分总10"""
        u = _unit('trailblazer_harmony')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        _use_skill(u, state, 'skill')
        assert e.toughness == pytest.approx(200 - 10, abs=1e-6)
        assert '中场馈赠的雨' in '\n'.join(state.log)
        assert u.total_damage_dealt > 0

    def test_e6_bounce_7(self):
        """E6: 战技额外伤害次数+2 → 7跳（削韧总数不变, 均分更多跳）"""
        u = _unit('trailblazer_harmony', eidolon=6)
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        _use_skill(u, state, 'skill')
        assert e.toughness == pytest.approx(200 - 10, abs=1e-6)

    def test_trace2_first_bounce_double_break(self):
        """行迹2: 首次伤害削韧+100%（首跳×2 → 总12）"""
        u = _unit('trailblazer_harmony')
        u.extra['tbh_bounce_first_double'] = True
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        _use_skill(u, state, 'skill')
        assert e.toughness == pytest.approx(200 - 12, abs=1e-6)  # 首跳4 + 4跳×2


class TestBandDance:
    def test_ult_band_dance_buff(self):
        """终结技: 伴舞全队击破特攻+30% + 超击破源"""
        u = _unit('trailblazer_harmony')
        u.current_energy = 140
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        _use_skill(u, state, 'ultimate')
        assert any(getattr(b, 'param_id', '') == 'tbh_band_dance' for b in u.buffs)
        # 伴舞提供超击破源
        assert _super_break_rate(state, u) == pytest.approx(1.0, abs=1e-9)
        # 击破特攻+30%生效（面板快照差异: 直接检查 buff 属性）
        b = next(b for b in u.buffs if getattr(b, 'param_id', '') == 'tbh_band_dance')
        assert b.attributes.get('BREAK_EFFECT') == pytest.approx(30.0, abs=1e-9)

    def test_trace1_super_break_mult_by_enemy_count(self):
        """行迹1: 伴舞超击破按敌人数增伤（1敌 ×1.6）"""
        u = _unit('trailblazer_harmony')
        from engine.runtime import TimedBuff
        u.buffs.append(TimedBuff(source_id='t', attributes={'_tbh_super_break': 1},
                                 remaining_turns=3, param_id='tbh_band_dance'))
        e = _enemy()
        e.is_broken = True
        e.toughness = 0.0
        state = SimState(enemies=[e], units=[u])
        state.current_av = 0.0
        hp0 = e.HP
        _use_skill(u, state, 'basic_attack')
        dmg = hp0 - e.HP
        # 超击破 = 削韧10×(1+BE)×1.6（1敌档）+ 直伤（普攻100%）
        from engine.core.attributes import compute_combat_stats
        stats = compute_combat_stats(u.char, None, None, None)
        sb = 10.0 * (1.0 + stats.BREAK_EFFECT) * 1.6
        assert dmg > sb * 0.99  # 直伤为正, 超击破按行迹1放大


class TestTracesAndTalent:
    def test_trace3_break_delay(self):
        """行迹3: 我方击破后敌方行动延后30%"""
        from engine.core.effect_resolver import _trace_tbh_t3_break_delay
        u = _unit('trailblazer_harmony')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _trace_tbh_t3_break_delay(u, state, enemy=e)
        assert e.extra.get('av_delayed', 0.0) == pytest.approx(3000.0, abs=1e-6)

    def test_talent_break_energy(self):
        """天赋: 敌方弱点被击破时恢复10能量"""
        from engine.core.effect_resolver import _trace_tbh_talent_energy
        u = _unit('trailblazer_harmony')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_tbh_talent_energy(u, state)
        assert u.current_energy == pytest.approx(10.0, abs=1e-6)

    def test_ally_break_triggers_owner_hooks(self):
        """队友击破时，同谐开拓者仍应回能并施加行迹3延后。"""
        from engine.core.effect_resolver import (
            _trace_tbh_t3_break_delay, _trace_tbh_talent_energy,
        )

        tbh = _unit('trailblazer_harmony')
        ally = _unit('seele', position=2)
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[tbh, ally])
        state.current_av = 0.0
        state.hooks.register('trailblazer_harmony', 'on_any_weakness_break',
                             _trace_tbh_t3_break_delay)
        state.hooks.register('trailblazer_harmony', 'on_any_weakness_break',
                             _trace_tbh_talent_energy)

        _use_skill(ally, state, 'basic_attack')

        assert tbh.current_energy == pytest.approx(10.0, abs=1e-6)
        assert e.extra.get('av_delayed', 0.0) == pytest.approx(5500.0, abs=1e-6)


class TestEidolons:
    def test_e1_first_skill_sp(self):
        """E1: 施放首次战技后立即回复1点战技点"""
        from engine.core.effect_resolver import _eid_tbh_e1
        u = _unit('trailblazer_harmony', eidolon=1)
        state = SimState(enemies=[_enemy()], units=[u])
        state.skill_points = 1
        state.current_av = 0.0
        state.hooks.register('trailblazer_harmony', 'on_skill', _eid_tbh_e1)
        _use_skill(u, state, 'skill')  # -1SP 施放 → E1 回1 → 1
        assert state.skill_points == pytest.approx(1.0, abs=1e-9)
        _use_skill(u, state, 'skill')  # 第二次: -1 → 0, E1 不再触发
        assert state.skill_points == pytest.approx(0.0, abs=1e-9)

    def test_e2_energy_regen(self):
        """E2: 战斗开始能量恢复效率+25% 3回合"""
        u = _unit('trailblazer_harmony', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.core.effect_resolver import _eid_tbh_e2
        _eid_tbh_e2(u, state)
        assert any(getattr(b, 'attributes', {}).get('ENERGY_REGEN') for b in u.buffs)

    def test_e4_team_be(self):
        """E4: 队友击破特攻 += 开拓者15%击破特攻"""
        from engine.core.effect_resolver import _eid_tbh_e4, _eid_tbh_e4_death
        tbh = _unit('trailblazer_harmony', eidolon=4)
        ally = _unit('seele', position=2)
        be0 = ally.base_stats.BREAK_EFFECT
        state = SimState(enemies=[_enemy()], units=[tbh, ally])
        _eid_tbh_e4(tbh, state)
        assert ally.base_stats.BREAK_EFFECT == pytest.approx(
            be0 + tbh.base_stats.BREAK_EFFECT * 0.15, abs=1e-9)
        # 阵亡失效
        tbh.is_alive = False
        _eid_tbh_e4_death(tbh, state)
        assert ally.base_stats.BREAK_EFFECT == pytest.approx(be0, abs=1e-9)
