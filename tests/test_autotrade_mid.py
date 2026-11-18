"""mid（方向不明）这一档不做单（2026-11-18）。

依据写在 `Cfg.mid_trade` 上面：332 笔平仓流水里 mid 12 笔，胜率 25%、
每手 -145.7、58% 被硬止损，三档最差（trend +90.0 / range -46.6）；
而 peak 浮盈均值每手 +139 —— 有浮盈机会但捞不住，是"进去就挨打"。

用户定的口径：「mid 信号就别弹窗了，自动化交易也不要 mid」，并且**连模型
都不调**（mid 占决策轮次 26%，结论是确定性的，不必每分钟再问一遍）。

这里钉的是**行为**，不是实现：
  1. 空仓 + mid → 不调模型；
  2. 流水留一条可统计的 skip（veto=mid_no_trade），last_decision 指向"程序
     规则"，面板才分得清"模型不做"和"这一档不做"；
  3. 「连续未进场」与等待锚点一起清掉 —— 那不是模型的放弃，是程序不让做；
  4. 辅助模式同样不弹窗、不生成待确认信号；
  5. mid_trade=True 回到 11-18 之前的行为（留着开关是为了能重跑对比）；
  6. **mid 不许影响出场** —— 有持仓时闸门照跑，这是最要紧的一条。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from tests._autotrade_base import NOW, TempLedger, bars_from_closes

from live import autotrade, cfg as cfg_mod


def mid_rows():
    """ER=0.400、收盘落在区间 90% —— 稳稳落在 mid（0.30~0.50）。

    构造方式：每两格净涨 2、回撤 1（4000→4008，路径 18），净/路径 = 0.44 的
    量级；实数见 `_structure` 的 er 输出。故意避开 0.30 / 0.50 两条边界，
    免得测试因为浮点抖动在两档之间跳。
    """
    return bars_from_closes([4000, 4002, 4001, 4003, 4002, 4004, 4003,
                             4005, 4004, 4006, 4005, 4007, 4006, 4008])


class TestMidFixture(TempLedger):
    """先钉住夹具本身：这堆 K 线确实是 mid，否则下面全部用例都是空转。"""

    def test_fixture_really_is_mid(self):
        rows = mid_rows()
        s = autotrade._structure(rows)
        self.assertGreater(s["er"], autotrade.ER_RANGE_MAX)
        self.assertLess(s["er"], autotrade.ER_TREND_MIN)
        self.assertEqual(autotrade.regime_of(rows), "mid")


class TestMidNoTrade(TempLedger):
    def setUp(self):
        super().setUp()
        self.cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True)

    def _tick(self, cfg=None, **kw):
        """跑一轮 tick，返回 (行, ask_entry 的 mock)。"""
        with mock.patch.object(autotrade, "ask_entry",
                               return_value={"p_up": 0.7, "reason": "顺势",
                                             "source": "llm"}) as m:
            row = autotrade.tick(cfg or self.cfg, now=NOW,
                                 quote=(4008.0, 4008.24),
                                 rows=mid_rows(), positions=[], **kw)
        return row, m

    # ---- 1. 不调模型 ---------------------------------------------------
    def test_mid_does_not_call_model(self):
        row, m = self._tick()
        m.assert_not_called()
        self.assertEqual(row["kind"], "skip")
        self.assertEqual(row["veto"], "mid_no_trade")
        self.assertEqual(row["regime"], "mid")
        self.assertEqual(row["source"], "rule")
        self.assertIn("方向不明", row["reason"])
        # feat 仍要落盘：不然后事连"这一段到底是不是 mid"都查不回来
        self.assertTrue((row.get("feat") or {}).get("ok"))
        self.assertEqual(row["feat"]["regime"], "mid")

    # ---- 2. last_decision 要指向"程序规则" ------------------------------
    def test_last_decision_marks_rule_not_model(self):
        self._tick()
        ld = autotrade.load_state()["last_decision"]
        self.assertEqual(ld["regime"], "mid")
        self.assertIs(ld["act"], False)
        self.assertEqual(ld["source"], "rule")
        self.assertEqual(ld["veto"], "mid_no_trade")
        # 没调模型就没有 p_up，写成 0.5 会被读成"模型判中性"
        self.assertIsNone(ld["p_up"])

    # ---- 3. 「连续未进场」和锚点一起清 ---------------------------------
    def test_mid_clears_wait_and_skip_streak(self):
        st = autotrade.load_state()
        st["wait"] = {"level": 4002.0, "ts": NOW - 60, "side": 1, "drift": 0}
        st["skip_streak"] = {"n": 5, "ts": NOW - 300, "price": 4000.0}
        st["prev_entry"] = {"ts": NOW - 60, "bid": 4001.0,
                            "p_up": 0.4, "reason": "等回踩"}
        autotrade.save_state(st)
        self._tick()
        st = autotrade.load_state()
        # 不清的话，切回 range 的第一轮会背着一句"你已经连续 5 轮不做"
        # ——而那 5 轮里模型一次都没被问过
        self.assertIsNone(st["wait"])
        self.assertIsNone(st["skip_streak"])
        self.assertIsNone(st["prev_entry"])

    # ---- 4. 辅助模式不弹窗 ---------------------------------------------
    def test_mid_never_raises_popup_in_assist(self):
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True,
                            assist=True)
        with mock.patch.object(autotrade, "_notify_signal") as notify, \
             mock.patch.object(autotrade, "_await_human") as wait:
            row, m = self._tick(cfg)
        m.assert_not_called()
        notify.assert_not_called()
        wait.assert_not_called()
        self.assertIsNone(autotrade.load_state().get("pending_signal"))
        self.assertEqual(row["veto"], "mid_no_trade")

    # ---- 5. 开关打开 = 回到旧行为 --------------------------------------
    def test_mid_trade_switch_restores_old_behaviour(self):
        cfg = autotrade.Cfg(cooldown_sec=0, interval_sec=60, dry_run=True,
                            mid_trade=True)
        row, m = self._tick(cfg)
        m.assert_called_once()
        # 旧行为下这一轮会走到开仓（dry_run 只记纸面单），不再是那条 skip
        self.assertNotEqual(row.get("veto"), "mid_no_trade")

    # ---- 6. mid 不许影响出场（最要紧的一条）----------------------------
    def test_mid_does_not_block_exit_gate(self):
        """有仓时 mid 只是"不再开新仓"，硬止损照跑。

        早退分支插在**空仓段**里（持仓分支在它之前就 return 了）。这条用例
        守的就是这个位置：一旦有人把它挪到持仓分支之前，mid 期间浮亏的单子
        就没人管了——而 mid 恰好是来回扫、最容易打到止损的那一档。
        """
        st = autotrade.load_state()
        st["position"] = {
            "ticket": -1, "direction": 1, "lots": 0.01,
            "price_open": 4012.0, "opened_at": NOW - 120,
            "dry_run": True, "plan": "range", "atr": 3.0,
            "sl_broker": 0.0, "be_arm_ts": 0, "tp_broker": 0.0,
        }
        autotrade.save_state(st)
        row, m = self._tick()
        m.assert_not_called()          # 出场这条路本来就不问入场模型
        self.assertEqual(row["kind"], "close")
        self.assertEqual(row["source"], "sl")
        self.assertIsNone(autotrade.load_state().get("position"))


class TestMidPlaybook(TempLedger):
    """面板上的「打法」文案：mid 必须说"不交易"，不能说"只做位置明显的单"。"""

    def _pb(self, **cfg):
        st = {"cfg": cfg,
              "last_decision": {"regime": "mid", "ts": int(time.time())}}
        return autotrade.playbook_of(st, [], now=time.time())

    def test_says_no_trade(self):
        pb = self._pb()
        self.assertEqual(pb["name"], "方向不明")
        self.assertEqual(pb["action"], "不交易")
        # 这一档没有"等回踩"这回事，锚点指示灯不许亮
        self.assertFalse(pb["anchor"])

    def test_switch_back_shows_old_text(self):
        pb = self._pb(mid_trade=True)
        self.assertEqual(pb["action"], "只做位置明显的单")
        self.assertTrue(pb["anchor"])


class TestMidCfgOwnership(unittest.TestCase):
    """新字段必须有归属，且**不能被面板传进来**（cfg_from_start_body 直接报错）。"""

    def test_mid_trade_is_code_only(self):
        self.assertIn("mid_trade", cfg_mod.CODE_ONLY)
        self.assertNotIn("mid_trade", cfg_mod.PANEL_FIELDS)
        self.assertIs(cfg_mod.Cfg().mid_trade, False)

    def test_panel_cannot_push_it(self):
        with self.assertRaises(ValueError):
            cfg_mod.cfg_from_start_body({"mid_trade": True})


if __name__ == "__main__":
    unittest.main()
