"""`tools/lesson_stats.py` 的口径测试：场次切分、归一化、趋势判定。

这个工具的输出会被人当成「经验到底有没有用」的证据，所以口径必须钉住：
- 场次按 closed_at 断场（间隔 > gap 分钟）；
- 尾部不补 0（否则趋势会假收敛）；
- 平仓笔数太少的场次不给归一化（1 笔里犯 1 次不是 10.0/10 笔）。
"""

from __future__ import annotations

import tempfile
import unittest

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths
from tools import lesson_stats

NOW = 1_790_000_000


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()

    def lesson(self, text, closed_at, theme="追破位/假突破", scope="entry"):
        autotrade.append_lesson({"ts": closed_at + 420, "closed_at": closed_at,
                                 "ticket": closed_at, "lesson": text,
                                 "theme": theme, "scope": scope, "regime": "range"})

    def close(self, ts):
        autotrade.append_log({"ts": ts, "kind": "close", "pnl": 0.0})


class TestSessions(Base):
    def test_split_on_gap(self):
        for i in range(3):
            self.lesson(f"追破位第{i}次", NOW + i * 60)
        self.lesson("第二场追破位", NOW + 3 * 3600)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual([s["n"] for s in rep["sessions"]], [3, 1])

    def test_gap_boundary_is_strict(self):
        """正好 30 分钟算同一场（口径是「超过」才断）。"""
        self.lesson("追破位第1次", NOW)
        self.lesson("追破位第2次", NOW + 30 * 60)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual(len(rep["sessions"]), 1)


class TestSeries(Base):
    def test_no_trailing_zeros(self):
        """只在首次~末次之间补 0，尾部不补：尾部 0 会把趋势读成收敛。"""
        self.lesson("追破位第1次", NOW)
        self.lesson("追破位第2次", NOW + 3600 * 5)
        rep = lesson_stats.build_report(None, gap_min=30)
        t = rep["themes"][0]
        self.assertEqual(len(t["per_session"]), 2)
        self.assertEqual(t["per_session"], [1, 1])

    def test_middle_gap_is_filled(self):
        """中间那场这个主题没犯，要补 0（而不是把两场挤在一起）。"""
        self.lesson("追破位第1次", NOW)
        self.lesson("逆势接刀一次", NOW + 3600 * 5, theme="逆势抢反弹/接刀")
        self.lesson("追破位第2次", NOW + 3600 * 10)
        rep = lesson_stats.build_report(None, gap_min=30)
        t = [x for x in rep["themes"] if x["theme"] == "追破位/假突破"][0]
        self.assertEqual(len(rep["sessions"]), 3)
        self.assertEqual(t["per_session"], [1, 0, 1])


class TestNormalization(Base):
    def test_norm_skipped_for_tiny_sessions(self):
        """平仓 2 笔的场次不给「每 10 笔」：小样本会算出 5.0 这种假数字。"""
        self.lesson("追破位第1次", NOW)
        self.close(NOW)
        self.close(NOW + 60)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual(rep["sessions"][0]["closes"], 2)
        self.assertEqual(rep["themes"][0]["per_10_closes"], [None])

    def test_norm_uses_close_count(self):
        self.lesson("追破位第1次", NOW)
        for i in range(9):
            self.close(NOW + i)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual(rep["sessions"][0]["closes"], 9)
        self.assertEqual(rep["themes"][0]["per_10_closes"], [1.1])

    def test_trend_converges(self):
        """前场犯 6 次、后场犯 1 次 → 收敛。"""
        for i in range(6):
            self.lesson(f"追破位第{i}次", NOW + i)
        for i in range(20):
            self.close(NOW + i)
        self.lesson("追破位第7次", NOW + 7200)
        for i in range(20):
            self.close(NOW + 7200 + i)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual(rep["themes"][0]["trend"], "收敛")

    def test_trend_worsens(self):
        self.lesson("追破位第1次", NOW)
        for i in range(20):
            self.close(NOW + i)
        for i in range(6):
            self.lesson(f"追破位第{i}次", NOW + 7200 + i)
        for i in range(20):
            self.close(NOW + 7200 + i)
        rep = lesson_stats.build_report(None, gap_min=30)
        self.assertEqual(rep["themes"][0]["trend"], "恶化")


class TestEmpty(Base):
    def test_empty_library_says_so(self):
        rep = lesson_stats.build_report(None)
        self.assertEqual(rep["lessons"], 0)
        self.assertIn("空", rep["note"])
        self.assertIn("空", lesson_stats.render(rep))
