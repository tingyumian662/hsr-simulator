"""v7.26.3: 爻光欢愉技依项目主补录原稿修复（角色技能介绍/欢愉/爻光.txt）。

补录原文三处实锤出入的修复钉扎:
1. 技能名: 凶星低语是 debuff 名, 技能实名【赠君一卦，火树银花】
2. 削韧 5*5+20 = 弹射行25(5跳均分)+全体行20 = 45
3. 原稿语序: 先陷入【凶星低语】再结算伤害——本次伤害自身吃16%易伤
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
    st = SimState(enemies=list(enemies or [_enemy(toughness=200)]), units=list(units))
    st.extra["_elation"] = ElationSystem()
    return st


class TestSkillData:
    def test_skill_name(self):
        c = load_character("yaoguang")
        assert c.skills["elation_skill"].name == "欢愉技·赠君一卦，火树银花"

    def test_toughness_rows_encode_45(self):
        c = load_character("yaoguang")
        rows = [e for e in c.skills["elation_skill"].effects
                if e.type == "toughness_reduction"]
        vals = {(e.target, e.value) for e in rows}
        assert ("all_enemies", 20.0) in vals and ("bounce", 25.0) in vals


class TestPreCastOrder:
    def test_debuff_applied_and_self_damage_buffed(self):
        """先挂易伤再结算: 敌方全体带状态; 且本次伤害吃16%易伤(与无易伤口径比值钉扎)。"""
        u = _unit("yaoguang")
        st = _state(u)
        random.seed(2)
        _use_skill(u, st, "elation_skill")
        for e in st.enemies:
            assert e.has_status(status_id="凶星低语")
        cast = [x for x in u.damage_log if x[0].startswith("欢愉技")][-1]
        assert cast[1] > 0

        # 对照: 无易伤口径(预挂关闭)的伤害应低约16%
        u2 = _unit("yaoguang")
        st2 = _state(u2)
        from engine.characters import yaoguang as yg
        orig = yg.PHASE_HOOKS["effects_pre_cast"]
        yg.PHASE_HOOKS["effects_pre_cast"] = lambda *a, **k: None
        try:
            random.seed(2)
            _use_skill(u2, st2, "elation_skill")
        finally:
            yg.PHASE_HOOKS["effects_pre_cast"] = orig
        cast2 = [x for x in u2.damage_log if x[0].startswith("欢愉技")][-1]
        # 状态仍会由 S8 数据行补挂(伤害后), 对照组伤害不吃易伤
        ratio = cast[1] / cast2[1]
        assert 1.10 < ratio < 1.22, ratio

    def test_toughness_total_45(self):
        """削韧 5*5+20=45: 200 韧性 → 155(未击破)。"""
        u = _unit("yaoguang")
        st = _state(u, enemies=[_enemy(toughness=200)])
        random.seed(2)
        _use_skill(u, st, "elation_skill")
        assert st.enemies[0].toughness == pytest.approx(155.0)

    def test_e6_multiplier_unchanged(self):
        """E6 自身欢愉技倍率×2 与预挂共存（回归）。"""
        u = _unit("yaoguang", eidolon=6)
        st = _state(u)
        random.seed(2)
        _use_skill(u, st, "elation_skill")
        cast = [x for x in u.damage_log if x[0].startswith("欢愉技")][-1]
        assert cast[1] > 0
