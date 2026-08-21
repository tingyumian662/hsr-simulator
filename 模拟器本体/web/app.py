"""FastAPI 应用入口"""
import traceback, sys
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="星穹铁道伤害模拟器", version="0.1.0")

# ── 全局异常处理：捕获所有未处理异常，返回 JSON 而非 HTML ──
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    print("\n=== SERVER ERROR ===", file=sys.stderr)
    print("".join(tb), file=sys.stderr)
    print("===================\n", file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__},
    )

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 注册 API 路由
from web.api import router
app.include_router(router, prefix="/api")

# 主页
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
