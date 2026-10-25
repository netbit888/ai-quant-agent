"""出场规则测试：程序闸门（止损/止盈门槛）与 K 线回放器。

这块的 bug 都是"看起来能用、亏得更多"那一类：
  · 止损排在止盈门槛后面 → 止损永不触发，参数越保守亏越多；
  · 空头忘记加点差 → 回放结果虚高，拿它定参数等于自己骗自己；
  · 同一根 K 线内先止盈后止损 → 系统性高估。没有 tick 就不知道先后，
    只能取不利的那个。
所以每一条都要钉死。
"""

from __future__ import annotations

import unittest

from live.autotrade import Cfg, _exit_gate, validate_cfg
from tools.replay_exit import replay_one, spec_of, summarize


class TestExitGate(unittest.TestCase):
    def test_stop_loss_fires_beyond_threshold(self):
        d = _exit_gate(Cfg(sl_usd=1.5, tp_min_usd=2.5), 300, -1.6)
        self.assertTrue(d["close"])
        self.assertEqual(d["source"], "sl")

    def test_stop_loss_does_not_fire_before_threshold(self):
        d = _exit_gate(Cfg(sl_usd=1.5, tp_min_usd=2.5), 300, -1.4)
        self.assertFalse(d["close"])
        self.assertEqual(d["source"], "tp_wait")

    def test_stop_loss_checked_before_tp_gate(self):
        """顺序错了就致命：浮亏 -5 会因为「没到止盈门槛」被判继续持有。

        这条是本文件最重要的断言。把 _exit_gate 里两个 if 调换顺序，它会红。
        """
        d = _exit_gate(Cfg(sl_usd=1.5, tp_min_usd=2.5), 300, -5.0)
        self.assertEqual(d["source"], "sl")
        self.assertTrue(d["close"])

    def test_below_tp_keeps_holding(self):
        d = _exit_gate(Cfg(sl_usd=1.5, tp_min_usd=2.5), 600, 2.0)
        self.assertFalse(d["close"])
        self.assertEqual(d["source"], "tp_wait")

    def test_at_tp_defers_to_model(self):
        """到门槛只是「允许平」，不是「强制平」——要不要跑让模型判断。"""
        self.assertIsNone(_exit_gate(Cfg(sl_usd=1.5, tp_min_usd=2.5), 600, 2.5))

    def test_tp_zero_falls_back_to_min_hold(self):
        d = _exit_gate(Cfg(tp_min_usd=0, min_hold_sec=60, min_hold_exempt_usd=0.5),
                       30, -0.2)
        self.assertEqual(d["source"], "min_hold")

    def test_tp_zero_and_hold_passed_defers_to_model(self):
        self.assertIsNone(
            _exit_gate(Cfg(tp_min_usd=0, min_hold_sec=60), 90, 0.3)
        )

    def test_sl_zero_disables_stop(self):
        """sl_usd=0 = 关掉止损（旧行为）。不能因为浮亏很大就强制平。"""
        d = _exit_gate(Cfg(sl_usd=0, tp_min_usd=0), 120, -9.0)
        self.assertIsNone(d)


class TestValidateCfgExitParams(unittest.TestCase):
    def test_rejects_tp_not_above_sl(self):
        issues = validate_cfg(Cfg(sl_usd=2.0, tp_min_usd=2.0))
        self.assertTrue(any("盈亏比" in x for x in issues), issues)

    def test_rejects_sl_inside_spread(self):
        issues = validate_cfg(Cfg(sl_usd=0.3, tp_min_usd=2.0))
        self.assertTrue(any("点差" in x for x in issues), issues)

    def test_defaults_pass(self):
        self.assertEqual(validate_cfg(Cfg()), [])


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.spread, self.contract, self.point = spec_of("XAUUSDm")
        self.unit = self.contract * 0.01          # 0.01 手 XAUUSD = 1.0

    def test_spec_matches_real_spread(self):
        """XAUUSDm 实测点差 240 points：0.24 价格单位，0.01 手 = 0.24 USD。"""
        self.assertAlmostEqual(self.spread, 0.24, places=4)
        self.assertAlmostEqual(self.unit * self.spread, 0.24, places=4)

    def test_long_stop_loss(self):
        bars = [{"time": 1, "open": 4000, "high": 4000.2, "low": 3998.3, "close": 3998.5}]
        pnl, kind, n = replay_one(bars, 0, 4000.0, 1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual((pnl, kind, n), (-1.5, "sl", 1))

    def test_long_take_profit(self):
        bars = [{"time": 1, "open": 4000, "high": 4002.6, "low": 3999.5, "close": 4002}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, 1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual((pnl, kind), (2.5, "tp"))

    def test_short_stop_loss_includes_spread(self):
        """空头开在 bid、平在 ask，少加 0.24 就会把亏损看小。"""
        bars = [{"time": 1, "open": 4000, "high": 4001.3, "low": 3999.9, "close": 4001}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, -1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual(kind, "sl")
        self.assertEqual(pnl, -1.5)

    def test_short_take_profit_includes_spread(self):
        bars = [{"time": 1, "open": 4000, "high": 3999.9, "low": 3997.2, "close": 3997.5}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, -1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual(kind, "tp")

    def test_same_bar_hits_both_takes_stop(self):
        """同一根里高低都够：没有 tick 判断先后，按最不利的算（止损）。"""
        bars = [{"time": 1, "open": 4000, "high": 4002.8, "low": 3998.4, "close": 4002}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, 1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual(kind, "sl")

    def test_timeout_settles_at_last_close(self):
        bars = [{"time": 1, "open": 4000, "high": 4001, "low": 3999, "close": 4000.5}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, 1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual((pnl, kind), (0.5, "timeout"))

    def test_walks_forward_until_trigger(self):
        """前几根不动，第三根才触发：要能一路走下去。"""
        bars = [
            {"time": 1, "open": 4000, "high": 4000.5, "low": 3999.8, "close": 4000.2},
            {"time": 2, "open": 4000, "high": 4000.6, "low": 3999.7, "close": 4000.3},
            {"time": 3, "open": 4000, "high": 4002.7, "low": 4000.0, "close": 4002.5},
        ]
        pnl, kind, n = replay_one(bars, 0, 4000.0, 1, 1.5, 2.5, self.spread, self.unit)
        self.assertEqual((pnl, kind, n), (2.5, "tp", 3))


class TestSummarize(unittest.TestCase):
    def test_required_win_rate_matches_ratio(self):
        """盈亏比 2 → 需要 33.3% 胜率，这个换算不能错。"""
        r = summarize("t", [2.0, 2.0, -1.0], {}, [60, 60, 60])
        self.assertAlmostEqual(r["ratio"], 2.0, places=2)
        self.assertAlmostEqual(r["need_rate"], 33.3, places=1)
        self.assertAlmostEqual(r["expect"], 1.0, places=3)

    def test_all_loss_has_no_ratio(self):
        r = summarize("t", [-1.0, -1.5], {}, [60, 60])
        self.assertIsNone(r["ratio"])
        self.assertEqual(r["win_rate"], 0.0)

    def test_counts_kinds(self):
        r = summarize("t", [2.0, -1.5], {"sl": 1, "tp": 1}, [60, 60])
        self.assertEqual(r["kinds"]["sl"], 1)


if __name__ == "__main__":
    unittest.main()
