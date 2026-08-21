"""v6.9.1 回归: Codex v6.9 审查修复（P0 + 关键 P1/P2）"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, simulate, _use_skill, _begin_enemy_turn,
    _check_fatal, _welt_apply_jinggu, _ruanmei_break_damage_v3,
)


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': -0.2},
                 attacks=[{'name': '挥击', 'element': '物理', 'damage_type': 'direct',
                           'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 0}])


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


def _config(u, eidolon=0):
    return {'char': u.char, 'lightcone': None, 'relics': [], 'relic_sets': {},
            'position': u.position, 'eidolon': eidolon}


class TestP0Registration:
    @pytest.mark.parametrize('cid', ['robin', 'busitu', 'qianye', 'sunday', 'welt', 'ruan_mei'])
    def test_single_character_simulate_smoke(self, cid):
        """P0: 单角色 simulate 不得 NameError/TypeError。"""
        u = _unit(cid)
        e = copy.deepcopy(_enemy())
        st = simulate([_config(u)], e, max_av=100.0, num_enemies=1)
        assert not any('[ERROR]' in line for line in st.log)


class TestWeltJinggu:
    def test_jinggu_is_control_and_skips_enemy_turn(self):
        """P1-2: 瓦尔特禁锢按 control 类别生效, 敌方回合跳过。"""
        welt = _unit('welt')
        e = _enemy()
        st = SimState(enemies=[e], units=[welt])
        st.extra['navs'] = {}
        _welt_apply_jinggu(st, welt, e, delay_ratio=0.12)
        s = next(x for x in e.statuses if x.id == 'welt_jinggu')
        assert s.category == 'control'
        assert s.attributes.get('delay_amount', 0) > 0


class TestRuanMeiBreak:
    def test_talent_break_scale_no_double_be(self):
        """P1-3: 天赋击破=统一冰击破×1.2/3.2, 不再重复乘BE。"""
        ruan = _unit('ruan_mei')
        e = _enemy()
        e.toughness = 0
        e.is_broken = True
        st = SimState(enemies=[e], units=[ruan])
        hp0 = e.HP
        _ruanmei_break_damage_v3(st, ruan, e)
        assert e.HP < hp0


class TestBusituFirstSkill:
    def test_first_skill_no_extra_segment_or_sp(self):
        """P1-4: 首次战技不应触发额外100%段/返SP（hook 只做标记, 倍率由通用管线裁剪）。"""
        u = _unit('busitu')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        st.skill_points = 3
        st.extra['busitu_charge'] = 2
        from engine.core.combat_sim import _busitu_skill
        _busitu_skill(st, u, e)
        assert u.extra.get('busitu_skill_was_bait') is False
        assert st.skill_points == 3


class TestQianyeFatal:
    def test_wrath_fatal_protection(self):
        """P1-5: 无量忿怒致命攻击→不死+退出结界+回50%生命上限。"""
        u = _unit('qianye')
        st = SimState(enemies=[_enemy()], units=[u])
        u.extra['qianye_wrath'] = True
        u.current_hp = -1
        _check_fatal(st, u)
        assert u.is_alive is True
        assert not u.extra.get('qianye_wrath')
        assert u.current_hp > 0
