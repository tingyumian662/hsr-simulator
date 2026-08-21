"""欢愉战斗状态数据结构 — 笑点、Aha、好活当赏

好活当赏规则（2026版）：
- 全队每个欢愉角色独立存储自身好活当赏，互不共享
- 多批次获取的 BUFF 持续时间独立倒计时，层数叠加，到期分批扣除
- 基础持续时长默认 2 回合，部分角色天赋/星魂延长至 3 回合

获取来源：
1. 战斗开局：每个欢愉角色自动获得 20 层（2 回合）
2. 阿哈时刻：消耗笑点 N → 全队欢愉角色统一获得 N 层（1:1 转化）
"""
from dataclasses import dataclass, field

# 默认好活当赏持续时间
DEFAULT_GOOD_SHOW_TURNS = 2
# 战斗开局好活当赏层数
BATTLE_START_GOOD_SHOW_STACKS = 20.0


@dataclass
class GoodShowInstance:
    """好活当赏状态实例（一个批次）"""
    laugh_points: float              # 该批次记录的笑点数（层数）
    remaining_turns: int = 2         # 剩余回合
    source: str = ""                 # 来源标记: "battle_start" | "aha_moment" | 角色技能ID

    def tick(self):
        """回合倒计时"""
        if self.remaining_turns > 0:
            self.remaining_turns -= 1

    def is_expired(self) -> bool:
        return self.remaining_turns <= 0


@dataclass
class ElationBattleState:
    """欢愉战斗全局状态（单场战斗唯一实例）"""
    laugh_points: float = 0.0
    good_shows: dict[str, list[GoodShowInstance]] = field(default_factory=dict)

    # ===== 笑点管理 =====

    def add_laugh_points(self, amount: float):
        self.laugh_points += amount

    def consume_all_laugh_points(self) -> float:
        consumed = self.laugh_points
        self.laugh_points = 0.0
        return consumed

    # ===== 好活当赏管理 =====

    def grant_good_show(self, character_id: str, laugh_points: float,
                        duration: int = DEFAULT_GOOD_SHOW_TURNS,
                        source: str = "") -> GoodShowInstance:
        """授予一个角色好活当赏批次"""
        instance = GoodShowInstance(
            laugh_points=laugh_points,
            remaining_turns=duration,
            source=source,
        )
        if character_id not in self.good_shows:
            self.good_shows[character_id] = []
        self.good_shows[character_id].append(instance)
        return instance

    def get_good_show_total(self, character_id: str) -> float:
        """获取某角色当前所有未过期好活当赏的层数总和"""
        total = 0.0
        for gs in self.good_shows.get(character_id, []):
            if not gs.is_expired():
                total += gs.laugh_points
        return total

    def tick_all_good_shows(self) -> dict[str, float]:
        """回合结束时所有角色的好活当赏独立倒计时，清理过期批次。

        v6.7: 返回本回合到期（被清出）的层数 dict[char_id, 层数]，
        供绯英行迹2「开不败」将队友好活 50% 转为自身（调用方消费，原有调用忽略）。"""
        expired = {}
        for char_id in list(self.good_shows.keys()):
            lost = self.tick_good_show(char_id)
            if lost > 0:
                expired[char_id] = lost
        return expired

    def tick_good_show(self, character_id: str) -> float:
        """Advance only one character's Good Show batches by one turn."""
        batches = self.good_shows.get(character_id, [])
        for batch in batches:
            batch.tick()
        kept = [batch for batch in batches if not batch.is_expired()]
        lost = sum(batch.laugh_points for batch in batches) \
            - sum(batch.laugh_points for batch in kept)
        if character_id in self.good_shows:
            self.good_shows[character_id] = kept
        return lost

    # ===== 标准事件 =====

    def battle_start_init(self, elation_character_ids: list[str],
                          duration: int = DEFAULT_GOOD_SHOW_TURNS):
        """战斗开局：每个欢愉角色获得 20 层好活当赏。笑点由外部按角色数初始化。"""
        for cid in elation_character_ids:
            self.grant_good_show(
                cid, BATTLE_START_GOOD_SHOW_STACKS,
                duration=duration, source="battle_start"
            )

    def aha_moment_convert(self, elation_character_ids: list[str],
                           duration: int = DEFAULT_GOOD_SHOW_TURNS) -> float:
        """阿哈时刻：消耗全部笑点 → 全队欢愉角色统一获得 N 层好活当赏（1:1）。

        返回消耗的笑点数 N（即每个角色获得的层数）。
        调用时机：阿哈行动、所有欢愉技释放完毕后。
        """
        n = self.consume_all_laugh_points()
        if n > 0:
            for cid in elation_character_ids:
                self.grant_good_show(
                    cid, n, duration=duration, source="aha_moment"
                )
        return n


# ===== 独立计算函数 =====

def calc_aha_speed(elation_speeds: list[float]) -> float:
    """阿哈速度: 80 + x/5 + y/10 + z/20 + w/50（xyzw 降序排列）"""
    sorted_speeds = sorted(elation_speeds, reverse=True)
    coefficients = [5.0, 10.0, 20.0, 50.0]
    speed = 80.0
    for i, coef in enumerate(coefficients):
        spd = sorted_speeds[i] if i < len(sorted_speeds) else 0.0
        speed += spd / coef
    return speed


def calc_laugh_multiplier(n: float) -> float:
    """笑点/好活乘区: 1 + 5N / (N + 240)"""
    if n <= 0:
        return 1.0
    return 1.0 + (5.0 * n) / (n + 240.0)
