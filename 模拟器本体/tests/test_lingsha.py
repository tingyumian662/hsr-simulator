"""v5.3: 灵砂（击破奶妈）测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.damage import calculate_damage
from engine.core.combat_engine import _use_skill, _build_effective_stats
from engine.runtime import SimUnit, SimState
from engine.systems.timeline_marker import TimelineMarkerSystem
from engine.characters.lingsha import _trace_lingsha_t2_be_to_atk_heal, _eid_lingsha_e2_ult


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


def _mk_sys(state):
    sys = TimelineMarkerSystem()
    from engine.characters import (marker_actions, marker_despawns,
                                   marker_spawns)
    sys.action_handlers.update(marker_actions(state))
    sys.despawn_handlers.update(marker_despawns(state))
    sys.spawn_handlers.update(marker_spawns(state))
    state.extra['_marker_sys'] = sys
    return sys


class TestHealAndBuff:
    def test_skill_heal_atk_based(self):
        """战技治疗为 ATK 基数（14%ATK+420, 满级）"""
        lingsha = _unit('lingsha')
        ally = _unit('seele', position=2)
        ally.current_hp -= 1000
        hp0 = ally.current_hp
        e = _enemy()
        state = SimState(enemies=[e], units=[lingsha, ally])
        state.current_av = 0.0
        _use_skill(lingsha, state, 'skill')
        stats = _build_effective_stats(lingsha, state)
        expect = stats.ATK * 0.14 + 420.0
        assert ally.current_hp == pytest.approx(hp0 + expect, abs=1e-6)

    def test_skill_heal_uses_effective_atk(self):
        """战斗中的攻击力增益应提高灵砂战技治疗。"""
        from engine.runtime import TimedBuff

        lingsha = _unit('lingsha')
        lingsha.buffs.append(TimedBuff(
            source_id='test', attributes={'ATK_PERCENT': 50.0}, remaining_turns=1,
        ))
        ally = _unit('seele', position=2)
        ally.current_hp -= 5000
        hp0 = ally.current_hp
        state = SimState(enemies=[_enemy()], units=[lingsha, ally])
        state.current_av = 0.0
        expected = _build_effective_stats(lingsha, state).ATK * 0.14 + 420.0

        _use_skill(lingsha, state, 'skill')

        assert ally.current_hp == pytest.approx(hp0 + expected, abs=1e-6)

    def test_trace2_be_scaling(self):
        """行迹2: ATK/治疗量 = BE×25%/10%（上限50%/20%）"""
        lingsha = _unit('lingsha')
        lingsha.base_stats.BREAK_EFFECT = 1.0
        atk0 = lingsha.base_stats.ATK
        state = SimState(enemies=[_enemy()], units=[lingsha])
        _trace_lingsha_t2_be_to_atk_heal(lingsha, state)
        assert lingsha.base_stats.ATK == pytest.approx(
            atk0 + lingsha.base_stats._base_ATK * 0.25, abs=1e-6)
        assert lingsha.base_stats.HEAL_BONUS == pytest.approx(0.10, abs=1e-9)

    def test_chunzui_break_vulnerability(self):
        """醇醉: 敌方受击破伤害+25%（仅击破类型, 直伤不受影响）"""
        u = _unit('seele')
        stats = compute_combat_stats(u.char, None, None, None)
        e_plain = _enemy()
        e_chun = _enemy()
        e_chun.add_status(EnemyStatus(id='lingsha_chunzui', name='醇醉', category='debuff',
                                      remaining_turns=2,
                                      attributes={'vulnerability_break': 0.25}))
        d_plain = calculate_damage(stats, e_plain, stats.ATK, 100.0, "direct", "量子", 80, False)
        d_chun = calculate_damage(stats, e_chun, stats.ATK, 100.0, "direct", "量子", 80, False)
        assert d_chun.final_damage == pytest.approx(d_plain.final_damage, rel=1e-9)  # 直伤不变
        sb_plain = calculate_damage(stats, e_plain, 0, 0, "super_break", "量子", 80, False,
                                    toughness_dmg=10.0)
        sb_chun = calculate_damage(stats, e_chun, 0, 0, "super_break", "量子", 80, False,
                                   toughness_dmg=10.0)
        assert sb_chun.vulnerability_mult == pytest.approx(
            sb_plain.vulnerability_mult * 1.25, rel=1e-9)  # 超击破+25%


class TestE1:
    def test_e1_def_down_on_break(self):
        """E1: 击破后敌方 DEF-20% (击破期间持续, 韧性恢复移除)"""
        from engine.characters.lingsha import _eid_lingsha_e1_break
        lingsha = _unit('lingsha', eidolon=1)
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[lingsha])
        state.hooks.register('lingsha', 'on_any_weakness_break', _eid_lingsha_e1_break)
        state.current_av = 0.0
        _use_skill(lingsha, state, 'basic_attack')  # 击破
        assert e.status_attribute('def_reduction') == pytest.approx(0.2, abs=1e-9)
        from engine.core.combat_engine import _begin_enemy_turn
        _begin_enemy_turn(state, e)  # 敌方回合: 韧性恢复
        assert e.status_attribute('def_reduction') == pytest.approx(0.0, abs=1e-9)

    def test_e1_def_down_on_ally_break(self):
        """E1 的击破期间减防由任何我方击破触发。"""
        from engine.characters.lingsha import _eid_lingsha_e1_break

        lingsha = _unit('lingsha', eidolon=1)
        ally = _unit('seele', position=2)
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[lingsha, ally])
        state.current_av = 0.0
        state.hooks.register('lingsha', 'on_any_weakness_break', _eid_lingsha_e1_break)

        _use_skill(ally, state, 'basic_attack')

        assert e.status_attribute('def_reduction') == pytest.approx(0.2, abs=1e-9)

    def test_e1_break_efficiency(self):
        """E1: 灵砂自身弱点击破效率+50%"""
        lingsha = _unit('lingsha', eidolon=1)
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[lingsha])
        state.current_av = 0.0
        _use_skill(lingsha, state, 'basic_attack')
        assert e.toughness == pytest.approx(200 - 15.0, abs=1e-6)  # 10×1.5


class TestTrace3:
    def test_ally_hp_loss_triggers_fuyuan_pursuit(self):
        """队友受伤时，灵砂持有的遗爇仍应使浮元立即追击。"""
        from engine.core.combat_engine import _apply_hit
        from engine.characters.lingsha import _trace_lingsha_t3_pursuit

        lingsha = _unit('lingsha')
        ally = _unit('seele', position=2)
        ally.current_hp = ally.max_hp * 0.60
        e = _enemy()
        state = SimState(enemies=[e], units=[lingsha, ally])
        _mk_sys(state).spawn(state, lingsha, 'lingsha_fuyuan')
        state.hooks.register('lingsha', 'on_hp_loss', _trace_lingsha_t3_pursuit)

        _apply_hit(state, ally, 1.0, e)

        assert '遗爇: 浮元立即追击' in '\n'.join(state.log)


class TestE2:
    def test_e2_ult_team_be(self):
        """E2: 终结技后全队击破特攻+40% 3回合"""
        from engine.characters.lingsha import _eid_lingsha_e2_ult
        lingsha = _unit('lingsha', eidolon=2)
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[lingsha, ally])
        state.hooks.register('lingsha', 'on_ultimate', _eid_lingsha_e2_ult)
        state.current_av = 0.0
        _use_skill(lingsha, state, 'ultimate')
        assert any(getattr(b, 'attributes', {}).get('BREAK_EFFECT') == pytest.approx(40.0)
                   for b in ally.buffs)


class TestFuyuanInteg:
    def test_skill_spawns_fuyuan(self):
        """战技: 召唤浮元(3次) + 全队治疗 + 浮元提前20%"""
        lingsha = _unit('lingsha')
        ally = _unit('seele', position=2)
        ally.current_hp -= 1000
        e = _enemy()
        state = SimState(enemies=[e], units=[lingsha, ally])
        _mk_sys(state)
        state.current_av = 0.0
        _use_skill(lingsha, state, 'skill')
        assert lingsha.marker is not None
        assert lingsha.marker.extra['charges'] == 3
        assert '浮元入场' in '\n'.join(state.log)
        assert lingsha.marker.extra['next_av'] == pytest.approx(10000.0 / 90.0 * 0.8, abs=1e-6)

    def test_skill_refresh_fuyuan_charges(self):
        """浮元在场时战技: 行动次数+3（上限5）"""
        lingsha = _unit('lingsha')
        e = _enemy()
        state = SimState(enemies=[e], units=[lingsha])
        _mk_sys(state)
        state.current_av = 0.0
        _use_skill(lingsha, state, 'skill')  # 3
        _use_skill(lingsha, state, 'skill')  # +3 → 5 (cap)
        assert lingsha.marker.extra['charges'] == 5

    def test_e6_team_res_down(self):
        """E6: 浮元在场→敌方全属性抗性-20%, 消失恢复"""
        lingsha = _unit('lingsha', eidolon=6)
        e = _enemy()
        state = SimState(enemies=[e], units=[lingsha])
        _mk_sys(state)
        state.current_av = 0.0
        _use_skill(lingsha, state, 'skill')
        assert e.element_res['火'] == pytest.approx(-0.20, abs=1e-9)
        # 消耗行动次数（3+3=5次）→ 浮元消失 → 抗性恢复
        for _ in range(6):
            m = lingsha.marker
            if m is None:
                break
            _mk_sys(state).handle_action(state, m)
        assert lingsha.marker is None
        assert e.element_res['火'] == pytest.approx(0.0, abs=1e-9)

    def test_e6_fuyuan_handles_no_remaining_enemy(self):
        """E6 浮元清场后不应继续对空目标随机取样。"""
        from engine.characters.lingsha import _lingsha_fuyuan_action

        lingsha = _unit('lingsha', eidolon=6)
        e = _enemy(hp=1)
        state = SimState(enemies=[e], units=[lingsha])

        _lingsha_fuyuan_action(state, marker=None)

        assert e.HP <= 0
