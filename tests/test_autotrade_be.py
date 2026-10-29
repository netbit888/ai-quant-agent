"""保本止损（2026-10-29）：趋势单摸到触发线后把券商端 SL 抬到开仓价。

三条要钉死的东西：
  1. **只给趋势单**。震荡单的止盈门槛本来就是 3.25，摸到就该让模型说话，
     再挂一条保本线等于给同一笔单两套规则。
  2. **挂不上不能就算了**。改单会失败（终端算法交易没开、锁被占、价格已经
     回到开仓价以下），失败时浮盈回到 0 必须由程序代平——否则这条规则
     最需要它的时候正好是空的。
  3. **顺序**：硬止损在前、保本兜底在后。浮亏 -3.5 的单子两条线同时为真，
     记成 sl 才是实情（那是入场方向错了，不是"保本生效"）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


def _pos(**kw) -> dict:
    base = {"ticket": 501, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "opened_at": NOW - 60, "p_up": 0.6, "entry_reason": "顺势",
            "plan": "trend", "dry_run": False}
    base.update(kw)
    return base


class TestBeStep(TempLedger):
    def test_only_trend_positions(self):
        """震荡单不受理：3.25 本来就是它的止盈门槛。"""
        cfg = autotrade.Cfg()
        for plan in ("range", "mid", None, ""):
            pos = _pos(plan=plan, peak_pnl=5.0)
            with mock.patch.object(autotrade, "send_modify_sl") as m:
                self.assertIsNone(autotrade._be_step(cfg, pos, 5.0, NOW))
            m.assert_not_called()
            self.assertNotIn("be_arm_ts", pos)

    def test_waits_for_threshold(self):
        cfg = autotrade.Cfg(be_arm_usd=3.25)
        pos = _pos(peak_pnl=3.0)
        self.assertIsNone(autotrade._be_step(cfg, pos, 3.0, NOW))
        self.assertNotIn("be_arm_ts", pos)

    def test_switch_off(self):
        cfg = autotrade.Cfg(be_at_entry=False)
        pos = _pos(peak_pnl=9.0)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._be_step(cfg, pos, 9.0, NOW))
        m.assert_not_called()
        self.assertNotIn("be_arm_ts", pos)

    def test_paper_position_arms_locally_once(self):
        """纸面单券商那边没有这一笔，改单无从谈起 → 记 paper，交给本地兜底。"""
        cfg = autotrade.Cfg()
        pos = _pos(dry_run=True, peak_pnl=3.6)
        row = autotrade._be_step(cfg, pos, 3.6, NOW)
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "be")
        self.assertEqual(row["mode"], "paper")
        self.assertAlmostEqual(row["sl"], 4000.0)
        self.assertEqual(pos["be_mode"], "paper")
        self.assertFalse(pos.get("be_ok"))
        # 第二格不能重复记账（快查 2 秒一格，不然一小时刷出上千行）
        self.assertIsNone(autotrade._be_step(cfg, pos, 3.6, NOW + 2))

    def test_broker_arm_success(self):
        cfg = autotrade.Cfg()
        pos = _pos(peak_pnl=3.6)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4000.0}) as m:
            row = autotrade._be_step(cfg, pos, 3.6, NOW)
        m.assert_called_once_with(cfg, 501, 4000.0)
        self.assertEqual(row["kind"], "be")
        self.assertEqual(row["mode"], "broker")
        self.assertEqual(pos["be_ok"], 1)
        self.assertAlmostEqual(pos["be_sl"], 4000.0)
        # 挂上之后每格空转，不再碰券商
        with mock.patch.object(autotrade, "send_modify_sl") as m2:
            self.assertIsNone(autotrade._be_step(cfg, pos, 5.0, NOW + 5))
        m2.assert_not_called()

    def test_failure_logged_once_then_throttled(self):
        """失败要留痕，但不能每 2 秒留一次——一小时能刷 1800 行。"""
        cfg = autotrade.Cfg(be_retry_sec=20)
        pos = _pos(peak_pnl=3.6)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": False, "reason": "终端忙"}) as m:
            row = autotrade._be_step(cfg, pos, 3.6, NOW)
            self.assertEqual(row["kind"], "be_fail")
            self.assertIn("终端忙", row["error"])
            self.assertEqual(pos["be_tries"], 1)
            # 节流期内不再试
            self.assertIsNone(autotrade._be_step(cfg, pos, 3.6, NOW + 5))
            self.assertEqual(m.call_count, 1)
            # 过了节流期再试，仍失败但不再重复写流水
            self.assertIsNone(autotrade._be_step(cfg, pos, 3.6, NOW + 21))
            self.assertEqual(m.call_count, 2)
            self.assertEqual(pos["be_tries"], 2)
        self.assertFalse(pos.get("be_ok"))

    def test_busy_is_not_a_failure(self):
        """终端正忙（连接中/锁被占）不算失败：不消耗重试节流，下一格再来。

        算成失败的话，一次 30 秒的 `initialize` 会顺手吃掉一次重试窗口，
        而那正好是价格最可能回头的时候。
        """
        cfg = autotrade.Cfg(be_retry_sec=20)
        pos = _pos(peak_pnl=3.6)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": False, "busy": True,
                                             "reason": "终端忙"}) as m:
            self.assertIsNone(autotrade._be_step(cfg, pos, 3.6, NOW))
            self.assertIsNone(autotrade._be_step(cfg, pos, 3.6, NOW + 2))
        self.assertEqual(m.call_count, 2)          # 下一格立刻重试，不等 20 秒
        self.assertNotIn("be_err", pos)
        self.assertEqual(pos["be_arm_ts"], NOW)

    def test_price_back_below_entry_sets_arm_without_attempt(self):
        """价格已经回到开仓价以下：挂上去会被券商拒（10016），但
        be_arm_ts 必须记下来——本地兜底靠它。"""
        cfg = autotrade.Cfg()
        pos = _pos(peak_pnl=3.6)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._be_step(cfg, pos, -0.4, NOW))
        m.assert_not_called()
        self.assertEqual(pos["be_arm_ts"], NOW)
        self.assertFalse(pos.get("be_ok"))


class TestBeLocalFallback(TempLedger):
    def test_only_when_broker_did_not_arm(self):
        cfg = autotrade.Cfg()
        armed = _pos(be_arm_ts=NOW, be_ok=1)
        self.assertFalse(autotrade._be_local_hit(cfg, armed, -0.2))
        failed = _pos(be_arm_ts=NOW)
        self.assertTrue(autotrade._be_local_hit(cfg, failed, 0.0))
        self.assertTrue(autotrade._be_local_hit(cfg, failed, -0.3))
        self.assertFalse(autotrade._be_local_hit(cfg, failed, 0.3))

    def test_needs_arm_marker(self):
        cfg = autotrade.Cfg()
        self.assertFalse(autotrade._be_local_hit(cfg, _pos(), -0.2))

    def test_range_position_never(self):
        cfg = autotrade.Cfg()
        self.assertFalse(autotrade._be_local_hit(cfg, _pos(plan="range", be_arm_ts=NOW), -0.2))

    def test_guard_hit_reports_breakeven(self):
        """快查也要认这条线：浮盈回到 0 就提前结束等待，让下一轮去平。"""
        cfg = autotrade.Cfg(sl_usd=3.0)
        pos = _pos(be_arm_ts=NOW)
        msg = autotrade._guard_hit(cfg, pos, 4000.0, 4000.24, fp=-0.2)
        self.assertIn("保本线", msg)
        self.assertEqual(autotrade._guard_hit(cfg, pos, 4001.0, 4001.24, fp=1.0), "")

    def test_guard_hit_still_works_with_sl_disabled(self):
        cfg = autotrade.Cfg(sl_usd=0)
        pos = _pos(be_arm_ts=NOW)
        self.assertIn("保本线", autotrade._guard_hit(cfg, pos, 4000.0, 4000.24, fp=-0.2))


class TestExitGateOrder(TempLedger):
    def test_sl_wins_over_be_local(self):
        """浮亏 -3.5：两条线同时为真，记成 sl 才是实情。"""
        cfg = autotrade.Cfg(sl_usd=3.0)
        gate = autotrade._exit_gate(cfg, 120, -3.5, "trend", be_local=True)
        self.assertEqual(gate["source"], "sl")

    def test_be_local_beats_timeout(self):
        """保本兜底必须排在持仓上限之前——它的条件是"浮盈回到 0"，
        排在 tp_wait 后面就永远走不到。"""
        cfg = autotrade.Cfg(sl_usd=3.0, max_hold_trend=60)
        gate = autotrade._exit_gate(cfg, 99999, -0.1, "trend", be_local=True)
        self.assertEqual(gate["source"], "be_local")

    def test_default_off(self):
        cfg = autotrade.Cfg(sl_usd=3.0)
        gate = autotrade._exit_gate(cfg, 120, -0.1, "trend")
        self.assertEqual(gate["source"], "tp_wait")


class TestBeInTick(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(sl_usd=3.0, tp_min_usd=3.25, tp_min_trend=7.0,
                                 interval_sec=60, guard_sec=5)

    def _save(self, **kw):
        state = autotrade._empty_state()
        state["position"] = _pos(**kw)
        autotrade.save_state(state)
        return state

    def test_tick_arms_broker_sl(self):
        """60 秒那一格走到时也要挂——快查关掉（guard_sec=0）的人同样受保护。"""
        self._save(peak_pnl=1.0)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4000.0}) as m:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4004.0, 4004.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 501, "symbol": "XAUUSDm"}])
        m.assert_called_once()
        self.assertEqual(row["kind"], "hold")          # 4.0 < 7.0，仍不调模型
        after = autotrade.load_state()["position"]
        self.assertEqual(after["be_ok"], 1)
        self.assertAlmostEqual(after["be_sl"], 4000.0)

    def test_tick_logs_be_row(self):
        self._save(peak_pnl=1.0)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4000.0}):
            autotrade.tick(self.cfg, now=NOW, quote=(4004.0, 4004.24),
                           rows=bars_up(),
                           positions=[{"ticket": 501, "symbol": "XAUUSDm"}])
        kinds = [r.get("kind") for r in autotrade.read_log()]
        self.assertIn("be", kinds)

    def test_external_close_marked_be(self):
        """券商端那条线打的：成交在开仓价附近，来源要记 be 而不是 external。"""
        self._save(peak_pnl=3.6, be_ok=1, be_sl=4000.0)
        with mock.patch.object(autotrade, "_close_deal_reason", return_value=4):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "be")
        self.assertIn("保本", row["reason"])
        self.assertAlmostEqual(row["pnl"], 0.0, places=2)
        self.assertEqual(autotrade.load_state()["be_closes"], 1)

    def test_manual_close_after_arm_stays_external(self):
        """挂着保本线的时候人手动平了：deal.reason=0，不能算成规则生效。"""
        self._save(peak_pnl=3.6, be_ok=1, be_sl=4000.0)
        with mock.patch.object(autotrade, "_close_deal_reason", return_value=0):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4002.0, 4002.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["source"], "external")
        # 文案必须写明是"人工/终端操作"。写成"仓位已在终端消失"，复盘模型会读成
        # 程序故障，产出「别被终端消失吓出」这种教模型下次该走也别走的教训
        # （2026-10-29 那笔 +7.39→+3.71 的人工平仓就这么被写了）。
        self.assertIn("人工", row["reason"])
        self.assertNotIn("已在终端消失", row["reason"])

    def test_gate_closes_with_be_local_source(self):
        """券商端没挂上、浮盈回到 0：程序代平，close 行记为 be_local。"""
        self._save(peak_pnl=3.6, be_arm_ts=NOW - 30)   # 触发过但 be_ok 没有
        with mock.patch.object(autotrade, "send_close", return_value={"ok": True}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 501, "symbol": "XAUUSDm"}])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "be_local")
        self.assertIn("保本线", row["reason"])


class TestBeInFastLoop(TempLedger):
    class _Clock:
        def __init__(self, t: float = 1_000_000.0):
            self.t = t
            self.sleeps: list[float] = []

        def time(self) -> float:
            return self.t

        def sleep(self, sec: float) -> None:
            self.sleeps.append(round(sec, 2))
            self.t += sec

    def test_fast_loop_arms_within_one_grid(self):
        """峰值往往只在 2 秒格里出现一次：等 60 秒那一格再看，价格早回去了。"""
        state = autotrade._empty_state()
        state["position"] = _pos(peak_pnl=1.0)
        autotrade.save_state(state)
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, tp_min_trend=7.0,
                            tp_min_usd=3.25)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time), \
             mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4000.0}) as m:
            autotrade._wait_next(clock.t, {"kind": "hold"}, cfg,
                                 sleeper=clock.sleep,
                                 quote_fn=lambda s: (4004.0, 4004.24))
        m.assert_called_once()
        after = autotrade.load_state()["position"]
        self.assertEqual(after["be_ok"], 1)


class TestExecutorModifySl(TempLedger):
    """执行层那张改单请求本身（不连终端）。

    这一段单独测的理由：SLTP 请求有两个只在真单上才暴露的坑——
    `tp` 漏传会把持仓上已有的止盈清掉，参数用 kwargs 传给 `order_send`
    会得到 None + error -2。两条都不是异常，是"静默什么都没发生"。
    """

    def _ex(self, dry_run=True):
        from execution import live as exmod
        return exmod, exmod.Executor(exmod.ExecConfig(symbol="XAUUSDm",
                                                      dry_run=dry_run))

    def _pos(self, type_=0, tp=0.0):
        return SimpleNamespace(ticket=1, type=type_, symbol="XAUUSDm",
                               volume=0.01, price_open=4000.0, sl=0.0, tp=tp)

    def test_dry_run_builds_sltp_request_and_keeps_tp(self):
        exmod, ex = self._ex(dry_run=True)
        mt5 = exmod.mt5
        with mock.patch.object(mt5, "positions_get", return_value=[self._pos(tp=4010.0)]), \
             mock.patch.object(mt5, "symbol_info",
                               return_value=SimpleNamespace(digits=3, visible=True)), \
             mock.patch.object(mt5, "symbol_info_tick",
                               return_value=SimpleNamespace(bid=4005.0, ask=4005.24)):
            res = ex.modify_sl(1, 4000.0)
        self.assertTrue(res["ok"])
        req = res["request"]
        self.assertEqual(req["action"], mt5.TRADE_ACTION_SLTP)
        self.assertEqual(req["position"], 1)
        self.assertAlmostEqual(req["sl"], 4000.0)
        # 止盈必须原样带回去，否则改一次止损就把止盈抹了
        self.assertAlmostEqual(req["tp"], 4010.0)

    def test_refuses_stop_on_wrong_side_of_price(self):
        """多单止损设在现价上方 = 一挂就被打掉，券商也会拒；本地说清楚。"""
        exmod, ex = self._ex(dry_run=True)
        mt5 = exmod.mt5
        with mock.patch.object(mt5, "positions_get", return_value=[self._pos()]), \
             mock.patch.object(mt5, "symbol_info",
                               return_value=SimpleNamespace(digits=3, visible=True)), \
             mock.patch.object(mt5, "symbol_info_tick",
                               return_value=SimpleNamespace(bid=3998.0, ask=3998.24)):
            res = ex.modify_sl(1, 4000.0)
        self.assertFalse(res["ok"])
        self.assertIn("现价", res["reason"])

    def test_unreadable_positions_is_busy_not_missing(self):
        """读不到持仓 ≠ 没有持仓。前者下一格重试，后者说明仓已经没了。"""
        exmod, ex = self._ex(dry_run=True)
        mt5 = exmod.mt5
        with mock.patch.object(mt5, "positions_get", return_value=None):
            res = ex.modify_sl(1, 4000.0)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("busy"))


if __name__ == "__main__":
    unittest.main()
