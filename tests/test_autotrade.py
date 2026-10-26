"""AI 实时做单模块的单元测试（不依赖 MT5 终端、不花钱调模型）。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths

NOW = 1_790_000_000


def bar(o, h, l, c, t=NOW):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def bars_up(n=30):
    return [bar(4000 + i, 4001 + i, 3999 + i, 4000.5 + i, NOW + i * 60) for i in range(n)]


def bars_down(n=30):
    return [bar(4000 - i, 4001 - i, 3999 - i, 3999.5 - i, NOW + i * 60) for i in range(n)]


class TempLedger(unittest.TestCase):
    """每个用例独立的账本目录，绝不碰真实账户数据。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()


# ------------------------------------------------------------------ 解析


class TestParsePUp(unittest.TestCase):
    def test_clean_json(self):
        got = autotrade.parse_p_up('{"p_up": 0.62, "reason": "近 5 根连阳"}')
        self.assertAlmostEqual(got["p_up"], 0.62)
        self.assertEqual(got["reason"], "近 5 根连阳")

    def test_percent_number(self):
        got = autotrade.parse_p_up('{"p_up": 62}')
        self.assertAlmostEqual(got["p_up"], 0.62)

    def test_percent_string(self):
        got = autotrade.parse_p_up('{"p_up": "62%"}')
        self.assertAlmostEqual(got["p_up"], 0.62)

    def test_code_fence(self):
        text = '```json\n{"p_up": 0.41, "reason": "震荡"}\n```'
        got = autotrade.parse_p_up(text)
        self.assertAlmostEqual(got["p_up"], 0.41)

    def test_broken_json_still_finds_number(self):
        got = autotrade.parse_p_up('我的判断 p_up: 0.73 因为突破了')
        self.assertAlmostEqual(got["p_up"], 0.73)

    def test_out_of_range_rejected(self):
        # 500 不是合法概率，必须判失败，不能硬夹到 1.0 —— 夹了就等于替模型编数字
        self.assertIsNone(autotrade.parse_p_up('{"p_up": 500}'))

    def test_garbage_rejected(self):
        self.assertIsNone(autotrade.parse_p_up("看涨"))
        self.assertIsNone(autotrade.parse_p_up(""))
        self.assertIsNone(autotrade.parse_p_up(None))


class TestParseExit(unittest.TestCase):
    def test_true(self):
        self.assertTrue(autotrade.parse_exit('{"close": true, "reason": "动能衰竭"}')["close"])

    def test_false(self):
        self.assertFalse(autotrade.parse_exit('{"close": false, "reason": "还在趋势里"}')["close"])

    def test_chinese_action(self):
        self.assertTrue(autotrade.parse_exit('{"close": "平仓", "reason": "反转"}')["close"])

    def test_broken_json(self):
        self.assertTrue(autotrade.parse_exit('close: true')["close"])

    def test_garbage(self):
        self.assertIsNone(autotrade.parse_exit("再观察一下"))


# ------------------------------------------------------------------ 模型失败不兜底
#
# 以前模型调不通会退回「最近 10 根动量」猜概率，照样能过闸门真开单。
# 换成免费 API 之后 429 会很常见，让规则替我下单比少做一笔危险得多。


class TestNoRuleFallback(unittest.TestCase):
    def setUp(self):
        autotrade._JSON_MODE_OK = True

    def tearDown(self):
        autotrade._JSON_MODE_OK = True

    def test_entry_api_failure_means_no_trade(self):
        def boom(*a, **k):
            raise RuntimeError("429 Too Many Requests")

        with mock.patch("agent.llm.chat_ex", boom):
            got = autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())

        self.assertFalse(got["act"])
        self.assertEqual(got["source"], "error")
        self.assertIn("429", got["reason"])

    def test_entry_garbage_means_no_trade(self):
        with mock.patch("agent.llm.chat_ex", lambda *a, **k: ("看不懂", {})):
            got = autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())

        self.assertFalse(got["act"])
        self.assertEqual(got["source"], "error")
        self.assertIn("无法解析", got["reason"])

    def test_exit_api_failure_holds(self):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": NOW - 60}

        def boom(*a, **k):
            raise RuntimeError("timeout")

        with mock.patch("agent.llm.chat_ex", boom):
            got = autotrade.ask_exit(bars_up(20), pos, 4040.0, 4040.24,
                                     autotrade.Cfg())

        self.assertFalse(got["close"])       # 不知道就继续持有，不瞎平
        self.assertEqual(got["source"], "error")


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


# ------------------------------------------------------------------ 1 分钟内重开


class TestNextWait(unittest.TestCase):
    """平仓后必须尽快唤醒，否则「1 分钟内重新开仓」这条需求做不到。"""

    def test_close_wakes_just_after_cooldown(self):
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=30)
        self.assertEqual(autotrade.next_wait_sec({"kind": "close"}, cfg), 31)

    def test_other_kinds_wait_full_interval(self):
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=30)
        for kind in ("open", "hold", "skip", "open_failed"):
            self.assertEqual(autotrade.next_wait_sec({"kind": kind}, cfg), 60)

    def test_close_wait_never_reaches_a_minute(self):
        for cd in (0, 5, 10, 30, 45, 58):
            cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=cd)
            self.assertLess(autotrade.next_wait_sec({"kind": "close"}, cfg), 60)

    def test_cooldown_over_a_minute_is_rejected(self):
        """冷却 ≥60 秒时 1 分钟内重开数学上不可能，必须启动就拦。"""
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(cooldown_sec=30)), [])
        issues = autotrade.validate_cfg(autotrade.Cfg(cooldown_sec=60))
        self.assertTrue(any("1 分钟内" in x for x in issues))

    def test_validate_cfg_catches_bad_lots_and_interval(self):
        bad = autotrade.validate_cfg(autotrade.Cfg(lots=0, interval_sec=2))
        self.assertEqual(len(bad), 2)
        self.assertTrue(any("3 秒" in x for x in bad))

    def test_three_seconds_is_the_floor(self):
        """模型单次约 2.6 秒，3 秒是最小可持续周期；2 秒会堆积请求。"""
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(interval_sec=3)), [])
        self.assertEqual(len(autotrade.validate_cfg(autotrade.Cfg(interval_sec=2))), 1)

    def test_default_interval_is_60s(self):
        """用户 10-26 明确要求默认 60 秒。3 秒档等于不间断连打模型。"""
        self.assertEqual(autotrade.Cfg().interval_sec, 60)

    def test_cooldown_never_longer_than_interval(self):
        # 冷却配得比周期还长时，也不能让等待超过一个周期
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=120)
        self.assertEqual(autotrade.next_wait_sec({"kind": "close"}, cfg), 60)


# ------------------------------------------------------------------ 止损快查


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
        """平仓轮按 cooldown+1 唤醒，那时候手上已经没有仓了。"""
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


# ------------------------------------------------------------------ 开仓闸门


class TestCanOpen(unittest.TestCase):
    def setUp(self):
        self.cfg = autotrade.Cfg()

    def test_fresh_state_can_open(self):
        ok, why = autotrade.can_open(autotrade._empty_state(), NOW, False, self.cfg)
        self.assertTrue(ok, why)

    def test_blocked_when_position_exists(self):
        ok, why = autotrade.can_open(autotrade._empty_state(), NOW, True, self.cfg)
        self.assertFalse(ok)
        self.assertIn("已有持仓", why)

    def test_cooldown_after_close(self):
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 10
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertFalse(ok)
        self.assertIn("冷却", why)
        self.assertIn("20 秒", why)

    def test_cooldown_expired(self):
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 31
        ok, _ = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertTrue(ok)

    def test_same_tick_double_open_guard(self):
        """只防同一轮重复开仓，不是交易节奏。用 60 秒当守卫会把自己锁死。"""
        state = autotrade._empty_state()
        state["last_open_ts"] = NOW - 1
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertFalse(ok)
        self.assertIn("距上次开仓", why)

    def test_reopen_allowed_within_one_minute_after_close(self):
        """核心需求：一单做完，1 分钟内必须能重新开仓。

        冷却 30 秒 + 3 秒兜底守卫都不该挡住 31 秒后的重开。
        """
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 31
        state["last_open_ts"] = NOW - 90   # 上一笔是 90 秒前开的
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertTrue(ok, why)

    def test_stop_reason_blocks_everything(self):
        state = autotrade._empty_state()
        state["stop_reason"] = "手工熔断"
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertFalse(ok)
        self.assertIn("已停止", why)

    def test_max_trades(self):
        cfg = autotrade.Cfg(max_trades=2)
        state = autotrade._empty_state()
        state["trades"] = 2
        ok, why = autotrade.can_open(state, NOW, False, cfg)
        self.assertFalse(ok)
        self.assertIn("上限", why)


# ------------------------------------------------------------------ tick 行为


class TestTick(TempLedger):
    def setUp(self):
        super().setUp()
        # 默认走真实路径（send_open / send_close 会被 mock 掉），
        # 纸面模式的两个用例自己显式传 dry_run=True。
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False)

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
        cfg = autotrade.Cfg(dry_run=True)
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
                            min_edge=0)  # 这里只验证来源标记，先关掉强度闸门
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
        cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False, min_edge=0)
        with mock.patch.object(autotrade, "ask_entry",
                              return_value={"p_up": 0.51, "reason": "微偏",
                                            "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0, "order": 701}):
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")


# ------------------------------------------------------------------ 汇总


# ------------------------------------------------------------------ 真单失败处理


class TestFatalOrderFailure(TempLedger):
    """券商拒绝时必须自停。不停的话每分钟重试一次，每轮都要花一次模型调用。"""

    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(dry_run=False, interval_sec=60, cooldown_sec=30)

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


# ------------------------------------------------------------------ 连接保活


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


# ------------------------------------------------------------------ 一键清仓


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


# ------------------------------------------------------------------ 面板线程


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


# ------------------------------------------------------------------ 信息边界
#
# 用户指定的硬约束：AI 交易员只能看「当前 K 线 + 之前 13 根」。
# 这里测的不是提示词写了什么，而是**喂进去的数据真的只有 14 根**。
# 提示词可以写得很漂亮但传了 120 根进去，那种"约束"是假的。


class TestVisibleBars(unittest.TestCase):
    def test_caps_at_14(self):
        self.assertEqual(len(autotrade.visible_bars(bars_up(40))), 14)
        self.assertEqual(len(autotrade.visible_bars(bars_up(14))), 14)

    def test_fewer_bars_passed_through(self):
        self.assertEqual(len(autotrade.visible_bars(bars_up(3))), 3)

    def test_empty_is_safe(self):
        self.assertEqual(autotrade.visible_bars([]), [])

    def test_keeps_the_latest_not_the_oldest(self):
        rows = bars_up(40)
        got = autotrade.visible_bars(rows)
        self.assertEqual(got[-1]["time"], rows[-1]["time"])       # 含当前那根
        self.assertEqual(got[0]["time"], rows[-14]["time"])       # 往前数到第 14 根

    def test_entry_prompt_contains_exactly_14_bar_lines(self):
        p = autotrade.build_entry_prompt(bars_up(60), 4040.0, 4040.24)
        self.assertEqual(p.count("T-"), 13)          # T-13 … T-1
        # 数 K 线行，不能数 "当前" 两个字——提示词里还有"当前报价""当前 1 根"
        lines = re.findall(r"^(?:T-\d+|当前)\s+\d{2}-\d{2}", p, re.M)
        self.assertEqual(len(lines), 14)
        self.assertNotIn("T-14", p)                  # 第 15 根不许出现

    def test_exit_prompt_contains_exactly_14_bar_lines(self):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "opened_at": NOW - 60}
        p = autotrade.build_exit_prompt(bars_up(60), pos, 4040.0, 4040.24)
        self.assertEqual(p.count("T-"), 13)
        self.assertNotIn("T-14", p)

    def test_prompt_states_the_boundary(self):
        p = autotrade.build_entry_prompt(bars_up(60), 4040.0, 4040.24)
        self.assertIn("14 根", p)

    def test_returns_use_13_not_15(self):
        """短周期变化不能偷偷用 15 根窗口——那等于多看一眼。"""
        rows = bars_up(60)
        r1, r5, r13, span = autotrade._returns(rows)
        w = autotrade.visible_bars(rows)
        want = (w[-1]["close"] - w[-14]["close"]) / w[-14]["close"]
        self.assertAlmostEqual(r13, want, places=6)


# ------------------------------------------------------------------ tick 级数据
#
# 用户要的是「市场报价」那两根线背后的原始流。但 14 分钟黄金几千个 tick，
# 直接塞给模型既贵又没用，所以按分钟聚合成表 + 附最后十几笔原始 tick。
# 这里钉的是：分桶对、边界对、拿不到数据时 prompt 不能缺字段。


def ticks_for(bars, per_bar=3, step=0.1, spread=0.24):
    out = []
    for b in bars:
        t0 = int(b["time"])
        for k in range(per_bar):
            out.append({"time": t0 + k, "time_msc": 0,
                        "bid": 4000 + k * step, "ask": 4000 + k * step + spread,
                        "last": 0.0, "volume": 1})
    return out


class TestTickSummary(unittest.TestCase):
    def test_buckets_follow_visible_bars(self):
        bars = bars_down(30)
        ticks = ticks_for(bars)
        s = autotrade.summarize_ticks(ticks, bars, point=0.001)

        self.assertTrue(s["ok"])
        self.assertEqual(len(s["rows"]), 14)      # 只统计可见的 14 根
        self.assertTrue(all(r["n"] == 3 for r in s["rows"]))
        self.assertEqual(s["total"], 42)

    def test_spread_computed_in_points(self):
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars, spread=0.24), bars, point=0.001)
        # 0.24 价格差 / 0.001 点值 = 240 点
        self.assertAlmostEqual(s["spread_avg"], 240.0, places=3)
        self.assertAlmostEqual(s["spread_max"], 240.0, places=3)

    def test_bid_range_per_minute(self):
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars, per_bar=3, step=0.3), bars, point=0.001)
        row = s["rows"][-1]
        self.assertAlmostEqual(row["bid_lo"], 4000.0, places=3)
        self.assertAlmostEqual(row["bid_hi"], 4000.6, places=3)

    def test_empty_ticks_is_not_ok(self):
        self.assertFalse(autotrade.summarize_ticks([], bars_down(20), 0.001)["ok"])

    def test_block_text_mentions_spread_and_recent(self):
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars), bars, point=0.001)
        text = autotrade._fmt_tick_block(s)
        self.assertIn("点差", text)
        self.assertIn("最近", text)
        self.assertIn("当前", text)

    # ---- 2026-10-26 瘦身：卖价区间逐行重复、点差固定时每行都印一遍 ----
    #
    # 实测原格式占 tick 块 1/3、进场提示词 8.5%，而这两段信息是**可推算的**：
    # 卖价 = 买价 + 点差，点差在 Exness 上全窗口固定 240/240。
    # 省的是重复，不是数据——「点差有没有异常」必须还能看出来。

    def test_sell_range_is_not_repeated_per_row(self):
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars), bars, point=0.001)
        text = autotrade._fmt_tick_block(s)
        self.assertNotIn("卖价区间", text)     # 列没了
        self.assertIn("卖价 = 买价 + 点差", text)  # 但换算法要在表头讲清
        self.assertEqual(text.count("点差"), 2)  # 表头一次 + 汇总行一次

    def test_fixed_spread_is_stated_once(self):
        """固定点差说「恒」，比「平均 240、最宽 240」更短也更准。"""
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars), bars, point=0.001)
        self.assertIn("点差 恒 240 点", autotrade._fmt_tick_block(s))

    def test_abnormal_spread_is_still_flagged(self):
        """点差某分钟放大时那一行必须标出来，否则等于把风控信息删了。"""
        bars = bars_down(20)
        w = autotrade.visible_bars(bars)
        ticks = ticks_for(bars, per_bar=2)
        # 把最后一根里的 tick 点差拉宽到 0.7（700 点）
        last_t = int(w[-1]["time"])
        for t in ticks:
            if t["time"] >= last_t:
                t["ask"] = t["bid"] + 0.7
        s = autotrade.summarize_ticks(ticks, bars, point=0.001)
        text = autotrade._fmt_tick_block(s)
        self.assertIn("点差 700/700", text)
        # 基准（中位数）仍是 240，其他行不该跟着印，也不再宣称「恒」
        self.assertNotIn("点差 恒", text)
        self.assertEqual(text.count("240/240"), 0)

    def test_block_is_materially_shorter(self):
        """回归护栏：瘦身后长度不能反弹。原格式在这组数据上是 1612 字符量级。"""
        bars = bars_down(20)
        s = autotrade.summarize_ticks(ticks_for(bars, per_bar=20), bars, point=0.001)
        text = autotrade._fmt_tick_block(s)
        self.assertLess(len(text.split("\n")[0]), 60)   # 表头不许再胖回去
        for line in text.split("\n")[1:15]:
            if line.strip() and line[0] in "T当":
                self.assertLess(len(line), 45, f"每分钟行太宽: {line!r}")

    def test_unavailable_block_is_explicit(self):
        text = autotrade._fmt_tick_block({"ok": False})
        self.assertIn("不可用", text)


class TestTickWindow(unittest.TestCase):
    def setUp(self):
        self.cfg = autotrade.Cfg(ticks=True)

    def test_filters_out_of_window_ticks(self):
        bars = bars_down(30)
        w = autotrade.visible_bars(bars)
        good = ticks_for(w, per_bar=2)
        outside = [{"time": int(w[0]["time"]) - 600, "bid": 1.0, "ask": 1.2, "time_msc": 0},
                   {"time": int(w[-1]["time"]) + 9999, "bid": 1.0, "ask": 1.2, "time_msc": 0}]
        with mock.patch("mt5.bridge.get_ticks_live", return_value=good + outside):
            got = autotrade.fetch_ticks(self.cfg, bars)
        self.assertEqual(len(got), len(good))

    def test_fetch_failure_returns_empty(self):
        with mock.patch("mt5.bridge.get_ticks_live", side_effect=RuntimeError("no")):
            self.assertEqual(autotrade.fetch_ticks(self.cfg, bars_down(20)), [])

    def test_disabled_flag_skips_fetch(self):
        cfg = autotrade.Cfg(ticks=False)
        with mock.patch("mt5.bridge.get_ticks_live") as m:
            autotrade.fetch_ticks(cfg, bars_down(20))
        m.assert_not_called()
        self.assertIn("已关闭", autotrade._ticks_text(cfg, bars_down(20)))


class TestTickInPrompt(unittest.TestCase):
    def test_entry_prompt_carries_tick_block(self):
        p = autotrade.build_entry_prompt(bars_down(20), 4040.0, 4040.24,
                                         ticks_text="TICKBLOCK-测试")
        self.assertIn("TICKBLOCK-测试", p)
        self.assertIn("Tick 级数据", p)

    def test_exit_prompt_carries_tick_block(self):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "opened_at": NOW - 60}
        p = autotrade.build_exit_prompt(bars_down(20), pos, 4040.0, 4040.24,
                                        ticks_text="TICKBLOCK-测试")
        self.assertIn("TICKBLOCK-测试", p)

    def test_prompt_says_unavailable_when_missing(self):
        p = autotrade.build_entry_prompt(bars_down(20), 4040.0, 4040.24)
        self.assertIn("暂不可用", p)

    def test_tick_block_does_not_break_14_bar_cap(self):
        """加 tick 不能把 K 线数量撑破边界。"""
        p = autotrade.build_entry_prompt(bars_down(60), 4040.0, 4040.24,
                                         ticks_text="x")
        self.assertEqual(p.count("T-13"), 1)
        self.assertNotIn("T-14", p)


# ------------------------------------------------------------------ 提示词结构
#
# 目的只有一个：让**能缓存的部分尽量长**、**每次都变的部分尽量短**。
# 做法是静态规则全进 system，user 消息只放数据，且从"最不变"排到"最易变"，
# 时间戳放最后一行。这样 system 整段是稳定前缀，能被 DeepSeek 的自动前缀缓存命中。


class TestPromptLayout(unittest.TestCase):
    def test_user_message_starts_with_stable_fields(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        first = p.splitlines()[0]
        self.assertTrue(first.startswith("品种 XAUUSDm"), first)
        self.assertIn("M1", first)

    def test_timestamp_is_the_last_line(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        self.assertTrue(p.strip().splitlines()[-1].startswith("时间 "))

    def test_exit_timestamp_is_the_last_line(self):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "opened_at": NOW - 60}
        p = autotrade.build_exit_prompt(bars_down(20), pos, 4040.0, 4040.24)
        self.assertTrue(p.strip().splitlines()[-1].startswith("时间 "))

    def test_rules_live_in_system_not_user(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        self.assertIn("超短线", autotrade.ENTRY_SYSTEM)
        self.assertIn("信息边界", autotrade.ENTRY_SYSTEM)
        # 规则不该混进动态数据里，否则每轮都当新内容重算
        self.assertNotIn("超短线的敌人是成本", p)
        self.assertNotIn("必须给出方向", p)

    def test_system_is_byte_stable(self):
        """system 里不能出现任何会变的东西，否则缓存前缀天天作废。"""
        for sys in (autotrade.ENTRY_SYSTEM, autotrade.EXIT_SYSTEM, autotrade.REVIEW_SYSTEM):
            self.assertNotIn(str(NOW), sys)
            self.assertNotIn("2026", sys)
            # JSON 示例里的花括号是静态文本，不是 format 占位，允许存在

    def test_prices_use_fixed_decimals(self):
        # 1980.5 / 1980.50 这种长度抖动会破坏前缀，黄金价格必须固定三位
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        bar_lines = [ln for ln in p.splitlines() if ln.startswith(("T-", "当前  "))]
        self.assertTrue(bar_lines)
        for ln in bar_lines:
            prices = ln.split()[3:]          # 前三个是 序号 / 日期 / 时间
            self.assertEqual(len(prices), 4, ln)   # 开高低收
            for token in prices:
                self.assertRegex(token, r"^\d+\.\d{3}$", ln)


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


# ------------------------------------------------------------------ 行情结构 ER
#
# 用户给的打法分两种：小波动低买高卖、大波动回踩顺势。
# 要让模型别用反，就得先能判"现在是哪种"。ER = 净移动 ÷ 总行程，
# 一路直行≈1（趋势），来回磨≈0（震荡）。两个价格相除，没有黑箱参数。


def bars_from_closes(closes):
    return [{"time": NOW + i * 60, "open": c, "high": c + 1, "low": c - 1, "close": c}
            for i, c in enumerate(closes)]


class TestStructure(unittest.TestCase):
    def test_straight_line_is_trend(self):
        s = autotrade._structure(bars_from_closes([4000 + i for i in range(14)]))
        self.assertAlmostEqual(s["er"], 1.0, places=4)
        self.assertEqual(s["streak"], 13)
        self.assertIn("单边趋势", autotrade._structure_text(
            bars_from_closes([4000 + i for i in range(14)])))

    def test_zigzag_is_range(self):
        closes = [4000 + (2 if i % 2 else 0) for i in range(14)]
        s = autotrade._structure(bars_from_closes(closes))
        self.assertLess(s["er"], 0.30)
        self.assertIn("窄幅震荡", autotrade._structure_text(bars_from_closes(closes)))

    def test_position_percent(self):
        # 收盘在区间最上方 → 100%
        s = autotrade._structure(bars_from_closes([4000 + i for i in range(14)]))
        self.assertGreater(s["pos"], 0.85)
        # 收盘在区间最下方
        s2 = autotrade._structure(bars_from_closes([4000 - i for i in range(14)]))
        self.assertLess(s2["pos"], 0.15)

    def test_down_streak_is_negative(self):
        s = autotrade._structure(bars_from_closes([4000 - i for i in range(14)]))
        self.assertEqual(s["streak"], -13)

    def test_flat_market_is_safe(self):
        s = autotrade._structure(bars_from_closes([4000.0] * 14))
        self.assertEqual(s["er"], 0.0)
        self.assertAlmostEqual(s["pos"], 0.5)

    def test_too_few_bars(self):
        self.assertFalse(autotrade._structure(bars_from_closes([4000]))["ok"])
        self.assertIn("不足", autotrade._structure_text(bars_from_closes([4000])))

    def test_zone_judgement_uses_threshold(self):
        """位置判定由服务端算好，阈值放宽后模型跟着变，不许各算各的。"""
        # 收盘在区间 25% 附近：震荡区间 3999~4003，收盘 4000
        rows = bars_from_closes([4000, 4002, 4000, 4002, 4000, 4002,
                                 4000, 4002, 4000, 4002, 4000, 4002, 4000, 4000])
        tight = autotrade._structure_text(rows, zone_edge=0.20)
        loose = autotrade._structure_text(rows, zone_edge=0.35)
        self.assertIn("中间地带", tight)      # 旧阈值 20%：站稳 25% 不算贴边
        self.assertIn("贴下沿", loose)        # 放宽到 35% 后可以做多了
        self.assertIn("阈值 35%", loose)

    def test_zone_edge_boundaries(self):
        rows = bars_from_closes([4000, 4002, 4000, 4002, 4000, 4002,
                                 4000, 4002, 4000, 4002, 4000, 4002, 4000, 4002])
        text = autotrade._structure_text(rows, zone_edge=0.35)
        self.assertIn("贴上沿", text)          # 收在区间最高 → 做空侧

    def test_zone_edge_is_clamped(self):
        rows = bars_from_closes([4000 + i for i in range(14)])
        self.assertIn("位置判定", autotrade._structure_text(rows, zone_edge=9.9))

    # ---- 多空对称：位置判定必须带趋势方向（实测「只做多」的根因）----
    # 2026-10-25 话术降级：「不许接多」→「程序倾向」。判定逻辑不变，
    # 变的是语气——模型可不同意，但要给出理由（拆掉逼出 p_up=0.0 的框）。

    def test_downtrend_lower_edge_forbids_long(self):
        """下跌趋势里贴下沿是弱势延续，不是超卖——程序倾向不接多。

        漏了这一条时，14 根窗口在跌势里永远处于区间下部，位置判定永远显示
        「可考虑做多」；同时想空又被「追破位」铁律按住，结果只会做多。
        """
        rows = bars_from_closes([4000 - i for i in range(14)])
        text = autotrade._structure_text(rows)
        self.assertIn("下跌趋势", text)
        self.assertIn("倾向等反抽到上沿做空", text)
        self.assertIn("你可以不同意", text)

    def test_uptrend_upper_edge_forbids_chasing(self):
        rows = bars_from_closes([4000 + i for i in range(14)])
        text = autotrade._structure_text(rows)
        self.assertIn("倾向等回踩到下沿低吸", text)
        self.assertIn("你可以不同意", text)

    def test_range_keeps_symmetric_edges(self):
        """震荡里贴下沿仍然是买点，别把趋势线规则误用到震荡。"""
        rows = bars_from_closes([4002, 4000] * 7)     # 收在区间最低
        self.assertIn("贴下沿（程序倾向：可考虑做多", autotrade._structure_text(rows))

    def test_structure_line_shows_direction(self):
        """净移动必须带符号：只给绝对值，模型分不清在涨还是在跌。"""
        rows = bars_from_closes([4000 - i for i in range(14)])
        self.assertLess(autotrade._structure(rows)["drift"], 0)
        self.assertIn("下行", autotrade._structure_text(rows))

    def test_zone_text_direct(self):
        """直接钉 pos/er/drift 的组合，绕开构造 K 线的麻烦。"""
        # 跌势贴下沿 → 程序倾向等反抽做空
        self.assertIn("倾向等反抽到上沿做空", autotrade._zone_text(0.10, 0.70, -5.0, 0.35))
        # 涨势贴下沿 → 回踩到位倾向做多
        self.assertIn("倾向做多", autotrade._zone_text(0.10, 0.70, 5.0, 0.35))
        # 涨势贴上沿 → 程序倾向不追多
        self.assertIn("倾向等回踩到下沿低吸", autotrade._zone_text(0.90, 0.70, 5.0, 0.35))
        # 跌势贴上沿 → 反抽到位倾向做空
        self.assertIn("倾向做空", autotrade._zone_text(0.90, 0.70, -5.0, 0.35))
        # 震荡两边都对称
        self.assertIn("可考虑做多", autotrade._zone_text(0.10, 0.10, -5.0, 0.35))
        self.assertIn("可考虑做空", autotrade._zone_text(0.90, 0.10, 5.0, 0.35))
        # 中间地带不动手
        self.assertIn("不动手", autotrade._zone_text(0.50, 0.70, -5.0, 0.35))

    def test_prog_direction_matches_zone_text(self):
        """_prog_direction 必须和 _zone_text 的建议同向——各算各的必然漂移。"""
        # 跌势贴下沿 → 倾向空（等反抽）
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 - i for i in range(14)]), 0.35), -1)
        # 涨势贴下沿 → 倾向多（回踩到位）
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 + i for i in range(14)]), 0.35), 1)
        # 震荡收在区间最低 → 倾向多
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4002, 4000] * 7), 0.35), 1)
        # 中间地带 → 无建议（bars_from_closes 每根带 ±1 影线，
        # 收盘要真落在区间 35%~65% 里，得用比影线更高的振幅）
        mid_rows = [{"time": NOW + i * 60, "open": 4000 + (2 if i % 2 else 0),
                     "high": 4000 + (2 if i % 2 else 0) + 3,
                     "low": 4000 + (2 if i % 2 else 0) - 3,
                     "close": 4000 + (2 if i % 2 else 0)} for i in range(14)]
        self.assertEqual(autotrade._prog_direction(mid_rows, 0.35), 0)

    def test_system_suggests_not_commands(self):
        """提示词必须把位置判定定位成建议而非命令（p_up 两极分化的根因）。"""
        self.assertIn("建议", autotrade.ENTRY_SYSTEM)
        self.assertNotIn("唯一合法的方向", autotrade.ENTRY_SYSTEM)
        self.assertIn("可以不同意", autotrade.ENTRY_SYSTEM)
        self.assertIn("0.20 ~ 0.80", autotrade.ENTRY_SYSTEM)

    def test_parse_p_up_is_clamped(self):
        """0/1 是伪精确，代码层兜底截断到 [0.20, 0.80]。"""
        got = autotrade.parse_p_up('{"act": true, "p_up": 0.0, "reason": "x"}')
        self.assertAlmostEqual(got["p_up"], 0.20)
        got = autotrade.parse_p_up('{"act": true, "p_up": 1.0, "reason": "x"}')
        self.assertAlmostEqual(got["p_up"], 0.80)
        got = autotrade.parse_p_up('{"act": true, "p_up": 0.62, "reason": "x"}')
        self.assertAlmostEqual(got["p_up"], 0.62)
        # agree 字段解析：认得出的归布尔，没给的保持 None
        self.assertIs(autotrade.parse_p_up(
            '{"act": true, "p_up": 0.6, "agree": true, "reason": "x"}')["agree"], True)
        self.assertIsNone(autotrade.parse_p_up(
            '{"act": true, "p_up": 0.6, "reason": "x"}')["agree"])

    def test_system_forbids_one_side_locking(self):
        """提示词必须明说"机会对等"，否则教训里的偏空描述会锁死方向。

        实测 10-26：p_up<0.5 的 38 次机会一次空单都没开。旧文案那句
        「贴下沿 ≠ 买点，贴上沿 ≠ 卖点」是无条件的，模型读成"贴顶不许做空"，
        连震荡市（ER≈0）的高抛都一起禁了——把「低买高卖」废掉一半。
        """
        sys = autotrade.ENTRY_SYSTEM
        self.assertIn("机会是对等的", sys)
        # 震荡市必须显式允许贴边动手，这是唯一能"到了就做"的情形
        self.assertIn("震荡市贴边就动手", sys)
        self.assertIn("贴着上沿做空不叫追空", sys)
        # 看跌 ≠ 不做，这句是把 p_up 语义钉死的
        self.assertIn("看跌就要考虑做空", sys)
        # 趋势里"等反抽"不能被读成"永不出手"
        self.assertIn("不等于永远不做", sys)


class TestMA28(unittest.TestCase):
    """M1 的 MA28 背景偏向（用户 10-26 要求加，他自己做单也看这条）。

    28 根是可见窗口（14 根）的两倍，所以**取数要多拉**；但模型仍然只看到
    最近 14 根原始 K 线——多出来的只落成一个均线数字，和 M5 摘要同理。
    这条边界要是破了，等于偷偷给它多看一倍行情。
    """

    def test_above_ma_is_bullish(self):
        line = autotrade.ma_line(bars_up(30))       # 单调上行
        self.assertIn("MA28", line)
        self.assertIn("上方", line)
        self.assertIn("中期偏多", line)

    def test_below_ma_is_bearish(self):
        line = autotrade.ma_line(bars_down(30))
        self.assertIn("下方", line)
        self.assertIn("中期偏空", line)

    def test_not_enough_bars_returns_empty(self):
        """数据不够就整块不出现，绝不编一个数出来。"""
        self.assertEqual(autotrade.ma_line(bars_up(14)), "")
        self.assertEqual(autotrade.ma_line(bars_up(27)), "")

    def test_flat_price_is_neutral(self):
        rows = [bar(4000, 4000.4, 3999.6, 4000.0, NOW + i * 60) for i in range(30)]
        self.assertIn("中性", autotrade.ma_line(rows))

    def test_matches_manual_average(self):
        rows = bars_up(30)
        expect = sum(r["close"] for r in rows[-28:]) / 28
        self.assertAlmostEqual(autotrade._ma_calc(rows)["ma"], expect, places=6)

    def test_prompt_carries_ma_but_still_14_bars(self):
        """多拉的 28 根不能变成"模型可以多看 K 线"。"""
        p = autotrade.build_entry_prompt(bars_up(40), 4040.0, 4040.24)
        self.assertIn("MA28", p)
        lines = re.findall(r"^(?:T-\d+|当前)\s+\d{2}-\d{2}", p, re.M)
        self.assertEqual(len(lines), 14)
        self.assertNotIn("T-14", p)

    def test_prompt_omits_ma_line_when_data_short(self):
        """只有 14 根时不许留一行空的【背景】让模型猜。"""
        p = autotrade.build_entry_prompt(bars_up(14), 4040.0, 4040.24)
        self.assertNotIn("MA28", p)

    def test_feature_snapshot_records_ma(self):
        feat = autotrade.feature_snapshot(bars_up(30))
        self.assertIn("ma28", feat)
        self.assertEqual(feat["ma28_side"], 1)     # 单调上行 → 上方
        self.assertGreater(feat["ma28_gap"], 0)

    def test_feature_snapshot_skips_ma_when_short(self):
        feat = autotrade.feature_snapshot(bars_up(20))
        self.assertNotIn("ma28", feat)


class TestPromptPriority(unittest.TestCase):
    """用户明确要求：tick 是辅助，主看蜡烛图。提示词里主次写反了就白做。"""

    def test_bars_come_before_ticks(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24, ticks_text="X")
        self.assertLess(p.index("K线"), p.index("Tick 级数据"))

    def test_structure_line_present(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24)
        self.assertIn("结构 ER=", p)
        self.assertIn("判定：", p)

    def test_labels_mark_primary_and_auxiliary(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24, ticks_text="X")
        self.assertIn("【主】", p)
        self.assertIn("【辅】", p)

    def test_system_states_tick_is_auxiliary(self):
        self.assertIn("tick 级数据是辅助", autotrade.ENTRY_SYSTEM)
        self.assertIn("不得单独构成开仓理由", autotrade.ENTRY_SYSTEM)
        self.assertIn("以 K 线为准", autotrade.ENTRY_SYSTEM)

    def test_system_has_both_regimes(self):
        for kw in ("低买高卖", "回踩", "ER < 0.30", "ER > 0.50"):
            self.assertIn(kw, autotrade.ENTRY_SYSTEM)


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
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False)

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


# ------------------------------------------------------------------ 最短持仓


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


# ------------------------------------------------------------------ 缓存计量


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


# ------------------------------------------------------------------ 平仓复盘


class TestReviewWindow(unittest.TestCase):
    def _rows(self, n=60):
        return [bar(4000 + i, 4001 + i, 3999 + i, 4000.0 + i, NOW + i * 60)
                for i in range(n)]

    def test_window_is_15_bars_around_close(self):
        """复盘口径：平仓前 7 根 + 平仓这根 + 平仓后 7 根 = 15 根。"""
        close_ts = NOW + 20 * 60
        w = autotrade.review_window(self._rows(), close_ts, 60)
        self.assertEqual(len(w), 15)
        self.assertEqual(w[0]["time"], close_ts - 7 * 60)
        self.assertEqual(w[7]["time"], close_ts)          # 平仓那根在中间
        self.assertEqual(w[-1]["time"], close_ts + 7 * 60)

    def test_close_ts_is_aligned_to_bar_start(self):
        """平仓时刻在 K 线中间时，必须先对齐到 K 线起点，否则会少切半根。"""
        close_ts = NOW + 20 * 60 + 37          # 12:00:37 平仓，那根 K 线开的 12:00:00
        w = autotrade.review_window(self._rows(), close_ts, 60)
        self.assertEqual(len(w), 15)
        self.assertEqual(w[0]["time"], NOW + 13 * 60)
        self.assertEqual(w[-1]["time"], NOW + 27 * 60)

    def test_empty_rows(self):
        self.assertEqual(autotrade.review_window([], NOW, 60), [])

    def test_review_prompt_lists_window_and_marks_close_bar(self):
        close_ts = NOW + 20 * 60
        w = autotrade.review_window(self._rows(), close_ts, 60)
        pos = {"direction": -1, "lots": 0.01, "price_open": 4010.0,
               "entry_reason": "破位", "exit_reason": "动能衰竭"}
        p = autotrade.build_review_prompt(w, pos, 4005.0, 2.5, 120, close_ts=close_ts)
        self.assertIn("破位", p)
        self.assertIn("动能衰竭", p)
        self.assertIn("复盘", p)
        # 15 根、且平仓那根有标记，平仓后的走势不能被当成持仓中的走势
        self.assertIn("共 15 根", p)
        self.assertIn("← 平仓", p)
        self.assertIn("平仓后 7 根", p)


class TestReviewQueue(TempLedger):
    """平仓后不立刻复盘：排进队列，等 7 根 K 线走完再复盘，期间照常交易。"""

    def _position_state(self):
        state = autotrade._empty_state()
        state["position"] = {
            "ticket": 77, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "opened_at": NOW - 120, "p_up": 0.6, "entry_reason": "突破",
            "dry_run": True,
        }
        autotrade.save_state(state)

    def test_close_queues_instead_of_reviewing(self):
        self._position_state()
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": True, "reason": "动能衰竭",
                                             "source": "llm"}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True, tp_min_usd=0), now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up())
        self.assertEqual(row["kind"], "close")
        state = autotrade.load_state()
        self.assertEqual(len(state["pending_reviews"]), 1)
        # 平仓当场一条复盘都不许写：那时候还没有"平仓后走势"可看
        self.assertEqual([r for r in autotrade.read_log()
                          if r.get("kind") == "review"], [])

    def test_review_only_after_seven_bars(self):
        self._position_state()
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": True, "reason": "动能衰竭",
                                             "source": "llm"}):
            autotrade.tick(autotrade.Cfg(dry_run=True, tp_min_usd=0), now=NOW,
                           quote=(4000.0, 4000.24), rows=bars_up())

        cfg = autotrade.Cfg(dry_run=True)
        fake = mock.Mock(return_value={"ok": True, "lesson": "止盈太早",
                                       "bars": 15, "regime": "range"})
        with mock.patch.object(autotrade, "ask_review", fake):
            self.assertEqual(autotrade.process_due_reviews(cfg, NOW + 6 * 60), [])
            fake.assert_not_called()
            done = autotrade.process_due_reviews(cfg, NOW + 7 * 60)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["lesson"], "止盈太早")
        self.assertEqual(done[0]["closed_at"], NOW)
        self.assertEqual(autotrade.load_state()["pending_reviews"], [])

    def test_tick_processes_due_review_and_still_trades(self):
        """到期复盘跑掉的同时这一轮照常判断行情——复盘不能挡住交易。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 7 * 60
        state["pending_reviews"] = [{
            "ticket": 76, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "entry_reason": "突破", "exit_reason": "动能衰竭",
            "opened_at": NOW - 9 * 60, "closed_at": NOW - 7 * 60,
            "exit_price": 4001.0, "pnl": 0.5,
        }]
        autotrade.save_state(state)

        fake_review = mock.Mock(return_value={"ok": True, "lesson": "止盈太早",
                                              "bars": 15, "regime": "range"})
        with mock.patch.object(autotrade, "ask_review", fake_review), \
             mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "突破", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4001.2, "order": 555}):
            row = autotrade.tick(autotrade.Cfg(interval_sec=60), now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open")
        fake_review.assert_called_once()
        self.assertEqual(autotrade.load_state()["pending_reviews"], [])

    def test_summary_reports_pending(self):
        state = autotrade._empty_state()
        state["pending_reviews"] = [{"closed_at": NOW, "ticket": 1}]
        autotrade.save_state(state)
        s = autotrade.summary()
        self.assertEqual(s["pending_reviews"], 1)
        self.assertEqual(s["next_review_ts"],
                         NOW + autotrade.REVIEW_AFTER_BARS * 60)


class TestResetHistory(TempLedger):
    """重置：清空教训和战绩，旧流水归档而不是删除。"""

    def _seed(self):
        autotrade.append_log({"ts": NOW, "kind": "open", "direction": 1, "p_up": 0.7})
        autotrade.append_log({"ts": NOW + 60, "kind": "close", "pnl": -1.0})
        autotrade.append_log({"ts": NOW + 120, "kind": "review", "lesson": "追破位被扫"})
        st = autotrade._empty_state()
        st["closed"], st["wins"], st["pnl"] = 1, 0, -1.0
        autotrade.save_state(st)

    def test_archives_then_starts_clean(self):
        self._seed()
        res = autotrade.reset_history()
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["cleared"]["opens"], 1)
        self.assertEqual(res["cleared"]["closes"], 1)
        self.assertEqual(res["cleared"]["reviews"], 1)

        # 新流水只剩一行 reset 标记；教训和战绩清零
        self.assertEqual([r.get("kind") for r in autotrade.read_log()], ["reset"])
        self.assertIn("暂无", autotrade.recent_lessons())
        self.assertEqual(autotrade.summary()["closed"], 0)

        # 旧文件还在，只是改了名——删掉就再也说不清当时为什么那么判断
        acc = autotrade.log_path().parent
        self.assertEqual(len(list(acc.glob("autotrade_log.*.jsonl"))), 1)
        self.assertEqual(len(list(acc.glob("autotrade_state.*.json"))), 1)
        self.assertEqual(res["archived"]["log"],
                         list(acc.glob("autotrade_log.*.jsonl"))[0].name)

    def test_refuses_when_position_open(self):
        st = autotrade._empty_state()
        st["position"] = {"ticket": 1, "direction": 1, "lots": 0.01,
                          "price_open": 4000.0}
        autotrade.save_state(st)
        res = autotrade.reset_history()
        self.assertFalse(res["ok"])
        self.assertIn("持仓", res["error"])
        # 拒绝时一个文件都不许动
        self.assertIsNotNone(autotrade.load_state()["position"])

    def test_reset_clears_pending_reviews(self):
        st = autotrade._empty_state()
        st["pending_reviews"] = [{"closed_at": NOW, "ticket": 1}]
        autotrade.save_state(st)
        autotrade.reset_history()
        self.assertEqual(autotrade.load_state()["pending_reviews"], [])


class TestLessons(TempLedger):
    def _add(self, lesson, ts):
        # 直接写教训库（append_lesson 走 lessons_path → active 包）。
        # 以前这里写流水、靠 lesson_rows 的 fallback 捞——fallback 移除后
        # 必须写包。语义不变：往教训库加一条。
        autotrade.append_lesson({"ts": ts, "lesson": lesson})

    def test_same_theme_is_merged_and_counted(self):
        """三条说法不同但讲同一件事的教训，必须并成一条并标次数。

        不合并的话，3 个名额会被同义句占满，等于只记住一件事。
        """
        for i, n in enumerate((4031, 4032, 4033)):
            self._add(f"在{n}追破位空被假突破打掉，下次等真跌破再进", NOW + i)
        self._add("逆势抢反弹被扫，等止跌阳线再进", NOW + 10)

        s = autotrade.recent_lessons()
        self.assertIn("已出现 3 次", s)
        self.assertIn("逆势抢反弹", s)
        # 次数多的排第一
        first = [ln for ln in s.splitlines() if ln.startswith("- ")][0]
        self.assertIn("追破位", first)

    def test_tie_prefers_newer(self):
        self._add("点差太大不要开仓", NOW)
        self._add("追破位常常是假突破", NOW + 100)
        self._add("趋势末端别抢反弹", NOW + 200)
        s = autotrade.recent_lessons(n=1)
        self.assertIn("趋势末端别抢反弹", s)

    def test_no_hedge_that_kills_the_lesson(self):
        """以前写"别当规律"，等于自己把经验废掉——模型当然照犯。"""
        self._add("点差太大不要开仓", NOW)
        s = autotrade.recent_lessons()
        self.assertNotIn("别当规律", s)
        self.assertNotIn("仅供参考", s)

    def test_empty_when_no_review(self):
        self.assertIn("暂无", autotrade.recent_lessons())

    def test_single_occurrence_marked_weak(self):
        self._add("逆势接刀不要做", NOW)
        self.assertIn("证据弱", autotrade.recent_lessons())

    def test_cost_lessons_are_filtered_out(self):
        """用户要求无视点差成本，那就不能再把"点差太高别做"喂回去。"""
        self._add("点差240点吃掉利润，下次别开仓", NOW)
        self._add("逆势接刀不要做", NOW + 1)
        s = autotrade.recent_lessons(ignore_cost=True)
        self.assertNotIn("点差", s)
        self.assertIn("逆势接刀", s)

    def test_cost_lessons_kept_when_flag_off(self):
        self._add("点差240点吃掉利润，下次别开仓", NOW)
        s = autotrade.recent_lessons(ignore_cost=False)
        self.assertIn("点差", s)

    def test_entry_prompt_carries_lessons(self):
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24,
                                         lessons="- 别再追第一根大阳线")
        self.assertIn("别再追第一根大阳线", p)

    def test_exit_prompt_carries_lessons(self):
        """出场判断以前完全看不到经验，"止跌没延续就走"这类教训用不上。"""
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "opened_at": NOW - 60}
        p = autotrade.build_exit_prompt(bars_down(20), pos, 4040.0, 4040.24,
                                        lessons="- 止跌没延续就立刻走")
        self.assertIn("止跌没延续就立刻走", p)

    def test_exit_uses_recent_lessons(self):
        self._add("止跌没延续就立刻走", NOW)
        seen = {}

        def fake(prompt, system=None, json_mode=False):
            seen["prompt"] = prompt
            return '{"close": false, "reason": "持有"}', {}

        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "opened_at": NOW - 60}
        with mock.patch("agent.llm.chat_ex", fake):
            autotrade.ask_exit(bars_up(20), pos, 4040.0, 4040.24, autotrade.Cfg())
        self.assertIn("止跌没延续就立刻走", seen["prompt"])

    def test_do_review_respects_flag(self):
        pos = {"ticket": 1, "direction": 1, "lots": 0.01,
               "price_open": 4000.0, "opened_at": NOW - 120}
        self.assertIsNone(autotrade._do_review(
            autotrade.Cfg(review=False), pos, 4005.0, 5.0, NOW))
        self.assertEqual(
            [r for r in autotrade.read_log() if r.get("kind") == "review"], [])

    def test_do_review_logs_row_even_on_failure(self):
        pos = {"ticket": 1, "direction": 1, "lots": 0.01,
               "price_open": 4000.0, "opened_at": NOW - 120}
        autotrade._do_review(autotrade.Cfg(review=True), pos, 4005.0, 5.0, NOW)
        rows = [r for r in autotrade.read_log() if r.get("kind") == "review"]
        self.assertEqual(len(rows), 1)
        # 没有 K 线时失败也要留下痕迹，便于判断"为什么不复盘"
        self.assertIn("error", rows[0])

    # ---- 行情状态标签与匹配、铁律升级（P3） ----

    def _add_regime(self, lesson, ts, regime):
        autotrade.append_lesson({"ts": ts, "lesson": lesson, "regime": regime})

    def test_regime_of_buckets(self):
        """regime_of 必须和结构行的「行情判定」用同一套 ER 阈值。"""
        self.assertEqual(autotrade.regime_of(bars_up(20)), "trend")
        self.assertEqual(autotrade.regime_of(bars_down(20)), "trend")
        flat = [bar(4000, 4001, 3999, 4000 + (0.5 if i % 2 else -0.5),
                    NOW + i * 60) for i in range(20)]
        self.assertEqual(autotrade.regime_of(flat), "range")
        # 先单边爬 7 步、再小幅来回磨：ER=7/19≈0.37，落在中间档
        closes = [4000 + i for i in range(8)] + [4005, 4007, 4005, 4007, 4005, 4007]
        mid = [bar(c, c + 1, c - 1, c, NOW + i * 60) for i, c in enumerate(closes)]
        self.assertEqual(autotrade.regime_of(mid), "mid")
        self.assertEqual(autotrade.regime_of([]), "")

    def test_review_row_carries_regime(self):
        """复盘落盘必须带 regime 标签，否则状态匹配永远只能用不到旧数据。"""
        pos = {"ticket": 1, "direction": 1, "lots": 0.01,
               "price_open": 4000.0, "opened_at": NOW - 120}

        def fake_review(*a, **k):
            return {"ok": True, "lesson": "追破位被扫", "bars": 20,
                    "regime": "trend"}

        with mock.patch.object(autotrade, "ask_review", fake_review):
            autotrade._do_review(autotrade.Cfg(review=True), pos, 4005.0, 5.0, NOW)
        rows = [r for r in autotrade.read_log() if r.get("kind") == "review"]
        self.assertEqual(rows[0]["regime"], "trend")

    def test_same_regime_ranks_first(self):
        """总次数更少但当前状态犯过的教训，要排在总次数更多的其它状态教训前面。"""
        for i in range(3):
            self._add_regime(f"在{4030 + i}追破位被假突破打掉，下次等确认再进",
                             NOW + i, "trend")
        self._add_regime("区间中间位置不上不下别动手", NOW + 10, "range")
        s = autotrade.recent_lessons(regime="range")
        first = [ln for ln in s.splitlines() if ln.startswith("- ")][0]
        self.assertIn("区间中间", first)

    def test_other_regime_still_fills_slots(self):
        """软匹配：样本少时其它状态的教训必须补位，硬排除会经常没东西可注。"""
        self._add_regime("追破位被扫，等确认再进", NOW, "trend")
        s = autotrade.recent_lessons(regime="range")
        self.assertIn("追破位", s)

    def test_untagged_legacy_lessons_participate(self):
        """历史数据没有 regime 字段，任何状态下都要能用。"""
        self._add("逆势接刀不要做", NOW)      # 旧格式，无 regime
        s = autotrade.recent_lessons(regime="trend")
        self.assertIn("逆势接刀", s)

    def test_hard_rule_promoted(self):
        """同一主题犯满 5 次升级为铁律：单独成块放最前，不占普通名额。"""
        for i in range(5):
            self._add(f"在{4030 + i}追破位被假突破打掉，等确认再进", NOW + i)
        for i, w in enumerate(("逆势接刀不要做", "区间中间别动手", "等止跌再进别抢")):
            self._add(w, NOW + 10 + i)
        s = autotrade.recent_lessons()
        self.assertIn("铁律", s)
        self.assertIn("已犯 5 次", s)
        # 铁律块必须排在普通教训块前面
        self.assertLess(s.index("铁律"), s.index("按重复次数排序"))
        # 教训文本是模型自己写的、天然偏空，渲染层必须消歧一次
        self.assertIn("同等生效", s)
        # 铁律不占 3 个普通名额：三条普通教训都要还在
        self.assertIn("逆势接刀", s)
        self.assertIn("区间中间", s)
        self.assertIn("等止跌", s)

    def test_hard_rule_respects_ignore_cost(self):
        """铁律也不能谈成本：用户定了无视点差，禁令里一样不许出现。"""
        for i in range(6):
            self._add(f"点差{240 + i}点吃掉利润，下次别开仓", NOW + i)
        s = autotrade.recent_lessons(ignore_cost=True)
        self.assertNotIn("铁律", s)
        self.assertIn("暂无", s)

    def test_entry_prompt_prefers_current_regime(self):
        """端到端：趋势行情里，趋势状态下攒的教训要排在前面。"""
        self._add_regime("区间中间别动手", NOW, "range")
        for i in range(2):
            self._add_regime(f"追破位被扫第{i}次，等确认再进", NOW + 1 + i, "trend")
        seen = {}

        def fake(prompt, system=None, json_mode=False):
            seen["prompt"] = prompt
            return '{"act": false, "p_up": 0.5, "reason": "观望"}', {}

        with mock.patch("agent.llm.chat_ex", fake):
            autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())
        p = seen["prompt"]
        # bars_up 是趋势行情：趋势里犯的「追破位」要排在震荡教训前面
        self.assertLess(p.index("追破位"), p.index("区间中间"))


# ---------------------------------------------------------------- 复盘结构化输出


class TestParseReview(unittest.TestCase):
    """复盘输出是 {scope, theme, lesson}。宽进：JSON / 纯文本都要能吃。"""

    def test_clean_json(self):
        got = autotrade.parse_review(
            '{"scope": "exit", "theme": "止盈太早", "lesson": "平早了，该等结构走完"}')
        self.assertEqual(got["scope"], "exit")
        self.assertEqual(got["theme"], "止盈太早")
        self.assertEqual(got["lesson"], "平早了，该等结构走完")

    def test_plain_prose_still_usable(self):
        """json_mode 会被一次 429 永久关掉，现场基本是纯文本返回——不能不认。"""
        got = autotrade.parse_review("主要因为追破位被假突破打掉，下次等真跌破再进。")
        self.assertEqual(got["theme"], "追破位/假突破")
        self.assertEqual(got["scope"], "entry")
        self.assertIn("追破位", got["lesson"])

    def test_other_theme_falls_back_to_keywords(self):
        """模型偷懒写"其他"时，关键词还能救一次。"""
        got = autotrade.parse_review(
            '{"scope": "exit", "theme": "其他", "lesson": "止盈太早，该多拿一会儿"}')
        self.assertEqual(got["theme"], "止盈太早")

    def test_illegal_scope_is_inferred(self):
        got = autotrade.parse_review(
            '{"scope": "???", "theme": "追破位/假突破", "lesson": "追破位被扫"}')
        self.assertEqual(got["scope"], "entry")

    def test_empty_returns_none(self):
        self.assertIsNone(autotrade.parse_review("   "))

    def test_theme_enum_is_closed(self):
        """枚举之外的主题不许自由生长，否则聚类的老毛病（一桶一条）会回来。"""
        got = autotrade.parse_review(
            '{"scope": "entry", "theme": "手痒了", "lesson": "手痒开了一笔"}')
        self.assertIn(got["theme"], autotrade.LESSON_THEME_ENUM)


class TestAskReview(unittest.TestCase):
    """ask_review 端到端：模型回 JSON 时，theme/scope 要一起带出来。"""

    def _rows(self):
        return [bar(4000 + i, 4001 + i, 3999 + i, 4000.0 + i, NOW + i * 60)
                for i in range(60)]

    def _pos(self):
        return {"direction": 1, "lots": 0.01, "price_open": 4000.0,
                "entry_reason": "突破", "exit_reason": "动能衰竭",
                "opened_at": NOW}

    def _run(self, reply):
        with mock.patch.object(autotrade, "_bars", return_value=self._rows()), \
             mock.patch("agent.llm.chat_ex", return_value=(reply, {})):
            return autotrade.ask_review(autotrade.Cfg(review=True), self._pos(),
                                        4005.0, 5.0, NOW, NOW + 20 * 60)

    def test_json_reply_parsed(self):
        res = self._run('{"scope": "exit", "theme": "止盈太早", "lesson": "平早了，结构没走完"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["theme"], "止盈太早")
        self.assertEqual(res["scope"], "exit")
        self.assertEqual(res["lesson"], "平早了，结构没走完")
        # regime 由程序按平仓前那一段算，不许为空壳（bars 够时必须打上）
        self.assertIn(res["regime"], ("range", "trend", "mid"))

    def test_prose_reply_still_works(self):
        """json_mode 掉了也照样能用——现场多数时候是纯文本。"""
        res = self._run("主要因为追破位被假突破打掉，下次等真跌破再进。")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["theme"], "追破位/假突破")
        self.assertEqual(res["scope"], "entry")


class TestAskReviewCounterfact(TempLedger):
    """模型说"平太早"，但平后行情先回到止损线 → 改判「离场正确」。

    这是整条链最要紧的一环：不跑这个对照，复盘只看"平后涨没涨"，
    「止盈太早」永远成立（实测攒到 67 条），而 227 笔反事实回放
    早证明放松出场全部更差。
    """

    def _rows(self, post_low):
        """平仓前一路涨到 4005；平仓后要么横盘（post_low 为 None），要么跌到 post_low。"""
        out = []
        for i in range(30):                      # 平仓前：稳步上行
            p = 4000 + i * 0.2
            out.append(bar(p, p + 0.5, p - 0.3, p, NOW + i * 60))
        close_bar = NOW + 30 * 60
        for i in range(8):                       # 平仓后
            if post_low is None:                 # 横盘微涨：回撤仅 0.6，不该否决
                p = 4005.0 + i * 0.1
                lo = 4004.4
            else:                                # 缓慢回落：回撤够大但没破止损线
                p = 4005.0 - i * 0.4
                lo = post_low if i == 5 else p - 0.3
            out.append(bar(p, p + 0.3, lo, p, close_bar + i * 60))
        return out

    def _run(self, post_low, reply):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0,
               "entry_reason": "上沿突破", "exit_reason": "动能衰竭",
               "opened_at": NOW}
        with mock.patch.object(autotrade, "_bars", return_value=self._rows(post_low)), \
             mock.patch("agent.llm.chat_ex", return_value=(reply, {})):
            return autotrade.ask_review(autotrade.Cfg(review=True), pos,
                                        4005.0, 3.3, NOW, NOW + 30 * 60)

    def test_early_claim_overturned_when_price_falls_back_to_sl(self):
        """开仓 4000、止损 3.0 → 线在 3997；平后跌到 3996.5，继续持有会更差。"""
        res = self._run(3996.5, '{"scope":"exit","theme":"止盈太早",'
                                '"lesson":"平早了，平后还在涨，该多拿一根"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["counterfact"], "sl_hit")
        self.assertEqual(res["verdict"], autotrade.VERDICT_OK)
        self.assertEqual(res["theme"], "离场正确")
        self.assertIn("反事实校正", res["lesson_extra"])

    def test_overturned_lesson_is_marked_on_disk(self):
        """被推翻的那条落盘时要带校正说明，免得以后翻文本又拿它当"该多拿"的证据。"""
        pos = {"ticket": 1, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "entry_reason": "上沿突破", "exit_reason": "动能衰竭",
               "opened_at": NOW}
        fake = {"ok": True, "lesson": "平早了，该多拿", "theme": "离场正确",
                "scope": "exit", "bars": 15, "regime": "range",
                "verdict": autotrade.VERDICT_OK, "counterfact": "sl_hit",
                "lesson_extra": "（反事实校正：平仓后行情先回撤到止损线）"}
        with mock.patch.object(autotrade, "ask_review", return_value=fake):
            autotrade._do_review(autotrade.Cfg(review=True), pos, 4005.0, 3.3,
                                 NOW + 20 * 60)
        stored = [r for r in autotrade.lesson_rows() if r.get("ticket") == 1]
        self.assertEqual(len(stored), 1)
        self.assertIn("反事实校正", stored[0]["lesson"])
        self.assertEqual(stored[0]["verdict"], autotrade.VERDICT_OK)
        self.assertEqual(stored[0]["counterfact"], "sl_hit")

    def test_early_claim_stands_when_price_never_falls_back(self):
        """平后横盘、回撤只有 0.6 —— 这才是真的平早了，不许否决。"""
        res = self._run(None, '{"scope":"exit","theme":"止盈太早",'
                              '"lesson":"平早了，平后还在涨，该多拿一根"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["counterfact"], "")
        self.assertEqual(res["verdict"], autotrade.VERDICT_EARLY)
        self.assertEqual(res["theme"], "止盈太早")

    def test_drawdown_vetoes_even_without_sl_hit(self):
        """回撤过半个止盈门槛但没到止损线 —— 也要否决。

        实测今天 #2745576501：平后只多涨 0.35，先回撤 2.31。
        """
        res = self._run(4002.0, '{"scope":"exit","theme":"止盈太早",'
                                '"lesson":"平早了，该多拿一根"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["counterfact"], "dd_risk")
        self.assertEqual(res["theme"], "离场正确")

    def test_entry_lesson_is_not_counterfact_checked(self):
        """进场教训跟"平仓后走势"无关，不该被这套逻辑碰。"""
        res = self._run(3996.5, '{"scope":"entry","theme":"逆势抢反弹/接刀",'
                                '"lesson":"下跌途中接刀，等真阳确认再进"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["counterfact"], "")
        self.assertEqual(res["theme"], "逆势抢反弹/接刀")


class TestLessonScope(TempLedger):
    """教训分域：写偏一端，整端都别用。

    实测 188 条老教训里 143 条（76%）讲的是平仓时机，原来它们被同时注入
    进场提示词——进场决策读到"平仓别太早"，等于每轮都喂一遍无关信息。
    """

    def _add(self, lesson, ts, scope=None):
        row = {"ts": ts, "lesson": lesson}
        if scope:
            row["scope"] = scope
        autotrade.append_lesson(row)

    def test_exit_lesson_not_injected_into_entry(self):
        self._add("止盈太早，结构没走完就平了", NOW, scope="exit")
        self.assertIn("暂无", autotrade.recent_lessons(scope="entry"))
        self.assertIn("止盈太早", autotrade.recent_lessons(scope="exit"))

    def test_entry_lesson_not_injected_into_exit(self):
        self._add("逆势接刀，等止跌阳线再进", NOW, scope="entry")
        self.assertIn("逆势接刀", autotrade.recent_lessons(scope="entry"))
        self.assertIn("暂无", autotrade.recent_lessons(scope="exit"))

    def test_both_reaches_both_ends(self):
        self._add("方向与依据矛盾，两端都没想清楚", NOW, scope="both")
        self.assertIn("方向与依据", autotrade.recent_lessons(scope="entry"))
        self.assertIn("方向与依据", autotrade.recent_lessons(scope="exit"))

    def test_legacy_rows_get_inferred_scope(self):
        """老数据没有 scope 字段：靠关键词推断，不许直接丢掉。"""
        self._add("平仓太早，该等结构走完", NOW)
        self.assertIn("平仓太早", autotrade.recent_lessons(scope="exit"))
        self.assertIn("暂无", autotrade.recent_lessons(scope="entry"))

    def test_no_scope_means_no_filter(self):
        self._add("平仓太早，该等结构走完", NOW, scope="exit")
        self._add("逆势接刀，等止跌再进", NOW + 1, scope="entry")
        s = autotrade.recent_lessons()
        self.assertIn("平仓太早", s)
        self.assertIn("逆势接刀", s)

    def test_theme_domain_beats_scope_label(self):
        """主题本身就是某一端的错，标签写"两端"也不许跨端注入。

        实测：老数据被标成 scope=both 的「出场时点不对」共 35 条，
        分域后仍然整块进了进场提示词——等于白分。
        """
        self._add("平仓时机过早，应等待突破后确认再平仓", NOW, scope="both")
        self.assertIn("暂无", autotrade.recent_lessons(scope="entry"))
        self.assertIn("平仓时机", autotrade.recent_lessons(scope="exit"))

    def test_entry_and_exit_use_their_own_pool(self):
        """端到端：进场问模型时提示词里不许出现出场教训，反之亦然。"""
        self._add("止盈太早，结构没走完就平了", NOW, scope="exit")
        seen = {}

        def fake(prompt, system=None, json_mode=False):
            seen["entry"] = prompt
            return '{"act": false, "p_up": 0.5, "reason": "观望"}', {}

        with mock.patch("agent.llm.chat_ex", fake):
            autotrade.ask_entry(bars_up(20), 4040.0, 4040.24, autotrade.Cfg())
        self.assertNotIn("止盈太早", seen["entry"])


class TestLessonStore(TempLedger):
    """教训库独立于流水：重置战绩不清经验，清经验不动战绩。"""

    def _review(self, lesson, ts, ticket=1, theme="", scope=""):
        with mock.patch.object(autotrade, "ask_review",
                               return_value={"ok": True, "lesson": lesson, "bars": 15,
                                             "regime": "range", "theme": theme,
                                             "scope": scope}):
            pos = {"ticket": ticket, "direction": 1, "lots": 0.01,
                   "price_open": 4000.0, "opened_at": ts - 120}
            return autotrade._do_review(autotrade.Cfg(review=True), pos, 4005.0, 5.0, ts)

    def test_review_lands_in_own_file(self):
        row = self._review("追破位被扫", NOW, theme="追破位/假突破", scope="entry")
        self.assertEqual(row["theme"], "追破位/假突破")
        self.assertEqual(row["scope"], "entry")
        p = autotrade.lessons_path()
        self.assertTrue(p.exists())
        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["theme"], "追破位/假突破")
        self.assertEqual(lines[0]["scope"], "entry")
        self.assertEqual(lines[0]["source"], "live")

    def test_missing_theme_is_filled_by_keywords(self):
        """测试桩/老调用只给 lesson 时，落盘也必须带 theme+scope。"""
        row = self._review("逆势接刀，等止跌再进", NOW)
        self.assertEqual(row["theme"], "逆势抢反弹/接刀")
        self.assertEqual(row["scope"], "entry")

    def test_reset_keeps_lessons(self):
        """这是本次改动的核心：重置战绩不许把经验一起清掉。"""
        self._review("追破位被扫，等确认再进", NOW, theme="追破位/假突破", scope="entry")
        autotrade.append_log({"ts": NOW + 60, "kind": "open", "direction": 1, "p_up": 0.7})
        res = autotrade.reset_history()
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["lessons_kept"])
        self.assertEqual(len(autotrade.lesson_rows()), 1)
        self.assertIn("追破位", autotrade.recent_lessons(scope="entry"))
        # 战绩确实清了
        self.assertEqual([r.get("kind") for r in autotrade.read_log()], ["reset"])

    def test_reset_lessons_clears_only_lessons(self):
        self._review("追破位被扫", NOW, theme="追破位/假突破", scope="entry")
        autotrade.append_log({"ts": NOW + 60, "kind": "open", "direction": 1, "p_up": 0.7})
        res = autotrade.reset_lessons()
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["cleared"], 1)
        self.assertEqual(autotrade.lesson_rows(), [])
        # 清完不许回退去读归档流水，否则"清空"立刻又长回来
        self.assertEqual(len(autotrade.read_log()), 2)
        self.assertIn("暂无", autotrade.recent_lessons())
        # 归档文件确实留下了（改名不删除——教训是唯一副本）
        self.assertTrue(res.get("archived"))

    def test_no_400_row_window(self):
        """教训库必须全量：以前 read_log(limit=400) 让 188 条复盘只剩 86 条进库。"""
        for i in range(420):
            autotrade.append_log({"ts": NOW + i, "kind": "skip", "reason": "冷却中"})
        for i in range(3):
            autotrade.append_lesson({"ts": NOW + 500 + i,
                                     "lesson": f"在{4030 + i}追破位被假突破打掉，等确认再进"})
        self.assertIn("已出现 3 次", autotrade.recent_lessons())

    def test_lesson_file_makes_log_fallback_irrelevant(self):
        """有教训库之后，即使流水被清空，经验也还在。"""
        self._review("逆势接刀，等止跌再进", NOW, theme="逆势抢反弹/接刀", scope="entry")
        autotrade.reset_history()                 # 流水归档
        autotrade.append_log({"ts": NOW + 90, "kind": "reset"})
        self.assertIn("逆势接刀", autotrade.recent_lessons(scope="entry"))

    def test_migrate_pulls_archived_lessons_back(self):
        """升级时把历史流水里的复盘搬进教训库：含归档文件，重复跑不翻倍。"""
        autotrade.append_log({"ts": NOW, "kind": "review", "ticket": 1,
                              "lesson": "追破位被扫，等确认再进", "regime": "trend"})
        autotrade.append_log({"ts": NOW + 60, "kind": "review", "ticket": 2,
                              "lesson": "止盈太早，该多拿一会儿", "regime": "range"})
        autotrade.reset_history()                 # 流水变成归档文件
        res = autotrade.migrate_lessons()
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["migrated"], 2)
        rows = autotrade.lesson_rows()
        self.assertEqual(len(rows), 2)
        got = {r["lesson"][:4]: r for r in rows}
        self.assertEqual(got["追破位被"]["theme"], "追破位/假突破")
        self.assertEqual(got["止盈太早"]["scope"], "exit")
        self.assertEqual(got["追破位被"]["source"], "migrated")
        # 重复跑不覆盖
        self.assertFalse(autotrade.migrate_lessons()["ok"])



# ------------------------------------------------------------------ 汇总


class TestSummary(TempLedger):
    def test_reports_direction_skew(self):
        """只做多这种偏置，看盈亏是发现不了的，必须单独统计多空。"""
        for i in range(9):
            autotrade.append_log({"ts": NOW + i, "kind": "close",
                                  "direction": 1, "pnl": -0.2})
        autotrade.append_log({"ts": NOW + 20, "kind": "close",
                              "direction": -1, "pnl": 0.3})
        d = autotrade.summary()["dir_recent"]
        self.assertEqual((d["n"], d["long"], d["short"]), (10, 9, 1))
        self.assertTrue(d["skew"])

    def test_hit_rate_separates_llm_from_fallback(self):
        rows = [
            {"ts": NOW, "kind": "open", "direction": 1, "p_up": 0.7, "source": "llm"},
            {"ts": NOW + 60, "kind": "close", "pnl": 1.0, "source": "llm"},
            {"ts": NOW + 120, "kind": "open", "direction": -1, "p_up": 0.3, "source": "llm"},
            {"ts": NOW + 180, "kind": "close", "pnl": -0.5, "source": "llm"},
            {"ts": NOW + 240, "kind": "open", "direction": 1, "p_up": 0.6, "source": "fallback"},
            {"ts": NOW + 300, "kind": "close", "pnl": -1.0, "source": "fallback"},
        ]
        for r in rows:
            autotrade.append_log(r)

        s = autotrade.summary()
        self.assertEqual(s["closed"], 3)
        self.assertEqual(s["wins"], 1)
        self.assertAlmostEqual(s["hit_rate"], 1 / 3, places=4)
        # 只算模型自己决定的成交：1 胜 1 负 = 50%
        self.assertEqual(s["llm_closes"], 2)
        self.assertAlmostEqual(s["llm_hit_rate"], 0.5, places=4)

    def test_empty_log_has_no_hit_rate(self):
        s = autotrade.summary()
        self.assertEqual(s["closed"], 0)
        self.assertIsNone(s["hit_rate"])

    def test_log_survives_corrupt_line(self):
        autotrade.append_log({"ts": NOW, "kind": "skip", "reason": "x"})
        p = autotrade.log_path()
        with p.open("a", encoding="utf-8") as fh:
            fh.write("{坏行\n")
        self.assertEqual(len(autotrade.read_log()), 1)


class TestSymbolSupport(unittest.TestCase):
    """多品种：BTCUSDm 上线，价格精度必须跟着品种走。

    背景：以前黄金写死 3 位小数、启动请求里写死 symbol='XAUUSDm'。
    加第二个品种时这两处都会静默失效——BTC 会显示 80185.810，
    前端选了下拉框也永远只发黄金。
    """

    def test_btc_spec_is_measured(self):
        """规格必须来自实测，不能拍：point/contract 读 symbol_info，
        spread 是 tick 中位数，atr 是 H4 ATR(14) 中位数。"""
        from backtest.symbols import SYMBOLS

        s = SYMBOLS["BTCUSDm"]
        self.assertEqual(s["point"], 0.01)
        self.assertEqual(s["contract"], 1.0)
        self.assertEqual(s["digits"], 2)
        self.assertEqual(s["spread"], 640)
        self.assertEqual(s["atr_pts"], 82706)
        # 成本占 ATR 的比例必须远低于 15% 的排除线，否则不该上实盘
        self.assertLess(s["spread"] / s["atr_pts"], 0.05)

    def test_btc_in_whitelist_and_unknown_rejected(self):
        self.assertIn("BTCUSDm", autotrade.TRADABLE_SYMBOLS)
        self.assertIn("XAUUSDm", autotrade.TRADABLE_SYMBOLS)
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(symbol="BTCUSDm")), [])
        # 没实测过的品种必须被挡：点差成本会退回 0，把"没测过"显示成"零成本"
        issues = autotrade.validate_cfg(autotrade.Cfg(symbol="EURUSDm"))
        self.assertTrue(any("白名单" in i for i in issues), issues)

    def test_symbol_options_only_whitelisted(self):
        """面板下拉框的数据源必须等于后端白名单，前端不许另存一份。"""
        opts = autotrade.symbol_options()
        self.assertEqual([o["symbol"] for o in opts],
                         list(autotrade.TRADABLE_SYMBOLS))
        for o in opts:
            self.assertTrue(o["label"])
            self.assertTrue(o["digits"])
            self.assertIsNotNone(o["point"])

    def test_price_precision_follows_symbol(self):
        self.assertEqual(autotrade._px("BTCUSDm", 80185.812), "80185.81")
        self.assertEqual(autotrade._px("BTCUSDm", 80185.0), "80185.00")
        self.assertEqual(autotrade._px("XAUUSDm", 4024.1394), "4024.139")
        self.assertEqual(autotrade._digits("BTCUSDm"), 2)
        self.assertEqual(autotrade._digits("XAUUSDm"), 3)
        # 未知品种退回 3 位，不抛异常（不能让一个品种名把 prompt 构建搞崩）
        self.assertEqual(autotrade._digits("WHATEVER"), 3)

    def test_entry_prompt_btc_two_decimals(self):
        rows = [{"time": NOW + i * 60, "open": 80000.0 + i * 10,
                 "high": 80050.0 + i * 10, "low": 79950.0 + i * 10,
                 "close": 80000.0 + i * 10} for i in range(14)]
        p = autotrade.build_entry_prompt(rows, 80185.81, 80192.21,
                                        symbol="BTCUSDm", point=0.01)
        self.assertIn("80185.81", p)          # 2 位，不是 80185.810
        self.assertIn("点差=640 点", p)        # (80192.21-80185.81)/0.01
        self.assertNotIn("80185.810", p)
        self.assertIn("品种 BTCUSDm", p)

    def test_btc_spread_cost_scales_with_contract(self):
        """0.01 手 BTC 的点差成本 = 640 × 0.01 × 0.01 × 1 = 0.064 USD，
        黄金 = 240 × 0.001 × 0.01 × 100 = 0.24 USD。公式必须同一个。"""
        c_btc = autotrade._spread_cost("BTCUSDm", 0.01)
        c_xau = autotrade._spread_cost("XAUUSDm", 0.01)
        self.assertAlmostEqual(c_btc, 0.064, places=6)
        self.assertAlmostEqual(c_xau, 0.24, places=6)


# ------------------------------------------------------------------ 特征工程
#
# 2026-10-26 加的一批：ATR 归一化、特征快照落盘、多周期背景、system 参数化。
# 共同点是「算出来的东西不能只活在内存里」——每条断言都钉一个事后要用的具体数字。


class TestAtr(unittest.TestCase):
    def test_gap_counts_as_true_range(self):
        """跳空必须计入 TR。只算 high-low 会严重低估波动。

        构造：第 1 根收 100，第 2 根直接在 110 开盘、高低只差 1。
        看高低差会得到 1；真实 TR 应该是 |111-100| = 11。
        """
        rows = [
            {"time": NOW, "open": 100, "high": 101, "low": 99, "close": 100},
            {"time": NOW + 60, "open": 110, "high": 111, "low": 110, "close": 110},
        ]
        a = autotrade._atr(rows)
        self.assertTrue(a["ok"])
        self.assertEqual(a["n_tr"], 1)          # 2 根只能出 1 个 TR
        self.assertAlmostEqual(a["atr"], 11.0, places=6)

    def test_known_series(self):
        """bars_up 每根 high-low 恒为 2、前收差 1.5，所以 TR 恒 2、ATR=2。

        14 根极差 = (4001+13) - 3999 = 15，比值 7.5。
        这个数一旦变了，说明 TR 公式或窗口被动过。
        """
        a = autotrade._atr(bars_up(14))
        self.assertAlmostEqual(a["atr"], 2.0, places=6)
        self.assertAlmostEqual(a["span"], 15.0, places=6)
        self.assertAlmostEqual(a["span_atr"], 7.5, places=6)

    def test_ratio_is_dimensionless(self):
        """比值必须无量纲：整体价格乘 10，比值一个字节都不该变。

        这是它能跨品种（黄金 vs BTC）、跨时段（亚洲盘 vs 纽约盘）比较的前提。
        """
        base = bars_up(14)
        scaled = [{**r, "open": r["open"] * 10, "high": r["high"] * 10,
                   "low": r["low"] * 10, "close": r["close"] * 10} for r in base]
        self.assertAlmostEqual(autotrade._atr(base)["span_atr"],
                               autotrade._atr(scaled)["span_atr"], places=6)

    def test_degenerate_inputs(self):
        self.assertFalse(autotrade._atr([])["ok"])
        self.assertFalse(autotrade._atr(bars_up(1))["ok"])
        self.assertEqual(autotrade._atr(bars_up(1))["atr"], 0.0)

    def test_structure_line_exposes_the_ratio(self):
        text = autotrade._structure_text(bars_up(14))
        self.assertIn("倍单根均幅", text)
        self.assertIn("7.5 倍", text)


class TestFeatureSnapshot(unittest.TestCase):
    def test_all_fields_for_offline_analysis(self):
        """快照必须自带分析所需的全部维度，否则落盘等于白落。"""
        snap = autotrade.feature_snapshot(bars_up(20), autotrade.Cfg())
        for key in ("ok", "symbol", "tf", "n", "win", "er", "drift", "pos",
                    "span", "streak", "path", "r1", "r5", "r13",
                    "atr", "span_atr", "regime", "prog_dir"):
            self.assertIn(key, snap)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["n"], 14)
        self.assertEqual(snap["symbol"], "XAUUSDm")
        self.assertEqual(snap["regime"], "trend")     # 一路上涨

    def test_window_bounds_are_the_real_14(self):
        """win 记的是实际用到的窗口，事后核对「这笔决策看的哪 14 分钟」靠它。"""
        rows = bars_up(40)
        snap = autotrade.feature_snapshot(rows, autotrade.Cfg())
        self.assertEqual(snap["win"], [rows[-14]["time"], rows[-1]["time"]])

    def test_values_are_rounded(self):
        """日志是给机器算也是给人看的，长尾小数只会把文件撑大。"""
        snap = autotrade.feature_snapshot(bars_up(14), autotrade.Cfg())
        for key in ("er", "pos", "span_atr"):
            self.assertLessEqual(len(str(snap[key])), 8, key)

    def test_degenerate_rows(self):
        self.assertFalse(autotrade.feature_snapshot([])["ok"])
        self.assertFalse(autotrade.feature_snapshot(bars_up(1))["ok"])

    def test_mtf_info_is_embedded(self):
        snap = autotrade.feature_snapshot(
            bars_up(14), autotrade.Cfg(),
            mtf={"tf": "M5", "er": 0.6, "pos": 0.8, "regime": "单边上行"})
        self.assertEqual(snap["mtf"]["regime"], "单边上行")


class TestFeatureSnapshotLands(TempLedger):
    """快照必须真的写进日志——算出来不落盘，等于没算。"""

    def test_open_row_carries_snapshot(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "act": True,
                                             "agree": True, "reason": "结构支持",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "order": 7, "price": 4040.0}), \
             mock.patch.object(autotrade, "_mtf_bundle",
                               return_value={"text": "", "info": None}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4040.0, 4040.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "open")
        feat = row["feat"]
        self.assertTrue(feat["ok"])
        self.assertEqual(feat["n"], 14)
        self.assertIn("er", feat)
        self.assertIn("span_atr", feat)

    def test_skip_row_carries_snapshot(self):
        """被闸门拦掉的单也要留特征——否则没法回答「min_edge 是不是误杀」。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.52, "act": True,
                                             "agree": True, "reason": "轻微看多",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_mtf_bundle",
                               return_value={"text": "", "info": None}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4040.0, 4040.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("信号不够强", row["reason"])
        self.assertTrue(row["feat"]["ok"])


class TestMtfBackground(unittest.TestCase):
    def test_degrades_silently_when_terminal_down(self):
        """终端没连时不给背景，也不写"不可用"——那会让模型去想缺了什么。"""
        with mock.patch.object(autotrade, "_bars", side_effect=RuntimeError("no mt5")):
            b = autotrade._mtf_bundle("XAUUSDm")
        self.assertEqual(b["text"], "")
        self.assertIsNone(b["info"])

    def test_too_few_bars_gives_nothing(self):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(3)):
            self.assertEqual(autotrade._mtf_bundle("XAUUSDm")["text"], "")

    def test_one_line_with_er_no_raw_bars(self):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(20)):
            b = autotrade._mtf_bundle("XAUUSDm", 0.35, 3)
        self.assertTrue(b["text"].startswith("【背景】M5 14 根"))
        self.assertIn("ER=", b["text"])
        self.assertEqual(b["text"].count("\n"), 0)      # 只有一行，没有 K 线表
        self.assertNotIn("T-13", b["text"])
        self.assertEqual(b["info"]["tf"], "M5")
        self.assertEqual(b["info"]["n"], 14)

    def test_prompt_bar_count_unchanged(self):
        """加了 M5 背景后，M1 的 K 线行数必须还是 14——边界没有松动。"""
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(20)):
            mtf = autotrade._mtf_text("XAUUSDm")
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24,
                                         mtf_text=mtf)
        lines = re.findall(r"^(?:T-\d+|当前)\s+\d{2}-\d{2}", p, re.M)
        self.assertEqual(len(lines), 14)
        self.assertIn("【背景】M5", p)
        self.assertNotIn("T-14", p)


class TestSystemFollowsCfg(unittest.TestCase):
    def test_no_hardcoded_symbol_in_templates(self):
        """三个 system 模板里不许再出现具体品种名（加 BTC 时漏改过的 bug）。"""
        for name in ("ENTRY_SYSTEM", "EXIT_SYSTEM", "REVIEW_SYSTEM"):
            tmpl = getattr(autotrade, name)
            self.assertIn("{SYMBOL_LABEL}", tmpl, name)
            self.assertNotIn("XAUUSD", tmpl, name)

    def test_gold_renders_gold(self):
        text = autotrade.system_for(autotrade.ENTRY_SYSTEM, autotrade.Cfg())
        self.assertIn("黄金 XAUUSD", text)
        self.assertNotIn("{SYMBOL_LABEL}", text)

    def test_btc_renders_btc(self):
        """选 BTC 时不能再告诉模型"你是黄金交易员"。

        只检查开头那行自我介绍。正文里"黄金实盘口径"是标注实测来源的，
        留着才对——它提醒模型这个数字是别的品种跑出来的。
        """
        cfg = autotrade.Cfg(symbol="BTCUSDm")
        for name in ("ENTRY_SYSTEM", "EXIT_SYSTEM", "REVIEW_SYSTEM"):
            first = autotrade.system_for(getattr(autotrade, name), cfg).splitlines()[0]
            self.assertIn("BTCUSD", first, name)
            self.assertNotIn("XAUUSD", first, name)
            self.assertNotIn("黄金", first, name)

    def test_exit_thresholds_follow_cfg(self):
        """出场阈值同理：cfg 改了 system 必须跟着改，否则就是在骗模型。"""
        cfg = autotrade.Cfg(sl_usd=5.0, tp_min_usd=4.5, max_hold_sec=1800)
        text = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg)
        self.assertIn("浮亏达到 5 USD", text)
        self.assertIn("浮盈不到 4.5 USD", text)
        self.assertIn("持仓满 30 分钟", text)
        self.assertNotIn("{SL_USD}", text)
        self.assertNotIn("{TP_MIN_USD}", text)
        self.assertNotIn("{MAX_HOLD_MIN}", text)

    def test_unknown_symbol_falls_back_to_code(self):
        text = autotrade.system_for(autotrade.ENTRY_SYSTEM,
                                    autotrade.Cfg(symbol="WHATEVER"))
        self.assertIn("WHATEVER", text)


class TestEntrySystemExample(unittest.TestCase):
    """输出示例里的 p_up 不能是会被模型照抄的占位值。

    实测 193 笔里 89 笔（46.1%）的 p_up 精确等于 0.0，**且这 89 笔全是空单**
    —— 根因是 ENTRY_SYSTEM 的 JSON 示例写着 `"p_up": 0.00`，与正文
    「只在 0.20 ~ 0.80 之间取值」直接冲突。模型按示例填，程序按
    `p_up > 0.5 else -1` 读成「最强看空」，于是开空单。
    clamp_p_up 只是把它压成 0.20（仍是极端看空），日志里再也看不到 0.0，
    等于把 bug 藏起来了 —— 所以这里必须直接断言**示例原文**，不能经 clamp。
    """

    def _example(self):
        m = re.search(r'\{"act".*?\}', autotrade.ENTRY_SYSTEM)
        self.assertIsNotNone(m, "ENTRY_SYSTEM 里找不到 JSON 输出示例")
        return json.loads(m.group(0))

    def test_example_p_up_inside_stated_range(self):
        """示例值必须落在正文声明的区间里（不走 parse_p_up，否则会被 clamp 洗白）。"""
        v = float(self._example()["p_up"])
        self.assertGreaterEqual(v, autotrade.P_UP_FLOOR)
        self.assertLessEqual(v, autotrade.P_UP_CEIL)

    def test_example_p_up_is_not_a_template_value(self):
        """0 / 0.5 / 1 都不该做示例：要么会被照抄，要么示例本身就在示范弃权。"""
        self.assertNotIn(float(self._example()["p_up"]), (0.0, 0.5, 1.0))

    def test_example_tells_model_not_to_copy(self):
        self.assertIn("只是格式示范", autotrade.ENTRY_SYSTEM)

    def test_stated_range_matches_clamp(self):
        """正文写的区间必须和 clamp_p_up 的常量同源，否则又是一处自相矛盾。"""
        self.assertIn(f"{autotrade.P_UP_FLOOR:.2f} ~ {autotrade.P_UP_CEIL:.2f}",
                      autotrade.ENTRY_SYSTEM)


class TestSummaryFloatPnl(TempLedger):
    """面板顶部常驻状态带要显示「这笔单现在赚还是亏」，数据来自 summary。

    为什么必须由后端算：券商的浮动盈亏是整个账户的（含手动单），
    和 AI 这一笔对不上；前端又拿不到合约乘数，自己算必然错。
    """

    def _open(self, price_open=4000.0, direction=1):
        state = autotrade._empty_state()
        state["cfg"] = {"symbol": "XAUUSDm", "dry_run": True}
        state["position"] = {"ticket": -1, "direction": direction, "lots": 0.01,
                             "price_open": price_open, "opened_at": NOW - 30,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        autotrade.save_state(state)

    def test_float_pnl_is_computed_from_live_quote(self):
        self._open(direction=1)
        with mock.patch.object(autotrade, "_quote", return_value=(4001.5, 4001.74)):
            s = autotrade.summary()
        self.assertEqual(s["float_pnl"], 1.5)      # (4001.5-4000)*100*0.01

    def test_short_uses_ask(self):
        """空单平在 ask：拿 bid 算会白送 0.24 的点差利润（-1.26 而不是 -1.50）。"""
        self._open(direction=-1)
        with mock.patch.object(autotrade, "_quote", return_value=(4001.26, 4001.5)):
            s = autotrade.summary()
        self.assertEqual(s["float_pnl"], -1.5)     # -(4001.5-4000)*1.0

    def test_no_quote_means_none_not_zero(self):
        """拿不到报价就给 None。返回 0.0 会被当成「正好保本」，那是假数据。"""
        self._open()
        with mock.patch.object(autotrade, "_quote", return_value=None):
            s = autotrade.summary()
        self.assertIsNone(s["float_pnl"])

    def test_flat_account_has_no_float_pnl(self):
        autotrade.save_state(autotrade._empty_state())
        with mock.patch.object(autotrade, "_quote", return_value=(4000.0, 4000.24)):
            self.assertIsNone(autotrade.summary()["float_pnl"])

    def test_summary_exposes_guard_sec(self):
        """面板靠这个字段决定「止损快查」胶囊显示「每 N 秒」还是「关闭」。"""
        state = autotrade._empty_state()
        state["cfg"] = {"guard_sec": 7.5}
        autotrade.save_state(state)
        self.assertEqual(autotrade.summary()["guard_sec"], 7.5)
        # 旧 state 没有这个字段 → 回退到当前默认值，不能是 None
        state["cfg"] = {}
        autotrade.save_state(state)
        self.assertEqual(autotrade.summary()["guard_sec"], autotrade.Cfg().guard_sec)


class TestSummaryTodayScope(TempLedger):
    """「今日累计」必须真的是今天。

    踩过的坑：面板和幕布原来用 `state["pnl"]` 当"今日"，那是**本进程累计**——
    点一次「重置战绩」就归零，跨午夜也不会归零。标题写"今日"就得按自然日算。
    """

    def _close(self, ts, pnl, source="llm"):
        autotrade.append_log({"ts": ts, "kind": "close", "ticket": 1,
                              "direction": 1, "pnl": pnl, "source": source,
                              "held_sec": 60})

    def test_today_scope_is_local_date_not_process_total(self):
        now = int(autotrade.time.time())
        self._close(now, 3.0)
        self._close(now + 1, -3.42, source="sl")
        self._close(now - 86400, 5.0)          # 昨天那笔不该算进今日

        s = autotrade.summary()
        self.assertEqual(s["closed"], 3)       # 全量仍是 3 笔
        self.assertEqual(s["closed_today"], 2)
        self.assertEqual(s["wins_today"], 1)
        self.assertEqual(s["losses_today"], 1)
        self.assertAlmostEqual(s["pnl_today"], -0.42, places=2)

    def test_wins_plus_losses_equals_closed_today(self):
        now = int(autotrade.time.time())
        for i, pnl in enumerate([1.0, -1.0, 2.0, -3.0]):
            self._close(now + i, pnl)
        s = autotrade.summary()
        self.assertEqual(s["wins_today"] + s["losses_today"], s["closed_today"])
        self.assertEqual(s["wins_today"], 2)

    def test_hit_rate_excludes_stop_losses(self):
        """两个口径并存，含义完全不同，别混用。

        `llm_hit_rate` 的分母只算模型主动平的，硬止损不进——所以它天生接近
        100%，**不能当胜率用**。
        `hit_rate` 的分母是全部平仓单，止损照样算——面板卡片显示的是这个
        （用户 10-26 直接问"硬止损为什么不算"，这就是答案：以前真没算）。
        """
        now = int(autotrade.time.time())
        self._close(now, 3.0)
        self._close(now + 1, -3.42, source="sl")
        s = autotrade.summary()
        self.assertEqual(s["llm_closes"], 1)
        self.assertEqual(s["llm_hit_rate"], 1.0)   # 单看模型平仓是 100%
        self.assertEqual(s["hit_rate"], 0.5)       # 真实胜率 50%，这才是给人看的
        self.assertEqual(s["sl_closes"], 1)


class TestExitVerdict(unittest.TestCase):
    """一条出场教训到底在说"平太早"还是"平太晚"。

    实测最大铁律桶（「出场时点不对」102 条）里同时装着方向相反的教训：
    59 条说平太早、10 条说平太晚、4 条说平得对。同一个「已犯 102 次」
    同时命令模型"别急着平"和"要及时平"，等于什么都没说。
    """

    def test_ok_beats_early(self):
        """「平得对，但下次应该多拿一根」——模型自己都说这笔平得对，归 ok。"""
        self.assertEqual(
            autotrade.exit_verdict("平得对：离场后先破位下探，下次应再等一根"),
            autotrade.VERDICT_OK)

    def test_early(self):
        self.assertEqual(
            autotrade.exit_verdict("多单因恐惧上沿而过早止盈，平后连拉5根到4018.9"),
            autotrade.VERDICT_EARLY)

    def test_late(self):
        self.assertEqual(
            autotrade.exit_verdict("没及时平仓，扛单拖到深亏才砍"),
            autotrade.VERDICT_LATE)

    def test_unclear_stays_empty(self):
        """判不出就别猜：猜错方向等于把"平得对"喂成"平太早"。"""
        self.assertEqual(
            autotrade.exit_verdict("主要因为判断错误导致亏损，下次应更谨慎观察"),
            "")

    def test_committed_verdict_wins_over_text(self):
        """反事实推翻后落盘的 verdict 优先，不再被文本拉回去。"""
        row = {"lesson": "止盈太早，该多拿", "verdict": autotrade.VERDICT_OK}
        self.assertEqual(autotrade.row_verdict(row), autotrade.VERDICT_OK)

    def test_theme_label_drives_verdict_when_text_has_no_keyword(self):
        """模型在主题里说了「止盈太早」，正文没有"太早"也要认。

        实测今天的 4 条里有 2 条正文是"贴上沿就慌着离场""遇ER回落就急平"，
        一个"太早"都没有——只靠关键词会漏掉一半。
        """
        self.assertEqual(
            autotrade.verdict_for("止盈太早", "贴上沿就慌着离场，平后仍在高位磨盘"),
            autotrade.VERDICT_EARLY)
        self.assertEqual(
            autotrade.verdict_for("止损太晚", "拖着没砍，最后深亏出局"),
            autotrade.VERDICT_LATE)

    def test_text_overrides_ambiguous_theme(self):
        """主题是含糊的垃圾桶时，正文说"平得对"就归 ok。"""
        self.assertEqual(
            autotrade.verdict_for("出场时点不对", "平得对，离场后行情继续下探"),
            autotrade.VERDICT_OK)

    def test_ambiguous_theme_without_signal_stays_empty(self):
        self.assertEqual(
            autotrade.verdict_for("出场时点不对", "主要因为判断错误导致亏损"), "")


class TestExitCounterfact(unittest.TestCase):
    """反事实：平仓后行情有没有先回到止损线。

    没有这个对照，复盘只看"平后涨没涨"，"止盈太早"永远成立——
    这正是它攒到 67 条的原因。
    """

    @staticmethod
    def _bars(pairs):
        return [{"low": lo, "high": hi} for lo, hi in pairs]

    def test_long_hit_when_falls_back_through_sl(self):
        bars = self._bars([(3996.5, 4000.2), (3998.0, 4001.0)])
        self.assertEqual(autotrade.exit_counterfact(bars, 1, 3997.0), "sl_hit")

    def test_long_miss_when_price_holds(self):
        bars = self._bars([(4002.0, 4005.0), (4003.0, 4006.0)])
        self.assertEqual(autotrade.exit_counterfact(bars, 1, 3997.0), "")

    def test_short_hit_when_rallies_through_sl(self):
        bars = self._bars([(3999.0, 4003.5), (4000.0, 4002.0)])
        self.assertEqual(autotrade.exit_counterfact(bars, -1, 4003.0), "sl_hit")

    def test_no_sl_line_returns_empty(self):
        """止损关掉时没有参照线，别硬算。"""
        self.assertEqual(
            autotrade.exit_counterfact(self._bars([(3900.0, 4000.0)]), 1, 0.0), "")

    def test_drawdown_risk_vetoes_early_claim(self):
        """平后没破止损线、但先回撤超过半个止盈门槛 → 多拿不划算。

        实测今天 #2745401213：平后只多涨 0.03，却先回撤 2.90。
        这种点记成"止盈太早"是把噪音当规律。
        """
        bars = self._bars([(4005.75, 4008.70)])       # 回撤 2.90
        self.assertEqual(
            autotrade.exit_counterfact(bars, 1, 4002.32, 4008.65, dd_limit=1.625),
            "dd_risk")

    def test_small_drawdown_does_not_veto(self):
        """回撤没到线就别否决：这才是真的平早了。"""
        bars = self._bars([(4011.75, 4016.91)])       # 回撤 0.97，之后又涨 4.19
        self.assertEqual(
            autotrade.exit_counterfact(bars, 1, 4005.54, 4012.72, dd_limit=1.625), "")

    def test_sl_hit_takes_priority_over_drawdown(self):
        bars = self._bars([(3996.0, 4000.0)])
        self.assertEqual(
            autotrade.exit_counterfact(bars, 1, 3997.0, 4005.0, dd_limit=1.625),
            "sl_hit")

    def test_empty_bars_safe(self):
        self.assertEqual(autotrade.exit_counterfact([], 1, 3997.0), "")

    def test_sl_price_converts_usd_by_lots(self):
        """sl_usd 是美元，必须按手数换成价格距离。

        0.01 手黄金 1 美元价格 = 1 USD，止损 3.0 美元 → 价格距离 3.0；
        0.05 手时同样 3.0 美元只对应 0.6 的价格距离。写死 1:1 会差 5 倍。
        """
        cfg = autotrade.Cfg(symbol="XAUUSDm", sl_usd=3.0)
        small = autotrade._sl_price_for(cfg, {"direction": 1, "lots": 0.01,
                                              "price_open": 4000.0})
        big = autotrade._sl_price_for(cfg, {"direction": 1, "lots": 0.05,
                                            "price_open": 4000.0})
        self.assertAlmostEqual(4000.0 - small, 3.0, places=3)
        self.assertAlmostEqual(4000.0 - big, 0.6, places=3)
        short = autotrade._sl_price_for(cfg, {"direction": -1, "lots": 0.01,
                                              "price_open": 4000.0})
        self.assertAlmostEqual(short - 4000.0, 3.0, places=3)


class TestExitThemeSplit(TempLedger):
    """「出场时点不对」这一桶要按方向拆开，不能再是一个垃圾桶。"""

    def _lesson(self, lesson, ts, theme=None, scope="exit", verdict=None):
        row = {"ts": ts, "kind": "review", "lesson": lesson, "scope": scope}
        if theme:
            row["theme"] = theme
        if verdict:
            row["verdict"] = verdict
        autotrade.append_lesson(row)

    def _bucket(self, name, domain=None):
        for c in autotrade.lesson_clusters():
            if c["name"] == name and (domain is None or c["domain"] == domain):
                return c
        return None

    def test_trash_bucket_splits_by_direction(self):
        self._lesson("平得太早，结构没走完", NOW, theme="出场时点不对")
        self._lesson("没及时平仓，扛到深亏", NOW + 1, theme="出场时点不对")
        self._lesson("平得对，离场后继续跌", NOW + 2, theme="出场时点不对")
        self.assertIsNone(self._bucket("出场时点不对"))   # 垃圾桶已经不存在
        self.assertEqual(self._bucket("止盈太早")["count"], 1)
        self.assertEqual(self._bucket("止损太晚")["count"], 1)
        self.assertEqual(self._bucket("离场正确")["count"], 1)

    def test_unclear_direction_stays_in_place(self):
        """判不出方向的不硬搬：留原桶，别猜错方向。"""
        self._lesson("主要因为判断错误导致亏损，下次应更谨慎", NOW,
                     theme="出场时点不对")
        self.assertEqual(self._bucket("出场时点不对")["count"], 1)

    def test_correct_exit_is_not_injected(self):
        """「平得对」不是教训，绝不能注入——那等于告诉模型这没什么可学的。"""
        self._lesson("平得对，离场后行情继续下探", NOW,
                     theme="离场正确", verdict="ok")
        self.assertIn("暂无", autotrade.recent_lessons(scope="exit"))


class TestHardRuleGate(TempLedger):
    """出场类教训没过反事实检验，不许升级成铁律。

    实测：67 条「止盈太早」+ 48 条「出场时点不对」全部来自单边上涨的复盘，
    而 227 笔真实入场点的反事实回放证明所有放松出场的版本都更差。
    """

    def _many(self, theme, n, verdict=None, ts0=NOW - 5000):
        for i in range(n):
            row = {"ts": ts0 + i, "kind": "review", "scope": "exit",
                   "theme": theme, "lesson": "平在区间上沿就落袋，平后还在涨，该多拿"}
            if verdict:
                row["verdict"] = verdict
            autotrade.append_lesson(row)

    def test_unverified_exit_theme_cannot_be_hard_rule(self):
        self._many("止盈太早", 12)
        s = autotrade.recent_lessons(scope="exit")
        self.assertNotIn("【铁律", s)          # 附录里那句"别当铁律"不算
        self.assertIn("未过反事实检验", s)

    def test_verified_exit_theme_can_be_hard_rule(self):
        self._many("止盈太早", 12, verdict="early")
        s = autotrade.recent_lessons(scope="exit")
        self.assertIn("【铁律", s)
        self.assertNotIn("未过反事实检验", s)

    def test_entry_theme_unaffected_by_gate(self):
        """进场类教训不受这道闸门管：它们的错误在开仓那一刻就定死了。"""
        for i in range(8):
            autotrade.append_lesson({
                "ts": NOW - 100 + i, "kind": "review", "scope": "entry",
                "theme": "逆势抢反弹/接刀", "lesson": "下跌途中接刀，等真阳确认再进"})
        self.assertIn("【铁律", autotrade.recent_lessons(scope="entry"))


if __name__ == "__main__":
    unittest.main()
