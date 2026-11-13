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


if __name__ == "__main__":
    unittest.main()
