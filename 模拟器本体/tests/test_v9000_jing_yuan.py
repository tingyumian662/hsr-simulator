"""v9.0.0 批3: 景元 空壳→完整（神君标记/段数变速/破阵/奇缘1强化）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.characters.jing_yuan import (  # noqa: E402
    _add_segments, _apply_empower, _seg_cap, _shenjun_action, _jy_settle_self,
)
from engine.core.combat_engine import _ensure_marker_system, simulate  # noqa: E402


def _jy_state(eidolon=0, fate_bond=0, enemies=None, spawn_ll=True):
    u = _unit('jing_yuan', eidolon=eidolon, fate_bond=fate_bond)
    st = SimState(enemies=list(enemies or [_enemy(hp=1000000)]), units=[u])
    if spawn_ll:
        sys = _ensure_marker_system(st)
        sys.spawn(st, u, 'jingyuan_shenjun')
    return st


class TestData:
    def test_shape(self):
        c = load_character('jing_yuan', 'data/characters')
        assert (c.base_HP, c.base_ATK, c.base_DEF, c.base_SPD) == (1164, 698, 485, 99)
        assert c.max_energy == 130 and c.taunt == 75
        assert c.skills['ultimate'].multipliers[0].scale == 200.0
        assert c.fate_bonds[0].hook_name == 'jy_bond1'
        assert len(c.eidolons) == 6


class TestSegments:
    def test_cap_and_speed(self):
        st = _jy_state()
        u, m = st.units[0], st.units[0].marker
        assert _seg_cap(u) == 10
        m.extra['segments'] = 3
        _add_segments(st, u, 7)                       # →10 满档(无奇缘不立即行动)
        assert m.extra['segments'] == 10
        assert m.data['base_SPD'] == pytest.approx(60.0 + 10.0 * 7)
        assert m.extra['next_av'] > st.current_av      # 未满立即行动(无奇缘)

    def test_fate_bond_cap30_immediate(self):
        st = _jy_state(fate_bond=1)
        u, m = st.units[0], st.units[0].marker
        assert _seg_cap(u) == 30
        _add_segments(st, u, 27)                       # 3+27=30 满
        assert m.extra['segments'] == 30
        assert m.extra['next_av'] == st.current_av     # 奇缘: 满段立即行动
        assert '立即行动' in '\n'.join(st.log)

    def test_settle_accrual_with_empower(self):
        st = _jy_state(fate_bond=1)
        u, m = st.units[0], st.units[0].marker
        u.buffs = []                                    # 去掉开场强化, 先验证裸加段
        _jy_settle_self(u, st, None, 'skill', 10.0)     # 战技 +2
        assert m.extra['segments'] == 5
        _apply_empower(st, u)                           # 强化期间战技 +2+6
        _jy_settle_self(u, st, None, 'skill', 10.0)
        assert m.extra['segments'] == 13                # 5+2+6
        _jy_settle_self(u, st, None, 'ultimate', 10.0)  # 终结技 +3+12
        assert m.extra['segments'] == 28                # 13+15
        # 行迹3·遣将: 战技后暴击率+10%×2回合
        assert any(b.param_id == 'jy_qianjiang' and b.attributes['CRIT_RATE'] == 10.0
                   for b in u.buffs)


class TestShenjunAction:
    def test_action_damage_reset_and_e2(self):
        st = _jy_state(eidolon=2)
        u, m = st.units[0], st.units[0].marker
        m.extra['segments'] = 5
        hp0 = st.enemies[0].HP
        _shenjun_action(st, m)
        assert st.enemies[0].HP < hp0
        assert u.total_damage_dealt > 0
        assert u.damage_log[-1][0] == '神君·斩勘神形'
        # 行动后重置: 段数3/速度60
        assert m.extra['segments'] == 3 and m.data['base_SPD'] == 60.0
        assert any(b.param_id == 'jy_e2_dmg' and b.attributes['DMG_BONUS_ALL'] == 20.0
                   for b in u.buffs)

    def test_e4_energy_per_segment(self):
        st = _jy_state(eidolon=4)
        u, m = st.units[0], st.units[0].marker
        m.extra['segments'] = 4
        u.current_energy = 10.0
        _shenjun_action(st, m)
        assert u.current_energy == pytest.approx(10.0 + 4 * 2.0)  # 每段+2

    def test_fate_bond_empower_after_action(self):
        st = _jy_state(fate_bond=1)
        u, m = st.units[0], st.units[0].marker
        u.buffs = []                                    # 清开场强化, 验证行动后再挂
        _shenjun_action(st, m)
        emp = next(b for b in u.buffs if b.param_id == 'jy_empower')
        assert emp.attributes['DMG_BONUS_ALL'] == 240.0 and emp.remaining_turns == 4

    def test_pozhen_crit_dmg(self):
        """破阵: 段数≥6 该次行动暴伤+25（以伤害比验证, 单敌确定）。"""
        def _dmg(seg):
            st = _jy_state()
            u, m = st.units[0], st.units[0].marker
            m.extra['segments'] = seg
            _shenjun_action(st, m)
            return u.damage_log[-1][1] / seg
        low, high = _dmg(3), _dmg(6)
        assert high > low  # 6段带破阵(暴伤+25) → 每段均伤更高


class TestFullSim:
    def test_solo_runs(self):
        team = [{'char': load_character('jing_yuan', 'data/characters'),
                 'eidolon': 0, 'fate_bond': 1, 'position': 1}]
        st = simulate(team, _enemy(), max_av=500)
        log = '\n'.join(st.log)
        assert st.units[0].total_damage_dealt > 0
        assert '神君行动' in log and '神君入场' in log
        assert '奇缘·万钧入盏' in log

    def test_technique_first_round_plus3(self):
        team = [{'char': load_character('jing_yuan', 'data/characters'),
                 'eidolon': 0, 'fate_bond': 0, 'position': 1}]
        st = simulate(team, _enemy(), max_av=300)
        log = '\n'.join(st.log)
        assert '神君第1回合段数+3' in log
        # 首轮 = 3(基础)+3(秘技) = 6段; 行迹1·破阵按≥6段应触发
        assert '行迹1·破阵' in log
