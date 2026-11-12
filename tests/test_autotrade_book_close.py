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

    def test_exit_source_is_stamped_on_position(self):
        """平仓来源必须落回 pos：`queue_review` 靠它决定教训归出场端还是两端。

        2026-11-10 那笔就漏在这里：券商端保本止损触发，source 算成了 "be"，
        但没人写回 pos，复盘拿到的 exit_source 是空 → scope 停在 exit、
        src 为空 → 「这个位置当初就不该开」永远进不了入场提示词。
        """
        for src in SOURCES:
            with self.subTest(source=src):
                pos = _pos()
                autotrade._book_close(autotrade._empty_state(), pos, 0.0, NOW,
                                      source=src, reason="x")
                self.assertEqual(pos["exit_source"], src)

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

    def test_be_close_queues_review_with_source(self):
        """保本止损在终端被打掉 → 复盘队列里要带着 exit_source="be"。"""
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=98, be_ok=True, be_sl=4000.0,
                                 opened_at=NOW - 60)
        autotrade.save_state(state)

        row = autotrade.tick(autotrade.Cfg(review=True), now=NOW,
                             quote=(4005.0, 4005.24), rows=bars_up(),
                             positions=[])
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "be")
        pend = autotrade.load_state()["pending_reviews"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["exit_source"], "be")


class TestGoneSource(unittest.TestCase):
    """仓位在终端消失时，这笔平仓算谁的。

    这条判定以前只写在 `tick` 里；面板清仓那条路没有，于是「用户在终端手动
    平掉、再点面板停止并清仓」这个组合根本没人记账（2026-11-12 实盘）。
    现在两条路共用这一个函数，判据只有一个：券商成交回执的 `DEAL_REASON_*`。
    """

    def test_tp_reason_5(self):
        src, why = autotrade._gone_source(_pos(tp_broker=4003.0), 5)
        self.assertEqual(src, "tp")
        self.assertIn("4003.000", why)

    def test_reason_5_without_broker_tp_is_external(self):
        """没挂过券商端 TP 却收到 reason=5：不认，保守记人工。"""
        src, _ = autotrade._gone_source(_pos(tp_broker=0.0), 5)
        self.assertEqual(src, "external")

    def test_be_wins_even_without_receipt(self):
        """保本线被打出来的是 profit 0.00，回执常还没落——读不到也认 be。"""
        src, _ = autotrade._gone_source(_pos(be_ok=True, be_sl=4000.0), None)
        self.assertEqual(src, "be")

    def test_sl_needs_reason_4(self):
        """挂了止损不等于止损被打：人手动平了也会让仓位消失。"""
        self.assertEqual(
            autotrade._gone_source(_pos(sl_broker=3997.0), 4)[0], "sl")
        self.assertEqual(
            autotrade._gone_source(_pos(sl_broker=3997.0), None)[0], "external")
        self.assertEqual(
            autotrade._gone_source(_pos(sl_broker=3997.0), 0)[0], "external")


class TestFlattenAfterManualClose(TempLedger):
    """「你先在终端平了，再点面板停止并清仓」——这笔必须入账。

    2026-11-12 实盘：手动平仓 → 面板停止 → 清仓时账户上已经没这笔仓，
    `done` 为空 → 旧代码什么都不做 → 仓位卡在 state 里当幽灵：pnl 不计、
    closed 不加、重启第一眼看到"还有仓"、复盘也排不进去。
    """

    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg()

    def _close_all_with(self, positions):
        with mock.patch.object(autotrade, "_all_positions", return_value=positions), \
             mock.patch.object(autotrade, "_close_fills",
                               return_value={"pnl": 12.5, "price": 4008.0,
                                             "reason": 0,
                                             "time": int(time.time())}), \
             mock.patch.object(autotrade, "_quote",
                               return_value=(4008.0, 4008.24)):
            return autotrade.close_all(self.cfg, why="面板停止并清仓")

    def test_already_closed_is_booked_as_external(self):
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=4321, opened_at=int(time.time()) - 40)
        autotrade.save_state(state)

        res = self._close_all_with([])

        rows = [r for r in autotrade.read_log() if r.get("kind") == "close"]
        self.assertEqual(len(rows), 1, "平仓必须落一条 close 行")
        row = rows[0]
        self.assertEqual(row["source"], "external")
        self.assertEqual(row["pnl"], 12.5)
        self.assertEqual(row["pnl_src"], "deal", "有成交回执就不能按报价估")
        for key in ("plan", "atr", "peak_pnl", "peak_ts"):
            self.assertIn(key, row, f"补记的 close 行缺 {key}")

        st = autotrade.load_state()
        self.assertIsNone(st["position"], "仓位不能在 state 里留幽灵")
        self.assertEqual(st["pnl"], 12.5)
        self.assertEqual(st["closed"], 1)
        self.assertTrue(any(d.get("external") for d in res["closed"]))
        # 面板上不能显示成"平掉 1 笔"——那笔不是这次平的，只是把账补上
        self.assertIn("早已不在账户上", res.get("note") or "")

    def test_broker_tp_hit_while_flattening_is_booked_as_tp(self):
        """同一格也可能是券商端 TP 先成交了——来源不能一律算人工。"""
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=4322, tp_broker=4003.0,
                                 opened_at=int(time.time()) - 40)
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "_all_positions", return_value=[]), \
             mock.patch.object(autotrade, "_close_fills",
                               return_value={"pnl": 16.3, "price": 4003.0,
                                             "reason": 5,
                                             "time": int(time.time())}), \
             mock.patch.object(autotrade, "_quote",
                               return_value=(4003.0, 4003.24)):
            autotrade.close_all(self.cfg, why="面板停止并清仓")

        rows = [r for r in autotrade.read_log() if r.get("kind") == "close"]
        self.assertEqual(rows[0]["source"], "tp")
        self.assertEqual(autotrade.load_state()["position"], None)

    def test_unknown_account_leaves_position_alone(self):
        """读不到持仓 = 不知道，不能当"已经平了"记一笔不存在的盈亏。"""
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=4323, opened_at=int(time.time()) - 40)
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "_all_positions", return_value=None):
            res = autotrade.close_all(self.cfg, why="面板停止并清仓")

        self.assertFalse(res["ok"])
        rows = [r for r in autotrade.read_log() if r.get("kind") == "close"]
        self.assertEqual(rows, [])
        self.assertEqual(autotrade.load_state()["position"]["ticket"], 4323)


if __name__ == "__main__":
    unittest.main()
