"""分层边界的守门测试：`live/mt5conn.py`、`live/lessons.py`、`live/jsonl.py`。

为什么值得一组测试
------------------
2026-11-13 的解耦把三块内容搬出了 `autotrade.py`：

  · `live/ta.py`、`live/cfg.py`  —— 纯层（守门测试在 test_ta_module.py）
  · `live/lessons.py`           —— 教训库（自带存储，依赖 jsonl/ta/paths）
  · `live/mt5conn.py`           —— MT5 连接唯一持有者（从 journal 下沉）
  · `live/jsonl.py`             —— jsonl 读写 + lesson 拆包（两个模块共用）

搬出去本身不难，难的是**保持搬完之后不回流**。这类回归的形态都很像：
某人为了图方便在新模块里 `from live import autotrade`，包级依赖图上立刻
长出一条新的环，而所有测试仍然全绿——因为功能没坏，只是架构又烂回去了。
所以这里把「谁可以 import 谁」写成断言。

钉住的四条
----------
1. `mt5conn` 只依赖标准库 + `live.mt5lock`。它一旦 import `journal`，
   `mt5/bridge → live.mt5conn → live.journal` 就绕回了原来的环。
2. 连接**只有一份**：`journal._mt5 is mt5conn._mt5`。如果哪天有人把连接层
   "复制一份到 journal"，两个 `_connected` 各自为政，引用计数会打架
   （这类 bug 的现场是「连上了但查不到持仓」，查起来极慢）。
3. `lessons` 不 import `autotrade`：教训库是被调用方，不能反过来要 autotrade。
4. `mt5/bridge.py` 不再 import `live.journal`：回测取数不该依赖实盘账本。
   这条是第 1 条的目的——没有它，第 1 条只是把环换成另一个写法。

外加一条**保险的保险**：`tests/_stubs.py` 的断链必须落在 `mt5conn.ensure` 上。
打桩点搬错地方不会变红，只会让整个测试套件悄悄开始连真实终端——那是本文件
存在的最实在的理由。
"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 必须走一遍：断链是在 install 里装的，不装的话这条用例会真的去碰终端
# （而且是"碰失败"，看起来也像抛 RuntimeError，断言会假绿）。
from tests._stubs import install_missing_stubs  # noqa: E402

install_missing_stubs()

BUSINESS = {"live", "tools", "agent", "mt5", "execution", "backtest", "risk",
            "portfolio"}


def _imports(path: Path) -> list[tuple[int, str]]:
    """(行号, 被导入的**完整**目标)。

    `from live import journal` 记成 `live.journal`——只记模块名会漏掉这种写法，
    而它恰恰是最常见的"偷偷依赖"形式。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((node.lineno, a.name))
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            for a in node.names:
                full = f"{base}.{a.name}" if base else a.name
                out.append((node.lineno, full))
    return out


class TestMt5ConnIsALeaf(unittest.TestCase):
    """连接层是底座：只能靠标准库和锁模块，不能靠任何业务模块。"""

    def test_only_stdlib_and_mt5lock(self):
        bad = []
        for lineno, mod in _imports(ROOT / "live" / "mt5conn.py"):
            top = mod.split(".")[0]
            if top in BUSINESS and not mod.startswith("live.mt5lock"):
                bad.append(f"L{lineno} {mod}")
        self.assertEqual([], bad,
                         "mt5conn 出现了业务依赖（会重新长成环）：\n"
                         + "\n".join(bad))

    def test_connection_exists_only_once(self):
        from live import journal, mt5conn

        self.assertIs(journal._mt5, mt5conn._mt5,
                      "journal._mt5 必须是 mt5conn._mt5 本身，不是副本"
                      "（两个 _connected 会各管各的引用计数）")
        self.assertFalse(hasattr(journal, "_ensure"),
                         "journal 不该再导出 _ensure——测试若打 journal._ensure "
                         "的桩会静默失效，然后开始连真实终端")

    def test_state_lives_in_mt5conn(self):
        from live import mt5conn

        for name in ("_connected", "_conn_failed_at", "_launch_failed_at",
                     "_last_launch_ok", "CONN_BACKOFF", "LAUNCH_BACKOFF"):
            self.assertTrue(hasattr(mt5conn, name), f"mt5conn 缺状态 {name}")


class TestLessonsDoesNotDependOnAutotrade(unittest.TestCase):
    def test_no_autotrade_import(self):
        # lessons 允许依赖 live.paths / live.jsonl / live.ta，唯独不许碰 autotrade
        offenders = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "lessons.py")
                     if m.startswith("live.autotrade")]
        self.assertEqual([], offenders,
                         "lessons 反向依赖了 autotrade：\n" + "\n".join(offenders))

    def test_jsonl_is_a_leaf(self):
        bad = [(ln, m) for ln, m in _imports(ROOT / "live" / "jsonl.py")
               if m.split(".")[0] in BUSINESS]
        self.assertEqual([], bad,
                         "jsonl 必须只依赖标准库：\n"
                         + "\n".join(f"L{ln} {m}" for ln, m in bad))


class TestBridgeNoLongerBorrowsTheLedger(unittest.TestCase):
    """回测取数不该为了拿一条连接去依赖实盘账本。"""

    def test_bridge_does_not_import_journal(self):
        # 允许 live.appdirs / live.mt5lock / live.mt5conn 这些底座，只禁账本
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "mt5" / "bridge.py")
               if m.startswith("live.journal")]
        self.assertEqual([], bad,
                         "mt5/bridge 又去借 journal 了：\n" + "\n".join(bad))


class TestBreakChainStillArmed(unittest.TestCase):
    """测试进程不许碰真实终端。这条一破，跑测试会连上本机 MT5。"""

    def test_ensure_is_stubbed(self):
        if os.environ.get("AQA_ALLOW_MT5"):
            self.skipTest("AQA_ALLOW_MT5 打开时本来就不断链")
        from live import mt5conn

        with self.assertRaises(RuntimeError):
            mt5conn.ensure()
        self.assertTrue(callable(getattr(mt5conn, "_ensure_real", None)),
                        "断链时也要保留真实实现，否则专门测连接的用例没法还原")


# --------------------------------------------------------------------------
# 第二轮搬迁（2026-11-13 下午）：storage / market / report / bootstrap / units
#
# 这五个模块的共同点是「都被 autotrade 重新导出」——它们之间谁依赖谁，
# 决定了 autotrade 能不能继续缩。特别地：
#
#   units → storage        （降级要写流水）
#   bootstrap → storage / market / cfg
#   market → cfg / mt5conn / journal（读汇率与持仓）
#   storage → cfg          （只为 `_log_signal` 的类型注解，纯层不反向）
#
# 方向一旦反过来（比如 storage 为了省事 `from live.market import ...`），
# 立刻回到 autotrade 那种「谁都能调谁」的局面。
# --------------------------------------------------------------------------

FORBIDDEN_IN_NEW_LAYERS = ("live.autotrade",)


class TestNewLayersDoNotImportAutotrade(unittest.TestCase):
    """五个新模块谁都不许反向依赖 autotrade——那会让环立刻长回来。"""

    LAYERS = ("storage.py", "market.py", "report.py", "bootstrap.py",
              "units.py")

    def test_no_autotrade_dependency(self):
        bad = []
        for name in self.LAYERS:
            for ln, mod in _imports(ROOT / "live" / name):
                if mod.startswith(FORBIDDEN_IN_NEW_LAYERS):
                    bad.append(f"{name}:L{ln} {mod}")
        self.assertEqual([], bad,
                         "新层反向依赖了 autotrade：\n" + "\n".join(bad))

    def test_journal_no_longer_imports_autotrade(self):
        """**这条是整个第二轮的成果**。

        `journal` 曾经为了写一行 `kind="degrade"` 的流水去
        `from live.autotrade import _degrade`，把 import 藏在函数体里绕开
        循环。`degrade()` 下沉到 `units.py` 之后这条边应该彻底消失——
        如果它回来了，说明有人又在账本里直接调用交易层的函数。
        """
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "journal.py")
               if m.startswith("live.autotrade")]
        self.assertEqual([], bad,
                         "journal 又开始依赖 autotrade 了（环回来了）：\n"
                         + "\n".join(bad))


class TestStorageLayer(unittest.TestCase):
    def test_only_leaf_dependencies(self):
        allowed = {"live.paths", "live.cfg"}          # cfg 是纯层，只为注解
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "storage.py")
               if m.split(".")[0] in BUSINESS
               and not any(m == a or m.startswith(a + ".") for a in allowed)]
        self.assertEqual([], bad,
                         "storage 只该依赖 paths / cfg：\n" + "\n".join(bad))

    def test_reexported_by_autotrade(self):
        from live import autotrade, storage

        for name in ("read_log", "load_state", "save_state", "append_log",
                     "_empty_state", "log_path", "state_path", "_log_signal",
                     "cache_stats", "_tail_lines"):
            self.assertIs(getattr(autotrade, name), getattr(storage, name),
                          f"autotrade.{name} 不是 storage 里的同一个对象")

    def test_empty_state_is_the_only_schema(self):
        """state 的键集合只有一个定义处。多一份就会缺键/多键而没人发现。"""
        from live.storage import _empty_state

        keys = set(_empty_state())
        self.assertGreaterEqual(len(keys), 25, "state 键数异常，schema 被改过？")
        for must in ("running", "position", "last_decision", "pending_reviews",
                     "cfg", "owner", "pid"):
            self.assertIn(must, keys)


class TestMarketLayer(unittest.TestCase):
    def test_no_autotrade_or_bootstrap(self):
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "market.py")
               if m.startswith(("live.autotrade", "live.bootstrap",
                                "live.storage", "live.report"))]
        self.assertEqual([], bad,
                         "market 是取数层，不该依赖上层：\n" + "\n".join(bad))

    def test_caches_are_shared_not_copied(self):
        """缓存必须是同一个 dict。复制一份 = 两份 TTL 各算各的。"""
        from live import autotrade, market

        for name in ("_CONTRACT_CACHE", "_ACCT_CUR", "_ACCT_F"):
            self.assertIs(getattr(autotrade, name), getattr(market, name),
                          f"{name} 被复制了")

    def test_price_dist_uses_shared_contract_cache(self):
        """价距换算走 `_symbol_contract`，不许另写一套合约规格。"""
        import inspect
        from live import market

        src = inspect.getsource(market.price_dist_of)
        self.assertIn("_symbol_contract", src)


class TestReportAndBootstrap(unittest.TestCase):
    def test_report_is_pure(self):
        # backtest.symbols 是静态符号表（point/contract/digits），不是实盘逻辑，
        # 读它不构成对回测引擎的耦合。
        allowed = {"live.cfg", "live.ta", "backtest.symbols"}
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "report.py")
               if m.split(".")[0] in BUSINESS
               and not any(m == a or m.startswith(a + ".") for a in allowed)]
        self.assertEqual([], bad,
                         "report 只该依赖 cfg / ta：\n" + "\n".join(bad))

    def test_symbol_options_shape(self):
        from live.report import symbol_options

        rows = symbol_options()
        self.assertTrue(rows, "下拉框数据不能为空")
        for r in rows:
            for k in ("symbol", "label", "point", "contract"):
                self.assertIn(k, r)

    def test_validate_cfg_still_rejects_bad_lots(self):
        """搬迁不许把校验弄丢：手数 0.013 必须被拒（券商会静默改成 0.01）。"""
        from live.bootstrap import validate_cfg
        from live.cfg import Cfg

        issues = validate_cfg(Cfg(lots=0.013))
        self.assertTrue(any("手数" in i for i in issues),
                        f"非法手数没被拦：{issues}")


class TestUnitsLayer(unittest.TestCase):
    def test_units_depends_only_on_storage(self):
        # 同上：symbols 是静态表。units 的全部运行时依赖只有 storage。
        allowed = ("live.storage", "backtest.symbols")
        bad = [f"L{ln} {m}" for ln, m in _imports(ROOT / "live" / "units.py")
               if m.split(".")[0] in BUSINESS
               and not any(m == a or m.startswith(a + ".") for a in allowed)]
        self.assertEqual([], bad,
                         "units 只该依赖 storage：\n" + "\n".join(bad))

    def test_digits_and_px_agree(self):
        from live import units

        self.assertEqual(units.px("XAUUSDm", 4000.123456), "4000.123")
        self.assertEqual(units.digits("XAUUSDm"), 3)
        self.assertEqual(units.px("XAUUSDm", None), "—")


if __name__ == "__main__":
    unittest.main()
