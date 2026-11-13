"""上一轮判断回灌：`_skip_note` / `_skip_streak_note` / `_skip_step`。

为什么单独钉这一组
------------------
模型每轮都是全新的（prompt 从零拼，没有历史消息）。等待锚点解决「等回踩被
改写成追」，但**趋势市里锚点整体停用**——于是趋势市连续 act=false 时，
模型每轮都像第一次看盘：上一轮说了什么、之后价格走没走，它不知道。

回灌必须是「结论 + 之后发生了什么」。只回灌结论等于逼它为自洽继续坚持
上一轮（p_up=0.00 那 46% 的样本就是模型自我重复的硬证据），所以下面钉住
「之后价格 … 你没有进场」这一句，不只是钉抬头。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.autotrade import (  # noqa: E402
    SKIP_NOTE_MAX_SEC, SKIP_STREAK_ATR, SKIP_STREAK_MIN,
    build_entry_prompt, _remember_skip, _skip_note, _skip_step,
    _skip_streak_note,
)

SYM = "XAUUSDm"
NOW = 1_700_000_000


def _prev(**kw):
    base = {"ts": NOW - 62, "bid": 4044.0, "p_up": 0.42,
            "reason": "等回踩4044再进"}
    base.update(kw)
    return base


class SkipNoteTest(unittest.TestCase):
    def test_no_record_is_empty(self):
        self.assertEqual(_skip_note(None, 4046.2, 1.0, SYM, NOW), "")
        self.assertEqual(_skip_note({}, 4046.2, 1.0, SYM, NOW), "")

    def test_stale_record_dropped(self):
        old = _prev(ts=NOW - SKIP_NOTE_MAX_SEC - 1)
        self.assertEqual(_skip_note(old, 4046.2, 1.0, SYM, NOW), "")

    def test_edge_of_window_kept(self):
        # 正好 600 秒仍在窗口内（判断用 > 而不是 >=，边界别悄悄丢一轮）
        ok = _prev(ts=NOW - SKIP_NOTE_MAX_SEC)
        self.assertIn("上一轮你的判断", _skip_note(ok, 4046.2, 1.0, SYM, NOW))

    def test_carries_conclusion_and_move(self):
        text = _skip_note(_prev(), 4046.2, 1.0, SYM, NOW)
        self.assertIn("【上一轮你的判断】62 秒前", text)
        self.assertIn("不做", text)
        self.assertIn("p_up=0.42", text)
        self.assertIn("等回踩4044再进", text)
        # 关键：必须带"之后发生了什么"，否则只是自我强化
        self.assertIn("之后价格 4044.000 → 4046.200", text)
        self.assertIn("+2.200", text)
        self.assertIn("≈2.2 倍单根均幅", text)
        self.assertIn("你没有进场", text)

    def test_reason_newline_flattened(self):
        text = _skip_note(_prev(reason="震荡\n上沿不追"), 4046.2, 1.0, SYM, NOW)
        self.assertNotIn("\n\n", text)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_missing_bid_keeps_heading_only(self):
        text = _skip_note(_prev(bid=None), 4046.2, 1.0, SYM, NOW)
        self.assertIn("上一轮你的判断", text)
        self.assertNotIn("之后价格", text)

    def test_missing_p_up_does_not_crash(self):
        text = _skip_note(_prev(p_up=None), 4046.2, 1.0, SYM, NOW)
        self.assertIn("p_up 未给", text)


class SkipStreakTest(unittest.TestCase):
    START = NOW - 600

    def _streak(self, n, price=4032.1):
        return {"n": n, "ts": self.START, "price": price}

    def test_below_threshold_silent(self):
        for n in range(1, SKIP_STREAK_MIN):
            self.assertEqual(
                _skip_streak_note(self._streak(n), 4046.2, 1.0, SYM, NOW), "",
                f"n={n} 不该提「整段错过」")

    def test_flat_market_silent(self):
        # 连续 5 轮但只走了 0.9 倍均幅：那是横盘，说"你错过了"是冤枉它
        self.assertEqual(
            _skip_streak_note(self._streak(5), 4033.0, 1.0, SYM, NOW), "")

    def test_trend_wording_blocks_waiting(self):
        text = _skip_streak_note(self._streak(4), 4046.2, 1.0, SYM, NOW,
                                 regime="trend")
        self.assertIn("【连续未进场】", text)
        self.assertIn("连续 4 轮", text)
        self.assertIn("≈14.1 倍单根均幅", text)
        # 趋势市不许拿位置当理由 —— 措辞要和 system 的【判断原则】一致
        self.assertIn("趋势市没有「等回踩」这个选项", text)
        self.assertIn("形状证据", text)
        self.assertNotIn("不要再把等待价往现价挪", text)

    def test_range_wording_allows_anchor_talk(self):
        text = _skip_streak_note(self._streak(4), 4046.2, 1.0, SYM, NOW,
                                 regime="range")
        self.assertIn("不要再把等待价往现价挪", text)
        self.assertNotIn("趋势市没有", text)

    def test_min_atr_is_respected(self):
        # 恰好等于阈值应当提（判断是 < 才沉默）
        move = SKIP_STREAK_ATR
        text = _skip_streak_note(self._streak(3), 4032.1 + move, 1.0, SYM, NOW)
        self.assertIn("【连续未进场】", text)


class SkipStepTest(unittest.TestCase):
    def test_first_skip_starts_streak(self):
        st = _skip_step(None, False, 4032.1, NOW)
        self.assertEqual(st["n"], 1)
        self.assertEqual(st["ts"], NOW)
        self.assertEqual(st["price"], 4032.1)

    def test_start_price_does_not_drift(self):
        # 起点价必须钉住第一轮：跟着价格挪就算不出"整段"
        st = _skip_step({"n": 1, "ts": NOW - 60, "price": 4032.1}, False,
                        4040.0, NOW)
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["price"], 4032.1)
        self.assertEqual(st["ts"], NOW - 60)
        self.assertEqual(st["last"], NOW)

    def test_acting_clears(self):
        self.assertIsNone(_skip_step({"n": 3, "ts": NOW, "price": 1.0},
                                     True, 4046.2, NOW))

    def test_remember_skip_roundtrip(self):
        state = {}
        _remember_skip(state, False, 4044.0, NOW, 0.42, "等回踩")
        self.assertEqual(state["prev_entry"]["bid"], 4044.0)
        self.assertEqual(state["prev_entry"]["p_up"], 0.42)
        self.assertEqual(state["skip_streak"]["n"], 1)
        _remember_skip(state, False, 4045.0, NOW + 60, 0.44, "还是等")
        self.assertEqual(state["skip_streak"]["n"], 2)
        self.assertEqual(state["prev_entry"]["bid"], 4045.0)
        # 做了 → 两个字段一起清空，别让旧记忆在下一段行情里诈尸
        _remember_skip(state, True, 4046.0, NOW + 120, 0.61, "顺势做")
        self.assertIsNone(state["prev_entry"])
        self.assertIsNone(state["skip_streak"])

    def test_failed_call_does_not_touch_memory(self):
        # 调用失败不是"它的判断"：既不覆盖上一轮结论，也不推进连续计数
        state = {}
        _remember_skip(state, False, 4044.0, NOW, 0.42, "等回踩")
        before = (dict(state["prev_entry"]), dict(state["skip_streak"]))
        _remember_skip(state, False, 4046.2, NOW + 60, 0.5,
                       "模型调用失败（TimeoutError）", ok=False)
        self.assertEqual(state["prev_entry"], before[0])
        self.assertEqual(state["skip_streak"], before[1])


class PromptWiringTest(unittest.TestCase):
    """块要真的进 prompt，而且位置在教训库之前（先看发生了什么，再看经验）。"""

    def _build(self, **kw):
        rows = [{"time": NOW - i * 60, "open": 4040.0 + i, "high": 4042.0 + i,
                 "low": 4039.0 + i, "close": 4041.0 + i}
                for i in range(30, 0, -1)]
        return build_entry_prompt(rows, 4046.0, 4046.24, SYM, "M1", 0.001,
                                  {"closed": 10, "wins": 4},
                                  lessons="【教训】某条", **kw)

    def test_block_present_before_lessons(self):
        note = ("【连续未进场】你已经连续 4 轮判「不做」…\n"
                "【上一轮你的判断】62 秒前 · 不做（p_up=0.42）\n")
        text = self._build(skip_note=note)
        self.assertIn("【连续未进场】", text)
        self.assertLess(text.index("【上一轮你的判断】"), text.index("【教训】"))

    def test_default_has_no_orphan(self):
        text = self._build()
        self.assertNotIn("上一轮你的判断", text)
        self.assertNotIn("连续未进场", text)
        # 不留空行：块的占位符是紧贴的 {recent_close}{wait_note}{skip_note}
        self.assertNotIn("\n\n战绩", text)


if __name__ == "__main__":
    unittest.main()
