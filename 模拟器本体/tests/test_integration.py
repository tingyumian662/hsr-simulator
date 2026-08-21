"""端到端集成测试：从 JSON 加载 → 属性汇总 → 伤害计算"""
from engine.models.character import Character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.damage import calculate_damage


def test_full_pipeline():
    """完整管线：加载角色+敌人 → 汇总属性 → 计算伤害"""
    char_data = {
        "id": "test_integration",
        "name": "集成测试角色",
        "element": "雷",
        "path": "智识",
        "base_HP": 1000, "base_ATK": 700, "base_DEF": 400, "base_SPD": 110,
        "trace_stats": {"CRIT_RATE": 12.0, "CRIT_DMG": 30.0, "ATK_percent": 10.0},
        "skills": {
            "ultimate": {
                "name": "终结技", "type": "ultimate",
                "cost": {"energy": 130}, "target": "all_enemies",
                "multipliers": [{"stat": "ATK", "scale": 300.0, "damageType": "direct"}],
                "effects": []
            }
        },
        "traces": [], "eidolons": []
    }

    enemy_data = {
        "id": "test_enemy",
        "name": "测试敌人",
        "level": 80, "HP": 50000, "ATK": 400, "DEF": 800, "SPD": 120,
        "toughness": 100, "dmg_reduction": 0.1, "vulnerability": 0.2,
        "element_res": {"物理": 0.2, "雷": 0.0, "火": 0.2, "冰": 0.2, "风": 0.2, "量子": 0.2, "虚数": 0.2}
    }

    character = Character.from_dict(char_data)
    enemy = Enemy.from_dict(enemy_data)

    assert character.name == "集成测试角色"
    assert enemy.name == "测试敌人"

    stats = compute_combat_stats(character)
    base_atk = 700.0
    expected_atk = base_atk * (1.0 + 0.10)
    assert abs(stats.ATK - expected_atk) < 0.1
    assert stats.CRIT_RATE == 0.05 + 0.12
    assert stats.CRIT_DMG == 0.50 + 0.30

    skill = character.get_skill("ultimate")
    mult = skill.multipliers[0]
    scaling = stats.ATK

    result = calculate_damage(
        stats=stats, enemy=enemy,
        scaling_stat_value=scaling, multiplier=mult.scale,
        damage_type="direct", element="雷", attacker_level=80,
        is_crit=True,
    )

    # 手动验算
    expected_base = scaling * 3.0
    assert abs(result.base_damage - expected_base) < 0.1

    # 防御区: 1000/(1000+800) = 1000/1800 = 0.5556
    expected_def = 1000.0 / 1800.0
    assert abs(result.def_mult - expected_def) < 0.01

    # 韧性减伤区: 未击破 → 0.9
    assert abs(result.toughness_mult - 0.9) < 0.01

    # 减伤区: 1 - 0.1 = 0.9
    assert abs(result.dmg_reduction_mult - 0.9) < 0.01

    # 易伤区: 1 + 0.2 = 1.2
    assert abs(result.vulnerability_mult - 1.2) < 0.01

    # 最终伤害 > 0
    assert result.final_damage > 0
    print(f"集成测试通过！最终伤害: {result.final_damage:.2f}")


def test_to_dict_roundtrip():
    """DamageBreakdown.to_dict() 输出完整性"""
    from engine.core.damage import DamageBreakdown
    bd = DamageBreakdown(
        base_damage=1000.0,
        dmg_bonus_mult=1.5,
        def_mult=0.5,
        res_mult=0.9,
        crit_mult=2.0,
        dmg_reduction_mult=0.9,
        vulnerability_mult=1.2,
        true_dmg_mult=1.0,
        final_damage=810.0,
    )
    d = bd.to_dict()
    assert "base_damage" in d
    assert "dmg_bonus_mult" in d
    assert "def_mult" in d
    assert "res_mult" in d
    assert "crit_mult" in d
    assert "dmg_reduction_mult" in d
    assert "vulnerability_mult" in d
    assert "true_dmg_mult" in d
    assert "final_damage" in d
    assert "total_multiplier" in d
    # total_multiplier should be product of all multipliers
    expected_total = 1.5 * 0.5 * 0.9 * 2.0 * 0.9 * 1.2 * 1.0
    assert abs(d["total_multiplier"] - expected_total) < 0.001


def test_empty_character_minimal():
    """最简角色只加载不崩溃"""
    char = Character(id="minimal", name="最简", element="物理", path="毁灭")
    stats = compute_combat_stats(char)
    assert stats.HP == 0.0
    assert stats.ATK == 0.0
    assert stats.CRIT_RATE == 0.05  # default


def test_target_dummy_loaded_from_disk():
    """能从磁盘实际加载默认木桩"""
    import pathlib
    data_dir = pathlib.Path(__file__).parent.parent / "data" / "enemies"
    enemy = Enemy.from_json(str(data_dir / "target_dummy.json"))
    assert enemy.id == "target_dummy"
    assert enemy.DEF == 1000.0
    assert enemy.dmg_reduction == 0.0
    # 新防御公式: 1000/(1000+1000)=0.5
    assert abs(enemy.get_def_multiplier(80) - 0.5) < 0.01
