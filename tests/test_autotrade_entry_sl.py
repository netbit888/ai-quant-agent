"""开仓即挂券商端硬止损（2026-11-09）。

要钉死的四件事：

1. **这个函数只管保护线的挂载，不判断该不该有止盈。** 止盈价由
   `_entry_tp_price` 算好传进来（2026-11-11 起，**只给非趋势单**），
   SL+TP 合成**同一条** SLTP 请求发出去（2026-11-12：分两步会被券商的
   「每秒两个交易请求」限流，TP 一次都挂不上）。传 None 时行为与
   2026-11-09 完全一致，只挂止损。
2. **挂止损失败不许污染开仓结果。** `ok` 只反映开仓本身；仓位已经真实
   存在，报成失败会让调用方下一轮再开一笔。
3. **挂不上要留痕**（`sl_broker=0` + `sl_err`），否则"这笔没有券商端保护"
   在流水里查不出来，亏损只能看到结果看不到保护状态。
4. **券商端 SL 被打掉要记成 sl，不是 external。** 否则一笔正常止损会按
   "人工平仓"进教训库，变成一条教模型"下次该走也别走"的反向教训——
   同样的坑 2026-10-29 在保本止损上踩过一次。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


def _ex(results):
    """按调用顺序返回结果，并记录收到的 (ticket, sl)。最后一个结果会重复。"""
    calls: list[tuple[int, float]] = []

    def modify_sl(ticket, sl):
        calls.append((int(ticket), float(sl)))
        return results[min(len(calls) - 1, len(results) - 1)]

    return SimpleNamespace(modify_sl=modify_sl, calls=calls)


def _res(order=1234, price=4100.0, ok=True):
    return {"ok": ok, "order": order, "price": price, "retcode": 10009}


class TestAttachEntrySl(unittest.TestCase):
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

    def test_long_sl_below_entry(self):
        ex = _ex([{"ok": True, "price": 4097.0}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertTrue(out["ok"])
        self.assertEqual(ex.calls, [(1234, 4097.0)])
        self.assertAlmostEqual(out["sl"], 4097.0, places=3)
        self.assertEqual(out["tries"], 1)

    def test_short_sl_above_entry(self):
        ex = _ex([{"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), -1)
        self.assertTrue(out["ok"])
        self.assertEqual(ex.calls, [(1234, 4103.0)])

    def test_dry_run_skipped(self):
        """纸面单在券商端没有仓位，改单无从谈起。"""
        ex = _ex([{"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(dry_run=True), _res(), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["skipped"], "dry_run")
        self.assertEqual(ex.calls, [])

    def test_sl_disabled_is_explicit(self):
        """sl_usd=0 是"这笔不要硬止损"的显式设定，不是漏配。"""
        ex = _ex([{"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(sl_usd=0.0), _res(), 1)
        self.assertEqual(out["skipped"], "sl_disabled")
        self.assertEqual(ex.calls, [])

    def test_no_ticket_skipped(self):
        ex = _ex([{"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(order=0), 1)
        self.assertEqual(out["skipped"], "no_ticket")
        self.assertEqual(ex.calls, [])

    def test_no_price_skipped(self):
        ex = _ex([{"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(price=0.0), 1)
        self.assertEqual(out["skipped"], "no_price_or_dist")
        self.assertEqual(ex.calls, [])

    def test_dist_inside_spread_is_refused(self):
        """价距比点差还窄时拒绝挂单。

        真实触发路径：`_symbol_contract()` 拿不到品种时静默退回 100，
        同一笔 sl_usd 算出的价距会缩到 1/100（XAUUSDc 的 3.0 变成 0.03），
        正好落进 0.24 的点差里。这种线挂上去等于"一开仓就触发"——
        必须挡在挂单之前，而不是让 modify_sl 的方向自检去兜
        （自检只管方向，不管距离够不够）。
        """
        ex = _ex([{"ok": True}])
        with mock.patch.object(autotrade, "price_dist_of", return_value=0.03):
            out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["skipped"], "dist_within_spread")
        self.assertEqual(ex.calls, [])
        self.assertAlmostEqual(out["spread"], 0.24, places=3)

    def test_no_quote_is_refused(self):
        """拿不到报价就没法判断价距是否落在点差里，宁可不挂。"""
        ex = _ex([{"ok": True}])
        with mock.patch.object(autotrade, "_quote", return_value=None):
            out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertEqual(out["skipped"], "no_quote")
        self.assertEqual(ex.calls, [])

    def test_retries_while_position_not_ready(self):
        """刚成交时 positions_get 可能还查不到这笔，要退一步再试。"""
        ex = _ex([{"ok": False, "reason": "持仓不存在"},
                  {"ok": False, "busy": True},
                  {"ok": True}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertTrue(out["ok"])
        self.assertEqual(out["tries"], 3)
        self.assertEqual(self.sleeper.call_count, 2)

    def test_no_retry_on_invalid_price(self):
        """方向非法这类参数错，重试也没用——白白拖慢开仓。"""
        ex = _ex([{"ok": False, "reason": "止损价不低于现价（价格已回到开仓价下方）"}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(out["tries"], 1)
        self.assertIn("止损价不低于现价", out["reason"])

    def test_gives_up_after_three_busy(self):
        ex = _ex([{"ok": False, "busy": True, "reason": "终端忙"}])
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(len(ex.calls), 3)
        self.assertEqual(out["tries"], 3)
        self.assertIn("终端忙", out["reason"])

    def test_never_raises(self):
        """改单抛异常也要变成返回值，不能把开仓线程带崩。"""
        def boom(ticket, sl):
            raise RuntimeError("terminal gone")

        ex = SimpleNamespace(modify_sl=boom)
        out = autotrade._attach_entry_sl(ex, autotrade.Cfg(), _res(), 1)
        self.assertFalse(out["ok"])
        self.assertIn("terminal gone", out["reason"])


class TestSendOpenAttachesSl(TempLedger):
    """send_open 的契约：开仓成败与挂止损失败分开报。"""

    def setUp(self):
        super().setUp()
        quote = mock.patch.object(autotrade, "_quote", return_value=(4100.0, 4100.24))
        quote.start()
        self.addCleanup(quote.stop)

    def _patch_executor(self, open_res, sl_res):
        ex = SimpleNamespace(
            connect=lambda: True,
            open_market=lambda **kw: dict(open_res),
            modify_sl=lambda ticket, sl: dict(sl_res),
            shutdown=lambda: None,
        )
        return mock.patch.object(autotrade, "_executor", return_value=ex), ex

    def test_open_ok_even_if_sl_attach_fails(self):
        patcher, _ = self._patch_executor(_res(), {"ok": False, "reason": "终端忙"})
        with patcher, mock.patch.object(autotrade, "price_dist_of", return_value=3.0), \
                mock.patch.object(autotrade.time, "sleep"):
            out = autotrade.send_open(autotrade.Cfg(), 1)
        self.assertTrue(out["ok"], "仓位已经真实存在，不能因为挂止损失败就报开仓失败")
        self.assertFalse(out["sl_attach"]["ok"])
        self.assertEqual(out["order"], 1234)

    def test_open_ok_and_sl_attached(self):
        patcher, _ = self._patch_executor(_res(), {"ok": True, "price": 4097.0})
        with patcher, mock.patch.object(autotrade, "price_dist_of", return_value=3.0):
            out = autotrade.send_open(autotrade.Cfg(), 1)
        self.assertTrue(out["ok"])
        self.assertTrue(out["sl_attach"]["ok"])
        self.assertAlmostEqual(out["sl_attach"]["sl"], 4097.0, places=3)

    def test_dry_run_does_not_attach(self):
        patcher, _ = self._patch_executor(
            {"ok": True, "dry_run": True, "request": {}}, {"ok": True})
        with patcher:
            out = autotrade.send_open(autotrade.Cfg(dry_run=True), 1)
        self.assertTrue(out["ok"])
        self.assertEqual(out["sl_attach"]["skipped"], "dry_run")

    def test_open_failure_has_no_sl_attach(self):
        patcher, _ = self._patch_executor(
            {"ok": False, "retcode": 10027}, {"ok": True})
        with patcher:
            out = autotrade.send_open(autotrade.Cfg(), 1)
        self.assertFalse(out["ok"])
        self.assertNotIn("sl_attach", out, "没开仓就不该报止损挂载结果")


class TestBrokerSlCloseSource(TempLedger):
    """仓在终端消失时，reason=4 + sl_broker 必须记成 sl。"""

    def _pos(self, **kw):
        base = {"ticket": 77, "direction": 1, "lots": 0.01, "price_open": 4100.0,
                "opened_at": NOW - 300, "p_up": 0.6, "plan": "trend",
                "dry_run": False}
        base.update(kw)
        return base

    def _tick(self, pos, fills):
        state = autotrade._empty_state()
        state["position"] = pos
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "_close_fills", return_value=fills), \
                mock.patch.object(autotrade, "_quote", return_value=(4096.0, 4096.24)):
            return autotrade.tick(autotrade.Cfg(), now=NOW, quote=(4096.0, 4096.24),
                                  rows=bars_up(), positions=[])

    def test_sl_hit_recorded_as_sl(self):
        row = self._tick(self._pos(sl_broker=4097.0),
                         {"pnl": -3.0, "price": 4097.0, "reason": 4, "time": NOW})
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")
        self.assertIn("入场止损", row["reason"])

    def test_manual_close_still_external(self):
        """止损挂着但人在终端手动平了——reason 不是 4，就得记人工。"""
        row = self._tick(self._pos(sl_broker=4097.0),
                         {"pnl": 1.5, "price": 4101.5, "reason": 3, "time": NOW})
        self.assertEqual(row["source"], "external")

    def test_be_takes_priority_over_entry_sl(self):
        """保本挂上后 sl_broker 会同步成保本价，但来源仍该记 be。"""
        row = self._tick(self._pos(be_ok=1, be_sl=4100.0, sl_broker=4100.0),
                         {"pnl": 0.0, "price": 4100.0, "reason": 4, "time": NOW})
        self.assertEqual(row["source"], "be")

    def test_no_broker_sl_means_external(self):
        """没挂过券商端止损的单子（老仓位/挂载失败），reason=4 也不该记 sl。"""
        row = self._tick(self._pos(), {"pnl": -3.0, "price": 4097.0,
                                       "reason": 4, "time": NOW})
        self.assertEqual(row["source"], "external")


if __name__ == "__main__":
    unittest.main()
