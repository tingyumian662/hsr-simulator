"""v8.1.0: 智识十把光锥项目主核实录入（光锥技能介绍/智识, 2026-09-30）。

覆盖: 四星5把(天才们的休憩/宇宙大生意/早餐的仪式感/氤氲麦香的梦/谐乐静默之后)
+ 五星5把(向着不可追问处/忍法帖/拂晓之前/片刻留在眼底/偏偏希望无价)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState  # noqa: E402
from engine.models.equipment import load_lightcone  # noqa: E402


def _state(*units, enemies=None):
    from engine.systems.elation import ElationSystem
    st = SimState(enemies=list(enemies or [_enemy()]), units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


IDS = ['geniuses_repose', 'the_great_cosmic_enterprise', 'the_seriousness_of_breakfast',
       'a_dream_scented_in_wheat', 'after_the_charmony_fall', 'into_the_unreachable_veil',
       'ninjutsu_inscription_dazzling_evilbreaker', 'before_dawn',
       'an_instant_before_a_gaze', 'yet_hope_is_priceless']


class TestData:
    def test_all_ten_five_tiered(self):
        for lc_id in IDS:
            d = load_lightcone(lc_id)
            scaled = [e for e in d.effects if getattr(e, 'values', None)]
            assert scaled, lc_id
            for e in scaled:
                assert len(e.values) == 5 and any(v > 0 for v in e.values), (lc_id, e)

    def test_veil_sp_row_no_values(self):
        d = load_lightcone('into_the_unreachable_veil')
        sp = [e for e in d.effects if e.condition_code == 'event_ult_after_sp']
        assert sp and not getattr(sp[0], 'values', None)  # 布尔条件行不吃缩放


class TestFourStars:
    def test_geniuses_repose_kill_cd(self):
        from engine.core.combat_engine import _lc_geniuses_repose_kill
        u = _unit('himeko_nova', lc_id='geniuses_repose')
        u.lightcone.rank = 5
        st = _state(u)
        _lc_geniuses_repose_kill(st, u)
        b = next(b for b in u.buffs if b.param_id == 'lc_gr_cd')
        assert b.attributes['CRIT_DMG'] == 48.0 and b.remaining_turns == 3

    def test_breakfast_kill_stacks_cap3(self):
        from engine.core.combat_engine import _lc_breakfast_kill
        u = _unit('himeko_nova', lc_id='the_seriousness_of_breakfast')
        u.lightcone.rank = 5
        st = _state(u)
        atk0 = u.base_stats.ATK
        for _ in range(5):
            _lc_breakfast_kill(st, u)
        assert u.extra['lc_breakfast_stacks'] == 3
        assert u.base_stats.ATK == pytest.approx(atk0 + 3 * u.base_stats._base_ATK * 0.08)

    def test_cosmic_enterprise_weakness_vuln(self):
        from engine.core.combat_engine import _lc_cosmic_enterprise
        u = _unit('himeko_nova', lc_id='the_great_cosmic_enterprise')
        u.lightcone.rank = 5
        e = _enemy()
        e.weakness = ['火', '火', '冰', '雷']  # 3种不同属性
        st = _state(u, enemies=[e])
        _lc_cosmic_enterprise(st, u)
        assert e.extra['lc_enterprise_vuln'] == pytest.approx(3 * 0.08)

    def test_dream_wheat_fua_and_ult(self):
        from engine.core.combat_engine import _lc_dream_wheat_fua
        u = _unit('himeko_nova', lc_id='a_dream_scented_in_wheat')
        u.lightcone.rank = 5
        st = _state(u)
        _lc_dream_wheat_fua(st, u)
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == pytest.approx(0.40)
        # 终结技段走 attrs permanent 行
        d = load_lightcone('a_dream_scented_in_wheat')
        ult_row = next(e for e in d.effects
                       if 'DMG_BONUS_ULTIMATE' in (e.attributes or {}))
        assert ult_row.values == [24, 28, 32, 36, 40]

    def test_charmony_spd(self):
        from engine.core.combat_engine import _lc_charmony_spd
        u = _unit('himeko_nova', lc_id='after_the_charmony_fall')
        u.lightcone.rank = 5
        st = _state(u)
        _lc_charmony_spd(st, u)
        b = next(b for b in u.buffs if b.param_id == 'lc_charmony_spd')
        assert b.attributes['SPD_PERCENT'] == 16.0 and b.remaining_turns == 2


class TestFiveStars:
    def test_veil_ult_buff_and_sp(self):
        from engine.core.combat_engine import _lc_veil_ult
        u = _unit('himeko_nova', lc_id='into_the_unreachable_veil')
        u.lightcone.rank = 5
        st = _state(u)
        st.skill_points = 3
        _lc_veil_ult(st, u)
        b = next(b for b in u.buffs if b.param_id == 'lc_veil_dmg')
        assert b.attributes['DMG_BONUS_SKILL'] == 100.0
        assert b.remaining_turns == 3
        # 姬子能量140 → 回1SP
        assert u.char.max_energy >= 140
        assert st.skill_points == 4

    def test_ninjutsu_raiton_cycle(self):
        from engine.core.combat_engine import (_lc_ninjutsu_entry,
                                               _lc_ninjutsu_raiton_arm,
                                               _lc_ninjutsu_raiton_count)
        u = _unit('himeko_nova', lc_id='ninjutsu_inscription_dazzling_evilbreaker')
        u.lightcone.rank = 5
        st = _state(u)
        e0 = u.current_energy
        _lc_ninjutsu_entry(st, u)
        assert u.current_energy == pytest.approx(e0 + 40.0)
        _lc_ninjutsu_raiton_arm(st, u)
        _lc_ninjutsu_raiton_count(st, u)          # 第1次普攻
        assert u.extra.get('lc_ninjutsu_raiton') is True
        navs = st.extra.setdefault('navs', {0: 5000.0})
        _lc_ninjutsu_raiton_count(st, u)          # 第2次→拉条70%并移除
        assert 'lc_ninjutsu_raiton' not in u.extra
        assert navs[0] < 5000.0                   # 行动提前

    def test_before_dawn_dream_cycle(self):
        from engine.core.combat_engine import (_lc_before_dawn_dream_arm,
                                               _lc_before_dawn_dream_consume)
        u = _unit('himeko_nova', lc_id='before_dawn')
        u.lightcone.rank = 5
        st = _state(u)
        _lc_before_dawn_dream_arm(st, u, 'skill')
        fua0 = u.base_stats.DMG_BONUS_BY_ATTACK_TYPE.get('follow_up', 0.0)
        _lc_before_dawn_dream_consume(st, u)
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == \
            pytest.approx(fua0 + 0.80)
        _lc_before_dawn_dream_consume(st, u)      # 无梦身→不再加
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == \
            pytest.approx(fua0 + 0.80)
        _lc_before_dawn_dream_arm(st, u, 'ultimate')  # 重新臂→旧段收回
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == \
            pytest.approx(fua0)

    def test_instant_gaze_energy_cap(self):
        from engine.core.combat_engine import _lc_instant_gaze_energy
        u = _unit('himeko_nova', lc_id='an_instant_before_a_gaze')
        u.lightcone.rank = 5
        st = _state(u)
        u0 = u.base_stats.DMG_BONUS_BY_SKILL_TYPE.get('ultimate', 0.0)
        _lc_instant_gaze_energy(st, u)
        pts = min(u.char.max_energy, 180)
        assert u.base_stats.DMG_BONUS_BY_SKILL_TYPE['ultimate'] == \
            pytest.approx(u0 + pts * 0.60 / 100.0)

    def test_yet_hope_stacks_and_defpen(self):
        from engine.core.combat_engine import (_lc_yet_hope_defpen,
                                               _lc_yet_hope_refresh)
        u = _unit('himeko_nova', lc_id='yet_hope_is_priceless')
        u.lightcone.rank = 5
        st = _state(u)
        u.base_stats.CRIT_DMG = 2.0  # 200% → (200-120)//20=4层
        _lc_yet_hope_refresh(st, u, battle_start=True)
        assert u.extra['lc_yethope_fua'] == 4
        assert u.base_stats.DMG_BONUS_BY_ATTACK_TYPE['follow_up'] == \
            pytest.approx(4 * 0.20)
        _lc_yet_hope_defpen(st, u)
        assert u.base_stats.DEF_PEN_BY_TYPE['ultimate'] == pytest.approx(0.36)
        assert u.base_stats.DEF_PEN_BY_TYPE['follow_up'] == pytest.approx(0.36)
        # 两次非普攻动作→窗口过期
        _lc_yet_hope_refresh(st, u, battle_start=False)
        _lc_yet_hope_refresh(st, u, battle_start=False)
        assert u.base_stats.DEF_PEN_BY_TYPE.get('ultimate', 0.0) == pytest.approx(0.0)
