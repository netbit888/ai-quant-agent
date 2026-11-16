"""手工单在场时，判断不能停（2026-11-16）。

实盘：用户在 MT5 自己开了一笔，做单台辅助模式从那一刻起就哑了——流水每轮
一条 `skip 品种上已有持仓，不叠加`，`last_decision` 冻在上一次，模型一次都
没调。根因是入场前的闸门把「实盘上有仓」当成「不必判断」：

    ok, why = can_open(state, now, bool(live_positions), cfg)

而 `can_open` 里 `has_position=True` 直接返回 False，于是整轮在调模型之前就
结束了。手工单不归程序管（只读展示，见 test_autotrade_manual_pos），但它
不该让程序闭嘴——判断、理由、特征照给，能不能真下单由 send_open 之前那道
`live_positions` 闸门挡。

这个文件钉四条：
  1. 有手工单时**仍然调模型**（回归这条就死）。
  2. 模型真想开 -> skip 且 `veto=manual_position`，`send_open` 一次都不许调。
  3. 模型说做不做是它自己的事：`act=false` 那轮不被贴上 manual_position。
  4. 辅助模式不弹卡：弹了也只能在 90 秒后被作废，白让人盯屏。
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_up

from live import autotrade


def _manual(**kw):
    """实盘里的一笔手工持仓，字段照 MT5 `positions_get` 的形状。"""
    row = {
        "ticket": 4980599921,
        "symbol": "XAUUSDc",
        "direction": 1,
        "volume": 0.1,
        "price_open": 3871.988,
        "price_current": 3870.569,
        "sl": 3865.788,
        "tp": 0.0,
        "profit": -14.19,
        "swap": 0.0,
        "open_time": NOW,
        "comment": "",
    }
    row.update(kw)
    return row


class ManualEntryTest(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(assist=False, dry_run=False, interval_sec=60,
                                 cooldown_sec=30)

    def _tick(self, act=True, p_up=0.72, reason="结构支持",
              positions=None, cfg=None):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": act, "p_up": p_up,
                                             "reason": reason,
                                             "source": "llm"}) as m_entry, \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4000.24,
                                             "order": 555}) as m_open:
            row = autotrade.tick(cfg or self.cfg, now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up(),
                                 positions=positions if positions is not None else [])
        return row, m_entry, m_open

    # ---- 1. 不再哑火 ---------------------------------------------------
    def test_manual_hold_still_asks_the_model(self):
        row, m_entry, _ = self._tick(positions=[_manual()])
        m_entry.assert_called_once()          # 核心回归：这一行以前不会执行
        self.assertEqual(row["kind"], "skip")

    # ---- 2. 判断照给，但绝不叠加 ---------------------------------------
    def test_model_wants_long_but_manual_hold_blocks_open(self):
        row, _, m_open = self._tick(positions=[_manual()])
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "manual_position")
        self.assertEqual(row["direction"], 1)
        self.assertIn("手工持仓", row["reason"])
        m_open.assert_not_called()

        ld = autotrade.load_state().get("last_decision") or {}
        # 面板要能看到「它现在怎么想」：判断落盘了，才不是假的哑火
        self.assertEqual(ld.get("act"), True)
        self.assertEqual(ld.get("veto"), "manual_position")
        self.assertEqual(ld.get("manual_hold"), 1)

    # ---- 3. 它自己说不做，就不该背 manual_position 这个锅 --------------
    def test_model_says_no_is_not_labelled_manual(self):
        row, m_entry, _ = self._tick(act=False, p_up=0.52, reason="贴上沿不追",
                                     positions=[_manual()])
        m_entry.assert_called_once()
        self.assertEqual(row["kind"], "skip")
        self.assertNotIn("veto", row)
        self.assertIn("贴上沿不追", row["reason"])

    # ---- 4. 辅助模式不弹一张注定作废的卡 -------------------------------
    def test_assist_does_not_pop_when_manual_hold_exists(self):
        cfg = autotrade.Cfg(assist=True, dry_run=False, interval_sec=60,
                            cooldown_sec=30)
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.72,
                                             "reason": "结构支持",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human") as m_wait, \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[_manual()])
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "manual_position")
        m_wait.assert_not_called()            # 没弹卡
        m_open.assert_not_called()
        self.assertIsNone(autotrade.load_state().get("pending_signal"))

    # ---- 5. 空仓照常开：改动不能把正常路径也堵了 -----------------------
    def test_no_manual_hold_opens_as_before(self):
        row, _, m_open = self._tick(positions=[])
        self.assertEqual(row["kind"], "open")
        m_open.assert_called_once()

    # ---- 6. 程序自己的仓仍走持仓分支，不在空仓分支里调入场模型 ---------
    def test_own_position_does_not_enter_entry_branch(self):
        st = autotrade.load_state()
        st["position"] = {"ticket": 555, "direction": 1, "lots": 0.1,
                          "price_open": 4000.24, "opened_at": NOW - 60,
                          "p_up": 0.7, "plan": "range", "atr": 1.0,
                          "atr_pnl": 10.0, "dry_run": False,
                          "sl_broker": 0.0, "tp_broker": 0.0}
        autotrade.save_state(st)
        live = [_manual(ticket=555, volume=0.1, price_open=4000.24)]
        with mock.patch.object(autotrade, "ask_entry") as m_entry, \
             mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": False,
                                             "reason": "继续持有",
                                             "source": "llm"}):
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=live)
        m_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
