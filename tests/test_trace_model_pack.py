"""开仓行要记住「这一笔是谁判的」（2026-11-18）。

背景：模型和经验包都是**运行中热生效**的（`/api/llm/switch` 改进程内
os.environ，教训是每轮现读 `active_file()`）。流水里不留这两个字段，事后
复盘就只能看到「模型判断做多」，分不清这一周变差是换了模型、换了一套经验，
还是行情本身变了——三种归因对应完全不同的处置，而流水里一个字都没有。

留痕字段的硬约束：**取不到就写空串，不许编**。编一个名字会让归因彻底错掉，
比缺字段更糟。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import NOW, TempLedger, bars_up


class TestTraceHelpers(unittest.TestCase):
    """两个取值函数本身的边界：热切换读的是 env，读不到是空串。"""

    def test_model_name_reads_env(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "glm-4-flash"}):
            self.assertEqual(autotrade._model_name(), "glm-4-flash")

    def test_model_name_strips(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "  qwen3-8b  "}):
            self.assertEqual(autotrade._model_name(), "qwen3-8b")

    def test_pack_id_from_lesson_packs(self):
        with mock.patch("live.lesson_packs.active_id", return_value="p-123"):
            self.assertEqual(autotrade._active_pack_id(), "p-123")

    def test_pack_id_empty_when_no_pack(self):
        """一个包都没有时 active 是 None —— 落盘要是空串，不是字符串 "None"。"""
        with mock.patch("live.lesson_packs.active_id", return_value=None):
            self.assertEqual(autotrade._active_pack_id(), "")

    def test_pack_id_swallows_errors(self):
        """索引文件坏了不该让开仓失败：留痕是附属信息，不是前置条件。"""
        with mock.patch("live.lesson_packs.active_id", side_effect=OSError("boom")):
            self.assertEqual(autotrade._active_pack_id(), "")


class TestOpenRowTrace(TempLedger):
    """真正跑一轮 tick 到开仓，断言字段落在流水行上（而不是只有函数能算）。"""

    def setUp(self):
        super().setUp()
        # 与 tests/test_autotrade_tick.py::TestTick 同参：关掉入场闸门，
        # 因为这条用例验证的是落盘字段，不是入场规则（bars_up 是单调上涨，
        # 默认会撞「贴上沿禁多」）。
        self.cfg = autotrade.Cfg(cooldown_sec=30, interval_sec=60, dry_run=False,
                                 act_guard=False, top_long_ban=False)

    def _open_row(self):
        with mock.patch.object(
                autotrade, "ask_entry",
                return_value={"p_up": 0.7, "reason": "突破", "source": "llm"}), \
             mock.patch.object(
                autotrade, "send_open",
                return_value={"ok": True, "price": 4001.2, "order": 555}):
            return autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                  rows=bars_up(), positions=[])

    def test_open_row_carries_model_and_pack(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "glm-4-flash"}), \
                mock.patch("live.lesson_packs.active_id", return_value="p-42"):
            row = self._open_row()

        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["model"], "glm-4-flash")
        self.assertEqual(row["pack"], "p-42")

    def test_open_row_trace_survives_missing_pack(self):
        """没有经验包（或索引坏了）时，开仓照常，字段是空串。"""
        with mock.patch.dict(os.environ, {"LLM_MODEL": "glm-4-flash"}), \
                mock.patch("live.lesson_packs.active_id", side_effect=OSError("no index")):
            row = self._open_row()

        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["pack"], "")


if __name__ == "__main__":
    unittest.main()
