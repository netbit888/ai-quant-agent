"""平仓记账：三条路必须记出同一份账。

为什么单开一条（2026-11-05）
--------------------------
平仓有三个入口——决策轮自己平的、发现仓位在终端消失（人工 / 保本线被触发）、
面板「停止并清仓」。以前三处各写一遍「加计数 + 拼 close 行」，字段已经分叉：
`peak_pnl`/`peak_ts`/`plan`/`atr` **只有决策轮那条写**。

后果不是报错，是统计静默变形：「这笔 tp 是几倍 ATR」「按 plan 分组」这些口径
会把人工平仓和清仓的单子整批排除——而那些数字正是用来定 tp 参数的。同理
`sl_closes`/`be_closes` 只有决策轮计数，「止损占比」看着比实际低。

这里守两件事：
1. `_book_close` 对任何来源都产出同一套字段（少了就红）；
2. **路径真的在用它**——外部平仓和清仓的行里必须带 plan/atr/peak_pnl。
   第 2 条单独存在的原因：函数写对、路径没接，是两件事。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_up

from live import autotrade

# 三个入口实际会写进来的来源值（见 _exit_gate / 外部平仓判定 / close_all）
SOURCES = ("llm", "sl", "be_local", "be", "external", "panel", "timeout")

COMMON_KEYS = {
    "ts", "kind", "ticket", "direction", "pnl", "p_up", "hit", "held_sec",
    "peak_pnl", "peak_ts", "plan", "atr", "entry_reason",
    "reason", "source", "dry_run",
}


def _pos(**over) -> dict:
    pos = {
        "ticket": 77, "direction": 1, "lots": 0.01, "price_open": 4000.0,
        "opened_at": NOW - 90, "p_up": 0.62, "entry_reason": "突破",
        "plan": "trend", "atr": 1.2, "peak_pnl": 2.5, "peak_ts": NOW - 30,
        "be_ok": False, "dry_run": False,
    }
    pos.update(over)
    return pos


class TestBookCloseShape(unittest.TestCase):
    def test_every_source_writes_the_same_fields(self):
        """字段集合必须与来源无关——这就是抽这个函数的目的。"""
        for src in SOURCES:
            with self.subTest(source=src):
                state = autotrade._empty_state()
                row = autotrade._book_close(state, _pos(), 1.5, NOW,
                                            source=src, reason="测试")
                self.assertEqual(set(row), COMMON_KEYS)
                self.assertEqual(row["source"], src)
                self.assertEqual(row["plan"], "trend")
                self.assertEqual(row["atr"], 1.2)

    def test_counters_and_state_are_updated(self):
        state = autotrade._empty_state()
        autotrade._book_close(state, _pos(), 2.0, NOW, source="llm", reason="平")
        self.assertEqual(state["closed"], 1)
        self.assertEqual(state["wins"], 1)
        self.assertEqual(state["pnl"], 2.0)
        self.assertEqual(state["last_close_ts"], NOW)
        self.assertEqual(state["last_close_pnl"], 2.0)
        self.assertEqual(state["last_close_dir"], 1)
        self.assertIsNone(state["position"])

    def test_loss_is_not_counted_as_win(self):
        state = autotrade._empty_state()
        autotrade._book_close(state, _pos(), -3.0, NOW, source="sl", reason="止损")
        self.assertEqual(state["wins"], 0)
        self.assertEqual(state["pnl"], -3.0)

    def test_source_buckets(self):
        """硬止损 / 保本止损分开计数：入场错了 vs 方向对过没吃到。"""
        state = autotrade._empty_state()
        autotrade._book_close(state, _pos(), -3.0, NOW, source="sl", reason="x")
        self.assertEqual(state["sl_closes"], 1)
        self.assertEqual(state["be_closes"], 0)

        state = autotrade._empty_state()
        autotrade._book_close(state, _pos(), 0.1, NOW, source="be", reason="x")
        self.assertEqual(state["be_closes"], 1)
        self.assertEqual(state["sl_closes"], 0)

        # 模型自己平的 / 面板清仓的：两边都不该计数
        for src in ("llm", "panel", "external"):
            state = autotrade._empty_state()
            autotrade._book_close(state, _pos(), 1.0, NOW, source=src, reason="x")
            self.assertEqual((state["sl_closes"], state["be_closes"]), (0, 0),
                             f"{src} 不该进止损/保本统计")

    def test_held_sec_defaults_to_opened_at(self):
        state = autotrade._empty_state()
        row = autotrade._book_close(state, _pos(opened_at=NOW - 120), 1.0, NOW,
                                    source="llm", reason="x")
        self.assertEqual(row["held_sec"], 120)

    def test_dry_run_follows_position_unless_overridden(self):
        state = autotrade._empty_state()
        row = autotrade._book_close(state, _pos(dry_run=True), 1.0, NOW,
                                    source="llm", reason="x")
        self.assertTrue(row["dry_run"])
        state = autotrade._empty_state()
        row = autotrade._book_close(state, _pos(dry_run=False), 1.0, NOW,
                                    source="llm", reason="x", dry_run=True)
        self.assertTrue(row["dry_run"], "显式传入应覆盖仓位上的记录")


class TestPathsUseIt(TempLedger):
    """路径级：函数写对不等于接上了。这里走真实入口。"""

    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg()

    def test_external_close_row_carries_plan_and_atr(self):
        """外部平仓（人工在终端点的）以前不带 plan/atr/peak_pnl。"""
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=99, opened_at=NOW - 60)
        autotrade.save_state(state)

        row = autotrade.tick(self.cfg, now=NOW, quote=(4005.0, 4005.24),
                             rows=bars_up(), positions=[])

        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "external")
        for key in ("plan", "atr", "peak_pnl", "peak_ts", "dry_run"):
            self.assertIn(key, row, f"外部平仓的 close 行缺 {key}")
        self.assertEqual(row["plan"], "trend")

    def test_panel_close_row_carries_plan_and_atr(self):
        """面板「停止并清仓」的同一笔，也不该在统计里消失。"""
        state = autotrade._empty_state()
        # close_all 用的是真实时钟，开仓时间也得按真实时钟造，否则 held_sec
        # 会变成「1970 到现在的秒数」——测的就不是持仓时长了。
        state["position"] = _pos(ticket=55, dry_run=True,
                                 opened_at=int(time.time()) - 30)
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "_quote", return_value=(4005.0, 4005.24)):
            res = autotrade.close_all(autotrade.Cfg(dry_run=True), why="测试清仓")
        self.assertTrue(res["ok"], res)

        rows = [r for r in autotrade.read_log() if r.get("kind") == "close"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "panel")
        for key in ("plan", "atr", "peak_pnl", "peak_ts", "dry_run"):
            self.assertIn(key, row, f"清仓的 close 行缺 {key}")
        # 不钉死秒数：钉了就是在测"跑用例的那一秒钟"。
        self.assertGreaterEqual(row["held_sec"], 29)
        self.assertLess(row["held_sec"], 120)

    def test_decision_row_still_counts_sl(self):
        """决策轮那条路的计数行为不能被抽函数改掉。"""
        cfg = autotrade.Cfg(sl_usd=3.0)
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=66, direction=1, price_open=4010.0,
                                 opened_at=NOW - 60)
        autotrade.save_state(state)

        # 报价砸到开仓价下方 5 美元：浮亏远超 sl_usd=3.0
        with mock.patch.object(autotrade, "send_close", return_value={"ok": True}):
            row = autotrade.tick(cfg, now=NOW, quote=(4005.0, 4005.24),
                                 rows=bars_up(),
                                 positions=[{"ticket": 66, "symbol": "XAUUSDm"}])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")
        self.assertEqual(autotrade.load_state()["sl_closes"], 1)


if __name__ == "__main__":
    unittest.main()
