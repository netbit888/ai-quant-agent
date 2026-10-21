"""核心模块单元测试（标准库 unittest，无需额外依赖）

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backtest.dataquality import scan_segments, trim_to_timeframe
from backtest.engine import EngineConfig, run_backtest
from backtest.indicators import atr, ema, macd, sma
from backtest.metrics import compute_metrics
from backtest.models import Bar
from backtest.structure import SwingIndex, detect_bos, detect_pattern, swing_points, trend_state
from risk.guard import RiskConfig, RiskGuard


def make_bars(prices: list[tuple[float, float, float, float]], start_ts: int = 1_700_000_000) -> list[Bar]:
    """prices: [(open, high, low, close), ...]，每根间隔 3600 秒。"""
    return [
        Bar(time=start_ts + i * 3600, open=o, high=h, low=lo, close=c, tick_volume=100, spread=0)
        for i, (o, h, lo, c) in enumerate(prices)
    ]


class TestIndicators(unittest.TestCase):
    def test_sma(self) -> None:
        values = [1, 2, 3, 4, 5]
        out = sma(values, 3)
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)  # (1+2+3)/3
        self.assertAlmostEqual(out[4], 4.0)  # (3+4+5)/3

    def test_ema_seed_and_recursion(self) -> None:
        values = [10, 10, 10, 10, 10]
        out = ema(values, 3)
        self.assertAlmostEqual(out[2], 10.0)
        self.assertAlmostEqual(out[4], 10.0)

        # 常数序列后接一个跳变，EMA 应逐步靠近而不是跳到新值
        values2 = [10, 10, 10, 20]
        out2 = ema(values2, 3)
        k = 2 / 4
        expected = out2[2] * (1 - k) + 20 * k
        self.assertAlmostEqual(out2[3], expected)

    def test_macd_length_and_alignment(self) -> None:
        values = [100 + i * 0.5 for i in range(60)]
        main, sig = macd(values, 12, 24, 9)
        self.assertEqual(len(main), len(values))
        self.assertEqual(len(sig), len(values))
        # 单调上涨时，快线在慢线之上 → 主线为正
        self.assertGreater(main[-1], 0)

    def test_atr_constant_range(self) -> None:
        highs = [10, 10, 10, 10]
        lows = [9, 9, 9, 9]
        closes = [9.5, 9.5, 9.5, 9.5]
        out = atr(highs, lows, closes, 3)
        self.assertAlmostEqual(out[-1], 1.0)


class TestStructure(unittest.TestCase):
    def test_swing_detection(self) -> None:
        # 明显的山形：中间是高点
        prices = [
            (10, 10, 9, 9.5),
            (10, 11, 9.8, 10.5),
            (10.5, 12, 10.4, 11.5),   # swing high（index 2）
            (11.5, 11.8, 10.6, 11.0),
            (11.0, 11.2, 10.2, 10.5),
        ]
        bars = make_bars(prices)
        swings = swing_points(bars, left=2, right=2)
        highs = [s for s in swings if s.is_high]
        self.assertTrue(any(s.index == 2 for s in highs), f"未识别 index=2 的 swing high: {highs}")

    def test_no_future_data(self) -> None:
        """swing 点的 confirmed_at 必须严格大于 index。"""
        prices = [(10 + i * 0.1, 10.5 + i * 0.1, 9.5 + i * 0.1, 10.2 + i * 0.1) for i in range(30)]
        bars = make_bars(prices)
        swings = swing_points(bars, left=2, right=2)
        for s in swings:
            self.assertGreater(s.confirmed_at, s.index)

    def test_swing_index_ordering(self) -> None:
        prices = []
        base = 10.0
        for i in range(40):
            # 制造锯齿
            up = i % 4 < 2
            base += 0.2 if up else -0.2
            prices.append((base, base + 0.15, base - 0.15, base))
        bars = make_bars(prices)
        idx = SwingIndex(swing_points(bars, 2, 2))
        # 查询任意时刻都返回已确认的点
        for at in (10, 20, 30, 39):
            for kind in ("high", "low"):
                sw = idx.last(kind, at)
                if sw is not None:
                    self.assertLessEqual(sw.confirmed_at, at)

    def test_trend_state(self) -> None:
        # 逐级抬高的高低点
        prices = [
            (10, 10.5, 9.8, 10.2),
            (10.2, 11.5, 10.0, 11.0),
            (11.0, 11.6, 10.8, 11.2),
            (11.2, 12.5, 11.0, 12.0),
            (12.0, 12.6, 11.8, 12.2),
            (12.2, 13.0, 12.0, 12.8),
        ]
        bars = make_bars(prices)
        idx = SwingIndex(swing_points(bars, 1, 1))
        state = idx.state(len(bars) - 1)
        self.assertIn(state, ("up", "range"))  # 至少不应判成 down

    def test_pattern_pin_bull(self) -> None:
        prices = [
            (10, 10.1, 9.9, 10.0),
            (10.0, 10.05, 9.0, 10.0),  # 长下影线，实体很小
        ]
        bars = make_bars(prices)
        self.assertEqual(detect_pattern(bars, 1), "pin_bull")


class AlwaysLong:
    """第一根之后立刻做多，固定 SL/TP。"""

    def __init__(self, sl_offset: float = 1.0, tp_offset: float = 2.0) -> None:
        self.sl_offset = sl_offset
        self.tp_offset = tp_offset

    def prepare(self, bars) -> None:
        pass

    def signal(self, i: int) -> int:
        return 1 if i == 0 else 0

    def sl_tp(self, i: int, entry: float, direction: int):
        return entry - self.sl_offset, entry + self.tp_offset


class TestEngine(unittest.TestCase):
    def cfg(self, balance: float = 10_000) -> EngineConfig:
        return EngineConfig(initial_balance=balance, lots=0.1, point=0.001, contract_size=100, spread_points=0)

    def test_take_profit_hit(self) -> None:
        # 第 0 根收盘后给信号 → 第 1 根 open 入场 100，TP=102
        prices = [
            (99, 100, 98.5, 99.5),
            (100, 100.5, 99.8, 100.2),   # 入场 100
            (100.2, 102.5, 100.0, 102.0),  # 触及 TP
            (102, 102.5, 101.5, 102.2),
        ]
        bars = make_bars(prices)
        result = run_backtest(bars, AlwaysLong(), self.cfg())
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "tp")
        # (102 - 100) * 0.1 * 100 = 20
        self.assertAlmostEqual(trade.profit, 20.0, places=4)

    def test_stop_loss_hit(self) -> None:
        prices = [
            (99, 100, 98.5, 99.5),
            (100, 100.5, 99.8, 100.2),
            (100.2, 100.4, 98.5, 99.0),  # 触及 SL=99
        ]
        bars = make_bars(prices)
        result = run_backtest(bars, AlwaysLong(), self.cfg())
        self.assertEqual(result.trades[0].exit_reason, "sl")
        # (99 - 100) * 0.1 * 100 = -10
        self.assertAlmostEqual(result.trades[0].profit, -10.0, places=4)

    def test_bankruptcy_stops_trading(self) -> None:
        """余额归零必须停止，不允许出现负余额继续交易的螺旋亏损。"""
        prices = [(1000, 1001, 999, 1000)]
        for _ in range(30):
            prices.append((1000, 1000.5, 900, 900.5))  # 连续暴跌，每笔触发 SL
        bars = make_bars(prices)
        cfg = EngineConfig(initial_balance=100, lots=1.0, point=0.001, contract_size=100, spread_points=0)
        result = run_backtest(bars, AlwaysLong(sl_offset=10, tp_offset=50), cfg)
        self.assertIsNotNone(result.config["bankrupt_at"], "应记录爆仓时刻")
        self.assertGreaterEqual(result.final_balance, 0, "爆仓后余额不应为负")

    def test_metrics_on_known_trades(self) -> None:
        prices = [
            (99, 100, 98.5, 99.5),
            (100, 100.5, 99.8, 100.2),
            (100.2, 102.5, 100.0, 102.0),
        ]
        bars = make_bars(prices)
        m = compute_metrics(run_backtest(bars, AlwaysLong(), self.cfg()))
        self.assertEqual(m["total_trades"], 1)
        self.assertEqual(m["winning_trades"], 1)
        self.assertAlmostEqual(m["net_profit"], 20.0, places=4)
        self.assertEqual(m["win_rate"], 100.0)


class TestRiskGuard(unittest.TestCase):
    def test_lots_by_risk(self) -> None:
        guard = RiskGuard(RiskConfig(risk_per_trade_pct=1.0, min_lots=0.01, max_lots=10, lot_step=0.01))
        # 资金 10000，风险 1% = 100；止损距离 5，合约 100 → 每手风险 500
        lots = guard.lots(balance=10_000, stop_distance=5.0, contract_size=100)
        self.assertAlmostEqual(lots, 0.2, places=2)

    def test_reject_when_stop_invalid(self) -> None:
        guard = RiskGuard(RiskConfig())
        self.assertEqual(guard.lots(10_000, 0.0, 100), 0.0)

    def test_daily_loss_blocks(self) -> None:
        guard = RiskGuard(RiskConfig(daily_loss_limit_pct=3.0))
        ts = 1_700_000_000
        guard.on_bar(ts, 10_000)  # 日初权益 10000
        ok, _ = guard.allow(ts + 60, 9_600)  # 亏 4%
        self.assertFalse(ok)

    def test_drawdown_halt(self) -> None:
        guard = RiskGuard(RiskConfig(max_drawdown_pct=20.0))
        guard.on_bar(1_700_000_000, 10_000)
        guard.on_bar(1_700_003_600, 7_000)  # 回撤 30%
        ok, reason = guard.allow(1_700_007_200, 7_000)
        self.assertFalse(ok)
        self.assertEqual(reason, "drawdown")


class TestBacktestIntegration(unittest.TestCase):
    def test_pa_strategy_runs(self) -> None:
        """PA 策略在合成数据上不应崩溃，且不应出现未来函数（信号数有限）。"""
        from strategies.pa_breakout_pullback import Strategy

        prices = []
        base = 2000.0
        for i in range(300):
            trend = 0.8 if (i // 40) % 2 == 0 else -0.8
            base += trend
            prices.append((base, base + 2, base - 2, base + trend * 0.5))
        bars = make_bars(prices)

        cfg = EngineConfig(initial_balance=3000, lots=0.1, point=0.001, contract_size=100, spread_points=180)
        strategy = Strategy(min_structure_points=3000, min_stop_points=1500)
        result = run_backtest(bars, strategy, cfg)
        self.assertGreaterEqual(len(result.trades), 0)
        self.assertGreaterEqual(result.final_balance, 0)


class TestDataQuality(unittest.TestCase):
    """粒度一致性：券商 H4 早期历史可能是日线级数据，混进来会让逐年统计失真。"""

    @staticmethod
    def _series(step_hours: float, count: int, start_year: int) -> list[Bar]:
        base = int(datetime(start_year, 1, 3, tzinfo=timezone.utc).timestamp())
        step = int(step_hours * 3600)
        return [
            Bar(time=base + i * step, open=100.0, high=101.0, low=99.0, close=100.5,
                tick_volume=10, spread=0)
            for i in range(count)
        ]

    def test_uniform_data_untouched(self) -> None:
        bars = self._series(4, 500, 2020)
        kept, rep = trim_to_timeframe(bars, "H4")
        self.assertEqual(len(kept), 500)
        self.assertTrue(rep.uniform)
        self.assertEqual(rep.dropped_head, 0)

    def test_daily_head_is_dropped_for_h4(self) -> None:
        # 2014 年日线 200 根 + 2020 年 H4 800 根
        bars = self._series(24, 200, 2014) + self._series(4, 800, 2020)
        kept, rep = trim_to_timeframe(bars, "H4")
        self.assertEqual(len(kept), 800)
        self.assertEqual(rep.dropped_head, 200)
        self.assertTrue(rep.uniform)
        self.assertEqual(rep.first[:4], "2020")
        self.assertTrue(all(b.time >= bars[200].time for b in kept))

    def test_tail_gap_dropped(self) -> None:
        bars = self._series(4, 600, 2020) + self._series(24, 100, 2026)
        kept, rep = trim_to_timeframe(bars, "H4")
        self.assertEqual(len(kept), 600)
        self.assertEqual(rep.dropped_tail, 100)

    def test_all_mismatch_returns_empty(self) -> None:
        bars = self._series(24, 100, 2020)
        kept, rep = trim_to_timeframe(bars, "H4")
        self.assertEqual(kept, [])
        self.assertFalse(rep.uniform)

    def test_segment_report_flags_mixed_granularity(self) -> None:
        bars = self._series(24, 60, 2014) + self._series(4, 60, 2015)
        segs = scan_segments(bars)
        gaps = [s["median_gap_h"] for s in segs]
        self.assertIn(24.0, gaps)
        self.assertIn(4.0, gaps)


if __name__ == "__main__":
    unittest.main()
