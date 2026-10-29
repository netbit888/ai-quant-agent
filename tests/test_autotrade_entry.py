"""入场侧：解析、闸门、K 线读数、prompt 布局、建议与锚点。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import (
    NOW, TempLedger, bar, bars_down,
    bars_from_closes, bars_range, bars_up, ticks_for,
)


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


class TestNextWait(unittest.TestCase):
    """平仓后在冷却结束点提前唤醒一次——给模型一次决策机会，
    开不开是它的事（教训库和【刚平仓提醒】管着）。"""

    def test_close_wakes_just_after_cooldown(self):
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=30)
        self.assertEqual(autotrade.next_wait_sec({"kind": "close"}, cfg), 31)

    def test_other_kinds_wait_full_interval(self):
        cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=30)
        for kind in ("open", "hold", "skip", "open_failed"):
            self.assertEqual(autotrade.next_wait_sec({"kind": kind}, cfg), 60)

    def test_close_wait_bounded_by_interval(self):
        """冷却再短也不提前于「冷却+1 秒」，再长也不超过一个周期。"""
        for cd in (0, 5, 10, 30, 45, 58, 120):
            cfg = autotrade.Cfg(interval_sec=60, cooldown_sec=cd)
            w = autotrade.next_wait_sec({"kind": "close"}, cfg)
            self.assertGreaterEqual(w, 1.0)
            self.assertLessEqual(w, 60)

    def test_cooldown_over_a_minute_is_allowed(self):
        """「1 分钟内必须重开」的硬需求已删：冷却只是防连环单的下限，
        想配 2 分钟冷却应该允许，不再启动就拦。"""
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(cooldown_sec=30)), [])
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(cooldown_sec=120)), [])
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(cooldown_sec=60)), [])

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


class TestRecentCloseContext(unittest.TestCase):
    """刚平仓上下文：把「报复单」从事后复盘标记变成事前提示。

    只在平仓后 5 分钟（300 秒，报复单判定窗口）内注入，之后不再提——
    每一轮都念叨「你平仓很久了」是噪音。
    """

    ROWS = [{"ts": 1000 + i, "open": 4000, "high": 4001, "low": 3999,
             "close": 4000.5} for i in range(14)]

    def test_injected_within_window(self):
        p = autotrade.build_entry_prompt(
            self.ROWS, 4000.0, 4000.24,
            recent_close={"seconds_ago": 45, "pnl": -0.5})
        self.assertIn("刚平仓提醒", p)
        self.assertIn("45 秒前", p)
        self.assertIn("-0.50", p)
        self.assertIn("报复单", p)

    def test_not_injected_outside_window(self):
        for seconds in (0, None, 400, 300):
            rc = None if seconds is None else {"seconds_ago": seconds, "pnl": -0.5}
            p = autotrade.build_entry_prompt(self.ROWS, 4000.0, 4000.24,
                                             recent_close=rc)
            self.assertNotIn("刚平仓提醒", p)

    def test_not_injected_by_default(self):
        """老调用方不传 recent_close：prompt 里不该出现空占位。"""
        p = autotrade.build_entry_prompt(self.ROWS, 4000.0, 4000.24)
        self.assertNotIn("刚平仓提醒", p)
        self.assertNotIn("{recent_close}", p)

    def test_pnl_missing_still_injects(self):
        """last_close_pnl 缺省 None（老 state）：只说刚平仓，不编盈亏。"""
        p = autotrade.build_entry_prompt(
            self.ROWS, 4000.0, 4000.24, recent_close={"seconds_ago": 60})
        self.assertIn("刚平仓提醒", p)
        self.assertNotIn("盈亏", p)


class TestCanOpen(unittest.TestCase):
    def setUp(self):
        # 冷却按方向分档（2026-10-29）：反向 60 秒、同向 180 秒。写死在用例里，
        # 以后改默认值不会让这批断言悄悄换含义。
        self.cfg = autotrade.Cfg(cooldown_sec=60, cooldown_same_sec=180)

    def test_fresh_state_can_open(self):
        ok, why = autotrade.can_open(autotrade._empty_state(), NOW, False, self.cfg)
        self.assertTrue(ok, why)

    def test_blocked_when_position_exists(self):
        ok, why = autotrade.can_open(autotrade._empty_state(), NOW, True, self.cfg)
        self.assertFalse(ok)
        self.assertIn("已有持仓", why)

    def test_cooldown_after_close(self):
        """方向未定（问模型之前那次）按短的这档算，不能一上来就锁 3 分钟。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 10
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertFalse(ok)
        self.assertIn("冷却", why)
        # 剩余秒数用 cfg 现算，不写死：冷却参数是会被调的（10-29 就从 30 调到 60），
        # 写死数字的话每次调参都要改测试，而这条用例想验的是「按短档算」。
        self.assertIn(f"{self.cfg.cooldown_sec - 10} 秒", why)

    def test_cooldown_expired(self):
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        ok, _ = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertTrue(ok)

    def test_same_dir_still_blocked_after_short_cooldown(self):
        """反向的 60 秒走完了，同向那 180 秒还在——这正是要拦的那一半。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        state["last_close_dir"] = 1
        state["last_open_ts"] = NOW - 200
        ok, why = autotrade.can_open(state, NOW, False, self.cfg, direction=1)
        self.assertFalse(ok)
        self.assertIn("同向", why)
        self.assertIn(f"{self.cfg.cooldown_same_sec - 61} 秒", why)

    def test_same_dir_reopen_after_long_cooldown(self):
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 181
        state["last_close_dir"] = 1
        ok, why = autotrade.can_open(state, NOW, False, self.cfg, direction=1)
        self.assertTrue(ok, why)

    def test_unknown_direction_uses_short_cooldown(self):
        """问模型之前拿不到方向 → 按短的算，否则反向单会被同向规则一起锁住。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        state["last_close_dir"] = 1
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertTrue(ok, why)

    def test_old_state_without_last_close_dir_uses_short_cooldown(self):
        """老状态没有 last_close_dir：当方向未知，回落短的冷却，不能误拦。"""
        state = autotrade._empty_state()
        state.pop("last_close_dir", None)
        state["last_close_ts"] = NOW - 61
        ok, why = autotrade.can_open(state, NOW, False, self.cfg, direction=1)
        self.assertTrue(ok, why)

    def test_same_tick_double_open_guard(self):
        """只防同一轮重复开仓，不是交易节奏。用 60 秒当守卫会把自己锁死。"""
        state = autotrade._empty_state()
        state["last_open_ts"] = NOW - 1
        ok, why = autotrade.can_open(state, NOW, False, self.cfg)
        self.assertFalse(ok)
        self.assertIn("距上次开仓", why)

    def test_reverse_reopen_after_short_cooldown(self):
        """反向重开只要走完短的那档：反向只防连环单，不是要拦的那一半。
        同向那 180 秒由 test_same_dir_still_blocked_after_short_cooldown 锁住。"""
        state = autotrade._empty_state()
        state["last_close_ts"] = NOW - 61
        state["last_close_dir"] = 1
        state["last_open_ts"] = NOW - 90   # 上一笔是 90 秒前开的
        ok, why = autotrade.can_open(state, NOW, False, self.cfg, direction=-1)
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
        self.assertIn("日内交易员", autotrade.ENTRY_SYSTEM)
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


class TestStructure(unittest.TestCase):
    def test_straight_line_is_trend(self):
        s = autotrade._structure(bars_from_closes([4000 + i for i in range(14)]))
        self.assertAlmostEqual(s["er"], 1.0, places=4)
        self.assertEqual(s["streak"], 13)
        self.assertIn("趋势市", autotrade._structure_text(
            bars_from_closes([4000 + i for i in range(14)])))

    def test_zigzag_is_range(self):
        closes = [4000 + (2 if i % 2 else 0) for i in range(14)]
        s = autotrade._structure(bars_from_closes(closes))
        self.assertLess(s["er"], 0.30)
        self.assertIn("震荡市", autotrade._structure_text(bars_from_closes(closes)))

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
        """下跌趋势里贴下沿是顺势做空位，不是超卖——程序倾向不接多。

        漏了这一条时，14 根窗口在跌势里永远处于区间下部，位置判定永远显示
        「可考虑做多」；同时想空又被「追破位」铁律按住，结果只会做多。
        2026-10-28 起趋势市的标准动作是顺势直接做：跌势贴下沿给做空方向，
        但明确要求当前根弱势收（长下影 = 有人接，不做）。
        """
        rows = bars_from_closes([4000 - i for i in range(14)])
        text = autotrade._structure_text(rows)
        self.assertIn("下跌趋势", text)
        self.assertIn("顺势做空", text)
        self.assertIn("不是超卖", text)
        self.assertIn("不要反手接多", text)

    def test_uptrend_upper_edge_allows_trend_long(self):
        """涨势贴上沿：2026-10-28 起合法——趋势市顺势直接做，位置高不是理由。

        旧话术（"此处不是买点，等回踩"）让趋势市 70 次决策只开 4 仓；
        实测按它等回踩的单 47 笔均值 -1.21。现在改为：可顺势做多，
        唯一否决依据是当前根形状（插针顶不做）。
        """
        rows = bars_from_closes([4000 + i for i in range(14)])
        text = autotrade._structure_text(rows)
        self.assertIn("顺势做多", text)
        self.assertIn("高位本身不是否决理由", text)
        self.assertIn("插针顶", text)

    def test_range_keeps_symmetric_edges(self):
        """震荡里贴下沿仍然是买点，别把趋势线规则误用到震荡。"""
        rows = bars_from_closes([4002, 4000] * 7)     # 收在区间最低
        self.assertIn("贴下沿（程序倾向：可考虑做多", autotrade._structure_text(rows))

    # ---- 出场端只讲结构，不给行动指令（2026-10-29）---------------------
    #
    # 那笔 0.01 多单：plan=range（止盈 3.25），一根插针把 ER 从 0.04 顶到 0.74，
    # 出场 prompt 里同时出现「行情判定：趋势市·上行 → 顺势做多，不等回踩」和
    # 「震荡单…别指望它变成趋势」。模型 13 秒里被问三次、三次都挑前者，从
    # +4.86 拿到 -3.90（K 线真实高点对应 +8.30）。
    #
    # 入场端靠这句话选打法，必须留着；出场端 plan 已经锁死，再看一遍行情判定
    # 只会给它反悔的理由。所以两端的文案从这里分开。

    def test_exit_structure_drops_action_directive(self):
        rows = bars_from_closes([4000 + i for i in range(14)])   # ER=1.0 单边上行
        entry_text = autotrade._structure_text(rows)
        exit_text = autotrade._structure_text(rows, for_exit=True)
        # 入场端原封不动
        self.assertIn("顺势做多", entry_text)
        self.assertIn("高位本身不是否决理由", entry_text)
        self.assertIn("趋势市·上行", entry_text)
        # 出场端不许再出现「该做多还是做空」
        self.assertNotIn("顺势做多", exit_text)
        self.assertNotIn("程序倾向", exit_text)
        self.assertNotIn("趋势市·上行", exit_text)

    def test_exit_structure_keeps_the_facts(self):
        """去掉的只是行动指令，结构事实一条都不能少——模型仍要判断走没走完。"""
        rows = bars_from_closes([4000 + i for i in range(14)])
        text = autotrade._structure_text(rows, for_exit=True)
        self.assertIn("结构 ER=", text)
        self.assertIn("位置判定：贴着区间上沿", text)
        self.assertIn("行情判定：单边上行", text)
        self.assertIn("当前根", text)

    def test_exit_prompt_has_no_entry_directive(self):
        pos = {"direction": 1, "lots": 0.01, "price_open": 4000.0, "plan": "range",
               "opened_at": NOW, "entry_reason": "测试"}
        rows = bars_from_closes([4000 + i for i in range(14)])
        p = autotrade.build_exit_prompt(rows, pos, 4013.0, 4013.24)
        self.assertNotIn("顺势做多", p)
        self.assertNotIn("程序倾向", p)
        self.assertIn("别指望它变成趋势", p)      # plan_text 还在（这单是震荡单）

    def test_entry_prompt_keeps_entry_directive(self):
        """入场端一个字都不许动：趋势市选打法全靠这句。"""
        rows = bars_from_closes([4000 + i for i in range(14)])
        p = autotrade.build_entry_prompt(rows, 4013.0, 4013.24)
        self.assertIn("顺势做多", p)

    def test_structure_line_shows_direction(self):
        """净移动必须带符号：只给绝对值，模型分不清在涨还是在跌。"""
        rows = bars_from_closes([4000 - i for i in range(14)])
        self.assertLess(autotrade._structure(rows)["drift"], 0)
        self.assertIn("下行", autotrade._structure_text(rows))

    def test_zone_text_direct(self):
        """直接钉 pos/er/drift 的组合，绕开构造 K 线的麻烦。"""
        # 跌势贴下沿 → 顺势做空（不是超卖，不接多）
        self.assertIn("顺势做空", autotrade._zone_text(0.10, 0.70, -5.0, 0.35))
        self.assertIn("不要反手接多", autotrade._zone_text(0.10, 0.70, -5.0, 0.35))
        # 涨势贴下沿 → 回踩已发生，直接做多
        self.assertIn("倾向：回踩已经发生，直接做多",
                      autotrade._zone_text(0.10, 0.70, 5.0, 0.35))
        # 涨势贴上沿 → 顺势做多，位置高不是否决理由（2026-10-28 改）
        self.assertIn("顺势做多", autotrade._zone_text(0.90, 0.70, 5.0, 0.35))
        self.assertIn("插针顶", autotrade._zone_text(0.90, 0.70, 5.0, 0.35))
        # 跌势贴上沿 → 反抽已发生，直接做空
        self.assertIn("倾向：反抽已经发生，直接做空",
                      autotrade._zone_text(0.90, 0.70, -5.0, 0.35))
        # 震荡两边都对称
        self.assertIn("可考虑做多", autotrade._zone_text(0.10, 0.10, -5.0, 0.35))
        self.assertIn("可考虑做空", autotrade._zone_text(0.90, 0.10, 5.0, 0.35))
        # 趋势中间地带 → 可顺势做（2026-10-28：不再是"不动手"）
        self.assertIn("可顺势做空", autotrade._zone_text(0.50, 0.70, -5.0, 0.35))
        # 震荡中间地带不动手
        self.assertIn("不动手", autotrade._zone_text(0.50, 0.10, -5.0, 0.35))
        # trend_follow=False：趋势市整体不交易
        self.assertIn("趋势打法已关闭",
                      autotrade._zone_text(0.90, 0.70, 5.0, 0.35, trend_follow=False))

    def test_prog_direction_matches_zone_text(self):
        """_prog_direction 必须和 _zone_text 的建议同向——各算各的必然漂移。"""
        # 跌势（任意位置）→ 顺势空（2026-10-28 起位置不决定方向）
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 - i for i in range(14)]), 0.35), -1)
        # 涨势（任意位置）→ 顺势多（贴上沿不再返回 0）
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 + i for i in range(14)]), 0.35), 1)
        # 趋势打法关闭 → 趋势市不给方向
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 + i for i in range(14)]), 0.35,
            trend_follow=False), 0)
        # 震荡收在区间最低 → 倾向多
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4002, 4000] * 7), 0.35), 1)
        # 震荡中间地带 → 无建议（bars_from_closes 每根带 ±1 影线，
        # 收盘要真落在区间 35%~65% 里，得用比影线更高的振幅）
        mid_rows = [{"time": NOW + i * 60, "open": 4000 + (2 if i % 2 else 0),
                     "high": 4000 + (2 if i % 2 else 0) + 3,
                     "low": 4000 + (2 if i % 2 else 0) - 3,
                     "close": 4000 + (2 if i % 2 else 0)} for i in range(14)]
        self.assertEqual(autotrade._prog_direction(mid_rows, 0.35), 0)

    def test_uptrend_gives_trend_direction(self):
        """趋势市：程序给顺势方向，与位置无关（2026-10-28 改）。

        原来「涨势贴上沿」返回 0（不给追高背书），后果是趋势市里程序长期
        没有方向，事后无法按 regime 对账。实测等回踩 47 笔均值 -1.21，
        顺势直接做 85 点固定持 10 根 +0.148——方向判断有边际，该给。
        追高与否交给当前根形状（_last_bar_shape）和提示词管，不由位置否决。
        """
        rows = bars_from_closes([4000 + i for i in range(14)])
        self.assertGreater(autotrade._structure(rows)["pos"], 0.65)
        self.assertEqual(autotrade._prog_direction(rows, 0.35), 1)
        # 震荡贴上沿仍然给做空（这是标准动作，不能连坐）
        self.assertEqual(autotrade._prog_direction(
            bars_from_closes([4000 + (2 if i % 2 else 0) for i in range(14)]), 0.35), -1)

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
        # 震荡市必须显式允许贴边动手
        self.assertIn("只有震荡市可以「贴边就动手」", sys)
        self.assertIn("贴着上沿做空不叫追空", sys)
        # 看跌 ≠ 不做，这句是把 p_up 语义钉死的
        self.assertIn("看跌就要考虑做空", sys)
        # 两套打法必须先判行情：趋势市"等回踩"不再合法，插针顶是当前根形状说了算
        self.assertIn("先判行情，再选打法", sys)
        self.assertIn("顺势直接做", sys)
        self.assertIn("插针顶", sys)


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


class TestEntryAdvice(unittest.TestCase):
    """程序对开仓的两条建议。用例里的原话全部取自 10-27 那轮真实日志。

    两条建议 2026-10-28 起默认不拦单（决策权交还模型），但"程序怎么看"
    必须算得准——否则面板上的「模型否决了建议」和日志里的 would_veto
    全是假的，事后拿它做对比就成了自欺。
    用真实原话测的理由：难的不是造句子，是"赢单的措辞不能被误判成问题单"。
    """

    def setUp(self):
        self.cfg = autotrade.Cfg()

    # ---------------- 闸门 1：理由与动作自相矛盾 ----------------

    def test_hangdan_with_act_true_is_vetoed(self):
        """18:45 那笔：reason 写"回踩 4004 挂多，不追高"，成交在 4010.917。"""
        veto = autotrade._entry_advice(
            self.cfg, 1, {"pos": 0.96},
            "回踩T-1实体中位4004附近挂多，不追高但方向偏多", True)
        self.assertIsNotNone(veto)
        self.assertEqual(veto[0], "act_guard")
        self.assertIn("挂多", veto[1])

    def test_wait_for_confirmation_with_act_true_is_vetoed(self):
        """18:09 那笔：reason 写"阴线未止跌…等收阳确认"，当场就进了，-3.49。"""
        veto = autotrade._entry_advice(
            self.cfg, 1, {"pos": 0.28},
            "ER=0.03窄幅震荡，3999附近支撑；当前阴线未止跌，轻仓做多等收阳确认", True)
        self.assertEqual(veto[0], "act_guard")

    def test_act_false_is_never_double_counted(self):
        """模型自己说不做时，这里不能抢功——它走到这一步之前就 return 了。"""
        self.assertIsNone(autotrade._entry_advice(
            self.cfg, 1, {"pos": 0.96}, "等确认", False))

    def test_winning_reasons_are_not_blocked(self):
        """四笔赢单的原话必须全部放行。

        它们的 reason 里都有"企稳""止跌""贴边"这类词——如果把裸词
        （而不是"等企稳""等止跌"）收进词表，这四笔 +4.00/+4.04/+6.35/+5.12
        会被自己的闸门挡掉，等于用规则杀掉当天唯一的赚钱来源。
        """
        winners = [
            "ER=0.07窄幅震荡，收盘66%贴上沿，连阳3根且当前81笔tick缩量，贴上沿做空，目标回4004.5均线",
            "ER=0.28窄幅震荡，收盘位置17%贴下沿，当前K收阳且tick连续上行收复3993.8，符合震荡贴边做多",
            "ER0.23震荡贴下沿25%，T-1当前连续收阳止跌，tick放量246笔承接，M5偏空故只轻仓看反弹",
            "ER0.21窄幅震荡，收盘位置28%贴下沿，T-2探底3993.3后连收两阳止跌，位置符合震荡下沿做多",
            "ER0.37、位置26%贴下沿，T-1与当前连阳收于3994.876，止跌确认；M5震荡不冲突，轻仓试多",
        ]
        for r in winners:
            self.assertIsNone(autotrade._hesitation_word(r), r)
            self.assertIsNone(
                autotrade._entry_advice(self.cfg, 1, {"pos": 0.25}, r, True), r)

    def test_unknown_reason_is_not_vetoed(self):
        self.assertIsNone(autotrade._entry_advice(self.cfg, -1, {"pos": 0.8}, "", True))
        self.assertIsNone(autotrade._entry_advice(self.cfg, -1, {"pos": 0.8}, None, True))

    # ---------------- 闸门 2：贴上沿做多 ----------------

    def test_long_at_top_edge_is_vetoed(self):
        """17:07 那笔：分位 0.97、ER 0.71、drift +8.4，追多 -3.24。"""
        veto = autotrade._entry_advice(
            self.cfg, 1, {"pos": 0.9664},
            "ER0.71单边上涨，5连阳收盘97%贴顶；回踩3998-3996是顺势多单点", True)
        self.assertEqual(veto[0], "top_long_ban")
        self.assertIn("0.97", veto[1])

    def test_short_at_top_edge_is_allowed(self):
        """贴上沿做空是本打法的标准动作（15:44 那笔 +3.71），不能连坐。"""
        self.assertIsNone(autotrade._entry_advice(
            self.cfg, -1, {"pos": 0.80},
            "ER=0.19窄幅震荡，收盘位置80%贴上沿，贴边做空符合区间卖压", True))

    def test_long_below_edge_is_allowed(self):
        self.assertIsNone(autotrade._entry_advice(
            self.cfg, 1, {"pos": 0.6499}, "贴下沿做多", True))

    def test_missing_pos_does_not_veto(self):
        """特征快照算不出来时不能凭空拦单——那是数据问题，不是位置问题。"""
        self.assertIsNone(autotrade._entry_advice(self.cfg, 1, {}, "贴边做多", True))

    def test_threshold_follows_zone_edge(self):
        """门槛必须跟着 cfg 走：面板把 zone_edge 调到 0.20，0.75 就不该再拦。"""
        cfg = autotrade.Cfg(zone_edge=0.20)
        self.assertIsNone(autotrade._entry_advice(cfg, 1, {"pos": 0.79}, "贴边做多", True))
        self.assertIsNotNone(autotrade._entry_advice(cfg, 1, {"pos": 0.81}, "贴边做多", True))

    # ---------------- 开关与提示词一致性 ----------------

    def test_gate_switch_decides_whether_it_blocks(self):
        """建议永远算得出来；要不要拦由开关定，默认两个都是关的。

        2026-10-28 用户定的定位：程序只给建议和特征，决策权在模型。
        这意味着默认情况下 `_entry_advice` 有意见、但 `_gate_blocks` 返回 False。
        """
        cfg = autotrade.Cfg()
        self.assertFalse(cfg.act_guard)
        self.assertFalse(cfg.top_long_ban)
        advice = autotrade._entry_advice(cfg, 1, {"pos": 0.99}, "回踩4004挂多，不追高", True)
        self.assertIsNotNone(advice)                       # 建议照给
        self.assertFalse(autotrade._gate_blocks(cfg, advice[0]))   # 但不拦

        strict = autotrade.Cfg(act_guard=True, top_long_ban=True)
        self.assertTrue(autotrade._gate_blocks(strict, "act_guard"))
        self.assertTrue(autotrade._gate_blocks(strict, "top_long_ban"))
        self.assertFalse(autotrade._gate_blocks(strict, "unknown_code"))

    def test_prompt_states_the_two_gates(self):
        """提示词必须和代码说同一件事。

        {ZONE_UP} 要真的被换掉——留着占位符等于没写。
        关键字别再写"代码会拦"：现在默认不拦了，提示词要是还说"会拦"，
        模型就会以为写了 act=true 也没用，反而乱写。
        """
        cfg = autotrade.Cfg()
        s = autotrade.system_for(autotrade.ENTRY_SYSTEM, cfg)
        self.assertNotIn("{ZONE_UP}", s)
        self.assertIn("没有挂单", s)
        self.assertIn("不会替你拦单", s)
        # 门槛由 zone_edge 派生，这里跟着 cfg 算，别写死 65%——改默认值不该红。
        self.assertIn(f"{1 - cfg.zone_edge:.0%}", s)      # 1 - 默认 zone_edge 0.35
        cfg2 = autotrade.Cfg(zone_edge=0.20)
        s2 = autotrade.system_for(autotrade.ENTRY_SYSTEM, cfg2)
        self.assertIn(f"{1 - cfg2.zone_edge:.0%}", s2)


class TestEntryAdviceInTick(TempLedger):
    """建议真的接进 tick() 了吗？

    2026-10-28 起默认行为是「只建议不拦」——所以这里既要证明默认放行，
    也要证明把开关打开后仍然拦得住（否则开关是死的）。
    走完整 tick()：纯函数对、接线错的话，单子照样会出去。
    """

    def test_default_lets_the_model_decide(self):
        """默认（两个开关都关）：贴着上沿做多照样开，只留一条 would_veto。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "agree": True, "reason": "结构支持",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                              return_value={"ok": True, "price": 4001.0,
                                            "order": 600}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["would_veto"], "top_long_ban")   # 意见留痕
        self.assertTrue(row["feat"]["ok"])
        state = autotrade.load_state()
        self.assertEqual(state["last_decision"]["advice"], "top_long_ban")
        self.assertNotIn("veto", state["last_decision"])       # 没有拦

    def test_top_long_ban_blocks_when_switched_on(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "agree": True, "reason": "结构支持",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(autotrade.Cfg(dry_run=True, top_long_ban=True),
                                 now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "top_long_ban")
        m_open.assert_not_called()

    def test_act_guard_blocks_when_switched_on(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.3,
                                             "agree": True,
                                             "reason": "反抽到4002挂空，等收阴确认",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(autotrade.Cfg(dry_run=True, act_guard=True),
                                 now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_down(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "act_guard")
        self.assertIn("模型原话", row["reason"])   # 面板上要看得到它自己写了什么
        m_open.assert_not_called()

    def test_veto_lands_in_last_decision(self):
        """面板要能区分「模型自己不做」和「被规则否决」——后者挂在 last_decision 上。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "send_open"):
            autotrade.tick(autotrade.Cfg(dry_run=True, top_long_ban=True),
                           now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        self.assertEqual(autotrade.load_state()["last_decision"]["veto"],
                         "top_long_ban")


class TestWaitAnchor(unittest.TestCase):
    """等回踩的价位不许跟着价格漂。

    2026-10-27 22:29–22:40 实测十二分钟：模型说的回踩位 4037 → 4038 → 4041 →
    4043 → 4045 → 4046 → 4049.9，价格涨了 12 美元，一次都没等到。名义在等、
    实际在追，而且真回调时锚点已经变成新的高位。
    """

    # ---------------- 解析 ----------------

    def test_parse_real_reasons(self):
        """每一条都是当晚日志里的原话（价格 4050 附近）。"""
        cases = [
            ("ER0.57 单边趋势，收盘位置达96%贴近上沿，此时做多是追最后一根阳线；"
             "等价格回踩到4046附近再做多方。", 4046.0),
            ("ER0.55单边上行，收盘位置99%贴上沿，MA28上方+10.1已显过热；"
             "连阳3根不追多，等回踩前根实体中位4049.9再进", 4049.9),
            ("距MA28已+6.63=2.6倍均幅，追高即接最后一棒；"
             "等回踩至T-1实体中位4043附近再顺势多", 4043.0),
            ("连阳3根不追多，等回踩4041-4040实体中位再进", 4041.0),
            ("ER0.35方向不明，T-1放量476笔后涨势未续，等回踩4040附近再顺势多", 4040.0),
        ]
        for reason, want in cases:
            self.assertEqual(autotrade.parse_wait_level(reason, 4050.0), want, reason)

    def test_no_level_returns_none(self):
        """只说"等贴边再进"但没给价位——不锁，免得锁一个模型没说的东西。"""
        self.assertIsNone(autotrade.parse_wait_level(
            "震荡ER=0.19，收盘58%在中间地带，程序倾向不动手，等贴边再进", 4050.0))

    def test_far_away_number_is_ignored(self):
        """差 0.5% 开外的价位等的不是这一波结构（14 根 M1 振幅只有几美元）。"""
        self.assertIsNone(autotrade.parse_wait_level("等回踩4000附近", 4050.0))

    def test_numbers_in_the_same_sentence_do_not_confuse_it(self):
        """ER/倍数/前高都在句子里，只有紧跟在"等回踩"后面的才算。"""
        r = "ER0.72单边涨，收盘95%贴上沿，振幅5.5倍已拉伸，前高4050.9，等回踩4045一线再做多"
        self.assertEqual(autotrade.parse_wait_level(r, 4050.0), 4045.0)

    # ---------------- 状态机 ----------------

    def _wait(self, level=4044.0, ts=1500, side=1):
        # ts 默认贴近 now=2000（500 秒前定的锚，没过 900 秒 ttl）
        return {"level": level, "ts": ts, "side": side, "drift": 0}

    def test_first_time_sets_anchor(self):
        st, flag = autotrade._wait_step(None, "等回踩4044附近再进", False,
                                        4050.0, 1.5, 2000, 1)
        self.assertEqual(flag, "set")
        self.assertEqual(st["level"], 4044.0)
        self.assertEqual(st["ts"], 2000)

    def test_upward_drift_is_rejected(self):
        """这就是当初那个毛病：新价位比原锚点高，系统不采纳。"""
        st, flag = autotrade._wait_step(self._wait(4044.0), "等回踩4049.9再进",
                                        False, 4050.0, 1.5, 2000, 1)
        self.assertEqual(flag, "drift")
        self.assertEqual(st["level"], 4044.0)      # 原锚点不动
        self.assertEqual(st["drift"], 1)

    def test_more_conservative_move_is_allowed(self):
        """往下改是"更耐心的等"，不是漂移，允许，且 ts 保留（等的还是同一次机会）。"""
        st, flag = autotrade._wait_step(self._wait(4044.0), "等回踩4030附近再进",
                                        False, 4050.0, 1.5, 2000, 1)
        self.assertEqual(flag, "move")
        self.assertEqual(st["level"], 4030.0)
        self.assertEqual(st["ts"], 1500)

    def test_short_side_drifts_downward(self):
        """空单镜像：价位往下挪才是跟着价格跑。"""
        st, flag = autotrade._wait_step(self._wait(4044.0, side=-1),
                                        "等反抽4038附近做空", False, 4040.0, 1.5, 2000, -1)
        self.assertEqual(flag, "drift")
        self.assertEqual(st["level"], 4044.0)

    def test_expired_anchor_is_dropped(self):
        st, flag = autotrade._wait_step(self._wait(4044.0, ts=1000), "等回踩4044附近",
                                        False, 4050.0, 1.5, 1000 + 901, 1)
        self.assertEqual(flag, "expired")
        self.assertEqual(st["level"], 4044.0)      # 过期后按这轮重设，ts 变新
        self.assertEqual(st["ts"], 1000 + 901)

    def test_touched_but_still_refusing_consumes_anchor(self):
        """已经跌到锚点还不进 → 这条等待作废，别占着位置假装在等。"""
        st, flag = autotrade._wait_step(self._wait(4044.0), "再看一下，不动手",
                                        False, 4042.0, 1.5, 2000, 1)
        self.assertIsNone(st)
        self.assertEqual(flag, "consumed")

    def test_entering_clears_anchor(self):
        st, flag = autotrade._wait_step(self._wait(4044.0), "到了，进", True,
                                        4044.0, 1.5, 2000, 1)
        self.assertIsNone(st)
        self.assertEqual(flag, "cleared_open")

    def test_keep_when_no_new_level(self):
        st, flag = autotrade._wait_step(self._wait(4044.0), "等确认再说", False,
                                        4050.0, 1.5, 2000, 1)
        self.assertEqual(flag, "keep")
        self.assertEqual(st["level"], 4044.0)

    # ---------------- 喂回模型的提示 ----------------

    def test_note_says_locked_with_gap(self):
        note = autotrade._wait_note(self._wait(4044.0, ts=2000), 4050.0, 1.5,
                                    "XAUUSDm", 2060)
        self.assertIn("等待锚点·锁定", note)
        self.assertIn("4044", note)
        self.assertIn("不会生效", note)      # 明确告诉它改成近价没用
        self.assertIn("60 秒", note)

    def test_note_announces_arrival(self):
        note = autotrade._wait_note(self._wait(4044.0, ts=2000), 4043.0, 1.5,
                                    "XAUUSDm", 2060)
        self.assertIn("已到位", note)
        self.assertIn("二选一", note)

    def test_note_announces_expiry(self):
        note = autotrade._wait_note(self._wait(4044.0, ts=1000), 4050.0, 1.5,
                                    "XAUUSDm", 1000 + 1200)
        self.assertIn("已作废", note)

    def test_no_anchor_no_note(self):
        self.assertEqual(autotrade._wait_note(None, 4050.0, 1.5, "XAUUSDm", 2000), "")
        self.assertEqual(autotrade._wait_note({"level": 0}, 4050.0, 1.5,
                                              "XAUUSDm", 2000), "")

    def test_prompt_carries_the_note(self):
        p = autotrade.build_entry_prompt(bars_up(30), 4050.0, 4050.24,
                                         wait_note="【等待锚点·锁定】4044\n")
        self.assertIn("等待锚点·锁定", p)


class TestWaitAnchorInTick(TempLedger):
    """锚点必须真的接到 tick 上：纯函数再对，没接线就是没改。"""

    def _seed(self, level=4044.0, ts=NOW - 120):
        st = autotrade._empty_state()
        st["wait"] = {"level": level, "ts": ts, "side": 1, "drift": 0}
        autotrade.save_state(st)

    def test_previous_anchor_reaches_the_model(self):
        self._seed()
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": False, "p_up": 0.6,
                                             "reason": "等回踩4044附近",
                                             "source": "llm"}) as m:
            autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                           quote=(4050.0, 4050.24), rows=bars_range(),
                           positions=[])          # 不传就会去终端查持仓
        note = m.call_args.kwargs.get("wait_note", "")
        self.assertIn("等待锚点·锁定", note)
        self.assertIn("4044", note)

    def test_drift_is_blocked_and_counted(self):
        self._seed()
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": False, "p_up": 0.6,
                                             "reason": "等回踩4049.9再进多方",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open"):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4050.0, 4050.24), rows=bars_range(),
                                 positions=[])
        self.assertEqual(row["wait_flag"], "drift")
        state = autotrade.load_state()
        self.assertEqual(state["wait"]["level"], 4044.0)   # 没被改走
        self.assertEqual(state["wait_drift"], 1)

    def test_anchor_survives_into_next_tick(self):
        """第一轮定锚、第二轮把锚点写回 prompt——这才是"外部记忆"的意义。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": False, "p_up": 0.6,
                                             "reason": "等回踩4044附近再进",
                                             "source": "llm"}) as m:
            autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                           quote=(4050.0, 4050.24), rows=bars_range(),
                           positions=[])
            self.assertEqual(m.call_args.kwargs.get("wait_note"), "")   # 第一轮没有
            autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW + 60,
                           quote=(4050.0, 4050.24), rows=bars_range(),
                           positions=[])
        self.assertIn("等待锚点·锁定", m.call_args.kwargs.get("wait_note", ""))

    def test_anchor_disabled_in_trend(self):
        """趋势市里锚点整体停用（2026-10-28）：不清不楚的"等回踩"不留状态。

        实测按锚点等回踩 47 笔均值 -1.21，等回踩是接刀不是低吸；趋势打法的
        标准动作是顺势直接做。所以单边行情下：旧锚点被清、新锚点定不了、
        模型也看不到任何 wait_note。
        """
        self._seed()
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": False, "p_up": 0.6,
                                             "reason": "等回踩4044附近",
                                             "source": "llm"}) as m:
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4050.0, 4050.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["wait_flag"], "disabled")
        self.assertIsNone(autotrade.load_state().get("wait"))
        self.assertEqual(m.call_args.kwargs.get("wait_note"), "")


class TestPlanRegime(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False)

    def _open(self, rows, price=4001.2, order=601):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": price,
                                             "order": order}):
            return autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                  rows=rows, positions=[])

    def test_trend_open_locks_plan(self):
        """趋势市开仓：position / open 行 / last_decision 三处都落 plan=trend。"""
        row = self._open(bars_up())
        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["plan"], "trend")
        state = autotrade.load_state()
        self.assertEqual(state["position"]["plan"], "trend")
        self.assertEqual(state["last_decision"]["regime"], "trend")

    def test_range_open_locks_plan(self):
        row = self._open(bars_range())
        self.assertEqual(row["plan"], "range")
        self.assertEqual(autotrade.load_state()["position"]["plan"], "range")

    def test_open_locks_atr(self):
        """开仓把当时的 ATR 锁进持仓：出场阈值以后要按它归一化，中途重算
        会让同一笔仓前后被两套门槛管（和 plan 同一个原则）。"""
        row = self._open(bars_range())
        atr = float(row["feat"]["atr"])
        self.assertGreater(atr, 0.0)
        self.assertAlmostEqual(autotrade.load_state()["position"]["atr"], atr,
                               places=6)

    def test_trend_position_holds_below_wider_tp(self):
        """趋势单浮盈 4.0：按震荡门槛早该问模型，按趋势门槛（7.0）继续持有。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 600,
                             "p_up": 0.6, "entry_reason": "顺势", "dry_run": True,
                             "plan": "trend"}
        autotrade.save_state(state)
        cfg = autotrade.Cfg(dry_run=True)
        with mock.patch.object(autotrade, "ask_exit") as m_exit:
            row = autotrade.tick(cfg, now=NOW, quote=(4004.0, 4004.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row["source"], "tp_wait")
        self.assertIn("7.0", row["reason"])
        m_exit.assert_not_called()

    def test_position_without_plan_keeps_old_behavior(self):
        """旧 state 没有 plan 字段：按震荡参数管（浮盈 4.0 > 3.25 → 问模型）。"""
        state = autotrade._empty_state()
        state["position"] = {"ticket": -1, "direction": 1, "lots": 0.01,
                             "price_open": 4000.0, "opened_at": NOW - 600,
                             "p_up": 0.6, "entry_reason": "x", "dry_run": True}
        autotrade.save_state(state)
        cfg = autotrade.Cfg(dry_run=True)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": False, "reason": "继续拿",
                                             "source": "llm"}) as m_exit:
            row = autotrade.tick(cfg, now=NOW, quote=(4004.0, 4004.24),
                                 rows=bars_up(), positions=[])
        m_exit.assert_called_once()      # 到 3.25 门槛了，决定权交给模型
        self.assertEqual(row["kind"], "hold")

    def test_exit_system_renders_plan_params(self):
        """system 里的门槛数字必须按 plan 渲染，不能骗模型。"""
        cfg = autotrade.Cfg()
        trend_sys = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg, plan="trend")
        self.assertIn("浮盈不到 7 USD", trend_sys)
        self.assertIn("持仓满 180 分钟", trend_sys)
        range_sys = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg, plan="range")
        self.assertIn("浮盈不到 3.25 USD", range_sys)
        self.assertIn("持仓满 60 分钟", range_sys)


class TestLastBarShape(unittest.TestCase):
    def test_strong_close(self):
        rows = [bar(4000, 4002, 3999, 4001.8, NOW)]
        sh = autotrade._last_bar_shape(rows)
        self.assertTrue(sh["ok"])
        self.assertGreater(sh["clv"], 0.8)      # 收在自身区间顶部附近
        self.assertLess(sh["upper_wick"], 0.2)

    def test_shooting_star(self):
        """冲高被打回来：上影长、收盘掉到下半——趋势市追这种就是接最后一棒。"""
        rows = [bar(4000, 4002.5, 3999.5, 3999.8, NOW)]
        sh = autotrade._last_bar_shape(rows)
        self.assertTrue(sh["ok"])
        self.assertGreater(sh["upper_wick"], 0.8)
        self.assertLess(sh["clv"], 0.2)

    def test_bad_data(self):
        self.assertFalse(autotrade._last_bar_shape([])["ok"])
        self.assertFalse(autotrade._last_bar_shape(
            [{"time": NOW, "open": 0, "high": 0, "low": 0, "close": 0}])["ok"])

    def test_feature_snapshot_carries_shape(self):
        """shape 落盘是事后「追高单里形状占多大权重」的唯一数据来源。"""
        feat = autotrade.feature_snapshot(bars_up(14), autotrade.Cfg())
        self.assertTrue(feat["ok"])
        self.assertTrue(feat["shape"]["ok"])
        self.assertIn("clv", feat["shape"])


class TestWaitStepRegime(unittest.TestCase):
    def test_trend_disables_anchor(self):
        """趋势市里连"set"都不接受——模型说"等回踩到 X"不留任何状态。"""
        wait, flag = autotrade._wait_step(
            None, "等回踩4044再进", False, 4050.0, 1.5, NOW, 1,
            regime="trend")
        self.assertIsNone(wait)
        self.assertEqual(flag, "disabled")

    def test_trend_clears_existing_anchor(self):
        wait, flag = autotrade._wait_step(
            {"level": 4044.0, "ts": NOW - 60, "side": 1, "drift": 0},
            "继续等", False, 4050.0, 1.5, NOW, 1, regime="trend")
        self.assertIsNone(wait)
        self.assertEqual(flag, "disabled")

    def test_range_unchanged(self):
        """震荡市锚点逻辑一个字没动（回归保险）。"""
        wait, flag = autotrade._wait_step(
            None, "等回踩4044再进", False, 4050.0, 1.5, NOW, 1,
            regime="range")
        self.assertIsNotNone(wait)
        self.assertEqual(flag, "set")


class TestMtfConflictAdvice(unittest.TestCase):
    """中期方向与这笔相反 → 给建议（默认不拦）。

    实测 2026-10-28 本场：两笔亏损开仓时 M5 净变动 -7.72 / -7.11，
    唯一赢的那笔（上沿做空）M5 只有 -2.45。M1 判震荡就允许"贴下沿做多"，
    中期方向在这套打法里没有发言权——这是漏洞。
    """

    def _feat(self, regime="range", drift=-7.1, gap=-2.0):
        return {"regime": regime, "pos": 0.34,
                "mtf": {"tf": "M5", "drift": drift, "er": 0.37},
                "ma28_gap": gap, "ma28_side": -1}

    def test_long_against_m5_downtrend_is_flagged(self):
        a = autotrade._entry_advice(autotrade.Cfg(), 1, self._feat(), "贴下沿做多", True)
        self.assertIsNotNone(a)
        self.assertEqual(a[0], "mtf_conflict")
        self.assertIn("-7.10", a[1])
        self.assertIn("逆着中期接刀", a[1])

    def test_short_with_m5_downtrend_is_fine(self):
        """顺着中期方向做空：赢的那笔就是这个形态，不许误伤。"""
        a = autotrade._entry_advice(autotrade.Cfg(), -1, self._feat(drift=-2.45),
                                    "贴上沿做空", True)
        self.assertIsNone(a)

    def test_mild_drift_is_not_flagged(self):
        """M5 只有 -2.45 算走平，不到 5.0 的门槛就不开口。"""
        a = autotrade._entry_advice(autotrade.Cfg(), 1, self._feat(drift=-2.45),
                                    "贴下沿做多", True)
        self.assertIsNone(a)

    def test_short_against_m5_uptrend_is_flagged(self):
        a = autotrade._entry_advice(autotrade.Cfg(), -1, self._feat(drift=6.2),
                                    "贴下沿做空", True)
        self.assertIsNotNone(a)
        self.assertEqual(a[0], "mtf_conflict")
        self.assertIn("上行", a[1])

    def test_trend_regime_not_flagged(self):
        """趋势市本来就是顺势打法，由 prog_dir 管，这条不插手。"""
        a = autotrade._entry_advice(autotrade.Cfg(), 1, self._feat(regime="trend"),
                                    "顺势做多", True)
        self.assertIsNone(a)

    def test_missing_mtf_is_not_flagged(self):
        self.assertIsNone(autotrade._entry_advice(
            autotrade.Cfg(), 1, {"regime": "range", "pos": 0.34}, "贴下沿", True))

    def test_advice_only_unless_switch_is_on(self):
        """默认只建议不拦；开关打开才升级成闸门。"""
        cfg = autotrade.Cfg()
        self.assertFalse(autotrade._gate_blocks(cfg, "mtf_conflict"))
        self.assertTrue(autotrade._gate_blocks(
            autotrade.Cfg(mtf_conflict=True), "mtf_conflict"))

    def test_real_session_replay(self):
        """本场三笔真实特征回放：两笔亏损命中，赢的那笔不命中。"""
        losers = [
            {"direction": 1, "regime": "range", "mtf": {"drift": -7.715},
             "ma28_gap": -3.47},
            {"direction": 1, "regime": "range", "mtf": {"drift": -7.105},
             "ma28_gap": -1.98},
        ]
        winner = {"direction": -1, "regime": "range", "mtf": {"drift": -2.45},
                  "ma28_gap": 0.17}
        for f in losers:
            a = autotrade._entry_advice(autotrade.Cfg(), f["direction"], f,
                                        "贴下沿做多", True)
            self.assertEqual(a[0] if a else None, "mtf_conflict")
        self.assertIsNone(autotrade._entry_advice(
            autotrade.Cfg(), winner["direction"], winner, "贴上沿做空", True))
