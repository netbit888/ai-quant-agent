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

from live.autotrade import Cfg, _exit_gate, exit_params, validate_cfg
from tools.replay_exit import (_atr_at, replay_one, run_grid, spec_of, summarize)


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


class TestStaleGate(unittest.TestCase):
    """峰值停滞唤醒（2026-10-29）在闸门这一侧的契约。

    要钉住的是两件事：
      1. stale_ready=True 时**只放行不改变结论**——返 None（交模型），
         绝不能返 {"close": True}，否则就变成"程序在停滞时直接平仓"，
         那是另一条规则，不是这次要的东西；
      2. 它必须排在 sl 和 timeout 之后。反过来的话，一笔已经该止损的单子
         会因为"峰值停滞"被推给模型，止损被稀释成一次咨询。
    """

    def test_stale_defers_to_model_instead_of_tp_wait(self):
        d = _exit_gate(Cfg(sl_usd=3.0, tp_min_usd=3.25), 600, 1.85,
                       stale_ready=True)
        self.assertIsNone(d)

    def test_without_stale_flag_it_still_waits(self):
        """同一个浮盈、同一个时点，不开 stale 就是老行为——这是回归线。"""
        d = _exit_gate(Cfg(sl_usd=3.0, tp_min_usd=3.25), 600, 1.85,
                       stale_ready=False)
        self.assertEqual(d["source"], "tp_wait")
        self.assertFalse(d["close"])

    def test_stop_loss_still_wins_over_stale(self):
        d = _exit_gate(Cfg(sl_usd=3.0, tp_min_usd=3.25), 600, -3.2,
                       stale_ready=True)
        self.assertEqual(d["source"], "sl")
        self.assertTrue(d["close"])

    def test_timeout_still_wins_over_stale(self):
        d = _exit_gate(Cfg(sl_usd=3.0, tp_min_usd=3.25, max_hold_sec=3600),
                       3660, 1.0, stale_ready=True)
        self.assertEqual(d["source"], "timeout")
        self.assertTrue(d["close"])

    def test_stale_at_or_above_tp_is_moot(self):
        """浮盈已经到门槛：本来就该交模型，stale 加不加都是 None。"""
        self.assertIsNone(_exit_gate(Cfg(tp_min_usd=3.25), 600, 3.30,
                                     stale_ready=True))


class TestPlanExitParams(unittest.TestCase):
    """分 regime 的出场参数（2026-10-28「先判行情，再选打法」）。

    依据：85 个真实决策点上，tp 3.25 把固定持 10 根 +12.6 的信号变成 -5.0；
    6×6 网格里 tp≥4.5 全部转正。止损宽度没有可靠结论（去重后单调性消失），
    所以 sl 不动，只有 tp 和持仓上限分 plan。
    """

    def test_trend_plan_uses_trend_params(self):
        ep = exit_params(Cfg(), "trend")
        self.assertEqual(ep["tp_min_usd"], 7.0)
        self.assertEqual(ep["max_hold_sec"], 10800)
        self.assertEqual(ep["sl_usd"], 3.0)   # 止损不分 plan

    def test_range_plan_unchanged(self):
        cfg = Cfg()
        ep = exit_params(cfg, "range")
        self.assertEqual(ep["tp_min_usd"], cfg.tp_min_usd)
        self.assertEqual(ep["max_hold_sec"], cfg.max_hold_sec)

    def test_mid_plan_uses_range_params(self):
        ep = exit_params(Cfg(), "mid")
        self.assertEqual(ep["tp_min_usd"], 3.25)

    def test_none_plan_defaults_to_range(self):
        """旧 state 没有 plan 字段：按震荡参数管，行为与改前完全一致。"""
        ep = exit_params(Cfg(), None)
        self.assertEqual(ep["tp_min_usd"], 3.25)
        self.assertEqual(ep["max_hold_sec"], 3600)

    def test_tp_min_trend_zero_falls_back(self):
        """tp_min_trend=0 关掉趋势宽止盈，趋势单退回老门槛。"""
        ep = exit_params(Cfg(tp_min_trend=0.0), "trend")
        self.assertEqual(ep["tp_min_usd"], 3.25)

    def test_trend_gate_tp_is_wider(self):
        """趋势单浮盈 4.0：震荡门槛（3.25）早该问模型，趋势门槛（7.0）继续拿。"""
        cfg = Cfg()
        d_range = _exit_gate(cfg, 600, 4.0, plan="range")
        self.assertIsNone(d_range)              # 到 3.25 了 → 交给模型
        d_trend = _exit_gate(cfg, 600, 4.0, plan="trend")
        self.assertFalse(d_trend["close"])      # 没到 7.0 → 继续持有
        self.assertEqual(d_trend["source"], "tp_wait")

    def test_trend_gate_timeout_is_longer(self):
        """趋势单持仓上限 3 小时：震荡单 61 分钟就该平，趋势单还拿着。"""
        cfg = Cfg()
        self.assertEqual(_exit_gate(cfg, 3660, 1.0, plan="range")["source"], "timeout")
        d = _exit_gate(cfg, 3660, 1.0, plan="trend")
        self.assertFalse(d["close"])

    def test_trend_gate_stop_same_as_range(self):
        """止损不分 plan：趋势单同样 -3.0 平。"""
        d = _exit_gate(Cfg(), 600, -3.1, plan="trend")
        self.assertEqual(d["source"], "sl")

    def test_trend_gate_stop_still_before_tp(self):
        """plan 分支不许破坏顺序：趋势单浮亏过线时，止盈门槛再宽也得先平。"""
        d = _exit_gate(Cfg(), 600, -3.1, plan="trend")
        self.assertTrue(d["close"])
        self.assertEqual(d["source"], "sl")


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

    def test_timeout_settles_at_window_end_not_dataset_end(self):
        """老 bug：超时结算取的是整段数据最后一根（可能是几小时之后的价格）。
        max_bars 截断后必须取第 max_bars 根，否则长持仓全按末价结算。"""
        bars = [{"time": i, "open": 4000, "high": 4000.5, "low": 3999.5,
                 "close": 4000.0} for i in range(10)]
        bars[-1]["close"] = 4100.0          # 数据末尾远在未来
        pnl, kind, n = replay_one(bars, 0, 4000.0, 1, 3.0, 3.25,
                                  self.spread, self.unit, max_bars=3)
        self.assertEqual((pnl, kind, n), (0.0, "timeout", 3))

    # --- 峰值回吐保护（2026-10-29）----------------------------------------

    def _gb_bars(self):
        """先冲高到 +2.0，再回落收盘到 +1.1。"""
        return [
            {"time": 1, "open": 4000, "high": 4002.0, "low": 4000.5, "close": 4001.8},
            {"time": 2, "open": 4001.8, "high": 4002.1, "low": 4001.0, "close": 4001.1},
        ]

    def test_giveback_exits_when_peak_fades(self):
        """峰值 2.09、回撤 40% 线 1.25：收盘 1.1 破线 → 按 1.25 结算。"""
        pnl, kind, n = replay_one(self._gb_bars(), 0, 4000.0, 1, 3.0, 4.0,
                                  self.spread, self.unit, arm=1.5, frac=0.4)
        self.assertEqual(kind, "giveback")
        self.assertEqual(n, 2)
        self.assertAlmostEqual(pnl, 2.1 * 0.6, places=2)

    def test_giveback_off_keeps_old_result(self):
        """不给 arm/frac 就是老行为——这是"新规则是否真的生效"的对照线。"""
        pnl, kind, _ = replay_one(self._gb_bars(), 0, 4000.0, 1, 3.0, 4.0,
                                  self.spread, self.unit)
        self.assertEqual((pnl, kind), (1.1, "timeout"))

    def test_giveback_never_steals_a_take_profit(self):
        """同一根里既冲到 tp 又回落：按 tp 算。否则新规则会把大赢单
        系统性改写成小赢，用回放工具自己制造一个更差的规则。"""
        bars = [{"time": 1, "open": 4000, "high": 4004.2, "low": 4001.0, "close": 4001.2}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, 1, 3.0, 4.0,
                                  self.spread, self.unit, arm=1.5, frac=0.4)
        self.assertEqual((pnl, kind), (4.0, "tp"))

    def test_giveback_does_not_arm_below_arm_line(self):
        """峰值只有 1.0，没到武装线 1.5：回吐也不管。"""
        bars = [{"time": 1, "open": 4000, "high": 4001.0, "low": 3999.9, "close": 4000.1}]
        pnl, kind, _ = replay_one(bars, 0, 4000.0, 1, 3.0, 4.0,
                                  self.spread, self.unit, arm=1.5, frac=0.4)
        self.assertEqual(kind, "timeout")

    # --- ATR 归一化 --------------------------------------------------------

    def test_atr_matches_live_formula(self):
        """回放的 ATR 和 live 的 _atr 必须同源：两套算法各写一份，
        算出来的 k 用到生产上就是错位。"""
        from live.autotrade import _atr
        rows = [{"time": i, "open": 4000 + i, "high": 4001 + i, "low": 3999.5 + i,
                 "close": 4000.5 + i} for i in range(14)]
        self.assertAlmostEqual(_atr_at(rows, 14, 14), _atr(rows)["atr"], places=9)

    def test_atr_needs_no_future_bars(self):
        """窗口必须只看入场那一刻之前的 14 根；把后面的价格改掉，结果不能变。"""
        rows = [{"time": i, "open": 4000, "high": 4001 + (i % 3), "low": 3999,
                 "close": 4000} for i in range(30)]
        a = _atr_at(rows, 20, 14)
        rows2 = [dict(r) for r in rows]
        for r in rows2[20:]:
            r["high"] += 50
        self.assertAlmostEqual(a, _atr_at(rows2, 20, 14), places=9)

    def test_atr_mode_uses_each_trade_own_atr(self):
        """atr 模式：门槛按每笔自己的 ATR 算，行名要能看出是 ×ATR 而不是固定数。"""
        bars = []
        for i in range(80):
            amp = 0.5 if i < 40 else 2.0      # 前段波动小、后段大
            bars.append({"time": 1000 + i * 60, "open": 4000,
                         "high": 4000 + amp, "low": 4000 - amp,
                         "close": 4000 + amp * 0.3})
        trades = [
            {"ticket": 1, "ts": 1000 + 10 * 60, "price_open": 4000.0,
             "direction": 1, "lots": 0.01, "actual_pnl": 0.0, "actual_held": 60},
            {"ticket": 2, "ts": 1000 + 50 * 60, "price_open": 4000.0,
             "direction": 1, "lots": 0.01, "actual_pnl": 0.0, "actual_held": 60},
        ]
        rows = run_grid(trades, bars, "XAUUSDm", [3.0], [0.0],
                        mode="atr", atr_k_list=[1.0], atr_floor=0.4, atr_cap=3.25)
        atr_rows = [r for r in rows if "×ATR" in r["name"]]
        self.assertEqual(len(atr_rows), 1)
        self.assertEqual(atr_rows[0]["n"], 2)
        self.assertIn("mfe", atr_rows[0])     # 每笔都带回了峰值，能看 MFE

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
