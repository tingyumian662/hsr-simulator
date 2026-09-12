"""v7.24.0: 版本号展示 + 一键复制诊断——数据源一致性 + 前端文本 pin。

版本机制: 模拟器本体/data/VERSION 为应用内单一版本源（随版本轮次与 HARNESS
同步 bump, _build_exe.py 双源一致性硬校验）; /api/list 暴露 version; 前端
brand-kicker 显示版本; 伤害汇总卡片"复制诊断"按钮导出版本+配置+结果。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "data" / "VERSION"
APP_JS = ROOT / "web" / "static" / "app.js"
INDEX_HTML = ROOT / "web" / "templates" / "index.html"


class TestVersionSource:
    def test_version_file_format(self):
        ver = VERSION_FILE.read_text(encoding="utf-8").strip()
        assert re.fullmatch(r"v7\.\d+\.\d+", ver), ver

    def test_api_list_exposes_version(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.api import APP_VERSION, router
        assert APP_VERSION == VERSION_FILE.read_text(encoding="utf-8").strip()
        app = FastAPI()
        app.include_router(router, prefix="/api")
        resp = TestClient(app).get("/api/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == APP_VERSION


class TestFrontendPins:
    def test_appjs_version_and_diagnostics(self):
        src = APP_JS.read_text(encoding="utf-8")
        assert "ALL.version || 'dev'" in src              # /api/list 版本消费
        assert "brand-kicker" in src                      # 标题栏版本徽标
        assert "async function copyDiagnostics()" in src  # 一键诊断
        assert "lastSim = {request: body, data}" in src   # 模拟结果暂存
        assert "execCommand" in src                       # 剪贴板回退路径
        assert "action_counts" in src                     # 诊断含行动数

    def test_html_button_and_cache_bust(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        assert 'onclick="copyDiagnostics()"' in src
        assert 'id="brand-kicker"' in src
        # 静态资源缓存版本串（WebView2 旧缓存防线, 改前端必须 bump）
        assert "app.js?v=20260907" in src
        assert "style.css?v=20260907" in src
