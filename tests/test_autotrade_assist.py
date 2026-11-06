"""辅助模式：人工确认、超时执行、信号队列、硬止损教训回灌。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from live import autotrade, paths
from tests._autotrade_base import (
    NOW, TempLedger, _FakeClock, _sig,
    bar, bars_up,
)


class TestAssistCfg(unittest.TestCase):
    def test_defaults_are_off_and_documented(self):
        cfg = autotrade.Cfg()
        self.assertFalse(cfg.assist)          # 不选第三档 = 与旧行为完全等价
        self.assertEqual(cfg.assist_entry_wait_sec, 90)
        self.assertEqual(cfg.assist_exit_wait_sec, 120)
        self.assertAlmostEqual(cfg.assist_max_slip_usd, 0.80)
        self.assertEqual(cfg.assist_deny_cooldown, 180)
        self.assertTrue(cfg.assist_raise_window)

    def test_paper_drill_is_a_legal_combo(self):
        """辅助 + 纸面 = 演练。互斥是错的：先把流程跑顺再上真钱是正经用法。"""
        self.assertEqual(autotrade.validate_cfg(
            autotrade.Cfg(assist=True, dry_run=True)), [])

    def test_bad_numbers_are_rejected_only_in_assist(self):
        # 关掉辅助时这几个参数不参与决策，不该拦人启动
        self.assertEqual(autotrade.validate_cfg(
            autotrade.Cfg(assist=False, assist_entry_wait_sec=0)), [])
        issues = autotrade.validate_cfg(autotrade.Cfg(assist=True,
                                                      assist_entry_wait_sec=0))
        self.assertTrue(any("入场确认时限" in i for i in issues))
        issues = autotrade.validate_cfg(autotrade.Cfg(assist=True,
                                                      assist_exit_wait_sec=1))
        self.assertTrue(any("出场确认时限" in i for i in issues))
        issues = autotrade.validate_cfg(autotrade.Cfg(assist=True,
                                                      assist_max_slip_usd=0))
        self.assertTrue(any("价格漂移" in i for i in issues))
        issues = autotrade.validate_cfg(autotrade.Cfg(assist=True,
                                                      assist_deny_cooldown=-1))
        self.assertTrue(any("否决冷却" in i for i in issues))

    def test_cfg_survives_state_roundtrip(self):
        """面板重启后要能还原"上次是辅助模式"，否则会把辅助当真实跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_root(tmp)
            paths.set_login("999")
            try:
                cfg = autotrade.Cfg(assist=True, assist_entry_wait_sec=45,
                                    assist_max_slip_usd=0.5)
                state = autotrade.load_state()
                state["cfg"] = autotrade.cfg_to_dict(cfg)
                autotrade.save_state(state)
                back = autotrade.cfg_from_state()
                self.assertTrue(back.assist)
                self.assertEqual(back.assist_entry_wait_sec, 45)
                self.assertAlmostEqual(back.assist_max_slip_usd, 0.5)
            finally:
                paths.set_root(None)

    def test_empty_state_has_signal_slots(self):
        st = autotrade._empty_state()
        self.assertIsNone(st["pending_signal"])
        self.assertEqual(st["assist_deny_until"], {})
        self.assertIsNone(st["last_signal_ts"])

    def test_fmt_renders_signal_row(self):
        text = autotrade._fmt({"ts": NOW, "kind": "signal", "sig_kind": "entry",
                               "answer": "approve", "waited_sec": 12.4,
                               "slip": 0.06})
        self.assertIn("人工确认", text)
        self.assertIn("同意", text)


class TestAssistDesk(TempLedger):
    def tearDown(self):
        autotrade.DESK.close()
        super().tearDown()

    def test_post_only_matches_current_id(self):
        autotrade.DESK.open("a")
        self.assertFalse(autotrade.DESK.post("b", True))   # 上一轮遗留，丢弃
        self.assertIsNone(autotrade.DESK.take_answer())
        self.assertTrue(autotrade.DESK.post("a", True))
        self.assertTrue(autotrade.DESK.wait(0.05))
        self.assertTrue(autotrade.DESK.take_answer()["approve"])
        self.assertIsNone(autotrade.DESK.take_answer())    # 取一次即消费

    def test_cancel_wakes_and_is_reset_by_open(self):
        autotrade.DESK.open("a")
        autotrade.DESK.cancel()
        self.assertTrue(autotrade.DESK.wait(0.05))
        self.assertTrue(autotrade.DESK.cancelled())
        autotrade.DESK.open("b")
        self.assertFalse(autotrade.DESK.cancelled())

    def test_answer_without_pending_is_rejected(self):
        r = autotrade.answer_signal("whatever", True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["expired"])
        self.assertFalse(autotrade.signal_answer_path().exists())

    def test_answer_writes_file_and_wakes_waiter(self):
        sig = _sig()
        state = autotrade.load_state()
        state["pending_signal"] = sig
        autotrade.save_state(state)
        autotrade.DESK.open(sig["id"])

        r = autotrade.answer_signal(sig["id"], True)
        self.assertTrue(r["ok"])
        raw = json.loads(autotrade.signal_answer_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["id"], sig["id"])
        self.assertTrue(raw["approve"])
        self.assertTrue(autotrade.DESK.take_answer()["approve"])

    def test_answer_after_expiry_is_rejected(self):
        sig = _sig(wait=5)
        sig["expires_at"] = int(time.time()) - 1
        state = autotrade.load_state()
        state["pending_signal"] = sig
        autotrade.save_state(state)
        r = autotrade.answer_signal(sig["id"], True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["expired"])
        self.assertFalse(autotrade.signal_answer_path().exists())


class TestAwaitHuman(TempLedger):
    def tearDown(self):
        autotrade.DESK.close()
        super().tearDown()

    def _wait(self, sig, **kw):
        cfg = kw.pop("cfg", None) or autotrade.Cfg()
        clock = kw.pop("clock", None) or _FakeClock(sig["ts"])
        return autotrade._await_human(cfg, sig, clock=clock,
                                      sleeper=clock.sleep, **kw), clock

    def test_approve_returns_fast(self):
        sig = _sig()
        out, clock = self._wait(
            sig, quote_fn=lambda s: (autotrade.DESK.post(sig["id"], True), (4000.0, 4000.24))[1])
        self.assertEqual(out["answer"], "approve")
        self.assertLess(out["waited_sec"], 1.0)

    def test_deny(self):
        sig = _sig()
        out, _ = self._wait(
            sig, quote_fn=lambda s: (autotrade.DESK.post(sig["id"], False), (4000.0, 4000.24))[1])
        self.assertEqual(out["answer"], "deny")

    def test_expires_at_deadline(self):
        sig = _sig(wait=3)
        out, _ = self._wait(sig, quote_fn=lambda s: (4000.0, 4000.24))
        self.assertEqual(out["answer"], "expired")
        self.assertAlmostEqual(out["waited_sec"], 3.0, places=1)

    def test_stop_loss_preempts_human(self):
        """止损和点击同时到：按止损处理，人的点击记 late。"""
        sig = _sig(wait=60, kind="exit")
        pos = {"ticket": 7, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": int(sig["ts"]), "p_up": 0.6}
        used = {"n": 0}

        def quote(_s):
            used["n"] += 1
            if used["n"] == 1:
                autotrade.DESK.post(sig["id"], True)     # 人点了
            return (3990.0, 3990.24)

        with mock.patch.object(autotrade, "_realized_pnl", return_value=-3.4):
            out, _ = self._wait(sig, pos=pos, quote_fn=quote,
                                positions_fn=lambda s: [{"ticket": 7}])
        self.assertEqual(out["answer"], "sl")
        self.assertTrue(out["late"])

    def test_max_hold_preempts_human(self):
        sig = _sig(wait=60, kind="exit")
        pos = {"ticket": 7, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": int(sig["ts"]) - 4000, "p_up": 0.6}
        cfg = autotrade.Cfg()
        clock = _FakeClock(sig["ts"])
        out = autotrade._await_human(cfg, sig, pos=pos, clock=clock,
                                     sleeper=clock.sleep,
                                     quote_fn=lambda s: (4000.0, 4000.24),
                                     positions_fn=lambda s: [{"ticket": 7}])
        self.assertEqual(out["answer"], "timeout")

    def test_position_gone(self):
        sig = _sig(wait=60, kind="exit")
        pos = {"ticket": 7, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": int(sig["ts"]), "p_up": 0.6}
        out, _ = self._wait(sig, pos=pos, quote_fn=lambda s: (4000.0, 4000.24),
                            positions_fn=lambda s: [])
        self.assertEqual(out["answer"], "gone")

    def test_positions_unreadable_keeps_waiting(self):
        """读不到持仓（None）是"不知道"，不能判成已平仓 —— 否则会漏平一笔真仓。"""
        sig = _sig(wait=2, kind="exit")
        pos = {"ticket": 7, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": int(sig["ts"]), "p_up": 0.6}
        out, _ = self._wait(sig, pos=pos, quote_fn=lambda s: (4000.0, 4000.24),
                            positions_fn=lambda s: None)
        self.assertEqual(out["answer"], "expired")

    def test_cancel_returns_cancelled(self):
        sig = _sig()
        out, _ = self._wait(
            sig, quote_fn=lambda s: (autotrade.DESK.cancel(), (4000.0, 4000.24))[1])
        self.assertEqual(out["answer"], "cancelled")
        self.assertLess(out["waited_sec"], 1.0)

    def test_entry_blocked_when_position_appears(self):
        """等你点同意的这 90 秒里，你自己在终端开了一笔 —— 不能再叠一笔。"""
        sig = _sig(dry_run=False)
        out, _ = self._wait(sig, quote_fn=lambda s: (4000.0, 4000.24),
                            positions_fn=lambda s: [{"ticket": 99}])
        self.assertEqual(out["answer"], "blocked")

    def test_paper_entry_does_not_check_positions(self):
        sig = _sig(dry_run=True)
        out, _ = self._wait(sig, cfg=autotrade.Cfg(dry_run=True),
                            quote_fn=lambda s: (autotrade.DESK.post(sig["id"], True),
                                                (4000.0, 4000.24))[1],
                            positions_fn=lambda s: [{"ticket": 99}])
        self.assertEqual(out["answer"], "approve")

    def test_answer_file_is_consumed(self):
        sig = _sig()
        state = autotrade.load_state()
        state["pending_signal"] = sig
        autotrade.save_state(state)
        autotrade.answer_signal(sig["id"], True)
        autotrade.DESK.close()               # 丢掉内存通道，只靠文件
        clock = _FakeClock(sig["ts"])
        out = autotrade._await_human(autotrade.Cfg(), sig, clock=clock,
                                     sleeper=clock.sleep,
                                     quote_fn=lambda s: (4000.0, 4000.24))
        self.assertEqual(out["answer"], "approve")
        self.assertFalse(autotrade.signal_answer_path().exists())

    def test_market_checks_are_throttled(self):
        """90 秒里不能每 0.2 秒打一次终端——答复走 Event，行情走定时器。"""
        sig = _sig(wait=10)
        calls = {"n": 0}

        def quote(_s):
            calls["n"] += 1
            return (4000.0, 4000.24)

        self._wait(sig, quote_fn=quote)
        self.assertLessEqual(calls["n"], 3)


class TestAssistEntry(TempLedger):
    """辅助模式的入场侧。等待逻辑本身已在 TestAwaitHuman 里测过，
    这里只验**接到 tick 之后**分支走对没有、日志落对没有。"""

    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(assist=True, dry_run=True, interval_sec=60,
                                 cooldown_sec=30)

    def _tick(self, answer, bid=4000.0, ask=4000.24, cfg=None, p_up=0.7,
              reason="结构支持", positions=None, ans_ask=None, ans_bid=None):
        ans = {"answer": answer, "bid": bid if ans_bid is None else ans_bid,
               "ask": ask if ans_ask is None else ans_ask, "fp": None,
               "waited_sec": 3.0, "src": "human", "late": False}
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": p_up,
                                             "reason": reason,
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human", return_value=ans) as m_wait, \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": ask,
                                             "order": 555}) as m_open:
            row = autotrade.tick(cfg or self.cfg, now=NOW,
                                 quote=(bid, ask), rows=bars_up(),
                                 positions=[] if positions is None else positions)
        return row, m_open, m_wait

    def _signal_rows(self):
        return [r for r in autotrade.read_log() if r.get("kind") == "signal"]

    def test_approve_opens_and_marks_exec_by(self):
        row, m_open, m_wait = self._tick("approve")
        self.assertEqual(row["kind"], "open")
        self.assertEqual(row["exec_by"], "human")
        self.assertTrue(row["signal_id"])
        m_open.assert_called_once()
        self.assertIsNone(autotrade.load_state()["pending_signal"])

    def test_pending_signal_is_visible_while_waiting(self):
        """面板靠 state 里这条记录弹窗——等的时候必须真实存在。"""
        seen = {}

        def fake_wait(cfg, sig, login=None, pos=None, **kw):
            seen["id"] = (autotrade.load_state()["pending_signal"] or {}).get("id")
            seen["sig"] = sig["id"]
            return {"answer": "approve", "bid": 4000.0, "ask": 4000.24,
                    "fp": None, "waited_sec": 1.0, "src": "human", "late": False}

        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human", side_effect=fake_wait), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4000.24,
                                             "order": 555}):
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        self.assertEqual(seen["id"], seen["sig"])
        self.assertIsNone(autotrade.load_state()["pending_signal"])

    def test_deny_skips_and_sets_cooldown(self):
        row, m_open, _ = self._tick("deny")
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["answer"], "deny")
        self.assertIn("人工否决", row["reason"])
        m_open.assert_not_called()
        state = autotrade.load_state()
        self.assertGreaterEqual(state["assist_deny_until"]["entry"],
                                int(time.time()) + 170)

    def test_expired_is_no_trade(self):
        row, m_open, _ = self._tick("expired")
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["answer"], "expired")
        m_open.assert_not_called()
        self.assertIn("无人应答", row["reason"])

    def test_cancelled_never_opens_and_skips_cooldown(self):
        row, m_open, _ = self._tick("cancelled")
        self.assertEqual(row["kind"], "skip")
        m_open.assert_not_called()
        self.assertEqual(autotrade.load_state()["assist_deny_until"], {})

    def test_cooldown_skips_model_call(self):
        state = autotrade.load_state()
        state["assist_deny_until"] = {"entry": int(time.time()) + 120}
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "ask_entry") as m_entry:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("不再弹窗", row["reason"])
        m_entry.assert_not_called()          # 省的是 token，不是版面

    def test_slip_voids_the_signal(self):
        # 信号价 ask=4000.24，答复时 ask=4001.10 → 不利偏离 0.86 > 0.80
        row, m_open, _ = self._tick("approve", ans_ask=4001.10)
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["answer"], "void_slip")
        self.assertAlmostEqual(row["slip"], 0.86)
        m_open.assert_not_called()

    def test_slip_at_limit_still_opens(self):
        row, m_open, _ = self._tick("approve", ans_ask=4001.03)   # 0.79
        self.assertEqual(row["kind"], "open")
        self.assertAlmostEqual(row["slip"], 0.79)
        m_open.assert_called_once()

    def test_favorable_move_is_not_blocked(self):
        """买得更便宜是好事，不能判作废。"""
        row, m_open, _ = self._tick("approve", ans_ask=3999.0)    # 有利 -1.24
        self.assertEqual(row["kind"], "open")
        m_open.assert_called_once()

    def test_position_appearing_blocks(self):
        cfg = autotrade.Cfg(assist=True, dry_run=False, interval_sec=60,
                            cooldown_sec=30)
        with mock.patch.object(autotrade, "_positions",
                               return_value=[{"ticket": 9}]), \
             mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human",
                               return_value={"answer": "blocked", "bid": 4000.0,
                                             "ask": 4000.24, "fp": None,
                                             "waited_sec": 5.0, "src": None,
                                             "late": False}), \
             mock.patch.object(autotrade, "send_open") as m_open:
            row = autotrade.tick(cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        m_open.assert_not_called()

    def test_one_signal_row_per_attempt(self):
        self._tick("deny")
        rows = self._signal_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("signal_id", "sig_kind", "answer", "waited_sec",
                    "direction", "p_up", "prog_dir", "reason", "drv"):
            self.assertIn(key, r)
        self.assertEqual(r["sig_kind"], "entry")

    # ---- 否决后状态带要改口（2026-10-29）------------------------------------
    # last_decision 是轮首写的，应答发生在它之后。不打标的话状态带会一直挂着
    # 「模型开仓 + 做多的理由」，而弹窗写着「已否决」——同一块屏幕上两句相反的话。

    def test_deny_marks_last_decision(self):
        self._tick("deny")
        ld = autotrade.load_state()["last_decision"]
        self.assertEqual(ld["answer"], "deny")
        self.assertGreater(ld["deny_until"], int(time.time()) + 150)
        # 模型原话一个字都不许动：复盘要看的仍是它当时怎么说的
        self.assertEqual(ld["reason"], "结构支持")
        self.assertTrue(ld["act"])

    def test_expired_marks_last_decision(self):
        self._tick("expired")
        ld = autotrade.load_state()["last_decision"]
        self.assertEqual(ld["answer"], "expired")
        self.assertGreater(ld["deny_until"], int(time.time()) + 150)

    def test_cancelled_marks_without_deny_until(self):
        """取消不写冷却，所以不能挂一个静默时间上去。"""
        self._tick("cancelled")
        ld = autotrade.load_state()["last_decision"]
        self.assertEqual(ld["answer"], "cancelled")
        self.assertIsNone(ld["deny_until"])

    def test_slip_void_marks_last_decision(self):
        self._tick("approve", ans_ask=4001.10)      # 0.86 > 0.80 上限
        ld = autotrade.load_state()["last_decision"]
        self.assertEqual(ld["answer"], "void_slip")

    def test_approve_does_not_mark_answer(self):
        """真开了仓就不能带 answer，否则标签会被「已否决」盖掉。"""
        self._tick("approve")
        ld = autotrade.load_state()["last_decision"]
        self.assertNotIn("answer", ld)

    def test_assist_off_never_asks(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human") as m_wait, \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "price": 4000.24,
                                             "order": 555}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4000.0, 4000.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "open")
        m_wait.assert_not_called()

    # ---- 同意的结局要能对上号（2026-11-06）----------------------------------
    # 实盘：用户在最后几秒点了同意，价已朝不利方向跑 3.38，漂移保护把这笔
    # 作废。面板只写了一句「正在按当前价执行…」就再没下文 → 看着像卡死；
    # 紧接着的冷却行还写「上一条信号未获批准」，把"人批了但没执行"说成
    # "人没批"。下面几条钉住：文案要说实话，流水要能按 signal_id 对上号。

    def test_cooldown_text_says_agreed_when_slip_voided(self):
        self._tick("approve", ans_ask=4001.10)          # 0.86 > 0.80 → 作废
        row = autotrade.tick(self.cfg, now=NOW + 60, quote=(4000.0, 4000.24),
                             rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["answer"], "void_slip")     # 面板靠它出标签
        self.assertIn("已同意", row["reason"])
        self.assertNotIn("未获批准", row["reason"])
        self.assertIn("不再弹窗", row["reason"])

    def test_cooldown_text_after_deny_says_denied(self):
        self._tick("deny")
        row = autotrade.tick(self.cfg, now=NOW + 60, quote=(4000.0, 4000.24),
                             rows=bars_up(), positions=[])
        self.assertIn("否决", row["reason"])
        self.assertNotIn("已同意", row["reason"])

    def test_cooldown_text_without_history_stays_generic(self):
        """旧 state 没有 last_decision.answer（升级后第一次跑）也不能崩。"""
        state = autotrade.load_state()
        state["assist_deny_until"] = {"entry": int(time.time()) + 120}
        state["last_decision"] = None
        autotrade.save_state(state)
        row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                             rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("未获批准", row["reason"])

    def test_signal_row_uses_issue_ts_not_write_ts(self):
        """signal 行在人答完之后才写盘，ts 必须是**信号发出**的时刻。
        用写盘时的墙钟，会把这一行盖到 120 秒等待窗的末尾，流水按 ts 排
        就变成「先有结论、后有信号」——像时间倒流。等了多久由 waited_sec 表达。"""
        seen = {}

        def fake_wait(cfg, sig, login=None, pos=None, **kw):
            seen["ts"] = sig["ts"]
            return {"answer": "approve", "bid": 4000.0, "ask": 4001.10,
                    "fp": None, "waited_sec": 88.6, "src": "human", "late": False}

        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human", side_effect=fake_wait), \
             mock.patch.object(autotrade, "send_open") as m_open:
            autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                           rows=bars_up(), positions=[])
        m_open.assert_not_called()                       # 3.38 级别漂移 → 作废
        sig = self._signal_rows()[-1]
        self.assertEqual(sig["ts"], seen["ts"])
        self.assertEqual(sig["waited_sec"], 88.6)

    def test_approved_but_blocked_by_cooldown_keeps_signal_id(self):
        """点了同意、却被同向冷却挡下：流水必须能把这笔对上号，
        否则面板只能盯着 30 行流水猜哪一条是它的结局。

        时间基准：辅助模式下这道判定用的是**真实时钟**（人可能点了 90 秒），
        所以这里把 time.time 钉到 NOW 附近，让"短冷却已过、同向冷却还差 140 秒"
        这个状态成立——`can_open` 用 now（不知方向，按 30 秒算）放行，
        应答回来之后按方向再判一次才拦下，记成 void_state。"""
        state = autotrade.load_state()
        state["last_close_ts"] = NOW - 40            # 短冷却 30 秒已过
        state["last_close_dir"] = 1                  # 和这笔同向（180 秒档）
        autotrade.save_state(state)
        with mock.patch.object(autotrade.time, "time", return_value=NOW + 1):
            row, m_open, _ = self._tick("approve")
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["answer"], "void_state")
        self.assertIn("冷却", row["reason"])
        self.assertTrue(row["signal_id"])
        m_open.assert_not_called()

    def test_open_failed_keeps_signal_id(self):
        """同意之后被券商拒单——最需要能对上号的一种结局。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": True, "p_up": 0.7,
                                             "reason": "结构支持", "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human",
                               return_value={"answer": "approve", "bid": 4000.0,
                                             "ask": 4000.24, "fp": None,
                                             "waited_sec": 3.0, "src": "human",
                                             "late": False}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": False, "reason": "保证金不足"}):
            row = autotrade.tick(self.cfg, now=NOW, quote=(4000.0, 4000.24),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "open_failed")
        self.assertTrue(row["signal_id"])



class TestAssistExit(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(assist=True, dry_run=True, interval_sec=60,
                                 cooldown_sec=30)
        state = autotrade.load_state()
        state["position"] = {
            "ticket": -1, "direction": 1, "lots": 0.01,
            "price_open": 4000.0, "opened_at": NOW - 120, "p_up": 0.7,
            "entry_reason": "结构支持", "dry_run": True, "prog_dir": 1,
        }
        autotrade.save_state(state)

    def _tick(self, answer="approve", bid=4003.6, ask=4003.84, cfg=None,
              close=True, fp=None, await_wait=None):
        ans = {"answer": answer, "bid": bid, "ask": ask,
               "fp": None if fp is None else fp,
               "waited_sec": 8.0, "src": "human", "late": False}
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": close,
                                             "reason": "到位了，落袋",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human",
                               return_value=ans) as m_wait, \
             mock.patch.object(autotrade, "send_close") as m_close:
            row = autotrade.tick(cfg or self.cfg, now=NOW,
                                 quote=(bid, ask), rows=bars_up(),
                                 positions=[])
        return row, m_close, m_wait

    def test_approve_closes_and_keeps_source(self):
        row, m_close, m_wait = self._tick("approve", bid=4006.0, ask=4006.24)
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["exec_by"], "human")
        self.assertEqual(row["source"], "llm")      # 不能被人工覆盖
        self.assertIn("人工确认平仓", row["reason"])
        m_wait.assert_called_once()

    def test_pnl_is_recomputed_with_new_quote(self):
        """等了 120 秒，成交价早变了，不能拿弹窗那刻的浮盈记账。"""
        row, _, _ = self._tick("approve", bid=4006.0, ask=4006.24)
        self.assertAlmostEqual(row["pnl"], 6.0, places=2)

    def test_deny_holds_and_cools_down(self):
        row, m_close, _ = self._tick("deny")
        self.assertEqual(row["kind"], "hold")
        self.assertIn("人工否决平仓", row["reason"])
        m_close.assert_not_called()
        state = autotrade.load_state()
        self.assertIsNotNone(state["position"])
        self.assertGreaterEqual(state["assist_deny_until"]["exit"],
                                int(time.time()) + 170)

    def test_expired_executes_model_decision(self):
        row, _, _ = self._tick("expired")
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["exec_by"], "assist_expired")
        self.assertIn("无人应答", row["reason"])

    def test_deny_cooldown_suppresses_second_popup(self):
        state = autotrade.load_state()
        state["assist_deny_until"] = {"exit": int(time.time()) + 120}
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "ask_exit",
                               return_value={"close": True, "reason": "到位",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_await_human") as m_wait:
            row = autotrade.tick(self.cfg, now=NOW, quote=(4003.6, 4003.84),
                                 rows=bars_up(), positions=[])
        self.assertEqual(row["kind"], "hold")
        m_wait.assert_not_called()

    def test_stop_loss_never_prompts(self):
        row, m_close, m_wait = self._tick(bid=3996.9, ask=3997.14)
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")
        m_wait.assert_not_called()
        self.assertEqual(autotrade.load_state()["sl_closes"], 1)

    def test_max_hold_never_prompts(self):
        state = autotrade.load_state()
        state["position"]["opened_at"] = NOW - 4000
        autotrade.save_state(state)
        row, _, m_wait = self._tick()
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "timeout")
        m_wait.assert_not_called()

    def test_below_tp_threshold_no_prompt_and_no_model(self):
        row, _, m_wait = self._tick(bid=4002.0, ask=4002.24)
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row["source"], "tp_wait")
        m_wait.assert_not_called()

    def test_model_holds_no_prompt(self):
        row, _, m_wait = self._tick(close=False)
        self.assertEqual(row["kind"], "hold")
        m_wait.assert_not_called()

    def test_paper_exit_never_touches_broker(self):
        row, m_close, _ = self._tick("approve")
        m_close.assert_not_called()
        self.assertTrue(row["dry_run"])

    def test_signal_row_logged_for_exit(self):
        self._tick("approve")
        rows = [r for r in autotrade.read_log() if r.get("kind") == "signal"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sig_kind"], "exit")


class TestAssistRuntime(TempLedger):
    def tearDown(self):
        autotrade.DESK.close()
        super().tearDown()

    def test_orphan_signal_is_cleared(self):
        """面板重启后 state 里那条信号没人等它 —— 不能一直弹窗。"""
        state = autotrade.load_state()
        state["pending_signal"] = _sig(wait=90)
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"act": False, "p_up": 0.5,
                                             "reason": "不做", "source": "llm"}):
            autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                           quote=(4000.0, 4000.24), rows=bars_up(), positions=[])
        self.assertIsNone(autotrade.load_state()["pending_signal"])
        rows = [r for r in autotrade.read_log() if r.get("kind") == "signal"]
        self.assertEqual(rows[-1]["answer"], "orphan")

    def test_stop_cancels_waiting_signal(self):
        """「关闭并清仓」不能让等待者醒来之后补开一笔。"""
        sig = _sig(wait=60)
        started = time.time()
        th = threading.Thread(
            target=lambda: autotrade._await_human(autotrade.Cfg(), sig,
                                                  positions_fn=lambda s: []),
            daemon=True)
        th.start()
        time.sleep(0.05)
        autotrade.WORKER.stop(flatten=False)
        th.join(timeout=3)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - started, 3)

    def test_summary_exposes_assist_fields(self):
        state = autotrade.load_state()
        state["cfg"] = autotrade.cfg_to_dict(autotrade.Cfg(assist=True))
        # 信号只有在真有 worker 在等它的时候才会发给页面（见 10-29 孤儿弹窗），
        # 所以这里要把 running 和"本进程有活线程"一起造出来。
        state["running"] = True
        state["owner"] = "panel"
        state["pending_signal"] = _sig(wait=60)
        autotrade.save_state(state)
        worker = mock.Mock()
        worker.alive.return_value = True
        with mock.patch.object(autotrade, "WORKER", worker):
            s = autotrade.summary()
        self.assertTrue(s["assist"])
        self.assertIsNotNone(s["pending_signal"])
        self.assertGreater(s["pending_signal"]["remain_sec"], 0)
        self.assertIn("signal_stats", s)
        self.assertIn("server_ts", s)
        self.assertIn("assist_cfg", s)

    def test_summary_signal_stats_counts(self):
        for ans in ("approve", "deny", "expired"):
            autotrade.append_log({"ts": int(time.time()), "kind": "signal",
                                  "signal_id": ans, "sig_kind": "entry",
                                  "answer": ans, "waited_sec": 5})
        st = autotrade.summary()["signal_stats"]
        self.assertEqual(st["n"], 3)
        self.assertEqual(st["approve"], 1)
        self.assertEqual(st["deny"], 1)
        self.assertEqual(st["expired"], 1)
        self.assertAlmostEqual(st["approve_rate"], 1 / 3, places=3)

    def test_old_state_without_assist_fields(self):
        """旧 state 没有 assist 字段时不能炸，也不能显示成"辅助"。"""
        st = autotrade.load_state()
        st.pop("assist_deny_until", None)
        st.pop("pending_signal", None)
        st["cfg"] = None
        autotrade.save_state(st)
        s = autotrade.summary()
        self.assertFalse(s["assist"])
        self.assertIsNone(s["pending_signal"])


class TestHardStopLessonReachesEntry(TempLedger):
    """被硬止损砍掉的单，教训不许只归出场端。

    实测 2026-10-28：21:46 写下「破 4047.4 双底立即止损」（模型标 scope=exit），
    22:07 在 4047.44 又接了一笔多（-3.15）——因为那条教训根本进不了入场提示词。
    根因是出场那一刻是程序按 sl_usd 砍的，模型没有出场决策权，
    把它锁在出场端等于这条教训永远影响不到下一次入场。
    """

    def _rows(self, n=60):
        return [bar(4000 + i, 4001 + i, 3999 + i, 4000.0 + i, NOW + i * 60)
                for i in range(n)]

    def _review(self, exit_source, reply):
        pos = {"ticket": 1, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "entry_reason": "贴下沿做多", "exit_reason": "硬止损",
               "exit_source": exit_source, "opened_at": NOW}
        with mock.patch.object(autotrade, "_bars", return_value=self._rows()), \
             mock.patch("agent.llm.chat_ex", return_value=(reply, {})):
            return autotrade.ask_review(autotrade.Cfg(review=True), pos,
                                        3997.0, -3.0, NOW, NOW + 20 * 60,
                                        exit_source=exit_source)

    def test_sl_close_forces_scope_both(self):
        res = self._review("sl", '{"scope":"exit","theme":"止损太晚",'
                                 '"lesson":"接双底多单后破位，下次破位立刻止损"}')
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["scope"], "both")
        self.assertEqual(res["src"], "sl")
        self.assertEqual(res["theme"], "止损太晚")     # 主题不动，只放开端

    def test_model_close_keeps_exit_scope(self):
        """模型自己平的仓：出场确实是它的决策，照旧归出场端。"""
        res = self._review("llm", '{"scope":"exit","theme":"止损太晚",'
                                  '"lesson":"砍晚了"}')
        self.assertEqual(res["scope"], "exit")
        self.assertEqual(res["src"], "")

    def test_sl_lesson_reaches_entry_prompt(self):
        """端到端：这条教训必须出现在入场端，出场端也要在。"""
        autotrade.append_lesson({
            "ts": NOW, "lesson": "接双底多单后破位，下次破 4047.4 立刻止损",
            "theme": "止损太晚", "scope": "both", "src": "sl",
            "verdict": "late", "regime": "range"})
        self.assertIn("4047.4", autotrade.recent_lessons(scope="entry"))
        self.assertIn("4047.4", autotrade.recent_lessons(scope="exit"))

    def test_non_sl_exit_lesson_still_blocked(self):
        """没有 sl 标记的出场教训，入场端照旧不许看到（分域规则不倒退）。"""
        autotrade.append_lesson({
            "ts": NOW, "lesson": "接双底多单后破位", "theme": "止损太晚",
            "scope": "both", "regime": "range"})
        self.assertIn("暂无", autotrade.recent_lessons(scope="entry"))
        self.assertIn("接双底", autotrade.recent_lessons(scope="exit"))

    def test_sl_lesson_says_who_closed_it(self):
        """注入文本要说明"这笔不是你平的"，否则模型会读成"下次早点平"。"""
        autotrade.append_lesson({
            "ts": NOW, "lesson": "接双底多单后破位", "theme": "止损太晚",
            "scope": "both", "src": "sl", "regime": "range"})
        s = autotrade.recent_lessons(scope="entry")
        self.assertIn("硬止损砍的", s)
        self.assertIn("该不该进", s)

    def test_exit_source_survives_the_queue(self):
        """平仓 → 排队 → 复盘，exit_source 一路不许丢。"""
        state = autotrade._empty_state()
        pos = {"ticket": 88, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "entry_reason": "贴下沿", "exit_reason": "硬止损：浮亏 -3.14",
               "exit_source": "sl", "opened_at": NOW - 300}
        autotrade.queue_review(state, autotrade.Cfg(review=True), pos, 3997.0,
                               -3.14, NOW)
        item = state["pending_reviews"][0]
        self.assertEqual(item["exit_source"], "sl")
        autotrade.save_state(state)
        with mock.patch.object(autotrade, "ask_review",
                               return_value={"ok": True, "lesson": "接刀破位",
                                             "theme": "止损太晚", "scope": "both",
                                             "src": "sl", "bars": 15,
                                             "regime": "range"}) as spy:
            autotrade.process_due_reviews(autotrade.Cfg(review=True),
                                          NOW + 60 * 60)
        self.assertEqual(spy.call_args.kwargs["exit_source"], "sl")
        # 落盘的行和教训库都要带 src，否则注入端认不出来
        row = [r for r in autotrade.read_log() if r.get("kind") == "review"][0]
        self.assertEqual(row["src"], "sl")
        self.assertEqual(autotrade.lesson_rows()[0]["src"], "sl")
