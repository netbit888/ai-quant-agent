"""纯层的守门测试：`live/ta.py`（指标）和 `live/cfg.py`（参数）不许重新长出 I/O。

为什么值得一条测试
------------------
这次搬迁（2026-11-13）靠的是「`autotrade` 顶部重新导出」这个不变量：
`autotrade._atr` / `Cfg` / `PANEL_FIELDS` 这些名字被 17,000 行测试里的
130+ 处引用，一旦有人"顺手清理"掉那行 import，测试会以 AttributeError
的形态成片红，但报错位置离真正的原因（删了一行 import）很远。

三条不变量各钉一组用例：
  1. **纯**：两个模块只准 import 标准库。一旦有人为了省事
     `from live.journal import _mt5`，这层就重新变脏，回放工具又不敢复用它。
  2. **无副作用**：模块级不许 open / 写盘 / initialize。
  3. **可达**：搬走的名字在 `live.autotrade` 上仍能取到，且取自同一个对象
     （`is` 判断，不是值相等）——避免有人复制一份同名函数回去。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TA = ROOT / "live" / "ta.py"
CFG = ROOT / "live" / "cfg.py"

INTERNAL = {"live", "tools", "agent", "mt5", "execution", "backtest", "risk",
            "portfolio"}

# 搬迁清单：改名/删除都会让老路径失效，所以逐条列出而不是动态扫。
MOVED_TA_FUNCS = [
    "_atr", "_fmt_bar_lines", "_last_bar_shape", "_ma_calc", "_norm_bool",
    "_norm_prob", "_prog_direction", "_regime_word", "_returns",
    "_spread_points", "_structure", "_structure_text", "_symbol_label",
    "_zone_fact", "_zone_text", "clamp_p_up", "feature_snapshot", "ma_line",
    "parse_p_up", "regime_of", "visible_bars",
]
MOVED_TA_CONSTS = [
    "ER_RANGE_MAX", "ER_TREND_MIN", "MA_PERIOD", "P_UP_CEIL", "P_UP_FLOOR",
    "SYMBOL_LABELS", "VISIBLE_BARS", "_JSON_RE", "_PUP_RE",
]
MOVED_CFG = [
    "BASE_LOTS", "CODE_ONLY", "Cfg", "PANEL_FIELDS", "TRADABLE_SYMBOLS",
    "_SCALE_EPS", "_TYPE_LABEL", "_as_bool", "_coerce", "_field_kind",
    "cfg_from_start_body", "cfg_to_dict", "lot_scale", "money_at_lots",
    "money_label",
]


class TestPureModules(unittest.TestCase):
    def test_only_stdlib_imports(self):
        """ta.py / cfg.py 只准 import 标准库——出现 `from live...` 就是纯度破了。"""
        bad = []
        for path in (TA, CFG):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] in INTERNAL:
                            bad.append(f"{path.name} L{node.lineno} import {a.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        bad.append(f"{path.name} L{node.lineno} 相对导入")
                    elif (node.module or "").split(".")[0] in INTERNAL:
                        bad.append(f"{path.name} L{node.lineno} from {node.module}")
        self.assertEqual([], bad, "纯层不纯了：\n" + "\n".join(bad))

    def test_no_side_effects_at_module_level(self):
        """模块级不许出现 open( / write_text / mt5.initialize 这类副作用。"""
        offenders = []
        for path in (TA, CFG):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:          # 只看模块级，函数体里的 time.strftime 合法
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        name = (fn.id if isinstance(fn, ast.Name)
                                else getattr(fn, "attr", ""))
                        if name in {"open", "write_text", "write_json", "initialize"}:
                            offenders.append(f"{path.name} L{sub.lineno} {name}(...)")
        self.assertEqual([], offenders, "纯层模块级出现了副作用调用")


class TestOldPathsStillWork(unittest.TestCase):
    def test_reexported_from_autotrade(self):
        from live import autotrade, cfg, ta

        missing = []
        for name in MOVED_TA_FUNCS + MOVED_TA_CONSTS:
            if not hasattr(autotrade, name):
                missing.append(f"ta.{name}")
            elif getattr(autotrade, name) is not getattr(ta, name):
                missing.append(f"ta.{name}（不是同一个对象）")
        for name in MOVED_CFG:
            if not hasattr(autotrade, name):
                missing.append(f"cfg.{name}")
            elif getattr(autotrade, name) is not getattr(cfg, name):
                missing.append(f"cfg.{name}（不是同一个对象）")
        self.assertEqual([], missing,
                         "这些名字在 autotrade 上断链了：\n" + "\n".join(missing))

    def test_cfg_field_ownership_is_total(self):
        """每个 Cfg 字段必须归属面板或代码其中之一——加字段不分类要红。"""
        from live import cfg

        fields = set(cfg.Cfg.__dataclass_fields__)
        owned = set(cfg.PANEL_FIELDS) | set(cfg.CODE_ONLY)
        self.assertEqual(set(), fields - owned, "这些字段没有归属")
        self.assertEqual(set(), (set(cfg.PANEL_FIELDS) & set(cfg.CODE_ONLY)),
                         "同一个字段不能同时归两处")

    def test_atr_still_produces_13_tr(self):
        """搬完之后 ATR 口径不变：14 根产出 13 个 TR，这是回放对表的前提。"""
        from live import autotrade

        rows = [{"open": 1.0, "high": 1.0 + i * 0.1, "low": 0.9 + i * 0.1,
                 "close": 1.0 + i * 0.1, "tick_volume": 1} for i in range(20)]
        out = autotrade._atr(rows)
        self.assertTrue(out["ok"])
        self.assertEqual(13, out["n_tr"])


if __name__ == "__main__":
    unittest.main()
