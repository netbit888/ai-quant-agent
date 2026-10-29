"""统计与特征：日汇总、账户评分、ATR / 特征快照、品种与配置。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import (
    NOW, TempLedger, bars_up,
)


class TestSummary(TempLedger):
    def test_reports_direction_skew(self):
        """只做多这种偏置，看盈亏是发现不了的，必须单独统计多空。"""
        for i in range(9):
            autotrade.append_log({"ts": NOW + i, "kind": "close",
                                  "direction": 1, "pnl": -0.2})
        autotrade.append_log({"ts": NOW + 20, "kind": "close",
                              "direction": -1, "pnl": 0.3})
        d = autotrade.summary()["dir_recent"]
        self.assertEqual((d["n"], d["long"], d["short"]), (10, 9, 1))
        self.assertTrue(d["skew"])

    def test_hit_rate_separates_llm_from_fallback(self):
        rows = [
            {"ts": NOW, "kind": "open", "direction": 1, "p_up": 0.7, "source": "llm"},
            {"ts": NOW + 60, "kind": "close", "pnl": 1.0, "source": "llm"},
            {"ts": NOW + 120, "kind": "open", "direction": -1, "p_up": 0.3, "source": "llm"},
            {"ts": NOW + 180, "kind": "close", "pnl": -0.5, "source": "llm"},
            {"ts": NOW + 240, "kind": "open", "direction": 1, "p_up": 0.6, "source": "fallback"},
            {"ts": NOW + 300, "kind": "close", "pnl": -1.0, "source": "fallback"},
        ]
        for r in rows:
            autotrade.append_log(r)

        s = autotrade.summary()
        self.assertEqual(s["closed"], 3)
        self.assertEqual(s["wins"], 1)
        self.assertAlmostEqual(s["hit_rate"], 1 / 3, places=4)
        # 只算模型自己决定的成交：1 胜 1 负 = 50%
        self.assertEqual(s["llm_closes"], 2)
        self.assertAlmostEqual(s["llm_hit_rate"], 0.5, places=4)

    def test_empty_log_has_no_hit_rate(self):
        s = autotrade.summary()
        self.assertEqual(s["closed"], 0)
        self.assertIsNone(s["hit_rate"])

    def test_log_survives_corrupt_line(self):
        autotrade.append_log({"ts": NOW, "kind": "skip", "reason": "x"})
        p = autotrade.log_path()
        with p.open("a", encoding="utf-8") as fh:
            fh.write("{坏行\n")
        self.assertEqual(len(autotrade.read_log()), 1)


class TestSymbolSupport(unittest.TestCase):
    """多品种：BTCUSDm 上线，价格精度必须跟着品种走。

    背景：以前黄金写死 3 位小数、启动请求里写死 symbol='XAUUSDm'。
    加第二个品种时这两处都会静默失效——BTC 会显示 80185.810，
    前端选了下拉框也永远只发黄金。
    """

    def test_btc_spec_is_measured(self):
        """规格必须来自实测，不能拍：point/contract 读 symbol_info，
        spread 是 tick 中位数，atr 是 H4 ATR(14) 中位数。"""
        from backtest.symbols import SYMBOLS

        s = SYMBOLS["BTCUSDm"]
        self.assertEqual(s["point"], 0.01)
        self.assertEqual(s["contract"], 1.0)
        self.assertEqual(s["digits"], 2)
        self.assertEqual(s["spread"], 640)
        self.assertEqual(s["atr_pts"], 82706)
        # 成本占 ATR 的比例必须远低于 15% 的排除线，否则不该上实盘
        self.assertLess(s["spread"] / s["atr_pts"], 0.05)

    def test_btc_in_whitelist_and_unknown_rejected(self):
        self.assertIn("BTCUSDm", autotrade.TRADABLE_SYMBOLS)
        self.assertIn("XAUUSDm", autotrade.TRADABLE_SYMBOLS)
        self.assertEqual(autotrade.validate_cfg(autotrade.Cfg(symbol="BTCUSDm")), [])
        # 没实测过的品种必须被挡：点差成本会退回 0，把"没测过"显示成"零成本"
        issues = autotrade.validate_cfg(autotrade.Cfg(symbol="EURUSDm"))
        self.assertTrue(any("白名单" in i for i in issues), issues)

    def test_symbol_options_only_whitelisted(self):
        """面板下拉框的数据源必须等于后端白名单，前端不许另存一份。"""
        opts = autotrade.symbol_options()
        self.assertEqual([o["symbol"] for o in opts],
                         list(autotrade.TRADABLE_SYMBOLS))
        for o in opts:
            self.assertTrue(o["label"])
            self.assertTrue(o["digits"])
            self.assertIsNotNone(o["point"])

    def test_price_precision_follows_symbol(self):
        self.assertEqual(autotrade._px("BTCUSDm", 80185.812), "80185.81")
        self.assertEqual(autotrade._px("BTCUSDm", 80185.0), "80185.00")
        self.assertEqual(autotrade._px("XAUUSDm", 4024.1394), "4024.139")
        self.assertEqual(autotrade._digits("BTCUSDm"), 2)
        self.assertEqual(autotrade._digits("XAUUSDm"), 3)
        # 未知品种退回 3 位，不抛异常（不能让一个品种名把 prompt 构建搞崩）
        self.assertEqual(autotrade._digits("WHATEVER"), 3)

    def test_entry_prompt_btc_two_decimals(self):
        rows = [{"time": NOW + i * 60, "open": 80000.0 + i * 10,
                 "high": 80050.0 + i * 10, "low": 79950.0 + i * 10,
                 "close": 80000.0 + i * 10} for i in range(14)]
        p = autotrade.build_entry_prompt(rows, 80185.81, 80192.21,
                                        symbol="BTCUSDm", point=0.01)
        self.assertIn("80185.81", p)          # 2 位，不是 80185.810
        self.assertIn("点差=640 点", p)        # (80192.21-80185.81)/0.01
        self.assertNotIn("80185.810", p)
        self.assertIn("品种 BTCUSDm", p)

    def test_btc_spread_cost_scales_with_contract(self):
        """0.01 手 BTC 的点差成本 = 640 × 0.01 × 0.01 × 1 = 0.064 USD，
        黄金 = 240 × 0.001 × 0.01 × 100 = 0.24 USD。公式必须同一个。"""
        c_btc = autotrade._spread_cost("BTCUSDm", 0.01)
        c_xau = autotrade._spread_cost("XAUUSDm", 0.01)
        self.assertAlmostEqual(c_btc, 0.064, places=6)
        self.assertAlmostEqual(c_xau, 0.24, places=6)


class TestAtr(unittest.TestCase):
    def test_gap_counts_as_true_range(self):
        """跳空必须计入 TR。只算 high-low 会严重低估波动。

        构造：第 1 根收 100，第 2 根直接在 110 开盘、高低只差 1。
        看高低差会得到 1；真实 TR 应该是 |111-100| = 11。
        """
        rows = [
            {"time": NOW, "open": 100, "high": 101, "low": 99, "close": 100},
            {"time": NOW + 60, "open": 110, "high": 111, "low": 110, "close": 110},
        ]
        a = autotrade._atr(rows)
        self.assertTrue(a["ok"])
        self.assertEqual(a["n_tr"], 1)          # 2 根只能出 1 个 TR
        self.assertAlmostEqual(a["atr"], 11.0, places=6)

    def test_known_series(self):
        """bars_up 每根 high-low 恒为 2、前收差 1.5，所以 TR 恒 2、ATR=2。

        14 根极差 = (4001+13) - 3999 = 15，比值 7.5。
        这个数一旦变了，说明 TR 公式或窗口被动过。
        """
        a = autotrade._atr(bars_up(14))
        self.assertAlmostEqual(a["atr"], 2.0, places=6)
        self.assertAlmostEqual(a["span"], 15.0, places=6)
        self.assertAlmostEqual(a["span_atr"], 7.5, places=6)

    def test_ratio_is_dimensionless(self):
        """比值必须无量纲：整体价格乘 10，比值一个字节都不该变。

        这是它能跨品种（黄金 vs BTC）、跨时段（亚洲盘 vs 纽约盘）比较的前提。
        """
        base = bars_up(14)
        scaled = [{**r, "open": r["open"] * 10, "high": r["high"] * 10,
                   "low": r["low"] * 10, "close": r["close"] * 10} for r in base]
        self.assertAlmostEqual(autotrade._atr(base)["span_atr"],
                               autotrade._atr(scaled)["span_atr"], places=6)

    def test_degenerate_inputs(self):
        self.assertFalse(autotrade._atr([])["ok"])
        self.assertFalse(autotrade._atr(bars_up(1))["ok"])
        self.assertEqual(autotrade._atr(bars_up(1))["atr"], 0.0)

    def test_structure_line_exposes_the_ratio(self):
        text = autotrade._structure_text(bars_up(14))
        self.assertIn("倍单根均幅", text)
        self.assertIn("7.5 倍", text)


class TestFeatureSnapshot(unittest.TestCase):
    def test_all_fields_for_offline_analysis(self):
        """快照必须自带分析所需的全部维度，否则落盘等于白落。"""
        snap = autotrade.feature_snapshot(bars_up(20), autotrade.Cfg())
        for key in ("ok", "symbol", "tf", "n", "win", "er", "drift", "pos",
                    "span", "streak", "path", "r1", "r5", "r13",
                    "atr", "span_atr", "regime", "prog_dir"):
            self.assertIn(key, snap)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["n"], 14)
        self.assertEqual(snap["symbol"], "XAUUSDm")
        self.assertEqual(snap["regime"], "trend")     # 一路上涨

    def test_window_bounds_are_the_real_14(self):
        """win 记的是实际用到的窗口，事后核对「这笔决策看的哪 14 分钟」靠它。"""
        rows = bars_up(40)
        snap = autotrade.feature_snapshot(rows, autotrade.Cfg())
        self.assertEqual(snap["win"], [rows[-14]["time"], rows[-1]["time"]])

    def test_values_are_rounded(self):
        """日志是给机器算也是给人看的，长尾小数只会把文件撑大。"""
        snap = autotrade.feature_snapshot(bars_up(14), autotrade.Cfg())
        for key in ("er", "pos", "span_atr"):
            self.assertLessEqual(len(str(snap[key])), 8, key)

    def test_degenerate_rows(self):
        self.assertFalse(autotrade.feature_snapshot([])["ok"])
        self.assertFalse(autotrade.feature_snapshot(bars_up(1))["ok"])

    def test_mtf_info_is_embedded(self):
        snap = autotrade.feature_snapshot(
            bars_up(14), autotrade.Cfg(),
            mtf={"tf": "M5", "er": 0.6, "pos": 0.8, "regime": "单边上行"})
        self.assertEqual(snap["mtf"]["regime"], "单边上行")


class TestFeatureSnapshotLands(TempLedger):
    """快照必须真的写进日志——算出来不落盘，等于没算。"""

    def test_open_row_carries_snapshot(self):
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "act": True,
                                             "agree": True, "reason": "结构支持",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "send_open",
                               return_value={"ok": True, "order": 7, "price": 4040.0}), \
             mock.patch.object(autotrade, "_mtf_bundle",
                               return_value={"text": "", "info": None}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True, act_guard=False,
                                               top_long_ban=False), now=NOW,
                                 quote=(4040.0, 4040.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "open")
        feat = row["feat"]
        self.assertTrue(feat["ok"])
        self.assertEqual(feat["n"], 14)
        self.assertIn("er", feat)
        self.assertIn("span_atr", feat)

    def test_skip_row_carries_snapshot(self):
        """被闸门拦掉的单也要留特征——否则没法回答「min_edge 是不是误杀」。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.52, "act": True,
                                             "agree": True, "reason": "轻微看多",
                                             "source": "llm"}), \
             mock.patch.object(autotrade, "_mtf_bundle",
                               return_value={"text": "", "info": None}):
            row = autotrade.tick(autotrade.Cfg(dry_run=True), now=NOW,
                                 quote=(4040.0, 4040.24), rows=bars_up(),
                                 positions=[])
        self.assertEqual(row["kind"], "skip")
        self.assertIn("信号不够强", row["reason"])
        self.assertTrue(row["feat"]["ok"])


class TestMtfBackground(unittest.TestCase):
    def test_degrades_silently_when_terminal_down(self):
        """终端没连时不给背景，也不写"不可用"——那会让模型去想缺了什么。"""
        with mock.patch.object(autotrade, "_bars", side_effect=RuntimeError("no mt5")):
            b = autotrade._mtf_bundle("XAUUSDm")
        self.assertEqual(b["text"], "")
        self.assertIsNone(b["info"])

    def test_too_few_bars_gives_nothing(self):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(3)):
            self.assertEqual(autotrade._mtf_bundle("XAUUSDm")["text"], "")

    def test_one_line_with_er_no_raw_bars(self):
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(20)):
            b = autotrade._mtf_bundle("XAUUSDm", 0.35, 3)
        self.assertTrue(b["text"].startswith("【背景】M5 14 根"))
        self.assertIn("ER=", b["text"])
        self.assertEqual(b["text"].count("\n"), 0)      # 只有一行，没有 K 线表
        self.assertNotIn("T-13", b["text"])
        self.assertEqual(b["info"]["tf"], "M5")
        self.assertEqual(b["info"]["n"], 14)

    def test_prompt_bar_count_unchanged(self):
        """加了 M5 背景后，M1 的 K 线行数必须还是 14——边界没有松动。"""
        with mock.patch.object(autotrade, "_bars", return_value=bars_up(20)):
            mtf = autotrade._mtf_text("XAUUSDm")
        p = autotrade.build_entry_prompt(bars_up(20), 4040.0, 4040.24,
                                         mtf_text=mtf)
        lines = re.findall(r"^(?:T-\d+|当前)\s+\d{2}-\d{2}", p, re.M)
        self.assertEqual(len(lines), 14)
        self.assertIn("【背景】M5", p)
        self.assertNotIn("T-14", p)


class TestSystemFollowsCfg(unittest.TestCase):
    def test_no_hardcoded_symbol_in_templates(self):
        """三个 system 模板里不许再出现具体品种名（加 BTC 时漏改过的 bug）。"""
        for name in ("ENTRY_SYSTEM", "EXIT_SYSTEM", "REVIEW_SYSTEM"):
            tmpl = getattr(autotrade, name)
            self.assertIn("{SYMBOL_LABEL}", tmpl, name)
            self.assertNotIn("XAUUSD", tmpl, name)

    def test_gold_renders_gold(self):
        text = autotrade.system_for(autotrade.ENTRY_SYSTEM, autotrade.Cfg())
        self.assertIn("黄金 XAUUSD", text)
        self.assertNotIn("{SYMBOL_LABEL}", text)

    def test_btc_renders_btc(self):
        """选 BTC 时不能再告诉模型"你是黄金交易员"。

        只检查开头那行自我介绍。正文里"黄金实盘口径"是标注实测来源的，
        留着才对——它提醒模型这个数字是别的品种跑出来的。
        """
        cfg = autotrade.Cfg(symbol="BTCUSDm")
        for name in ("ENTRY_SYSTEM", "EXIT_SYSTEM", "REVIEW_SYSTEM"):
            first = autotrade.system_for(getattr(autotrade, name), cfg).splitlines()[0]
            self.assertIn("BTCUSD", first, name)
            self.assertNotIn("XAUUSD", first, name)
            self.assertNotIn("黄金", first, name)

    def test_exit_thresholds_follow_cfg(self):
        """出场阈值同理：cfg 改了 system 必须跟着改，否则就是在骗模型。"""
        cfg = autotrade.Cfg(sl_usd=5.0, tp_min_usd=4.5, max_hold_sec=1800)
        text = autotrade.system_for(autotrade.EXIT_SYSTEM, cfg)
        self.assertIn("浮亏达到 5 USD", text)
        self.assertIn("浮盈不到 4.5 USD", text)
        self.assertIn("持仓满 30 分钟", text)
        self.assertNotIn("{SL_USD}", text)
        self.assertNotIn("{TP_MIN_USD}", text)
        self.assertNotIn("{MAX_HOLD_MIN}", text)

    def test_unknown_symbol_falls_back_to_code(self):
        text = autotrade.system_for(autotrade.ENTRY_SYSTEM,
                                    autotrade.Cfg(symbol="WHATEVER"))
        self.assertIn("WHATEVER", text)


class TestEntrySystemExample(unittest.TestCase):
    """输出示例里的 p_up 不能是会被模型照抄的占位值。

    实测 193 笔里 89 笔（46.1%）的 p_up 精确等于 0.0，**且这 89 笔全是空单**
    —— 根因是 ENTRY_SYSTEM 的 JSON 示例写着 `"p_up": 0.00`，与正文
    「只在 0.20 ~ 0.80 之间取值」直接冲突。模型按示例填，程序按
    `p_up > 0.5 else -1` 读成「最强看空」，于是开空单。
    clamp_p_up 只是把它压成 0.20（仍是极端看空），日志里再也看不到 0.0，
    等于把 bug 藏起来了 —— 所以这里必须直接断言**示例原文**，不能经 clamp。
    """

    def _example(self):
        m = re.search(r'\{"act".*?\}', autotrade.ENTRY_SYSTEM)
        self.assertIsNotNone(m, "ENTRY_SYSTEM 里找不到 JSON 输出示例")
        return json.loads(m.group(0))

    def test_example_p_up_inside_stated_range(self):
        """示例值必须落在正文声明的区间里（不走 parse_p_up，否则会被 clamp 洗白）。"""
        v = float(self._example()["p_up"])
        self.assertGreaterEqual(v, autotrade.P_UP_FLOOR)
        self.assertLessEqual(v, autotrade.P_UP_CEIL)

    def test_example_p_up_is_not_a_template_value(self):
        """0 / 0.5 / 1 都不该做示例：要么会被照抄，要么示例本身就在示范弃权。"""
        self.assertNotIn(float(self._example()["p_up"]), (0.0, 0.5, 1.0))

    def test_example_tells_model_not_to_copy(self):
        self.assertIn("只是格式示范", autotrade.ENTRY_SYSTEM)

    def test_stated_range_matches_clamp(self):
        """正文写的区间必须和 clamp_p_up 的常量同源，否则又是一处自相矛盾。"""
        self.assertIn(f"{autotrade.P_UP_FLOOR:.2f} ~ {autotrade.P_UP_CEIL:.2f}",
                      autotrade.ENTRY_SYSTEM)


class TestSummaryFloatPnl(TempLedger):
    """面板顶部常驻状态带要显示「这笔单现在赚还是亏」，数据来自 summary。

    为什么必须由后端算：券商的浮动盈亏是整个账户的（含手动单），
    和 AI 这一笔对不上；前端又拿不到合约乘数，自己算必然错。
    """

    def _open(self, price_open=4000.0, direction=1):
        state = autotrade._empty_state()
        state["cfg"] = {"symbol": "XAUUSDm", "dry_run": True}
        state["position"] = {"ticket": -1, "direction": direction, "lots": 0.01,
                             "price_open": price_open, "opened_at": NOW - 30,
                             "p_up": 0.6, "entry_reason": "突破", "dry_run": True}
        autotrade.save_state(state)

    def test_float_pnl_is_computed_from_live_quote(self):
        self._open(direction=1)
        with mock.patch.object(autotrade, "_quote", return_value=(4001.5, 4001.74)):
            s = autotrade.summary()
        self.assertEqual(s["float_pnl"], 1.5)      # (4001.5-4000)*100*0.01

    def test_short_uses_ask(self):
        """空单平在 ask：拿 bid 算会白送 0.24 的点差利润（-1.26 而不是 -1.50）。"""
        self._open(direction=-1)
        with mock.patch.object(autotrade, "_quote", return_value=(4001.26, 4001.5)):
            s = autotrade.summary()
        self.assertEqual(s["float_pnl"], -1.5)     # -(4001.5-4000)*1.0

    def test_no_quote_means_none_not_zero(self):
        """拿不到报价就给 None。返回 0.0 会被当成「正好保本」，那是假数据。"""
        self._open()
        with mock.patch.object(autotrade, "_quote", return_value=None):
            s = autotrade.summary()
        self.assertIsNone(s["float_pnl"])

    def test_flat_account_has_no_float_pnl(self):
        autotrade.save_state(autotrade._empty_state())
        with mock.patch.object(autotrade, "_quote", return_value=(4000.0, 4000.24)):
            self.assertIsNone(autotrade.summary()["float_pnl"])

    def test_summary_exposes_guard_sec(self):
        """面板靠这个字段决定「止损快查」胶囊显示「每 N 秒」还是「关闭」。"""
        state = autotrade._empty_state()
        state["cfg"] = {"guard_sec": 7.5}
        autotrade.save_state(state)
        self.assertEqual(autotrade.summary()["guard_sec"], 7.5)
        # 旧 state 没有这个字段 → 回退到当前默认值，不能是 None
        state["cfg"] = {}
        autotrade.save_state(state)
        self.assertEqual(autotrade.summary()["guard_sec"], autotrade.Cfg().guard_sec)


class TestSummaryTodayScope(TempLedger):
    """「今日累计」必须真的是今天。

    踩过的坑：面板和幕布原来用 `state["pnl"]` 当"今日"，那是**本进程累计**——
    点一次「重置战绩」就归零，跨午夜也不会归零。标题写"今日"就得按自然日算。
    """

    def _close(self, ts, pnl, source="llm"):
        autotrade.append_log({"ts": ts, "kind": "close", "ticket": 1,
                              "direction": 1, "pnl": pnl, "source": source,
                              "held_sec": 60})

    def test_today_scope_is_local_date_not_process_total(self):
        now = int(autotrade.time.time())
        self._close(now, 3.0)
        self._close(now + 1, -3.42, source="sl")
        self._close(now - 86400, 5.0)          # 昨天那笔不该算进今日

        s = autotrade.summary()
        self.assertEqual(s["closed"], 3)       # 全量仍是 3 笔
        self.assertEqual(s["closed_today"], 2)
        self.assertEqual(s["wins_today"], 1)
        self.assertEqual(s["losses_today"], 1)
        self.assertAlmostEqual(s["pnl_today"], -0.42, places=2)

    def test_wins_plus_losses_equals_closed_today(self):
        now = int(autotrade.time.time())
        for i, pnl in enumerate([1.0, -1.0, 2.0, -3.0]):
            self._close(now + i, pnl)
        s = autotrade.summary()
        self.assertEqual(s["wins_today"] + s["losses_today"], s["closed_today"])
        self.assertEqual(s["wins_today"], 2)

    def test_hit_rate_excludes_stop_losses(self):
        """两个口径并存，含义完全不同，别混用。

        `llm_hit_rate` 的分母只算模型主动平的，硬止损不进——所以它天生接近
        100%，**不能当胜率用**。
        `hit_rate` 的分母是全部平仓单，止损照样算——面板卡片显示的是这个
        （用户 10-26 直接问"硬止损为什么不算"，这就是答案：以前真没算）。
        """
        now = int(autotrade.time.time())
        self._close(now, 3.0)
        self._close(now + 1, -3.42, source="sl")
        s = autotrade.summary()
        self.assertEqual(s["llm_closes"], 1)
        self.assertEqual(s["llm_hit_rate"], 1.0)   # 单看模型平仓是 100%
        self.assertEqual(s["hit_rate"], 0.5)       # 真实胜率 50%，这才是给人看的
        self.assertEqual(s["sl_closes"], 1)


class TestBackfillSlSrc(TempLedger):
    """老教训没有 src 字段：按流水的 close 行回填，救回被锁在出场端的那批。"""

    def _seed(self, source, scope="exit"):
        autotrade.append_log({"ts": NOW, "kind": "close", "ticket": 7,
                              "direction": 1, "pnl": -3.0, "source": source})
        autotrade.append_lesson({"ts": NOW, "ticket": 7, "lesson": "接刀后破位",
                                 "theme": "止损太晚", "scope": scope,
                                 "regime": "range"})

    def test_sl_closed_ticket_gets_marked(self):
        self._seed("sl")
        res = autotrade.backfill_sl_src()
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["changed"], 1)
        rows = autotrade.lesson_rows()
        self.assertEqual(rows[0]["src"], "sl")
        self.assertEqual(rows[0]["scope"], "both")   # 出场端锁死的话标记了也没用
        self.assertIn("接刀后破位", autotrade.recent_lessons(scope="entry"))

    def test_idempotent(self):
        self._seed("sl")
        self.assertEqual(autotrade.backfill_sl_src()["changed"], 1)
        self.assertEqual(autotrade.backfill_sl_src()["changed"], 0)

    def test_model_closed_ticket_untouched(self):
        """模型自己平的仓，出场是它的决策，不许被回填成 sl。"""
        self._seed("llm")
        self.assertEqual(autotrade.backfill_sl_src()["changed"], 0)
        self.assertFalse(autotrade.lesson_rows()[0].get("src"))

    def test_no_lessons_file(self):
        self.assertFalse(autotrade.backfill_sl_src()["ok"])
