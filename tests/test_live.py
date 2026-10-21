"""实盘账本与统计的单元测试（不依赖 MT5 终端）。"""

from __future__ import annotations

import unittest

from live.journal import Trade, _reason, build_trades
from live.stats import compute, risk_flags

DAY = 86400
T0 = 1_790_000_000


def deal(ticket, pid, typ, entry, vol, price, profit=0.0, t=T0, comment="", swap=0.0):
    return {
        "ticket": ticket, "order": ticket, "position_id": pid, "symbol": "XAUUSDm",
        "type": typ, "entry": entry, "volume": vol, "price": price,
        "profit": profit, "commission": 0.0, "swap": swap, "fee": 0.0,
        "time": t, "comment": comment, "magic": 0,
    }


class TestBuildTrades(unittest.TestCase):
    def test_simple_long_roundtrip(self):
        deals = [
            deal(1, 100, 0, 0, 0.1, 4000.0),
            deal(2, 100, 1, 1, 0.1, 4010.0, profit=99.0, t=T0 + 600, comment="[tp 4010.0]"),
        ]
        trades = build_trades(deals)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.direction, 1)
        self.assertAlmostEqual(t.profit, 99.0)
        self.assertEqual(t.close_reason, "tp")
        self.assertEqual(t.holding_min, 10.0)

    def test_short_direction(self):
        deals = [
            deal(1, 101, 1, 0, 0.1, 4000.0),
            deal(2, 101, 0, 1, 0.1, 3990.0, profit=99.0, t=T0 + 60),
        ]
        t = build_trades(deals)[0]
        self.assertEqual(t.direction, -1)
        self.assertAlmostEqual(t.profit, 99.0)

    def test_partial_close_weighted_price(self):
        deals = [
            deal(1, 102, 0, 0, 0.2, 4000.0),
            deal(2, 102, 1, 1, 0.1, 4010.0, profit=50.0, t=T0 + 60),
            deal(3, 102, 1, 1, 0.1, 4020.0, profit=100.0, t=T0 + 120),
        ]
        t = build_trades(deals)[0]
        self.assertEqual(t.volume, 0.2)
        self.assertAlmostEqual(t.exit_price, 4015.0)
        self.assertAlmostEqual(t.profit, 150.0)
        self.assertEqual(t.exit_time, T0 + 120)

    def test_open_position_not_closed(self):
        deals = [deal(1, 103, 0, 0, 0.1, 4000.0)]
        t = build_trades(deals)[0]
        self.assertFalse(t.is_closed)
        self.assertIsNone(t.holding_min)

    def test_balance_deals_excluded(self):
        deals = [
            deal(1, 0, 2, 0, 0.0, 0.0, profit=500.0, comment="D-ALLINT"),
            deal(2, 104, 0, 0, 0.1, 4000.0),
            deal(3, 104, 1, 1, 0.1, 3990.0, profit=-99.0, t=T0 + 60),
        ]
        trades = build_trades(deals)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].profit, -99.0)

    def test_swap_and_commission_included(self):
        deals = [
            deal(1, 105, 0, 0, 0.1, 4000.0),
            deal(2, 105, 1, 1, 0.1, 4010.0, profit=99.0, t=T0 + 60, swap=-3.5),
        ]
        t = build_trades(deals)[0]
        self.assertAlmostEqual(t.profit, 95.5)

    def test_reason_parsing(self):
        self.assertEqual(_reason("[tp 4010.13600]"), "tp")
        self.assertEqual(_reason("[sl 3980.0]"), "sl")
        self.assertEqual(_reason("[so -4.52%/-0.94/20."), "stop_out")
        self.assertEqual(_reason(""), "manual")
        self.assertEqual(_reason("anything else"), "manual")


def _trade(pid, profit, entry_offset, hold_min=60, reason="sl", direction=1):
    return {
        "position_id": pid, "symbol": "XAUUSDm", "direction": direction, "volume": 0.1,
        "entry_time": T0 + entry_offset, "entry_price": 4000.0,
        "exit_time": T0 + entry_offset + int(hold_min * 60), "exit_price": 4000.0,
        "profit": profit, "gross": profit, "commission": 0.0, "swap": 0.0,
        "comment": "", "close_reason": reason,
    }


class TestStats(unittest.TestCase):
    def test_empty(self):
        s = compute([], None, 0)
        self.assertTrue(s["empty"])

    def test_basic_metrics(self):
        trades = [
            _trade(1, 100.0, 0),
            _trade(2, -50.0, 600),
            _trade(3, 200.0, 1200),
            _trade(4, -50.0, 1800),
        ]
        s = compute(trades, {"balance": 1200}, 1000.0)
        self.assertEqual(s["count"], 4)
        self.assertAlmostEqual(s["net"], 200.0)
        self.assertEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["pf"], 3.0)  # 300 / 100
        self.assertAlmostEqual(s["avg_win"], 150.0)
        self.assertAlmostEqual(s["avg_loss"], -50.0)
        self.assertAlmostEqual(s["risk_reward"], 3.0)
        self.assertAlmostEqual(s["expectancy"], 50.0)
        self.assertAlmostEqual(s["return_pct"], 20.0)

    def test_drawdown_is_peak_to_trough(self):
        # 起始 1000 → 赚 100 到 1100 → 亏 300 到 800
        # 回撤 = 1100 - 800 = 300，占峰值 1100 的 27.3%（不是占本金的 30%）
        trades = [_trade(1, 100.0, 0), _trade(2, -300.0, 600)]
        s = compute(trades, {"balance": 800}, 1000.0)
        self.assertAlmostEqual(s["max_dd"], 300.0)
        self.assertAlmostEqual(s["max_dd_pct"], 27.3, places=1)
        self.assertAlmostEqual(s["peak_balance"], 1100.0)

    def test_drawdown_pct_never_exceeds_100(self):
        # 穿仓：本金 1000，亏 1500 → 余额被负余额保护抹到 0
        trades = [_trade(1, -1500.0, 0)]
        s = compute(trades, {"balance": 0}, 1000.0)
        self.assertEqual(s["max_dd_pct"], 100.0)
        self.assertAlmostEqual(s["max_dd"], 1000.0)
        self.assertEqual(s["wipeouts"], 1)
        self.assertAlmostEqual(s["trough_balance"], 0.0)
        self.assertGreaterEqual(s["trough_balance"], 0.0)

    def test_withdrawal_is_not_a_drawdown(self):
        # 入金 1000 → 赚 200 → 出金 700 → 亏 100
        # 出金不是亏损，回撤只算那 100
        flows = [{"time": T0 - 100, "amount": 1000}, {"time": T0 + 300, "amount": -700}]
        trades = [_trade(1, 200.0, 0), _trade(2, -100.0, 600)]
        s = compute(trades, {"balance": 400}, 300.0, flows)
        self.assertAlmostEqual(s["max_dd"], 100.0)
        self.assertAlmostEqual(s["max_dd_pct"], 20.0)  # 从出金后的 500 跌到 400
        self.assertEqual(s["wipeouts"], 0)

    def test_balance_hitting_zero_counts_wipeout(self):
        flows = [{"time": T0 - 100, "amount": 500}]
        trades = [_trade(1, -500.0, 0)]
        s = compute(trades, {"balance": 0}, 500.0, flows)
        self.assertEqual(s["wipeouts"], 1)
        self.assertEqual(s["max_dd_pct"], 100.0)

    def test_peak_is_historical_high_not_final(self):
        # 入金 1000 → 赚 600 到 1600（历史峰值）→ 出金 800 → 亏 300
        # 峰值必须仍然是 1600，不能被后面的出金改写成最终余额
        flows = [{"time": T0 - 100, "amount": 1000}, {"time": T0 + 2000, "amount": -800}]
        trades = [_trade(1, 600.0, 0, hold_min=10), _trade(2, -300.0, 2000, hold_min=10)]
        s = compute(trades, {"balance": 500}, 200.0, flows)
        self.assertAlmostEqual(s["peak_balance"], 1600.0)
        # 出金后的水位是 800，再亏 300 → 回撤 300 / 800 = 37.5%
        self.assertAlmostEqual(s["max_dd"], 300.0)
        self.assertAlmostEqual(s["max_dd_pct"], 37.5)

    def test_streaks(self):
        trades = [
            _trade(1, -10, 0), _trade(2, -10, 600), _trade(3, -10, 1200),
            _trade(4, 20, 1800), _trade(5, 20, 2400),
        ]
        s = compute(trades, {}, 1000.0)
        self.assertEqual(s["max_loss_streak"], 3)
        self.assertEqual(s["max_win_streak"], 2)

    def test_win_rate_breakeven_math(self):
        # 盈亏比 0.75、胜率 44.7% → 期望为负，PF 必须 < 1
        trades = [_trade(i, 5.0, i * 600) for i in range(1, 45)]
        trades += [_trade(100 + i, -7.0, (45 + i) * 600) for i in range(1, 56)]
        s = compute(trades, {"balance": 0}, 1000.0)
        self.assertLess(s["pf"], 1.0)
        self.assertLess(s["expectancy"], 0)

    def test_risk_flags_on_blown_account(self):
        trades = [_trade(i, -50.0, i * 600) for i in range(1, 6)]
        s = compute(trades, {"balance": 20, "margin_level": 0}, 1000.0)
        flags = risk_flags(s)
        titles = " ".join(f["title"] for f in flags)
        self.assertIn("本金已亏损", titles)
        self.assertIn("盈利因子", titles)

    def test_risk_flags_all_good(self):
        trades = [_trade(i, 50.0, i * 600) for i in range(1, 30)]
        s = compute(trades, {"balance": 5000, "margin_level": 0}, 1000.0)
        flags = risk_flags(s)
        self.assertEqual(flags[0]["level"], "ok")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 近期质量
import itertools

from live.quality import (
    MIN_SAMPLE,
    compute as quality_compute,
    merge_snapshot,
    rolling_expectancy,
    snapshot_row,
    wilson_ci,
)


def _q(profit, i, gap_days=1.0):
    """第 i 笔：间隔 gap_days 天平仓。"""
    exit_ts = T0 + int(i * gap_days * 86400)
    return {
        "position_id": i, "symbol": "XAUUSDm", "direction": 1, "volume": 0.1,
        "entry_time": exit_ts - 3600, "entry_price": 4000.0,
        "exit_time": exit_ts, "exit_price": 4000.0,
        "profit": profit, "gross": profit, "commission": 0.0, "swap": 0.0,
        "comment": "", "close_reason": "manual",
    }


class TestWilsonCI(unittest.TestCase):
    def test_known_values(self):
        # 5/10 的 Wilson 95% 区间约 [23.7%, 76.3%]
        lo, hi = wilson_ci(5, 10)
        self.assertAlmostEqual(lo, 23.7, places=1)
        self.assertAlmostEqual(hi, 76.3, places=1)

    def test_widens_as_sample_shrinks(self):
        w10 = wilson_ci(5, 10)
        w100 = wilson_ci(50, 100)
        self.assertGreater(w10[1] - w10[0], w100[1] - w100[0])

    def test_bounds_stay_in_range(self):
        for k, n in ((0, 5), (5, 5), (0, 1), (1, 1)):
            lo, hi = wilson_ci(k, n)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 100.0)

    def test_empty(self):
        self.assertEqual(wilson_ci(0, 0), (0.0, 0.0))


class TestQualityWindows(unittest.TestCase):
    def _series(self, n=40, profit_a=20.0, profit_b=-10.0):
        return [_q(profit_a if i % 2 == 0 else profit_b, i) for i in range(n)]

    def test_count_window_takes_last_n(self):
        trades = self._series()
        now = T0 + 40 * 86400
        q = quality_compute(trades, now=now)
        w = next(x for x in q["windows"] if x["key"] == "20n")
        self.assertEqual(w["current"]["n"], 20)
        # 盈亏交替 → 10 胜 10 负
        self.assertEqual(w["current"]["win_rate"], 50.0)
        self.assertAlmostEqual(w["current"]["expectancy"], 5.0)

    def test_previous_window_is_equal_length(self):
        trades = self._series()
        q = quality_compute(trades, now=T0 + 40 * 86400)
        w = next(x for x in q["windows"] if x["key"] == "20n")
        self.assertEqual(w["previous"]["n"], 20)

    def test_time_window_filters_by_exit_time(self):
        trades = self._series()
        now = T0 + 39 * 86400 + 3600
        q = quality_compute(trades, now=now)
        w = next(x for x in q["windows"] if x["key"] == "7d")
        # 最近 7 天内平仓的：第 33~39 笔，共 7 笔
        self.assertEqual(w["current"]["n"], 7)
        self.assertTrue(w["current"]["low_sample"])

    def test_low_sample_threshold(self):
        small = quality_compute(self._series(n=MIN_SAMPLE - 1), now=T0 + 20 * 86400)
        w = next(x for x in small["windows"] if x["key"] == "20n")
        self.assertTrue(w["current"]["low_sample"])

        enough = quality_compute(self._series(n=MIN_SAMPLE), now=T0 + 20 * 86400)
        w2 = next(x for x in enough["windows"] if x["key"] == "20n")
        self.assertFalse(w2["current"]["low_sample"])

    def test_no_baseline_when_no_prior_trades(self):
        trades = self._series(n=10)
        q = quality_compute(trades, now=T0 + 10 * 86400)
        w = next(x for x in q["windows"] if x["key"] == "20n")
        self.assertEqual(w["previous"]["n"], 0)
        self.assertEqual(w["verdict"], "low_sample")  # 当前窗口本身就不足

    def test_open_positions_excluded(self):
        trades = self._series(n=25)
        trades.append({
            "position_id": 999, "symbol": "XAUUSDm", "direction": 1, "volume": 0.1,
            "entry_time": T0, "entry_price": 4000.0, "exit_time": None,
            "profit": 0.0, "close_reason": "open",
        })
        q = quality_compute(trades, now=T0 + 30 * 86400)
        w = next(x for x in q["windows"] if x["key"] == "20n")
        self.assertEqual(w["current"]["n"], 20)

    def test_empty(self):
        q = quality_compute([], now=T0)
        self.assertTrue(q["empty"])

    def test_drawdown_in_r_is_scale_free(self):
        # 连亏三笔 -10，风险单位也是 10 → 回撤 3R；之后 +50 回本
        trades = [_q(-10.0, i) for i in range(3)] + [_q(50.0, 3)]
        q = quality_compute(trades, now=T0 + 30 * 86400)
        w = next(x for x in q["windows"] if x["key"] == "20n")
        self.assertAlmostEqual(w["current"]["dd_r"], 3.0, places=2)


class TestRollingExpectancy(unittest.TestCase):
    def test_length_and_values(self):
        trades = [_q(20.0 if i % 2 == 0 else -10.0, i) for i in range(40)]
        roll = rolling_expectancy(trades, span=20)
        self.assertEqual(len(roll), 40 - 20 + 1)
        # 期望 = (10*20 + 10*(-10))/20 = 5；风险单位 10 → 0.5R
        self.assertAlmostEqual(roll[0]["r"], 0.5, places=3)

    def test_insufficient_trades(self):
        self.assertEqual(rolling_expectancy([_q(10.0, i) for i in range(5)], span=20), [])


class TestQualitySnapshot(unittest.TestCase):
    def test_same_day_overwrites(self):
        q = quality_compute([_q(20.0 if i % 2 == 0 else -10.0, i) for i in range(25)],
                            now=T0 + 30 * 86400)
        row1 = snapshot_row(q, day="2026-10-19")
        row2 = snapshot_row(q, day="2026-10-19")
        merged = merge_snapshot([row1], row2)
        self.assertEqual(len(merged), 1)

    def test_different_days_appended_in_order(self):
        q = quality_compute([_q(20.0, i) for i in range(25)], now=T0 + 30 * 86400)
        rows = merge_snapshot([], snapshot_row(q, day="2026-10-20"))
        rows = merge_snapshot(rows, snapshot_row(q, day="2026-10-18"))
        self.assertEqual([r["date"] for r in rows], ["2026-10-18", "2026-10-20"])

    def test_row_has_all_windows(self):
        q = quality_compute([_q(20.0 if i % 2 == 0 else -10.0, i) for i in range(25)],
                            now=T0 + 30 * 86400)
        row = snapshot_row(q, day="2026-10-19")
        for key in ("7d", "30d", "1y", "20n", "100n"):
            self.assertIn(key, row)
            self.assertIn("expectancy_r", row[key])


# ---------------------------------------------------------------- 付费 API 确认闸门
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from live import advisor, paths
from live.stats import compute as stats_compute


ADVICE_JSON = {
    "headline": "测试结论",
    "verdict": "亏损",
    "problems": [{"severity": "高", "title": "p", "evidence": "e", "root_cause": "r"}],
    "actions": [{"priority": 1, "action": "a", "why": "w", "how_to_verify": "v"}],
    "note": "n",
}


class TestAdviceConfirmationGate(unittest.TestCase):
    """核心约束：没有 allow_call=True 就绝不允许发出外部请求。"""

    LOGIN = "999001"   # 测试账户，数据写临时目录

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        self.trades = [_q(20.0 if i % 2 == 0 else -10.0, i) for i in range(30)]
        self.stats = stats_compute(self.trades, {"balance": 1000}, 1000.0)

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log_path(self):
        return paths.account_file(self.LOGIN, "api_calls.jsonl")

    def test_without_cache_and_without_allow_returns_need_confirm(self):
        with mock.patch("agent.llm.chat_ex") as fake:
            res = advisor.advise(self.stats, [], self.trades, allow_call=False, login=self.LOGIN)
        self.assertFalse(res["ok"])
        self.assertTrue(res["need_confirm"])
        fake.assert_not_called()

    def test_allow_call_triggers_exactly_one_request_and_logs(self):
        payload = "```json\n" + json.dumps(ADVICE_JSON, ensure_ascii=False) + "\n```"
        with mock.patch("agent.llm.chat_ex", return_value=(payload, {"total_tokens": 123})) as fake:
            res = advisor.advise(self.stats, [], self.trades, allow_call=True, login=self.LOGIN)
        self.assertTrue(res["ok"])
        self.assertFalse(res["cached"])
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(res["advice"]["headline"], "测试结论")

        lines = self._log_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertTrue(row["ok"])
        self.assertEqual(row["usage"]["total_tokens"], 123)

    def test_second_call_hits_cache_without_request(self):
        payload = "```json\n" + json.dumps(ADVICE_JSON, ensure_ascii=False) + "\n```"
        with mock.patch("agent.llm.chat_ex", return_value=(payload, {})) as fake:
            advisor.advise(self.stats, [], self.trades, allow_call=True, login=self.LOGIN)
            res = advisor.advise(self.stats, [], self.trades, allow_call=True, login=self.LOGIN)
        self.assertEqual(fake.call_count, 1)   # 第二次不再请求
        self.assertTrue(res["cached"])

    def test_failed_call_is_logged(self):
        with mock.patch("agent.llm.chat_ex", side_effect=RuntimeError("boom")):
            res = advisor.advise(self.stats, [], self.trades, allow_call=True, login=self.LOGIN)
        self.assertFalse(res["ok"])
        row = json.loads(self._log_path().read_text(encoding="utf-8").strip())
        self.assertFalse(row["ok"])
        self.assertIn("boom", row["error"])

    def test_summary_counts_calls(self):
        payload = "```json\n" + json.dumps(ADVICE_JSON, ensure_ascii=False) + "\n```"
        with mock.patch("agent.llm.chat_ex", return_value=(payload, {"total_tokens": 50})):
            advisor.advise(self.stats, [], self.trades, allow_call=True, login=self.LOGIN)
        s = advisor.api_call_summary(self.LOGIN)
        self.assertEqual(s["calls"], 1)
        self.assertEqual(s["total_tokens"], 50)



# ---------------------------------------------------------------- 交易评分
from live.profile import (
    breakeven_win_rate,
    cost_hurdle,
    exante_table,
    exante_win_rate,
    load_profile,
)
from live.scoring import (
    TRADE_WEIGHTS,
    score_account,
    score_trade,
    score_trades,
)
from live.advisor import build_prompt


def _st(**kw):
    base = {
        "empty": False, "count": 76, "expectancy_r": -0.216, "risk_unit": 7.26,
        "risk_reward": 0.75, "win_rate": 44.7,
        "max_dd_pct": 100.0, "peak_balance": 71.21, "trough_balance": 0.0,
        "wipeouts": 2, "by_reason": [{"key": "手动平仓", "trades": 41}],
        "balance_curve": [{"ts": T0 + i * 600, "balance": 1000.0 - i * 10} for i in range(60)],
    }
    base.update(kw)
    return base


class TestBreakevenMath(unittest.TestCase):
    """盈亏平衡线是评分里唯一不需要外部基准的东西，必须算准。"""

    def test_posterior_formula(self):
        # 期望 = p*rr - (1-p) = 0 → p = 1/(1+rr)
        self.assertAlmostEqual(breakeven_win_rate(1.0), 50.0)
        self.assertAlmostEqual(breakeven_win_rate(0.75), 57.14, places=1)
        self.assertAlmostEqual(breakeven_win_rate(2.0), 33.33, places=1)
        self.assertAlmostEqual(breakeven_win_rate(3.0), 25.0)

    def test_posterior_handles_bad_input(self):
        self.assertIsNone(breakeven_win_rate(None))
        self.assertIsNone(breakeven_win_rate(0))
        self.assertIsNone(breakeven_win_rate(-1))

    def test_exante_includes_spread(self):
        # SL 100, TP 200, 点差 20 → (100+20)/(200+100) = 40%
        self.assertAlmostEqual(exante_win_rate(100, 200, 20), 40.0)
        # 点差为 0 时退化为 1/(1+rr)
        self.assertAlmostEqual(exante_win_rate(100, 100, 0), 50.0)

    def test_exante_rejects_bad_input(self):
        self.assertIsNone(exante_win_rate(0, 100, 20))
        self.assertIsNone(exante_win_rate(100, 0, 20))

    def test_exante_table_monotonic(self):
        rows = exante_table({"spread_points": 240, "atr_points": 1866}, rr_list=(0.5, 1.0, 2.0))
        needs = [r["required_win_rate"] for r in rows]
        self.assertEqual(needs, sorted(needs, reverse=True))

    def test_cost_hurdle_ratios(self):
        h = cost_hurdle({"spread_points": 240, "range_points": 1743, "atr_points": 1866, "point": 0.001})
        self.assertAlmostEqual(h["spread_over_range_pct"], 13.8, places=1)
        self.assertAlmostEqual(h["spread_over_atr_pct"], 12.9, places=1)
        self.assertAlmostEqual(h["spread_usd_per_lot"], 24.0, places=2)


class TestProfile(unittest.TestCase):
    def test_defaults_present(self):
        p = load_profile()
        for k in ("symbol", "timeframe", "spread_points", "atr_points"):
            self.assertIn(k, p)


class TestTradeScore(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(TRADE_WEIGHTS.values()), 100)

    def test_take_profit_beats_manual(self):
        tp = score_trade({"profit": 20.0, "close_reason": "tp", "entry_time": T0,
                          "exit_time": T0 + 3600}, 10.0, 1000.0)
        manual = score_trade({"profit": 20.0, "close_reason": "manual", "entry_time": T0,
                              "exit_time": T0 + 3600}, 10.0, 1000.0)
        self.assertGreater(tp["dims"]["discipline"], manual["dims"]["discipline"])
        self.assertGreater(tp["score"], manual["score"])

    def test_stop_out_is_penalised(self):
        t = score_trade({"profit": -80.0, "close_reason": "stop_out", "entry_time": T0,
                         "exit_time": T0 + 3600}, 10.0, 100.0)
        self.assertEqual(t["dims"]["discipline"], 0)
        self.assertEqual(t["dims"]["risk"], 0)
        self.assertIn("穿仓强平", t["flags"])
        self.assertEqual(t["grade"], "F")

    def test_short_hold_flagged(self):
        t = score_trade({"profit": 5.0, "close_reason": "tp", "entry_time": T0,
                         "exit_time": T0 + 30}, 10.0, 1000.0)
        self.assertEqual(t["dims"]["duration"], 0)
        self.assertIn("持仓不足 1 分钟", t["flags"])

    def test_planned_rr_from_snapshot(self):
        plan = {"price_open": 4000.0, "sl": 3990.0, "tp": 4020.0}
        t = score_trade({"profit": 20.0, "close_reason": "tp", "entry_time": T0,
                         "exit_time": T0 + 600}, 10.0, 1000.0, plan=plan)
        self.assertAlmostEqual(t["planned_rr"], 2.0)

    def test_manual_exit_with_plan_flagged(self):
        plan = {"price_open": 4000.0, "sl": 3990.0, "tp": 4020.0}
        t = score_trade({"profit": 8.0, "close_reason": "manual", "entry_time": T0,
                         "exit_time": T0 + 600}, 10.0, 1000.0, plan=plan)
        self.assertTrue(any("未按计划" in f for f in t["flags"]))

    def test_no_plan_flagged(self):
        t = score_trade({"profit": 8.0, "close_reason": "tp", "entry_time": T0,
                         "exit_time": T0 + 600}, 10.0, 1000.0)
        self.assertIsNone(t["planned_rr"])
        self.assertIn("无计划SL/TP记录", t["flags"])

    def test_score_within_bounds(self):
        for profit, reason in ((100, "tp"), (-100, "stop_out"), (0, "manual"), (-5, "sl")):
            t = score_trade({"profit": profit, "close_reason": reason,
                             "entry_time": T0, "exit_time": T0 + 600}, 10.0, 1000.0)
            self.assertGreaterEqual(t["score"], 0)
            self.assertLessEqual(t["score"], 100)


class TestAccountScore(unittest.TestCase):
    def test_margin_dimension_computed(self):
        # 盈亏比 0.75 → 需 57.1%；胜率 44.7% → 边际 -12.4pp
        res = score_account(_st(), {"windows": []}, [])
        be = res["breakeven"]
        self.assertAlmostEqual(be["required_win_rate"], 57.1, places=1)
        self.assertAlmostEqual(be["margin_pp"], -12.4, places=1)
        margin = next(d for d in res["dimensions"] if d["key"] == "margin")
        self.assertEqual(margin["grade"], "F")

    def test_margin_positive_grades_high(self):
        res = score_account(_st(risk_reward=2.0, win_rate=50.0), {"windows": []}, [])
        margin = next(d for d in res["dimensions"] if d["key"] == "margin")
        # 需 33.3%，实际 50% → +16.7pp → A
        self.assertEqual(margin["grade"], "A")
        self.assertAlmostEqual(res["breakeven"]["margin_pp"], 16.7, places=1)

    def test_red_flag_suggests_required_rr(self):
        res = score_account(_st(), {"windows": []}, [])
        self.assertTrue(any("平衡线" in f for f in res["red_flags"]))

    def test_wipeout_forces_f(self):
        res = score_account(_st(wipeouts=2, max_dd_pct=100.0), {"windows": []}, [])
        self.assertEqual(res["grade"], "F")
        risk = next(d for d in res["dimensions"] if d["key"] == "risk")
        self.assertEqual(risk["grade"], "F")

    def test_grade_is_worst_dimension_not_average(self):
        stats = _st(expectancy_r=0.30, risk_reward=3.0, win_rate=60.0,
                    max_dd_pct=60.0, wipeouts=1)
        res = score_account(stats, {"windows": []}, [])
        top = next(d for d in res["dimensions"] if d["key"] == "expectancy")
        self.assertEqual(top["grade"], "A")
        self.assertEqual(res["grade"], "F")

    def test_small_sample_marked_insufficient(self):
        res = score_account(_st(count=5, wipeouts=0, max_dd_pct=0,
                                by_reason=[{"key": "手动平仓", "trades": 0}]),
                            {"windows": []}, [])
        self.assertEqual(res["grade"], "insufficient")

    def test_six_dimensions_and_order(self):
        res = score_account(_st(), {"windows": []}, [])
        keys = [d["key"] for d in res["dimensions"]]
        self.assertEqual(keys, ["expectancy", "margin", "stability", "risk", "discipline", "sample"])

    def test_empty_stats(self):
        res = score_account({"empty": True}, {}, [])
        self.assertEqual(res["grade"], "insufficient")
        self.assertIsNone(res["score"])
        self.assertIsNone(res["breakeven"])

    def test_plan_stats_reported(self):
        trades = [{"position_id": 1, "symbol": "X", "direction": 1, "profit": 20.0,
                   "entry_time": T0, "exit_time": T0 + 600, "close_reason": "tp"}]
        plans = {"1": {"price_open": 4000.0, "sl": 3990.0, "tp": 4020.0}}
        res = score_account(_st(count=1, by_reason=[]), {"windows": []}, trades, plans=plans)
        self.assertEqual(res["plan"]["with_plan"], 1)
        self.assertAlmostEqual(res["plan"]["avg_planned_rr"], 2.0)

    def test_plan_note_when_missing(self):
        res = score_account(_st(), {"windows": []}, [])
        self.assertEqual(res["plan"]["with_plan"], 0)
        self.assertIn("还没有开仓时的 SL/TP", res["plan"]["note"])


class TestScoreTrades(unittest.TestCase):
    def test_uses_risk_unit_and_balance(self):
        trades = [
            {"position_id": 1, "symbol": "XAUUSDm", "direction": 1, "profit": -20.0,
             "entry_time": T0, "exit_time": T0 + 600, "close_reason": "sl"},
        ]
        stats = _st(risk_unit=10.0, balance_curve=[{"ts": T0 + 600, "balance": 980.0}])
        rows = score_trades(trades, stats)
        self.assertAlmostEqual(rows[0]["r"], -2.0, places=2)
        self.assertAlmostEqual(rows[0]["risk_pct"], 2.04, places=1)

    def test_limit_returns_latest(self):
        trades = [{"position_id": i, "symbol": "X", "direction": 1, "profit": 1.0,
                   "entry_time": T0 + i * 100, "exit_time": T0 + i * 100 + 60,
                   "close_reason": "tp"} for i in range(30)]
        rows = score_trades(trades, _st(risk_unit=1.0, balance_curve=[]), limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["position_id"], 29)


class TestAdvicePrompt(unittest.TestCase):
    def setUp(self):
        self.trades = [_q(20.0 if i % 2 == 0 else -10.0, i) for i in range(30)]
        self.stats = stats_compute(self.trades, {"balance": 1000}, 1000.0)
        self.scoring = score_account(self.stats, {"windows": []}, self.trades)

    def test_prompt_contains_breakeven(self):
        p = build_prompt(self.stats, [], self.trades, self.scoring)
        self.assertIn("规则引擎给出的评级", p)
        self.assertIn("盈亏平衡线", p)
        self.assertIn(self.scoring["grade"], p)

    def test_prompt_without_scoring_still_valid(self):
        p = build_prompt(self.stats, [], self.trades, None)
        self.assertIn("没有评分数据", p)
        self.assertIn("总体统计", p)


# ---------------------------------------------------------------- 多账户隔离

def _deal(ticket, login, profit=0.0, symbol="XAUUSDm", type_=2, time_=0, position_id=0):
    return {
        "ticket": ticket, "order": ticket, "position_id": position_id, "symbol": symbol,
        "type": type_, "entry": 0, "volume": 0.01, "price": 100.0, "profit": profit,
        "commission": 0.0, "swap": 0.0, "fee": 0.0, "time": time_ or ticket,
        "comment": "", "magic": 0, "login": login,
    }


class TestMultiAccountIsolation(unittest.TestCase):
    """切账户后两个账本必须完全隔离。

    混在一起的后果不是「数据多一点」，而是净额 / 胜率 / 回撤 / 入金全错，
    而且没有账户号就再也拆不开了。
    """

    A, B = "111001", "222002"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        paths.set_login(None)

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_account_gets_its_own_directory(self):
        self.assertEqual(paths.account_dir(self.A), self.tmp / "accounts" / self.A)
        self.assertEqual(paths.account_file(self.A, "deals.jsonl").name, "deals.jsonl")
        self.assertNotEqual(paths.account_dir(self.A), paths.account_dir(self.B))

    def test_ledger_does_not_leak_across_accounts(self):
        from live.journal import _append_ledger, load_ledger

        _append_ledger([_deal(1, int(self.A)), _deal(2, int(self.A))], self.A)
        _append_ledger([_deal(3, int(self.B))], self.B)

        self.assertEqual(sorted(load_ledger(self.A)), [1, 2])
        self.assertEqual(sorted(load_ledger(self.B)), [3])

    def test_mixed_file_is_filtered_by_login(self):
        """即使文件里物理混了别的账户的记录（旧版遗留），读的时候也要过滤掉。"""
        from live.journal import load_ledger

        p = paths.account_file(self.A, "deals.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for d in (_deal(1, int(self.A)), _deal(2, int(self.B)), _deal(3, int(self.A))):
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")

        self.assertEqual(sorted(load_ledger(self.A)), [1, 3])

    def test_records_without_login_are_adopted_by_their_folder(self):
        """最老的记录没有 login 字段。既然躺在这个账户目录里，就认这个账户的。"""
        from live.journal import load_ledger

        p = paths.account_file(self.A, "deals.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        legacy = _deal(7, int(self.A))
        legacy.pop("login")
        p.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(sorted(load_ledger(self.A)), [7])

    def test_position_snapshots_are_isolated(self):
        from live.positions import initial_plans, record_positions

        record_positions([{"position_id": 1, "symbol": "XAUUSDm", "sl": 1.0, "tp": 2.0,
                           "volume": 0.01, "price_open": 1.5}], now=100, login=self.A)
        record_positions([{"position_id": 2, "symbol": "XAUUSDm", "sl": 3.0, "tp": 4.0,
                           "volume": 0.02, "price_open": 3.5}], now=100, login=self.B)

        self.assertEqual(list(initial_plans(self.A)), ["1"])
        self.assertEqual(list(initial_plans(self.B)), ["2"])
        self.assertEqual(initial_plans(self.A)["1"]["sl"], 1.0)

    def test_switching_account_does_not_mark_other_positions_closed(self):
        """切到 B 之后，A 的持仓不能被误判成「已平仓」。"""
        from live.positions import load_snapshots, record_positions

        pa = [{"position_id": 1, "symbol": "XAUUSDm", "sl": 1.0, "tp": 2.0,
               "volume": 0.01, "price_open": 1.5}]
        record_positions(pa, now=100, login=self.A)
        record_positions([], now=200, login=self.B)      # B 没有持仓
        record_positions(pa, now=300, login=self.A)      # A 的还在

        self.assertEqual([r["event"] for r in load_snapshots(self.A)], ["open"])
        self.assertEqual(load_snapshots(self.B), [])

    def test_advice_cache_does_not_leak_across_accounts(self):
        """A 的缓存不能让 B 「看起来已经复盘过了」——那是别人的结论。"""
        from live import advisor

        paths.write_json(self.A, "advice.json", {"key": "30:29", "advice": {"headline": "A 的结论"},
                                                 "generated_at": 1})
        self.assertIsNone(advisor.load_cached(self.B))
        self.assertEqual(advisor.load_cached(self.A)["advice"]["headline"], "A 的结论")

    def test_api_call_summary_aggregates_all_accounts(self):
        from live import advisor

        paths.account_dir(self.A).mkdir(parents=True, exist_ok=True)
        paths.account_dir(self.B).mkdir(parents=True, exist_ok=True)
        advisor._log_call(True, {"total_tokens": 10}, login=self.A)
        advisor._log_call(True, {"total_tokens": 25}, login=self.B)

        self.assertEqual(advisor.api_call_summary(self.A)["total_tokens"], 10)
        self.assertEqual(advisor.api_call_summary()["calls"], 2)
        self.assertEqual(advisor.api_call_summary()["total_tokens"], 35)

    def test_index_tracks_accounts_by_recency(self):
        paths.touch_account({"login": int(self.A), "server": "S1", "balance": 10.0})
        paths.touch_account({"login": int(self.B), "server": "S2", "balance": 20.0})
        rows = paths.list_accounts()
        self.assertEqual([r["login"] for r in rows], [int(self.B), int(self.A)])
        # 重复 touch 不新增条目，只更新最后活跃
        paths.touch_account({"login": int(self.A), "server": "S1", "balance": 11.0})
        self.assertEqual(len(paths.list_accounts()), 2)
        self.assertEqual(next(r for r in paths.list_accounts() if str(r["login"]) == self.A)["last_balance"], 11.0)

    def test_resolve_login_falls_back_to_most_recent(self):
        paths.touch_account({"login": int(self.A), "server": "S1"})
        paths.set_login(self.B)
        self.assertEqual(paths.resolve_login(), self.B)
        paths.set_login(None)
        self.assertEqual(paths.resolve_login(), self.A)


class TestOfflineArchive(unittest.TestCase):
    """归档账户：不连 MT5 也要能看完整统计。"""

    LOGIN = "333003"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        from live.journal import _append_ledger

        # 3 笔：+20 / -10 / +5，外加一笔 100 的入金
        deals = [
            _deal(1, int(self.LOGIN), profit=100.0, time_=1000),
            _deal(2, int(self.LOGIN), profit=0.0, type_=1, time_=1100, position_id=500),
            _deal(3, int(self.LOGIN), profit=20.0, type_=0, time_=1200, position_id=500),
            _deal(4, int(self.LOGIN), profit=0.0, type_=1, time_=1300, position_id=501),
            _deal(5, int(self.LOGIN), profit=-10.0, type_=0, time_=1400, position_id=501),
        ]
        for d in deals:
            d["entry"] = 0 if d["type"] == 1 else 1
        # type 1 = sell(in), type 0 = buy(out) —— 用 MT5 的语义重建多空
        deals[1].update({"type": 1, "entry": 0, "position_id": 500, "profit": 0.0})
        deals[2].update({"type": 0, "entry": 1, "position_id": 500, "profit": 20.0})
        deals[3].update({"type": 0, "entry": 0, "position_id": 501, "profit": 0.0})
        deals[4].update({"type": 1, "entry": 1, "position_id": 501, "profit": -10.0})
        _append_ledger(deals, self.LOGIN)

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_reads_without_mt5(self):
        from live.journal import collect_offline

        info = collect_offline(self.LOGIN)
        self.assertTrue(info["ok"])
        self.assertTrue(info["offline"])
        self.assertEqual(info["positions"], [])
        self.assertEqual(len(info["trades"]), 2)
        self.assertAlmostEqual(info["deposits"], 100.0, places=2)

    def test_offline_reconstructs_balance_from_ledger(self):
        """account.json 被后来的账户覆盖时，余额只能从成交反推。"""
        from live.journal import _balance_from_ledger, collect_offline, load_ledger

        info = collect_offline(self.LOGIN)
        expect = _balance_from_ledger(load_ledger(self.LOGIN))
        self.assertAlmostEqual(info["account"]["balance"], expect, places=2)
        self.assertAlmostEqual(expect, 110.0, places=2)   # 100 入金 +20 -10

    def test_offline_uses_last_trade_time_as_now(self):
        """历史账户的「近 7 天」必须是相对它最后一笔，否则窗口永远空。"""
        from live.journal import collect_offline

        info = collect_offline(self.LOGIN)
        q = info["quality"]
        self.assertFalse(q.get("empty"), q.get("error"))

    def test_unknown_account_reports_empty_not_crash(self):
        from live.journal import collect_offline

        info = collect_offline("999999")
        self.assertFalse(info["ok"])
        self.assertIn("没有已归档", info["error"])


class TestLedgerSplit(unittest.TestCase):
    """拆分规则：终端能读到的 ticket 归当前账户，其余归历史账户。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_split_assigns_every_deal_exactly_once(self):
        from tools.migrate_accounts import rebuild_daily

        cur, other = "444004", "555005"
        current_tickets = {10, 11}
        legacy = [_deal(i, 0) for i in (10, 11, 20, 21, 22)]

        mine = [d for d in legacy if d["ticket"] in current_tickets]
        others = [d for d in legacy if d["ticket"] not in current_tickets]
        self.assertEqual([d["ticket"] for d in mine], [10, 11])
        self.assertEqual(len(others), 3)
        self.assertEqual(len(mine) + len(others), len(legacy))

        for login, rows in ((cur, mine), (other, others)):
            with paths.account_file(login, "deals.jsonl").open("w", encoding="utf-8") as fh:
                for d in rows:
                    fh.write(json.dumps({**d, "login": int(login)}, ensure_ascii=False) + "\n")
            rebuild_daily(login, [], 0.0)

        for login, n in ((cur, 2), (other, 3)):
            p = paths.account_file(login, "deals.jsonl")
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(rows), n)
            self.assertTrue(all(r["login"] == int(login) for r in rows))


class TestMT5FastFail(unittest.TestCase):
    """MT5 不在时必须**立刻**报错。

    mt5.initialize() 会阻塞整个进程 60 秒（MT5 内部最小超时就是 60s，
    timeout 参数调小无效）。面板是 HTTP 服务，被冻 60 秒等于整个页面假死，
    所以要先用进程列表挡一道。
    """

    def setUp(self):
        from live import journal

        self.journal = journal
        self._saved = (journal._connected, journal._conn_failed_at)
        journal._connected = False
        journal._conn_failed_at = 0.0

    def tearDown(self):
        self.journal._connected, self.journal._conn_failed_at = self._saved

    def test_missing_terminal_raises_without_touching_mt5(self):
        with mock.patch.object(self.journal, "_terminal_alive", return_value=False), \
             mock.patch("MetaTrader5.initialize") as init:
            with self.assertRaises(RuntimeError) as ctx:
                self.journal._ensure()
        self.assertIn("未运行", str(ctx.exception))
        init.assert_not_called()          # 关键：根本不去调那个会冻 60 秒的函数

    def test_terminal_check_does_not_depend_on_console_encoding(self):
        """中文 Windows 上 tasklist 是 GBK 输出，按文本解码会抛异常并把 stdout 吞掉，
        结果变成「终端开着也一律判成没开」。这里按字节匹配。"""
        import subprocess

        def fake_run(*a, **kw):
            self.assertNotIn("text", kw)
            self.assertTrue(kw.get("capture_output"))
            return subprocess.CompletedProcess(a, 0, b"\xd0\xc5\xcf\xa2 terminal64.exe 1234 Console\r\n")

        with mock.patch.object(subprocess, "run", fake_run):
            self.assertTrue(self.journal._terminal_alive())

    def test_terminal_check_returns_false_on_empty_listing(self):
        import subprocess

        with mock.patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, b"INFO: No tasks are running.\r\n"),
        ):
            self.assertFalse(self.journal._terminal_alive())

    def test_terminal_check_fails_open_when_tasklist_unavailable(self):
        """查不出来就别拦着，交给 initialize 自己判断——宁可慢一次，不要误判成没开。"""
        import subprocess

        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("tasklist")):
            self.assertTrue(self.journal._terminal_alive())

    def test_initialize_failure_is_remembered(self):
        """终端在但连不上时，退避期内不要反复去等那 60 秒。"""
        with mock.patch.object(self.journal, "_terminal_alive", return_value=True), \
             mock.patch("MetaTrader5.initialize", return_value=False), \
             mock.patch("MetaTrader5.last_error", return_value=(-1, "boom")):
            with self.assertRaises(RuntimeError):
                self.journal._ensure()
            self.assertGreater(self.journal._conn_failed_at, 0)

            with mock.patch("MetaTrader5.initialize") as init2:
                with self.assertRaises(RuntimeError) as ctx:
                    self.journal._ensure()
            init2.assert_not_called()     # 退避生效
            self.assertIn("刚失败过", str(ctx.exception))


class TestTerminalAutostart(unittest.TestCase):
    """MT5 没开时，能不能把它拉起来。

    能不能取决于**本进程从哪来**：从资源管理器（双击 bat）起来的进程树可以，
    从 agent 进程树里拉会 0xC0000139。所以自动启动只该在面板自己跑着时生效，
    而且必须由后台线程调，不能挡住 HTTP。
    """

    def setUp(self):
        from live import journal

        self.journal = journal
        self._saved = {
            "_connected": journal._connected,
            "_conn_failed_at": journal._conn_failed_at,
            "_launch_failed_at": journal._launch_failed_at,
            "_last_launch_ok": journal._last_launch_ok,
        }
        journal._connected = False
        journal._conn_failed_at = 0.0
        journal._launch_failed_at = 0.0
        journal._last_launch_ok = None

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.journal, k, v)

    def test_no_launch_when_terminal_already_running(self):
        with mock.patch.object(self.journal, "_terminal_alive", return_value=True), \
             mock.patch.object(self.journal, "_start_terminal") as start:
            self.assertTrue(self.journal.ensure_terminal())
        start.assert_not_called()

    def test_start_terminal_returns_false_without_exe(self):
        with mock.patch.object(self.journal, "terminal_path", return_value=None):
            self.assertFalse(self.journal._start_terminal())

    def test_start_terminal_swallows_popen_errors(self):
        with mock.patch.object(self.journal, "terminal_path", return_value=Path("C:/nope/terminal64.exe")), \
             mock.patch("subprocess.Popen", side_effect=OSError("boom")):
            self.assertFalse(self.journal._start_terminal())

    def test_launch_failure_is_remembered(self):
        with mock.patch.object(self.journal, "_terminal_alive", return_value=False), \
             mock.patch.object(self.journal, "_start_terminal", return_value=False) as start:
            self.assertFalse(self.journal.ensure_terminal(timeout=0.1))
            self.assertFalse(self.journal.ensure_terminal(timeout=0.1))   # 第二次直接短路
        self.assertEqual(start.call_count, 1)

    def test_collect_refuses_to_launch_unless_allowed(self):
        """HTTP 路径永远不许触发启动——那会阻塞，页面会假死。"""
        with mock.patch.object(self.journal, "_terminal_alive", return_value=False), \
             mock.patch.object(self.journal, "_start_terminal") as start:
            info = self.journal.collect(force=True)
        self.assertFalse(info["ok"])
        self.assertIn("终端未运行", info["error"])
        start.assert_not_called()

    def test_collect_can_launch_when_explicitly_allowed(self):
        with mock.patch.object(self.journal, "_terminal_alive", return_value=False), \
             mock.patch.object(self.journal, "ensure_terminal", return_value=False) as ensure:
            info = self.journal.collect(force=True, allow_launch=True)
        ensure.assert_called_once()
        self.assertFalse(info["ok"])
