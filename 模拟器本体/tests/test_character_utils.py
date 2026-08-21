"""黄金裔检测工具函数测试"""
import pytest
from engine.core.character_utils import (
    is_gold_offspring,
    count_gold_offspring,
    count_remembrance,
    count_gold_or_memory,
    GOLD_OFFSPRING_IDS,
)


def _char(cid, path="记忆", gold=None):
    """构造带 gold_offspring 字段的角色对象"""
    attrs = {"id": cid, "path": path}
    if gold is not None:
        attrs["gold_offspring"] = gold
    return type('C', (), attrs)()


def _unit(cid, path="记忆", gold=None):
    """构造 SimUnit 兼容对象"""
    return type('U', (), {'char': _char(cid, path, gold)})()


class TestGoldOffspring:
    def test_data_field_priority(self):
        """JSON 数据字段优先（长夜月 JSON 标了 true）"""
        assert is_gold_offspring(_unit("changyeyue", gold=True)) is True
        assert is_gold_offspring(_unit("seele", gold=False)) is False

    def test_known_gold_offspring_fallback(self):
        """空壳角色（无字段）用 ID 集合兜底"""
        assert is_gold_offspring(_unit("xiadie")) is True
        assert is_gold_offspring(_unit("changyeyue")) is True  # 已补进集合
        assert is_gold_offspring(_unit("aglaea")) is True
        assert is_gold_offspring(_unit("seele")) is False
        assert is_gold_offspring(_unit("bronya")) is False

    def test_gold_offspring_set_contains_changyeyue(self):
        """集合应包含长夜月"""
        assert "changyeyue" in GOLD_OFFSPRING_IDS

    def test_count_gold_or_memory_excludes(self):
        """count_gold_or_memory 应正确排除指定角色"""
        units = [
            _unit("xiadie"), _unit("fengjin"), _unit("changyeyue"),
            _unit("xilian", gold=False),  # 昔涟自己标 false 也能走记忆命途
        ]
        assert count_gold_or_memory(units) == 4
        assert count_gold_or_memory(units, exclude_id="xilian") == 3
