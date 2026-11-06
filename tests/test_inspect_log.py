"""账本浏览器（tools/inspect_log.py）：摊平与过滤。

钉的不是"能跑"，而是两次踩过的坑：
  1. `pnl = 0.00`（保本止损的正常结果）不能被 `or ""` 吞成空白；
  2. 列不能按第一行裁——第一行是 open 行，没有 pnl/exit_price，
     按它定表头会把 close 行独有的字段整批丢掉（导出的 CSV 里没有 pnl 列）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import inspect_log  # noqa: E402

from live import autotrade  # noqa: E402
from tests._autotrade_base import NOW, TempLedger  # noqa: E402


class TestFlatten(unittest.TestCase):
    def test_two_levels_are_flattened(self):
        """`feat.mtf.drift` 要变成一列——按 M5 方向分组看结果靠它。"""
        flat = inspect_log._flatten(
            {"kind": "open", "feat": {"er": 0.59, "mtf": {"drift": 11.79}}})
        self.assertEqual(flat["feat_er"], 0.59)
        self.assertEqual(flat["feat_mtf_drift"], 11.79)
        self.assertNotIn("feat", flat)

    def test_list_is_kept_as_json_text(self):
        """`recent` 这种列表展开成列会让列数爆炸，保留成文本。"""
        flat = inspect_log._flatten({"recent": [{"pnl": 1.0}]})
        self.assertIsInstance(flat["recent"], str)
        self.assertIn("pnl", flat["recent"])

    def test_empty_list_is_empty_string(self):
        self.assertEqual(inspect_log._flatten({"win": []})["win"], "")


class TestRecords(TempLedger):
    def test_zero_pnl_survives(self):
        """保本止损的 0.00 是结论，不是空值。"""
        recs = inspect_log.to_records([{"kind": "close", "pnl": 0.0}])
        self.assertEqual(recs[0]["pnl"], 0.0)
        self.assertNotEqual(recs[0]["pnl"], "")

    def test_columns_are_union_of_all_rows(self):
        """open 行 + close 行：两边独有的字段都要留下。"""
        rows = [{"kind": "open", "ticket": 1, "feat": {"er": 0.6}},
                {"kind": "close", "ticket": 1, "pnl": 0.0,
                 "exit_price": 4076.756}]
        recs = inspect_log.to_records(rows)
        for key in ("feat_er", "pnl", "exit_price", "ticket"):
            self.assertIn(key, recs[0], f"列并集里缺 {key}")
            self.assertIn(key, recs[1], f"列并集里缺 {key}")
        self.assertEqual(recs[1]["pnl"], 0.0)

    def test_head_columns_come_first(self):
        recs = inspect_log.to_records([{"kind": "close", "pnl": 1.0,
                                        "aaa_noise": 1}])
        keys = list(recs[0])
        self.assertLess(keys.index("kind"), keys.index("aaa_noise"))

    def test_time_is_local_and_formatted(self):
        recs = inspect_log.to_records([{"ts": NOW}])
        self.assertRegex(recs[0]["time"], r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestLoadRows(TempLedger):
    def setUp(self):
        super().setUp()
        autotrade.append_log({"ts": NOW, "kind": "open", "ticket": 7,
                              "feat": {"ok": True}}, None)
        autotrade.append_log({"ts": NOW + 60, "kind": "close", "ticket": 7,
                              "pnl": 0.0}, None)
        autotrade.append_log({"ts": NOW + 120, "kind": "close", "ticket": 8,
                              "pnl": -3.11}, None)

    def test_kind_filter(self):
        rows = inspect_log.load_rows(kind="close")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "close" for r in rows))

    def test_ticket_filter(self):
        rows = inspect_log.load_rows(ticket=7)
        self.assertEqual({r["kind"] for r in rows}, {"open", "close"})

    def test_broken_line_is_skipped(self):
        p = Path(autotrade.log_path())
        with p.open("a", encoding="utf-8") as fh:
            fh.write("{半行坏数据\n")
        rows = inspect_log.load_rows(kind="close")
        self.assertEqual(len(rows), 2, "坏行应该跳过，好行要留下")

    def test_csv_keeps_close_only_columns(self):
        rows = inspect_log.load_rows(ticket=7)
        recs = inspect_log.to_records(rows)
        dest = Path(self._tmp.name) / "out.csv"
        inspect_log._write_csv(recs, dest)
        import csv
        with dest.open(encoding="utf-8-sig") as fh:
            got = list(csv.DictReader(fh))
        self.assertEqual(len(got), 2)
        self.assertIn("pnl", got[0], "CSV 表头缺 pnl（按第一行裁的老毛病）")
        self.assertEqual(got[-1]["pnl"], "0.0")


if __name__ == "__main__":
    unittest.main()
