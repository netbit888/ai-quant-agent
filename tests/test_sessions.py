"""场次切分、自动行为标记、下一场规则、账户删除的单元测试。

这些功能的共同点是「不需要用户动手」：切场靠时间戳、标记靠价格和时长、
规则检查靠指标重算。所以它们必须能被单测钉死——一旦算错，
面板上看到的每一个结论都是错的，而且错得不明显。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from live import paths, rules, sessions

T0 = 1_792_000_000
MIN = 60


def trade(pid, entry_t, exit_t, profit, *, direction=1, volume=0.01,
          entry_price=4000.0, exit_price=None, reason="manual", symbol="XAUUSDm"):
    return {
        "position_id": pid,
        "symbol": symbol,
        "direction": direction,
        "entry_time": entry_t,
        "exit_time": exit_t,
        "entry_price": entry_price,
        "exit_price": exit_price if exit_price is not None else entry_price,
        "volume": volume,
        "profit": profit,
        "gross": profit,
        "commission": 0.0,
        "swap": 0.0,
        "close_reason": reason,
        "comment": "",
    }


class TestSplit(unittest.TestCase):
    def test_gap_over_30min_starts_new_session(self):
        trades = [
            trade(1, T0, T0 + 5 * MIN, 1.0),
            trade(2, T0 + 40 * MIN, T0 + 45 * MIN, 1.0),   # 距上一笔平仓 35 分钟
        ]
        groups = sessions.split(trades)
        self.assertEqual([len(g) for g in groups], [1, 1])

    def test_gap_under_30min_stays_in_session(self):
        trades = [
            trade(1, T0, T0 + 5 * MIN, 1.0),
            trade(2, T0 + 29 * MIN, T0 + 35 * MIN, 1.0),
        ]
        self.assertEqual([len(g) for g in sessions.split(trades)], [2])

    def test_gap_is_measured_from_previous_exit_not_entry(self):
        # 第一笔持仓 20 分钟，第二笔在它平仓后 15 分钟才开 → 同一场
        trades = [
            trade(1, T0, T0 + 20 * MIN, 1.0),
            trade(2, T0 + 35 * MIN, T0 + 40 * MIN, 1.0),
        ]
        self.assertEqual([len(g) for g in sessions.split(trades)], [2])

    def test_overlapping_positions_are_same_session(self):
        trades = [
            trade(1, T0, T0 + 30 * MIN, 1.0),
            trade(2, T0 + 10 * MIN, T0 + 40 * MIN, 1.0),
        ]
        self.assertEqual([len(g) for g in sessions.split(trades)], [2])

    def test_open_trades_are_ignored(self):
        closed = trade(1, T0, T0 + MIN, 1.0)
        opened = trade(2, T0 + 5 * MIN, None, 0.0)
        self.assertEqual(len(sessions.split([closed, opened])[0]), 1)

    def test_session_id_is_stable(self):
        items = [trade(1, T0, T0 + MIN, 1.0)]
        self.assertEqual(sessions.session_id(items), f"S{T0}")


class TestEnrich(unittest.TestCase):
    def test_risk_pct_uses_balance_at_entry(self):
        trades = [trade(1, T0, T0 + MIN, -0.5, entry_price=4000.0)]
        flows = [{"time": T0 - 100, "amount": 100.0, "comment": "deposit"}]
        plans = {"1": {"sl": 3999.5}}     # 0.5 价格 = 500 points
        rows = sessions.enrich(trades, flows=flows, deposits=100.0, plans=plans)
        r = rows[0]
        self.assertEqual(r["sl_points"], 500)
        self.assertAlmostEqual(r["risk_amount"], 0.5)     # 500pt × 0.001 × 0.01lot × 100
        self.assertEqual(r["balance_before"], 100.0)
        self.assertAlmostEqual(r["risk_pct"], 0.5)

    def test_sl_points_falls_back_to_stop_out_price(self):
        # 没有持仓快照时，止损单用成交价反推止损距离
        t = trade(1, T0, T0 + MIN, -1.0, entry_price=4000.0, exit_price=3999.0, reason="sl")
        r = sessions.enrich([t])[0]
        self.assertEqual(r["sl_points"], 1000)
        self.assertIsNone(r["sl_price"])

    def test_no_sl_means_no_risk_pct(self):
        t = trade(1, T0, T0 + MIN, 1.0, reason="tp")
        r = sessions.enrich([t])[0]
        self.assertIsNone(r["sl_points"])
        self.assertIsNone(r["risk_pct"])

    def test_balance_before_walks_deposits_and_pnl(self):
        trades = [
            trade(1, T0 + 10, T0 + 20, -2.0),
            trade(2, T0 + 30, T0 + 40, 1.0),
        ]
        flows = [{"time": T0, "amount": 10.0, "comment": "deposit"}]
        bal = sessions.balance_before(trades, flows)
        self.assertAlmostEqual(bal["1"], 10.0)
        self.assertAlmostEqual(bal["2"], 8.0)

    def test_negative_balance_is_floored_at_zero(self):
        trades = [trade(1, T0 + 10, T0 + 20, -50.0), trade(2, T0 + 30, T0 + 40, 1.0)]
        bal = sessions.balance_before(trades, [{"time": T0, "amount": 10.0}])
        self.assertEqual(bal["2"], 0.0)


class TestFlags(unittest.TestCase):
    def setUp(self):
        self.trades = [
            trade(1, T0, T0 + 3 * MIN, -2.0, reason="sl", entry_price=4000.0, exit_price=3998.0),
            # 亏损后 2 分钟再入场 → 报复单
            trade(2, T0 + 5 * MIN, T0 + 5 * MIN + 30, -0.5, reason="sl",
                  entry_price=3995.0, exit_price=3994.5),
        ]

    def test_revenge_flag(self):
        s = sessions.build(self.trades)[0]
        codes = [f["code"] for f in s["flags"]]
        self.assertIn("revenge", codes)

    def test_itchy_flag(self):
        s = sessions.build(self.trades)[0]
        itchy = [f for f in s["flags"] if f["code"] == "itchy"]
        self.assertEqual(len(itchy), 1)
        self.assertEqual(str(itchy[0]["pid"]), "2")

    def test_wide_stop_flag(self):
        trades = [
            trade(1, T0, T0 + 5 * MIN, -1.0, reason="sl", entry_price=4000.0, exit_price=3999.0),
            trade(2, T0 + 6 * MIN, T0 + 11 * MIN, -1.0, reason="sl", entry_price=4000.0, exit_price=3999.0),
            trade(3, T0 + 12 * MIN, T0 + 17 * MIN, -4.0, reason="sl", entry_price=4000.0, exit_price=3996.0),
        ]
        s = sessions.build(trades)[0]
        wide = [f for f in s["flags"] if f["code"] == "wide_stop"]
        self.assertEqual(len(wide), 1)
        self.assertEqual(str(wide[0]["pid"]), "3")

    def test_clean_session_has_no_high_flags(self):
        trades = [
            trade(1, T0, T0 + 30 * MIN, 2.0, reason="tp"),
            trade(2, T0 + 40 * MIN, T0 + 70 * MIN, 2.0, reason="tp"),
        ]
        s = sessions.build(trades)[0]
        self.assertFalse([f for f in s["flags"] if f["level"] == "high"])


class TestSessionStats(unittest.TestCase):
    def test_stats_and_breakeven_margin(self):
        trades = [
            trade(1, T0, T0 + 10 * MIN, 2.0, reason="tp"),
            trade(2, T0 + 20 * MIN, T0 + 30 * MIN, -1.0, reason="sl"),
        ]
        st = sessions.build(trades)[0]["stats"]
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["wins"], 1)
        self.assertAlmostEqual(st["rr"], 2.0)
        self.assertAlmostEqual(st["need_win_rate"], 33.3)
        self.assertAlmostEqual(st["margin_pp"], 16.7)

    def test_sessions_are_newest_first(self):
        trades = [
            trade(1, T0, T0 + MIN, 1.0),
            trade(2, T0 + 60 * MIN, T0 + 61 * MIN, 1.0),
        ]
        built = sessions.build(trades)
        self.assertEqual(built[0]["index"], 2)
        self.assertEqual(built[1]["index"], 1)

    def test_same_direction_streak_metric(self):
        trades = [
            trade(1, T0, T0 + MIN, 1.0, direction=1),
            trade(2, T0 + 5 * MIN, T0 + 6 * MIN, 1.0, direction=1),
            trade(3, T0 + 10 * MIN, T0 + 11 * MIN, -1.0, direction=-1),
            trade(4, T0 + 15 * MIN, T0 + 16 * MIN, 1.0, direction=-1),
        ]
        m = sessions.build(trades)[0]["metrics"]
        self.assertEqual(m["max_same_direction_streak"], 2)
        self.assertEqual(m["trades"], 4)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        paths.set_login("9001")

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validate_rejects_unknown_metric(self):
        self.assertIsNone(rules.validate({"text": "x", "metric": "nope", "value": 1}))
        self.assertIsNone(rules.validate({"text": "", "metric": "trades", "value": 1}))
        self.assertIsNone(rules.validate({"text": "x", "metric": "trades", "value": "abc"}))
        self.assertIsNone(rules.validate(None))

    def test_validate_forces_direction_from_table(self):
        # 模型给了 >=，但「最大风险 ≤」的方向由指标表决定，不允许反着写
        r = rules.validate({"text": "单笔风险别超过 5%", "metric": "max_risk_pct",
                            "op": ">=", "value": 5})
        self.assertEqual(r["op"], "<=")

    def test_save_load_clear(self):
        r = rules.validate({"text": "同向最多 2 笔", "metric": "max_same_direction_streak", "value": 2})
        rules.save(r, after_ts=T0, session_id="S1")
        loaded = rules.load()
        self.assertEqual(loaded["rule"]["text"], "同向最多 2 笔")
        self.assertEqual(loaded["rule"]["after_ts"], T0)
        rules.clear()
        self.assertIsNone(rules.load()["rule"])

    def test_rule_only_applies_after_its_session(self):
        rule = {"metric": "trades", "op": "<=", "value": 2, "after_ts": T0}
        self.assertFalse(rules.applies_to(rule, {"start": T0 - 1}))
        self.assertFalse(rules.applies_to(rule, {"start": T0}))   # 同一场不算
        self.assertTrue(rules.applies_to(rule, {"start": T0 + 1}))
        self.assertFalse(rules.applies_to(None, {"start": T0 + 1}))

    def test_check_pass_and_fail(self):
        rule = {"metric": "max_risk_pct", "op": "<=", "value": 5, "text": "风险 ≤5%", "label": "最大风险"}
        ok = rules.check(rule, {"start": T0 + 1, "metrics": {"max_risk_pct": 3.0}})
        self.assertEqual(ok["status"], "pass")
        bad = rules.check(rule, {"start": T0 + 1, "metrics": {"max_risk_pct": 9.0}})
        self.assertEqual(bad["status"], "fail")
        missing = rules.check(rule, {"start": T0 + 1, "metrics": {}})
        self.assertEqual(missing["status"], "unknown")

    def test_summarize_pending_without_later_session(self):
        rule = {"metric": "trades", "op": "<=", "value": 2, "text": "最多 2 笔", "after_ts": T0}
        out = rules.summarize(rule, [{"start": T0 - 10, "id": "S0", "metrics": {"trades": 5}}])
        self.assertEqual(out["status"], "pending")

    def test_summarize_uses_latest_session(self):
        rule = {"metric": "trades", "op": "<=", "value": 2, "text": "最多 2 笔", "after_ts": T0}
        sess = [
            {"start": T0 + 100, "id": "S2", "metrics": {"trades": 3}},
            {"start": T0 + 50, "id": "S1", "metrics": {"trades": 1}},
        ]
        out = rules.summarize(rule, sess)
        self.assertEqual(out["session_id"], "S2")
        self.assertEqual(out["status"], "fail")


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        paths.set_login("9002")

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_roundtrip(self):
        paths.write_note("S1", "手痒", ["手痒"])
        notes = paths.read_notes()
        self.assertEqual(notes["S1"]["note"], "手痒")
        self.assertEqual(notes["S1"]["tags"], ["手痒"])

    def test_empty_note_removes_entry(self):
        paths.write_note("S1", "x")
        paths.write_note("S1", "", [])
        self.assertNotIn("S1", paths.read_notes())


class TestDeleteAccount(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        paths.set_login("9003")

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, login):
        d = paths.ensure_account_dir(login)
        (d / "deals.jsonl").write_text(
            json.dumps({"ticket": 1, "login": login}) + "\n"
            + json.dumps({"ticket": 2, "login": login}) + "\n",
            encoding="utf-8",
        )
        paths.touch_account({"login": login, "server": "X", "balance": 10})

    def test_soft_delete_moves_to_trash_and_drops_index(self):
        self._seed("9004")
        res = paths.delete_account("9004")
        self.assertTrue(res["ok"])
        self.assertEqual(res["deals"], 2)
        self.assertFalse(paths.account_dir("9004").exists())
        self.assertIn("9004", res["moved_to"])
        self.assertTrue(Path(res["moved_to"]).exists())
        self.assertNotIn("9004", paths.load_index())
        self.assertIn("移回", res["restore"])

    def test_refuses_current_account(self):
        self._seed("9005")
        res = paths.delete_account("9005", current="9005")
        self.assertFalse(res["ok"])
        self.assertIn("当前登录", res["error"])
        self.assertTrue(paths.account_dir("9005").exists())

    def test_refuses_unknown_account(self):
        res = paths.delete_account("9999")
        self.assertFalse(res["ok"])
        self.assertIn("没有归档目录", res["error"])

    def test_hard_delete_removes_directory(self):
        self._seed("9006")
        res = paths.delete_account("9006", hard=True)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["moved_to"])
        self.assertFalse(paths.account_dir("9006").exists())


class TestSessionAdviceGate(unittest.TestCase):
    """和账户级复盘同一条硬约束：不确认绝不发请求。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        paths.set_root(self.tmp)
        paths.set_login("9007")
        self.session = sessions.build([
            trade(1, T0, T0 + MIN, 1.0),
            trade(2, T0 + 5 * MIN, T0 + 6 * MIN, -1.0, reason="sl"),
        ])[0]

    def tearDown(self):
        paths.set_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_confirm_never_calls_llm(self):
        import agent.llm

        def boom(*a, **k):
            raise AssertionError("不允许在未确认时调用 LLM")

        orig = agent.llm.chat_ex
        agent.llm.chat_ex = boom
        try:
            from live.advisor import advise_session

            res = advise_session(self.session, allow_call=False)
        finally:
            agent.llm.chat_ex = orig
        self.assertTrue(res["need_confirm"])
        self.assertFalse(res["ok"])

    def test_normalize_trims_to_two_evidence(self):
        from live.advisor import _normalize_session_advice

        out = _normalize_session_advice({
            "headline": "x",
            "evidence": [{"claim": "a", "numbers": "1"}, {"claim": "b", "numbers": "2"},
                         {"claim": "c", "numbers": "3"}],
            "rule": {"metric": "trades", "value": 2, "text": "t"},
        })
        self.assertEqual(len(out["evidence"]), 2)
        self.assertEqual(out["rule"]["metric"], "trades")

    def test_prompt_contains_numbers_and_prev_session(self):
        from live.advisor import build_session_prompt

        prev = sessions.build([trade(9, T0 - 3600, T0 - 3000, 1.0)])[0]
        p = build_session_prompt(self.session, prev, {"note": "手痒了", "tags": ["手痒"]},
                                 {"rule": {"text": "最多 2 笔", "op": "<=", "value": 2,
                                           "label": "笔数", "metric": "trades"},
                                  "rule_check": {"status": "pass", "value": 2,
                                                 "label": "笔数", "target": "<= 2"}})
        self.assertIn("手痒了", p)
        self.assertIn("上一场", p)
        self.assertIn("最多 2 笔", p)
        self.assertIn("遵守了", p)
        self.assertIn("max_risk_pct", p)      # 指标表要喂给模型


if __name__ == "__main__":
    unittest.main()
