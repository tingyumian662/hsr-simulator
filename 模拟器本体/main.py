"""崩坏：星穹铁道 伤害及配队模拟器 - 启动入口

模式（v7.23.0 双轨）:
- python main.py            → web 开发模式（uvicorn 8000, 浏览器访问）
- python main.py --window   → 桌面窗口模式（本地服务 + 系统 WebView 窗口, 关窗即退）
- 打包 exe（PyInstaller frozen）→ 自动桌面窗口模式
"""
import os
import pathlib
import socket
import sys
import threading
import time

# v6.11.1: 显式把本文件目录放进 sys.path——即使从其他目录/方式启动,
# web 包也能被找到（任何 Python 版本稳定）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# v7.23.0: frozen 模式下相对路径 data/... 必须落在解包目录（sys._MEIPASS）
if getattr(sys, "frozen", False):
    os.chdir(sys._MEIPASS)

from web.app import app


def want_window(argv, frozen=False):
    """模式判定（纯函数, 供测试钉扎）: --window/-w 显式开窗; frozen 自动开窗。"""
    args = [a for a in argv if not a.endswith((".py", ".exe"))]
    return frozen or "--window" in args or "-w" in args


def pick_port(preferred=8000, attempts=20):
    """从 preferred 起找一个可绑定端口（窗口模式防开发服务端口冲突）。"""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {preferred}..{preferred + attempts - 1}")


def wait_server(port, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run_window():
    """桌面窗口模式: 守护线程跑 uvicorn, 主线程跑 WebView 消息循环。"""
    import uvicorn
    import webview  # lazy: 仅窗口模式需要（requirements-desktop.txt）

    port = pick_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    if not wait_server(port):
        raise RuntimeError("local server failed to start")
    webview.create_window("星铁模拟器", f"http://127.0.0.1:{port}",
                          width=1440, height=940, min_size=(1100, 700))
    webview.start()  # 阻塞至关窗; 守护线程随进程退出


def main():
    if want_window(sys.argv, frozen=getattr(sys, "frozen", False)):
        run_window()
        return
    import uvicorn
    # 直接传 app 对象 + 关热重载：reload=True 会 spawn 子进程用字符串重新导入,
    # 在 Python 3.14 / 新 uvicorn 下子进程 sys.path 不含项目目录 → No module named 'web'
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
