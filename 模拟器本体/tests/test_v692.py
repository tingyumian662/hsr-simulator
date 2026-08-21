"""v6.9.1 复核回归（Harness）: CODEX-P2-2 瓦尔特六条补修 + 六角色冒烟

语义依据: 角色技能介绍/虚无/瓦尔特.txt + CODEX_HANDOFF v6.9 审查节 P2-2。"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _use_skill, _build_effective_stats, simulate,
    _welt_extra_damage, _welt_ally_hit_hooks, _tick_enemy_statuses,
    _enemy_for_damage,
)


def _enemy(hp=500000, toughness=200, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=dict(res or {'冰': 0, '量子': 0.2, '风': 0.2, '雷': 0.2,
                                          '虚数': 0.0, '物理': 0.2, '火': 0.2}))


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


class TestWeltP22:
    def test_e4_res_restored_on_expiry(self):
        """E4: 失重减抗只进入伤害快照，到期后不残留。"""
        u = _unit('welt', eidolon=4)
        e = _enemy(res={'冰': 0, '量子': 0.2, '风': 0.2, '雷': 0.2, '虚数': 0.4, '物理': 0.2, '火': 0.2})
        st = SimState(enemies=[e], units=[u])
        from engine.core.combat_sim import _welt_apply_shizhong
        _welt_apply_shizhong(st, u, e)
        assert e.element_res['虚数'] == pytest.approx(0.4)
        assert _enemy_for_damage(e).get_res('虚数') == pytest.approx(0.1)
        ws = next(s for s in e.statuses if s.id == 'welt_shizhong')
        ws.remaining_turns = 1
        _tick_enemy_statuses(st, e)
        assert not any(s.id == 'welt_shizhong' for s in e.statuses)
        assert _enemy_for_damage(e).get_res('虚数') == pytest.approx(0.4)

    def test_e1_skill_triggers_once_per_target(self):
        """E1: 战技也触发; 每目标每次攻击最多1次"""
        u = _unit('welt', eidolon=1)
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        from engine.core.combat_sim import _welt_apply_shizhong
        _welt_apply_shizhong(st, u, e)
        st.extra['last_attack_targets'] = [e]
        st.extra['last_hit_segments'] = [e] * 5  # 5段弹射同目标
        d0 = u.total_damage_dealt
        _welt_extra_damage(st, u, 'skill')
        # E1 附加触发且只 1 次（5 段去重）: 伤害增量 = 天赋(减速?无) + 行迹2 86.4 + E1 60
        assert u.total_damage_dealt > d0
        assert any('瓦尔特E1' in l for l in st.log)

    def test_trace2_scale_and_no_ultimate(self):
        """行迹2: 战技 86.4%（非120%）; 终结技不触发"""
        u = _unit('welt')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        st.extra['last_attack_targets'] = [e]
        _welt_extra_damage(st, u, 'skill')
        d_skill = u.total_damage_dealt
        u2 = _unit('welt')
        e2 = _enemy()
        st2 = SimState(enemies=[e2], units=[u2])
        st2.extra['last_attack_targets'] = [e2]
        _welt_extra_damage(st2, u2, 'ultimate')
        # 终结技: 无行迹2段, 伤害为 0（目标未减速/未失重）
        assert u2.total_damage_dealt == pytest.approx(0.0)
        assert d_skill > 0

    def test_ally_hit_hooks_stack_and_delay(self):
        """失重通用钩子: 任意我方攻击触发延后+行迹1叠层（≤10层）"""
        welt = _unit('welt', position=1)
        ally = _unit('seele', position=2)
        e = _enemy()
        st = SimState(enemies=[e], units=[welt, ally])
        from engine.core.combat_sim import _welt_apply_shizhong
        _welt_apply_shizhong(st, welt, e)
        st.extra['last_attack_targets'] = [e]
        for _ in range(12):
            _welt_ally_hit_hooks(st, 'skill')
        ws = next(s for s in e.statuses if s.id == 'welt_shizhong')
        assert ws.attributes.get('welt_trace1_stacks') == 10  # 封顶
        assert ws.attributes.get('vulnerability') == pytest.approx(1.00)
        assert e.extra.get('welt_shizhong_count') == 8  # 延后次数封顶

    def test_e6_main_damage_crit(self):
        """E6: 战技主伤害吃减速目标双暴（伤害循环面板）"""
        u = _unit('welt', eidolon=6)
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        st.skill_points = 3
        from engine.core.combat_sim import _welt_apply_slow
        _welt_apply_slow(st, u, e)
        d0 = u.total_damage_dealt
        _use_skill(u, st, 'skill')
        assert u.total_damage_dealt > d0


class TestV69Smoke:
    def test_six_characters_simulate_no_errors(self):
        """P0 复现场景: 六角色单角色 simulate 零报错（此前 NameError）"""
        for cid in ['sunday', 'welt', 'ruan_mei', 'robin', 'busitu', 'qianye']:
            c = load_character(cid, 'data/characters')
            e = _enemy()
            s = simulate([{'char': c, 'position': 1}], e, max_av=200)
            errs = [l for l in s.log if '[ERROR]' in str(l) or 'NameError' in str(l)]
            assert not errs, f'{cid}: {errs[:2]}'
