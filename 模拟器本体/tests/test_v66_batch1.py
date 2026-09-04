"""v6.6 批1 回归: 缇宝/刻律德菈/丹恒·腾荒

语义依据: 角色技能介绍/{同谐/缇宝,刻律德菈,存护/丹恒·腾荒}.txt + CLAUDE_HANDOFF v6.6 节"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _begin_regular_turn
from engine.characters.tribbie import _tribbie_apply_shenqi
from engine.characters.cerydra import _cerydra_grant_jungong
from engine.runtime import SimState, SimUnit, TimedBuff
from engine.systems.remembrance import RemembranceSystem
from engine.characters.xilian import _xilian_support_skill


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


class TestTribbie:
    def test_shenqi_all_res_pen(self):
        """战技【神启】: 全队全属性抗性穿透+24%"""
        u = _unit('tribbie')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        pen0 = ally.base_stats.RES_PEN_ALL
        _use_skill(u, state, 'skill')
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(pen0 + 0.24)

    def test_shenqi_expires_on_turn(self):
        """神启到期回减（tick 递减由 _begin_regular_turn 处理; AI 战技会刷新神启为真实行为）"""
        from engine.characters.tribbie import _tribbie_remove_shenqi
        u = _unit('tribbie')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _tribbie_apply_shenqi(u, state, turns=1)
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(0.24)
        _tribbie_remove_shenqi(u, state)
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(0.0, abs=1e-9)
        assert u.extra.get('tribbie_shenqi_turns') == 0

    def test_ult_field_vulnerability(self):
        """终结技结界: 敌受伤+30%"""
        u = _unit('tribbie')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert e.vulnerability == pytest.approx(0.30)
        assert state.extra.get('tribbie_field_turns') == 2


class TestCerydra:
    def test_jungong_and_charge(self):
        """战技【军功】: 目标获军功 + 充能+1"""
        u = _unit('cerydra')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _use_skill(u, state, 'skill')
        assert ally.extra.get('cerydra_jungong') is True
        assert u.extra.get('cerydra_charge') == 1

    def test_promote_to_juewei(self):
        """充能≥6: 升【爵位】"""
        u = _unit('cerydra')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        for _ in range(6):
            _cerydra_grant_jungong(state, u, ally)
        assert ally.extra.get('cerydra_juewei') is True

    def test_jungong_holder_fua_after_attack(self):
        """天赋: 军功者攻击后刻律德菈60%ATK风附加"""
        u = _unit('cerydra')
        ally = _unit('seele', position=2)
        ally.extra['cerydra_jungong'] = True
        state = SimState(enemies=[_enemy()], units=[u, ally])
        hp0 = state.enemies[0].HP
        _use_skill(ally, state, 'basic_attack')
        assert state.enemies[0].HP < hp0
        assert any('刻律德菈附加' in l for l in state.log)


class TestDanHeng:
    def test_skill_shield_and_tongpao(self):
        """战技: 同袍 + 全队护盾"""
        u = _unit('dan_heng_permansor_terrae')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _use_skill(u, state, 'skill')
        assert ally.extra.get('dht_tongpao') is True
        assert u.shield > 0
        assert ally.shield > 0

    def test_ult_enhances_longling(self):
        """终结技: 龙灵强化2次行动"""
        u = _unit('dan_heng_permansor_terrae')
        state = SimState(enemies=[_enemy()], units=[u])
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert u.extra.get('dht_longling_enhanced') == 2


class TestPoemActivation:
    def test_tribbie_poem_menjing(self):
        """献予「门径」: 缇宝无视12%防御"""
        u = _unit('xilian')
        trib = _unit('tribbie', position=2)
        state = SimState(enemies=[_enemy()], units=[u, trib])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        _xilian_support_skill(state, u, u.memsprite_unit)
        assert trib.extra.get('poem_menjing') is True
        assert trib.base_stats.DEF_PEN == pytest.approx(0.12)
