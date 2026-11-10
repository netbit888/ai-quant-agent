"""体检器的判别力：坏账本、坏备份、僵尸状态必须被判出来。

为什么值得测
------------
维护工具的价值全在「判得准」：判漏了等于没做体检（备份打不开却报 OK，
比不检查更危险——它给了虚假的安全感）；判错了会天天喊狼来了，报告很快
没人看。所以这里不测字符串文案，只测**三档结论是否落在该落的地方**。

另一个理由：这个脚本会读真实账本目录。它必须在临时目录上跑得通，证明
它没有任何「只能对着真数据工作」的隐含依赖。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import maintain as M  # noqa: E402


def _levels(items: list[M.Item]) -> dict[str, str]:
    return {i.name: i.level for i in items}


class TestZipVerify(unittest.TestCase):
    def test_good_zip_with_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "2026-11-10.zip"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("deals.jsonl", '{"ticket": 1}\n')
            ok, why = M._verify_zip(p, require_ledger=True)
            self.assertTrue(ok, why)

    def test_missing_ledger_file(self):
        """只有 profile.json 的包对账户备份来说是废的——恢复不出成交记录。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.zip"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("profile.json", "{}")
            ok, why = M._verify_zip(p, require_ledger=True)
            self.assertFalse(ok)
            self.assertIn("关键文件", why)

    def test_global_does_not_require_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.zip"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("profile.json", "{}")
            ok, _ = M._verify_zip(p, require_ledger=False)
            self.assertTrue(ok)

    def test_truncated_zip_is_caught(self):
        """截断的包在 namelist 阶段可能不报错，所以实现里必须真读一个成员。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.zip"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("deals.jsonl", "x" * 5000)
            data = p.read_bytes()
            p.write_bytes(data[: len(data) // 2])
            ok, _ = M._verify_zip(p, require_ledger=True)
            self.assertFalse(ok)


class TestLedgerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.accounts = base / "accounts"
        self.accounts.mkdir()
        self.index = base / "index.json"
        self._patch = [
            mock.patch.object(M.paths, "ACCOUNTS_DIR", self.accounts),
            mock.patch.object(M.paths, "INDEX_FILE", self.index),
        ]
        for p in self._patch:
            p.start()

    def tearDown(self):
        for p in self._patch:
            p.stop()
        self.tmp.cleanup()

    def _acct(self, login: str = "123") -> Path:
        d = self.accounts / login
        d.mkdir()
        return d

    def test_bad_json_line_is_fail_with_lineno(self):
        d = self._acct()
        (d / "deals.jsonl").write_text(
            '{"ticket": 1, "time": 1}\n'
            '{"ticket": 2, "time": 2}\n'
            '{"ticket": 3, "time": \n'      # 半行：写盘被打断的典型残迹
            '{"ticket": 4, "time": 4}\n',
            encoding="utf-8",
        )
        items = M._check_deals(d)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].level, M.FAIL)
        self.assertIn("3", items[0].detail)  # 行号必须指出，否则没法修

    def test_clean_deals_ok(self):
        d = self._acct()
        (d / "deals.jsonl").write_text('{"ticket": 1, "time": 1}\n', encoding="utf-8")
        self.assertEqual(M._check_deals(d)[0].level, M.OK)

    def test_zombie_running_state_is_warn(self):
        """running=true 但 tick 停在几个小时前：崩溃后状态没落盘。"""
        d = self._acct()
        (d / "autotrade_state.json").write_text(json.dumps({
            "running": True,
            "last_tick_ts": int(time.time()) - 6 * 3600,
            "position": None,
        }), encoding="utf-8")
        items = _levels(M._check_state(d))
        self.assertEqual(items["123 心跳"], M.WARN)

    def test_fresh_running_state_is_ok(self):
        d = self._acct()
        (d / "autotrade_state.json").write_text(json.dumps({
            "running": True,
            "last_tick_ts": int(time.time()) - 5,
            "position": {},
        }), encoding="utf-8")
        self.assertEqual(_levels(M._check_state(d))["123 心跳"], M.OK)

    def test_leftover_position_surfaced(self):
        """state 里还挂着仓位，是「以为空仓其实有单」的唯一本地线索。"""
        d = self._acct()
        (d / "autotrade_state.json").write_text(json.dumps({
            "running": False, "last_tick_ts": int(time.time()) - 60,
            "position": {"ticket": -1, "lots": 0.01},
        }), encoding="utf-8")
        self.assertIn("123 残留持仓", _levels(M._check_state(d)))

    def test_smoke_dir_is_ignored(self):
        """accounts/ 下还有 smoke/，它不是账户，不该被当成账本检查。"""
        self._acct("123")
        (self.accounts / "smoke").mkdir()
        names = [p.name for p in M._account_dirs()]
        self.assertEqual(names, ["123"])

    def test_index_mismatch_is_warn(self):
        self._acct("123")
        self.index.write_text(json.dumps({}), encoding="utf-8")
        items = M._check_ledger()
        lv = _levels(items)
        self.assertEqual(lv.get("账户索引"), M.WARN)

    def test_main_returns_nonzero_on_fail(self):
        """退出码是接告警的唯一接口：FAIL 必须变成 1。"""
        d = self._acct()
        (d / "deals.jsonl").write_text("not json\n", encoding="utf-8")
        (d / "autotrade_state.json").write_text("{}", encoding="utf-8")
        code = M.main(["--only", "ledger", "--no-report", "--json"])
        self.assertEqual(code, 1)


class TestResidue(unittest.TestCase):
    def test_patterns(self):
        for name in ("autotrade_state.json.bak-pnlfix", "deals.jsonl.legacy",
                     "autotrade_log.20261105-210450.jsonl",
                     "autotrade_state.20251105-210450.json"):
            self.assertTrue(
                M._RESIDUE_RE.search(name) or M._STAMPED_RE.match(name), name)
        for name in ("autotrade_state.json", "deals.jsonl", "autotrade_log.jsonl"):
            self.assertFalse(
                M._RESIDUE_RE.search(name) or M._STAMPED_RE.match(name), name)


if __name__ == "__main__":
    unittest.main()
