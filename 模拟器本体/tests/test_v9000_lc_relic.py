"""v9.0.0 批6: 双新欢愉光锥 + 双新内圈遗器 + 映射/推荐。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.models.equipment import load_lightcone  # noqa: E402
from engine.core.combat_engine import (  # noqa: E402
    _lc6_first_aha_ally_skill, _lc_count_evaluate, _lc_state_aha_alive,
    simulate)


class TestLightConeData:
    def test_shapes(self):
        aha = load_lightcone('when_the_first_aha_rings_out')
        assert (aha.rarity, aha.path, aha.base_ATK) == (5, '欢愉', 582)
        worlds = load_lightcone('meet_at_worlds_end')
        assert (worlds.rarity, worlds.path, worlds.base_ATK) == (4, '欢愉', 476)
        # 五档齐备
        for lc in (aha, worlds):
            assert all(len(getattr(e, 'values', []) or []) == 5
                       for e in lc.effects if getattr(e, 'values', None))

    def test_mapping_entries(self):
        import json
        m = json.load(open('data/light_cone_characters.json', encoding='utf-8'))
        assert m['当第一声「阿哈」响起'] == '星神★阿哈'
        assert m['在世界尽头相会吧！'] == '—'


class TestFirstAhaCone:
    def test_trigger_laugh_defpen_icd(self):
        u = _unit('aha', lc_id='when_the_first_aha_rings_out')
        mate = _unit('zhenzhu')
        st = SimState(enemies=[_enemy()], units=[u, mate])
        st.extra['_elation'] = ElationSystem()
        lp0 = st.laugh_points
        u.extra['lc_last_skill_target'] = mate
        _lc6_first_aha_ally_skill(st, u)
        assert st.laugh_points == pytest.approx(lp0 + 10.0)      # S1 笑点10
        assert u.base_stats.DEF_PEN == pytest.approx(0.16)        # S1 全队无视防16%
        assert mate.base_stats.DEF_PEN == pytest.approx(0.16)
        # 同一自身行动周期 ICD(裁定⑤B: 按佩戴者 action_counts) + 防穿一次性
        st.laugh_points = lp0
        _lc6_first_aha_ally_skill(st, u)
        assert st.laugh_points == pytest.approx(lp0)
        assert u.base_stats.DEF_PEN == pytest.approx(0.16)
        # 下次自身行动(计数+1)后可再触发笑点
        st.action_counts['aha'] = st.action_counts.get('aha', 0) + 1
        _lc6_first_aha_ally_skill(st, u)
        assert st.laugh_points == pytest.approx(lp0 + 10.0)
        assert u.base_stats.DEF_PEN == pytest.approx(0.16)        # 防穿仍不叠加

    def test_no_fire_for_self_target(self):
        u = _unit('aha', lc_id='when_the_first_aha_rings_out')
        st = SimState(enemies=[_enemy()], units=[u])
        st.extra['_elation'] = ElationSystem()
        u.extra['lc_last_skill_target'] = u
        _lc6_first_aha_ally_skill(st, u)
        assert '当第一声「阿哈」响起' not in '\n'.join(st.log)


class TestWorldsEndCone:
    def test_count_and_aha_evaluators(self):
        aha = _unit('aha')
        zz = _unit('zhenzhu')
        yl = _unit('yinlang')
        st = SimState(enemies=[_enemy()], units=[aha, zz, yl])
        assert _lc_count_evaluate(st, aha, 'elation_allies') == 3   # 含自身
        assert _lc_state_aha_alive(aha, st) is True
        st2 = SimState(enemies=[_enemy()], units=[zz, yl])
        assert _lc_state_aha_alive(zz, st2) is False


class TestNewRelics:
    def test_yakong_speed_threshold(self):
        import json
        d = json.load(open('data/relics/329_亚空异容所.json', encoding='utf-8'))
        assert d['effects'][0]['condition'] == 'spd_threshold_145_team_be'
        from engine.core.attributes import CombatStats, _eval_relic_condition
        s = CombatStats()
        s.SPD = 150.0
        _eval_relic_condition(s, 'spd_threshold_145_team_be', {}, None)
        assert s.BREAK_EFFECT == pytest.approx(0.18)
        s2 = CombatStats()
        s2.SPD = 140.0
        _eval_relic_condition(s2, 'spd_threshold_145_team_be', {}, None)
        assert s2.BREAK_EFFECT == pytest.approx(0.0)

    def test_yakong_team_dynamic_grant_and_revoke(self):
        """裁定⑥B: 队友分发按战斗有效速度实时判定(授予/跌破撤除/不可叠加)。"""
        from engine.core.relic_conditions import _yakong_team_be_apply
        wearer = _unit('zhenzhu')
        mate = _unit('seele')
        st = SimState(enemies=[_enemy()], units=[wearer, mate])
        st.extra['_elation'] = ElationSystem()
        wearer._active_relic_conditions = {'spd_threshold_145_team_be'}
        wearer.base_stats.SPD = 150.0
        _yakong_team_be_apply(wearer, st)
        assert mate.base_stats.BREAK_EFFECT == pytest.approx(0.18)   # 授予队友
        assert wearer.base_stats.BREAK_EFFECT == pytest.approx(0.0)  # 佩戴者自给走静态链
        _yakong_team_be_apply(wearer, st)                            # 不可叠加
        assert mate.base_stats.BREAK_EFFECT == pytest.approx(0.18)
        wearer.base_stats.SPD = 130.0                                # 跌破→撤除
        _yakong_team_be_apply(wearer, st)
        assert mate.base_stats.BREAK_EFFECT == pytest.approx(0.0)
        assert 'yakong_be_owner' not in st.extra

    def test_shunxi_elation_advance(self):
        from engine.core.relic_conditions import _elation_40_advance
        yl = _unit('yinlang')          # 银狼行迹1: 速度≥160→欢愉度≥50%≥40%
        st = SimState(enemies=[_enemy()], units=[yl])
        st.extra['_elation'] = ElationSystem()
        yl.base_stats.SPD = 170.0
        _elation_40_advance(yl, st)
        assert '瞬息欢愉' in '\n'.join(st.log)
        # 欢愉度不足40%: 不触发
        zz = _unit('zhenzhu')
        st2 = SimState(enemies=[_enemy()], units=[zz])
        st2.extra['_elation'] = ElationSystem()
        _elation_40_advance(zz, st2)
        assert '瞬息欢愉' not in '\n'.join(st2.log)

    def test_relic_counts(self):
        import glob
        inner = [f for f in glob.glob('data/relics/3*.json')]
        assert len(inner) == 30  # 301-330（v9.0.0 新增 329/330）


class TestFullSim:
    def test_aha_with_own_cone(self):
        team = [{'char': load_character('aha', 'data/characters'),
                 'lightcone': load_lightcone('when_the_first_aha_rings_out'),
                 'eidolon': 0, 'fate_bond': 0, 'position': 1},
                {'char': load_character('zhenzhu', 'data/characters'),
                 'eidolon': 0, 'fate_bond': 0, 'position': 2}]
        st = simulate(team, _enemy(), max_av=400)
        log = '\n'.join(st.log)
        assert st.units[0].total_damage_dealt > 0
        # 令使点名(单体我方技能)触发光锥
        assert '当第一声「阿哈」响起' in log
