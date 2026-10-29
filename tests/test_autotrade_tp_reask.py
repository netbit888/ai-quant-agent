"""到过止盈门槛后的复核节拍（2026-10-29）。

起因是一笔真实持仓（空 0.01@4027.164，plan=trend）：
  浮盈摸到 peak=+7.39（≥ tp_min_trend=7.00），tp_wake 把模型叫来问过一次，
  模型说继续持有；随后回落到 +6.08，_exit_gate 立刻改口
  「未达止盈门槛 7.00，本轮不调模型」——**这笔单从此再没人看过**。
  唯一的重复唤醒是 _stale_hit，而它要求 fp < tp×0.6 = 4.2，
  于是 4.2~7.0 整段是静默区：吐光利润也不问一句。

四条要钉死的东西：
  1. **门槛不是单程票**：到过一次之后，fp 在门槛下方也照样按 tp_reask_sec
     把决定交回模型；
  2. **节流不是静默**：tp_left>0 只代表"这一分钟刚问过"，不是永久闭嘴；
  3. **止损优先**：这条规则只决定"要不要问模型"，绝不能绕过 sl / be_local /
     timeout（它们返回的是 close=True，和本规则的 None 是两码事）；
  4. **快查不插队**：已过门槛的单子不再因 fp≥tp 提前唤醒，否则浮盈稳定在
     门槛上方会变成每 2~5 秒问一次模型，把 60 秒的判断做成 2 秒的骚扰。
"""

from __future__ import annotations

import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


def _pos(**kw) -> dict:
    base = {"ticket": 601, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "opened_at": NOW - 60, "p_up": 0.6, "entry_reason": "顺势",
            "plan": "trend", "dry_run": False}
    base.update(kw)
    return base


# --- 判定函数本身 ---------------------------------------------------------

class TestTpReachedStep(unittest.TestCase):
    def test_below_threshold_does_not_mark(self):
        """没到过门槛的单子：一个字都不该变，还是 tp_wait 静默。"""
        cfg = autotrade.Cfg()
        pos = _pos(peak_pnl=6.0)
        self.assertEqual(autotrade._tp_reached_step(cfg, pos, 6.0, NOW), (False, 0))
        self.assertNotIn("tp_reached_ts", pos)

    def test_reaching_threshold_marks_and_asks_now(self):
        """首次到门槛：记标记 + 立刻问（tp_left=0，没有节流）。"""
        cfg = autotrade.Cfg()
        pos = _pos()
        self.assertEqual(autotrade._tp_reached_step(cfg, pos, 7.39, NOW), (True, 0))
        self.assertEqual(pos["tp_reached_ts"], NOW)

    def test_falling_back_still_asks_after_interval(self):
        """**这条是本次改动的核心**：回落到门槛以内，60 秒后照样问。"""
        cfg = autotrade.Cfg()
        pos = _pos(tp_reached_ts=NOW - 600, tp_ask_ts=NOW - 60)
        seen, left = autotrade._tp_reached_step(cfg, pos, 6.08, NOW)
        self.assertTrue(seen)
        self.assertEqual(left, 0)

    def test_throttled_within_the_minute(self):
        """刚问过 20 秒 → 这一轮不问，但只说"还有 40 秒"，不是永久闭嘴。"""
        cfg = autotrade.Cfg()
        pos = _pos(tp_reached_ts=NOW - 600, tp_ask_ts=NOW - 20)
        self.assertEqual(autotrade._tp_reached_step(cfg, pos, 6.08, NOW),
                         (True, 40))

    def test_switch_off_restores_old_behaviour(self):
        """tp_reask_sec=0 = 关掉，连标记都不写，回到"单程票"。"""
        cfg = autotrade.Cfg(tp_reask_sec=0)
        pos = _pos()
        self.assertEqual(autotrade._tp_reached_step(cfg, pos, 9.0, NOW), (False, 0))
        self.assertNotIn("tp_reached_ts", pos)

    def test_threshold_follows_the_plan(self):
        """门槛按这一笔的 plan 取：趋势 7.0，震荡 3.25。同一个浮盈两种结论。"""
        cfg = autotrade.Cfg()
        self.assertEqual(autotrade._tp_reached_step(cfg, _pos(plan="trend"),
                                                    4.0, NOW), (False, 0))
        self.assertEqual(autotrade._tp_reached_step(cfg, _pos(plan="range"),
                                                    4.0, NOW), (True, 0))

    def test_old_state_uses_peak_as_fallback(self):
        """升级瞬间手里拿着一笔已经到过门槛的单子（旧 state 没标记）：
        用 peak_pnl 补记，否则这个洞只在新开的单上被堵住。"""
        cfg = autotrade.Cfg()
        pos = _pos(peak_pnl=7.8)
        self.assertEqual(autotrade._tp_reached_step(cfg, pos, 6.08, NOW), (True, 0))
        self.assertEqual(pos["tp_reached_ts"], NOW)

    def test_no_position(self):
        cfg = autotrade.Cfg()
        self.assertEqual(autotrade._tp_reached_step(cfg, None, 9.0, NOW), (False, 0))


# --- 闸门 ---------------------------------------------------------------

class TestTpRecheckGate(unittest.TestCase):
    def test_below_threshold_without_history_stays_quiet(self):
        """从没到过门槛 → 老行为不变：tp_wait，不调模型。"""
        gate = autotrade._exit_gate(autotrade.Cfg(), 60, 6.08, "trend")
        self.assertEqual(gate["source"], "tp_wait")
        self.assertIn("本轮不调模型", gate["reason"])

    def test_seen_and_due_hands_over_to_model(self):
        gate = autotrade._exit_gate(autotrade.Cfg(), 60, 6.08, "trend",
                                    tp_seen=True, tp_left=0)
        self.assertIsNone(gate)

    def test_seen_but_not_due_reports_recheck(self):
        """节流窗口内：给一句能对账的理由，而不是"未达门槛"。"""
        gate = autotrade._exit_gate(autotrade.Cfg(), 60, 6.08, "trend",
                                    tp_seen=True, tp_left=40)
        self.assertFalse(gate["close"])
        self.assertEqual(gate["source"], "tp_recheck")
        self.assertIn("40s 后复核", gate["reason"])
        self.assertNotIn("本轮不调模型", gate["reason"])

    def test_above_threshold_always_asks(self):
        gate = autotrade._exit_gate(autotrade.Cfg(), 60, 7.2, "trend",
                                    tp_seen=True, tp_left=40)
        self.assertIsNone(gate)

    def test_stop_loss_still_wins(self):
        """止损必须排在这条规则前面：它只决定"要不要问"，不能压住 close=True。"""
        gate = autotrade._exit_gate(autotrade.Cfg(), 60, -3.5, "trend",
                                    tp_seen=True, tp_left=0)
        self.assertTrue(gate["close"])
        self.assertEqual(gate["source"], "sl")

    def test_timeout_still_wins(self):
        gate = autotrade._exit_gate(autotrade.Cfg(), 10_900, 6.08, "trend",
                                    tp_seen=True, tp_left=40)
        self.assertTrue(gate["close"])
        self.assertEqual(gate["source"], "timeout")


# --- 快查循环 -----------------------------------------------------------

class TestTpRecheckWait(TempLedger):
    class _Clock:
        def __init__(self, t: float = 1_000_000.0):
            self.t = t
            self.sleeps: list[float] = []

        def time(self) -> float:
            return self.t

        def sleep(self, sec: float) -> None:
            self.sleeps.append(round(sec, 2))
            self.t += sec

    def _position(self, **kw) -> dict:
        state = autotrade._empty_state()
        pos = {"ticket": -1, "direction": 1, "lots": 0.01,
               "price_open": 4000.0, "opened_at": NOW - 60,
               "p_up": 0.6, "entry_reason": "顺势", "dry_run": True}
        pos.update(kw)
        state["position"] = pos
        autotrade.save_state(state)
        return pos

    def test_first_cross_still_wakes_immediately(self):
        """首次穿越照旧：≤5 秒就叫醒，不能因为新规则把首穿也拖到 60 秒。"""
        self._position(plan="trend")
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4007.5, 4007.74))
        self.assertLessEqual(clock.t - 1_000_000.0, 5)

    def test_after_marked_no_longer_jumps_the_queue(self):
        """已过门槛、浮盈仍在门槛上方 → 不再每格叫醒，睡满整轮（分钟节拍）。"""
        self._position(plan="trend", tp_reached_ts=NOW - 300, tp_ask_ts=NOW - 30)
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4007.5, 4007.74))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)

    def test_trend_threshold_is_seven_not_three_twentyfive(self):
        """快查的门槛跟着 plan 走。旧的固定 3.25 会让趋势单在 4.0 就被叫醒，
        醒来又撞上 tp_wait → tick 空转，每 2 秒白算一次 K 线。"""
        self._position(plan="trend")
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4004.2, 4004.44))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)


class TestExitPromptPeak(unittest.TestCase):
    """峰值必须每一轮都写进 user——**模型没有跨轮记忆**。

    只把模型叫来、不给它"已经摸到过 7.39 又吐回 6.08"这个事实，它会当成
    一笔普通的 +6.08 趋势单照旧回答"继续拿"，每分钟问一次、每次都说拿住，
    等于把 token 烧在信息缺失的判断上。
    """

    def _prompt(self, peak, bid=4020.0, tp=7.0) -> str:
        pos = {"direction": -1, "lots": 0.01, "price_open": 4027.164,
               "opened_at": NOW - 600, "entry_reason": "冲高回落顺势空",
               "plan": "trend"}
        if peak is not None:
            pos["peak_pnl"] = peak
        return autotrade.build_exit_prompt(bars_up(30), pos, bid, bid + 0.24,
                                          tp_min=tp)

    def test_reached_threshold_is_announced(self):
        p = self._prompt(7.39)
        self.assertIn("浮盈峰值 +7.39", p)
        self.assertIn("已触及", p)
        self.assertIn("回落", p)
        self.assertIn("破坏结构", p)   # 明确要求它给结论，不许含糊

    def test_unreached_says_so(self):
        p = self._prompt(2.0)
        self.assertIn("尚未到线", p)
        self.assertNotIn("已触及", p)

    def test_peak_above_current_is_not_called_a_pullback(self):
        """peak_pnl 是每 2~5 秒采样的，可能比此刻浮盈略低。
        这时候不许说"回落 −0.03"——读起来像程序算错了。"""
        p = self._prompt(7.39, bid=4019.4)   # 当前浮盈 ≈ 7.76 > peak
        self.assertNotIn("随后回落", p)
        self.assertIn("比记录到的峰值还高", p)

    def test_missing_peak_does_not_invent_numbers(self):
        p = self._prompt(None)
        self.assertIn("未记录", p)
        self.assertNotIn("已触及", p)


class TestTpReaskConfig(unittest.TestCase):
    def test_negative_rejected_zero_allowed(self):
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(tp_reask_sec=60)), [])
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(tp_reask_sec=0)), [])
        self.assertTrue(any("复核间隔不能为负" in x for x in autotrade.validate_cfg(
            autotrade.Cfg(tp_reask_sec=-1))))

    def test_exposed_to_dashboard(self):
        """面板必须看得见这个值，否则"本轮不调模型"会被当成程序坏了。"""
        self.assertEqual(autotrade.summary()["tp_reask_sec"], 60)
