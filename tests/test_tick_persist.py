"""tick 的落盘纪律：**每一个出口都必须落盘**，异常出口也一样。

为什么单独立一组用例
--------------------
tick() 有二十多个出口，改造前每处手写「append_log + save_state + return」。
漏一处不报错，只是流水有了、state 没有（或反过来）：面板显示空仓而实际
持仓还在、信号一直挂着等人点。这类问题要等复盘对账才发现，而账就是钱。

所以这里不去断言业务结果（那是 test_autotrade 的事），只断言一条结构性质：
**tick 返回之后，save_state 至少被调用过一次。**
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths

NOW = 1_790_000_000


def bar(o, h, l, c, t=NOW):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def bars_up(n=30):
    return [bar(4000 + i, 4001 + i, 3999 + i, 4000.5 + i, NOW + i * 60) for i in range(n)]


class TempLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")
        self.cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=False,
                                 min_edge=0, act_guard=False, top_long_ban=False)

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()


class TestEveryExitPersists(TempLedger):
    def _count_saves(self):
        return mock.patch.object(autotrade, "save_state",
                                 wraps=autotrade.save_state)

    def test_skip_exit_persists(self):
        """拿不到报价 → skip，也要落盘（last_tick_ts 是面板的心跳）。"""
        with mock.patch.object(autotrade, "_quote", return_value=None), \
                self._count_saves() as m:
            row = autotrade.tick(self.cfg, now=NOW, rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertGreaterEqual(m.call_count, 1, "skip 出口没落盘")
        self.assertEqual(autotrade.load_state()["last_tick_ts"], NOW)

    def test_open_exit_persists(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.62, "reason": "突破",
                                             "source": "llm"}), \
                mock.patch.object(autotrade, "send_open",
                                 return_value={"ok": True, "price": 4001.0,
                                               "order": 700}), \
                self._count_saves() as m:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")
        self.assertGreaterEqual(m.call_count, 1, "open 出口没落盘")
        self.assertIsNotNone(autotrade.load_state()["position"])

    def test_close_exit_persists(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.62, "reason": "突破",
                                             "source": "llm"}), \
                mock.patch.object(autotrade, "send_open",
                                 return_value={"ok": True, "price": 4001.0,
                                               "order": 701}):
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": True, "reason": "见顶",
                                             "source": "llm"}), \
                mock.patch.object(autotrade, "send_close",
                                  return_value={"ok": True}), \
                self._count_saves() as m:
            row = autotrade.tick(self.cfg, now=NOW + 300, quote=(4003.0, 4003.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertGreaterEqual(m.call_count, 1, "close 出口没落盘")
        self.assertIsNone(autotrade.load_state()["position"])

    def test_exception_exit_still_persists(self):
        """tick 中途炸掉：要留一行 error，而且 state 也要落（不能停在上一轮）。"""
        with mock.patch.object(autotrade, "regime_of",
                               side_effect=RuntimeError("行情模块炸了")), \
                self._count_saves() as m:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "error")
        self.assertIn("行情模块炸了", row["reason"])
        self.assertGreaterEqual(m.call_count, 1, "异常出口没落盘")
        self.assertEqual(autotrade.load_state()["last_tick_ts"], NOW)

    def test_exception_row_lands_in_log(self):
        with mock.patch.object(autotrade, "regime_of",
                               side_effect=RuntimeError("boom")):
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        kinds = [r.get("kind") for r in autotrade.read_log()]
        self.assertIn("error", kinds)


if __name__ == "__main__":
    unittest.main()
