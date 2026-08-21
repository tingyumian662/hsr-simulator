"""测试属性汇总管线"""
from engine.core.attributes import compute_combat_stats, CombatStats
from engine.models.character import Character


def _make_test_char() -> Character:
    return Character(
        id="test_char",
        name="测试角色",
        element="冰",
        path="毁灭",
        base_HP=1358, base_ATK=679, base_DEF=460, base_SPD=100,
        trace_stats={"CRIT_RATE": 12.0, "ATK_percent": 10.0},
    )


def test_base_stats_only():
    """仅基础属性时的汇总"""
    char = _make_test_char()
    stats = compute_combat_stats(char)

    assert stats.HP == 1358.0, f"Expected HP=1358, got {stats.HP}"
    assert stats.ATK == 679.0 * (1.0 + 0.10), f"Expected ATK={679*1.10}, got {stats.ATK}"
    assert stats.DEF == 460.0, f"Expected DEF=460, got {stats.DEF}"
    assert stats.SPD == 100.0, f"Expected SPD=100, got {stats.SPD}"
    assert stats.CRIT_RATE == 0.05 + 0.12, f"Expected CR={0.17}, got {stats.CRIT_RATE}"
    assert stats.CRIT_DMG == 0.50, f"Expected CD=0.50, got {stats.CRIT_DMG}"


def test_with_lightcone():
    """带光锥基础属性"""
    from engine.models.equipment import LightCone
    char = _make_test_char()
    lc = LightCone(id="test_lc", name="测试光锥", rank=1, path="毁灭",
                   base_HP=1058, base_ATK=476, base_DEF=264)
    stats = compute_combat_stats(char, lightcone=lc)

    base_atk = 679 + 476
    assert abs(stats.ATK - base_atk * (1.0 + 0.10)) < 0.1
    base_hp = 1358 + 1058
    assert abs(stats.HP - base_hp) < 0.1  # no HP% bonus


def test_with_relics():
    """带遗器属性"""
    from engine.models.equipment import RelicPiece
    char = _make_test_char()
    relics = [
        RelicPiece(slot="head", set_name="测试套", main_stat_type="HP_flat", main_stat_value=705),
        RelicPiece(slot="hands", set_name="测试套", main_stat_type="ATK_flat", main_stat_value=352),
        RelicPiece(slot="body", set_name="测试套", main_stat_type="CRIT_DMG", main_stat_value=64.8),
    ]
    stats = compute_combat_stats(char, relics=relics)

    assert stats.HP == 1358 + 705  # base + flat
    assert stats.ATK == 679 * 1.10 + 352  # base*(1+pct) + flat
    assert stats.CRIT_DMG == 0.50 + 0.648  # base + 64.8%


def test_def_multiplier_from_enemy():
    """防御乘区验证: 1000/(1000+1000)=0.5"""
    from engine.models.enemy import Enemy
    e = Enemy(id="test", name="test", level=80, DEF=1000)
    dm = e.get_def_multiplier(80)
    expected = 1000.0 / 2000.0
    assert abs(dm - expected) < 0.0001, f"Expected {expected}, got {dm}"


def test_lightcone_path_mismatch():
    """光锥命途不匹配：只有白值，特效不生效"""
    from engine.models.equipment import LightCone, LightConeEffect
    char = _make_test_char()  # path = "毁灭"
    lc = LightCone(
        id="test_lc_hunt", name="巡猎光锥", rank=1, path="巡猎",
        base_HP=950, base_ATK=500, base_DEF=300,
        effects=[LightConeEffect(
            type="permanent_buff",
            attributes={"ATK_percent": 30.0, "CRIT_RATE": 16.0}
        )],
    )
    stats = compute_combat_stats(char, lightcone=lc)

    # 白值照常生效
    assert stats.HP == 1358 + 950
    base_atk = 679 + 500
    # 只有行迹10%ATK，光锥的30%ATK不生效
    assert abs(stats.ATK - base_atk * 1.10) < 0.1
    # 光锥的16%暴击也不生效
    assert stats.CRIT_RATE == 0.05 + 0.12  # only base + trace


def test_lightcone_path_match():
    """光锥命途匹配：白值+特效全部生效"""
    from engine.models.equipment import LightCone, LightConeEffect
    char = _make_test_char()  # path = "毁灭"
    lc = LightCone(
        id="test_lc_destr", name="毁灭光锥", rank=1, path="毁灭",
        base_HP=950, base_ATK=500, base_DEF=300,
        effects=[LightConeEffect(
            type="permanent_buff",
            attributes={"ATK_percent": 30.0, "CRIT_RATE": 16.0}
        )],
    )
    stats = compute_combat_stats(char, lightcone=lc)

    base_atk = 679 + 500
    # 行迹10% + 光锥30% = 40% ATK
    assert abs(stats.ATK - base_atk * 1.40) < 0.1
    # 光锥16%暴击生效
    assert stats.CRIT_RATE == 0.05 + 0.12 + 0.16
    assert stats.HP == 1358 + 950


def test_effective_dmg_bonus():
    """有效增伤乘区"""
    stats = CombatStats()
    stats.DMG_BONUS_ALL = 0.20
    stats.DMG_BONUS["冰"] = 0.388
    bonus = stats.get_effective_dmg_bonus("冰")
    assert abs(bonus - 1.588) < 0.001, f"Expected 1.588, got {bonus}"
    bonus_other = stats.get_effective_dmg_bonus("火")
    assert abs(bonus_other - 1.20) < 0.001
