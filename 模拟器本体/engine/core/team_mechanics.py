"""队伍机制检测 — 扫描队伍命途组成，激活对应子系统"""


class TeamMechanics:
    """战斗开始前检测队伍，决定激活哪些特殊机制"""
    active: set  # {"elation", "remembrance", ...}

    def __init__(self, units: list):
        self.active = set()
        paths = {u.char.path for u in units}
        if "欢愉" in paths:
            self.active.add("elation")
        if "记忆" in paths:
            self.active.add("remembrance")

    def has(self, name: str) -> bool:
        return name in self.active
