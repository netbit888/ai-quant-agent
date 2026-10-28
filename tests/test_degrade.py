"""兜底常量必须留痕：`_point` / `_digits` / `_spread_cost` 猜的值要能被查出来。

为什么
------
这些函数在算不出来时会退回兜底值（点值 0.001、小数位 3）。兜底本身是必要的
——一个陌生品种名不该让 prompt 构建崩掉——但它是**安静地算错**：点值猜错时，
黄金还凑合，BTC 的点差会被读成 640000 点塞给模型。没有痕迹的话，事后只能
怀疑"模型怎么这么蠢"。

所以这里断言两件事：降级要写字（kind=degrade），而且要**节流**（MT5 掉线时
每 tick 都会走这条路，不节流就是一天几千行，把真流水淹掉）。
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths


class TempLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")
        autotrade._DEGRADE_SEEN.clear()

    def tearDown(self):
        autotrade._DEGRADE_SEEN.clear()
        paths.set_root(None)
        self._tmp.cleanup()

    def degrade_rows(self):
        return [r for r in autotrade.read_log() if r.get("kind") == "degrade"]


class TestPointFallback(TempLedger):
    def test_unknown_symbol_still_returns_value(self):
        """陌生品种不许抛异常——prompt 构建不能因为一个品种名崩掉。"""
        self.assertAlmostEqual(autotrade._point("WHATEVER"), 0.001)
        self.assertEqual(autotrade._digits("WHATEVER"), 3)

    def test_unknown_symbol_leaves_trace(self):
        autotrade._point("WHATEVER")
        rows = self.degrade_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["what"], "point")
        self.assertEqual(rows[0]["symbol"], "WHATEVER")

    def test_known_symbol_is_silent(self):
        autotrade._point("XAUUSDm")
        autotrade._digits("XAUUSDm")
        self.assertEqual(self.degrade_rows(), [])

    def test_repeated_fallback_is_throttled(self):
        """10 分钟内同一个 (函数, 品种) 只记一行——否则掉线时刷爆流水。"""
        for _ in range(5):
            autotrade._point("WHATEVER")
            autotrade._digits("WHATEVER")
        self.assertEqual(len(self.degrade_rows()), 2)   # point 一行、digits 一行

    def test_throttle_expires(self):
        autotrade._point("WHATEVER")
        key = ("point", "WHATEVER")
        autotrade._DEGRADE_SEEN[key] -= autotrade._DEGRADE_INTERVAL + 1
        autotrade._point("WHATEVER")
        self.assertEqual(len(self.degrade_rows()), 2)


class TestSpreadCostHonesty(TempLedger):
    def test_returns_none_on_failure(self):
        """算不出来必须是 None，不能是 0.0——0.0 会让"成本为零"的闸门失效。"""
        with mock.patch("backtest.symbols.spread_cost_usd",
                        side_effect=RuntimeError("符号表坏了")):
            self.assertIsNone(autotrade._spread_cost("XAUUSDm", 0.01))
        rows = self.degrade_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["what"], "spread_cost")

    def test_returns_none_on_empty(self):
        with mock.patch("backtest.symbols.spread_cost_usd", return_value=None):
            self.assertIsNone(autotrade._spread_cost("XAUUSDm", 0.01))
        self.assertEqual(len(self.degrade_rows()), 1)

    def test_known_value_passes_through(self):
        with mock.patch("backtest.symbols.spread_cost_usd", return_value=0.24):
            self.assertAlmostEqual(autotrade._spread_cost("XAUUSDm", 0.01), 0.24)
        self.assertEqual(self.degrade_rows(), [])


if __name__ == "__main__":
    unittest.main()
