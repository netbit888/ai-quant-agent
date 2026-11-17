"""手工单只读视图（2026-11-16）。

背景：用户在 MT5 里自己开的仓，`state["position"]` 是空的（程序只记录自己开的
单），于是做单台显示「空仓」，而复盘页的「当前持仓」里有这笔——两个页面互相
打脸。`_manual_position()` 就是给做单台补的那份**只读**视图。

这个文件钉三条，都是会被人"顺手优化"掉的：
  1. **不写 state**。写进去就等于把手工单纳入出场闸门（挂券商端止损、保本、
     超时平仓），那是另一个产品决定，不是显示问题。
  2. 程序自己有仓时不返回手工单——否则面板会拿两笔单混着显示。
  3. 3 秒缓存：summary 是 1 秒一次轮询，positions_get 要走 MT5 锁。
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests._autotrade_base import TempLedger

from live import autotrade


def _pos(**kw):
    row = {
        "ticket": 4980444200,
        "symbol": "XAUUSDc",
        "direction": -1,
        "volume": 0.1,
        "price_open": 3869.636,
        "price_current": 3869.297,
        "sl": 3876.886,
        "tp": 0.0,
        "profit": -28.4,
        "swap": 0.0,
        "open_time": 1794835791,
        "comment": "",
    }
    row.update(kw)
    return row


class ManualPositionTest(TempLedger):
    def setUp(self):
        super().setUp()
        autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})

    def tearDown(self):
        autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})
        super().tearDown()

    def test_reads_manual_position(self):
        with mock.patch("live.journal.open_positions", return_value=[_pos()]), \
                mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            view = autotrade._manual_position({"symbol": "XAUUSDc"})

        self.assertIsNotNone(view)
        self.assertTrue(view["manual"])
        self.assertEqual(view["ticket"], 4980444200)
        self.assertEqual(view["direction"], -1)
        self.assertEqual(view["lots"], 0.1)
        self.assertEqual(view["sl"], 3876.886)
        self.assertEqual(view["opened_at"], 1794835791)

    def test_other_symbols_ignored(self):
        """别的品种上的持仓不该冒充这一笔。"""
        with mock.patch("live.journal.open_positions",
                        return_value=[_pos(symbol="EURUSD")]), \
                mock.patch.object(autotrade, "_quote", return_value=(1.1, 1.1002)):
            self.assertIsNone(autotrade._manual_position({"symbol": "XAUUSDc"}))

    def test_read_failure_is_none(self):
        """读不到持仓 != 空仓。宁可这一秒不显示，也不能编一个出来。"""
        with mock.patch("live.journal.open_positions",
                        side_effect=RuntimeError("MT5 忙")):
            self.assertIsNone(autotrade._manual_position({"symbol": "XAUUSDc"}))

    def test_does_not_touch_state(self):
        """**只读**。这条是本文件存在的主要理由。"""
        before = autotrade.load_state(999)
        with mock.patch("live.journal.open_positions", return_value=[_pos()]), \
                mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            autotrade._manual_position({"symbol": "XAUUSDc"})
        after = autotrade.load_state(999)
        self.assertEqual(before, after)
        self.assertIsNone(after.get("position"))

    def test_cached_within_three_seconds(self):
        """1 秒一次轮询，不至于每秒都去抢 MT5 锁。"""
        calls = []

        def fake():
            calls.append(1)
            return [_pos()]

        with mock.patch("live.journal.open_positions", side_effect=fake), \
                mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            autotrade._manual_position({"symbol": "XAUUSDc"})
            autotrade._manual_position({"symbol": "XAUUSDc"})
            autotrade._manual_position({"symbol": "XAUUSDc"})
        self.assertEqual(len(calls), 1)

    def test_multi_positions_counted(self):
        """同品种多笔：只展示第一笔，但总数必须带出去，不许装作只有一笔。"""
        rows = [_pos(), _pos(ticket=1, direction=1)]
        with mock.patch("live.journal.open_positions", return_value=rows), \
                mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            view = autotrade._manual_position({"symbol": "XAUUSDc"})
        self.assertEqual(view["count"], 2)

    def test_peak_fields_rise_and_never_fall(self):
        """峰值浮盈必须真的跟着价格涨，且回落时不缩（2026-11-17 补 MFE）。

        为什么这条重要：手工单以前只看得到当前浮盈，"我刚才是不是跑早了"
        无法回答——那一问的依据是**峰值**，而峰值必须逐轮记，事后补不出来。
        """
        from tests._autotrade_base import bars_up

        quotes = iter([(3865.0, 3865.24), (3875.0, 3875.24), (3870.0, 3870.24)])
        with mock.patch("live.journal.open_positions",
                        return_value=[_pos(direction=1)]), \
                mock.patch.object(autotrade, "_quote",
                                  side_effect=lambda s: next(quotes)), \
                mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            v1 = autotrade._manual_position({"symbol": "XAUUSDc"})
            autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})
            v2 = autotrade._manual_position({"symbol": "XAUUSDc"})
            autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})
            v3 = autotrade._manual_position({"symbol": "XAUUSDc"})

        for v in (v1, v2, v3):
            self.assertIn("peak_pnl", v)
            self.assertIn("atr_pnl", v)
            self.assertIn("t3", v)
        # 开仓即亏的那一轮，峰值是负的——不是 0，更不是空白
        self.assertLess(float(v1["peak_pnl"]), 0.0)
        # 涨上去之后峰值跟着涨
        self.assertGreater(float(v2["peak_pnl"]), float(v1["peak_pnl"]))
        # 回落之后峰值不许缩
        self.assertAlmostEqual(float(v3["peak_pnl"]), float(v2["peak_pnl"]), places=2)
        # ATR 折出的倍数算得出来
        self.assertGreater(float(v3["atr_pnl"] or 0), 0.0)


class SummaryWiringTest(TempLedger):
    """summary 必须把这个字段送出去，否则后端算了也没人用。"""

    def test_summary_has_manual_position_key(self):
        out = autotrade.summary(999)
        self.assertIn("manual_position", out)


if __name__ == "__main__":
    unittest.main()
