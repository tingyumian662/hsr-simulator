"""崩坏：星穹铁道 伤害及配队模拟器 - 启动入口"""
import uvicorn
from web.app import app

if __name__ == "__main__":
    # 直接传 app 对象 + 关闭热重载：reload=True 会 spawn 子进程用字符串重新导入,
    # 在 Python 3.14 / 新 uvicorn 下子进程 sys.path 不含项目目录 → No module named 'web'
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
