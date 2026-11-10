"""退出确认里的「有几笔是裸的」必须来自券商端实际状态。

为什么值得单独钉
----------------
弹窗那句文案是用户**做决定**的依据：「选否」到底等于裸奔还是仍有保护。
早先写死「本策略不挂止盈止损」，而 `send_open` 现在成交后会把硬止损挂到
券商端（`_attach_entry_sl`，只不挂 TP）。于是这句话从"保守"变成了"错"——
用户以为退出就会失去全部保护，可能因此去点「先平仓」，白白平掉一笔本来
有止损的单。

这里只测 `_quit_preview()` 的两个计数，不起窗口、不连 MT5、不下单：
positions 的 `sl` 字段（0 = 没挂）是唯一判据，不看日志里的 `sl_broker`。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from tools import dashboard


def _pos(ticket: int, sl: float, profit: float = 0.0) -> dict:
    return {"ticket": ticket, "symbol": "XAUUSDc", "sl": sl, "tp": 0.0,
            "profit": profit, "type": 0}


class QuitPreviewCase(unittest.TestCase):

    def _run(self, positions, state=None):
        with mock.patch("live.journal.open_positions", return_value=positions), \
             mock.patch("live.autotrade.load_state", return_value=state or {}):
            return dashboard._quit_preview()

    def test_mixed_book_reports_bare_count(self):
        """一笔挂上了、一笔没挂——两种都要报出来，别一律说"仍有止损"。"""
        p = self._run([_pos(1, 4046.5, -1.2), _pos(2, 0.0, 3.4)])
        self.assertTrue(p["mt5"])
        self.assertEqual(p["count"], 2)
        self.assertEqual(p["sl_count"], 1)
        self.assertEqual(p["bare_count"], 1)
        self.assertAlmostEqual(p["float_pnl"], 2.2)

    def test_all_protected(self):
        p = self._run([_pos(1, 4046.5), _pos(2, 4041.0)])
        self.assertEqual(p["sl_count"], 2)
        self.assertEqual(p["bare_count"], 0)

    def test_missing_sl_field_counts_as_bare(self):
        """老终端/异常持仓可能干脆没有 sl 键：按裸的算，宁可多提醒。"""
        p = self._run([{"ticket": 3, "profit": 0.0}])
        self.assertEqual(p["bare_count"], 1)

    def test_no_positions(self):
        p = self._run([])
        self.assertEqual(p["count"], 0)
        self.assertEqual(p["bare_count"], 0)

    def test_mt5_failure_does_not_fake_protection(self):
        """读不到持仓时不能报 bare_count=0 让弹窗说"止损仍在"——那是假保证。"""
        with mock.patch("live.journal.open_positions",
                        side_effect=RuntimeError("mt5 offline")), \
             mock.patch("live.autotrade.load_state", return_value={}):
            p = dashboard._quit_preview()
        self.assertFalse(p["mt5"])
        self.assertIn("mt5 offline", p["error"])
        self.assertEqual(p["bare_count"], 0)
        self.assertEqual(p["count"], 0)


if __name__ == "__main__":
    unittest.main()
