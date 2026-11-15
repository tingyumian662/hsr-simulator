"""v9.0.0 批4: 星神★阿哈本体（愿力/令使点名/欢愉万相/阿哈时刻接管/欢愉负债）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.models.elation import calc_aha_speed  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402
from engine.characters.aha import (  # noqa: E402
    WISH_ULT, _aha_cast_envoy_skill, _aha_cast_ult, _aha_elation_settle,
    _aha_laugh_gain, _aha_ally_turn_start, _aha_on_heal, _aha_turn_tick,
    _wish_gain,
)
from engine.core.combat_engine import simulate  # noqa: E402


def _es(units):
    st = SimState(enemies=[_enemy(hp=1000000)], units=list(units))
    st.extra['_elation'] = ElationSystem()
    return st


class TestData:
    def test_shape(self):
        c = load_character('aha', 'data/characters')
        assert (c.base_HP, c.base_ATK, c.base_DEF, c.base_SPD) == (1047, 679, 509, 94)
        assert c.path == '欢愉' and c.element == '量子'
        assert c.cast_number == 0            # 参演编号0(专属天赋: 无视编号最后施放)
        assert c.max_energy == 10 and c.energy_type == 'special'
        assert c.fate_bonds == []            # 阿哈无奇缘星魂
        assert 'elation_skill' in c.skills

    def test_aha_speed_base_takeover(self):
        """专属天赋: 阿哈在场→阿哈速度公式基础 80→94(白值)。"""
        assert calc_aha_speed([100.0], base_speed=94.0) == pytest.approx(94.0 + 20.0)
        assert calc_aha_speed([100.0]) == pytest.approx(100.0)  # 默认80不变


class TestWish:
    def test_gain_cap(self):
        aha = _unit('aha')
        st = _es([aha])
        for _ in range(15):
            _wish_gain(st, aha, 1)
        assert aha.extra['aha_wish'] == 10            # 上限10(溢出可容至10不超)

    def test_ult_consume_and_wuxiang(self):
        aha = _unit('aha')
        st = _es([aha])
        aha.extra['aha_wish'] = 10.0
        _aha_cast_ult(aha, st)
        assert aha.extra['aha_wish'] == 2.0           # 耗8
        assert any(b.param_id == 'aha_wuxiang' and b.remaining_turns == 3
                   for b in aha.buffs)
        assert aha._pending_action_advance > 0        # 行动提前20%

    def test_casts_grant_wish(self):
        aha = _unit('aha')
        st = _es([aha])
        _aha_elation_settle(aha, st, None, 'basic_attack', 100.0)
        _aha_elation_settle(aha, st, None, 'skill', 100.0)
        _aha_elation_settle(aha, st, None, 'elation_skill', 100.0)
        assert aha.extra['aha_wish'] == 3             # 施放普攻/战技/欢愉技后各+1

    def test_talent40_once_per_turn(self):
        aha = _unit('aha')
        zz = _unit('zhenzhu')
        st = _es([aha, zz])
        from engine.systems.elation import gain_laugh
        gain_laugh(st, 40)
        _aha_laugh_gain(None, st)
        assert aha.extra['aha_wish'] == 1
        assert any(b.param_id == 'aha_talent_cd' for b in zz.buffs)  # 全队暴伤buff
        gain_laugh(st, 5)                              # 同回合再涨不触发
        _aha_laugh_gain(None, st)
        assert aha.extra['aha_wish'] == 1
        _aha_turn_tick(aha, st)                        # 阿哈回合结束→再武装
        gain_laugh(st, 5)                              # 45≥40 但 armed 后仍需达标检查
        _aha_laugh_gain(None, st)
        assert aha.extra['aha_wish'] == 2

    def test_envoy_turn_start_grant(self):
        """令使点名回填(昔涟未来模式): 令使目标回合开始→阿哈+1愿力。"""
        aha = _unit('aha')
        zz = _unit('zhenzhu')
        st = _es([aha, zz])
        zz.has_envoy = True
        _aha_ally_turn_start(zz, st)
        assert aha.extra['aha_wish'] == 1
        _aha_ally_turn_start(aha, st)                  # 非令使目标不触发
        assert aha.extra['aha_wish'] == 1

    def test_trace1_heal_once_per_ult(self):
        aha = _unit('aha')
        zz = _unit('zhenzhu')
        st = _es([aha, zz])
        _aha_on_heal(zz, st, healer=zz, targets=[aha, zz])
        assert aha.extra['aha_wish'] == 1
        _aha_on_heal(zz, st, healer=zz, targets=[aha])  # 每终结技1次
        assert aha.extra['aha_wish'] == 1
        aha.extra['aha_tr1_used'] = False              # 终结技后重置
        _aha_on_heal(zz, st, healer=zz, targets=[aha])
        assert aha.extra['aha_wish'] == 2

    def test_envoy_skill_designate_and_fua(self):
        aha = _unit('aha')
        zz = _unit('zhenzhu')
        st = _es([aha, zz])
        e = st.enemies[0]
        hp0 = e.HP
        _aha_cast_envoy_skill(aha, st)
        assert zz.has_envoy and any(b.param_id == 'aha_envoy_elation' for b in zz.buffs)
        assert e.HP < hp0                              # 追击50%全体(单敌)
        # 复唱转移: 再点名(真珠已是唯一目标, 验证旧标记清理不报错)
        _aha_cast_envoy_skill(aha, st)
        assert sum(1 for x in st.units if getattr(x, 'has_envoy', False)) == 1


class TestElationCore:
    def test_aha_casts_last_in_window(self):
        """参演编号0: 阿哈时刻内阿哈始终最后施放。"""
        aha = _unit('aha')
        zz = _unit('zhenzhu')      # cast 104
        st = _es([aha, zz])
        el = st.extra['_elation']
        from engine.systems.elation import gain_laugh
        gain_laugh(st, 30)
        order = []
        import engine.characters.aha as aha_mod
        import engine.characters.zhenzhu as zz_mod
        # 借 damage_log 记序
        el._run_aha_turn(st, n=30, consume_pool=True, reschedule=True)
        # 直接按窗口执行顺序断言: 真珠欢愉技在前(有 rider/伤害), 阿哈真伤段在后
        log = '\n'.join(st.log)
        assert log.index('真珠 欢愉技') < log.index('欢愉技真伤')

    def test_window_damage_true_rider(self):
        aha = _unit('aha')
        st = _es([aha])
        aha.extra['aha_wish'] = 9.0
        e = st.enemies[0]
        hp0 = e.HP
        st.extra['aha_window_dmg'] = 1000.0
        _aha_elation_settle(aha, st, None, 'elation_skill', 0.0)
        assert e.HP < hp0
        assert any('欢愉技真伤' in line for line in st.log)

    def test_e4_true_ratio(self):
        def _true(unit):
            st = _es([unit])
            st.extra['aha_window_dmg'] = 1000.0
            e = st.enemies[0]
            hp0 = e.HP
            _aha_elation_settle(unit, st, None, 'elation_skill', 0.0)
            return hp0 - e.HP
        base = _true(_unit('aha'))
        e4 = _true(_unit('aha', eidolon=4))
        assert base == pytest.approx(200.0)      # 1000×20%
        assert e4 == pytest.approx(250.0)        # E4: ×1.25


class TestDebt:
    def test_accumulation_and_cap(self):
        aha = _unit('aha')
        st = _es([aha])
        st.extra['aha_debt_on'] = True
        e = st.enemies[0]
        from engine.core.combat_engine import _commit_enemy_damage
        _commit_enemy_damage(st, aha, e, 1000.0)
        assert e.extra['aha_debt'] == pytest.approx(250.0)            # 1000×25%
        assert e.HP > 0                                             # 未到负债线
        _commit_enemy_damage(st, aha, e, 1000000.0)                   # 巨量→封顶+占线即死
        assert e.extra['aha_debt'] == pytest.approx(e.max_hp * 0.10)
        assert e.HP == 0.0                                          # 裁定: 仅占用血条

    def test_e6_floor_effective_80pct_hp(self):
        """E6: 底线20%→敌人实际只有80%血量(生命降至20%线即死)。"""
        aha = _unit('aha', eidolon=6)
        st = _es([aha])
        st.extra['aha_debt_on'] = True
        st.extra['aha_debt_floor_ratio'] = 0.20   # E6 INIT 所置
        e = st.enemies[0]
        from engine.core.combat_engine import _commit_enemy_damage
        _commit_enemy_damage(st, aha, e, 100.0)
        assert e.extra['aha_debt'] == pytest.approx(e.max_hp * 0.20)  # E6底线20%
        assert e.HP > 0
        # 打到剩余 25% 血(>20%线): 不死; 再打 6%: 剩19%<20%线 → 死
        _commit_enemy_damage(st, aha, e, e.max_hp * 0.75)
        assert e.HP > 0
        _commit_enemy_damage(st, aha, e, e.max_hp * 0.06)
        assert e.HP == 0.0


class TestFullSim:
    def test_solo_runs(self):
        team = [{'char': load_character('aha', 'data/characters'),
                 'eidolon': 0, 'fate_bond': 0, 'position': 1}]
        st = simulate(team, _enemy(), max_av=500)
        log = '\n'.join(st.log)
        aha = st.units[0]
        assert aha.total_damage_dealt > 0
        assert '阿哈时刻在' in log and '满月，是神在的时间' in log
        assert '【愿力】' in log
        assert aha.extra.get('aha_debt', 0) > 0 or st.enemies[0].extra.get('aha_debt', 0) > 0

    def test_e1_entry_wish(self):
        team = [{'char': load_character('aha', 'data/characters'),
                 'eidolon': 1, 'fate_bond': 0, 'position': 1}]
        st = simulate(team, _enemy(), max_av=200)
        aha = st.units[0]
        log = '\n'.join(st.log)
        assert '【愿力】: 5/10' in log                    # E1开局+5(愿力后续会被终结技消耗)
        assert aha.base_stats.LAUGH_BOOST == pytest.approx(0.15)  # E1增笑=LAUGH_BOOST全队
        assert 'E1·增笑' in log
