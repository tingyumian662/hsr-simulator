"""v5.7 批次3 回归测试: 缺失机制补齐（嘲讽/TBR星魂/召唤与消失天赋, 2026-08-13）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import _use_skill, _apply_enemy_taunt, _select_enemy_target, _effective_spd
from engine.runtime import SimState, SimUnit, AV_PER_TURN
from engine.core.attributes import compute_combat_stats
from engine.systems.remembrance import RemembranceSystem
from engine.characters.trailblazer_remembrance import _tbr_support_skill


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


def _three_enemies():
    e1, e2, e3 = _enemy(), _enemy(), _enemy()
    e1.id, e2.id, e3.id = 'a', 'b', 'c'
    return [e1, e2, e3]


class TestMydeiTaunt:
    def test_taunt_forces_applier_as_target(self):
        u = _unit('mydei')
        ally = _unit('seele', position=2)
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u, ally])
        _apply_enemy_taunt(state, u, [e1, e2], turns=2)
        assert any(s.name == '嘲讽' for s in e1.statuses)
        t = _select_enemy_target(state, attacker=e1)
        assert t is u
        t2 = _select_enemy_target(state, attacker=e3)
        assert t2 in (u, ally)

    def test_ultimate_applies_taunt_and_priority(self):
        u = _unit('mydei')
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        u.current_energy = 160
        _use_skill(u, state, 'ultimate')
        assert u.extra.get('mydei_priority_target_id') == 'a'
        assert any(s.name == '嘲讽' for s in e1.statuses)
        assert any(s.name == '嘲讽' for s in e2.statuses)


class TestTbrEidolons:
    def test_e1_support_grants_crit(self):
        u = _unit('trailblazer_remembrance', eidolon=1)
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.extra['charge'] = 100
        _tbr_support_skill(state, u, ms)
        assert any(b.attributes.get('CRIT_RATE') == 10.0 for b in ally.buffs)

    def test_e2_memsprite_action_gains_energy_once(self):
        from engine.characters.trailblazer_remembrance import _eid_tbr_e2
        u = _unit('trailblazer_remembrance', eidolon=2)
        fengjin = _unit('fengjin', position=2)
        state = SimState(enemies=[_enemy()], units=[u, fengjin])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, fengjin, fengjin.char.memsprite)
        ms = fengjin.memsprite_unit
        e0 = u.current_energy
        _eid_tbr_e2(u, state, char_id='trailblazer_remembrance', ms_unit=ms)
        assert u.current_energy == pytest.approx(e0 + 8, rel=1e-6)
        _eid_tbr_e2(u, state, char_id='trailblazer_remembrance', ms_unit=ms)
        assert u.current_energy == pytest.approx(e0 + 8, rel=1e-6)

    def test_e2_ignores_own_mimi(self):
        from engine.characters.trailblazer_remembrance import _eid_tbr_e2
        u = _unit('trailblazer_remembrance', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        e0 = u.current_energy
        _eid_tbr_e2(u, state, char_id='trailblazer_remembrance', ms_unit=u.memsprite_unit)
        assert u.current_energy == pytest.approx(e0, rel=1e-6)

    def test_e4_zero_energy_skill_charges_mimi(self):
        u = _unit('trailblazer_remembrance', eidolon=4)
        z = _unit('fu_xuan', position=2)
        z.char.max_energy = 0
        state = SimState(enemies=[_enemy()], units=[u, z])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ch0 = ms.extra.get('charge', 0)
        _use_skill(z, state, 'basic_attack')
        # E4 +3% + 天赋"每累计回10能量+1%"（普攻回20能量→+2%）
        assert ms.extra.get('charge', 0) == pytest.approx(min(100, ch0 + 5), rel=1e-9)


class TestDespawnAdvance:
    def test_mimi_despawn_advances_tbr_25(self):
        u = _unit('trailblazer_remembrance')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 10000.0}
        state.current_av = 100.0
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        expected = 10000.0 - (AV_PER_TURN / _effective_spd(u, state)) * 0.25
        rem.despawn_memsprite(state, u, ms, reason='test')
        assert state.extra['navs'][0] == pytest.approx(expected, rel=1e-9)

    def test_xiaoyika_despawn_advances_fengjin_30(self):
        u = _unit('fengjin')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 10000.0}
        state.current_av = 100.0
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        expected = 10000.0 - (AV_PER_TURN / _effective_spd(u, state)) * 0.30
        rem.despawn_memsprite(state, u, ms, reason='test')
        assert state.extra['navs'][0] == pytest.approx(expected, rel=1e-9)


class TestXilianHelloWorld:
    def test_summon_cleanses_controls(self):
        from engine.runtime import PlayerStatus
        u = _unit('xilian')
        ally = _unit('seele', position=2)
        ally.statuses.append(PlayerStatus(id='frozen', name='冻结', category='control',
                                          remaining_turns=2, attributes={}))
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        assert not ally.statuses


class TestChangyeyueE2Night:
    def test_e2_crit_dmg_both(self):
        u = _unit('changyeyue', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        cd0 = u.base_stats.CRIT_DMG
        rem.summon_memsprite(state, u, u.char.memsprite)
        assert u.base_stats.CRIT_DMG == pytest.approx(cd0 + 0.40, rel=1e-9)
        assert u.memsprite_unit.base_stats.CRIT_DMG == pytest.approx(cd0 + 0.40, rel=1e-9)

    def test_night_abyss_bonus_and_removal(self):
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        assert any(getattr(b, 'param_id', '') == 'changyeyue_night_abyss' for b in u.buffs)
        assert any(getattr(b, 'param_id', '') == 'changyeyue_night_abyss' for b in ms.buffs)
        rem.despawn_memsprite(state, u, ms, reason='test')
        assert not any(getattr(b, 'param_id', '') == 'changyeyue_night_abyss' for b in u.buffs)


class TestFengjinSummonDespawn:
    def test_first_summon_energy_45(self):
        u = _unit('fengjin')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        e0 = u.current_energy
        rem.summon_memsprite(state, u, u.char.memsprite)
        assert u.current_energy == pytest.approx(e0 + 45, rel=1e-6)
