"""live/paths.py 的路径穿越防线（2026-11-05 安全审查 P1）。

背景：曾实测 GET /api/autotrade?account=..\\..\\config 直接读到了 data/config/
下的文件——删除路径有防护而读/写没有。修复是把校验放在 account_dir()，
所有路径拼接的汇聚点。这里验证：穿越被拒、正常目录名（含 smoke/unknown）不受影响。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live import paths


class TestAccountDirGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(Path(self._tmp.name) / "live")

    def tearDown(self) -> None:
        paths.set_root(None)
        self._tmp.cleanup()

    def test_dotdot_rejected(self):
        with self.assertRaises(ValueError):
            paths.account_dir("..")

    def test_forward_traversal_rejected(self):
        with self.assertRaises(ValueError):
            paths.account_dir("../config")

    def test_backslash_traversal_rejected(self):
        """实测过的 payload：?account=..\\..\\config 曾读到 data/config/。"""
        with self.assertRaises(ValueError):
            paths.account_dir("..\\..\\config")

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            paths.account_dir("C:\\Windows\\system32")

    def test_plain_names_still_work(self):
        """不强制纯数字：内部 fallback 和测试目录要照常工作。"""
        self.assertEqual(paths.account_dir("smoke").name, "smoke")
        self.assertEqual(paths.account_dir("unknown").name, "unknown")
        self.assertEqual(paths.account_dir(277733809).name, "277733809")

    def test_read_json_traversal_raises(self):
        with self.assertRaises(ValueError):
            paths.read_json("../config", "autotrade_state.json")

    def test_write_json_traversal_raises(self):
        with self.assertRaises(ValueError):
            paths.write_json("../config", "notes.json", {})


if __name__ == "__main__":
    unittest.main()
