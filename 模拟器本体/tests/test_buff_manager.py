"""测试 Buff 管理器"""
from engine.core.buff_manager import BuffManager


def test_apply_buff():
    bm = BuffManager()
    template = {
        "id": "atk_buff_50",
        "name": "攻击力提升50%",
        "type": "buff",
        "source": "character_skill",
        "attributes": {"ATK_percent": 50.0},
        "duration": {"type": "target_turn", "turns": 2, "permanent": False},
    }
    instance = bm.apply(template, "caster_1", "target_1")
    assert instance.remaining_turns == 2
    assert instance.attributes["ATK_percent"] == 50.0


def test_buff_tick_and_expire():
    bm = BuffManager()
    template = {
        "id": "temp_buff",
        "name": "临时buff",
        "type": "buff",
        "source": "character_skill",
        "attributes": {"SPD_percent": 10.0},
        "duration": {"type": "fixed_turns", "turns": 1},
    }
    bm.apply(template, "c1", "t1")

    # 未过期
    attrs = bm.get_active_buff_attributes("t1")
    assert len(attrs) == 1

    # 一回合后过期
    bm.tick_target("t1")
    attrs = bm.get_active_buff_attributes("t1")
    assert len(attrs) == 0


def test_permanent_buff():
    bm = BuffManager()
    template = {
        "id": "perm_buff",
        "name": "永久buff",
        "type": "buff",
        "source": "relic",
        "attributes": {"CRIT_RATE": 8.0},
        "duration": {"type": "permanent", "permanent": True},
    }
    bm.apply(template, "c1", "t1")
    bm.tick_target("t1")
    bm.tick_target("t1")
    attrs = bm.get_active_buff_attributes("t1")
    assert len(attrs) == 1  # 永不过期


def test_refresh_existing_buff():
    """同ID buff不可叠加时刷新持续时间"""
    bm = BuffManager()
    template = {
        "id": "refresh_buff",
        "name": "可刷新buff",
        "type": "buff",
        "source": "character_skill",
        "attributes": {"ATK_percent": 30.0},
        "duration": {"type": "fixed_turns", "turns": 2},
    }
    bm.apply(template, "c1", "t1")
    bm.tick_target("t1")  # remaining: 1
    # 重新施加，刷新为2
    bm.apply(template, "c1", "t1")
    buffs = bm.get_buffs_on("t1")
    assert len(buffs) == 1  # 不重复添加
    assert buffs[0].remaining_turns == 2  # 已刷新
