"""v7.26.5: 第二波逐角色深审修复——五早期欢愉角色 vs 原稿。

裁决11-14（2026-09-30）+ 九处实锤数据/实现修复的钉扎:
- 裁决11: 攻击获得笑点严格按原文——开拓者天赋平3(原6/9), 火花/绯英无源基础3废除
- 裁决12: 银狼奖励关盲盒交错(每33段一次, 共3次)
- 裁决13: 银狼全灭暂停+新敌再施放+每回合首触增益延长
- 裁决14: 火花陷阱连发(每发1SP/爆点, 倍率提高语义)
- 实锤: 银狼行迹3封顶250%(原100%)/奖励关削韧0.2*100+30/末击均分;
  火花强化普攻满级100/50(原1级档50/25)/天赋弹射20%(原10%)/欢愉技削韧bounce33.2;
  绯英秘技削韧20/240累计单次clamp/480下限限终结技
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState, _use_skill  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402


def _state(*units, enemies=None):
    st = SimState(enemies=list(enemies or [_enemy(toughness=500)]),
                  units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


class TestRuling11LaughOnAttack:
    def test_trailblazer_flat_3(self):
        """开拓者天赋: 攻击后固定+3笑点+10能量(不是6/9)。"""
        from engine.characters import activate
        u = _unit("trailblazer_elation")
        st = _state(u)
        activate(st, ["trailblazer_elation"], elation_active=True)
        st.laugh_points = 0.0
        e0 = u.current_energy
        random.seed(1)
        _use_skill(u, st, "basic_attack")
        assert st.laugh_points == pytest.approx(3.0)
        assert u.current_energy == pytest.approx(e0 + 10.0 + 20.0)  # 天赋10+普攻20

    def test_sparxie_no_free_laugh(self):
        """火花: 原文无攻击获得笑点行——普攻/战技不再+3。"""
        from engine.characters import activate
        u = _unit("sparxie")
        st = _state(u)
        activate(st, ["sparxie"], elation_active=True)
        st.laugh_points = 0.0
        random.seed(1)
        _use_skill(u, st, "basic_attack")
        assert st.laugh_points == pytest.approx(0.0)

    def test_evanescia_no_free_laugh(self):
        from engine.characters import activate
        u = _unit("evanescia")
        st = _state(u)
        activate(st, ["evanescia"], elation_active=True)
        st.laugh_points = 0.0
        random.seed(1)
        _use_skill(u, st, "basic_attack")
        assert st.laugh_points == pytest.approx(0.0)

    def test_yaoguang_keeps_sourced_3(self):
        """爻光: 天赋原文'普攻/战技后+3笑点'保留。"""
        from engine.characters import activate
        u = _unit("yaoguang")
        st = _state(u)
        activate(st, ["yaoguang"], elation_active=True)
        st.laugh_points = 0.0
        random.seed(1)
        _use_skill(u, st, "skill")
        assert st.laugh_points == pytest.approx(3.0)


class TestYinlangFixes:
    def test_trace3_cap_250(self):
        """行迹3: 160→+50%, 210→+150%, 260+→+250% 封顶(原误100%)。"""
        from engine.core.attributes import CombatStats
        from engine.characters.yinlang import _yl_eff_stats
        u = _unit("yinlang")
        st = _state(u)

        def _el(spd):
            s = CombatStats()
            s.ELATION_LEVEL = 0.0
            return _yl_eff_stats(u, st, s, effective_spd=spd)

        assert _el(160.0).ELATION_LEVEL == pytest.approx(0.50)
        assert _el(210.0).ELATION_LEVEL == pytest.approx(0.50 + 50 * 0.02)
        assert _el(300.0).ELATION_LEVEL == pytest.approx(0.50 + 100 * 0.02)

    def test_enhanced_basic_toughness(self):
        """奖励关削韧: 弹射每段0.2(0.2*100)+末击30 → 500韧性→370。"""
        from engine.characters.yinlang import silver_enhanced_basic
        u = _unit("yinlang")
        u.hidden_score = 60.0
        u.invincible_active = True
        e = _enemy(hp=10_000_000.0, toughness=500)
        st = _state(u, enemies=[e])
        random.seed(3)
        silver_enhanced_basic(u, st)
        assert e.toughness == pytest.approx(500 - (100 * 0.2 + 30.0))

    def test_blindbox_interleaved_and_suspend(self):
        """弱敌中途全灭 → 暂停(记录剩余段/盲盒, 不计完成次数)。"""
        from engine.characters.yinlang import silver_enhanced_basic
        u = _unit("yinlang")
        u.hidden_score = 200.0
        u.invincible_active = True
        e = _enemy(hp=1.0, toughness=0)
        st = _state(u, enemies=[e])
        random.seed(3)
        silver_enhanced_basic(u, st)
        seg, bb = u.extra["yinlang_reward_state"]
        assert 0 < seg < 100
        assert 1 <= bb <= 3
        assert u.invincible_basics_done == 0  # 暂停不计完成

    def test_reward_resume_queues_extra_turn(self):
        from engine.characters.yinlang import _yl_reward_resume
        u = _unit("yinlang")
        u.extra["yinlang_reward_state"] = (40, 2)
        st = _state(u)
        _yl_reward_resume(st)
        assert any(x is u for x, k in st.extra["extra_turns"])
        # 再呼不重复入队
        _yl_reward_resume(st)
        assert sum(1 for x, k in st.extra["extra_turns"] if x is u) == 1


class TestSparxieFixes:
    def test_enhanced_basic_max_level(self):
        c = load_character("sparxie")
        m = c.skills["basic_attack_enhanced"].multipliers
        assert (m[0].scale, m[1].scale) == (100.0, 50.0)

    def test_elation_toughness_bounce_row(self):
        c = load_character("sparxie")
        rows = {(e.target, e.value) for e in c.skills["elation_skill"].effects
                if e.type == "toughness_reduction"}
        assert ("all_enemies", 6.66) in rows and ("bounce", 33.2) in rows

    def test_trap_chain_sp_accounting(self, monkeypatch):
        """裁决14: 连发受SP限制——礼物SP回补会资助下一发(真实语义);
        用固定掷(无礼物SP)验证纯SP耗尽边界。"""
        from engine.characters import sparxie as spx_mod
        from engine.characters.sparxie import _sparxie_trap_chain_adjust
        skill = load_character("sparxie").skills["basic_attack_enhanced"]
        # 固定掷→恍恍惚惚(+1笑点, 不回SP): 纯SP边界
        monkeypatch.setattr(spx_mod.random, "random", lambda: 0.9)
        u = _unit("sparxie")
        st = _state(u)
        st.max_sp = 20
        u.extra["sparxie_trap_uses"] = 2
        st.skill_points = 1
        new = _sparxie_trap_chain_adjust(u, st, skill=skill,
                                         skill_key="basic_attack_enhanced")
        assert u.extra["sparxie_trap_uses"] == 1  # SP不足停
        assert new.multipliers[0].scale == 120.0

        monkeypatch.undo()
        u2 = _unit("sparxie")
        st2 = _state(u2)
        st2.max_sp = 20
        u2.extra["sparxie_trap_uses"] = 3
        st2.skill_points = 5
        random.seed(1)
        new2 = _sparxie_trap_chain_adjust(u2, st2, skill=skill,
                                          skill_key="basic_attack_enhanced")
        assert u2.extra["sparxie_trap_uses"] == 0
        assert new2.multipliers[0].scale == 100.0 + 60.0  # 3发×20%
        assert new2.multipliers[1].scale == 50.0 + 30.0   # 3发×10%

    def test_talent_bounce_uses_fired_count(self):
        """持好活时每发陷阱1次20%弹射(满级档)——经 fired 计数传递。"""
        from engine.characters.sparxie import (_sparxie_enhanced_settle,
                                               _sparxie_trap_chain_adjust)
        u = _unit("sparxie")
        st = _state(u)
        st.max_sp = 20
        st.skill_points = 5
        u.extra["sparxie_trap_uses"] = 2
        skill = load_character("sparxie").skills["basic_attack_enhanced"]
        random.seed(1)
        _sparxie_trap_chain_adjust(u, st, skill=skill,
                                   skill_key="basic_attack_enhanced")
        assert u.extra["sparxie_traps_fired"] == 2
        dmg0 = u.total_damage_dealt
        st.elation_state.grant_good_show("sparxie", 10.0, duration=2)
        random.seed(1)
        _sparxie_enhanced_settle(st, u)
        assert u.total_damage_dealt > dmg0
        assert "sparxie_traps_fired" not in u.extra  # 消费后清除


class TestEvanesciaFixes:
    def test_technique_toughness(self):
        from engine.characters.evanescia import _tech_evanescia
        u = _unit("evanescia")
        e = _enemy(toughness=200)
        st = _state(u, enemies=[e])
        random.seed(1)
        _tech_evanescia(st, u, is_opener=True)
        assert e.toughness == pytest.approx(180.0)

    def test_energy_bank_clamp_240(self):
        """单次大额回能最多计入240累计(原全额入账)。"""
        from engine.characters.evanescia import _trace_evanescia_energy_convert
        u = _unit("evanescia")
        st = _state(u)
        random.seed(1)
        _trace_evanescia_energy_convert(u, st, amount=300.0)
        # 300→clamp 240 → FUA 触发一次, 余0(原余60? 240-240=0; 原 300-240=60)
        assert u.extra.get("evanescia_energy_bank", 0.0) == 0.0

    def test_480_floor_ult_only(self):
        """480 好活下限仅终结技段吃——战技16%追伤 laugh_n 用实持。"""
        import engine.characters.evanescia as eva_mod
        u = _unit("evanescia")
        st = _state(u)
        st.elation_state.grant_good_show("evanescia", 5.0, duration=2)
        captured = {}
        orig = eva_mod.calculate_damage

        def spy(stats, enemy, scaling, scale, *a, **kw):
            captured.setdefault("skill", []).append(kw.get("laugh_n"))
            return orig(stats, enemy, scaling, scale, *a, **kw)

        eva_mod.calculate_damage = spy
        try:
            random.seed(1)
            eva_mod._evanescia_goodshow_extra(st, u, "skill")
        finally:
            eva_mod.calculate_damage = orig
        assert captured["skill"] and all(
            n == pytest.approx(5.0) for n in captured["skill"])  # 不吃480下限
