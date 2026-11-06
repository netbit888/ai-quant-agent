"""SQL 查账本（tools/query.py）：视图能建、预设口径对。

口径类工具最怕"数字看着对、其实算错"——所以这里不只测能跑，
还拿一组手写的三笔单去核对 `plans` / `sources` / `giveback` 的具体数值。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import query  # noqa: E402

from live import autotrade  # noqa: E402
from tests._autotrade_base import NOW, TempLedger  # noqa: E402

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


@unittest.skipUnless(HAS_DUCKDB, "没装 duckdb（requirements-dev.txt）")
class TestQuery(TempLedger):
    def setUp(self):
        super().setUp()
        rows = [
            # 两笔震荡：+2.0（峰值 3.0）、-1.0（峰值 -0.5）
            {"ts": NOW, "kind": "open", "ticket": 1, "plan": "range",
             "feat": {"er": 0.2, "mtf": {"drift": 8.0}}},
            {"ts": NOW + 10, "kind": "close", "ticket": 1, "plan": "range",
             "pnl": 2.0, "peak_pnl": 3.0, "source": "llm"},
            {"ts": NOW + 20, "kind": "open", "ticket": 2, "plan": "range",
             "feat": {"er": 0.1, "mtf": {"drift": -9.0}}},
            {"ts": NOW + 30, "kind": "close", "ticket": 2, "plan": "range",
             "pnl": -1.0, "peak_pnl": -0.5, "source": "sl"},
            # 一笔趋势：0.0（峰值 4.88），M5 与方向相反
            {"ts": NOW + 40, "kind": "open", "ticket": 3, "plan": "trend",
             "direction": -1, "feat": {"er": 0.6, "mtf": {"drift": 11.0}}},
            {"ts": NOW + 50, "kind": "close", "ticket": 3, "plan": "trend",
             "pnl": 0.0, "peak_pnl": 4.88, "source": "be"},
        ]
        for r in rows:
            autotrade.append_log(r, None)

    def _preset(self, name):
        con = query.connect()
        return query.run(con, query.PRESETS[name][1])

    def test_plans_aggregates(self):
        cols, rows = self._preset("plans")
        got = {r[0]: dict(zip(cols, r)) for r in rows}
        self.assertEqual(got["range"]["n"], 2)
        self.assertAlmostEqual(got["range"]["total"], 1.0, places=2)
        self.assertAlmostEqual(got["range"]["win_pct"], 50.0, places=1)
        self.assertEqual(got["trend"]["n"], 1)

    def test_zero_pnl_is_not_dropped(self):
        """0.00 是保本止损，不许被当成空值过滤掉（今天第三次遇到同一个坑）。"""
        cols, rows = self._preset("sources")
        got = {r[0]: dict(zip(cols, r)) for r in rows}
        self.assertIn("be", got)
        self.assertEqual(got["be"]["n"], 1)
        self.assertAlmostEqual(got["be"]["total"], 0.0, places=2)

    def test_giveback_sorts_by_giveback(self):
        cols, rows = self._preset("giveback")
        self.assertEqual(len(rows), 3)
        first = dict(zip(cols, rows[0]))
        self.assertEqual(first["ticket"], 3)
        self.assertAlmostEqual(first["giveback"], 4.88, places=2)

    def test_conflict_classification(self):
        cols, rows = self._preset("conflict")
        got = {(r[0], r[1]): dict(zip(cols, r)) for r in rows}
        self.assertIn(("trend", "冲突"), got)       # 做空 + M5 +11
        self.assertIn(("range", "同向"), got)       # 做多 + M5 +8

    def test_unknown_sql_raises_clean(self):
        con = query.connect()
        with self.assertRaises(Exception):
            query.run(con, "SELECT * FROM 不存在的表")

    def test_render_counts_cjk_width(self):
        text = query._render(["打法", "n"], [["趋势", 2]])
        self.assertIn("趋势", text)
        # 表头与数据两行等宽（中文按两格算，否则会歪）
        lines = text.splitlines()
        self.assertEqual(len(lines[0]), len(lines[2]))


if __name__ == "__main__":
    unittest.main()
