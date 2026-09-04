"""v7.18.0 (M8) —— 引擎主模块改名（旧名→combat_engine）收尾的结构钉扎。

grep-zero 范围 = 模拟器本体 全树（显式排除 __pycache__/.pytest_cache/.mimosa）,
覆盖一切文本扩展名; 交接文档/历史设计稿/.zcode/plans 为追加制历史记录, 不在范围。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_CORE = ROOT / "engine" / "core"

SCAN_SUFFIXES = {".py", ".cjs", ".html", ".js", ".css", ".bat", ".md", ".ini",
                 ".txt", ".cfg", ".toml", ".json", ".yml", ".yaml"}
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mimosa", ".git"}
# 运行时拼接避免本文件自身命中（自指豁免不走文件名白名单——钉扎必须覆盖本测试文件）
DEAD_TOKEN = "combat" + "_sim"


def test_rename_no_residual_token_anywhere():
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(seg in EXCLUDE_DIRS for seg in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() not in SCAN_SUFFIXES:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if DEAD_TOKEN in text:
            hits.append(str(p.relative_to(ROOT)))
    assert hits == [], f"residual {DEAD_TOKEN} token: {hits}"


def test_module_file_layout():
    assert (ENGINE_CORE / "combat_engine.py").is_file()
    assert not (ENGINE_CORE / f"{DEAD_TOKEN}.py").exists()


def test_import_surface():
    import engine.core.combat_engine as ce
    assert callable(ce.simulate)
    assert callable(ce._use_skill)
    assert callable(ce._gain_energy)
    # 改名后旧名不得可导入（无兼容别名——机械改名而非补丁）
    import importlib
    assert importlib.util.find_spec("engine.core." + DEAD_TOKEN) is None
