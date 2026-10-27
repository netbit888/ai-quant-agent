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


class TestExportTo(unittest.TestCase):
    """export_to：默认落 export/ 目录，给了 dest 就写到用户在「另存为」里选的位置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        paths.set_root(Path(self._tmp.name))
        from live import lesson_packs
        src = Path(self._tmp.name) / "seed.jsonl"
        src.write_text(
            "".join(json.dumps({"ts": i, "ticket": i, "lesson": f"l{i}"},
                               ensure_ascii=False) + "\n" for i in (1, 2, 3)),
            encoding="utf-8")
        self.lp = lesson_packs
        self.pid = lesson_packs.import_from(src, "我的经验")["id"]

    def tearDown(self):
        paths.set_root(None)

    def _lines(self, p: Path) -> int:
        return len([x for x in p.read_text(encoding="utf-8").splitlines() if x])

    def test_default_goes_to_export_dir(self):
        res = self.lp.export_to(self.pid)
        self.assertTrue(res["ok"])
        self.assertFalse(res["custom"])
        p = Path(res["path"])
        self.assertEqual(p.parent.resolve(), self.lp.export_dir().resolve())
        self.assertEqual(self._lines(p), 3)

    def test_explicit_dest_is_honoured(self):
        """用户选了哪里就写哪里——这是这次改动的全部意义。"""
        dest = Path(self._tmp.name) / "backup" / "我的经验备份.jsonl"
        res = self.lp.export_to(self.pid, str(dest))
        self.assertTrue(res["ok"])
        self.assertTrue(res["custom"])
        self.assertEqual(Path(res["path"]), dest)
        self.assertEqual(self._lines(dest), 3)      # 父目录也一并建出来了

    def test_missing_suffix_is_appended(self):
        """手打文件名忘了后缀时要补上，否则产出个打不开的文件。"""
        dest = Path(self._tmp.name) / "无后缀备份"
        res = self.lp.export_to(self.pid, str(dest))
        self.assertTrue(res["ok"])
        self.assertEqual(Path(res["path"]).name, "无后缀备份.jsonl")
        self.assertTrue((Path(self._tmp.name) / "无后缀备份.jsonl").is_file())

    def test_refuses_to_overwrite_the_pack_itself(self):
        """导到包文件自己身上会让源和目标同一个文件，copy2 直接炸。"""
        idx = self.lp._read_index()
        own = self.lp.packs_dir() / idx["packs"][-1]["file"]
        res = self.lp.export_to(self.pid, str(own))
        self.assertFalse(res["ok"])
        self.assertIn("自己的文件", res["error"])
        self.assertTrue(own.is_file())               # 原包一个字没动

    def test_unknown_pack(self):
        res = self.lp.export_to("no-such-id", str(Path(self._tmp.name) / "x.jsonl"))
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["error"])


class _FakeWindow:
    """替掉 pywebview 窗口，把 create_file_dialog 的入参记下来。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class TestPickExportPath(unittest.TestCase):
    """导出用的系统「另存为」对话框（_Bridge.pick_export_path）。

    这条桥在打包版里决定"文件到底落到哪"，崩了就是导出按钮点了没反应，
    而页面只会看到一句 Promise rejection——所以每条分支都要有用例。
    """

    def test_returns_user_chosen_path(self):
        from unittest import mock
        import tools.dashboard as dash

        win = _FakeWindow((r"D:\备份\我的经验.jsonl",))
        with mock.patch.object(dash, "_WINDOW", win):
            res = dash._Bridge().pick_export_path("我的经验")
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], r"D:\备份\我的经验.jsonl")
        self.assertEqual(res["name"], "我的经验.jsonl")

    def test_default_filename_is_sanitized(self):
        """库名里的 / \\ : * ? 直接塞给 save_filename 是非法文件名。"""
        from unittest import mock
        import tools.dashboard as dash

        win = _FakeWindow(None)
        with mock.patch.object(dash, "_WINDOW", win):
            dash._Bridge().pick_export_path('我的/经验:v2*?')
        name = win.calls[0][1]["save_filename"]
        for bad in '/\\:*?':
            self.assertNotIn(bad, name)
        self.assertTrue(name.endswith(".jsonl"))

    def test_suffix_added_once(self):
        from unittest import mock
        import tools.dashboard as dash

        win = _FakeWindow(None)
        with mock.patch.object(dash, "_WINDOW", win):
            dash._Bridge().pick_export_path("备份.jsonl")
        self.assertEqual(win.calls[0][1]["save_filename"], "备份.jsonl")

    def test_cancel_is_not_an_error(self):
        from unittest import mock
        import tools.dashboard as dash

        with mock.patch.object(dash, "_WINDOW", _FakeWindow(None)):
            res = dash._Bridge().pick_export_path("x")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["path"])     # 前端据此静默返回

    def test_browser_mode_degrades(self):
        from unittest import mock
        import tools.dashboard as dash

        with mock.patch.object(dash, "_WINDOW", None):
            res = dash._Bridge().pick_export_path("x")
        self.assertFalse(res["ok"])
        self.assertIn("仅窗口模式", res["error"])


class TestDialogType(unittest.TestCase):
    """对话框类型常量：不许再碰 pywebview 的废弃模块常量。"""

    def test_values(self):
        from tools.dashboard import _dialog_type
        self.assertEqual(_dialog_type("OPEN"), 10)
        self.assertEqual(_dialog_type("SAVE"), 30)
        self.assertEqual(_dialog_type("FOLDER"), 20)

    def test_no_deprecation_noise(self):
        """OPEN_DIALOG 是 module_property，每访问一次就打一行 deprecated。"""
        import contextlib
        import io

        from tools.dashboard import _dialog_type

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            _dialog_type("OPEN")
            _dialog_type("SAVE")
        self.assertNotIn("deprecated", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
