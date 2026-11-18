"""2026-11-18 功能审查的修复回归。

每一条对应 `docs/review-2026-11-18.md` 里的一个问题编号。这里只钉**行为**，
不复述实现细节——实现改了、行为还在，测试就该继续绿。

  F1 券商端止损挂不上 → `position.sl_err` 必须有值，面板才可能提示
  F5 停止信号已 set → 这一轮不许再 send_open（否则清仓后多一笔幽灵仓）
  F6 部分成交 → `lots` 用实际成交量，open 行带 `partial_fill` 标记
  F7 程序持仓期间 → 手工单影子照样推进（只是不给面板看）
  F10 summary 累计口径 → state 计数器优先，窗口数只当下限
  F11 手工单影子 `seen_ts` → 长期持仓不会被 24 小时清理误删
  F12 取不到 K 线 → skip 行带真实原因，不是静默空列表
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_up

from live import autotrade


class TestPositionSlErr(TempLedger):
    """F1：挂载失败的原因必须进 pos，不能只留在流水里。"""

    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=False)

    def _open(self, send_res, cfg=None):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open", return_value=send_res):
            return autotrade.tick(cfg or self.cfg, now=NOW,
                                  quote=(4000.0, 4000.24), rows=bars_up(),
                                  positions=[])

    @staticmethod
    def _res(**over):
        base = {"ok": True, "price": 4001.0, "order": 1,
                "sl_attach": {"ok": False, "reason": "终端忙"},
                "tp_attach": {"ok": False, "wanted": False}}
        base.update(over)
        return base

    def test_attach_failure_lands_in_position(self):
        row = self._open(self._res())
        pos = autotrade.load_state()["position"]
        self.assertEqual(pos["sl_broker"], 0.0)
        self.assertEqual(pos["sl_err"], "终端忙")
        self.assertEqual(row["sl_err"], "终端忙")

    def test_skipped_reason_is_kept(self):
        """被跳过（sl_usd=0 / 价距落在点差里）也是"没有券商端保护"。"""
        row = self._open(self._res(sl_attach={"ok": False,
                                             "skipped": "dist_within_spread"}))
        self.assertEqual(autotrade.load_state()["position"]["sl_err"],
                         "dist_within_spread")
        self.assertEqual(row["sl_err"], "dist_within_spread")

    def test_dry_run_has_no_sl_err(self):
        """纸面单本来就没有券商端保护，写成"未挂"等于每笔都亮假警报。"""
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True)
        self._open(self._res(sl_attach={"ok": False, "skipped": "dry_run"}), cfg=cfg)
        self.assertEqual(autotrade.load_state()["position"]["sl_err"], "")

    def test_attached_sl_clears_err(self):
        self._open(self._res(sl_attach={"ok": True, "sl": 3998.0}))
        pos = autotrade.load_state()["position"]
        self.assertAlmostEqual(pos["sl_broker"], 3998.0, places=3)
        self.assertEqual(pos["sl_err"], "")


class TestPartialFill(TempLedger):
    """F6：券商只成交一部分时，不能按配置手数记账。"""

    def test_actual_volume_wins(self):
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=False)
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4001.0,
                                             "order": 7, "volume": 0.005,
                                             "sl_attach": {"ok": True, "sl": 3998.0},
                                             "tp_attach": {"ok": False,
                                                           "wanted": False}}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])

        pos = autotrade.load_state()["position"]
        self.assertAlmostEqual(pos["lots"], 0.005)
        self.assertAlmostEqual(pos["lots_cfg"], 0.01)
        self.assertAlmostEqual(row["lots"], 0.005)
        self.assertTrue(row["partial_fill"])
        self.assertAlmostEqual(row["lots_cfg"], 0.01)

    def test_unknown_volume_falls_back(self):
        """读不到成交量 = 不知道，退回配置手数（旧行为），不标 partial。"""
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=False)
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4001.0,
                                             "order": 7,
                                             "sl_attach": {"ok": True, "sl": 3998.0},
                                             "tp_attach": {"ok": False,
                                                           "wanted": False}}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertAlmostEqual(autotrade.load_state()["position"]["lots"], 0.01)
        self.assertNotIn("partial_fill", row)


class TestStopEventBlocksOpen(TempLedger):
    """F5：停止信号已经 set 时，这一轮绝不能再发单。"""

    def test_set_event_skips_send_open(self):
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True)
        ev = threading.Event()
        ev.set()
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[], stop_ev=ev)
        m_open.assert_not_called()
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "stopped")
        self.assertIsNone(autotrade.load_state().get("position"))

    def test_clear_event_opens_normally(self):
        """没 set 的 Event 不能影响正常下单——否则 CLI/单测全被挡。"""
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True)
        ev = threading.Event()
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4001.0,
                                             "order": -1}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[], stop_ev=ev)
        self.assertEqual(row["kind"], "open")


def _manual(ticket=4980444200, **kw):
    row = {
        "ticket": ticket, "symbol": "XAUUSDc", "direction": -1, "volume": 0.1,
        "price_open": 3869.636, "price_current": 3869.297, "sl": 3876.886,
        "tp": 0.0, "profit": -28.4, "swap": 0.0,
        "open_time": int(time.time()) - 7200, "comment": "",
    }
    row.update(kw)
    return row


class ManualShadowCase(TempLedger):
    def setUp(self):
        super().setUp()
        autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})

    def tearDown(self):
        autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})
        super().tearDown()


class TestShadowDecoupledFromView(ManualShadowCase):
    """F7：影子推进与"给不给面板看"解耦。"""

    def test_sync_runs_even_when_view_hidden(self):
        with mock.patch("live.journal.open_positions", return_value=[_manual()]), \
             mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            view = autotrade._manual_position({"symbol": "XAUUSDc"},
                                              allow_view=False)
        self.assertIsNone(view, "程序持仓时不画手工单面板")
        shadow = autotrade._load_manual_shadow()
        self.assertIn("4980444200", shadow, "但影子必须已经建立")

    def test_sync_settles_close_while_view_hidden(self):
        """程序持仓期间平掉的手工单，也要落 manual_close 行。"""
        with mock.patch("live.journal.open_positions", return_value=[_manual()]), \
             mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            autotrade._manual_position({"symbol": "XAUUSDc"}, allow_view=False)

        # 3 秒缓存会让第二次调用直接返回，必须手动过期——这里要验的正是"下一轮"
        autotrade._MANUAL_POS_CACHE.update({"ts": 0.0, "view": None})
        with mock.patch("live.journal.open_positions", return_value=[]), \
             mock.patch.object(autotrade, "_manual_close_row",
                               return_value={"ts": int(time.time()),
                                             "kind": "manual_close",
                                             "ticket": 4980444200, "pnl": 10.0}):
            autotrade._manual_position({"symbol": "XAUUSDc"}, allow_view=False)

        rows = [r for r in autotrade.read_log() if r.get("kind") == "manual_close"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(autotrade._load_manual_shadow(), {})


class TestShadowSeenTs(ManualShadowCase):
    """F11：持有超过 24 小时的手工单，影子不能被当成僵尸删掉。"""

    def test_seen_ts_refreshed(self):
        key = "4980444200"
        old = int(time.time()) - 25 * 3600
        autotrade._save_manual_shadow({
            key: {"ticket": int(key), "symbol": "XAUUSDc", "direction": -1,
                  "lots": 0.1, "price_open": 3869.636, "opened_at": old,
                  "seen_ts": old, "sl0": 3876.886, "risk0": -100.0},
        })
        with mock.patch("live.journal.open_positions", return_value=[_manual()]), \
             mock.patch.object(autotrade, "_quote", return_value=(3869.297, 3869.537)):
            autotrade._manual_position({"symbol": "XAUUSDc"})

        sh = autotrade._load_manual_shadow().get(key)
        self.assertIsNotNone(sh, "持仓中的条目不能被 24 小时清理删掉")
        self.assertGreater(int(sh["seen_ts"]), old)
        self.assertEqual(sh["sl0"], 3876.886, "风险基线必须原地不动")


class TestSummaryCumulative(TempLedger):
    """F10：累计笔数不能停在"最近 5000 行"。"""

    def test_state_counters_win(self):
        for i in range(3):
            autotrade.append_log({"ts": NOW + i, "kind": "close",
                                  "direction": 1, "pnl": -1.0})
        st = autotrade.load_state()
        st["closed"], st["wins"], st["pnl"] = 900, 400, 12.5
        st["sl_closes"], st["be_closes"] = 120, 30
        autotrade.save_state(st)

        s = autotrade.summary()
        self.assertEqual(s["closed"], 900)
        self.assertEqual(s["wins"], 400)
        self.assertEqual(s["sl_closes"], 120)
        self.assertEqual(s["be_closes"], 30)
        self.assertAlmostEqual(s["hit_rate"], 400 / 900, places=4)

    def test_window_used_when_state_behind(self):
        """直接写流水的脚本/旧账：state 计数器落后时用窗口数。"""
        for i in range(4):
            autotrade.append_log({"ts": NOW + i * 60, "kind": "close",
                                  "direction": 1, "pnl": 1.0})
        s = autotrade.summary()
        self.assertEqual(s["closed"], 4)
        self.assertEqual(s["wins"], 4)


class TestBarsErrorSkip(TempLedger):
    """F12：拿不到 K 线要说出原因。"""

    def test_empty_bars_skip_has_reason(self):
        cfg = autotrade.Cfg(dry_run=True)
        with mock.patch.object(autotrade, "_bars", return_value=[]), \
             mock.patch.object(autotrade, "last_bars_error",
                               return_value="ValueError: 品种不存在"):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("品种不存在", row["reason"])
        self.assertIn("品种不存在", row["bars_error"])

    def test_position_still_gets_exit_gate(self):
        """有持仓时不许因为"没 K 线"跳过出场闸门（止损不依赖 K 线）。"""
        st = autotrade._empty_state()
        st["position"] = {"ticket": 1, "direction": 1, "lots": 0.01,
                          "price_open": 4000.0, "opened_at": NOW - 100,
                          "p_up": 0.6, "plan": "range", "dry_run": False,
                          "sl_broker": 3997.0}
        autotrade.save_state(st)
        with mock.patch.object(autotrade, "_bars", return_value=[]), \
                mock.patch.object(autotrade, "_positions", return_value=[]), \
                mock.patch.object(autotrade, "_close_fills",
                                  return_value={"pnl": -3.0, "price": 3996.0,
                                                "reason": 4, "time": NOW}):
            row = autotrade.tick(autotrade.Cfg(), now=NOW,
                                 quote=(3996.0, 3996.24))
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")


if __name__ == "__main__":
    unittest.main()
