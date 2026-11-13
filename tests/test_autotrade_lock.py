"""脉冲锁利（`_lock_step`）的判据测试。

规则：趋势单浮盈**首次走满 5 个 ATR** 之后，把券商端 SL 从开仓价抬到
「峰值 − 1.5 个 ATR」，峰值再抬高就跟着抬。只改券商那条线，不新增平仓路径。

依据 `tools/burst_lock.py`（50 天 M1，5 ATR 档）：L=1.5 时 77% 会被打掉、
其中只有 6% 打掉后价格又创新高；L=0.5 砍错 16%、L=3.0 锁住的少一截。

这里测的是**算得对不对、幂等有没有破**，不测"这条规则该不该存在"。
"""
import unittest
from unittest import mock

from live import autotrade

NOW = 1_700_000_000


def _pos(**over) -> dict:
    """atr=2.0、atr_pnl=5.0 → 1 个 ATR = 2.0 美元 = 5 个账户货币单位。

    所以 peak 25.0 = 5 个 ATR，锁利线 = 峰值 5 ATR − 1.5 ATR = 3.5 ATR
    = 7.0 美元 → 多单 4007.0 / 空单 3993.0。
    """
    pos = {
        "ticket": 1, "direction": 1, "lots": 0.05, "price_open": 4000.0,
        "opened_at": NOW, "atr": 2.0, "atr_pnl": 5.0,
        "peak_pnl": 25.0, "peak_ts": NOW + 60, "plan": "trend",
        "t3": 2.0, "t5": 2.5, "assist": False,
    }
    pos.update(over)
    return pos


class TestLockTarget(unittest.TestCase):
    def test_long_target_is_peak_minus_one_and_half_atr(self):
        """峰值 5 ATR、L=1.5 → 锁在 3.5 ATR = 7.0 美元上方。"""
        self.assertAlmostEqual(autotrade._lock_target(_pos()), 4007.0, places=3)

    def test_short_target_is_below_entry(self):
        self.assertAlmostEqual(
            autotrade._lock_target(_pos(direction=-1)), 3993.0, places=3)

    def test_peak_exactly_at_l_next_to_nothing(self):
        """峰值刚好等于 L 个 ATR：锁出来是开仓价，没有利润可锁 → 不挂。"""
        # peak 7.5 = 1.5 ATR
        self.assertIsNone(autotrade._lock_target(_pos(peak_pnl=7.5)))

    def test_below_l_never_returns_a_losing_line(self):
        """峰值连 L 个 ATR 都不到时，公式会给一条**亏钱**的线，必须返回 None。

        这是最容易写错的一处：不判 move>0 的话，多单会拿到一个比开仓价
        还低的 SL（等于把止损往下松），比不做保护糟得多。
        """
        self.assertIsNone(autotrade._lock_target(_pos(peak_pnl=5.0)))   # 1 ATR

    def test_missing_inputs_give_none(self):
        """算不出来就不挂，不猜。旧持仓没有 atr_pnl 是常态。"""
        self.assertIsNone(autotrade._lock_target(_pos(atr_pnl=0.0)))
        self.assertIsNone(autotrade._lock_target(_pos(atr=0.0)))
        self.assertIsNone(autotrade._lock_target(_pos(price_open=0.0)))
        self.assertIsNone(autotrade._lock_target(_pos(direction=0)))
        self.assertIsNone(autotrade._lock_target(_pos(peak_pnl=None)))

    def test_follows_the_peak_upward(self):
        """峰值抬到 8 ATR：锁利线也跟着抬到 6.5 ATR = 13.0 美元。"""
        self.assertAlmostEqual(
            autotrade._lock_target(_pos(peak_pnl=40.0)), 4013.0, places=3)


class TestLockApplies(unittest.TestCase):
    def test_range_never_gets_locked(self):
        """震荡单有券商端硬 tp 3.25，加一条锁利线只会跟它打架。"""
        self.assertFalse(autotrade._lock_applies(_pos(plan="range")))

    def test_needs_t5(self):
        """没到过 5 ATR（t5=None）就没有脉冲，不挂。"""
        self.assertFalse(autotrade._lock_applies(_pos(t5=None)))

    def test_slow_burst_still_gets_locked(self):
        """4 分钟才走满 5 ATR 也要保护：3 分钟只是统计标签，不是保护门槛。

        这正是 `_lock_applies` 用 t5 是否为 None、而不是用 `_is_burst` 的原因。
        """
        self.assertTrue(autotrade._lock_applies(_pos(t5=4.2)))

    def test_trend_with_t5(self):
        self.assertTrue(autotrade._lock_applies(_pos()))

    def test_assist_mode_never_gets_locked(self):
        """辅助模式只弹窗：人正在看屏，程序不该替他把 SL 抬掉。

        这是 2026-11-13 用户定的分工——辅助模式下「锁利」这条规则整体不生效，
        抬不抬线由人在终端上决定或切到全自动。
        """
        self.assertFalse(autotrade._lock_applies(_pos(assist=True)))

    def test_missing_assist_means_auto(self):
        """升级瞬间的旧持仓没有 assist 字段：按全自动处理，别把保护抽走。

        与 `burst_alert` 的缺省方向一致（缺字段 → 不弹窗 → 全自动那一侧）。
        """
        pos = _pos()
        pos.pop("assist")
        self.assertTrue(autotrade._lock_applies(pos))


class TestLockStep(unittest.TestCase):
    def _cfg(self, **over):
        cfg = autotrade.Cfg()
        for k, v in over.items():
            setattr(cfg, k, v)
        return cfg

    def test_successful_modify_records_everything(self):
        pos = _pos()
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4007.0}) as m:
            row = autotrade._lock_step(self._cfg(), pos, NOW + 120)
        m.assert_called_once()
        self.assertEqual(m.call_args[0][1], 1)          # ticket
        self.assertAlmostEqual(m.call_args[0][2], 4007.0, places=3)
        self.assertEqual(pos["lock_ok"], 1)
        self.assertAlmostEqual(pos["lock_sl"], 4007.0, places=3)
        self.assertEqual(pos["lock_ts"], NOW + 120)
        # 券商端那条线现在就是锁利价，面板只该读 sl_broker 一处
        self.assertAlmostEqual(pos["sl_broker"], 4007.0, places=3)
        self.assertEqual(row["kind"], "lock")
        self.assertEqual(row["mode"], "broker")
        self.assertAlmostEqual(row["sl"], 4007.0, places=3)

    def test_assist_mode_does_not_send_any_modify(self):
        """辅助模式连改单都不发——不是发出去再回退，是压根不进这一步。

        用 mock 断言 `send_modify_sl` 未被调用：只判返回值的话，写错成
        "发了然后 return None" 也能过，那已经是动过券商的单了。
        """
        pos = _pos(assist=True)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        m.assert_not_called()
        self.assertNotIn("lock_ok", pos)
        self.assertNotIn("lock_try_ts", pos)

    def test_skip_when_target_not_a_half_atr_above_current(self):
        """峰值只抬高 0.3 ATR：不值得再抢一次 MT5 锁（会把快查挤成 5 秒）。"""
        # peak 26.5 = 5.3 ATR → 目标 4007.6，比 4007.0 只高 0.6 美元 < 0.5*2
        pos = _pos(lock_sl=4007.0, peak_pnl=26.0)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        m.assert_not_called()

    def test_modify_again_when_peak_moves_up_enough(self):
        """峰值从 5 ATR 抬到 8 ATR：目标高了 6 美元，必须重挂。"""
        pos = _pos(lock_sl=4007.0, peak_pnl=40.0)
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": True, "price": 4013.0}):
            row = autotrade._lock_step(self._cfg(), pos, NOW + 300)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(pos["lock_sl"], 4013.0, places=3)

    def test_busy_is_not_a_failure_and_does_not_eat_the_retry_window(self):
        """终端忙这一格不算失败：不写 lock_err、不消费重试节流。"""
        pos = _pos()
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": False, "busy": True}):
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        self.assertNotIn("lock_ok", pos)
        self.assertNotIn("lock_err", pos)
        self.assertNotIn("lock_try_ts", pos)

    def test_failure_records_reason_without_polluting_the_log(self):
        """改单失败（价格已回落到目标线下方）只记原因，不往流水写一行。"""
        pos = _pos()
        with mock.patch.object(autotrade, "send_modify_sl",
                               return_value={"ok": False, "reason": "止损价不低于现价"}):
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        self.assertEqual(pos["lock_err"], "止损价不低于现价")
        self.assertNotIn("lock_ok", pos)
        self.assertEqual(pos["lock_try_ts"], NOW + 120)

    def test_retry_throttle(self):
        """20 秒内不重复改单（失败后每格都锤券商是不行的）。"""
        pos = _pos(lock_try_ts=NOW + 110)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        m.assert_not_called()

    def test_paper_position_only_books(self):
        """纸面单券商那边没有这一笔：只记账，不发改单请求。"""
        pos = _pos(dry_run=True)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            row = autotrade._lock_step(self._cfg(), pos, NOW + 120)
        m.assert_not_called()
        self.assertEqual(row["mode"], "paper")
        self.assertEqual(pos["lock_mode"], "paper")
        self.assertAlmostEqual(pos["lock_sl"], 4007.0, places=3)
        self.assertNotIn("lock_ok", pos)      # 没真挂上，source 不能判成 lock

    def test_switch_off_does_nothing(self):
        pos = _pos()
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(
                autotrade._lock_step(self._cfg(lock_profit=False), pos, NOW + 120))
        m.assert_not_called()
        self.assertNotIn("lock_sl", pos)

    def test_no_op_when_t5_missing(self):
        pos = _pos(t5=None)
        with mock.patch.object(autotrade, "send_modify_sl") as m:
            self.assertIsNone(autotrade._lock_step(self._cfg(), pos, NOW + 120))
        m.assert_not_called()


class TestSourceAttribution(unittest.TestCase):
    """锁利单必须记成 `lock`，不能混进 be——两者的期望完全不同。"""

    def test_lock_wins_over_be(self):
        """lock 在 be 之后挂、位置更高，两个标记同时存在时必须判 lock。

        判成 be 的话，锁利单会进 be_closes 和「保本止损」那一桶，而 be 的期望
        天然接近 0（抬到开仓价）——那会把这条规则的效果彻底抹平。
        """
        pos = {"be_ok": 1, "be_sl": 4002.0, "lock_ok": 1, "lock_sl": 4007.0,
               "sl_broker": 4007.0}
        src, why = autotrade._gone_source(pos, 4)
        self.assertEqual(src, "lock")
        self.assertIn("4007", why)

    def test_be_still_wins_when_no_lock(self):
        pos = {"be_ok": 1, "be_sl": 4002.0, "sl_broker": 4002.0}
        self.assertEqual(autotrade._gone_source(pos, 4)[0], "be")

    def test_lock_recognized_with_missing_receipt(self):
        """抬过的线被打掉时 profit 常恰好 0、回执还没落：reason=None 也认。"""
        pos = {"lock_ok": 1, "lock_sl": 4007.0, "sl_broker": 4007.0}
        self.assertEqual(autotrade._gone_source(pos, None)[0], "lock")

    def test_paper_lock_is_not_recorded_as_lock(self):
        """纸面单只是记账，没真挂上券商线——不能因此把人工平仓记成锁利。"""
        pos = {"lock_mode": "paper", "lock_sl": 4007.0}
        self.assertEqual(autotrade._gone_source(pos, 4)[0], "external")

    def test_lock_is_a_forced_exit_source(self):
        self.assertIn("lock", autotrade._FORCED_EXIT_SOURCES)


class TestLocalFallback(unittest.TestCase):
    """券商端挂不上时，浮盈跌破锁定值由程序平——否则"锁利"只是文档里的一句话。"""

    def _cfg(self, **over):
        cfg = autotrade.Cfg()
        for k, v in over.items():
            setattr(cfg, k, v)
        return cfg

    def test_fires_when_tried_but_not_placed(self):
        """峰值 5 ATR（25）= 锁定值 3.5 ATR（17.5）；浮盈跌破 17.5 就该平。"""
        pos = _pos(lock_try_ts=NOW + 120, lock_err="止损价不低于现价")
        self.assertFalse(autotrade._lock_local_hit(self._cfg(), pos, 18.0))
        self.assertTrue(autotrade._lock_local_hit(self._cfg(), pos, 17.0))

    def test_never_fires_when_broker_line_is_on(self):
        """挂上了就交给券商：两道同时生效 = 谁先到谁赢，分不清是谁平的。"""
        pos = _pos(lock_ok=1, lock_sl=4007.0, lock_try_ts=NOW + 120)
        self.assertFalse(autotrade._lock_local_hit(self._cfg(), pos, 1.0))

    def test_never_fires_before_any_modify_attempt(self):
        """升级瞬间的旧持仓：t5 是刚补记的，没试过挂单，**不许**当场平掉。

        这是这个兜底最危险的一处：只按"脉冲成立 + 浮盈跌破"判，会把一笔
        峰值 6 ATR、现在 4.5 ATR 的旧持仓在升级那一格直接平掉。
        """
        pos = _pos(peak_pnl=30.0)          # 6 ATR，锁定值 4.5 ATR
        self.assertFalse(autotrade._lock_local_hit(self._cfg(), pos, 22.0))

    def test_paper_position_not_handled_here(self):
        """纸面单没有券商仓，程序代平无从谈起（由 _exit_gate 照常判）。"""
        pos = _pos(lock_mode="paper", lock_try_ts=NOW + 120)
        self.assertFalse(autotrade._lock_local_hit(self._cfg(), pos, 1.0))

    def test_assist_mode_does_not_take_over(self):
        """辅助模式连兜底也不接管：线从来没挂过，就不该由程序代平。

        否则会出现最难解释的一种成交——弹窗上写着"程序不会平仓"，几秒后
        程序自己平了（因为 `lock_try_ts` 是升级前那套流程留下的）。
        """
        pos = _pos(assist=True, lock_try_ts=NOW + 120)
        self.assertFalse(autotrade._lock_local_hit(self._cfg(), pos, 1.0))

    def test_switch_off(self):
        pos = _pos(lock_try_ts=NOW + 120)
        self.assertFalse(
            autotrade._lock_local_hit(self._cfg(lock_profit=False), pos, 1.0))

    def test_gate_returns_lock_local_as_source(self):
        cfg = autotrade.Cfg()
        gate = autotrade._exit_gate(cfg, 300, 17.0, "trend", lock_local=True)
        self.assertEqual(gate["source"], "lock_local")

    def test_hard_stop_still_wins(self):
        """浮亏 -5 的单子：锁利兜底和硬止损同时为真，必须记成 sl（实情）。"""
        cfg = autotrade.Cfg()
        gate = autotrade._exit_gate(cfg, 300, -5.0, "trend", lock_local=True)
        self.assertEqual(gate["source"], "sl")

    def test_lock_local_ranks_before_be_local(self):
        """两条兜底同时成立时必须判 lock_local：它的线更高，是先被破的那条。"""
        cfg = autotrade.Cfg()
        gate = autotrade._exit_gate(cfg, 300, 5.0, "trend",
                                    be_local=True, lock_local=True)
        self.assertEqual(gate["source"], "lock_local")


if __name__ == "__main__":
    unittest.main()
