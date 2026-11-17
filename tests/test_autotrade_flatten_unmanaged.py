"""「停止并清仓」读不到持仓时会发生什么。

为什么单开一条（2026-11-17 实盘）
--------------------------------
11:10 开多 0.1 手 @3873.022（券商端止损 3870.022），11:21 点「关闭并清仓」。
那一刻 `positions_get()` 返回 None，`close_all` 按设计"读不到就不动"：置
`running=false`、落一条 `flatten` 日志、返回 error。于是**一笔都没平**，
仓位留在券商端，只剩那条固定止损兜着，直到 22:05 人工发现才平——脱管
10 小时 44 分。

这不是崩溃，是设计上"宁可不动也不猜"的必然结果。所以修的不是"要不要猜"，
而是三件更基本的事：
1. 一次失败就放弃太脆。读不到多半是瞬时态（连接期闸门 `MT5Busy`、终端刚
   重连），重试两秒换一个"真平掉了"值得。
2. 失败原因不能吞。旧日志只写"失败：读不到持仓"，连是忙还是掉线都分不清，
   现场没法排查。
3. 提示必须**常驻**。旧实现只有 `atSay` 一次性提示，下一轮刷新就盖掉，
   用户以为平完了。现在写 `state["unmanaged"]`，面板吸顶带一直亮，直到
   人去终端确认过点掉（`ack_unmanaged`）。

守的边界同样重要：读不到持仓时**绝不能**当成"已经平了"去记账——那会记出
一笔不存在的盈亏，还把仓位从 state 里抹掉。这条在 test_autotrade_book_close
里已有，这里再钉一次。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from tests._autotrade_base import TempLedger

from live import autotrade


def _pos(ticket=5555, **over):
    pos = {
        "ticket": ticket, "direction": 1, "lots": 0.01, "price_open": 4000.0,
        "opened_at": int(time.time()) - 120, "p_up": 0.62,
        "entry_reason": "突破", "plan": "trend", "atr": 1.2,
        "peak_pnl": 1.0, "peak_ts": int(time.time()) - 60,
        "be_ok": False, "dry_run": False,
    }
    pos.update(over)
    return pos


class FlattenCase(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg()
        # 重试间隔在测试里归零：这条逻辑要验的是"有没有重试"，不是"等了多久"。
        self._gap = mock.patch.object(autotrade, "_FLATTEN_READ_GAP", 0)
        self._gap.start()
        self.addCleanup(self._gap.stop)

    def _seed_position(self, ticket=5555):
        state = autotrade._empty_state()
        state["position"] = _pos(ticket=ticket)
        autotrade.save_state(state)
        return state

    def _flatten_rows(self):
        return [r for r in autotrade.read_log() if r.get("kind") == "flatten"]

    def _close_rows(self):
        return [r for r in autotrade.read_log() if r.get("kind") == "close"]


class TestReadFailure(FlattenCase):
    def test_marks_unmanaged_and_keeps_position(self):
        """三次都读不到 → 标记落盘、仓位原样留着、日志带原因与重试次数。"""
        self._seed_position()
        with mock.patch.object(autotrade, "_all_positions", return_value=None):
            res = autotrade.close_all(self.cfg, why="面板停止并清仓")

        self.assertFalse(res["ok"])
        self.assertTrue(res.get("unmanaged"))
        self.assertEqual(res["tries"], 1 + autotrade._FLATTEN_READ_RETRIES)

        st = autotrade.load_state()
        self.assertFalse(st["running"])
        mark = st.get("unmanaged")
        self.assertIsInstance(mark, dict, "失败必须留常驻标记，不能只落一条日志")
        self.assertEqual(mark["why"], "面板停止并清仓")
        self.assertEqual(mark["tries"], 1 + autotrade._FLATTEN_READ_RETRIES)
        self.assertIn("error", mark, "原因必须留下来，否则现场没法排查")
        self.assertEqual(st["position"]["ticket"], 5555, "不知道就别动仓位")

        self.assertEqual(self._close_rows(), [], "读不到持仓不能记平仓")
        rows = self._flatten_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("失败", rows[0]["reason"])
        self.assertIn("tries", rows[0])

    def test_retry_recovers_instead_of_giving_up(self):
        """第一次读不到、第二次读到 → 正常平掉，不该留任何未确认标记。"""
        self._seed_position(ticket=6666)
        seq = iter([None, [{"ticket": 6666, "symbol": "XAUUSDc",
                            "direction": 1, "volume": 0.01}]])
        with mock.patch.object(autotrade, "_all_positions",
                               side_effect=lambda: next(seq, [])), \
             mock.patch.object(autotrade, "send_close",
                               return_value={"ok": True}), \
             mock.patch.object(autotrade, "_close_fills",
                               return_value={"pnl": 5.0, "price": 4005.0,
                                             "reason": 0,
                                             "time": int(time.time())}), \
             mock.patch.object(autotrade, "_quote", return_value=(4005.0, 4005.24)):
            res = autotrade.close_all(self.cfg, why="面板停止并清仓")

        self.assertTrue(res["ok"], res)
        self.assertNotIn("unmanaged", autotrade.load_state())
        self.assertEqual(len(self._close_rows()), 1, "重试成功后必须真平掉")

    def test_unmanaged_reason_survives_when_wrapped(self):
        """异常被 `_all_positions` 吞掉时，原因要能从日志里读回来。"""
        autotrade._last_pos_err = ""
        with mock.patch("live.journal.open_positions",
                        side_effect=RuntimeError("MT5 正在连接，稍后重试")):
            rows = autotrade._all_positions()
        self.assertIsNone(rows)
        self.assertIn("MT5 正在连接", autotrade._last_pos_err)


class TestAck(FlattenCase):
    def test_ack_clears_mark(self):
        self._seed_position()
        with mock.patch.object(autotrade, "_all_positions", return_value=None):
            autotrade.close_all(self.cfg, why="面板停止并清仓")
        self.assertIn("unmanaged", autotrade.load_state())

        res = autotrade.ack_unmanaged()
        self.assertTrue(res["cleared"])
        self.assertNotIn("unmanaged", autotrade.load_state())
        # 幂等：第二次点没东西可清，也不报错
        self.assertFalse(autotrade.ack_unmanaged()["cleared"])

    def test_ack_does_not_touch_position(self):
        """「我知道了」不是「帮我平」。仓位必须原地不动。"""
        self._seed_position(ticket=7777)
        with mock.patch.object(autotrade, "_all_positions", return_value=None):
            autotrade.close_all(self.cfg, why="面板停止并清仓")
        autotrade.ack_unmanaged()
        self.assertEqual(autotrade.load_state()["position"]["ticket"], 7777)

    def test_successful_flatten_drops_stale_mark(self):
        """下次真平掉了，旧标记自动失效——否则红条会挂到天荒地老。"""
        state = self._seed_position(ticket=8888)
        state["unmanaged"] = {"ts": int(time.time()) - 3600, "why": "面板停止并清仓",
                              "error": "MT5Busy", "tries": 3}
        autotrade.save_state(state)

        with mock.patch.object(autotrade, "_all_positions",
                               return_value=[{"ticket": 8888, "symbol": "XAUUSDc",
                                              "direction": 1, "volume": 0.01}]), \
             mock.patch.object(autotrade, "send_close", return_value={"ok": True}), \
             mock.patch.object(autotrade, "_close_fills",
                               return_value={"pnl": 1.0, "price": 4001.0,
                                             "reason": 0,
                                             "time": int(time.time())}):
            res = autotrade.close_all(self.cfg, why="面板停止并清仓")

        self.assertTrue(res["ok"], res)
        self.assertNotIn("unmanaged", autotrade.load_state())


if __name__ == "__main__":
    unittest.main()
