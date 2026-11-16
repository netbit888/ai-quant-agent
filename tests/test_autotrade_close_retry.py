"""平仓被券商拒：什么时候重试、什么时候必须停（2026-11-16）。

起因是真单事故：硬止损 -30.87 触发，程序发平仓单被 **10029（持仓被冻结）**
拒掉，然后直接 `_stop()` 把做单线程停了，仓位从此无人管理。

关键在于这不是偶发：10029 的触发条件是"价格贴近止损/止盈"，而**硬止损的
定义就是价格贴近止损**。所以这条路径每来一次正常止损，就有概率把系统打死。

这里守三条：
1. 瞬时码要换价重试，且重试期间券商 SL 先成交要认成成功（`already_closed`）；
2. 一次瞬时拒绝不停机，连续 `_CLOSE_FAIL_STOP` 轮才停；
3. 永久码（10027 算法交易没开之类）立刻停——重试不会自愈，只是白烧请求。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_up

from live import autotrade


class _FakeEx:
    """假执行器：按顺序吐出 close_position 的结果，记下被调了几次。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def connect(self):
        return True

    def close_position(self, ticket):
        self.calls += 1
        if not self._results:
            return {"ok": True, "price": 3999.0}
        return self._results.pop(0)

    def shutdown(self):
        pass


class TestSendCloseRetry(unittest.TestCase):
    def test_retries_frozen_then_succeeds(self):
        """10029 是"这一刻价格贴太近"，换一次报价重发就该过。"""
        ex = _FakeEx([{"ok": False, "retcode": 10029, "comment": "frozen"},
                      {"ok": True, "price": 3865.0}])
        with mock.patch.object(autotrade, "_executor", return_value=ex), \
             mock.patch.object(autotrade.time, "sleep"):
            res = autotrade.send_close(object(), 1)
        self.assertTrue(res["ok"])
        self.assertEqual(ex.calls, 2)
        self.assertNotIn("already_closed", res)

    def test_gone_counts_as_closed(self):
        """仓在发单前就没了（券商 SL 抢先成交）——这是成功，不是失败。"""
        ex = _FakeEx([{"ok": False, "gone": True, "retcode": None,
                       "reason": "持仓已不在券商端"}])
        with mock.patch.object(autotrade, "_executor", return_value=ex), \
             mock.patch.object(autotrade.time, "sleep"):
            res = autotrade.send_close(object(), 1)
        self.assertTrue(res["ok"])
        self.assertTrue(res["already_closed"])
        self.assertEqual(ex.calls, 1)

    def test_permanent_code_not_retried(self):
        """10027 重试一万次也是没开算法交易按钮。"""
        ex = _FakeEx([{"ok": False, "retcode": 10027, "comment": "no autotrade"}])
        with mock.patch.object(autotrade, "_executor", return_value=ex), \
             mock.patch.object(autotrade.time, "sleep"):
            res = autotrade.send_close(object(), 1)
        self.assertFalse(res["ok"])
        self.assertEqual(ex.calls, 1)
        self.assertIn("算法交易", res["reason"])

    def test_gives_up_after_attempts(self):
        """重试有上限，不能把一个卡死的平仓拖成死循环。"""
        ex = _FakeEx([{"ok": False, "retcode": 10029, "comment": "frozen"}] * 6)
        with mock.patch.object(autotrade, "_executor", return_value=ex), \
             mock.patch.object(autotrade.time, "sleep"):
            res = autotrade.send_close(object(), 1)
        self.assertFalse(res["ok"])
        self.assertEqual(ex.calls, 3)
        self.assertIn("冻结", res["reason"])


class TestTickCloseFailure(TempLedger):
    """tick 层：瞬时拒绝要不要停机。"""

    def _seed(self, **over) -> dict:
        state = autotrade._empty_state()
        pos = {"ticket": 42, "direction": 1, "lots": 0.01,
               "price_open": 4000.0, "opened_at": NOW - 60,
               "p_up": 0.6, "entry_reason": "突破"}
        pos.update(over)
        state["position"] = pos
        autotrade.save_state(state)
        return pos

    def _tick(self, close_res: dict):
        cfg = autotrade.Cfg(sl_usd=3.0, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": True, "reason": "动能衰竭",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_close", return_value=close_res):
            return autotrade.tick(cfg, now=NOW, quote=(4005.0, 4005.24),
                                  rows=bars_up(),
                                  positions=[{"ticket": 42, "symbol": "XAUUSDm"}])

    def test_frozen_reject_does_not_stop(self):
        """一次 10029 不停机：下一轮还有机会，券商 SL 也还挂着。"""
        self._seed()
        row = self._tick({"ok": False, "retcode": 10029,
                          "reason": autotrade.retcode_hint(10029)})

        self.assertFalse(row.get("stop"))
        self.assertEqual(row["kind"], "close_failed")
        self.assertEqual(row["streak"], 1)
        state = autotrade.load_state()
        self.assertIsNone(state.get("stop_reason"))
        self.assertIsNotNone(state["position"])   # 仓位还在，不能清

    def test_three_transient_failures_stop(self):
        """连续三轮都动不了，才是真故障——人得回终端。"""
        self._seed()
        res = {"ok": False, "retcode": 10029,
               "reason": autotrade.retcode_hint(10029)}
        rows = [self._tick(res) for _ in range(autotrade._CLOSE_FAIL_STOP)]

        self.assertFalse(rows[0].get("stop"))
        self.assertFalse(rows[1].get("stop"))
        self.assertTrue(rows[2].get("stop"))
        state = autotrade.load_state()
        self.assertIsNotNone(state.get("stop_reason"))
        self.assertIsNotNone(state["position"])

    def test_broker_fill_books_as_deal(self):
        """券商 SL 抢先成交：按 deal 记账，标记 fill_via=broker。"""
        self._seed()
        fills = {"pnl": -3.1, "price": 3996.9, "reason": 4, "time": NOW}
        with mock.patch.object(autotrade, "_settle_pnl",
                               return_value=(-3.1, 3996.9, fills)):
            row = self._tick({"ok": True, "already_closed": True,
                              "retcode": None, "price": 0.0})

        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["pnl"], -3.1)
        self.assertEqual(row["pnl_src"], "deal")
        self.assertEqual(row["fill_via"], "broker")
        self.assertEqual(row["exit_price"], 3996.9)
        self.assertIsNone(autotrade.load_state()["position"])

    def test_streak_resets_after_close(self):
        """失败计数必须随平仓归零，否则会跨笔累加，下一笔一失败就停机。"""
        self._seed()
        self._tick({"ok": False, "retcode": 10029,
                    "reason": autotrade.retcode_hint(10029)})
        state = autotrade.load_state()
        self.assertEqual(state["close_fail_streak"], 1)

        with mock.patch.object(autotrade, "_settle_pnl",
                               return_value=(-1.0, 3999.0, None)):
            row = self._tick({"ok": True, "already_closed": True,
                              "retcode": None, "price": 0.0})
        self.assertEqual(row["kind"], "close")
        self.assertEqual(autotrade.load_state()["close_fail_streak"], 0)


if __name__ == "__main__":
    unittest.main()
