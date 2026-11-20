"""人工标签：这笔为什么进（2026-11-18）。

守四件事
--------
1. 标签按 ticket 存、和 state 无关——手工单不在 state 里，也要能标；
2. regime 与 tag **必须成套**（震荡单里没有「顺势突破」），
   否则半年后没人能判断两种写法是不是同一件事，统计价值直接归零；
3. 平仓流水带一份**快照**（`趋势单 · 顺势突破`），不是键——标签表可改，
   流水是历史；
4. `summary` 下发给人看的 label，但**绝不写回 state**：
   `state["position"]` 是出场闸门的输入，多一个与交易无关的字段就是噪音。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger

from live import autotrade, labels, paths


def _pos(**over) -> dict:
    pos = {
        "ticket": 501, "direction": 1, "lots": 0.1, "price_open": 4000.0,
        "opened_at": NOW - 120, "p_up": 0.61, "entry_reason": "突破",
        "plan": "trend", "atr": 1.2, "peak_pnl": 3.0, "peak_ts": NOW - 40,
        "be_ok": False, "dry_run": False,
    }
    pos.update(over)
    return pos


class LabelStore(TempLedger):
    def test_roundtrip_and_text(self):
        item = labels.set_label(501, "trend", "顺势突破", "999")
        self.assertEqual(item["regime"], "trend")
        self.assertEqual(item["tag"], "顺势突破")
        got = labels.get_label(501, "999")
        self.assertEqual(got["tag"], "顺势突破")
        # 流水里那一格是人话，不是 regime/tag 两个键
        self.assertEqual(labels.label_of(501, "999"), "趋势单 · 顺势突破")

    def test_ticket_is_normalised_to_str_key(self):
        """JSON 键恒为字符串：int/str 两种入参必须落到同一格。"""
        labels.set_label(501, "range", "下沿多", "999")
        self.assertIsNotNone(labels.get_label("501", "999"))
        self.assertEqual(list(labels.load_labels("999")), ["501"])

    def test_regime_and_tag_must_match(self):
        """震荡单里没有「顺势突破」。允许自由文本 = 这一层白做。"""
        with self.assertRaises(ValueError):
            labels.set_label(501, "range", "顺势突破", "999")
        with self.assertRaises(ValueError):
            labels.set_label(501, "trend", "下沿多", "999")
        with self.assertRaises(ValueError):
            labels.set_label(501, "scalp", "顺势突破", "999")
        with self.assertRaises(ValueError):
            labels.set_label("abc", "trend", "顺势突破", "999")
        # 一条都不该落盘
        self.assertEqual(labels.load_labels("999"), {})

    def test_overwrite_and_clear(self):
        labels.set_label(501, "trend", "顺势突破", "999")
        labels.set_label(501, "range", "假突破", "999")
        self.assertEqual(labels.get_label(501, "999")["tag"], "假突破")
        self.assertTrue(labels.clear_label(501, "999"))
        self.assertIsNone(labels.get_label(501, "999"))
        self.assertFalse(labels.clear_label(501, "999"))

    def test_options_come_from_one_place(self):
        """面板拿到的选项必须就是校验用的那张表。

        前端写死一份、后端校验另一份，加一个标签要改两处，漏一处就是
        「界面能选、保存报 400」。这条钉住下发与校验同源。
        """
        opt = labels.tag_options()
        self.assertEqual(opt["tags"], labels.TAGS)
        for regime, tags in opt["tags"].items():
            for t in tags:
                # 每一项都必须能真的存进去
                labels.set_label(900 + len(t), regime, t, "999")

    def test_no_label_means_empty_text(self):
        """没有标签时流水那一格是空串，不是 None/`—`。

        空串让统计能直接用 `if row.get("label")` 分开「有归因」和「没归因」，
        写 `—` 就得在每个消费点再判一次。
        """
        self.assertEqual(labels.label_of(777, "999"), "")


class CloseRowCarriesLabel(TempLedger):
    def test_book_close_snapshot(self):
        labels.set_label(501, "trend", "回踩续势", "999")
        state = autotrade._empty_state()
        row = autotrade._book_close(state, _pos(), 1.5, NOW,
                                    source="llm", reason="走", login="999")
        self.assertEqual(row["label"], "趋势单 · 回踩续势")
        # 快照而不是键：改标签不能改历史流水
        labels.set_label(501, "range", "上沿空", "999")
        self.assertEqual(row["label"], "趋势单 · 回踩续势")

    def test_book_close_without_label_is_empty(self):
        state = autotrade._empty_state()
        row = autotrade._book_close(state, _pos(), 1.5, NOW,
                                    source="pending", reason="x", login="999")
        self.assertEqual(row["label"], "")

    def test_manual_close_row_carries_label(self):
        labels.set_label(601, "range", "下沿多", "999")
        sh = {"ticket": 601, "direction": 1, "lots": 0.1, "price_open": 4000.0,
              "opened_at": NOW - 300, "peak_pnl": 8.0, "peak_ts": NOW - 60,
              "risk0": -30.0, "sl0": 3997.0}
        fills = {"time": NOW, "price": 4005.0, "pnl": 5.0}
        with mock.patch.object(autotrade, "_close_fills", return_value=fills):
            row = autotrade._manual_close_row(sh, NOW, "999")
        self.assertEqual(row["label"], "震荡单 · 下沿多")


class SummaryWiring(TempLedger):
    def test_position_label_is_a_copy_not_state(self):
        labels.set_label(501, "trend", "加速中继", "999")
        state = autotrade._empty_state()
        state["position"] = _pos()
        autotrade.save_state(state, "999")

        out = autotrade.summary("999")
        self.assertEqual(out["position"]["label"]["tag"], "加速中继")
        # 下发的是副本：state 里那笔仓位不许被加上 label 键
        self.assertNotIn("label", autotrade.load_state("999")["position"])

    def test_label_tags_are_published(self):
        out = autotrade.summary("999")
        self.assertEqual(out["label_tags"]["tags"], labels.TAGS)
        self.assertEqual(out["label_tags"]["regime_txt"]["trend"], "趋势单")

    def test_file_lives_in_account_dir(self):
        labels.set_label(501, "trend", "顺势突破", "999")
        p = labels.labels_path("999")
        self.assertEqual(p.parent, paths.account_dir("999"))
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["501"]["tag"],
                         "顺势突破")


class LabelReachesReview(TempLedger):
    """标签必须能一路走到复盘（2026-11-20）。

    用户原话：「我最近做手工单都打了标签，在复盘好像没看到相关信息」。
    根因不是没存——`manual_close` / `close` 行里一直带着 `label` 快照——而是
    复盘台读的是**券商账本**（`journal.build_trades`），和按 ticket 存的标签表
    之间从来没有 join；AI 复盘的提示词也一个字都没读到。标了没人看，等于没标。
    """

    def test_label_map_batches_and_formats(self):
        labels.set_label(501, "trend", "顺势突破", "999")
        labels.set_label(602, "range", "下沿多", "999")
        m = labels.label_map("999")
        self.assertEqual(m, {"501": "趋势单 · 顺势突破", "602": "震荡单 · 下沿多"})
        # 键必须是字符串：前端拿 String(position_id) 去对（trade_notes 同一约定）
        self.assertTrue(all(isinstance(k, str) for k in m))
        self.assertEqual(labels.label_map("404"), {})

    def test_prompt_carries_label(self):
        from live import advisor

        labels.set_label(501, "trend", "逆势反抽", "999")
        trades = [{"position_id": 501, "symbol": "XAUUSDc", "direction": 1,
                   "volume": 0.1, "profit": 12.0, "entry_price": 4000.0,
                   "exit_price": 4012.0, "entry_time": NOW - 600,
                   "exit_time": NOW, "close_reason": "manual"}]
        stats = {"account": {}, "by_direction": [], "by_reason": [],
                 "by_hour": [], "by_weekday": [], "daily": [], "count": 1}
        txt = advisor.build_prompt(stats, [], trades, None, {},
                                   labels.label_map("999"))
        self.assertIn("标签: 趋势单 · 逆势反抽", txt)
        self.assertIn("1 笔打了打法标签", txt)

    def test_cache_key_invalidates_on_new_label(self):
        """补一个标签就该重新复盘——否则人标完，看到的还是旧结论。"""
        from live import advisor

        trades = [{"position_id": 501, "exit_time": NOW}]
        k1 = advisor._cache_key({}, trades, {}, {})
        k2 = advisor._cache_key({}, trades, {}, {"501": "趋势单 · 顺势突破"})
        k3 = advisor._cache_key({}, trades, {}, {"501": "震荡单 · 下沿多"})
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k2, k3)
        # 同一份标签必须稳定：跨进程也不能漂（不能用内置 hash()）
        self.assertEqual(k2, advisor._cache_key({}, trades, {}, {"501": "趋势单 · 顺势突破"}))


if __name__ == "__main__":
    unittest.main()
