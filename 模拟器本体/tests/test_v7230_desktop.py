"""v7.23.0: 桌面窗口入口——模式判定/端口选择/服务探活（纯逻辑, 不真开窗）。"""
import socket

import pytest

import main as entry


class TestWantWindow:
    def test_dev_default_off(self):
        assert entry.want_window(["main.py"]) is False
        assert entry.want_window(["python", "main.py"]) is False

    def test_flag_on(self):
        assert entry.want_window(["main.py", "--window"]) is True
        assert entry.want_window(["main.py", "-w"]) is True

    def test_frozen_auto_window(self):
        assert entry.want_window(["星铁模拟器.exe"], frozen=True) is True

    def test_exe_path_itself_not_flag(self):
        assert entry.want_window(["星铁模拟器_v7.23.0.exe"]) is False


class TestPickPort:
    def test_returns_port_in_range(self):
        assert 8000 <= entry.pick_port() < 8020

    def test_skips_occupied(self):
        p = entry.pick_port()
        dummy = socket.socket()
        try:
            dummy.bind(("127.0.0.1", p))
            dummy.listen(1)
            assert entry.pick_port(p) == p + 1
        finally:
            dummy.close()

    def test_exhaustion_raises(self):
        base = entry.pick_port()
        socks = []
        try:
            for p in range(base, base + 3):
                sk = socket.socket()
                sk.bind(("127.0.0.1", p))
                socks.append(sk)
            with pytest.raises(RuntimeError):
                entry.pick_port(base, attempts=3)
        finally:
            for sk in socks:
                sk.close()


class TestWaitServer:
    def test_ready_then_timeout_after_close(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert entry.wait_server(port, timeout=2.0) is True
        finally:
            srv.close()
        assert entry.wait_server(port, timeout=0.3) is False
