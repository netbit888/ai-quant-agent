"""手工单影子峰值：程序不接管，但必须留下 MFE。

为什么单开一条（2026-11-17）
--------------------------
用户原话：「手工单怎么没有脉冲浮盈分析？很多信息没有，你看下最近一单，
我是不是跑早了」。那笔 4991040502：3876.017 开多、3883.436 平掉 +74.2，
平仓 7 秒后价格 3886.96、那一分钟高点约 3888.5。面板上峰值浮盈是一根横杠。

根因不在"少画一个字段"：`_track_peak` / `_mark_burst` / `_peak_atr` 全挂在
`state["position"]` 上，而手工单按设计**不进 state**（进去就等于被出场闸门
接管）。于是 MFE（最大浮盈）根本没被记录过，事后补不出来——而"跑早了吗"
这一问的唯一依据就是 MFE。

这里守四件事：
1. 峰值只增不减，且落盘（面板重启不丢）；
2. 平仓时写一行 `kind="manual_close"`，带 peak_pnl / atr_peak / t3 / t5 / mae_pnl；
3. **一个字都不写 state**（接管与记录的分界线，写进去就是另一个产品）；
4. `manual_close` 不进程序战绩（`summary` 的 closed/wins/pnl 只认 `kind=="close"`）。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_up

from live import autotrade


def _rows(ticket=111, price_open=4000.0, lots=0.1, opened=None, sl=0.0):
    """journal.open_positions() 的真实字段形状。"""
    return [{
        "ticket": ticket, "position_id": ticket, "symbol": "XAUUSDc",
        "direction": 1, "volume": lots, "price_open": price_open,
        "price_current": price_open, "sl": sl, "tp": 0.0,
        "profit": 0.0, "swap": 0.0,
        "open_time": int(NOW if opened is None else opened),
        "comment": "",
    }]


def _cfg():
    return autotrade.Cfg(symbol="XAUUSDc", lots=0.1)


class ManualShadowPeak(TempLedger):
    def _sync(self, rows, fps, cfg=None):
        # _bars 走 MT5，测试里必须挡住；不然会去连终端。
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            return autotrade._manual_shadow_sync(cfg or _cfg(), rows, fps, "999")

    def test_peak_only_rises_and_survives_reload(self):
        sh = self._sync(_rows(), {111: 5.0})
        self.assertAlmostEqual(sh["111"]["peak_pnl"], 5.0, places=2)

        sh = self._sync(_rows(), {111: 12.0})
        self.assertAlmostEqual(sh["111"]["peak_pnl"], 12.0, places=2)

        # 回落不能把峰值抹掉——这正是"跑早了吗"要看的那个数
        sh = self._sync(_rows(), {111: 3.0})
        self.assertAlmostEqual(sh["111"]["peak_pnl"], 12.0, places=2)

        # 从磁盘重新读（面板重启 / 新进程）之后还是 12
        again = autotrade._load_manual_shadow("999")
        self.assertAlmostEqual(again["111"]["peak_pnl"], 12.0, places=2)

    def test_mae_is_tracked_separately(self):
        """MAE 回答的是"止损挪得对不对"，和峰值不是一回事，不能被峰值吞掉。"""
        self._sync(_rows(), {111: 2.0})
        sh = self._sync(_rows(), {111: -30.0})
        self.assertAlmostEqual(sh["111"]["mae_pnl"], -30.0, places=2)

    def test_atr_locked_once(self):
        sh = self._sync(_rows(), {111: 1.0})
        first = sh["111"]["atr"]
        self.assertIsNotNone(first)
        # 第二次同步换一组 K 线喂进去，锁定的 atr 不许变（否则同一笔单前后
        # 会被两套门槛换算，和程序单"开仓锁 atr"的规则冲突）
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(80)):
            sh = autotrade._manual_shadow_sync(_cfg(), _rows(), {111: 2.0}, "999")
        self.assertAlmostEqual(sh["111"]["atr"], first, places=5)

    def test_never_writes_state(self):
        """硬约束：记录 ≠ 接管。写 state 会让 _exit_gate 给手工单挂 SL/超时平仓。"""
        with mock.patch.object(autotrade, "save_state") as saver:
            self._sync(_rows(), {111: 5.0})
            self._sync([], {})
        saver.assert_not_called()


class ManualCloseRow(TempLedger):
    def test_close_row_carries_mfe_fields(self):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            autotrade._manual_shadow_sync(_cfg(), _rows(), {111: 40.0}, "999")

        fills = {"pnl": 74.2, "price": 3883.436, "reason": 0,
                 "time": NOW + 462}
        written: list = []
        with mock.patch.object(autotrade, "_close_fills", return_value=fills), \
                mock.patch.object(autotrade, "append_log",
                                  side_effect=lambda row, login=None: written.append(row)):
            autotrade._manual_shadow_sync(_cfg(), [], {}, "999")

        self.assertEqual(len(written), 1)
        row = written[0]
        self.assertEqual(row["kind"], "manual_close")
        self.assertEqual(row["pnl"], 74.2)
        self.assertEqual(row["exit_price"], 3883.436)
        self.assertEqual(row["held_sec"], 462)
        for key in ("peak_pnl", "peak_ts", "mae_pnl", "atr", "atr_pnl",
                    "atr_peak", "t3", "t5", "burst"):
            self.assertIn(key, row)
        self.assertAlmostEqual(row["peak_pnl"], 40.0, places=2)
        # 平完之后影子要清掉，否则下一笔会被旧峰值污染
        self.assertNotIn("111", autotrade._load_manual_shadow("999"))

    def test_close_waits_for_deal_then_gives_up(self):
        """成交回执迟到：先等，不按报价估一个数出来（11-06 那笔保本单的坑）。"""
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            autotrade._manual_shadow_sync(_cfg(), _rows(), {111: 5.0}, "999")

        written: list = []
        with mock.patch.object(autotrade, "_close_fills", return_value=None), \
                mock.patch.object(autotrade, "append_log",
                                  side_effect=lambda row, login=None: written.append(row)):
            autotrade._manual_shadow_sync(_cfg(), [], {}, "999")
            self.assertEqual(written, [])          # 第一轮：等
            self.assertIn("111", autotrade._load_manual_shadow("999"))

            # 等满上限之后落一行 pnl=None，明确承认没读到
            autotrade._MANUAL_SHADOW_MISS_MAX, old = 1, autotrade._MANUAL_SHADOW_MISS_MAX
            try:
                autotrade._manual_shadow_sync(_cfg(), [], {}, "999")
            finally:
                autotrade._MANUAL_SHADOW_MISS_MAX = old

        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["kind"], "manual_close")
        self.assertIsNone(written[0]["pnl"])
        self.assertEqual(written[0]["pnl_src"], "unread")


class ManualCloseNotInRecord(TempLedger):
    def test_summary_ignores_manual_close(self):
        """手工单是用户的成绩，不是程序的。混进 closed/wins 就是另一件事了。"""
        import time as _t
        today = int(_t.time())
        with mock.patch.object(autotrade, "read_log", return_value=[
            {"kind": "close", "pnl": 10.0, "direction": 1, "ts": today},
            {"kind": "manual_close", "pnl": 74.2, "direction": 1, "ts": today},
            {"kind": "manual_close", "pnl": -20.0, "direction": -1, "ts": today},
        ]), mock.patch.object(autotrade, "_manual_position", return_value=None):
            s = autotrade.summary("999")
        self.assertEqual(s["closed"], 1)
        self.assertEqual(s["wins"], 1)
        self.assertAlmostEqual(s["pnl_today"], 10.0, places=2)


class ManualRiskBaseline(TempLedger):
    """风险基线：程序不接管止损，但"人把风险放大了"必须留痕。

    11-17 那笔手工单把 SL 从 3869.447 挪到 3866.022（最坏 30 → 100 美元），
    账本里只剩一个 +74.2。有了基线，面板才能说"放宽 3.3 倍"，
    流水里也才有 manual_sl 这一行。
    """

    def _sync_logged(self, rows, fps):
        written: list = []
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)), \
                mock.patch.object(autotrade, "append_log",
                                  side_effect=lambda row, login=None: written.append(row)):
            sh = autotrade._manual_shadow_sync(_cfg(), rows, fps, "999")
        return sh, written

    def test_baseline_locked_on_first_seen_sl(self):
        sh, written = self._sync_logged(_rows(sl=3990.0), {111: 1.0})
        self.assertAlmostEqual(sh["111"]["sl0"], 3990.0, places=5)
        # 基线 = 用同一个 _pnl_at_price 算出的最坏亏损（合约乘数由测试环境定，
        # 不写死 100——写死等于把环境规格烤进断言里）
        exp = autotrade._pnl_at_price(
            _cfg(), {"direction": 1, "price_open": 4000.0, "lots": 0.1}, 3990.0)
        self.assertAlmostEqual(sh["111"]["risk0"], exp, places=2)
        self.assertLess(sh["111"]["risk0"], 0)
        # 第一次看到 SL 不算"改动"，不能凭空记一行
        self.assertEqual(written, [])

    def test_widening_keeps_baseline_and_logs_amounts(self):
        self._sync_logged(_rows(sl=3990.0), {111: 1.0})
        sh, written = self._sync_logged(_rows(sl=3980.0), {111: -50.0})

        self.assertEqual(len(written), 1)
        row = written[0]
        self.assertEqual(row["kind"], "manual_sl")
        self.assertTrue(row["widened"])
        # 3990 → 3980 是把止损距离从 10 拉成 20：最坏亏损正好翻倍
        self.assertLess(row["risk_from"], 0)
        self.assertAlmostEqual(row["risk_to"], row["risk_from"] * 2, places=2)
        self.assertIn("放宽", row["reason"])
        # 基线钉在第一次看到的那个 SL 上，不会跟着一起漂
        self.assertAlmostEqual(sh["111"]["risk0"], row["risk_from"], places=2)

    def test_tightening_and_removal(self):
        self._sync_logged(_rows(sl=3990.0), {111: 1.0})
        _, written = self._sync_logged(_rows(sl=3996.0), {111: -10.0})
        self.assertFalse(written[0]["widened"])       # 收紧：留痕但不算放宽
        self.assertIn("收紧", written[0]["reason"])

        _, written = self._sync_logged(_rows(sl=0.0), {111: -10.0})
        self.assertTrue(written[0]["widened"])        # 撤掉止损 = 风险无上限
        self.assertIsNone(written[0]["risk_to"])
        self.assertIn("撤掉", written[0]["reason"])

    def test_no_row_when_sl_unchanged(self):
        self._sync_logged(_rows(sl=3990.0), {111: 1.0})
        for fp in (2.0, 3.0, -1.0):
            _, written = self._sync_logged(_rows(sl=3990.0), {111: fp})
            self.assertEqual(written, [])

    def test_view_carries_risk_readout(self):
        """面板要的三个数：最坏亏多少、放宽几倍、止损离入场几个 ATR。"""
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            autotrade._manual_shadow_sync(_cfg(), _rows(sl=3990.0), {111: 1.0}, "999")
        with mock.patch("live.journal.open_positions",
                        return_value=_rows(sl=3980.0)), \
                mock.patch.object(autotrade, "_quote", return_value=(3990.0, 3990.2)), \
                mock.patch.object(autotrade, "_bars", return_value=bars_up(40)), \
                mock.patch.object(autotrade, "append_log"):
            autotrade._MANUAL_POS_CACHE["ts"] = 0.0
            view = autotrade._manual_position({"symbol": "XAUUSDc", "lots": 0.1})
        self.assertLess(view["sl_risk"], 0)
        self.assertAlmostEqual(view["risk_mult"], 2.0, places=2)
        self.assertAlmostEqual(view["risk0"], view["sl_risk"] / 2, places=2)
        self.assertIsNotNone(view["sl_atr"])
        self.assertGreater(view["sl_atr"], 0)


class ProgramTicketIsNotManual(TempLedger):
    """程序自己那一笔绝不能被当成手工单（2026-11-20）。

    11-18 把影子改成「无条件推进」之后留下的口子：`rows` 是按品种取的账户
    持仓，里面天然含着程序刚开的那笔（magic=0，券商眼里和手工单没区别）。
    结果程序持仓期间会给自己建一份影子，平仓时多落一行 `kind="manual_close"`
    ——一笔程序单在流水里有两行收尾，按 ticket 聚合的工具全被这条噪音污染。
    战绩本身不受影响（summary 只认 `kind=="close"`），所以它一直没被发现。
    """

    def _sync(self, rows, fps, skip=0):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(40)):
            return autotrade._manual_shadow_sync(_cfg(), rows, fps, "999",
                                                 skip_ticket=skip)

    def test_program_ticket_never_enters_shadow(self):
        sh = self._sync(_rows(ticket=222), {222: 5.0}, skip=222)
        self.assertEqual(sh, {})
        self.assertEqual(autotrade._load_manual_shadow("999"), {})

    def test_stale_program_entry_dropped_without_close_row(self):
        """升级前已经建下的影子：删掉就行，**不许**补一行平仓——它不是手工单。"""
        self._sync(_rows(ticket=222), {222: 5.0})          # 旧逻辑留下的
        self.assertIn("222", autotrade._load_manual_shadow("999"))

        written: list = []
        with mock.patch.object(autotrade, "append_log",
                               side_effect=lambda row, login=None: written.append(row)):
            self._sync([], {}, skip=222)
        self.assertEqual(written, [])
        self.assertEqual(autotrade._load_manual_shadow("999"), {})

    def test_view_and_count_skip_program_position(self):
        """同品种两笔仓（程序 222 + 手工 111）：视图只认手工那笔，count 也是 1。"""
        rows = _rows(ticket=111) + _rows(ticket=222, price_open=4010.0)
        with mock.patch("live.journal.open_positions", return_value=rows), \
                mock.patch.object(autotrade, "_quote", return_value=(4000.0, 4000.2)), \
                mock.patch.object(autotrade, "_bars", return_value=bars_up(40)), \
                mock.patch.object(autotrade, "append_log"):
            autotrade._MANUAL_POS_CACHE["ts"] = 0.0
            view = autotrade._manual_position({"symbol": "XAUUSDc", "lots": 0.1},
                                              skip_ticket=222)
        self.assertEqual(view["ticket"], 111)
        self.assertEqual(view["count"], 1)
        shadow = autotrade._load_manual_shadow("999")
        self.assertIn("111", shadow)
        self.assertNotIn("222", shadow)


class ManualVerdict(unittest.TestCase):
    """平仓判语：手工单不进复盘队列，这句话就是它的复盘。"""

    def test_capture_ratio_drives_wording(self):
        sh = {"peak_pnl": 100.0, "t5": 9.0}      # t5>3 → 不算脉冲
        text, cap = autotrade._manual_close_verdict(sh, 40.0)
        self.assertAlmostEqual(cap, 0.4, places=3)
        self.assertIn("跑得早", text)

        text, cap = autotrade._manual_close_verdict(sh, 90.0)
        self.assertAlmostEqual(cap, 0.9, places=3)
        self.assertIn("基本吃到整段", text)

        text, _ = autotrade._manual_close_verdict(sh, 65.0)
        self.assertIn("65%", text)
        self.assertNotIn("跑得早", text)

    def test_loss_branches(self):
        # 到过峰值又亏着出来：这是"该走没走"，不是"入场即错"
        text, cap = autotrade._manual_close_verdict({"peak_pnl": 100.0}, -5.0)
        self.assertIn("该走没走", text)
        self.assertAlmostEqual(cap, 0.0, places=3)
        # 全程没正过 = 入场问题，措辞必须和出场问题分开
        text, cap = autotrade._manual_close_verdict({"peak_pnl": -3.0}, -5.0)
        self.assertEqual(cap, 0.0)
        self.assertIn("入场即错", text)

    def test_unknown_pnl_says_so(self):
        text, cap = autotrade._manual_close_verdict({"peak_pnl": 12.0}, None)
        self.assertIsNone(cap)
        self.assertIn("没读到", text)
        self.assertIn("12", text)          # 峰值仍然要摆出来，那是唯一确知的数

    def test_burst_with_low_capture(self):
        text, _ = autotrade._manual_close_verdict(
            {"peak_pnl": 100.0, "t5": 2.0, "atr_pnl": 10.0}, 20.0)
        self.assertIn("脉冲后只吃到", text)


if __name__ == "__main__":
    unittest.main()
