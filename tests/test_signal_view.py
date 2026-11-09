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

import time
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

    def test_carries_cool_left_for_same_direction(self):
        """弹窗要能提前说「现在点同意也不会执行」。

        2026-11-09 实盘：21:11:33 平多，21:12:11（74 秒后）弹出同向做多，
        `cooldown_same_sec=180` 还剩 106 秒——人否决后以为错过一波上涨，
        其实那张卡本来就会被 `can_open(direction=...)` 拦成 void_state。
        """
        st = _state(running=True)
        st["cfg"].update({"cooldown_sec": 60, "cooldown_same_sec": 180})
        st["last_close_ts"] = int(time.time()) - 74
        st["last_close_dir"] = -1          # 与 _sig() 的做空同向
        s = self._call(autotrade.summary, st)
        cool = s["pending_signal"]["cool_left"]
        self.assertGreaterEqual(cool, 100, "平仓 74 秒后同向冷却应还剩约 106 秒")
        self.assertLessEqual(cool, 106)


class SignalCoolLeftTest(unittest.TestCase):
    """`signal_cool_left`：只在**入场**信号上生效，且必须与 cooldown_left 同源。

    这里用的是 2026-11-09 那笔的真实时间线（平仓 1794229893，信号 tick
    1794229967），换成相对秒数后断言，免得测试依赖绝对时钟。
    """

    def _st(self, **kw) -> dict:
        st = {
            "running": True, "owner": "panel",
            "cfg": {"assist": True, "cooldown_sec": 60, "cooldown_same_sec": 180},
            "last_close_ts": 1000, "last_close_dir": 1,
        }
        st.update(kw)
        return st

    @staticmethod
    def _sig(kind: str = "entry", direction: int = 1) -> dict:
        return {"id": "5c1f0a9b7e33", "kind": kind, "direction": direction}

    def test_same_direction_counts_down_the_long_tier(self):
        st = self._st()
        self.assertEqual(autotrade.signal_cool_left(st, self._sig(), now=1074), 106)

    def test_opposite_direction_only_waits_the_short_tier(self):
        """反向单 60 秒就放行：74 秒后不该再提示冷却。"""
        st = self._st()
        self.assertEqual(
            autotrade.signal_cool_left(st, self._sig(direction=-1), now=1074), 0)

    def test_exit_signal_never_waits(self):
        """平仓永远允许，出场信号不该背着冷却提示。"""
        st = self._st()
        self.assertEqual(
            autotrade.signal_cool_left(st, self._sig(kind="exit"), now=1074), 0)

    def test_no_previous_close_means_no_cooldown(self):
        st = self._st(last_close_ts=None)
        self.assertEqual(autotrade.signal_cool_left(st, self._sig(), now=1074), 0)

    def test_expired_cooldown_is_zero(self):
        st = self._st()
        self.assertEqual(autotrade.signal_cool_left(st, self._sig(), now=1200), 0)

    def test_missing_cfg_falls_back_to_defaults(self):
        """旧 state 里没有 cfg 快照也不能炸：退回 Cfg 默认档（60/180）。"""
        st = self._st(cfg=None)
        self.assertEqual(autotrade.signal_cool_left(st, self._sig(), now=1074), 106)


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
