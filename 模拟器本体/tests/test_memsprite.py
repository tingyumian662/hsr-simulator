"""记忆命途 — 忆灵数据模型测试"""
from engine.models.memsprite import MemSprite
from engine.models.character import Character, Skill


class TestMemSprite:
    """忆灵数据模型"""

    def test_basic_creation(self):
        ms = MemSprite(name="测试忆灵", base_HP=2000, base_ATK=500, element="冰")
        assert ms.name == "测试忆灵"
        assert ms.base_HP == 2000
        assert not ms.is_summoned
        assert not ms.is_alive()

    def test_position_default(self):
        """默认站位：召唤者左侧（偏移 -1）"""
        ms = MemSprite(name="忆灵")
        # 2号位角色召唤，忆灵在 2+0.5*(-1)=1.5 位
        pos = ms.calc_combat_position(2)
        assert pos == 1.5

    def test_position_right(self):
        """偏移 +1：召唤者右侧"""
        ms = MemSprite(name="忆灵", position_offset=1)
        pos = ms.calc_combat_position(2)
        assert pos == 2.5

    def test_position_slot_1_summoner(self):
        """1号位（最右侧）召唤默认左侧忆灵 → 0.5 位"""
        ms = MemSprite(name="忆灵")
        assert ms.calc_combat_position(1) == 0.5

    def test_inherit_stats(self):
        ms = MemSprite(name="忆灵", base_HP=1000, inherit_ratios={"HP": 0.5, "ATK": 0.8})
        assert ms.calc_inherited_stat(2000, "HP") == 1000.0    # 2000 * 0.5
        assert ms.calc_inherited_stat(1000, "ATK") == 800.0    # 1000 * 0.8
        assert ms.calc_inherited_stat(500, "DEF") == 500.0     # 未定义，默认 1.0

    def test_take_damage_and_death(self):
        ms = MemSprite(name="忆灵", base_HP=1000)
        ms.is_summoned = True
        ms.current_HP = 1000
        assert ms.is_alive()

        ms.take_damage(600)
        assert ms.current_HP == 400
        assert ms.is_alive()

        ms.take_damage(500)
        assert ms.current_HP == 0
        assert not ms.is_alive()


class TestCharacterWithMemSprite:
    """角色加载含忆灵数据"""

    def test_load_remembrance_character(self):
        char_data = {
            "id": "test_remembrance",
            "name": "记忆角色",
            "element": "冰",
            "path": "记忆",
            "base_HP": 1200, "base_ATK": 600, "base_DEF": 400, "base_SPD": 100,
            "trace_stats": {},
            "skills": {},
            "traces": [],
            "eidolons": [],
            "memsprite": {
                "name": "霜灵",
                "element": "冰",
                "base_HP": 2000,
                "base_ATK": 500,
                "base_DEF": 300,
                "base_SPD": 90,
                "inherit_ratios": {"HP": 0.5, "ATK": 0.8},
                "position_offset": -1,
                "skills": {
                    "memsprite_basic": {
                        "name": "霜灵普攻",
                        "type": "basic_attack",
                        "cost": {},
                        "target": "single_enemy",
                        "multipliers": [
                            {"stat": "ATK", "scale": 100.0, "damageType": "direct"}
                        ],
                        "effects": []
                    }
                }
            }
        }

        char = Character.from_dict(char_data)
        assert char.name == "记忆角色"
        assert char.path == "记忆"
        assert char.memsprite is not None
        assert char.memsprite.name == "霜灵"
        assert char.memsprite.base_HP == 2000
        assert char.memsprite.inherit_ratios["HP"] == 0.5
        assert len(char.memsprite.skills) == 1
        assert char.memsprite.skills["memsprite_basic"].name == "霜灵普攻"

    def test_load_non_remembrance_character(self):
        """非记忆角色无忆灵"""
        char_data = {
            "id": "test_normal",
            "name": "普通角色",
            "element": "物理",
            "path": "毁灭",
            "base_HP": 1000, "base_ATK": 500, "base_DEF": 300, "base_SPD": 100,
            "trace_stats": {},
            "skills": {},
            "traces": [],
            "eidolons": [],
        }

        char = Character.from_dict(char_data)
        assert char.name == "普通角色"
        assert char.memsprite is None
