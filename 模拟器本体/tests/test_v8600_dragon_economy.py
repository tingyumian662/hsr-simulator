"""v8.6.0: 记忆战舰两杠杆钉扎（项目主裁定 2026-10-01）。

裁定①: 游戏内"无论生命值回复还是损失都能为终结技充能"——治疗全额转新蕊/龙血,
        行迹2文本的 12%/人/动上限废止。
裁定③: 队友增益覆盖——死龙伤害面板须叠加召唤者战斗中动态 TimedBuff。
裁定(确认): 死龙忆灵技=回合自选爪击/焰息, 焰息连喷, 1点锁血自爆——现行逻辑正确。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState, TimedBuff  # noqa: E402
from engine.characters.xiadie import (  # noqa: E402
    _calc_flame_damage, _xiadie_heal_to_xinrui, xiadie_xinrui_cap,
)


def _state(*units, enemies=None):
    from engine.systems.elation import ElationSystem
    st = SimState(enemies=list(enemies or [_enemy()]), units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


class TestHealFullConversion:
    """裁定演进: v8.6.0 治疗计入充能 → v8.7.0 项目主裁定——忆灵也算受疗目标、
    对忆灵的治疗同样转化、转化按 12%/人/动 逐目标独立计帽（名义量含溢出照计）。"""

    def test_per_person_cap(self):
        """单目标超额治疗封顶 12%×新蕊上限(=4080), 溢出部分不重复计。"""
        x = _unit('xiadie')
        mate = _unit('fengjin')
        st = _state(x, mate)
        x.xinrui = 0.0
        _xiadie_heal_to_xinrui(st, [mate], 8000.0)
        assert x.xinrui == pytest.approx(34000 * 0.12)  # 4080 封顶

    def test_multi_target_sums_each_capped(self):
        """多目标=逐人独立 4080 帽, 总量求和（忆灵也是"人"）。"""
        from types import SimpleNamespace
        x = _unit('xiadie')
        a, b = _unit('fengjin'), _unit('changyeyue')
        ms = SimpleNamespace(is_alive=True, max_hp=3347.0, current_hp=1.0)
        st = _state(x, a, b)
        x.xinrui = 0.0
        _xiadie_heal_to_xinrui(st, [a, b, ms], 99999.0)
        assert x.xinrui == pytest.approx(3 * 34000 * 0.12)  # 3×4080
        # 同一动内再治疗: 预算已耗尽
        _xiadie_heal_to_xinrui(st, [a], 1000.0)
        assert x.xinrui == pytest.approx(3 * 34000 * 0.12)
        # 任意单位行动后重置预算
        st.extra['xiadie_heal_conv'] = {}
        _xiadie_heal_to_xinrui(st, [a], 1000.0)
        assert x.xinrui == pytest.approx(3 * 34000 * 0.12 + 1000.0)

    def test_small_heal_full_within_cap(self):
        """帽内小额治疗全额转化（含满血溢出照计）。"""
        x = _unit('xiadie')
        mate = _unit('fengjin')
        st = _state(x, mate)
        x.xinrui = 0.0
        assert mate.current_hp == mate.max_hp  # 满血→全程溢出
        _xiadie_heal_to_xinrui(st, [mate], 3000.0)
        assert x.xinrui == pytest.approx(3000.0)

    def test_heal_feeds_dragon_when_present(self):
        x = _unit('xiadie')
        st = _state(x)
        # 伪死龙: 挂一个存活的 memsprite_unit
        class _FakeMS:
            is_alive = True
            max_hp = 34000.0
            current_hp = 10000.0
        x.memsprite_unit = _FakeMS()
        _xiadie_heal_to_xinrui(st, [x], 5000.0)  # v8.7.0: 12%/人帽 → 4080
        assert x.memsprite_unit.current_hp == pytest.approx(10000.0 + 4080.0)
        assert x.xinrui == pytest.approx(0.0)  # 龙在场走龙血, 不攒新蕊

    def test_overheal_full_target_still_counts(self):
        """v8.6.1 项目主问询钉扎: 满血目标的溢出治疗量同样计入新蕊（帽内全额）。"""
        x = _unit('xiadie')
        mate = _unit('fengjin')
        st = _state(x, mate)
        x.xinrui = 0.0
        assert mate.current_hp == mate.max_hp  # 满血
        _xiadie_heal_to_xinrui(st, [mate], 3000.0)
        assert x.xinrui == pytest.approx(3000.0)  # 溢出部分照计

    def test_fengjin_e1_and_tech_heals_feed_xinrui(self):
        """v8.6.1: 风堇 E1/秘技两处直写回血——名义量(含溢出)直调遐蝶充能。"""
        from engine.characters.fengjin import _eid_fengjin_e1, _tech_fengjin
        x = _unit('xiadie')
        f = _unit('fengjin', eidolon=1)
        st = _state(x, f)
        x.xinrui = 0.0
        f.current_hp = f.max_hp  # 满血自疗 → 全程溢出
        _eid_fengjin_e1(f, st)
        assert x.xinrui == pytest.approx(f.max_hp * 0.08)  # 溢出照计
        x.xinrui = 0.0
        for eu in st.units:  # 全队满血 → 秘技治疗全程溢出
            eu.current_hp = eu.max_hp
        _tech_fengjin(st, f, is_opener=True)
        assert x.xinrui == pytest.approx((f.base_stats.HP * 0.30 + 600) * 2)  # 2人×全额


class TestDragonBuffOverlay:
    """裁定③: 召唤者动态 buff 叠加进龙伤面板（暴击/暴伤/增伤链）。"""

    def _ms(self, x):
        import copy
        from types import SimpleNamespace
        ms = SimpleNamespace(summoner_id='xiadie', is_alive=True,
                             max_hp=34000.0, current_hp=34000.0,
                             base_stats=copy.deepcopy(x.base_stats),
                             buffs=[])
        ms.base_stats.CRIT_RATE = 0.05
        ms.base_stats.CRIT_DMG = 0.50
        return ms

    def _flame(self, x, ms):
        st = _state(x, enemies=[_enemy()])
        return _calc_flame_damage(st, x, ms, 24.0)[0]

    def test_summoner_crit_buff_raises_flame(self):
        x = _unit('xiadie')
        ms = self._ms(x)
        base = self._flame(x, ms)
        # CR 5%→50% 跨过引擎期望暴击门(≥0.5), 暴击链启动
        x.buffs.append(TimedBuff(source_id='t', attributes={'CRIT_RATE': 45.0},
                                 remaining_turns=2, param_id='tb'))
        boosted = self._flame(x, ms)
        assert boosted > base * 1.15  # 动态暴击显著抬升龙伤

    def test_summoner_dmg_bonus_buff_applies(self):
        x = _unit('xiadie')
        ms = self._ms(x)
        base = self._flame(x, ms)
        x.buffs.append(TimedBuff(source_id='t', attributes={'DMG_BONUS_ALL': 50.0},
                                 remaining_turns=2, param_id='tb2'))
        boosted = self._flame(x, ms)
        assert boosted > base * 1.4  # 全增伤进龙伤

    def test_realm_true_dmg_covers_flame(self):
        """v8.6.2: 昔涟结界真伤(盛放吧，来世的乐土 24%)须覆盖焰息/晦翼。"""
        x = _unit('xiadie')
        ms = self._ms(x)
        base = self._flame(x, ms)
        st = _state(x, enemies=[_enemy()])
        st.realm_true_dmg = 0.24
        boosted = _calc_flame_damage(st, x, ms, 24.0)[0]
        assert boosted > base * 1.2  # +24% 真伤 rider

    def test_nuxiao_roar_on_summon(self):
        """v8.6.2: 震彻寂壤的怒啸——召唤死龙→全队伤害+10% 3回合(同源刷新)。"""
        from engine.characters.xiadie import _apply_nuxiao_roar
        x = _unit('xiadie')
        mate = _unit('fengjin')
        st = _state(x, mate)
        _apply_nuxiao_roar(st)
        for u in (x, mate):
            bs = [b for b in u.buffs if b.param_id == 'xiadie_nuxiao']
            assert len(bs) == 1 and bs[0].remaining_turns == 3
            assert bs[0].attributes['DMG_BONUS_ALL'] == pytest.approx(10.0)
        _apply_nuxiao_roar(st)  # 重召唤=刷新不叠层
        assert len([b for b in x.buffs if b.param_id == 'xiadie_nuxiao']) == 1
