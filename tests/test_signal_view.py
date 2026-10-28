"""待确认信号该不该发到页面上（回归：重启后弹出一个点了没用的窗）。

2026-10-29 实机：面板卡死 → 强关 → 重启，state 里那条 90 秒还没过期的
pending_signal 被原样送到页面，立刻弹出「同意做空」。但那个进程里没有任何
worker 在读答复——点了 approve 只是写一个没人消费的文件，倒计时走完消失。
人以为自己授权了一笔单，实际什么都没发生。

**面板真正读的是 `summary()["pending_signal"]`**（前端 sigPoll 每秒轮询
/api/autotrade），不是 `pending_signal_view()`——后者目前没有生产调用方，
但保留着同样的判据，两边都得对，所以两边都测。

判据落在 running 上：`_tick_body` 开始时写 running=True，`_stop` 清掉；
跨进程（CLI 跑 worker、面板只看）时 state 里同样是 True，不会被误杀。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade


def _sig() -> dict:
    return {
        "id": "2e0726d310f1", "kind": "entry", "direction": -1,
        "price": 4037.079, "expires_at": 9_999_999_999,
        "reason": "ER0.55下行趋势，顺势做空",
    }


def _state(*, running: bool, owner: str = "panel", with_sig: bool = True) -> dict:
    st = {"running": running, "owner": owner, "cfg": {"assist": True}}
    if with_sig:
        st["pending_signal"] = _sig()
    return st


class _Base(unittest.TestCase):
    def _call(self, fn, state: dict, alive: bool = True):
        worker = mock.Mock()
        worker.alive.return_value = alive
        with mock.patch.object(autotrade, "load_state", return_value=state), \
                mock.patch.object(autotrade, "read_log", return_value=[]), \
                mock.patch.object(autotrade, "WORKER", worker):
            return fn()


class SummarySignalTest(_Base):
    """summary() 是驱动弹窗的那一份。"""

    def test_live_worker_gets_the_signal(self):
        s = self._call(autotrade.summary, _state(running=True))
        self.assertIsNotNone(s["pending_signal"])
        self.assertEqual(s["pending_signal"]["id"], "2e0726d310f1")
        self.assertIn("remain_sec", s["pending_signal"])

    def test_stopped_run_does_not_send_signal(self):
        """面板已停 / 重启后 running=False：绝不弹窗。"""
        s = self._call(autotrade.summary, _state(running=False))
        self.assertIsNone(s["pending_signal"])
        self.assertFalse(s["running"])

    def test_dead_worker_with_stale_flag_does_not_send_signal(self):
        """state 还写着 running=True，但进程里的 worker 已经没了。"""
        s = self._call(autotrade.summary, _state(running=True), alive=False)
        self.assertIsNone(s["pending_signal"])
        self.assertFalse(s["running"], "界面也不该继续显示运行中")

    def test_cross_process_worker_is_not_killed(self):
        """CLI 在另一个进程里跑 worker、面板只看：不能因为没有本地线程就吞信号。"""
        s = self._call(autotrade.summary, _state(running=True, owner="cli"),
                       alive=False)
        self.assertIsNotNone(s["pending_signal"])

    def test_no_signal_is_just_none(self):
        s = self._call(autotrade.summary, _state(running=True, with_sig=False))
        self.assertIsNone(s["pending_signal"])


class PendingSignalViewTest(_Base):
    """同一个判据的另一份实现（现无生产调用方，防漂移）。"""

    def test_live_worker_gets_the_signal(self):
        v = self._call(autotrade.pending_signal_view, _state(running=True))
        self.assertIsNotNone(v["signal"])
        self.assertIsNone(v["signal_suppressed"])

    def test_stopped_run_does_not_send_signal(self):
        v = self._call(autotrade.pending_signal_view, _state(running=False))
        self.assertIsNone(v["signal"])
        self.assertEqual(v["signal_suppressed"], "not_running")

    def test_dead_worker_with_stale_flag_does_not_send_signal(self):
        v = self._call(autotrade.pending_signal_view, _state(running=True),
                       alive=False)
        self.assertIsNone(v["signal"])
        self.assertEqual(v["signal_suppressed"], "worker_dead")

    def test_cross_process_worker_is_not_killed(self):
        v = self._call(autotrade.pending_signal_view,
                       _state(running=True, owner="cli"), alive=False)
        self.assertIsNotNone(v["signal"])

    def test_no_signal_no_noise(self):
        v = self._call(autotrade.pending_signal_view,
                       _state(running=True, with_sig=False))
        self.assertIsNone(v["signal"])
        self.assertIsNone(v["signal_suppressed"], "本来就没信号，不该报抑制")


if __name__ == "__main__":
    unittest.main()
