"""v9.0.0 批2: 丹恒•饮月 空壳→完整（强化普攻三级链/逆鳞抵扣/亢心叱咤爬坡/奇缘1）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.characters.dan_heng_imbibitor_lunae import (  # noqa: E402
    _dhil_ai, _dhil_attack_type, _dhil_energy_gain, _dhil_settle_ally_ult,
    _dhil_settle_self, _dhil_settle_sp_watch, _dhil_skill_hook, _dhil_sp_cost,
    _gain_scales, _scale_cap,
)
from engine.core.combat_engine import simulate  # noqa: E402


def _st(u=None, enemies=None):
    us = [u] if u is not None else []
    return SimState(enemies=list(enemies or [_enemy()]), units=us)


class TestData:
    def test_shape(self):
        c = load_character('dan_heng_imbibitor_lunae', 'data/characters')
        assert (c.base_HP, c.base_ATK, c.base_DEF, c.base_SPD) == (1241, 698, 363, 102)
        assert c.max_energy == 140 and c.taunt == 125
        for k in ('basic_attack', 'skill', 'skill_hua', 'skill_tian', 'skill_pan', 'ultimate'):
            assert k in c.skills, k
        assert len(c.eidolons) == 6 and c.fate_bonds[0].hook_name == 'dhil_bond1'
        # 折算倍率与段数存档(审核P0-1修正: 满级折算——txt双行取第二值)
        pan = c.skills['skill_pan'].multipliers
        assert (pan[0].scale, pan[0].hits) == (3500.0, 7)    # 7×500%
        assert (pan[1].scale, pan[1].hits) == (720.0, 4)     # 4×180%
        tian = c.skills['skill_tian'].multipliers
        assert (tian[0].scale, tian[1].scale) == (1900.0, 120.0)
        assert c.skills['basic_attack'].multipliers[0].scale == 200.0


class TestScales:
    def test_cap_and_overflow(self):
        u = _unit('dan_heng_imbibitor_lunae')
        st = _st(u)
        assert _scale_cap(u) == 3
        _gain_scales(st, u, 9)
        assert u.extra['dhil_scales'] == 3          # 溢出丢弃
        u.fate_bond_rank = 1
        assert _scale_cap(u) == 4                   # 奇缘1: 逆鳞上限+1
        _gain_scales(st, u, 1)
        assert u.extra['dhil_scales'] == 4

    def test_sp_cost_offset(self):
        """逆鳞优先抵扣SP; 抵扣量计入 lc_sp_spent(视为消耗战技点)。"""
        u = _unit('dan_heng_imbibitor_lunae', fate_bond=1)
        st = _st(u)
        u.extra['dhil_scales'] = 2
        st.extra['lc_sp_spent'] = 10
        remain = _dhil_sp_cost(u, st, 3, 'skill_pan')
        assert remain == 1 and u.extra['dhil_scales'] == 0
        assert st.extra['lc_sp_spent'] == 12
        # 非强化普攻键/无逆鳞 → 不接管
        assert _dhil_sp_cost(u, st, 1, 'skill') is None
        u.extra['dhil_scales'] = 0
        assert _dhil_sp_cost(u, st, 3, 'skill_pan') is None


class TestRamp:
    """亢心/叱咤等差爬坡系数（S4 预计算, damage_mod 消费）。"""

    def test_ramp_values(self):
        u = _unit('dan_heng_imbibitor_lunae')
        st = _st(u)
        _dhil_skill_hook(u, st, 'skill_pan')
        # 亢心 (0+10+20+30+40+50+60)/7=30% + 叱咤 (12+24+36+48)/7
        assert u.extra['dhil_ramp'] == pytest.approx(1.30 + 120.0 / 7 / 100.0)
        _dhil_skill_hook(u, st, 'skill_tian')
        assert u.extra['dhil_ramp'] == pytest.approx(1.20 + 36.0 / 5 / 100.0)
        _dhil_skill_hook(u, st, 'skill_hua')
        assert u.extra['dhil_ramp'] == pytest.approx(1.10)
        _dhil_skill_hook(u, st, 'basic_attack')
        assert u.extra['dhil_ramp'] == pytest.approx(1.05)

    def test_ramp_e1(self):
        u = _unit('dan_heng_imbibitor_lunae', eidolon=1)
        st = _st(u)
        _dhil_skill_hook(u, st, 'skill_pan')
        # E1: 每段2层封顶10 → (0+20+40+60+80+100+100)/7 = 57.14%
        assert u.extra['dhil_ramp'] == pytest.approx(1.0 + 400.0 / 7 / 100.0 + 120.0 / 7 / 100.0)

    def test_settle_layers_buff(self):
        u = _unit('dan_heng_imbibitor_lunae')
        st = _st(u)
        _dhil_settle_self(u, st, None, 'skill_pan', 100.0)
        kang = next(b for b in u.buffs if b.param_id == 'dhil_kangxin')
        assert kang.attributes['DMG_BONUS_ALL'] == 60.0     # 7层×10%
        chi = next(b for b in u.buffs if b.param_id == 'dhil_chizha')
        assert chi.attributes['CRIT_DMG'] == 48.0 and chi.remaining_turns == 1  # 4层×12%
        # E4: 叱咤延一回合
        u2 = _unit('dan_heng_imbibitor_lunae', eidolon=4)
        _dhil_settle_self(u2, _st(u2), None, 'skill_pan', 100.0)
        chi2 = next(b for b in u2.buffs if b.param_id == 'dhil_chizha')
        assert chi2.remaining_turns == 2

    def test_e6_stacks_and_consume(self):
        u = _unit('dan_heng_imbibitor_lunae', eidolon=6)
        st = _st(u)
        mate = _unit('seele')
        st.units.append(mate)
        for _ in range(4):
            _dhil_settle_ally_ult(mate, st, None, 'ultimate', 10.0)
        assert u.extra['dhil_e6'] == 3                     # 封顶3
        _dhil_settle_ally_ult(u, st, None, 'ultimate', 10.0)  # 自身终结技不计
        assert u.extra['dhil_e6'] == 3
        _dhil_skill_hook(u, st, 'skill_pan')               # 盘拏耀跃消耗层
        pen = next(b for b in u.buffs if b.param_id == 'dhil_e6_pen')
        assert pen.attributes['RES_PEN_ALL'] == 60.0
        assert 'dhil_e6' not in u.extra


class TestPhases:
    def test_attack_type_and_energy(self):
        u = _unit('dan_heng_imbibitor_lunae')
        st = _st(u)
        assert _dhil_attack_type(u, st, None, 'skill_pan') == 'basic'
        assert _dhil_attack_type(u, st, None, 'ultimate') is None
        assert _dhil_energy_gain(u, st, 'skill_hua') == 30.0
        assert _dhil_energy_gain(u, st, 'skill_tian') == 35.0
        assert _dhil_energy_gain(u, st, 'skill_pan') == 40.0
        assert _dhil_energy_gain(u, st, 'basic_attack') is None

    def test_ai_tiering(self, monkeypatch):
        u = _unit('dan_heng_imbibitor_lunae')
        st = _st(u)
        u.extra['dhil_scales'] = 0
        st.skill_points = 3
        called = []
        import engine.characters.dan_heng_imbibitor_lunae as m
        monkeypatch.setattr(m, '_use_skill', lambda unit, state, key: called.append(key))
        _dhil_ai(u, st)
        assert called == ['skill_pan']
        st.skill_points = 0
        u.extra['dhil_scales'] = 2
        _dhil_ai(u, st)
        assert called[-1] == 'skill_tian'
        u.extra['dhil_scales'] = 0
        _dhil_ai(u, st)
        assert called[-1] == 'basic_attack'


class TestFateBondFUA:
    def test_sp_watch_fires_every_5(self):
        u = _unit('dan_heng_imbibitor_lunae', fate_bond=1)
        e = _enemy(hp=2000000)
        hp0 = e.HP
        st = _st(u, enemies=[e])
        st.extra['lc_sp_spent'] = 4
        _dhil_settle_sp_watch(None, st, None, 'skill', 1.0)
        assert e.HP == hp0
        assert '沧溟间' not in '\n'.join(st.log)   # 4点不足
        st.extra['lc_sp_spent'] = 5
        _dhil_settle_sp_watch(None, st, None, 'skill', 1.0)
        assert '沧溟间' in '\n'.join(st.log)
        assert u.extra['dhil_scales'] == 1         # +1逆鳞
        bond = next(b for b in u.buffs if b.param_id == 'dhil_bond')
        assert bond.attributes['DMG_BONUS_ALL'] == 240.0 and bond.remaining_turns == 3
        # 10点又一档(跨两次行动累计)
        st.extra['lc_sp_spent'] = 10
        n_before = sum(1 for line in st.log if '沧溟间: 追击' in line)
        _dhil_settle_sp_watch(None, st, None, 'skill', 1.0)
        assert sum(1 for line in st.log if '沧溟间: 追击' in line) == n_before + 1

    def test_gate_fate_bond_zero(self):
        u = _unit('dan_heng_imbibitor_lunae')      # fate_bond=0
        e = _enemy(hp=2000000)
        st = _st(u, enemies=[e])
        st.extra['lc_sp_spent'] = 5
        _dhil_settle_sp_watch(None, st, None, 'skill', 1.0)
        assert '沧溟间' not in '\n'.join(st.log)


class TestFullSim:
    def test_solo_runs(self):
        team = [{'char': load_character('dan_heng_imbibitor_lunae', 'data/characters'),
                 'eidolon': 0, 'fate_bond': 1, 'position': 1}]
        st = simulate(team, _enemy(), max_av=600)
        log = '\n'.join(st.log)
        u = st.units[0]
        assert u.total_damage_dealt > 0
        assert '【逆鳞】' in log and '【亢心】' in log
        # 技技点消耗累计到5 → 奇缘追击
        assert '沧溟间: 追击' in log

    def test_e2_advance(self):
        team = [{'char': load_character('dan_heng_imbibitor_lunae', 'data/characters'),
                 'eidolon': 2, 'fate_bond': 0, 'initial_energy_pct': 100, 'position': 1}]
        st = simulate(team, _enemy(), max_av=600)
        assert 'E2·九斿' in '\n'.join(st.log)
