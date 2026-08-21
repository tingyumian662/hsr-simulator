"""角色工具函数 — 黄金裔检测、队伍组成统计等

用于昔涟行迹「岁月的旅人」等需要判断队伍构成的场合。

黄金裔判定原则：**数据优先** — 角色 JSON 的 `gold_offspring` 字段是权威
（如 changyeyue.json 标了 true）。硬编码集合仅兜底空壳角色
（无技能数据的角色，其 JSON 可能未标该字段）。
"""

# 黄金裔角色 ID 兜底集合（来自昔涟设计文档 — 昔涟.txt line 73-74）
# 仅用于未录入技能的空壳角色；ID 用真实 JSON 文件名（曾 5 个写错导致检测失效）
GOLD_OFFSPRING_IDS: set[str] = {
    "aglaea", "mydei", "fengjin", "cipher", "cerydra",     # kezhuladela→cerydra(刻律德菈)
    "xiadie", "hysilens", "tribbie", "anaxa",              # haiserin→hysilens(海瑟音), nakexia→anaxa(那刻夏)
    "trailblazer_remembrance", "phainon", "xilian",        # baiu→phainon(白厄); 新增昔涟
    "dan_heng_permansor_terrae", "changyeyue",             # danheng_tenghuang→真实id(丹恒·腾荒)
}


def has_poem(unit) -> bool:
    """判断单位是否已获得过献予诗（extra 含 poem_ 前缀标记）"""
    extra = getattr(unit, 'extra', {}) or {}
    return any(str(k).startswith('poem_') for k in extra)


def is_gold_offspring(unit) -> bool:
    """判断角色是否为黄金裔。

    优先读取角色的 gold_offspring 字段（JSON 权威数据），
    空壳角色无字段时用 ID 集合兜底。
    兼容传入 SimUnit 或 Character。
    """
    char = getattr(unit, 'char', unit)  # SimUnit → char
    data_flag = getattr(char, 'gold_offspring', None)
    if data_flag is not None:
        return bool(data_flag)
    return getattr(char, 'id', '') in GOLD_OFFSPRING_IDS


def count_gold_offspring(units: list) -> int:
    """统计队伍中黄金裔角色数量"""
    return sum(1 for u in units if is_gold_offspring(u))


def count_remembrance(units: list) -> int:
    """统计队伍中记忆命途角色数量"""
    return sum(1 for u in units if u.char.path == "记忆")


def count_gold_or_memory(units: list, exclude_id: str = "") -> int:
    """统计队伍中黄金裔或记忆命途角色数量（可排除指定角色）

    用于昔涟行迹3「岁月的旅人」:
        1名→+2追忆, 2名→+3追忆, 3名+→+6追忆
    """
    return sum(
        1 for u in units
        if u.char.id != exclude_id
        and (is_gold_offspring(u) or u.char.path == "记忆")
    )
