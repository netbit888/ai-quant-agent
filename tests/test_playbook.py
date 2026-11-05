"""做单台态势带：当前打法 + 「模型 / 我」方向对照（2026-11-05）。

锁住四件最容易退回的事：
1. 打法与持仓无关——空仓也必须给具体文案。原来取 `position.plan`，
   空仓必然显示「–」，而人最需要知道打法的时刻恰恰是空仓准备下单时。
2. 大字承载的是「现在」的状态：超过两个决策间隔必须标过期，
   放大一个 14 分钟前的判定比写成小字更危险。
3. 同一笔单实测会留下 4 条 p_up 相同的流水（signal/open/signal/close），
   不去重就会把一次决策画成四格，历史序列直接失真。
4. 「我」取账本真实成交，盈亏按 position_id 归集，每个数字都要能对账。
"""

from __future__ import annotations

import unittest

from tests._stubs import install_missing_stubs

install_missing_stubs()   # 断链真实 MT5，必须早于 live 的 import

from live import autotrade  # noqa: E402

NOW = 1_790_000_000


def _state(regime: str, ts: int = NOW, **cfg) -> dict:
    return {
        "cfg": {"interval_sec": 60, "symbol": "XAUUSDc", **cfg},
        "last_decision": {"ts": ts, "kind": "entry", "regime": regime,
                          "p_up": 0.44, "act": False},
    }


def _deal(ticket: int, time: int, typ: int, entry: int,
          pos_id: int, profit: float = 0.0,
          symbol: str = "XAUUSDc", magic: int = 0) -> dict:
    return {"ticket": ticket, "time": time, "type": typ, "entry": entry,
            "position_id": pos_id, "profit": profit, "commission": 0.0,
            "swap": 0.0, "fee": 0.0, "volume": 0.01, "symbol": symbol,
            "magic": magic}


class TestPlaybook(unittest.TestCase):
    def test_flat_still_has_a_concrete_playbook(self):
        """空仓（state 里没有 position）不能把「打法」显示成 –。"""
        pb = autotrade.playbook_of(_state("range"), [], now=NOW + 10)
        self.assertEqual(pb["name"], "震荡市")
        self.assertEqual(pb["action"], "低买高卖")
        self.assertNotIn("–", pb["action"])
        self.assertIn("3.25", pb["detail"])
        self.assertIn("1h", pb["detail"])
        self.assertTrue(pb["anchor"])
        self.assertFalse(pb["stale"])

    def test_stale_after_two_intervals(self):
        """两个决策间隔之内是「现在」，之外必须标过期。"""
        fresh = autotrade.playbook_of(_state("range"), [], now=NOW + 119)
        stale = autotrade.playbook_of(_state("range"), [], now=NOW + 121)
        self.assertFalse(fresh["stale"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["age_sec"], 121)

    def test_trend_off_says_no_trade(self):
        """trend_follow=False 是「趋势市不交易」，不能退回震荡打法。"""
        pb = autotrade.playbook_of(
            _state("trend", trend_follow=False), [], now=NOW + 10)
        self.assertEqual(pb["action"], "不交易")
        self.assertIn("trend_follow", pb["detail"])
        self.assertFalse(pb["trend_follow"])

    def test_trend_uses_trend_exit_params_and_drift(self):
        """趋势打法的 tp / 持仓上限走 exit_params，方向取最近一次 drift。"""
        up = autotrade.playbook_of(
            _state("trend"), [{"feat": {"drift": 1.2}}], now=NOW + 10)
        down = autotrade.playbook_of(
            _state("trend"), [{"feat": {"drift": -1.2}}], now=NOW + 10)
        self.assertEqual(up["action"], "顺势做多")
        self.assertEqual(down["action"], "顺势做空")
        self.assertEqual(up["tp"], autotrade.Cfg().tp_min_trend)
        self.assertEqual(up["max_hold_sec"], autotrade.Cfg().max_hold_trend)
        self.assertIn("3h", up["detail"])
        self.assertFalse(up["anchor"])     # 趋势市停用等待锚点
        self.assertEqual(autotrade.playbook_of(
            _state("range"), [], now=NOW + 10)["tp"],
            autotrade.Cfg().tp_min_usd)

    def test_no_regime_is_a_placeholder_not_a_dash(self):
        pb = autotrade.playbook_of(_state(""), [], now=NOW + 10)
        self.assertEqual(pb["name"], "待判定")
        self.assertEqual(pb["action"], "等待首轮判定")
        self.assertNotIn("–", pb["action"])
        self.assertFalse(pb["stale"])


class TestModelVsActual(unittest.TestCase):
    def test_same_decision_does_not_become_four_slots(self):
        """signal / open / signal / close 四行 p_up 相同 = 一轮，不是四格。"""
        rows = [
            {"ts": NOW, "kind": "skip", "p_up": 0.44},
            {"ts": NOW + 60, "kind": "signal", "p_up": 0.62},
            {"ts": NOW + 60 + 17, "kind": "signal", "p_up": 0.62},
            {"ts": NOW + 120, "kind": "skip", "p_up": 0.44},
        ]
        vs = autotrade.model_vs_actual(rows, {}, interval_sec=60)
        self.assertEqual(len(vs["slots"]), 3)
        self.assertEqual([s["p_up"] for s in vs["slots"]], [0.44, 0.62, 0.44])

    def test_against_counts_real_trades_and_realized_pnl(self):
        """模型看空、账户却做多 = 逆模型；盈亏按 position_id 对账。"""
        rows = [{"ts": NOW + i * 60, "kind": "skip", "p_up": 0.44}
                for i in range(4)]
        deals = {
            1: _deal(1, NOW + 2 * 60 + 3, 0, 0, 900, magic=0),      # 手动开多
            2: _deal(2, NOW + 2 * 60 + 90, 1, 1, 900, profit=-7.51),
        }
        vs = autotrade.model_vs_actual(
            rows, deals, interval_sec=60, symbol="XAUUSDc")
        self.assertEqual(vs["trades"], 1)
        self.assertEqual(vs["against"], 1)
        self.assertEqual(vs["net"], -7.51)
        hit = vs["slots"][2]
        self.assertEqual(hit["actual_dir"], 1)
        self.assertEqual(hit["magic"], 0)
        self.assertTrue(hit["against"])

    def test_agreeing_trade_is_not_against(self):
        rows = [{"ts": NOW, "kind": "signal", "p_up": 0.62}]
        deals = {
            1: _deal(1, NOW + 2, 0, 0, 901, magic=20261022),   # 程序开多
            2: _deal(2, NOW + 40, 1, 1, 901, profit=2.2),
        }
        vs = autotrade.model_vs_actual(
            rows, deals, interval_sec=60, symbol="XAUUSDc")
        self.assertEqual(vs["against"], 0)
        self.assertEqual(vs["net"], 0)
        self.assertEqual(vs["slots"][0]["pnl"], 2.2)

    def test_other_symbol_and_balance_rows_are_ignored(self):
        rows = [{"ts": NOW, "kind": "skip", "p_up": 0.44}]
        deals = {
            1: _deal(1, NOW + 2, 0, 0, 902, symbol="EURUSD"),
            2: _deal(2, NOW + 2, 2, 0, 0, symbol=""),           # 入金
        }
        vs = autotrade.model_vs_actual(
            rows, deals, interval_sec=60, symbol="XAUUSDc")
        self.assertEqual(vs["trades"], 0)
        self.assertEqual(vs["slots"][0]["actual_dir"], 0)

    def test_short_window_keeps_real_slot_count(self):
        """只有两轮就返回两格：缺的格位由前端留空，不许自动铺开。"""
        rows = [{"ts": NOW, "kind": "skip", "p_up": 0.5},
                {"ts": NOW + 60, "kind": "skip", "p_up": 0.45}]
        vs = autotrade.model_vs_actual(rows, {}, interval_sec=60)
        self.assertEqual(len(vs["slots"]), 2)
        self.assertEqual(vs["slots"][0]["model_dir"], 0)


if __name__ == "__main__":
    unittest.main()
