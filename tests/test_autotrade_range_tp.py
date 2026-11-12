"""震荡单开仓即挂券商端硬止盈（2026-11-11，2026-11-12 改单请求）。

要钉死的六件事：

1. **只有非趋势单挂。** 304 笔反事实：震荡单硬 tp@3.25 是 +24.55，趋势单是
   **-13.06**。趋势单靠右尾吃饭，卡一条 3.25 的线等于砍掉利润来源。`plan`
   为空或 `"trend"` 时必须一条线都不挂。
2. **SL 和 TP 必须是同一条请求。** 2026-11-12 实盘：开仓 10009 → 挂 SL 10009 →
   挂 TP **10024 Too many trade requests**（这个券商每秒只放两个交易请求）。
   任何一次改动把保护线拆成两次改单，这一组断言先红。
3. **价距来自 `price_dist_of`，不是金额除手数。** TP 是价格轴上的一条线，
   和手数无关；用金额算会在放大手数后把线挂到找不到的地方。
4. **止盈被拒要退回只挂止损**，`tp_err` 留痕；不许因为多要一条线把止损丢掉，
   也不许影响 `ok`。
5. **挂不上要留痕**（`tp_broker=0` + `tp_err`），趋势单不写噪音。
6. **券商端止盈被打要记成 tp，不是 external。** 否则一笔正常止盈会按
   "人工平仓"进教训库，变成一条教模型"下次该走也别走"的反向教训。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


def _ex(protect_results=None, sl_results=None):
    """记录 modify_protect / modify_sl 的调用，按序返回预设结果。

    `calls` 是**混在一起**的：断言"只发了一条改单请求"必须看总条数，
    分开记就永远测不出限流那条坑。
    """
    calls: list[tuple] = []
    protect_results = protect_results or [{"ok": True, "tp": 4103.0}]
    sl_results = sl_results or [{"ok": True, "price": 4097.0}]

    def modify_protect(ticket, sl, tp):
        calls.append(("protect", int(ticket), float(sl), float(tp)))
        return protect_results[min(len(calls) - 1, len(protect_results) - 1)]

    def modify_sl(ticket, sl):
        calls.append(("sl", int(ticket), float(sl)))
        return sl_results[min(len(calls) - 1, len(sl_results) - 1)]

    return SimpleNamespace(modify_protect=modify_protect, modify_sl=modify_sl,
                           calls=calls)


def _res(order=1234, price=4100.0, ok=True):
    return {"ok": ok, "order": order, "price": price, "retcode": 10009}


class _Base(unittest.TestCase):
    def setUp(self):
        # 价距固定 3.0，断言只看方向与入场价，不依赖合约/账户类型。
        patcher = mock.patch.object(autotrade, "price_dist_of", return_value=3.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(autotrade.time, "sleep")
        self.sleeper = sleeper.start()
        self.addCleanup(sleeper.stop)
        quote = mock.patch.object(autotrade, "_quote", return_value=(4100.0, 4100.24))
        quote.start()
        self.addCleanup(quote.stop)


class TestEntryTpPrice(_Base):
    """纯计算：该不该挂、挂在哪个价。"""

    def test_range_long_tp_above_entry(self):
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, "range")
        self.assertTrue(out["wanted"])
        self.assertAlmostEqual(out["tp"], 4103.0, places=3)
        self.assertEqual(out["skipped"], "")

    def test_range_short_tp_below_entry(self):
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), -1, "range")
        self.assertTrue(out["wanted"])
        self.assertAlmostEqual(out["tp"], 4097.0, places=3)

    def test_mid_plan_also_gets_tp(self):
        """mid（方向不明、位置明显）在 exit_params 里就按震荡参数走，同档。"""
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, "mid")
        self.assertTrue(out["wanted"])
        self.assertAlmostEqual(out["tp"], 4103.0, places=3)

    def test_trend_plan_never_gets_tp(self):
        """趋势单挂 3.25 止盈的反事实是 -13.06，一条线都不许挂。"""
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, "trend")
        self.assertFalse(out["wanted"], "趋势单不该在流水里留 tp_err 噪音")
        self.assertEqual(out["skipped"], "trend_plan")
        self.assertEqual(out["tp"], 0.0)

    def test_unknown_plan_never_gets_tp(self):
        """plan 为空只可能来自旧调用/测试：不知道是不是震荡，就不截断利润。"""
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, None)
        self.assertEqual(out["skipped"], "no_plan")
        self.assertFalse(out["wanted"])

    def test_tp_disabled_is_explicit(self):
        """tp_min_usd=0 是"这笔不要止盈门槛"的显式设定，同时也不挂 TP 单。"""
        out = autotrade._entry_tp_price(autotrade.Cfg(tp_min_usd=0.0), _res(), 1, "range")
        self.assertEqual(out["skipped"], "tp_disabled")
        self.assertFalse(out["wanted"])

    def test_dry_run_skipped(self):
        out = autotrade._entry_tp_price(autotrade.Cfg(dry_run=True), _res(), 1, "range")
        self.assertEqual(out["skipped"], "dry_run")
        self.assertTrue(out["wanted"])

    def test_no_ticket_skipped(self):
        out = autotrade._entry_tp_price(autotrade.Cfg(), _res(order=0), 1, "range")
        self.assertEqual(out["skipped"], "no_ticket")

    def test_dist_inside_spread_is_refused(self):
        """价距落在点差里 = 一开仓就触发，必须挡在挂单之前（和止损同一把尺子）。"""
        with mock.patch.object(autotrade, "price_dist_of", return_value=0.03):
            out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, "range")
        self.assertEqual(out["skipped"], "dist_within_spread")
        self.assertTrue(out["wanted"])

    def test_no_quote_is_refused(self):
        with mock.patch.object(autotrade, "_quote", return_value=None):
            out = autotrade._entry_tp_price(autotrade.Cfg(), _res(), 1, "range")
        self.assertEqual(out["skipped"], "no_quote")


class TestAttachEntryProtect(_Base):
    """一条 SLTP 请求同时挂 SL 和 TP，以及被拒时的退路。"""

    def test_single_request_carries_both_lines(self):
        """SL 和 TP 必须一条请求发完——分两步 = 第三个交易请求 = 10024。"""
        ex = _ex([{"ok": True, "price": 4097.0, "tp": 4103.0}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertTrue(out["ok"])
        self.assertEqual(len(ex.calls), 1, "只许发一条改单请求")
        self.assertEqual(ex.calls, [("protect", 1234, 4097.0, 4103.0)])
        self.assertAlmostEqual(out["tp"], 4103.0, places=3)
        self.assertAlmostEqual(out["sl"], 4097.0, places=3)
        self.assertEqual(out["tries"], 1)

    def test_no_tp_keeps_old_sl_only_path(self):
        """没有 tp（趋势单）时行为与 2026-11-09 完全一致。"""
        ex = _ex()
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertTrue(out["ok"])
        self.assertEqual(ex.calls, [("sl", 1234, 4097.0)])
        self.assertNotIn("tp", out)

    def test_tp_rejected_falls_back_to_sl_only(self):
        """止盈被整单拒 → 退回只挂止损。保护不能因为多要一条线而丢掉。"""
        ex = _ex(protect_results=[{"ok": False, "retcode": 10016,
                                   "reason": "Invalid stops"}],
                 sl_results=[{"ok": True, "price": 4097.0}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertTrue(out["ok"], "止损挂上了，这一笔就算成功")
        self.assertNotIn("tp", out)
        self.assertIn("Invalid stops", out["tp_err"])
        self.assertEqual([c[0] for c in ex.calls], ["protect", "sl"])

    def test_sl_failure_reports_reason(self):
        ex = _ex(protect_results=[{"ok": False, "reason": "方向非法"}] * 3,
                 sl_results=[{"ok": False, "reason": "方向非法"}] * 3)
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertFalse(out["ok"])
        self.assertIn("方向非法", out["reason"])
        self.assertIn("方向非法", out["tp_err"])

    def test_retries_while_position_not_ready(self):
        """刚成交时 positions_get 可能还查不到这笔，要退一步再试。"""
        ex = _ex(protect_results=[{"ok": False, "reason": "持仓不存在"},
                                  {"ok": False, "busy": True},
                                  {"ok": True, "tp": 4103.0}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["tries"], 3)
        self.assertEqual(self.sleeper.call_count, 2)

    def test_gives_up_after_three_busy(self):
        ex = _ex(protect_results=[{"ok": False, "busy": True, "reason": "终端忙"}] * 3,
                 sl_results=[{"ok": False, "busy": True, "reason": "终端忙"}] * 3)
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertFalse(out["ok"])
        self.assertIn("终端忙", out["reason"])

    def test_never_raises(self):
        def boom(*a, **kw):
            raise RuntimeError("terminal gone")

        ex = SimpleNamespace(modify_protect=boom, modify_sl=boom)
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1, tp=4103.0)
        self.assertFalse(out["ok"])
        self.assertIn("terminal gone", out["reason"])

    def test_dry_run_skipped(self):
        ex = _ex()
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(dry_run=True), _res(), 1,
                                         tp=4103.0)
        self.assertEqual(out["skipped"], "dry_run")
        self.assertEqual(ex.calls, [])


class TestSendOpenAttachesTp(TempLedger):
    """send_open 的组装：开仓 / 止损 / 止盈三份结果分开报，改单只发一条。"""

    def setUp(self):
        super().setUp()
        quote = mock.patch.object(autotrade, "_quote", return_value=(4100.0, 4100.24))
        quote.start()
        self.addCleanup(quote.stop)

    def _patch_executor(self, open_res, protect_res):
        calls: list[tuple] = []

        def modify_protect(ticket, sl, tp):
            calls.append((int(ticket), float(sl), float(tp)))
            return dict(protect_res)

        def modify_sl(ticket, sl):
            calls.append((int(ticket), float(sl)))
            return dict(protect_res)

        ex = SimpleNamespace(
            connect=lambda: True,
            open_market=lambda **kw: dict(open_res),
            modify_sl=modify_sl,
            modify_protect=modify_protect,
            shutdown=lambda: None,
        )
        return mock.patch.object(autotrade, "_executor", return_value=ex), calls

    def test_range_gets_both_lines_in_one_request(self):
        patcher, calls = self._patch_executor(
            _res(), {"ok": True, "price": 4097.0, "tp": 4103.0})
        with patcher, mock.patch.object(autotrade, "price_dist_of", return_value=3.0):
            out = autotrade.send_open(autotrade.Cfg(), 1, "range")
        self.assertTrue(out["ok"])
        self.assertTrue(out["sl_attach"]["ok"])
        self.assertTrue(out["tp_attach"]["ok"],
                        "tp_attach.ok 为假时面板和流水都看不出这条线挂上了")
        self.assertAlmostEqual(out["tp_attach"]["tp"], 4103.0, places=3)
        self.assertEqual(len(calls), 1, "开仓后只许发一条改单请求（限流）")

    def test_trend_gets_no_tp(self):
        patcher, calls = self._patch_executor(_res(), {"ok": True, "price": 4097.0})
        with patcher, mock.patch.object(autotrade, "price_dist_of", return_value=3.0):
            out = autotrade.send_open(autotrade.Cfg(), 1, "trend")
        self.assertTrue(out["sl_attach"]["ok"])
        self.assertEqual(out["tp_attach"]["skipped"], "trend_plan")
        self.assertFalse(out["tp_attach"]["wanted"])
        self.assertEqual(len(calls), 1)

    def test_open_ok_even_if_protect_fails(self):
        """改单失败不改变 ok——仓位已经真实存在。"""
        patcher, _ = self._patch_executor(_res(), {"ok": False, "reason": "终端忙"})
        with patcher, mock.patch.object(autotrade, "price_dist_of", return_value=3.0), \
                mock.patch.object(autotrade.time, "sleep"):
            out = autotrade.send_open(autotrade.Cfg(), 1, "range")
        self.assertTrue(out["ok"])
        self.assertFalse(out["sl_attach"]["ok"])
        self.assertFalse(out["tp_attach"]["ok"])
        self.assertTrue(out["tp_attach"]["wanted"])


class TestBrokerTpCloseSource(TempLedger):
    """仓在终端消失时，reason=5 + tp_broker 必须记成 tp。"""

    def _pos(self, **kw):
        base = {"ticket": 88, "direction": 1, "lots": 0.01, "price_open": 4100.0,
                "opened_at": NOW - 300, "p_up": 0.6, "plan": "range",
                "dry_run": False}
        base.update(kw)
        return base

    def _tick(self, pos, fills):
        state = autotrade._empty_state()
        state["position"] = pos
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "_close_fills", return_value=fills), \
                mock.patch.object(autotrade, "_quote", return_value=(4103.0, 4103.24)):
            return autotrade.tick(autotrade.Cfg(), now=NOW, quote=(4103.0, 4103.24),
                                  rows=bars_up(), positions=[])

    def test_tp_hit_recorded_as_tp(self):
        row = self._tick(self._pos(tp_broker=4103.0, sl_broker=4097.0),
                         {"pnl": 3.0, "price": 4103.0, "reason": 5, "time": NOW})
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "tp")
        self.assertIn("止盈", row["reason"])

    def test_manual_close_still_external(self):
        """止盈挂着但人在终端手动平了——reason 不是 5，就得记人工。"""
        row = self._tick(self._pos(tp_broker=4103.0),
                         {"pnl": 1.5, "price": 4101.5, "reason": 3, "time": NOW})
        self.assertEqual(row["source"], "external")

    def test_no_broker_tp_means_external(self):
        """没挂过券商端止盈的单子（趋势单/挂载失败），reason=5 也不该记 tp。"""
        row = self._tick(self._pos(), {"pnl": 3.0, "price": 4103.0,
                                       "reason": 5, "time": NOW})
        self.assertEqual(row["source"], "external")

    def test_tp_is_a_forced_exit_source(self):
        """券商端止盈不是模型的决定，复盘教训必须能进入场端。"""
        self.assertIn("tp", autotrade._FORCED_EXIT_SOURCES)


if __name__ == "__main__":
    unittest.main()
