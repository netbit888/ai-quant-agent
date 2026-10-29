"""tick 主链路：止损快查、下单/平仓、worker 循环、决策开关。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import (
    NOW, TempLedger, bars_down, bars_up,
)


class TestGuardFastCheck(TempLedger):
    """持仓期的硬止损快查（guard_sec）。

    动机是能算出来的：决策间隔 60 秒，而 XAUUSD M1 单根 K 线振幅中位 1.51 USD。
    止损只在每 60 秒那一格被看一眼，意味着价格能在两次检查之间跑完整根 K 线，
    止损**永远不是按 3.00 成交的**。实测 2026-10-26 13:13 那笔：13:42:42 浮亏
    -2.06，13:43:42 以 -3.77 平掉，超调 0.77（把止损放大 26%）。

    快查只做一件事：提前结束等待，让下一轮 tick() 走老路去平仓。
    """

    class _Clock:
        """假时钟。sleep 推进时间，这样测「等了多久」不用真等。"""

        def __init__(self, t: float = 1_000_000.0):
            self.t = t
            self.sleeps: list[float] = []

        def time(self) -> float:
            return self.t

        def sleep(self, sec: float) -> None:
            self.sleeps.append(round(sec, 2))
            self.t += sec

    def _position(self, price_open=4000.0, direction=1, peak=None,
                  opened_at=NOW - 60) -> dict:
        state = autotrade._empty_state()
        pos = {"ticket": -1, "direction": direction, "lots": 0.01,
               "price_open": price_open, "opened_at": opened_at,
               "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        if peak is not None:
            pos["peak_pnl"] = peak
        state["position"] = pos
        autotrade.save_state(state)
        return pos

    # --- 快查本身 -------------------------------------------------------

    def test_guard_only_cares_about_stop_loss(self):
        """止盈到没到、持仓多久，都不归快查管——那些是 _exit_gate 的活。"""
        cfg = autotrade.Cfg(sl_usd=3.0, tp_min_usd=3.25)
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0}
        self.assertEqual(autotrade._guard_hit(cfg, pos, 4000.5, 4000.74), "")
        self.assertEqual(autotrade._guard_hit(cfg, pos, 3998.0, 3998.24), "")
        self.assertIn("触及", autotrade._guard_hit(cfg, pos, 3996.5, 3996.74))

    def test_short_side_stop_uses_ask(self):
        """空单平在 ask，快查不能拿 bid 算，否则点差会吃掉 0.24 的判定余量。"""
        cfg = autotrade.Cfg(sl_usd=3.0)
        pos = {"direction": -1, "lots": 0.01, "price_open": 4000.0}
        self.assertEqual(autotrade._guard_hit(cfg, pos, 4002.66, 4002.90), "")
        self.assertIn("触及", autotrade._guard_hit(cfg, pos, 4003.0, 4003.24))

    def test_zero_stop_disables_guard(self):
        cfg = autotrade.Cfg(sl_usd=0)
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0}
        self.assertEqual(autotrade._guard_hit(cfg, pos, 3900.0, 3900.24), "")

    # --- 等待时序 -------------------------------------------------------

    def test_stop_hit_wakes_up_before_the_round_ends(self):
        """这条是整个改动的目的：浮亏过线时不等满 60 秒。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            woke = autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg,
                sleeper=clock.sleep, quote_fn=lambda s: (3996.5, 3996.74))
        self.assertFalse(woke)
        self.assertLess(clock.t - 1_000_000.0, 60)      # 没等满一轮
        self.assertLessEqual(clock.t - 1_000_000.0, 5)  # 第一格就发现

    def test_no_stop_still_waits_the_full_round(self):
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg,
                sleeper=clock.sleep, quote_fn=lambda s: (4000.2, 4000.44))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)
        self.assertLessEqual(len(clock.sleeps), 14)     # 每 5 秒一格，不是死循环

    def test_flat_round_never_reads_quotes(self):
        """空仓时决策轮本来就是 60 秒，多读 12 次报价纯属浪费。"""
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5)
        clock = self._Clock()
        calls: list[str] = []
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "skip"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (calls.append(s), (4000.0, 4000.24))[1])
        self.assertEqual(calls, [])
        self.assertEqual(len(clock.sleeps), 1)

    def test_close_round_never_reads_quotes(self):
        """平仓轮在冷却结束点唤醒，那时候手上已经没有仓了——
        不需要读报价做止损快查，纯等。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=30, guard_sec=5)
        clock = self._Clock()
        calls: list[str] = []
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "close"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (calls.append(s), (4000.0, 4000.24))[1])
        self.assertEqual(calls, [])
        self.assertEqual(clock.sleeps, [31.0])

    def test_guard_zero_falls_back_to_plain_sleep(self):
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=0)
        clock = self._Clock()
        calls: list[str] = []
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (calls.append(s), (3996.5, 3996.74))[1])
        self.assertEqual(calls, [])
        self.assertEqual(clock.sleeps, [60.0])

    def test_stop_signal_breaks_the_wait(self):
        """面板线程的停止事件要能打断快查循环，否则停机要等一整格。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5)
        self.assertTrue(autotrade._wait_next(
            1_000_000.0, {"kind": "hold"}, cfg,
            stop_wait=lambda sec: True, quote_fn=lambda s: (4000.0, 4000.24)))

    def test_guard_bounds_are_validated(self):
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(guard_sec=5)), [])
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(guard_sec=0)), [])
        self.assertTrue(any("太短" in x
                            for x in autotrade.validate_cfg(autotrade.Cfg(guard_sec=1))))
        # 配得比决策间隔大不算错：_wait_next 会夹到 interval_sec
        self.assertEqual(
            autotrade.validate_cfg(autotrade.Cfg(interval_sec=60, guard_sec=90)), [])
        # 近档两条新规则：下限 2 秒、不能比远档粗（guard_sec=0 时快查整体关闭，不拦）
        self.assertTrue(any("太短" in x for x in autotrade.validate_cfg(
            autotrade.Cfg(guard_near_sec=1.5))))
        self.assertTrue(any("还粗" in x for x in autotrade.validate_cfg(
            autotrade.Cfg(guard_sec=5, guard_near_sec=10))))
        self.assertEqual(
            autotrade.validate_cfg(autotrade.Cfg(guard_sec=0, guard_near_sec=10)), [])

    # --- 自适应步长 + tp 穿越唤醒 ----------------------------------------

    def test_adaptive_step_tightens_near_threshold(self):
        """采样密度该花在决策边界附近：fp 远时 5 秒一格，进 tp-band 后 2 秒一格。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0,
                            tp_min_usd=3.25, tp_wake_band=1.0)
        # fp = bid - 4000：0.5 → 1.0 → 2.5（≥2.25 进近档）→ 停在 2.5
        remaining = [4000.5, 4001.0, 4002.5]

        def quote_fn(s):
            bid = remaining.pop(0) if remaining else 4002.5
            return (bid, bid + 0.24)

        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=quote_fn)
        # 前三格是远档 5 秒，之后全是近档 2 秒，且确实等到了近档
        # （末尾那格是「不足 1 秒保底睡」，不算步长）
        self.assertEqual(clock.sleeps[:3], [5.0, 5.0, 5.0])
        self.assertTrue(all(s == 2.0 for s in clock.sleeps[3:-1]))
        self.assertLess(clock.sleeps[-1], 2.0)
        # 峰值被快查循环实测到并落盘
        self.assertEqual(autotrade.load_state()["position"]["peak_pnl"], 2.5)

    def test_tp_cross_wakes_up_before_the_round_ends(self):
        """tp 穿越唤醒：fp ≥ 3.25 不等满 60 秒，下一轮交模型。参数没变只砍延迟。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0, tp_min_usd=3.25)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            woke = autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4003.5, 4003.74))
        self.assertFalse(woke)
        self.assertLessEqual(clock.t - 1_000_000.0, 5)   # 第一格就发现

    def test_tp_wake_disabled_keeps_old_rhythm(self):
        """tp_wake=False = 快查只记录峰值和止损，出场节奏完全回到老行为。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0,
                            tp_min_usd=3.25, tp_wake=False)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4003.5, 4003.74))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)
        self.assertLessEqual(len(clock.sleeps), 14)
        # 峰值照记：测量不随行为开关关闭
        self.assertEqual(autotrade.load_state()["position"]["peak_pnl"], 3.5)

    def test_stale_peak_wakes_up_before_the_round_ends(self):
        """峰值停滞也走快查：不等到 60 秒，下一轮就能把决定权交出去。"""
        pos = self._position()
        st = autotrade.load_state()
        st["position"] = {**pos, "peak_pnl": 1.85, "peak_ts": 1_000_000 - 240}
        autotrade.save_state(st)
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0,
                            tp_min_usd=3.25)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            woke = autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4000.7, 4000.94))
        self.assertFalse(woke)
        self.assertLessEqual(clock.t - 1_000_000.0, 5)

    def test_stale_off_keeps_waiting_the_full_round(self):
        """同一笔持仓、关掉 stale_wake 就要睡满 60 秒——
        这才证明上面那条的提前返回真来自这条规则，而不是别的原因。"""
        pos = self._position()
        st = autotrade.load_state()
        st["position"] = {**pos, "peak_pnl": 1.85, "peak_ts": 1_000_000 - 240}
        autotrade.save_state(st)
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0,
                            tp_min_usd=3.25, stale_wake=False)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4000.7, 4000.94))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)

    def test_band_zero_stays_far(self):
        """tp_wake_band=0 关闭近档：整轮都按远档 5 秒粗看。"""
        self._position()
        cfg = autotrade.Cfg(interval_sec=60, guard_sec=5, sl_usd=3.0,
                            tp_min_usd=3.25, tp_wake_band=0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4002.5, 4002.74))
        self.assertTrue(all(s == 5.0 for s in clock.sleeps[:-1]))
        self.assertLess(clock.sleeps[-1], 5.0)   # 尾段保底睡
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)

    # --- _track_peak 单元 ------------------------------------------------

    def test_track_peak_unit(self):
        """峰值只增不减；0.0 是合法峰值不能被 `or` 吞；peak_ts 只在抬高时更新。"""
        pos: dict = {}
        self.assertTrue(autotrade._track_peak(pos, 0.0, 100))      # 首次采样
        self.assertEqual(pos["peak_pnl"], 0.0)
        self.assertEqual(pos["peak_ts"], 100)
        self.assertFalse(autotrade._track_peak(pos, 0.0, 200))     # 持平不抬高
        self.assertEqual(pos["peak_ts"], 100)
        self.assertTrue(autotrade._track_peak(pos, 0.5, 300))
        self.assertEqual(pos["peak_ts"], 300)
        self.assertFalse(autotrade._track_peak(pos, -0.5, 400))    # 回落不覆盖
        self.assertEqual(pos["peak_pnl"], 0.5)
        self.assertEqual(pos["peak_ts"], 300)
        fresh: dict = {}
        self.assertTrue(autotrade._track_peak(fresh, -0.5, 500))   # 负值首采也记
        self.assertEqual(fresh["peak_pnl"], -0.5)

    # --- 峰值浮盈埋点 ---------------------------------------------------

    def test_peak_pnl_is_recorded_on_hold_and_close(self):
        """peak_pnl 的唯一用途：用实测数据回答「3.25 几次能到」。

        没有它只能靠 replay_exit 反事实回放 K 线，被问起来只能说「这是外推」。
        """
        self._position()
        cfg = autotrade.Cfg(dry_run=True, sl_usd=3.0, tp_min_usd=3.25)
        # 第一轮浮盈 +2.0 → 记下 2.0
        with mock.patch.object(autotrade, "ask_exit") as m_exit:
            row1 = autotrade.tick(cfg, now=NOW, quote=(4002.0, 4002.24),
                                  rows=bars_up(), positions=[])
        self.assertEqual(row1["kind"], "hold")
        self.assertEqual(row1["peak_pnl"], 2.0)
        m_exit.assert_not_called()

        # 第二轮回落到 +0.5 → 峰值必须留在 2.0，不能被 `or` 抹掉
        with mock.patch.object(autotrade, "ask_exit") as m_exit:
            row2 = autotrade.tick(cfg, now=NOW + 60, quote=(4000.5, 4000.74),
                                  rows=bars_up(), positions=[])
        self.assertEqual(row2["float_pnl"], 0.5)
        self.assertEqual(row2["peak_pnl"], 2.0)
        m_exit.assert_not_called()

        # 峰值正好是 0.0：不能因为 `or` 把 0.0 当假值退回当前浮盈
        self._position(peak=0.0)
        with mock.patch.object(autotrade, "ask_exit"):
            row3 = autotrade.tick(cfg, now=NOW, quote=(3999.5, 3999.74),
                                  rows=bars_up(), positions=[])
        self.assertEqual(row3["float_pnl"], -0.5)
        self.assertEqual(row3["peak_pnl"], 0.0)

    def test_peak_pnl_reaches_close_row(self):
        """止损平仓时也要带上峰值，否则「摸到过多少才被止损」数不出来。"""
        self._position()
        cfg = autotrade.Cfg(dry_run=True, sl_usd=3.0, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit"):
            autotrade.tick(cfg, now=NOW, quote=(4002.0, 4002.24),
                           rows=bars_up(), positions=[])
        row = autotrade.tick(cfg, now=NOW + 60, quote=(3996.5, 3996.74),
                             rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")
        self.assertEqual(row["peak_pnl"], 2.0)
        self.assertLess(row["pnl"], -3.0)   # 止损按市价成交，超调是真实存在的

    def test_close_row_carries_plan_and_atr(self):
        """平仓行冗余 plan/atr：统计「这笔 tp 是几倍 ATR」不用再按 ticket
        反查 open 行——少一次 join 就少一处漏。旧持仓没有 atr 时写 None。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 600,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True,
                             "plan": "range", "atr": 1.35,
                             "peak_pnl": 0.5, "peak_ts": NOW - 120}
        autotrade.save_state(state)
        cfg = autotrade.Cfg(dry_run=True, sl_usd=3.0, tp_min_usd=3.25)
        row = autotrade.tick(cfg, now=NOW, quote=(3996.5, 3996.74),
                             rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["plan"], "range")
        self.assertEqual(row["atr"], 1.35)

    def test_old_position_without_atr_closes_with_none(self):
        """旧 state 的持仓没有 atr 字段：平仓照走，字段落 None，不能 KeyError。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 600,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        autotrade.save_state(state)
        cfg = autotrade.Cfg(dry_run=True, sl_usd=3.0, tp_min_usd=3.25)
        row = autotrade.tick(cfg, now=NOW, quote=(3996.5, 3996.74),
                             rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertIsNone(row["atr"])
        self.assertIsNone(row["plan"])


class TestModelFailureStops(TempLedger):
    def setUp(self):
        super().setUp()
        autotrade._JSON_MODE_OK = True

    def tearDown(self):
        autotrade._JSON_MODE_OK = True
        super().tearDown()

    def test_entry_failure_skips_instead_of_opening(self):
        with mock.patch("agent.llm.chat_ex", side_effect=RuntimeError("429")), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(autotrade.Cfg(interval_sec=60), now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "skip")
        # 必须和"模型主动放弃"分开：故障不能被算成它变保守了
        self.assertEqual(row.get("source"), "error")
        m_open.assert_not_called()

    def test_three_exit_failures_stop_the_worker(self):
        state = autotrade._empty_state()
        state["position"] = {"ticket": 77, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 600,
                             "p_up": 0.6, "entry_reason": "突破"}
        autotrade.save_state(state)

        err = {"close": False, "source": "error",
               "reason": "模型调用失败（429）", "error": "429"}
        # tp_min_usd=0 是为了让出场决策真的走到「问模型」这一步——
        # 默认门槛 3.25 下浮盈 0 会被闸门拦住，永远不调模型，也测不到失败计数。
        cfg = autotrade.Cfg(interval_sec=60, tp_min_usd=0, max_hold_sec=0)
        with mock.patch.object(autotrade, "ask_exit", return_value=err):
            rows = [autotrade.tick(cfg, now=NOW + i,
                                   quote=(4000.0, 4000.24), rows=bars_up(),
                                   positions=[{"ticket": 77, "symbol": "XAUUSDm"}])
                    for i in (0, 60, 120)]

        self.assertEqual(rows[0]["kind"], "hold")
        self.assertEqual(rows[1]["kind"], "hold")
        # 第三次必须停：仓位还在手上、模型又调不通，不能让它一直挂着没人管
        self.assertEqual(rows[2]["kind"], "stop")
        self.assertIn("连续", rows[2]["reason"])

    def test_model_hold_row_carries_pnl_and_peak(self):
        """模型判「继续持有」的 hold 行必须带浮盈/峰值/单据号。

        2026-10-29 那笔插针单：模型在浮盈 +4.86 时判继续持有，但这条 hold 行
        只存了 reason 文本，`float_pnl` / `peak_pnl` / `ticket` 全是 None——
        复盘时根本看不出"它是在过门槛之后做的决定"，只能从中文里手抠数字。
        和闸门那条 hold 行对齐口径，两处都要能直接统计。
        """
        state = autotrade._empty_state()
        state["position"] = {"ticket": 88, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 60,
                             "p_up": 0.6, "entry_reason": "贴下沿",
                             "plan": "range"}
        autotrade.save_state(state)

        # tp_min_usd=0 且 min_hold_sec=0 → 闸门放行，本轮一定会问模型
        cfg = autotrade.Cfg(interval_sec=60, tp_min_usd=0, max_hold_sec=0,
                            min_hold_sec=0)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": False, "source": "llm",
                                             "reason": "结构没走完"}):
            row = autotrade.tick(cfg, now=NOW, quote=(4003.0, 4003.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 88, "symbol": "XAUUSDm"}])

        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row["ticket"], 88)
        self.assertAlmostEqual(row["float_pnl"], 3.0, places=2)
        self.assertAlmostEqual(row["peak_pnl"], 3.0, places=2)
        self.assertIn("peak_ts", row)

    def test_gate_hold_row_carries_ticket(self):
        """闸门那条 hold 行也要带单据号：同一次持仓里会有多条 hold，
        没有 ticket 就只能靠时间戳猜属于哪一笔。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": 99, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 60,
                             "p_up": 0.6, "entry_reason": "贴下沿", "plan": "range"}
        autotrade.save_state(state)

        cfg = autotrade.Cfg(interval_sec=60)          # 默认门槛 3.25，浮盈 0 会拦
        row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24), rows=bars_up(),
                             positions=[{"ticket": 99, "symbol": "XAUUSDm"}])
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row.get("source"), "tp_wait")
        self.assertEqual(row["ticket"], 99)
        self.assertAlmostEqual(row["float_pnl"], 0.0, places=2)


class TestTick(TempLedger):
    def setUp(self):
        super().setUp()
        # 默认走真实路径（send_open / send_close 会被 mock 掉），
        # 纸面模式的两个用例自己显式传 dry_run=True。
        #
        # 入场闸门在这里关掉：下面用的是 bars_up()，那是单调上涨序列，
        # 收盘位置恒为 1.0，默认会撞上「贴上沿禁多」。这些用例验证的是
        # 状态机与落盘（开仓记录、冷却、持仓互斥），不是入场规则；
        # 闸门本身在 TestEntryVeto 里逐个用例锁死。
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False,
                                 act_guard=False, top_long_ban=False)

    def test_open_records_state_and_log(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.7, "reason": "突破", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.2, "order": 555}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])

        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["direction"], 1)
        self.assertAlmostEqual(row["p_up"], 0.7)

        state = autotrade.load_state()
        self.assertEqual(state["trades"], 1)
        self.assertEqual(state["position"]["ticket"], 555)
        # 不挂止盈止损：position 里不该出现任何 sl/tp 字段
        self.assertNotIn("sl", state["position"])
        self.assertNotIn("tp", state["position"])

    def test_low_probability_goes_short(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.3, "reason": "破位", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 3999.0, "order": 556}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_down(), positions=[])
        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["direction"], -1)

    def test_skip_does_not_call_llm(self):
        """冷却期内的 tick 必须一次 API 都不花。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 5
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_entry") as m_entry, \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])

        self.assertEqual(row["kind"], "skip")
        m_entry.assert_not_called()
        m_open.assert_not_called()

    def test_same_dir_reentry_blocked_before_send_open(self):
        """同向重进：预闸门（那会儿不知道方向）放过了，开仓前这道必须拦住。

        冷却反向 30 秒在 self.cfg 里已经走完（61 秒），但同向默认 180 秒还在。
        这是 10-29 那 4 笔「平仓 31 秒后同向再进、4/4 止损」的直接防线。
        """
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        state["last_close_dir"] = 1
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "突破",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])

        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "same_dir_cooldown")
        self.assertIn("同向", row["reason"])
        m_open.assert_not_called()

    def test_reverse_reentry_not_blocked_before_send_open(self):
        """反向重进不受同向那 180 秒影响——否则等于把冷却偷偷改回单一档。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        state["last_close_dir"] = 1
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.3, "reason": "破位",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 3999.0,
                                             "order": 557}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_down(), positions=[])

        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["direction"], -1)

    def test_blocked_by_existing_position_does_not_call_llm(self):
        with mock.patch.object(autotrade, "ask_entry") as m_entry:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 1, "symbol": "XAUUSDm"}])
        self.assertEqual(row["kind"], "skip")
        m_entry.assert_not_called()

    def test_hold_when_model_says_keep(self):
        state = autotrade._empty_state()
        state["position"] = {"ticket": 77, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 60,
                             "p_up": 0.6, "entry_reason": "突破"}
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_exit",
                              return_value={"close": False, "reason": "还在趋势",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_close") as m_close:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4002.0, 4002.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 77, "symbol": "XAUUSDm"}])

        self.assertEqual(row["kind"], "hold")
        m_close.assert_not_called()
        self.assertIsNotNone(autotrade.load_state()["position"])

    def test_ai_closes_its_own_position(self):
        """平仓是 AI 自己发市价单，不是被 SL/TP 打掉的。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": 88, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 120,
                             "p_up": 0.6, "entry_reason": "突破"}
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_exit",
                              return_value={"close": True, "reason": "动能衰竭",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_close",
                              return_value={"ok": True}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4005.0, 4005.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 88, "symbol": "XAUUSDm"}])

        self.assertEqual(row["kind"], "close")
        # 多头平在 bid(4005.0)：5 美元 × 0.01 手 × 100 = 5.00
        self.assertAlmostEqual(row["pnl"], 5.0, places=2)
        self.assertTrue(row["hit"])
        # 平仓行必须带上当初的开仓理由，否则面板上「为什么进」那一列是空的
        self.assertEqual(row["entry_reason"], "突破")
        self.assertEqual(row["reason"], "动能衰竭")

        after = autotrade.load_state()
        self.assertIsNone(after["position"])
        self.assertEqual(after["closed"], 1)
        self.assertEqual(after["last_close_ts"], NOW)  # 冷却从这里起算
        self.assertAlmostEqual(after["pnl"], 5.0, places=2)

    def test_external_close_detected(self):
        """在 MT5 里手动平掉也要被认出来，否则会卡在"永远在仓"。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": 99, "direction": -1, "lots": 0.01,
                             "price_open": 4010.0, "opened_at": NOW - 60,
                             "p_up": 0.3}
        autotrade.save_state(state)

        row = autotrade.tick(self.cfg, now=NOW, quote=(4005.0, 4005.24),
                             rows=bars_down(), positions=[])

        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "external")
        # 空头平在 ask(4005.24)：(4005.24 - 4010) × -1 × 0.01 × 100 = 4.76
        self.assertAlmostEqual(row["pnl"], 4.76, places=2)
        self.assertIsNone(autotrade.load_state()["position"])

    def test_missing_quote_logs_skip(self):
        # 必须把 _quote 一起挡住：终端在线时 tick 会去拉真报价，这条就变成
        # 「环境决定断言」而不是「代码决定断言」。要测的是拿不到报价的分支。
        with mock.patch.object(autotrade, "_quote", return_value=None):
            row = autotrade.tick(self.cfg, now=NOW, quote=None,
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("报价", row["reason"])

    def test_max_loss_brake_stops(self):
        cfg = autotrade.Cfg(max_loss_usd=3.0)
        state = autotrade._empty_state()
        state["pnl"] = -3.5
        autotrade.save_state(state)

        row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                             rows=bars_up(), positions=[])
        self.assertTrue(row.get("stop"))
        self.assertIsNotNone(autotrade.load_state()["stop_reason"])

    def test_stopped_state_does_nothing(self):
        state = autotrade._empty_state()
        state["stop_reason"] = "手工熔断"
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_entry") as m_entry:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertTrue(row.get("stop"))
        m_entry.assert_not_called()

    def test_paper_position_not_matched_against_terminal(self):
        """纸面仓位只存在于 state，券商那边没有，不能被判成「已平仓」。"""
        cfg = autotrade.Cfg(dry_run=True, act_guard=False, top_long_ban=False)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.7, "reason": "突破", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "dry_run": True}), \
             mock.patch.object(autotrade, "_positions", return_value=[]):
            first = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24), rows=bars_up())
            self.assertEqual(first["kind"], "open")
            # 持仓还在券商那边查不到——这是纸面模式的常态，不是平仓信号
            with mock.patch.object(autotrade, "ask_exit",
                                  return_value={"close": False, "reason": "还在趋势",
                                                "source": "llm"}), \
                 mock.patch.object(autotrade, "ask_entry") as m_entry:
                second = autotrade.tick(cfg, now=NOW + 60, quote=(4002.0, 4002.24),
                                        rows=bars_up())

        self.assertEqual(second["kind"], "hold")
        self.assertIsNotNone(autotrade.load_state()["position"])
        m_entry.assert_not_called()

    def test_paper_close_does_not_hit_broker(self):
        """纸面平仓在本地结算，不发单——否则会拿到「持仓不存在」并永远卡住。"""
        cfg = autotrade.Cfg(dry_run=True)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.3, "reason": "破位", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "dry_run": True}), \
             mock.patch.object(autotrade, "_positions", return_value=[]):
            autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24), rows=bars_down())
            with mock.patch.object(autotrade, "ask_exit",
                                  return_value={"close": True, "reason": "反转",
                                                "source": "llm"}), \
                 mock.patch.object(autotrade, "send_close") as m_close:
                row = autotrade.tick(cfg, now=NOW + 60, quote=(4004.0, 4004.24),
                                     rows=bars_down())

        self.assertEqual(row["kind"], "close")
        m_close.assert_not_called()
        # 空单：开在 bid 4000 → 平在 ask 4004.24，(4004.24-4000) × -1 × 1 = -4.24
        self.assertAlmostEqual(row["pnl"], -4.24, places=2)

    def test_fallback_marked_in_log(self):
        """兜底决策必须标成 fallback，否则命中率统计会把规则的分算到模型头上。"""
        cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False,
                            min_edge=0, act_guard=False,  # 这里只验证来源标记，先关掉两道闸门
                            top_long_ban=False)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.55, "reason": "规则兜底",
                                            "source": "fallback"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 600}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["source"], "fallback")

    def test_weak_signal_is_skipped(self):
        """p_up 贴着 0.5 就别开——开一单先亏 0.24 点差，高频下必被磨死。"""
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.52, "reason": "略偏多",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("信号不够强", row["reason"])
        m_open.assert_not_called()

    def test_strong_signal_still_opens(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.62, "reason": "突破",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 700}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")

    def test_gate_can_be_disabled(self):
        cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False,
                            min_edge=0, act_guard=False, top_long_ban=False)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.51, "reason": "微偏",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 701}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")


class TestFatalOrderFailure(TempLedger):
    """券商拒绝时必须自停。不停的话每分钟重试一次，每轮都要花一次模型调用。"""

    def setUp(self):
        super().setUp()
        # 这里只关心"券商拒绝后要不要自停"，入场闸门关掉免得挡在前面。
        self.cfg = autotrade.Cfg(dry_run=False, interval_sec=60, cooldown_sec=30,
                                 act_guard=False, top_long_ban=False)

    def test_retcode_10027_says_enable_autotrading(self):
        msg = autotrade.retcode_hint(10027)
        self.assertIn("算法交易", msg)

    def test_unknown_retcode_returns_none(self):
        self.assertIsNone(autotrade.retcode_hint(99999))
        self.assertIsNone(autotrade.retcode_hint(None))

    def test_open_rejected_stops_loop(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.7, "reason": "突破",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": False, "retcode": 10027,
                                            "reason": autotrade.retcode_hint(10027)}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])

        self.assertTrue(row.get("stop"))
        self.assertIn("算法交易", row["reason"])
        self.assertIsNotNone(autotrade.load_state()["stop_reason"])

    def test_close_rejected_stops_loop(self):
        """平不掉还继续每分钟问一次要不要平，只会白烧钱。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": 42, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 60,
                             "p_up": 0.6, "entry_reason": "突破"}
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "ask_exit",
                              return_value={"close": True, "reason": "动能衰竭",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_close",
                              return_value={"ok": False, "retcode": 10027,
                                            "reason": autotrade.retcode_hint(10027)}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4005.0, 4005.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 42, "symbol": "XAUUSDm"}])

        self.assertTrue(row.get("stop"))
        # 仓位还在，不能清掉——让人去终端处理
        self.assertIsNotNone(autotrade.load_state()["position"])


class TestFillingFallback(unittest.TestCase):
    """真单能不能发出去，一半取决于成交方式（filling）选对没有。"""

    def _ex(self):
        from execution.live import ExecConfig, Executor

        return Executor(ExecConfig(symbol="XAUUSDm"))

    def test_respects_symbol_filling_flags(self):
        import MetaTrader5 as mt5

        fake = type("S", (), {"filling_mode": 1, "visible": True})()  # 只支持 FOK
        with mock.patch.object(mt5, "symbol_info", return_value=fake):
            modes = self._ex()._filling_modes()
        self.assertEqual(modes[0], getattr(mt5, "ORDER_FILLING_FOK", 0))

    def test_falls_back_to_ioc_first_when_unknown(self):
        import MetaTrader5 as mt5

        with mock.patch.object(mt5, "symbol_info", return_value=None):
            modes = self._ex()._filling_modes()
        self.assertEqual(modes[0], getattr(mt5, "ORDER_FILLING_IOC", 1))
        self.assertEqual(len(modes), 3)  # IOC / FOK / RETURN 都要试到


class TestConnectionKeepAlive(unittest.TestCase):
    def test_bars_does_not_shutdown_terminal(self):
        """常驻线程取 K 线不能 shutdown。

        MT5 的 initialize/shutdown 是进程级引用计数，取一次行情就关连接的话，
        下一轮连报价都拿不到，日志只会显示「拿不到报价」，看着像行情断了。
        """
        import MetaTrader5 as mt5  # 测试桩也带 shutdown 属性

        with mock.patch.object(mt5, "shutdown") as m_shutdown:
            rows = autotrade._bars("XAUUSDm", "M1", 10)
        m_shutdown.assert_not_called()
        # 只验证「取行情不会关连接」。原来还断言 rows == []，那是把
        # 「终端没开」当成前置条件——终端一开这条就随机变红（踩过很多次）。
        # 有没有行情是环境的事，跟这条逻辑无关。
        self.assertIsInstance(rows, list)
        if rows:
            self.assertIn("close", rows[0])

    def test_keep_alive_executor_skips_shutdown(self):
        from execution.live import ExecConfig, Executor

        cfg = ExecConfig(keep_alive=True)
        self.assertTrue(cfg.keep_alive)
        ex = Executor(cfg)
        ex.connected = True
        import MetaTrader5 as mt5

        with mock.patch.object(mt5, "shutdown") as m_shutdown:
            ex.shutdown()
        m_shutdown.assert_not_called()
        self.assertTrue(ex.connected)


class TestCloseAll(TempLedger):
    def _positions(self, n=2):
        return [{"ticket": i, "symbol": "XAUUSDm", "direction": 1} for i in range(1, n + 1)]

    def test_closes_every_position_regardless_of_source(self):
        """「关闭并清仓」平的是账户上所有持仓，不只是 AI 自己开的。"""
        with mock.patch.object(autotrade, "_all_positions", return_value=self._positions()), \
             mock.patch.object(autotrade, "send_close",
                              return_value={"ok": True}) as m_close:
            res = autotrade.close_all(autotrade.Cfg(dry_run=False))

        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertEqual(m_close.call_count, 2)

    def test_partial_failure_is_not_ok(self):
        with mock.patch.object(autotrade, "_all_positions", return_value=self._positions()), \
             mock.patch.object(autotrade, "send_close",
                              side_effect=[{"ok": True}, {"ok": False, "reason": "行情关闭"}]):
            res = autotrade.close_all(autotrade.Cfg(dry_run=False))

        self.assertFalse(res["ok"])
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["error"], "行情关闭")

    def test_unreadable_positions_says_so(self):
        """读不到持仓必须明说没平掉，不能让用户以为清仓成功。"""
        with mock.patch.object(autotrade, "_all_positions", return_value=None):
            res = autotrade.close_all(autotrade.Cfg(dry_run=False))

        self.assertFalse(res["ok"])
        self.assertIn("没有平掉", res["error"])
        self.assertFalse(autotrade.load_state()["running"])

    def test_paper_position_settled_locally(self):
        """纸面仓不在券商那儿，清仓时必须在本地结算，否则永远挂在 state 里。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 60,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "_all_positions", return_value=[]), \
             mock.patch.object(autotrade, "_quote", return_value=(4005.0, 4005.24)), \
             mock.patch.object(autotrade, "send_close") as m_close:
            res = autotrade.close_all(autotrade.Cfg(dry_run=True))

        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 1)
        m_close.assert_not_called()
        after = autotrade.load_state()
        self.assertIsNone(after["position"])
        self.assertAlmostEqual(after["pnl"], 5.0, places=2)  # 多头平在 bid 4005

    def test_paper_mode_skips_broker(self):
        with mock.patch.object(autotrade, "_all_positions", return_value=[{"ticket": 1}]), \
             mock.patch.object(autotrade, "send_close") as m_close:
            res = autotrade.close_all(autotrade.Cfg(dry_run=True))
        self.assertTrue(res["ok"])
        m_close.assert_not_called()

    def test_stop_sets_stop_reason(self):
        with mock.patch.object(autotrade, "_all_positions", return_value=[]):
            autotrade.close_all(autotrade.Cfg(dry_run=False))
        state = autotrade.load_state()
        self.assertFalse(state["running"])
        self.assertIn("清仓", state["stop_reason"])


class TestWorker(TempLedger):
    def tearDown(self):
        try:
            autotrade.WORKER.stop(flatten=False)
        except Exception:  # noqa: BLE001
            pass
        super().tearDown()

    def test_start_and_stop(self):
        cfg = autotrade.Cfg(interval_sec=9999, dry_run=True)
        with mock.patch.object(autotrade, "tick",
                               return_value={"kind": "skip", "reason": "mock"}):
            res = autotrade.WORKER.start(cfg)
            self.assertTrue(res["ok"])
            self.assertTrue(autotrade.WORKER.alive())
            self.assertTrue(autotrade.summary()["running"])

            out = autotrade.WORKER.stop(flatten=False)

        self.assertTrue(out["ok"])
        self.assertFalse(autotrade.WORKER.alive())
        self.assertFalse(autotrade.load_state()["running"])

    def test_double_start_rejected(self):
        cfg = autotrade.Cfg(interval_sec=9999, dry_run=True)
        with mock.patch.object(autotrade, "tick",
                               return_value={"kind": "skip", "reason": "mock"}):
            first = autotrade.WORKER.start(cfg)
            second = autotrade.WORKER.start(cfg)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("已经在跑", second["error"])

    def test_stale_running_flag_not_reported_as_running(self):
        """面板重启后磁盘上还留着 running=true，不能显示成还在做单。"""
        state = autotrade._empty_state()
        state["running"] = True
        state["owner"] = "panel"
        autotrade.save_state(state)

        s = autotrade.summary()
        self.assertFalse(s["running"])
        self.assertTrue(s["stale"])

    def test_stop_restores_dry_run_mode_from_state(self):
        """线程已经不在时点「关闭」，必须还知道上次是纸面还是真实。"""
        cfg = autotrade.Cfg(dry_run=False, symbol="XAUUSDm")
        state = autotrade._empty_state()
        state["cfg"] = autotrade.cfg_to_dict(cfg)
        state["owner"] = "panel"
        state["running"] = True
        autotrade.save_state(state)

        restored = autotrade.cfg_from_state()
        self.assertIsNotNone(restored)
        self.assertFalse(restored.dry_run)

    def test_old_saved_cfg_migrates_to_asymmetric_cooldown(self):
        """旧存档里只有 cooldown_sec=30（那是老默认值，不是人的选择）。

        还原时必须换成新的两档默认，否则重启一次冷却就悄悄退回单向 30 秒。
        """
        state = autotrade._empty_state()
        state["cfg"] = {"symbol": "XAUUSDm", "cooldown_sec": 30}
        autotrade.save_state(state)

        restored = autotrade.cfg_from_state()
        self.assertIsNotNone(restored)
        self.assertEqual(restored.cooldown_sec, 60)
        self.assertEqual(restored.cooldown_same_sec, 180)

    def test_custom_saved_cooldown_is_kept(self):
        """人自己配过的值不能替人改：120 保留，同向补默认 180。"""
        state = autotrade._empty_state()
        state["cfg"] = {"symbol": "XAUUSDm", "cooldown_sec": 120}
        autotrade.save_state(state)

        restored = autotrade.cfg_from_state()
        self.assertIsNotNone(restored)
        self.assertEqual(restored.cooldown_sec, 120)
        self.assertEqual(restored.cooldown_same_sec, 180)


class TestJsonMode(TempLedger):
    def setUp(self):
        super().setUp()
        autotrade._JSON_MODE_OK = True

    def tearDown(self):
        autotrade._JSON_MODE_OK = True
        super().tearDown()

    def test_asks_for_json_mode(self):
        seen = {}

        def fake(prompt, system=None, json_mode=False):
            seen["json"] = json_mode
            return '{"p_up": 0.62, "reason": "突破"}', {}

        with mock.patch("agent.llm.chat_ex", fake):
            got = autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())

        self.assertTrue(seen["json"])
        self.assertEqual(got["source"], "llm")
        self.assertAlmostEqual(got["p_up"], 0.62)

    def test_falls_back_when_model_rejects_json_mode(self):
        calls = []

        def fake(prompt, system=None, json_mode=False):
            calls.append(json_mode)
            if json_mode:
                raise RuntimeError("response_format not supported")
            return '{"p_up": 0.4, "reason": "弱"}', {}

        with mock.patch("agent.llm.chat_ex", fake):
            got = autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())

        self.assertEqual(calls, [True, False])   # 先试 JSON mode，失败再退
        self.assertEqual(got["source"], "llm")
        self.assertFalse(autotrade._JSON_MODE_OK)  # 记住，别每轮都白试一次

    def test_stops_trying_once_disabled(self):
        autotrade._JSON_MODE_OK = False
        calls = []

        def fake(prompt, system=None, json_mode=False):
            calls.append(json_mode)
            return '{"p_up": 0.5, "reason": "中性"}', {}

        with mock.patch("agent.llm.chat_ex", fake):
            autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())
        self.assertEqual(calls, [False])

    def test_transient_error_does_not_disqualify_json_mode(self):
        """429 / 超时跟 JSON mode 无关，不许因此把它永久关掉。

        实测 10-23 一天有 70 次 429/超时：旧逻辑第一次撞上就把开关关死，
        之后所有调用（含复盘）都在跑纯文本，而且从日志里看不出来。
        """
        for boom in ("429 Too Many Requests", "timeout", "Connection reset by peer"):
            autotrade._JSON_MODE_OK = True
            calls = []

            def fake(prompt, system=None, json_mode=False, _e=boom):
                calls.append(json_mode)
                if json_mode:
                    raise RuntimeError(_e)
                return '{"p_up": 0.4, "reason": "弱"}', {}

            with mock.patch("agent.llm.chat_ex", fake):
                got = autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())

            self.assertEqual(calls, [True], boom)          # 不重试，省一次配额
            self.assertTrue(autotrade._JSON_MODE_OK, boom)  # 开关不动
            self.assertEqual(got["source"], "error", boom)  # 这一轮不做


class TestNoTradingAllowed(TempLedger):
    """以前系统说"必须给出方向，不存在观望"，把复盘里"等确认再进"直接顶掉了。
    这是同一个错犯 12 次的直接原因之一。"""

    def test_system_allows_skipping(self):
        self.assertIn("不做交易是允许的", autotrade.ENTRY_SYSTEM)
        self.assertNotIn("必须给出方向", autotrade.ENTRY_SYSTEM)
        self.assertNotIn('"观望"', autotrade.ENTRY_SYSTEM)

    def test_system_tells_model_to_use_lessons(self):
        self.assertIn("上面的教训是你自己的历史", autotrade.ENTRY_SYSTEM)

    def test_flat_probability_skips_without_ordering(self):
        cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.50, "reason": "完全随机",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        m_open.assert_not_called()


class TestActFlag(TempLedger):
    """让模型显式说"不做"。以前只能靠 p_up≈0.5 暗示，结果常见
    "理由写不追、数值 0.38 照开"的自相矛盾。"""

    def setUp(self):
        super().setUp()
        # 同上：bars_up() 的 pos 恒为 1.0，关掉入场闸门才能测"act=true 就开"。
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False,
                                 act_guard=False, top_long_ban=False)

    def test_parse_act_true(self):
        got = autotrade.parse_p_up('{"act": true, "p_up": 0.62, "reason": "突破"}')
        self.assertTrue(got["act"])

    def test_parse_act_false(self):
        got = autotrade.parse_p_up('{"act": false, "p_up": 0.62, "reason": "等确认"}')
        self.assertFalse(got["act"])

    def test_parse_act_string_forms(self):
        self.assertFalse(autotrade.parse_p_up('{"act": "不做", "p_up": 0.3}')["act"])
        self.assertTrue(autotrade.parse_p_up('{"act": "yes", "p_up": 0.7}')["act"])
        self.assertFalse(autotrade.parse_p_up('{"action": "false", "p_up": 0.3}')["act"])

    def test_missing_act_is_none(self):
        self.assertIsNone(autotrade.parse_p_up('{"p_up": 0.6, "reason": "x"}')["act"])

    def test_act_false_skips_even_with_strong_probability(self):
        """p_up 很强但模型说 act=false → 不做。以它的决定为准。"""
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.75, "act": False,
                                            "reason": "等跌破再追", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("模型判定不做", row["reason"])
        m_open.assert_not_called()

    def test_act_true_still_subject_to_edge_gate(self):
        """act=true 但优势太薄，仍然不开——点差不会因为它的意愿消失。"""
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.52, "act": True,
                                            "reason": "略偏多", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("信号不够强", row["reason"])
        m_open.assert_not_called()

    def test_act_true_and_strong_opens(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.68, "act": True,
                                            "reason": "突破", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 800}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")

    def test_summary_counts_model_declines(self):
        """模型主动放弃 vs 被闸门挡掉要分开算，否则看不出它是胆小还是被拦。"""
        autotrade.append_log({"ts": NOW, "kind": "skip", "act": False,
                              "reason": "模型判定不做"})
        autotrade.append_log({"ts": NOW, "kind": "skip",
                              "reason": "信号不够强"})
        self.assertEqual(autotrade.summary()["model_declines"], 1)

    def test_act_is_recorded_in_state(self):
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.62, "act": True,
                                            "reason": "突破", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 801}):
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        self.assertTrue(autotrade.load_state()["last_decision"]["act"])

    def test_system_requires_act_field(self):
        self.assertIn('"act": true', autotrade.ENTRY_SYSTEM)
        self.assertIn("理由说不做", autotrade.ENTRY_SYSTEM)


class TestMinHold(TempLedger):
    """最短持仓保护。2026-10-23 起它只在 tp_min_usd=0（关掉止盈门槛）时才生效，
    默认路径改由「浮盈不到 3.25 就不平」管——那个门槛比 60 秒严格得多，
    刚开仓的单子不可能有 3.25 的浮盈，所以最短持仓天然满足。
    这里传 tp_min_usd=0 是在测**回退路径**，别以为默认行为是这样。"""

    def _open_position(self, opened_at, price_open=4000.0, direction=1):
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": direction, "lots": 0.01,
                             "price_open": price_open, "opened_at": opened_at,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        autotrade.save_state(state)

    def test_early_close_is_blocked_without_calling_model(self):
        self._open_position(NOW - 20)          # 才持仓 20 秒
        cfg = autotrade.Cfg(dry_run=True, min_hold_sec=60,
                            min_hold_exempt_usd=0.5, tp_min_usd=0)
        with mock.patch.object(autotrade, "ask_exit") as m_exit:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.5, 4000.74),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row["source"], "min_hold")
        self.assertIn("最短持仓中", row["reason"])
        m_exit.assert_not_called()             # 不调模型，省一次 token
        self.assertIsNotNone(autotrade.load_state()["position"])

    def test_big_loss_is_cut_by_stop_loss_not_the_model(self):
        """浮亏超过止损线：程序直接平，连模型都不问。

        旧行为是「浮亏超豁免线就放行交给模型判断」——第 3 场的账证明这条路
        走不通：前四大亏全是 p_up=0.0 的单子，它最自信时亏最多，不会认错。
        """
        self._open_position(NOW - 20)          # 远不到 60 秒
        cfg = autotrade.Cfg(dry_run=True, min_hold_sec=60,
                            min_hold_exempt_usd=0.5, sl_usd=3.0, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit") as m_exit:
            row = autotrade.tick(cfg, now=NOW, quote=(3995.0, 3995.24),
                                 rows=bars_down(), positions=[])
        m_exit.assert_not_called()             # 止损不用问模型
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")

    def test_after_min_hold_model_decides(self):
        self._open_position(NOW - 90)          # 已过 60 秒
        cfg = autotrade.Cfg(dry_run=True, min_hold_sec=60,
                            min_hold_exempt_usd=0.5, tp_min_usd=0)
        with mock.patch.object(autotrade, "ask_exit",
                              return_value={"close": True, "reason": "动能衰竭",
                                            "source": "llm"}):
            row = autotrade.tick(cfg, now=NOW, quote=(4002.0, 4002.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "close")

    def test_zero_disables_the_guard(self):
        self._open_position(NOW - 5)
        cfg = autotrade.Cfg(dry_run=True, min_hold_sec=0, tp_min_usd=0)
        with mock.patch.object(autotrade, "ask_exit",
                              return_value={"close": True, "reason": "走",
                                            "source": "llm"}) as m_exit:
            autotrade.tick(cfg, now=NOW, quote=(4001.0, 4001.24),
                           rows=bars_up(), positions=[])
        m_exit.assert_called_once()

    def test_exit_system_states_the_real_gate(self):
        """提示词必须和真实闸门一致，否则模型会以为还能自己扛单。

        2026-10-26 起阈值改成占位符渲染：原来写死 3.0/3.25/60，
        cfg 一改 system 就在骗模型（它按 3.0 判断"系统什么时候砍我"，
        实际却是 5.0）。所以断言必须打在**渲染之后**的文本上，
        并且验证改 cfg 时文案真的跟着变。
        """
        cfg = autotrade.Cfg()
        sys_text = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg)
        self.assertIn(f"浮盈不到 {cfg.tp_min_usd:g} USD", sys_text)
        self.assertIn("系统直接市价平", sys_text)
        # 旧文案说"没有止损也没有止盈"，现在程序层有止损，不能再这么写
        self.assertNotIn("没有止损也没有止盈", sys_text)
        # 占位符必须被替换干净，不能漏一个到模型眼前
        self.assertNotIn("{", sys_text.replace('{"close"', "") .replace('""}', ""))

    def test_exit_system_follows_cfg_not_hardcoded(self):
        """改 sl_usd / tp_min_usd / max_hold_sec，system 文案必须跟着变。"""
        cfg = autotrade.Cfg(sl_usd=5.0, tp_min_usd=4.5, max_hold_sec=1800)
        sys_text = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg)
        self.assertIn("浮亏达到 5 USD", sys_text)
        self.assertIn("浮盈不到 4.5 USD", sys_text)
        self.assertIn("持仓满 30 分钟", sys_text)
        self.assertNotIn("3.25", sys_text)
        self.assertNotIn("3.0 USD", sys_text)


class TestIgnoreSpread(unittest.TestCase):
    """用户要求「无视点差成本」：数字从输入里拿掉，规则里明说不许拿成本当借口。"""

    def test_entry_prompt_has_no_cost_number(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        self.assertNotIn("来回成本", p)
        self.assertNotIn("成本约", p)
        self.assertIn("点差=240 点", p)   # 原始点差数据保留（那是行情，不是成本判断）

    def test_system_says_cost_is_not_yours(self):
        self.assertIn("成本不由你负责", autotrade.ENTRY_SYSTEM)
        self.assertIn("不要以", autotrade.ENTRY_SYSTEM)
        self.assertNotIn("算账", autotrade.ENTRY_SYSTEM)

    def test_exit_system_also_ignores_cost(self):
        self.assertIn("成本不由你负责", autotrade.EXIT_SYSTEM)


class TestCacheStats(TempLedger):
    def test_sums_hit_and_miss(self):
        autotrade._log_call(True, {"cache_hit_tokens": 100, "cache_miss_tokens": 400})
        autotrade._log_call(True, {"cache_hit_tokens": 300, "cache_miss_tokens": 200})
        s = autotrade.cache_stats()
        self.assertEqual(s["hit_tokens"], 400)
        self.assertEqual(s["miss_tokens"], 600)
        self.assertAlmostEqual(s["hit_rate"], 0.4, places=4)

    def test_none_when_platform_does_not_report(self):
        """服务端不返回缓存字段时不许编一个数字出来。"""
        autotrade._log_call(True, {"prompt_tokens": 1000})
        self.assertIsNone(autotrade.cache_stats()["hit_rate"])

    def test_empty_ledger(self):
        s = autotrade.cache_stats()
        self.assertEqual(s["calls"], 0)
        self.assertIsNone(s["hit_rate"])


class TestLlmUsage(unittest.TestCase):
    """确认 usage 里的缓存字段真的被读出来，否则面板永远显示不了命中率。"""

    def _client_returning(self, captured, content="hi"):
        class Usage:
            prompt_tokens = 1000
            completion_tokens = 20
            total_tokens = 1020
            model_extra = {"prompt_cache_hit_tokens": 860,
                           "prompt_cache_miss_tokens": 140}

        class Msg:
            def __init__(self):
                self.content = content

        class Choice:
            def __init__(self):
                self.message = Msg()

        class Resp:
            def __init__(self):
                self.choices = [Choice()]
                self.usage = Usage()

        class Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return Resp()

        class Chat:
            completions = Completions()

        class Client:
            chat = Chat()

        return Client()

    def test_reads_cache_tokens(self):
        from agent import llm as L

        captured = {}
        with mock.patch.object(L, "_client",
                               return_value=self._client_returning(captured)):
            _, usage = L.chat_ex("p", system="s")
        self.assertEqual(usage["cache_hit_tokens"], 860)
        self.assertEqual(usage["cache_miss_tokens"], 140)

    def test_json_mode_sets_response_format(self):
        from agent import llm as L

        captured = {}
        with mock.patch.object(L, "_client",
                               return_value=self._client_returning(captured)):
            L.chat_ex("p", json_mode=True)
        self.assertEqual(captured.get("response_format"), {"type": "json_object"})

    def test_no_json_mode_by_default(self):
        from agent import llm as L

        captured = {}
        with mock.patch.object(L, "_client",
                               return_value=self._client_returning(captured)):
            L.chat_ex("p")
        self.assertNotIn("response_format", captured)
