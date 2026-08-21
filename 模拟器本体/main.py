"""崩坏：星穹铁道 伤害及配队模拟器 - 启动入口"""
import uvicorn
from web.app import app

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, reload=True)
