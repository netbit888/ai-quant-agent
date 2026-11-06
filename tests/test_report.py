"""账户报告（tools/report.py）：绝对口径的算法必须准。

quantstats 那半边靠库，这里钉的是**我们自己算的部分**——回撤、盈亏比、连亏。
口径错了不会报错，只会给出一个看起来合理的错数字，这是最难发现的一类。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import report  # noqa: E402

HAS_PANDAS = importlib.util.find_spec("pandas") is not None


def _row(pnl, ts=1000, kind="close"):
    return {"kind": kind, "pnl": pnl, "ts": ts}


class TestAbsoluteStats(unittest.TestCase):
    def test_basic_counts_and_totals(self):
        closes = [_row(2.0), _row(-1.0), _row(0.0), _row(-5.0), _row(3.0)]
        s = report.absolute_stats(closes)
        self.assertEqual(s["n"], 5)
        self.assertEqual((s["wins"], s["losses"], s["flats"]), (2, 2, 1))
        self.assertAlmostEqual(s["win_rate"], 0.4, places=4)
        self.assertAlmostEqual(s["total"], -1.0, places=2)
        self.assertAlmostEqual(s["avg_win"], 2.5, places=2)
        self.assertAlmostEqual(s["avg_loss"], -3.0, places=2)
        self.assertAlmostEqual(s["profit_factor"], 0.83, places=2)

    def test_max_drawdown_is_from_peak_of_equity(self):
        """累计 [2, 1, 1, -4, -1]：峰值 2，最低 -4 → 回撤 6。"""
        closes = [_row(p) for p in (2.0, -1.0, 0.0, -5.0, 3.0)]
        self.assertAlmostEqual(report.absolute_stats(closes)["max_drawdown"], 6.0, places=2)

    def test_zero_pnl_breaks_loss_streak(self):
        """0.00 是保本，既不算赢也不是"连亏"的一环。"""
        closes = [_row(p) for p in (-1.0, -2.0, 0.0, -3.0)]
        self.assertEqual(report.absolute_stats(closes)["longest_loss_streak"], 2)

    def test_no_losses_means_no_profit_factor(self):
        s = report.absolute_stats([_row(1.0), _row(2.0)])
        self.assertIsNone(s["profit_factor"])
        self.assertAlmostEqual(s["win_rate"], 1.0, places=4)

    def test_empty_is_safe(self):
        self.assertEqual(report.absolute_stats([]), {"n": 0})


class TestCloses(unittest.TestCase):
    def test_sorted_and_filtered(self):
        rows = [{"kind": "close", "pnl": 1.0, "ts": 30},
                {"kind": "open", "ts": 10},
                {"kind": "close", "ts": 20},            # 没有 pnl，坏行
                {"kind": "close", "pnl": -1.0, "ts": 20}]
        got = report._closes(rows)
        self.assertEqual([r["ts"] for r in got], [20, 30])


@unittest.skipUnless(HAS_PANDAS, "没装 pandas（requirements-dev.txt）")
class TestSeries(unittest.TestCase):
    def test_equity_and_returns_use_balance(self):
        closes = [_row(2.0, ts=1), _row(-1.0, ts=2)]
        equity, returns = report.build_series(closes, 100.0)
        self.assertAlmostEqual(float(equity.iloc[0]), 102.0, places=4)
        self.assertAlmostEqual(float(equity.iloc[-1]), 101.0, places=4)
        self.assertAlmostEqual(float(returns.iloc[0]), 0.02, places=6)
        self.assertAlmostEqual(float(returns.iloc[1]), -0.01, places=6)

    def test_bad_balance_is_rejected(self):
        with self.assertRaises(ValueError):
            report.build_series([_row(1.0)], 0.0)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            report.build_series([], 100.0)


class TestCli(unittest.TestCase):
    def test_html_without_balance_refuses(self):
        """没净值基数就不生成报告——否则回撤/收益率都是编的。"""
        self.assertEqual(report.main(["--html", "x.html"]), 2)


if __name__ == "__main__":
    unittest.main()
