"""脉冲留痕（浮盈速度特征）的判据测试。

被验证的假设来自 tools/measure_burst.py：3 根 M1 内走满 5 个 ATR 的事件，
之后 10 分钟中位回吐 1.27 ATR。这里**只测"有没有记对"**，不测"该不该平仓"——
后者还没到能改行为的样本量，代码里也没有任何消费方。
"""
import unittest
from unittest import mock

from live import autotrade

NOW = 1_700_000_000


def _pos(**over) -> dict:
    """atr_pnl=5.0 → 1 个 ATR 折 5 个账户货币单位，5 ATR 门槛 = 25.0。"""
    pos = {
        "ticket": 1, "direction": 1, "lots": 0.05, "price_open": 4000.0,
        "opened_at": NOW, "atr": 2.0, "atr_pnl": 5.0,
        "peak_pnl": None, "peak_ts": None, "plan": "trend",
    }
    pos.update(over)
    return pos


class TestAtrPnlUnit(unittest.TestCase):
    def test_formula_matches_pnl_at_price(self):
        """1 个 ATR 折成的钱 = 价差 1 ATR × 合约 × 手数 × 账户系数。"""
        with mock.patch.object(autotrade, "_symbol_contract", return_value=100.0), \
                mock.patch.object(autotrade, "_acct_factor", return_value=100.0):
            # 2.0(ATR) × 100(合约) × 0.05(手) × 100(美分户) = 1000
            self.assertEqual(autotrade._atr_pnl_unit(2.0, 0.05, "XAUUSDc"), 1000.0)

    def test_zero_inputs_give_zero(self):
        """算不出来一律 0（消费方按"无数据"降级，不能当"没到过 5 ATR"）。"""
        self.assertEqual(autotrade._atr_pnl_unit(0.0, 0.05, "XAUUSDc"), 0.0)
        self.assertEqual(autotrade._atr_pnl_unit(2.0, 0.0, "XAUUSDc"), 0.0)

    def test_broker_lookup_failure_is_zero_not_crash(self):
        """读不到合约乘数时返回 0，绝不能让开仓路径抛异常。"""
        with mock.patch.object(autotrade, "_symbol_contract",
                               side_effect=RuntimeError("MT5 断了")):
            self.assertEqual(autotrade._atr_pnl_unit(2.0, 0.05, "XAUUSDc"), 0.0)


class TestMarkBurst(unittest.TestCase):
    def test_below_threshold_records_nothing(self):
        pos = _pos()
        autotrade._mark_burst(pos, 14.99, NOW + 60)   # < 3 ATR
        self.assertIsNone(pos.get("t3"))
        self.assertIsNone(pos.get("t5"))

    def test_three_atr_step_is_recorded_separately(self):
        """3 ATR 单独记一档：峰值只有 1~3 ATR 的单子靠它才有速度可算。"""
        pos = _pos()
        autotrade._mark_burst(pos, 15.0, NOW + 120)   # 3 ATR，2 分钟
        self.assertEqual(pos["t3"], 2.0)
        self.assertIsNone(pos.get("t5"))
        autotrade._mark_burst(pos, 25.0, NOW + 300)   # 5 ATR，5 分钟
        self.assertEqual(pos["t5"], 5.0)
        self.assertEqual(pos["t3"], 2.0)              # 3 ATR 那档不被改写

    def test_exactly_at_threshold_counts(self):
        """门槛是 >=，不是 >：5.00 ATR 那一格就该记。"""
        pos = _pos()
        autotrade._mark_burst(pos, 25.0, NOW + 60)
        self.assertEqual(pos["t5"], 1.0)

    def test_records_once_and_never_overwrites(self):
        """t5 回答"多久走满这一段"，不是"峰值在第几分钟"，所以只写第一次。"""
        pos = _pos()
        autotrade._mark_burst(pos, 25.0, NOW + 60)      # 1.0 分
        autotrade._mark_burst(pos, 40.0, NOW + 600)     # 10 分后创新高
        self.assertEqual(pos["t5"], 1.0)

    def test_missing_atr_unit_skips(self):
        pos = _pos(atr_pnl=0.0)
        autotrade._mark_burst(pos, 999.0, NOW + 60)
        self.assertIsNone(pos.get("t5"))

    def test_missing_opened_at_skips(self):
        """旧 state 没有 opened_at：宁可不记，也不要写出一个负数分钟。"""
        pos = _pos(opened_at=0)
        autotrade._mark_burst(pos, 25.0, NOW + 60)
        self.assertIsNone(pos.get("t5"))


class TestTrackPeakIntegration(unittest.TestCase):
    def test_burst_detected_even_when_pnl_already_fell_back(self):
        """升级瞬间的旧持仓：峰值早就过 5 ATR，此刻浮盈已回落。

        用当前浮盈判会永远判不成脉冲，所以必须用峰值判——这是这个改动里
        唯一容易写错的一处。
        """
        pos = _pos(peak_pnl=40.0, peak_ts=NOW + 60)   # 8 ATR 的峰值已记录
        autotrade._track_peak(pos, 10.0, NOW + 120)   # 现在只剩 2 ATR
        self.assertEqual(pos["t5"], 2.0)              # (120-0)/60
        self.assertEqual(pos["t3"], 2.0)              # 两档一起补，都用峰值

    def test_peak_update_still_reports_true_only_when_raised(self):
        """补记脉冲不能改变 _track_peak 的返回值语义（写盘与否靠它）。"""
        pos = _pos()
        self.assertTrue(autotrade._track_peak(pos, 30.0, NOW + 60))
        self.assertFalse(autotrade._track_peak(pos, 20.0, NOW + 120))
        self.assertEqual(pos["peak_pnl"], 30.0)


class TestBurstAlert(unittest.TestCase):
    """弹窗标记只给趋势单：震荡单有券商端硬 tp，到线就走，不需要人插手。"""

    def test_trend_gets_alert(self):
        pos = _pos(plan="trend", ticket=88)
        autotrade._mark_burst(pos, 25.0, NOW + 180)   # 5 ATR，3 分钟
        al = pos.get("burst_alert") or {}
        self.assertEqual(al.get("ticket"), 88)
        self.assertEqual(al.get("t5"), 3.0)
        self.assertEqual(al.get("atr_peak"), 5.0)     # 25 / 5.0
        self.assertEqual(al.get("fp"), 25.0)
        self.assertEqual(al.get("direction"), 1)
        self.assertEqual(al.get("price_open"), 4000.0)

    def test_range_records_but_does_not_alert(self):
        """震荡单照样记 t3/t5（天然对照组），只是不弹窗。"""
        pos = _pos(plan="range")
        autotrade._mark_burst(pos, 25.0, NOW + 60)
        self.assertEqual(pos["t5"], 1.0)
        self.assertNotIn("burst_alert", pos)

    def test_alert_fires_once_and_keeps_original_ts(self):
        """弹过之后又涨到 8 ATR：不能把第二次的数覆盖上去，否则面板会重弹。"""
        pos = _pos(plan="trend")
        autotrade._mark_burst(pos, 25.0, NOW + 60)
        first = dict(pos["burst_alert"])
        autotrade._mark_burst(pos, 40.0, NOW + 600)
        self.assertEqual(pos["burst_alert"], first)

    def test_alert_uses_the_peak_that_triggered_it(self):
        """atr_peak 必须按触发它的那个峰值算，不能读 pos 里还没写入的 peak_pnl。"""
        pos = _pos(plan="trend", peak_pnl=1.0, peak_ts=NOW)
        autotrade._mark_burst(pos, 37.5, NOW + 60)    # 7.5 ATR
        self.assertEqual(pos["burst_alert"]["atr_peak"], 7.5)
        self.assertEqual(pos["peak_pnl"], 1.0)        # 这个函数不写峰值

    def test_returns_true_only_when_five_atr_crossed(self):
        pos = _pos(plan="trend")
        self.assertFalse(autotrade._mark_burst(pos, 15.0, NOW + 60))   # 3 ATR
        self.assertTrue(autotrade._mark_burst(pos, 25.0, NOW + 120))   # 5 ATR
        self.assertFalse(autotrade._mark_burst(pos, 40.0, NOW + 180))  # 已记过


class TestCloseRowFields(unittest.TestCase):
    def test_atr_peak_and_burst_land_in_close_row(self):
        state = autotrade._empty_state()
        pos = _pos(peak_pnl=30.0, peak_ts=NOW + 60, t5=2.5)
        row = autotrade._book_close(state, pos, 12.0, NOW + 300,
                                    source="llm", reason="平")
        self.assertEqual(row["atr_peak"], 6.0)   # 30 / 5.0
        self.assertEqual(row["t5"], 2.5)
        self.assertTrue(row["burst"])

    def test_slow_grind_is_not_burst(self):
        pos = _pos(peak_pnl=30.0, t5=43.8)
        row = autotrade._book_close(autotrade._empty_state(), pos, 12.0,
                                    NOW + 3000, source="llm", reason="平")
        self.assertFalse(row["burst"])

    def test_never_reached_threshold_is_none_not_false(self):
        """没到过 5 ATR：t5=None、burst=False，但 atr_peak 仍有值——
        分析时"没到过"和"没数据"必须能分开。"""
        pos = _pos(peak_pnl=3.0, peak_ts=NOW + 60)
        row = autotrade._book_close(autotrade._empty_state(), pos, 2.0,
                                    NOW + 600, source="llm", reason="平")
        self.assertIsNone(row["t5"])
        self.assertFalse(row["burst"])
        self.assertEqual(row["atr_peak"], 0.6)

    def test_missing_atr_pnl_leaves_atr_peak_empty(self):
        """旧持仓没有 atr_pnl：atr_peak=None 是"无数据"的唯一标记。"""
        pos = _pos(atr_pnl=None, peak_pnl=30.0)
        row = autotrade._book_close(autotrade._empty_state(), pos, 12.0,
                                    NOW + 300, source="llm", reason="平")
        self.assertIsNone(row["atr_peak"])
        self.assertFalse(row["burst"])


class TestIsBurstBoundary(unittest.TestCase):
    def test_three_minutes_is_inclusive(self):
        self.assertTrue(autotrade._is_burst({"t5": 3.0}))
        self.assertFalse(autotrade._is_burst({"t5": 3.01}))
        self.assertFalse(autotrade._is_burst({"t5": None}))
        self.assertFalse(autotrade._is_burst({}))


if __name__ == "__main__":
    unittest.main()
