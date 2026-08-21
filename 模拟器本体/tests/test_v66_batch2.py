"""v6.6 批2 回归: 海瑟音/那刻夏/赛飞儿

语义依据: 角色技能介绍/虚无/{海瑟音,赛飞儿}.txt、智识/那刻夏.txt + CLAUDE_HANDOFF v6.6 节"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _use_skill, _begin_enemy_turn,
    _hysilens_apply_dot, _anaxa_add_weakness, _cipher_pick_laozhuke,
)
from engine.systems.remembrance import RemembranceSystem


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


class TestHysilens:
    def test_talent_applies_random_dot(self):
        """天赋: 我方攻击后100%挂随机DOT"""
        u = _unit('hysilens')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _use_skill(ally, state, 'basic_attack')
        assert any(st.id.startswith('hysilens_dot') for st in state.enemies[0].statuses)

    def test_ult_field_and_dot_echo(self):
        """终结技: 结界 + DOT反打"""
        u = _unit('hysilens')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _hysilens_apply_dot(state, u, e)
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert e.extra.get('hysilens_field') is True
        hp_before_turn = e.HP
        _begin_enemy_turn(state, e)
        assert e.HP < hp_before_turn
        assert any('噬魂回响' in l for l in state.log)

    def test_trace1_field_on_start(self):
        """行迹1: 开局展开结界（on_enter_battle）"""
        u = _unit('hysilens')
        state = SimState(enemies=[_enemy()], units=[u])
        # on_enter_battle 由 simulate 触发; 直接验证行迹handler
        from engine.core.effect_resolver import _trace_hysilens_trace1
        _trace_hysilens_trace1(u, state)
        assert state.extra.get('hysilens_field_turns', 0) == 3


class TestAnaxa:
    def test_weakness_added_per_hit(self):
        """天赋: 每击中+1随机弱点"""
        u = _unit('anaxa')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        _use_skill(u, state, 'basic_attack')
        weaks = [s for s in e.statuses if s.id.startswith('anaxa_weak')]
        assert len(weaks) >= 1

    def test_ult_adds_all_weaknesses(self):
        """终结技【升华】: 全7属性弱点（对高抗敌人; 全抗0本就无弱点可加）"""
        u = _unit('anaxa')
        e = _enemy()
        for el in e.element_res:
            e.element_res[el] = 0.20
        state = SimState(enemies=[e], units=[u])
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        ult_weaks = [s for s in e.statuses if s.id.startswith('anaxa_ult_weak')]
        assert len(ult_weaks) == 7


class TestCipher:
    def test_laozhuke_locked(self):
        """天赋: 老主顾锁定（生命上限最高者）"""
        u = _unit('cipher')
        e1, e2 = _enemy(hp=100000), _enemy(hp=900000)
        e1.id, e2.id = 'a', 'b'
        state = SimState(enemies=[e1, e2], units=[u])
        t = _cipher_pick_laozhuke(state, u)
        assert t is e2  # 更高生命上限

    def test_record_and_ult_true_damage(self):
        """记录机制 + 终结技真伤"""
        u = _unit('cipher')
        ally = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, ally])
        e.extra['cipher_laozhuke'] = True
        _use_skill(ally, state, 'basic_attack')
        assert u.extra.get('cipher_record', 0) > 0
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert any('猫咪怪盗' in l for l in state.log)


class TestPoemBatch2:
    def test_hysilens_poem_haiyang(self):
        """献予「海洋」: 暖流+60能+伤害+120%"""
        u = _unit('xilian')
        hs = _unit('hysilens', position=2)
        state = SimState(enemies=[_enemy()], units=[u, hs])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        e0 = hs.current_energy
        rem._xilian_support_skill(state, u, u.memsprite_unit)
        assert hs.extra.get('poem_haiyang') is True
        assert hs.current_energy >= e0 + 60
        assert hs.base_stats.DMG_BONUS_ALL == pytest.approx(1.20)

    def test_cipher_poem_guiji(self):
        """献予「诡计」: 赛飞儿伤害+36%"""
        u = _unit('xilian')
        cp = _unit('cipher', position=2)
        state = SimState(enemies=[_enemy()], units=[u, cp])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        rem._xilian_support_skill(state, u, u.memsprite_unit)
        assert cp.extra.get('poem_guiji') is True
        assert cp.base_stats.DMG_BONUS_ALL == pytest.approx(0.36)
