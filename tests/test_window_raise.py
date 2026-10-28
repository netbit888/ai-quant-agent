"""信号叫醒窗口的行为契约（回归：最大化窗口被信号叫醒后掉成小窗）。

为什么这个值得单独一个文件
--------------------------
2026-10-29 实机复现：面板开着最大化，每来一个开仓信号窗口就变小窗，用户
得手动再最大化一次。根因是 pywebview 的 `Window.restore()` 实现成「无条件
WindowState = Normal」——它分不清"最小化要还原"和"最大化不许动"。
现在改成 Win32：只在 IsIconic 时 ShowWindow(SW_RESTORE)，置顶只用
SetWindowPos 改 z 序。

窗口状态本身在 CI 里没法验（没有 WinForms），所以这里锁的是**调用契约**：
该不该 ShowWindow、该不该置顶、定时器什么时候撤。真窗口上的行为由
tools/_probe_raise2.py 实测（A 全绿：zoomed 保持 true）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from tools import dashboard as dash


class _FakeUser32:
    """只记 ShowWindow，其余方法一概没有——**用到就说明走错路了**。"""

    def __init__(self) -> None:
        self.show_window: list[tuple[int, int]] = []

    def ShowWindow(self, h, cmd):  # noqa: N802  照 Win32 的名字
        self.show_window.append((h, cmd))
        return 1


class _FakeTimers:
    """替掉 threading.Timer：收集回调，由用例决定何时触发。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, interval, fn):
        self.items.append({"interval": interval, "fn": fn})
        return mock.Mock()

    def fire(self, i: int) -> None:
        self.items[i]["fn"]()


class RaiseWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.u32 = _FakeUser32()
        self.timers = _FakeTimers()
        self.topmost: list[bool] = []
        self.stack = __import__("contextlib").ExitStack()
        for p in (
            mock.patch.object(dash, "_window_hwnd", return_value=4321),
            mock.patch.object(dash, "_user32", return_value=self.u32),
            mock.patch.object(dash, "_set_topmost",
                              side_effect=lambda h, on: self.topmost.append(on)),
            mock.patch.object(dash.threading, "Timer", self.timers),
            mock.patch.object(dash, "_raise_seq", 0),
            mock.patch.object(dash, "_over_top", False),
        ):
            self.stack.enter_context(p)
        self.addCleanup(self.stack.close)

    # ---------------------------------------------------------------- 基础
    def test_no_window_returns_not_ok(self):
        with mock.patch.object(dash, "_window_hwnd", return_value=0):
            res = dash._Bridge().raise_window(5)
        self.assertFalse(res["ok"])
        self.assertEqual(self.u32.show_window, [])
        self.assertEqual(self.topmost, [])

    def test_maximized_window_is_not_restored(self):
        """非最小化（含最大化）时一个窗口状态命令都不发——这是本次修的核心。"""
        with mock.patch.object(dash, "_is_minimized", return_value=False):
            res = dash._Bridge().raise_window(5)
        self.assertTrue(res["ok"])
        self.assertFalse(res["was_minimized"])
        self.assertEqual(self.u32.show_window, [], "最大化窗口不该被 ShowWindow 碰")
        self.assertEqual(self.topmost, [True], "置顶该生效")

    def test_minimized_window_is_restored_then_topped(self):
        with mock.patch.object(dash, "_is_minimized", return_value=True):
            res = dash._Bridge().raise_window(5)
        self.assertTrue(res["was_minimized"])
        # SW_RESTORE 交给系统：原本最大化就回最大化，不会变 Normal
        self.assertEqual(self.u32.show_window, [(4321, dash._SW_RESTORE)])
        self.assertEqual(self.topmost, [True])

    # ---------------------------------------------------------------- 置顶撤销
    def test_hold_expiry_releases_topmost(self):
        with mock.patch.object(dash, "_is_minimized", return_value=False):
            dash._Bridge().raise_window(5)
        self.assertEqual(len(self.timers.items), 1)
        self.assertAlmostEqual(self.timers.items[0]["interval"], 5.0)
        self.timers.fire(0)
        self.assertEqual(self.topmost, [True, False], "到期必须撤销置顶")

    def test_second_raise_wins_over_first_timer(self):
        """连来两个信号：第一个的定时器不许撤掉第二个刚设的置顶。"""
        with mock.patch.object(dash, "_is_minimized", return_value=False):
            dash._Bridge().raise_window(5)
            dash._Bridge().raise_window(5)
        self.assertEqual(self.topmost, [True, True])
        self.timers.fire(0)                      # 旧定时器到点
        self.assertEqual(self.topmost, [True, True], "旧定时器不该动置顶")
        self.timers.fire(1)                      # 最新那个到点
        self.assertEqual(self.topmost, [True, True, False])

    def test_user_topmost_is_left_alone(self):
        """用户自己开了置顶：不重复抬，也不替他关。"""
        with mock.patch.object(dash, "_over_top", True), \
                mock.patch.object(dash, "_is_minimized", return_value=False):
            res = dash._Bridge().raise_window(5)
        self.assertTrue(res["restored_to"])
        self.assertEqual(self.topmost, [], "已经置顶就不该再设一次")
        self.timers.fire(0)
        self.assertEqual(self.topmost, [], "更不该在到期后关掉用户的置顶")

    def test_hold_sec_is_clamped(self):
        """夹到 [1, 60] 秒；传 0/None 走默认 5 秒（既有语义，别改成夹成 1）。"""
        with mock.patch.object(dash, "_is_minimized", return_value=False):
            zero = dash._Bridge().raise_window(0)
            tiny = dash._Bridge().raise_window(0.2)
            high = dash._Bridge().raise_window(9999)
        self.assertAlmostEqual(zero["hold_sec"], 5.0)
        self.assertAlmostEqual(tiny["hold_sec"], 1.0)
        self.assertAlmostEqual(high["hold_sec"], 60.0)

    # ---------------------------------------------------------------- 兼容
    def test_state_still_reports_flags(self):
        with mock.patch.object(dash, "_WINDOW", object()), \
                mock.patch.object(dash, "_over_top", True):
            st = dash._Bridge().state()
        self.assertTrue(st["native"])
        self.assertTrue(st["on_top"])

    def test_set_on_top_uses_win32_not_pywebview(self):
        """set_on_top 也不能落到 `_WINDOW.on_top = ...`（跨线程改 WinForms）。"""

        class _TopWin:
            def __init__(self) -> None:
                self.written: list[bool] = []

            @property
            def on_top(self):
                return False

            @on_top.setter
            def on_top(self, v):
                self.written.append(v)

        win = _TopWin()
        with mock.patch.object(dash, "_WINDOW", win), \
                mock.patch.object(dash, "_over_top", False):
            res = dash._Bridge().set_on_top(True)
        self.assertTrue(res["ok"])
        self.assertEqual(self.topmost, [True], "应走 SetWindowPos")
        self.assertEqual(win.written, [], "不该给 pywebview 的 on_top 赋值")


class HangWatchdogTest(unittest.TestCase):
    """卡死取证的落盘逻辑。

    2026-10-29 那次卡死只留下 Windows 事件日志 AppHangB1，没有线程栈，
    事后无从定位。watchdog 的职责就是把这个空白补上——所以它自身必须有
    测试：写出文件、带所有线程栈、同一轮不重复刷盘。
    """

    def test_dump_writes_all_thread_stacks(self):
        import tempfile
        from pathlib import Path

        from live import appdirs

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(appdirs, "data_dir", return_value=Path(d)), \
                    mock.patch.object(dash, "_last_hang_dump", 0.0):
                p = dash._dump_ui_hang("hung")
                self.assertIsNotNone(p)
                text = Path(p).read_text(encoding="utf-8")
                self.assertIn("hung", text)
                self.assertIn("thread 0x", text.lower(), "必须带线程栈")
                self.assertIn("ui_hang_", Path(p).name)

    def test_second_dump_within_cooldown_is_skipped(self):
        import tempfile
        from pathlib import Path

        from live import appdirs

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(appdirs, "data_dir", return_value=Path(d)), \
                    mock.patch.object(dash, "_last_hang_dump", 0.0):
                self.assertIsNotNone(dash._dump_ui_hang("hung"))
                self.assertIsNone(dash._dump_ui_hang("hung"),
                                  "10 分钟内不许重复刷盘")
                # 恢复日志不受冷却限制——它是事件，不是刷屏
                self.assertIsNotNone(dash._dump_ui_hang("recovered_after_12s"))

    def test_dead_handle_counts_as_not_responsive(self):
        """不存在的句柄：SendMessageTimeout 返回 0，判为无响应。

        反过来更要紧——探针自己**绝不能**卡住，所以用 SMTO_ABORTIFHUNG。
        """
        import time as _t

        t0 = _t.perf_counter()
        res = dash._ui_responsive(0, timeout_ms=200)
        self.assertFalse(res)
        self.assertLess(_t.perf_counter() - t0, 1.0, "探针不许把调用方拖住")

    def test_hwnd_lookup_sends_no_messages(self):
        """探活路径一个消息都不许发。

        实测踩过：`GetWindowTextLengthW` 同进程内是发 WM_GETTEXTLENGTH 问窗口的，
        窗口一睡死，找句柄这一步自己就挂 9 秒（探针里量到 probe_cost=9.0s），
        看门狗于是永远等不到第一次探测。只读样式位才不会挂。
        """
        import inspect

        src = inspect.getsource(dash._find_main_hwnd)
        for bad in ("u.GetWindowText", "u.IsWindowVisible", "SendMessage"):
            self.assertNotIn(bad, src, f"枚举窗口时不许调 {bad}")


if __name__ == "__main__":
    unittest.main()
