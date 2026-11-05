"""启动参数的归属：哪些面板能改、哪些只能跟代码走。

为什么单开一条（2026-11-05）
--------------------------
`/api/autotrade/start` 原先手写映射 14 个字段，而 `Cfg` 有 49 个。剩下的既
不在映射里、也没有任何地方声明，落在**没人管的地带**：调不动，也不报错。
更糟的是那份清单靠人肉跟 `Cfg` 同步——加字段的人不会知道有这么个清单，
新字段于是静静掉进同一个洞，表现是"改了没反应"。

这里守三件事：
1. 每个字段都有归属。加了字段忘了分类 → 红（这条是防腐层，值钱的就是它）；
2. 面板能改的，传了就一定生效（含 0 这种被 `or` 吞掉的值）；
3. 代码所有的，传了就一定报错。**静默忽略等于假开关**——调用方以为改了，
   跑起来的还是默认值，比拒绝更难查。
"""

from __future__ import annotations

import unittest

from live import autotrade
from tests._stubs import install_missing_stubs

install_missing_stubs()


def _all_fields() -> set[str]:
    return set(autotrade.Cfg.__dataclass_fields__)


class TestFieldOwnership(unittest.TestCase):
    def test_every_field_is_claimed(self):
        """没有归属的字段 = 没人管的地带。**这条是本次改动的核心。**"""
        unclaimed = _all_fields() - set(autotrade.PANEL_FIELDS) - set(autotrade.CODE_ONLY)
        self.assertEqual(
            sorted(unclaimed), [],
            f"这些字段既不在 PANEL_FIELDS 也不在 CODE_ONLY：{sorted(unclaimed)}。"
            "请决定它该归谁——面板能改的进 PANEL_FIELDS，"
            "阈值/证据类进 CODE_ONLY，别让它留在无人区。")

    def test_ownership_does_not_overlap(self):
        both = set(autotrade.PANEL_FIELDS) & set(autotrade.CODE_ONLY)
        self.assertEqual(sorted(both), [], f"同一字段被两个名单都认领：{sorted(both)}")

    def test_no_ghost_names(self):
        """名单里写着 Cfg 已经没有的字段 = 改名后留下的化石。"""
        ghosts = (set(autotrade.PANEL_FIELDS) | set(autotrade.CODE_ONLY)) - _all_fields()
        self.assertEqual(sorted(ghosts), [], f"名单里有 Cfg 不存在的字段：{sorted(ghosts)}")


class TestPanelFieldsTakeEffect(unittest.TestCase):
    def test_each_panel_field_round_trips(self):
        """面板能改的字段，传什么就该是什么。"""
        sample = {"int": 42, "float": 1.5, "bool": True, "str": "xyz"}
        for name in autotrade.PANEL_FIELDS:
            with self.subTest(field=name):
                want = sample[autotrade._field_kind(name)]
                cfg = autotrade.cfg_from_start_body({name: want})
                self.assertEqual(getattr(cfg, name), want)

    def test_zero_is_not_swallowed(self):
        """0 是合法值，不能被当成"没填"。

        旧写法 `int(body.get("cooldown_sec") or Cfg.cooldown_sec)` 会把
        "关掉冷却"(0) 变成 60——用户配了跟没配一样，还不报错。
        """
        cfg = autotrade.cfg_from_start_body(
            {"cooldown_sec": 0, "cooldown_same_sec": 0,
             "max_trades": 0, "max_loss_usd": 0.0, "max_minutes": 0})
        self.assertEqual(cfg.cooldown_sec, 0)
        self.assertEqual(cfg.cooldown_same_sec, 0)
        self.assertEqual(cfg.max_trades, 0)
        self.assertEqual(cfg.max_loss_usd, 0.0)
        self.assertEqual(cfg.max_minutes, 0)

    def test_empty_string_means_unset(self):
        """下拉框没选中时传的是空串——那是界面的空状态，不是用户的选择。"""
        cfg = autotrade.cfg_from_start_body({"symbol": "  ", "lots": None})
        self.assertEqual(cfg.symbol, autotrade.Cfg.symbol)
        self.assertEqual(cfg.lots, autotrade.Cfg.lots)

    def test_missing_body_is_all_defaults(self):
        self.assertEqual(vars(autotrade.cfg_from_start_body(None)),
                         vars(autotrade.Cfg()))

    def test_string_false_is_false(self):
        """`bool("false")` 是 True。这条路以后会被脚本走，不能骗人。"""
        cfg = autotrade.cfg_from_start_body({"dry_run": "false"})
        self.assertFalse(cfg.dry_run)
        self.assertTrue(autotrade.cfg_from_start_body({"dry_run": "true"}).dry_run)

    def test_wrong_type_names_the_field(self):
        with self.assertRaises(ValueError) as ctx:
            autotrade.cfg_from_start_body({"lots": "一手"})
        self.assertIn("lots", str(ctx.exception))

    def test_non_cfg_keys_are_ignored(self):
        """confirm / account / 旧版嵌套 cfg 是协议字段，不是参数。"""
        cfg = autotrade.cfg_from_start_body(
            {"confirm": True, "account": "123", "cfg": {}, "垃圾": 1})
        self.assertEqual(vars(cfg), vars(autotrade.Cfg()))


class TestCodeOnlyFields(unittest.TestCase):
    def test_code_only_field_is_rejected_not_ignored(self):
        """传了阈值就报错。静默忽略会让调用方以为改成功了。"""
        for name in ("sl_usd", "tp_min_usd", "min_edge", "trend_follow"):
            with self.subTest(field=name):
                with self.assertRaises(ValueError) as ctx:
                    autotrade.cfg_from_start_body({name: 1})
                self.assertIn(name, str(ctx.exception))

    def test_code_only_uses_this_version_defaults(self):
        """代码所有 = 用**本版**默认值，不吃存档。

        存档里的 cfg 是"上一场跑过的记录"，不是"我要的配置"。要是启动时继承
        它，改了代码默认值（比如 cooldown 那次改档）对老机器永远不生效——
        默认值升级会被一份旧 JSON 静默吃掉，比"被重置"更难发现。
        """
        cfg = autotrade.cfg_from_start_body({})
        for name in autotrade.CODE_ONLY:
            with self.subTest(field=name):
                self.assertEqual(getattr(cfg, name),
                                 autotrade.Cfg.__dataclass_fields__[name].default)


if __name__ == "__main__":
    unittest.main()
