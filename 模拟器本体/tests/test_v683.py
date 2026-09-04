"""v6.8.3 回归: Codex v6.8~v6.8.2 全量审查 6 项修复

语义依据: CODEX_HANDOFF.md「v6.8~v6.8.2 全量审查」节。"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill
from engine.characters.hysilens import _hysilens_dot_trigger_v3
from engine.characters.cerydra import _cerydra_grant_jungong
from engine.characters.tribbie import _tribbie_field_extra_damage, _tribbie_talent_fua
from engine.runtime import SimState, SimUnit


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
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


class TestHysilensEcho:
    def test_echo_deals_damage_immediately_and_no_status(self):
        """P1-1: 反打立即结算 80%ATK DOT, 不再写无快照的 hysilens_echo。"""
        hs = _unit('hysilens')
        e = _enemy()
        e.extra['hysilens_field'] = True
        st = SimState(enemies=[e], units=[hs])
        hp0 = e.HP
        d = _hysilens_dot_trigger_v3(st, hs, e)
        assert d > 0
        assert e.HP < hp0
        assert hs.total_damage_dealt > 0
        assert not any(s.id == 'hysilens_echo' for s in e.statuses)
        assert st.extra.get('hysilens_trigger_count') == 1


class TestAnaxaBounce:
    def test_skill_has_five_hits_after_data_fix(self):
        """P1-2 根因一: 战技弹射段数应为 5。"""
        c = load_character('anaxa', 'data/characters')
        m = c.skills['skill'].multipliers[0]
        assert m.hits == 5

    def test_skill_adds_five_weaknesses_not_six(self):
        """P1-2 根因二: 单敌 5 段只应加 5 次弱点, 不再叠加去重目标集。"""
        ax = _unit('anaxa')
        e = _enemy()
        st = SimState(enemies=[e], units=[ax])
        st.skill_points = 3
        _use_skill(ax, st, 'skill')
        weak_count = len([s for s in e.statuses if s.id.startswith('anaxa_weak')])
        assert weak_count == 5
        assert len(st.extra.get('last_hit_segments', [])) == 5


class TestCerydraSameTarget:
    def test_same_juewei_target_does_not_stack_res_pen(self):
        """P1-3: 同爵位目标重施战技只刷新, 不再重复叠加全抗穿透。"""
        cery = _unit('cerydra')
        target = _unit('seele', position=2)
        st = SimState(enemies=[_enemy()], units=[cery, target])
        cery.extra['cerydra_charge'] = 5
        _cerydra_grant_jungong(st, cery, target)  # 首次 → 升爵位
        assert target.extra.get('cerydra_juewei') is True
        assert target.base_stats.RES_PEN_ALL == pytest.approx(0.10)
        _cerydra_grant_jungong(st, cery, target)  # 同目标重施 → 不得重复升变
        assert target.extra.get('cerydra_juewei') is True
        assert target.base_stats.RES_PEN_ALL == pytest.approx(0.10)


class TestTribbieKillPipeline:
    def test_field_extra_damage_records_kill(self):
        """P2-1: 结界附加击杀走统一击杀管线。"""
        trib = _unit('tribbie')
        e = _enemy(hp=1)
        st = SimState(enemies=[e], units=[trib])
        st.extra['tribbie_field_turns'] = 2
        _tribbie_field_extra_damage(st, trib, [e], total_dmg=0.0)
        assert e.HP <= 0
        assert st.extra.get('killed_total', 0) == 1
        assert st.extra.get('killed_this_action', 0) == 1

    def test_talent_fua_records_kill(self):
        """P2-1: 天赋 FUA 击杀走统一击杀管线。"""
        trib = _unit('tribbie')
        e = _enemy(hp=1)
        st = SimState(enemies=[e], units=[trib])
        _tribbie_talent_fua(st, trib)
        assert e.HP <= 0
        assert st.extra.get('killed_total', 0) == 1
        assert st.extra.get('killed_this_action', 0) == 1


class TestMainEntry:
    def test_main_uses_app_object_no_reload(self):
        """v6.11.0 修复: python main.py 传 app 对象 + reload=False
        （Python 3.14 下 import string 子进程 cwd 不含项目目录 → No module named 'web'）"""
        with open('main.py', encoding='utf-8') as f:
            source = f.read()
        assert 'uvicorn.run(app' in source
        assert 'reload=False' in source
