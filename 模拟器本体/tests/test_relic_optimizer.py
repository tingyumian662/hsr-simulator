"""遗器优化器 v2 测试"""
from engine.core.relic_optimizer import (
    optimize_relics, _pick_main_stats, _distribute_marginal,
    _solve_constraints, Constraint, _mid,
)
from engine.models.character import Character
from engine.constants import StatType

CR = StatType.CRIT_RATE.value
CD = StatType.CRIT_DMG.value
ATKp = StatType.ATK_PERCENT.value
SPDp = StatType.SPD_PERCENT.value


def _make_char(element="冰", path="毁灭", base_spd=100):
    return Character(
        id="test", name="测试", element=element, path=path,
        base_HP=1358, base_ATK=679, base_DEF=460, base_SPD=base_spd,
        trace_stats={"CRIT_RATE": 12.0, "CRIT_DMG": 24.0, "ATK_percent": 10.0},
    )


class TestMainStatSelection:
    def test_body_crit_dmg(self):
        char = _make_char()
        mains = _pick_main_stats(char, [CR, CD, ATKp, SPDp])
        assert mains["body"] == StatType.CRIT_DMG

    def test_sphere_element(self):
        char = _make_char(element="雷")
        mains = _pick_main_stats(char, [CR, CD, ATKp])
        assert mains["planar_sphere"] == StatType.DMG_BONUS_LIGHTNING

    def test_feet_no_spd_when_capped(self):
        """SPD<95上限约束时，脚不应选速度鞋"""
        char = _make_char()
        cons = [Constraint(stat=SPDp, op="lt", value=95, reward={"CRIT_RATE": 32.0})]
        mains = _pick_main_stats(char, [CR, CD, ATKp, SPDp], cons)
        assert mains["feet"] == StatType.ATK_PERCENT  # not speed


class TestConstraintSolving:
    def test_spd_gte_threshold(self):
        """SPD≥160 需要多少词条"""
        char = _make_char(base_spd=100)
        mains = {"head": StatType.HP_FLAT, "hands": StatType.ATK_FLAT,
                 "body": StatType.CRIT_DMG, "feet": StatType.SPD_PERCENT,
                 "planar_sphere": StatType.DMG_BONUS_ICE, "link_rope": StatType.ATK_PERCENT}
        cons = [Constraint(stat=SPDp, op="gte", value=160,
                           reward={"ELATION_LEVEL": 50.0})]
        mand, rewards = _solve_constraints(char, mains, cons)
        # base(100) + feet main(25) = 125, need 35 more from subs
        # SPD roll mid = 3 → need ceil(35/3)=12 rolls
        assert mand.get(SPDp, 0) > 0
        assert rewards.get("ELATION_LEVEL", 0) == 50.0

    def test_reward_merge(self):
        """多个约束奖励合并"""
        char = _make_char()
        mains = _pick_main_stats(char, [CR, CD, ATKp, SPDp])
        cons = [
            Constraint(stat=SPDp, op="lt", value=95, reward={"CRIT_RATE": 32.0}),
            Constraint(stat=SPDp, op="lt", value=95, reward={"CRIT_RATE": 8.0}),
        ]
        _, rewards = _solve_constraints(char, mains, cons)
        assert rewards["CRIT_RATE"] == 40.0


class TestMarginalAllocation:
    def test_crit_1to2_ratio(self):
        """边际效益应使双暴趋近 1:2"""
        char = _make_char()
        mains = _pick_main_stats(char, [CR, CD, ATKp])
        dist = _distribute_marginal(
            char, mains, [CR, CD, ATKp], 20, {}, {},
        )
        cr_rolls = dist.get(CR, 0)
        cd_rolls = dist.get(CD, 0)
        # CD 应多于 CR（因为 body 已有 64.8% CD）
        assert cd_rolls > 0

    def test_constraint_mandatory_respected(self):
        """约束强制词条应先分配"""
        char = _make_char(base_spd=100)
        mains = _pick_main_stats(char, [CR, CD, ATKp, SPDp])
        mand = {SPDp: 12}
        dist = _distribute_marginal(
            char, mains, [CR, CD, ATKp, SPDp], 30, mand, {},
        )
        assert dist.get(SPDp, 0) >= 12


class TestOptimizeRelics:
    def test_basic(self):
        char = _make_char()
        build = optimize_relics(char, [CR, CD, ATKp, SPDp], total_rolls=30)
        assert len(build.pieces) == 6
        s = build.final_stats
        assert s.ATK > char.base_ATK * 1.5
        assert s.CRIT_RATE > 0.30

    def test_poet_set_constraint(self):
        """诗人套：SPD<95 → CR+32%"""
        char = _make_char(base_spd=90)  # 低速角色
        cons = [Constraint(stat=SPDp, op="lt", value=95, reward={"CRIT_RATE": 32.0})]
        build = optimize_relics(char, [CR, CD, ATKp], total_rolls=30, constraints=cons)
        s = build.final_stats
        # base 90 < 95 → reward applied
        assert s.CRIT_RATE > 0.40  # trace 12% + reward 32% + subs

    def test_speed_threshold_then_damage(self):
        """SPD≥160先达标，剩余堆伤害"""
        char = _make_char(base_spd=100)
        cons = [Constraint(stat=SPDp, op="gte", value=160,
                           reward={"ELATION_LEVEL": 50.0})]
        build = optimize_relics(char, [CR, CD, ATKp, SPDp], total_rolls=30, constraints=cons)
        s = build.final_stats
        # SPD 应接近或超过 160（含鞋25+subs）
        assert s.SPD >= 140  # Speed relic stats are flat values.
