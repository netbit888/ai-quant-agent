"""经验包导入相关测试：路径校验助手 + 文件选择器导入全流程。"""

import json
import tempfile
import unittest
from pathlib import Path

from live import paths


class TestResolveImportPath(unittest.TestCase):
    """_resolve_import_path：文件选择器路径的两道硬线。"""

    def test_ok(self):
        from tools.dashboard import _resolve_import_path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jsonl"
            p.write_text("{}\n", encoding="utf-8")
            got, err = _resolve_import_path(str(p))
        self.assertIsNone(err)
        self.assertEqual(got, p)

    def test_strips_quotes_and_spaces(self):
        from tools.dashboard import _resolve_import_path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.JSONL"
            p.write_text("", encoding="utf-8")
            got, err = _resolve_import_path(f'  "{p}" ')
        self.assertIsNone(err)
        self.assertEqual(got, p)          # 后缀大小写不敏感

    def test_wrong_suffix(self):
        from tools.dashboard import _resolve_import_path
        got, err = _resolve_import_path("C:/x/lesson.json")
        self.assertIsNone(got)
        self.assertIn(".jsonl", err)

    def test_missing_file(self):
        from tools.dashboard import _resolve_import_path
        got, err = _resolve_import_path("C:/nope/none.jsonl")
        self.assertIsNone(got)
        self.assertIn("不存在", err)

    def test_empty(self):
        from tools.dashboard import _resolve_import_path
        for bad in ("", None, '""', "   "):
            got, err = _resolve_import_path(bad)
            self.assertIsNone(got)
            self.assertEqual(err, "缺少 path")


class TestImportFromPath(unittest.TestCase):
    """_import_lesson_pack_from_path：走真实 lesson_packs 落盘。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        paths.set_root(Path(self._tmp.name))

    def tearDown(self):
        paths.set_root(None)

    def _write_src(self, rows) -> Path:
        src = Path(self._tmp.name) / "pick.jsonl"
        src.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
        return src

    def test_import_creates_pack_and_activates(self):
        from tools.dashboard import _import_lesson_pack_from_path
        from live import lesson_packs

        src = self._write_src([
            {"ts": 1, "ticket": 11, "lesson": "等确认再进场"},
            {"ts": 2, "ticket": 12, "lesson": "止盈别太早"},
            "not json",                       # 坏行：计数进 skipped_bad
        ])
        resp = _import_lesson_pack_from_path(str(src), "旧账经验")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["imported"], 2)
        self.assertEqual(resp["skipped_bad"], 1)
        idx = lesson_packs._read_index()
        self.assertEqual(idx["active"], resp["id"])
        self.assertEqual(idx["packs"][-1]["name"], "旧账经验")
        # 包内容落盘：2 行、顺序按 ts
        pack = lesson_packs.packs_dir() / idx["packs"][-1]["file"]
        lines = [json.loads(x) for x in pack.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["ticket"] for x in lines], [11, 12])

    def test_name_defaults_to_filename(self):
        from tools.dashboard import _import_lesson_pack_from_path
        from live import lesson_packs

        src = self._write_src([{"ts": 1, "ticket": 1, "lesson": "x"}])
        resp = _import_lesson_pack_from_path(str(src), "")
        self.assertTrue(resp["ok"])
        self.assertEqual(lesson_packs._read_index()["packs"][-1]["name"], "pick")

    def test_rejects_missing_file(self):
        from tools.dashboard import _import_lesson_pack_from_path
        resp = _import_lesson_pack_from_path("C:/nope/none.jsonl", "")
        self.assertFalse(resp["ok"])
        self.assertIn("不存在", resp["error"])

    def test_rejects_wrong_suffix(self):
        from tools.dashboard import _import_lesson_pack_from_path
        resp = _import_lesson_pack_from_path("C:/x/lesson.json", "")
        self.assertFalse(resp["ok"])
        self.assertIn(".jsonl", resp["error"])


if __name__ == "__main__":
    unittest.main()
