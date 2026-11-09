"""开仓手数与金额阈值的契约（2026-11-09）。

背景
----
出场那一套数（sl_usd=3.00 / tp_min_usd=3.25 / be_arm_usd=3.25 …）是在
0.01 手下标定的，227 笔反事实回放扫出来的其实是**价距**，只是当时
0.01 手 XAUUSDm = 1 盎司，金额和价距恰好 1:1，于是被写成了金额。

手数可选之后，浮盈随手数线性放大。若金额阈值不动，止损价距会缩到 1/N：
0.10 手时 3.00 USD 只剩 0.30 价距，而单笔点差约 0.24 —— 开仓即被打掉，
回放证据也静默失效。所以金额必须等比缩放，把价距钉回原处。

这里守四件事：
1. 金额缩放、秒数/概率不缩放；`exit_params` 是唯一出口。
2. 不同手数下硬止损的**价格距离恒定**。
3. 缩放只在运行时派生，不写回 Cfg（CODE_ONLY 仍是本版默认值）。
4. 档位白名单前后端同源，API 绕不过去；面板显示的是实际生效值。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths, prefs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class TestMoneyScale(unittest.TestCase):
    def test_exit_params_scales_amounts_not_seconds(self):
        """金额等比放大，持仓上限是秒数、不该跟着变。"""
        base = autotrade.Cfg()
        for lots in prefs.LOTS:
            with self.subTest(lots=lots):
                cfg = autotrade.Cfg(lots=lots)
                scale = lots / autotrade.BASE_LOTS
                ep = autotrade.exit_params(cfg, "range")
                self.assertAlmostEqual(ep["sl_usd"], base.sl_usd * scale)
                self.assertAlmostEqual(ep["tp_min_usd"], base.tp_min_usd * scale)
                self.assertEqual(ep["max_hold_sec"], base.max_hold_sec)
                # 趋势单的宽止盈同样要缩放，否则趋势打法被悄悄削成震荡打法
                trend = autotrade.exit_params(cfg, "trend")
                self.assertAlmostEqual(trend["tp_min_usd"],
                                       base.tp_min_trend * scale)
                self.assertEqual(trend["sl_usd"], ep["sl_usd"])

    def test_sl_price_distance_is_invariant(self):
        """核心：不管选几手，硬止损对应的价格距离都是 3.00。

        mock 掉合约与账户系数，这里只验换算关系，不碰 MT5。
        """
        with mock.patch.object(autotrade, "_symbol_contract", return_value=100.0), \
             mock.patch.object(autotrade, "_acct_factor", return_value=1.0):
            for lots in prefs.LOTS:
                with self.subTest(lots=lots):
                    cfg = autotrade.Cfg(lots=lots)
                    pos = {"direction": 1, "lots": lots, "price_open": 4000.0}
                    dist = 4000.0 - autotrade._sl_price_for(cfg, pos)
                    self.assertAlmostEqual(dist, 3.0, places=6)

    def test_sl_price_ignores_position_lots(self):
        """持仓手数和 cfg 手数不是同一个数时，价距仍然只能是 3.00。

        复盘回放和换配置后的老仓位都会撞上这种错位。若按 pos["lots"] 算金额
        再按 pos["lots"] 除，0.03 手的单会被算成 1.00 价距——「平太早」的
        反事实结论整个反过来。
        """
        with mock.patch.object(autotrade, "_symbol_contract", return_value=100.0), \
             mock.patch.object(autotrade, "_acct_factor", return_value=1.0):
            for cfg_lots, pos_lots in ((0.01, 0.03), (0.05, 0.01), (0.02, 0.02)):
                with self.subTest(cfg_lots=cfg_lots, pos_lots=pos_lots):
                    cfg = autotrade.Cfg(lots=cfg_lots)
                    pos = {"direction": 1, "lots": pos_lots, "price_open": 4000.0}
                    self.assertAlmostEqual(4000.0 - autotrade._sl_price_for(cfg, pos),
                                           3.0, places=6)
                    # 空头同理
                    pos["direction"] = -1
                    self.assertAlmostEqual(autotrade._sl_price_for(cfg, pos) - 4000.0,
                                           3.0, places=6)

    def test_dd_limit_is_price_not_money(self):
        """反事实回撤线要和 high/low 比，口径是价格。

        金额跟着手数放大后，若把 15.00 当价距用，这条线永远碰不到，
        「平太早」的反事实校正就静默失效了——所有平仓又都会被判成太早。
        """
        with mock.patch.object(autotrade, "_symbol_contract", return_value=100.0), \
             mock.patch.object(autotrade, "_acct_factor", return_value=1.0):
            for lots in prefs.LOTS:
                with self.subTest(lots=lots):
                    cfg = autotrade.Cfg(lots=lots)
                    dd = autotrade.price_dist_of(cfg, cfg.tp_min_usd) / 2.0
                    self.assertAlmostEqual(dd, 1.625, places=6)
                    # 一笔平后回撤 1.70 → 必须命中 dd_risk（金额口径下 1.70 比 8 小得多）
                    bars = [{"low": 3998.30, "high": 4000.0}]
                    self.assertEqual(
                        autotrade.exit_counterfact(bars, 1, 0.0, 4000.0, dd_limit=dd),
                        "dd_risk")

    def test_guard_hit_uses_the_same_scaled_sl(self):
        """5 秒快查和 60 秒决策轮必须用同一个数，不能一处缩放一处不缩放。"""
        cfg = autotrade.Cfg(lots=0.05)
        pos = {"direction": 1, "lots": 0.05, "price_open": 4000.0}
        with mock.patch.object(autotrade, "_be_local_hit", return_value=False):
            self.assertEqual(autotrade._guard_hit(cfg, pos, 0, 0, fp=-10.0), "")
            hit = autotrade._guard_hit(cfg, pos, 0, 0, fp=-16.0)
        self.assertIn("-15.00", hit)

    def test_money_at_lots_leaves_probability_and_brake_alone(self):
        """只换算账户货币金额；概率、价距、刹车由调用方决定是否参与。"""
        cfg = autotrade.Cfg(lots=0.05)
        self.assertAlmostEqual(autotrade.money_at_lots(cfg, 3.25), 16.25)
        self.assertEqual(cfg.min_edge, autotrade.Cfg.min_edge)
        self.assertEqual(cfg.assist_max_slip_usd, autotrade.Cfg.assist_max_slip_usd)
        self.assertEqual(cfg.max_loss_usd, autotrade.Cfg.max_loss_usd)

    def test_scaling_does_not_rewrite_code_only_defaults(self):
        """缩放是运行时派生，不能把 6.00 写回 Cfg。

        写回去就等于 CODE_ONLY 跟着面板变了，test_cfg_ownership 那套
        「代码所有的字段只跟本版默认值」会被悄悄破坏，state 存档也会被污染。
        """
        cfg = autotrade.cfg_from_start_body({"lots": 0.05})
        self.assertEqual(cfg.lots, 0.05)
        for name in autotrade.CODE_ONLY:
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(cfg, name),
                    autotrade.Cfg.__dataclass_fields__[name].default)

    def test_prompt_unchanged_at_base_lots(self):
        """0.01 手时提示词逐字不变——不想为默认路径引入任何行为差异。"""
        with mock.patch.object(autotrade, "_acct_currency", return_value="USD"):
            out = autotrade.system_for(autotrade.EXIT_SYSTEM,
                                       autotrade.Cfg(), "range")
        self.assertNotIn("{LOT_RULE}", out)
        self.assertNotIn("本场开仓", out)
        self.assertIn("浮亏达到 3 USD", out)

    def test_prompt_explains_scaled_amounts(self):
        """手数变大后必须告诉模型金额放大了，但价距没变。"""
        with mock.patch.object(autotrade, "_acct_currency", return_value="USD"):
            out = autotrade.system_for(autotrade.EXIT_SYSTEM,
                                       autotrade.Cfg(lots=0.03), "range")
        self.assertNotIn("{LOT_RULE}", out)
        self.assertIn("0.03 手", out)
        self.assertIn("浮亏达到 9 USD", out)
        self.assertIn("价格距离仍分别是 3 / 3.25", out)


class TestLotWhitelist(unittest.TestCase):
    def test_validate_cfg_accepts_only_menu_lots(self):
        zero = "；".join(autotrade.validate_cfg(autotrade.Cfg(lots=0)))
        self.assertIn("手数必须大于 0", zero)
        for lots in prefs.LOTS:
            with self.subTest(lots=lots):
                issues = "；".join(autotrade.validate_cfg(autotrade.Cfg(lots=lots)))
                self.assertNotIn("开仓手数", issues)
        for bad in (0.001, 0.013, 0.1, 1.5):
            with self.subTest(bad=bad):
                issues = "；".join(autotrade.validate_cfg(autotrade.Cfg(lots=bad)))
                self.assertIn("开仓手数只接受", issues)

    def test_template_options_match_backend(self):
        """前端下拉、后端白名单、默认值必须同源。

        写死两份迟早漂移：界面上多一档，用户选了，启动才被 validate_cfg 拒。
        """
        html = (ROOT / "tools" / "ui" / "html" / "view-setup.html").read_text(
            encoding="utf-8")
        block = re.search(r'<select id="atLots".*?</select>', html, re.S)
        self.assertIsNotNone(block, "设置舱里找不到 #atLots 下拉")
        values = tuple(float(v) for v in re.findall(r'value="([\d.]+)"',
                                                    block.group()))
        self.assertEqual(values, prefs.LOTS)
        selected = re.findall(r'value="([\d.]+)"\s+selected', block.group())
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(float(selected[0]), prefs.DEFAULT_PREFS["lots"])
        self.assertAlmostEqual(prefs.DEFAULT_PREFS["lots"],
                               autotrade.Cfg.lots)

    def test_prefs_clean_rejects_unknown_lots(self):
        self.assertIsNone(prefs.clean_lots("一手"))
        self.assertIsNone(prefs.clean_lots(0.013))
        self.assertAlmostEqual(prefs.clean_lots(0.03), 0.03)
        self.assertTrue(prefs.valid_lots("0.05"))


class TestSummaryShowsEffectiveAmounts(unittest.TestCase):
    """面板参数表必须显示实际生效值，不能把存档里的 3.00 摆出来。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()

    def test_summary_scales_displayed_thresholds(self):
        cfg = autotrade.Cfg(lots=0.05)
        autotrade.save_state({"cfg": autotrade.cfg_to_dict(cfg)}, "999")
        with mock.patch.object(autotrade, "_acct_currency", return_value="USD"):
            d = autotrade.summary()
        self.assertAlmostEqual(d["sl_usd"], 15.0)
        self.assertAlmostEqual(d["tp_min_usd"], 16.25)
        self.assertAlmostEqual(d["tp_min_trend"], cfg.tp_min_trend * 5)
        self.assertAlmostEqual(d["be_arm_usd"], cfg.be_arm_usd * 5)
        self.assertAlmostEqual(d["cfg"]["lots"], 0.05)


if __name__ == "__main__":
    unittest.main()
