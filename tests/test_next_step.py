"""建议器的口径：统计怎么算，以及「什么时候不该给建议」。

这个工具最危险的输出不是算错数，而是**在证据不足时给出一个像模像样的结论**。
所以测试分两类：
  1. 口径类：magic 过滤、只算平仓、成本计入、t 值公式——算错就全盘皆错；
  2. 纪律类：样本不足必须判 SAMPLE_DEFICIT、动作必须是「不要改参数」；
     账户选择必须取「最近活跃」而不是「笔数最多」（后者反映的是旧代码）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import next_step as N  # noqa: E402


def _deals(rows: list[tuple[int, int, float]], base_ts: int = 1_700_000_000) -> list[dict]:
    """(entry, magic, profit) -> deals 行。"""
    out = []
    for i, (entry, magic, profit) in enumerate(rows):
        out.append({"entry": entry, "magic": magic, "profit": profit,
                    "time": base_ts + i, "ticket": i})
    return out


class TestPnlExtraction(unittest.TestCase):
    def test_only_closing_deals_counted(self):
        """开仓行的 profit 恒为 0；算进去会让笔数翻倍、期望腰斩。"""
        rows = N._closed_pnls(_deals([(0, 1, 0.0), (1, 1, 2.0), (0, 1, 0.0), (1, 1, -1.0)]), 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual([p for _, p in rows], [2.0, -1.0])

    def test_magic_filter_excludes_other_ea(self):
        """账户里有别的 EA 和手工单（magic=0），不筛就是拿别人的战绩下结论。"""
        rows = N._closed_pnls(_deals([(1, 20261022, 1.0), (1, 0, 99.0), (1, 111, 50.0)]),
                              20261022)
        self.assertEqual([p for _, p in rows], [1.0])

    def test_magic_none_keeps_everything(self):
        rows = N._closed_pnls(_deals([(1, 20261022, 1.0), (1, 0, 99.0)]), None)
        self.assertEqual(len(rows), 2)

    def test_costs_are_included(self):
        """0.05 手一个来回的手续费可能比利润还大，漏了就是系统性高估。"""
        d = [{"entry": 1, "magic": 1, "profit": 2.0,
              "commission": -0.5, "swap": -0.2, "fee": -0.1, "time": 1}]
        self.assertAlmostEqual(N._closed_pnls(d, 1)[0][1], 1.2)

    def test_sorted_by_time(self):
        d = [{"entry": 1, "magic": 1, "profit": 1.0, "time": 300},
             {"entry": 1, "magic": 1, "profit": 2.0, "time": 100}]
        self.assertEqual([t for t, _ in N._closed_pnls(d, 1)], [100, 300])


class TestStats(unittest.TestCase):
    def test_t_value_matches_hand_calc(self):
        """[1..5]：mean=3, std=sqrt(2.5), se=std/sqrt(5) → t≈4.243。"""
        s = N._stats([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(s.mean, 3.0)
        self.assertAlmostEqual(s.t, 4.243, places=2)

    def test_zero_variance_does_not_divide(self):
        s = N._stats([2.0, 2.0, 2.0])
        self.assertEqual(s.t, 0.0)

    def test_win_rate(self):
        s = N._stats([1.0, -1.0, 1.0, -1.0])
        self.assertAlmostEqual(s.win_rate, 0.5)
        self.assertAlmostEqual(s.net, 0.0)


class TestStage(unittest.TestCase):
    def test_below_threshold_is_deficit(self):
        self.assertEqual(N._stage_of(N._stats([1.0] * (N.SAMPLE_MIN - 1))),
                         N.SAMPLE_DEFICIT)

    def test_significant_negative(self):
        s = N._stats([-1.0] * 25 + [1.0] * 5)
        self.assertEqual(N._stage_of(s), N.NEG_EDGE)

    def test_significant_positive(self):
        s = N._stats([1.0] * 25 + [-1.0] * 5)
        self.assertEqual(N._stage_of(s), N.POS_EDGE)

    def test_noise_stays_inconclusive(self):
        """均值为 0 的对称序列，样本再多也不是 edge。"""
        s = N._stats([1.0, -1.0] * 20)
        self.assertEqual(N._stage_of(s), N.INCONCLUSIVE)


class TestAccountChoice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Path(self.tmp.name) / "accounts"
        self.accounts.mkdir()
        self._patch = mock.patch.object(N.paths, "ACCOUNTS_DIR", self.accounts)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _write(self, login: str, rows: list[dict]) -> None:
        d = self.accounts / login
        d.mkdir()
        (d / "deals.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_prefers_most_recent_not_most_numerous(self):
        """旧账户 100 笔、新账户 2 笔——必须选新的：旧笔数反映的是旧代码。"""
        self._write("111", _deals([(1, 1, 1.0)] * 100, base_ts=1_700_000_000))
        self._write("222", _deals([(1, 1, 1.0)] * 2, base_ts=1_800_000_000))
        login, rows = N._pick_account(1, None)
        self.assertEqual(login, "222")
        self.assertEqual(len(rows), 2)

    def test_account_without_matching_magic_is_skipped(self):
        self._write("111", _deals([(1, 0, 1.0)] * 5))
        login, rows = N._pick_account(20261022, None)
        self.assertEqual(login, "")
        self.assertEqual(rows, [])


class TestBuild(unittest.TestCase):
    """端到端：样本不足时必须明确说「别动」，而不是给一堆可选项。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Path(self.tmp.name) / "accounts"
        self.accounts.mkdir()
        self.patches = [
            mock.patch.object(N.paths, "ACCOUNTS_DIR", self.accounts),
            mock.patch.object(N, "_blockers", return_value=[]),
            mock.patch.object(N, "_lesson_signal", return_value=[]),
            mock.patch.object(N, "_version_compare", return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write(self, login: str, rows: list[dict]) -> None:
        d = self.accounts / login
        d.mkdir()
        (d / "deals.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_deficit_says_do_not_tune(self):
        self._write("111", _deals([(1, 7, 1.0)] * 5))
        a = N.build(magic=7)
        self.assertEqual(a.stage, N.SAMPLE_DEFICIT)
        self.assertIn("不要改参数", a.action)
        self.assertIn("5", a.headline)

    def test_no_deals_is_not_a_crash(self):
        a = N.build(magic=7)
        self.assertEqual(a.stage, N.SAMPLE_DEFICIT)
        self.assertIn("没有", a.headline)
        self.assertTrue(a.action)

    def test_positive_edge_points_to_validation(self):
        self._write("111", _deals([(1, 7, 1.0)] * 25 + [(1, 7, -1.0)] * 5))
        a = N.build(magic=7)
        self.assertEqual(a.stage, N.POS_EDGE)
        self.assertIn("样本外", a.action)


if __name__ == "__main__":
    unittest.main()
