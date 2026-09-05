"""v7.20.2: 录入基础属性离群检测（项目主 2026-09-06 指示）。

录入新角色时, 若基础属性与同星级同命途既有角色相差过大, 应复核询问项目主是否手误
（管线流程见 character-pipeline.md 五c; 禁止 AI 自行放行）。

阈值校准（v7.20.2 首跑全量 92 角色实证）:
- HP/ATK/DEF 取 ±40% vs 组中位数: 25% 档误报 21/92（DEF 呈天然胖尾——毁灭组真实区间
  194~776 全为游戏原值）, 40% 档仅 3 例存活且全为合理设计值（见白名单）;
  真实手误形态（缺位/错位数字, 如 1242→124/421）偏差普遍 >50%, 40% 可靠捕获;
  846→847 类一位数微误任何阈值都不可捕, 依赖项目主目检。
- SPD 取 ±15%: 现库所有组真实偏差 ≤±10%（速度带窄）, 15% 零误报。
- 组内 <3 人不判定（小样本中位数无意义）, 仅在报告中列出。
"""
import json
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "characters"

THRESHOLDS = {"base_HP": 0.40, "base_ATK": 0.40, "base_DEF": 0.40, "base_SPD": 0.15}
MIN_GROUP = 3

# 白名单: 每条必须带原因（HARNESS 记录裁决出处; 项目主可随时否决移除）
_EXCEPTIONS = {
    "firefly": "真实游戏数值: 完全燃烧型面板（HP 低/DEF 高）——v7.20.2 首跑校准",
    "mydei": "真实游戏数值: 血牛设计（HP 1552 最高档/DEF 194 最低档）——v7.20.2 首跑校准",
    "evanescia": "真实游戏数值: ATK 737 高于欢愉组中位（组内分布所致; 项目主 2026-09-06 复核确认为非自定义角色）",
    "tribbie": "真实游戏数值: DEF 727（同谐组中位 485 偏低）——v7.20.2 首跑校准",
    "phainon": "真实游戏数值: DEF 703（毁灭组 DEF 天然宽分布）——v7.20.2 首跑校准",
}


def _load_characters():
    chars = []
    for f in sorted(DATA.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        if raw.get("id") != f.stem:  # 模板/退场文件不入库
            continue
        chars.append(raw)
    return chars


def _find_outliers():
    groups = {}
    for d in _load_characters():
        groups.setdefault((d.get("rarity", 5), d.get("path")), []).append(d)
    flagged, small_groups = [], []
    for (rarity, path), members in sorted(groups.items()):
        if len(members) < MIN_GROUP:
            small_groups.append(f"{rarity}★{path}(n={len(members)})")
            continue
        med = {k: statistics.median(m[k] for m in members) for k in THRESHOLDS}
        for m in members:
            if m["id"] in _EXCEPTIONS:
                continue
            hits = []
            for k, th in THRESHOLDS.items():
                dev = (m[k] - med[k]) / med[k]
                if abs(dev) > th:
                    hits.append(f"{k[5:]}={m[k]:.0f}({dev:+.0%} vs 组中位{med[k]:.0f})")
            if hits:
                flagged.append(f"{m['name']}({m['id']}) {rarity}★{path}: {'; '.join(hits)}")
    return flagged, small_groups


def test_base_stats_within_peer_band():
    """同星级同命途基础属性离群清零（手误防线; 新角色录入必跑）。"""
    flagged, small = _find_outliers()
    assert not flagged, (
        "基础属性离群（录入复核流程: 询问项目主是否手误, 修正或带原因入白名单）:\n  "
        + "\n  ".join(flagged))


def test_exception_entries_exist_and_are_documented():
    """白名单条目必须对应真实角色且每条带原因注释（防滥用）。"""
    ids = {d["id"] for d in _load_characters()}
    for cid, reason in _EXCEPTIONS.items():
        assert cid in ids, f"白名单指向不存在的角色: {cid}"
        assert reason.strip(), f"白名单 {cid} 缺少原因说明"


def test_thresholds_documented_shape():
    """阈值形态钉扎: 三属性 40% / 速度 15% / 最小样本 3（改动须同步更新模块 docstring 校准记录）。"""
    assert THRESHOLDS == {"base_HP": 0.40, "base_ATK": 0.40,
                          "base_DEF": 0.40, "base_SPD": 0.15}
    assert MIN_GROUP == 3
