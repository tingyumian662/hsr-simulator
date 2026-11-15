"""v9.0.0 批5: 欢愉升格体系（阿哈E2全量实现, 项目主裁定③）。

升格 = 阿哈E2 + 持有【欢愉万相】期间: 笑点/好活当赏强化为爽点/绝活共赏;
八条角色强化逐线锚定(银狼/绯英/火花/爻光/真珠/砂金·戏浪/开拓者/阿哈自身)。
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState, TimedBuff  # noqa: E402
from engine.systems.elation import ElationSystem, gain_laugh  # noqa: E402
from engine.characters.aha import ascension_active  # noqa: E402


def _wuxiang(u):
    u.buffs.append(TimedBuff(source_id='aha', attributes={}, remaining_turns=3,
                             param_id='aha_wuxiang', source_name='欢愉万相'))
    return u


def _asc_state(*units, enemies=None, eidolon=2, wuxiang=True):
    aha = _unit('aha', eidolon=eidolon)
    if wuxiang:
        _wuxiang(aha)
    us = [aha] + list(units)
    st = SimState(enemies=list(enemies or [_enemy(hp=2000000)]), units=us)
    st.extra['_elation'] = ElationSystem()
    st.elation_state.battle_start_init([u.char.id for u in us if u.char.path == '欢愉'])
    return st


class TestAscensionCore:
    def test_flag_gates(self):
        st = _asc_state(_unit('zhenzhu'))
        assert ascension_active(st) is True
        st.units[0].buffs = []                     # 欢愉万相消失
        assert ascension_active(st) is False
        st2 = _asc_state(_unit('zhenzhu'), eidolon=1)  # E1 无升格
        assert ascension_active(st2) is False

    def test_refund_10pct(self):
        """爽点返还: 阿哈时刻消耗100→返还10。"""
        st = _asc_state()
        gain_laugh(st, 100.0)
        el = st.extra['_elation']
        el._run_aha_turn(st, n=100.0, consume_pool=True, reschedule=True)
        assert st.laugh_points == pytest.approx(10.0)
        assert '升格·爽点返还' in '\n'.join(st.log)

    def test_aha_self_eff(self):
        """升格自身: 欢愉万相期间暴击率+20%+无视防60%(不限通道)。"""
        from engine.characters.aha import _aha_eff_stats
        aha = _wuxiang(_unit('aha', eidolon=2))
        st = _asc_state()
        st.units[0] = aha
        s = _aha_eff_stats(aha, st, s=copy.deepcopy(aha.base_stats),
                           effective_spd=200)
        assert s.CRIT_RATE == pytest.approx(aha.base_stats.CRIT_RATE + 0.20)
        assert s.DEF_PEN == pytest.approx(aha.base_stats.DEF_PEN + 0.60)
        assert s.ELATION_LEVEL == pytest.approx(0.50)  # 速度≥150 档


class TestYinlangLine:
    def test_hidden_cap_999_and_goodshow_convert(self):
        yl = _unit('yinlang')
        st = _asc_state(yl)
        gain_laugh(st, 400.0)
        assert yl.hidden_score == pytest.approx(400.0)      # 升格: 溢出至999(400<999)
        st.extra['_elation'].grant_good_show(st, 'yinlang', 100.0)
        assert yl.hidden_score == pytest.approx(400.0 + 40.0)  # 绝活共赏→40%隐藏分
        # 无升格: 上限300
        st2 = _asc_state(_unit('yinlang'), wuxiang=False)
        yl2 = st2.units[1]
        st2.extra['_elation'].grant_good_show(st2, 'yinlang', 5.0)  # 先给好活(不动HS)
        gain_laugh(st2, 400.0)
        assert yl2.hidden_score == pytest.approx(300.0)

    def test_eff_cd40(self):
        from engine.characters.yinlang import _yl_eff_stats
        yl = _unit('yinlang')
        st = _asc_state(yl)
        s = _yl_eff_stats(yl, st, copy.deepcopy(yl.base_stats), effective_spd=0)
        assert s is not None
        assert s.CRIT_DMG == pytest.approx(yl.base_stats.CRIT_DMG + 0.40)  # 面板小数
        st.units[0].buffs = []                              # 升格关闭
        s2 = _yl_eff_stats(yl, st, copy.deepcopy(yl.base_stats), effective_spd=0)
        assert s2 is None


class TestSparxieLine:
    def test_elation_x22_and_defpen(self):
        from engine.characters.sparxie import (_sparxie_elation_burst,
                                               _spx_team_defpen)
        spx = _unit('sparxie')
        st = _asc_state(spx)
        _sparxie_elation_burst(spx, st, 'elation_skill')
        assert spx.extra.get('_spx_asc_shot') == 2.2
        assert _spx_team_defpen(None, st) == 0.20
        st.units[0].buffs = []
        assert _spx_team_defpen(None, st) is None

    def test_asc_damage_mod_consumption(self):
        from engine.characters.sparxie import (_spx_asc_damage_mod,
                                               _spx_clear_asc_shot)
        spx = _unit('sparxie')
        spx.extra['_spx_asc_shot'] = 2.2
        # 审核P1-1修正: 弹射段同覆盖→peek 语义, 窗口结束统一清旗
        assert _spx_asc_damage_mod(spx, None, 100.0, None) == pytest.approx(220.0)
        assert _spx_asc_damage_mod(spx, None, 100.0, None) == pytest.approx(220.0)
        assert spx.extra.get('_spx_asc_shot') == 2.2
        _spx_clear_asc_shot(spx, None)
        assert '_spx_asc_shot' not in spx.extra
        assert _spx_asc_damage_mod(spx, None, 100.0, None) is None


class TestYaoguangLine:
    def test_dajidali_x14_and_ult(self):
        from engine.characters.yaoguang import _yaoguang_dajidali, _yg_asc_ult

        def _run(wuxiang_on):
            yao = _unit('yaoguang')
            st = _asc_state(yao, eidolon=2, wuxiang=wuxiang_on)
            st.extra['_elation'].grant_good_show(st, 'yaoguang', 20.0)
            st.extra['last_attack_targets'] = [st.enemies[0]]
            hp0 = st.enemies[0].HP
            _yaoguang_dajidali(st, yao, 'basic_attack')
            return hp0 - st.enemies[0].HP
        base, asc = _run(False), _run(True)
        assert asc / base == pytest.approx(1.4, rel=1e-3)
        # 终结技: +30能量+10爽点
        yao = _unit('yaoguang')
        st = _asc_state(yao)
        yao.current_energy = 10.0
        lp0 = st.laugh_points
        _yg_asc_ult(yao, st, 'ultimate')
        assert yao.current_energy == pytest.approx(40.0)
        assert st.laugh_points == pytest.approx(lp0 + 10.0)


class TestZhenzhuLine:
    def test_cap75_and_retention(self):
        from engine.characters.zhenzhu import _zz_extra_turn_end
        zz = _unit('zhenzhu')
        st = _asc_state(zz)
        el = st.extra['_elation']
        el.grant_good_show(st, 'zhenzhu', 60.0)
        # 开局battle_start已+20, 20+60=80 → 升格上限75截断
        assert st.elation_state.get_good_show_total('zhenzhu') == pytest.approx(75.0)
        el.grant_good_show(st, 'zhenzhu', 30.0)
        assert st.elation_state.get_good_show_total('zhenzhu') == pytest.approx(75.0)
        # 保留20%: 额外回合结束时只收回80%
        st.extra['zz_temp_gs'] = ('zhenzhu', 50.0)
        st.extra['zz_temp_laugh'] = 100.0
        gain_laugh(st, 200.0)
        _zz_extra_turn_end(zz, st, kind='zz_base')
        assert st.elation_state.get_good_show_total('zhenzhu') == pytest.approx(75.0 - 40.0)
        assert st.laugh_points == pytest.approx(200.0 - 80.0)


class TestEvanesciaLine:
    def test_bank_and_480_boost(self):
        from engine.characters.evanescia import (
            _eva_asc_eff_stats, _eva_asc_ult_arm, _eva_asc_damage_mod,
            _eva_goodshow_ascension, _evanescia_ult_cast_post,
            _trace_evanescia_energy_convert)
        eva = _unit('evanescia')
        st = _asc_state(eva)
        eva.current_energy = eva.char.max_energy  # 满能量→溢出
        _trace_evanescia_energy_convert(eva, st, amount=50.0)
        assert eva.extra.get('eva_asc_bank') == pytest.approx(50.0)
        # 480累计→终结技×2.5
        _eva_goodshow_ascension(None, st, char_id='evanescia', amount=500.0)
        assert eva.extra.get('eva_ult_boost') is True
        _eva_asc_ult_arm(eva, st, 'ultimate')
        assert eva.extra.get('_eva_boost_shot') == 2.5
        # 裁定②B: 无视防24%已移 DEF_PEN 通道, damage_mod 仅剩 ×2.5
        assert _eva_asc_damage_mod(eva, st, 100.0, None) == pytest.approx(250.0)
        s = _eva_asc_eff_stats(eva, st, s=copy.deepcopy(eva.base_stats))
        assert s.DEF_PEN == pytest.approx(eva.base_stats.DEF_PEN + 0.24)
        # 终结技后: 银行清空恢复(锁内), 增幅旗清
        eva.current_energy = 0.0
        _evanescia_ult_cast_post(eva, st)
        assert eva.current_energy == pytest.approx(50.0)
        assert 'eva_asc_bank' not in eva.extra
        assert '_eva_boost_shot' not in eva.extra


class TestTrailblazerLine:
    def test_pre_aha_cast(self):
        from engine.characters.trailblazer_elation import _tb_pre_aha_cast
        tb = _unit('trailblazer_elation')
        st = _asc_state(tb)
        st.extra['_elation'].grant_good_show(st, 'trailblazer_elation', 30.0)
        hp0 = st.enemies[0].HP
        _tb_pre_aha_cast(None, st, n=50.0)
        assert st.enemies[0].HP < hp0
        assert st.extra.get('tb_pre_cast_dmg', 0.0) > 0


class TestAvwLine:
    def test_full_sim_aha_solo_branch(self):
        from engine.models.character import load_character
        from engine.core.combat_engine import simulate
        team = [
            {'char': load_character('aventurine_waveflair', 'data/characters'),
             'eidolon': 0, 'fate_bond': 0, 'position': 1},
            {'char': load_character('aha', 'data/characters'),
             'eidolon': 2, 'fate_bond': 0, 'position': 2},
        ]
        st = simulate(team, _enemy(), max_av=400)
        log = '\n'.join(st.log)
        avw = st.units[0]
        assert avw.extra.get('avw_solo') is True          # 阿哈独狼→独狼分支生效
        assert avw.extra.get('avw_aha_solo') is True
        assert '升格·纵享惊涛(阿哈独狼)' in log           # 全队速度+25%


class TestRulingB20261115:
    """八项近似口径裁定(2026-11-15 "1A 2B 3B 4A 5B 6B 7B 8A")中 ③⑦ 落地钉扎。"""

    def test_hidden_clip_on_ascension_end(self):
        """③B: 升格结束瞬间银狼隐藏分回截300(丢弃超出)。"""
        from engine.characters.aha import _aha_ascension_end_tick
        aha = _wuxiang(_unit('aha', eidolon=2))
        yl = _unit('yinlang')
        st = _asc_state()
        st.units = [aha, yl]
        yl.hidden_score = 500.0
        _aha_ascension_end_tick(aha, st)              # 升格中→记录 was=True
        assert yl.hidden_score == pytest.approx(500.0)
        aha.buffs = []                                # 欢愉万相到期
        _aha_ascension_end_tick(aha, st)              # 转换→回截
        assert yl.hidden_score == pytest.approx(300.0)
        assert '回截300' in '\n'.join(st.log)

    def test_hidden_no_clip_below_300(self):
        from engine.characters.aha import _aha_ascension_end_tick
        aha = _wuxiang(_unit('aha', eidolon=2))
        yl = _unit('yinlang')
        st = _asc_state()
        st.units = [aha, yl]
        yl.hidden_score = 120.0
        _aha_ascension_end_tick(aha, st)
        aha.buffs = []
        _aha_ascension_end_tick(aha, st)
        assert yl.hidden_score == pytest.approx(120.0)  # 300以下不动

    def test_death_retract_e1_and_e6_floor(self):
        """⑦B: 阿哈阵亡→E1全队增笑撤除 + E6负债底线清除。"""
        from engine.characters.aha import _aha_on_death
        aha = _unit('aha', eidolon=6)
        zz = _unit('zhenzhu')
        st = _asc_state()                     # aha eidolon=2 基座, 手动覆盖
        st.units = [aha, zz]
        for x in st.units:
            x.base_stats.LAUGH_BOOST = 0.15   # 模拟 E1 INIT 已加
        st.extra['aha_e1_active'] = True
        st.extra['aha_debt_floor_ratio'] = 0.20
        _aha_on_death(zz, st)                 # 非阿哈阵亡: 不动
        assert st.units[0].base_stats.LAUGH_BOOST == pytest.approx(0.15)
        _aha_on_death(aha, st)                # 阿哈阵亡: 撤除
        assert all(x.base_stats.LAUGH_BOOST == pytest.approx(0.0) for x in st.units)
        assert 'aha_e1_active' not in st.extra
        assert 'aha_debt_floor_ratio' not in st.extra
        _aha_on_death(aha, st)                # 幂等: 二次不重复扣
        assert all(x.base_stats.LAUGH_BOOST == pytest.approx(0.0) for x in st.units)


class TestFullTeam:
    def test_e2_full_team_smoke(self):
        """阿哈E2满编欢愉队: 升格全链路无崩溃。"""
        from engine.models.character import load_character
        from engine.core.combat_engine import simulate
        team = [
            {'char': load_character('aha', 'data/characters'),
             'eidolon': 2, 'fate_bond': 0, 'position': 1},
            {'char': load_character('yinlang', 'data/characters'),
             'eidolon': 0, 'fate_bond': 0, 'position': 2},
            {'char': load_character('zhenzhu', 'data/characters'),
             'eidolon': 0, 'fate_bond': 0, 'position': 3},
            {'char': load_character('sparxie', 'data/characters'),
             'eidolon': 0, 'fate_bond': 0, 'position': 4},
        ]
        st = simulate(team, _enemy(hp=5000000), max_av=600)
        log = '\n'.join(st.log)
        assert '欢愉万相' in log                          # 升格源激活过
        assert any(u.total_damage_dealt > 0 for u in st.units)
