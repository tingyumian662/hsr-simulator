"""v7.23.1: 测试者反馈双 bug 修复——忆灵侧光锥条件旁路 + 晴歌气氛死代码。

- Bug A: remembrance._ms_effective_stats 深拷贝携带光锥条件行静态值且无抵消
  （致长夜的星光【夜色】门控、织金岁月【织锦】54%CD 满层在忆灵侧无条件常驻）；
  修复=对忆灵副本执行与持有者同口径的 _apply_lc_condition_corrections。
- Bug B: 晴歌战技重施"回血+气氛+6"分支引擎路径不可达（summon_memsprite 在场
  提前返回, v6.12.0 起）→ Fever 比设计慢; 修复=ms_reheal 相位。
"""
import pytest

from engine.core.combat_engine import simulate
from engine.models.character import load_character
from engine.models.equipment import load_lightcone
from engine.systems import remembrance as rem
from helpers import _enemy


def _sim_lc(cid, lc_id, max_av):
    c = load_character(cid)
    lc = load_lightcone(lc_id)
    return simulate([{"char": c, "position": 1, "lightcone": lc}],
                    _enemy(), max_av=max_av)


def _ms_of(st, summoner_id):
    return next(m for m in st.memsprites if m.summoner_id == summoner_id)


class TestMsLightconeConditionGating:
    def test_starlight_yese_gates_memsprite_panel(self):
        """【夜色】=0 时忆灵面板不含 +30% 增伤/20% 忆灵穿防; =1 时恰好一份。
        （150AV 窗口: 召唤后忆灵在场——300AV 恰处迷梦消失间隙, 500AV 已再召唤）"""
        st = _sim_lc("changyeyue", "starlight_to_the_long_night", 150)
        u = st.units[0]
        ms = getattr(u, "memsprite_unit", None)
        assert ms is not None
        key = "starlight_to_the_long_night::yese"
        assert u.lc_stacks.get(key, 0) >= 1  # 模拟后忆灵技能已叠层
        s1 = rem._ms_effective_stats(ms, st)
        u.lc_stacks[key] = 0
        s0 = rem._ms_effective_stats(ms, st)
        assert s1.DMG_BONUS_ALL - s0.DMG_BONUS_ALL == pytest.approx(0.30)
        assert s1.DEF_PEN_MEMSPRITE - s0.DEF_PEN_MEMSPRITE == pytest.approx(0.20)
        assert s0.DEF_PEN_MEMSPRITE == pytest.approx(0.0)  # 套装外无其他来源

    def test_woven_gold_zhijin_per_stack_not_full_static(self):
        """织金岁月: 忆灵暴伤 = 9%/层（≤54%）, 不再是静态54%+9%/层双算。"""
        st = _sim_lc("aglaea", "time_woven_into_gold", 300)
        u = st.units[0]
        ms = _ms_of(st, "aglaea")
        key = "time_woven_into_gold::zhijin"
        cnt = u.lc_stacks.get(key, 0)
        assert cnt >= 1
        s_cnt = rem._ms_effective_stats(ms, st)
        u.lc_stacks[key] = 0
        s0 = rem._ms_effective_stats(ms, st)
        delta = s_cnt.CRIT_DMG - s0.CRIT_DMG
        assert delta == pytest.approx(0.09 * cnt)
        assert delta <= 0.54 + 1e-9  # 满层封顶, 无静态+动态双算


class TestQinggeResummonAtmo:
    def test_resummon_grants_atmo_via_engine_path(self):
        """战技重施（引擎路径）: 回血+气氛+6 → 单人 400AV 可达 Fever 并产出伤害。
        （Fever 为倒计时制, 窗口结束时可已散场——不断言终态忆灵在条）"""
        st = simulate([{"char": load_character("robin_summeretto"),
                        "position": 1}], _enemy(), max_av=400)
        log = "\n".join(st.log)
        assert "晴歌气氛+6" in log            # Bug B 修复: 重施分支可达
        assert "进入【Fever】" in log          # +6×2 → 全员登台
        assert st.units[0].total_damage_dealt > 0  # Fever 中忆灵技造成伤害

    def test_memsprite_acts_before_fever(self):
        """v7.23.2 项目主裁决: 「晴空乐手」召唤即在行动条上按自身速度行动——
        150AV 未进 Fever 亦产出伤害（此前 SPD=0 界外锁导致零伤害）。"""
        st = simulate([{"char": load_character("robin_summeretto"),
                        "position": 1}], _enemy(), max_av=150)
        log = "\n".join(st.log)
        assert "进入【Fever】" not in log
        ms = st.units[0].memsprite_unit
        assert ms is not None and ms.runtime_spd > 0  # 召唤即上条
        assert st.units[0].total_damage_dealt > 0     # 忆灵技（150%HP AoE）伤害
