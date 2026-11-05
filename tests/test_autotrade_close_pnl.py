"""平仓盈亏记账：以券商成交为准，不拿"此刻报价"估。

2026-11-06 实盘那笔 4919144776 是券商端保本止损平的，MT5 账本上
`price=4110.781 / profit=0.00`，而面板和流水记成 **-0.96**。

根因不在成交、在记账：`_realized_pnl` 用 `if total:` 判断"有没有读到券商
成交"，而 0.00 是假值，被当成"没读到"，于是退回用 **48 秒后**的 bid 估算。
保本止损的成交天然就是 0.00，所以这条路径必然算错——不是偶发，是必现。

三条判据：
  1. 有平仓 deal 就照它的 `profit` 记，**0.00 也是结论**；
  2. 没有平仓 deal（只有开仓那条）才退回报价估算；
  3. 出场价同样以成交为准，复盘拿到的价格不能是几十秒后的。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


def _deal(entry: int, price: float, profit: float, reason: int = 4,
          volume: float = 0.01) -> SimpleNamespace:
    return SimpleNamespace(entry=entry, price=price, profit=profit,
                           commission=0.0, swap=0.0, fee=0.0,
                           reason=reason, time=NOW, volume=volume)


def _fake_mt5(deals):
    """替掉 journal._mt5：history_deals_get 返回给定的 deal 列表。"""
    return SimpleNamespace(history_deals_get=lambda **kw: deals)


def _pos(**kw) -> dict:
    base = {"ticket": 501, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "opened_at": NOW - 300, "p_up": 0.62, "entry_reason": "顺势",
            "plan": "trend", "dry_run": False, "peak_pnl": 4.75}
    base.update(kw)
    return base


class TestRealizedPnl(TempLedger):
    def test_breakeven_deal_wins_over_quote(self):
        """成交盈亏 0.00 必须照用——这正是保本止损的常态。

        报价已经跑到 3999.04（-0.96），但它不是成交价：40 秒后程序才在下一轮
        发现仓位没了。照报价记，每一笔保本止损都会变成亏损。
        """
        pos = _pos()
        deals = [_deal(0, 4000.0, 0.0), _deal(1, 4000.0, 0.0)]
        with mock.patch("live.journal._mt5", return_value=_fake_mt5(deals)):
            pnl = autotrade._realized_pnl(autotrade.Cfg(), pos, 3999.04, 3999.28)
        self.assertEqual(pnl, 0.0, "0.00 的成交被当成'读不到'了")

    def test_no_closing_deal_falls_back_to_quote(self):
        """只有开仓那条 deal 时才是"读不到成交"，这时才按报价估。"""
        pos = _pos()
        deals = [_deal(0, 4000.0, 0.0)]
        with mock.patch("live.journal._mt5", return_value=_fake_mt5(deals)):
            pnl = autotrade._realized_pnl(autotrade.Cfg(), pos, 3999.04, 3999.28)
        # 0.01 手 XAUUSD = 1 盎司，-0.96 美元价差 → -0.96
        self.assertAlmostEqual(pnl, -0.96, places=2)

    def test_commission_and_swap_included(self):
        """净盈亏不是只有 profit：佣金、隔夜利息要一起算进去。"""
        pos = _pos()
        d = _deal(1, 4000.0, 2.0)
        d.commission, d.swap = -0.5, -0.2
        with mock.patch("live.journal._mt5",
                        return_value=_fake_mt5([_deal(0, 4000.0, 0.0), d])):
            pnl = autotrade._realized_pnl(autotrade.Cfg(), pos, 4000.0, 4000.24)
        self.assertAlmostEqual(pnl, 1.3, places=2)

    def test_partial_close_weighted_price(self):
        """部分平仓有多条 deal：成交价按量加权，不能只取第一条。"""
        pos = _pos(lots=0.02)
        deals = [_deal(1, 4000.0, 0.0, volume=0.01),
                 _deal(1, 4010.0, 0.0, volume=0.01)]
        with mock.patch("live.journal._mt5", return_value=_fake_mt5(deals)):
            fills = autotrade._close_fills(pos)
        self.assertAlmostEqual(fills["price"], 4005.0, places=3)


class TestCloseBooking(TempLedger):
    def _save(self, **kw):
        state = autotrade._empty_state()
        state["position"] = _pos(**kw)
        autotrade.save_state(state)

    def test_broker_sl_books_real_deal_pnl(self):
        """仓位在终端消失 → 按 deal 记 0.00，而不是按此刻报价记 -0.96。"""
        self._save(be_ok=1, be_sl=4000.0)
        deals = [_deal(0, 4000.0, 0.0), _deal(1, 4000.0, 0.0, reason=4)]
        with mock.patch("live.journal._mt5", return_value=_fake_mt5(deals)):
            row = autotrade.tick(autotrade.Cfg(), now=NOW,
                                 quote=(3999.04, 3999.28),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["source"], "be")
        self.assertEqual(row["pnl"], 0.0)
        self.assertEqual(row["hit"], False, "0 不算赢单")
        self.assertEqual(row["pnl_src"], "deal")
        self.assertAlmostEqual(row["exit_price"], 4000.0, places=3)
        self.assertEqual(autotrade.load_state()["be_closes"], 1)

    def test_missing_deal_is_marked_as_estimate(self):
        """读不到成交就老实标 quote，别让人以为那是账本数字。"""
        self._save(be_ok=1, be_sl=4000.0)
        with mock.patch("live.journal._mt5",
                        return_value=_fake_mt5([_deal(0, 4000.0, 0.0)])):
            row = autotrade.tick(autotrade.Cfg(), now=NOW,
                                 quote=(3999.04, 3999.28),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["pnl_src"], "quote")
        self.assertAlmostEqual(row["pnl"], -0.96, places=2)

    def test_program_close_uses_fill_price(self):
        """程序平仓按回执成交价记，不用发单前的报价（滑点不能被吞掉）。"""
        self._save(peak_pnl=0.0)
        with mock.patch.object(autotrade, "send_close",
                               return_value={"ok": True, "price": 3996.5}):
            row = autotrade.tick(autotrade.Cfg(), now=NOW,
                                 quote=(3996.0, 3996.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 501, "symbol": "XAUUSDm"}])
        self.assertEqual(row["source"], "sl")
        # 按报价算是 -4.00，按成交价是 -3.50：差的就是这 0.5 的滑点
        self.assertAlmostEqual(row["pnl"], -3.5, places=2)
        self.assertEqual(row["pnl_src"], "fill")
        self.assertAlmostEqual(row["exit_price"], 3996.5, places=3)


if __name__ == "__main__":
    unittest.main()
