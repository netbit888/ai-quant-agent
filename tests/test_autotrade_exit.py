"""出场与复盘：止盈判定、复盘队列、教训库、峰值停滞。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import (
    NOW, TempLedger, bar, bars_down,
    bars_up,
)


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
            row = autotrade.tick(autotrade.Cfg(interval_sec=60, act_guard=False,
                                               top_long_ban=False), now=NOW,
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


class TestStaleHit(unittest.TestCase):
    """峰值停滞判定（2026-10-29）。纯函数：不碰 state、不读时钟。

    背景那一笔（多 0.01@4039.118，atr=1.35，tp=3.25）：峰值 1.85 之后
    455 秒、7 轮 tick 模型一次没被问过。这里每条用例都对应一个会让
    "唤醒"变成"每分钟烧一次 token"或"对着旧持仓抛异常"的边界。
    """

    def _pos(self, peak=1.85, peak_ts=NOW - 200, **kw) -> dict:
        p = {"ticket": -1, "direction": 1, "lots": 0.01,
             "price_open": 4039.118, "plan": "range"}
        if peak is not None:
            p["peak_pnl"] = peak
        if peak_ts is not None:
            p["peak_ts"] = peak_ts
        p.update(kw)
        return p

    def test_fires_when_peak_goes_stale_and_pnl_fades(self):
        """峰值 200 秒没抬高、浮盈掉到 0.70（< 3.25×0.6）：该把决定权交出去。"""
        self.assertTrue(autotrade._stale_hit(
            autotrade.Cfg(tp_min_usd=3.25), self._pos(), 0.70, NOW))

    def test_does_not_fire_while_peak_is_fresh(self):
        self.assertFalse(autotrade._stale_hit(
            autotrade.Cfg(tp_min_usd=3.25), self._pos(peak_ts=NOW - 60), 0.70, NOW))

    def test_does_not_fire_when_pnl_still_close_to_tp(self):
        """浮盈还在门槛 60% 以上：它马上会被 tp_wake 问到，多问一次是浪费。"""
        self.assertFalse(autotrade._stale_hit(
            autotrade.Cfg(tp_min_usd=3.25), self._pos(), 2.50, NOW))

    def test_missing_peak_keys_are_safe(self):
        """旧 state 和测试里手写的 pos 都没有 peak_ts —— 必须判否，不能抛。"""
        cfg = autotrade.Cfg(tp_min_usd=3.25)
        self.assertFalse(autotrade._stale_hit(cfg, {"direction": 1}, 0.5, NOW))
        self.assertFalse(autotrade._stale_hit(cfg, None, 0.5, NOW))
        self.assertFalse(autotrade._stale_hit(cfg, self._pos(peak=None), 0.5, NOW))

    def test_negative_or_tiny_peak_is_not_our_business(self):
        """从没赚过的单子不归它管——那是止损和持仓上限的活。"""
        cfg = autotrade.Cfg(tp_min_usd=3.25)
        self.assertFalse(autotrade._stale_hit(cfg, self._pos(peak=-0.5), -2.0, NOW))
        self.assertFalse(autotrade._stale_hit(cfg, self._pos(peak=0.3), 0.1, NOW))

    def test_reask_cooldown_prevents_asking_every_round(self):
        """防抖：没有它 peak_ts 不再更新会让判定每轮为真，退化成每 60 秒问一次。"""
        cfg = autotrade.Cfg(tp_min_usd=3.25)
        p = self._pos(stale_ts=NOW - 10)
        self.assertFalse(autotrade._stale_hit(cfg, p, 0.70, NOW))
        self.assertTrue(autotrade._stale_hit(cfg, p, 0.70, NOW + 300))

    def test_switch_off_restores_old_behavior(self):
        cfg = autotrade.Cfg(tp_min_usd=3.25, stale_wake=False)
        self.assertFalse(autotrade._stale_hit(cfg, self._pos(), 0.70, NOW))

    def test_trend_plan_uses_its_own_tp(self):
        """趋势单门槛 7.0：fp 3.0 < 4.2 仍算"离门槛很远"，4.5 就不该唤醒。"""
        cfg = autotrade.Cfg(tp_min_usd=3.25, tp_min_trend=7.0)
        p = self._pos(plan="trend", peak=4.0)
        self.assertTrue(autotrade._stale_hit(cfg, p, 3.0, NOW))
        self.assertFalse(autotrade._stale_hit(cfg, p, 4.5, NOW))


class TestStaleWakeInTick(TempLedger):
    """接线：纯函数再对，没接到 tick 上就等于没改。"""

    def _seed(self, peak=1.85, peak_ts=NOW - 240, plan="range"):
        st = autotrade._empty_state()
        st["position"] = {
            "ticket": -1, "direction": 1, "lots": 0.01, "price_open": 4000.0,
            "opened_at": NOW - 400, "p_up": 0.6, "dry_run": True,
            "plan": plan, "atr": 1.35,
            "peak_pnl": peak, "peak_ts": peak_ts,
        }
        autotrade.save_state(st)

    def test_stale_position_reaches_the_model_and_marks_via(self):
        self._seed()
        cfg = autotrade.Cfg(dry_run=True, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": False,
                                             "reason": "还没走坏，再拿一会",
                                             "source": "llm"}) as m:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.7, 4000.94),
                                 rows=bars_up(), positions=[])
        m.assert_called_once()
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row.get("via"), "stale")
        self.assertEqual(autotrade.load_state()["position"]["stale_ts"], NOW)

    def test_cooldown_stops_the_next_round_from_asking_again(self):
        """第一轮问过之后，紧接的下一轮必须回到"不调模型"的老节奏。"""
        self._seed()
        cfg = autotrade.Cfg(dry_run=True, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": False, "reason": "拿",
                                             "source": "llm"}):
            autotrade.tick(cfg, now=NOW, quote=(4000.7, 4000.94),
                           rows=bars_up(), positions=[])
        with mock.patch.object(autotrade, "ask_exit") as m2:
            row = autotrade.tick(cfg, now=NOW + 60, quote=(4000.7, 4000.94),
                                 rows=bars_up(), positions=[])
        m2.assert_not_called()
        self.assertNotIn("via", row)
        self.assertEqual(row["source"], "tp_wait")

    def test_fresh_peak_keeps_the_old_quiet_behavior(self):
        """峰值刚抬高（60 秒前）：不唤醒，仍然 tp_wait 且不调模型。"""
        self._seed(peak_ts=NOW - 60)
        cfg = autotrade.Cfg(dry_run=True, tp_min_usd=3.25)
        with mock.patch.object(autotrade, "ask_exit") as m:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.7, 4000.94),
                                 rows=bars_up(), positions=[])
        m.assert_not_called()
        self.assertEqual(row["source"], "tp_wait")
