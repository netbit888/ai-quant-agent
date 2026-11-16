"""VERSIONINFO / 应用身份。

这一层管的是「用户在任务管理器、任务栏、通知里看到什么名字」。
没有 VERSIONINFO 时任务管理器只能拿文件名当显示名——用户看到的是
"Python"（源码态）或 "AIQuant"（安装版），而不是「SeatOne 实盘账本」。

钉三件事：
  1. 生成的资源文件能被 PyInstaller 解析（格式对了，不是看着像）。
  2. 版本号只有 app_version 一个来源，四段补齐（1.5.30 -> 1.5.30.0）。
  3. spec 真的把它传给 EXE，且 desktop 真的设了 AUMID —— 否则以后有人
     一重构就静默丢掉，用户只会觉得"名字又变回去了"。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION  # noqa: E402
from tools.version_info import APP_ID, APP_NAME, render, write_version_file  # noqa: E402


class VersionInfoTest(unittest.TestCase):
    def test_pyinstaller_can_parse(self):
        """格式不对的话打包会直接崩，且报错信息指不到这里。"""
        try:
            from PyInstaller.utils.win32.versioninfo import (
                load_version_info_from_text_file,
            )
        except ImportError:  # 没装 pyinstaller（CI 精简环境）就跳过
            self.skipTest("PyInstaller 未安装")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = write_version_file(Path(tmp) / "vi.txt")
            text = str(load_version_info_from_text_file(str(p)))
        self.assertIn("FileDescription", text)
        self.assertIn(APP_NAME, text)

    def test_version_is_four_parts(self):
        """VERSIONINFO 固定四段；少一段 Windows 直接不认。"""
        body = render("1.5.30")
        self.assertIn("filevers=(1, 5, 30, 0)", body)
        self.assertIn("prodvers=(1, 5, 30, 0)", body)

    def test_version_from_single_source(self):
        """版本号必须来自 app_version，不允许在别处硬编码。"""
        self.assertIn(f"FileVersion', '{APP_VERSION}'", render())

    def test_write_is_idempotent(self):
        """内容没变不刷新 mtime：否则每次打包都让增量构建全部失效。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = write_version_file(Path(tmp) / "vi.txt")
            m1 = p.stat().st_mtime_ns
            p2 = write_version_file(Path(tmp) / "vi.txt")
            self.assertEqual(p, p2)
            self.assertEqual(m1, p2.stat().st_mtime_ns)

    def test_spec_passes_version(self):
        spec = (ROOT / "aqa.spec").read_text(encoding="utf-8")
        self.assertIn("write_version_file", spec)
        self.assertIn("version=str(_VERSION_FILE)", spec)

    def test_run_window_sets_app_id(self):
        """AUMID 必须在 create_window 之前设，顺序不能被重排掉。"""
        src = (ROOT / "tools" / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("SetCurrentProcessExplicitAppUserModelID", src)
        body = src[src.index("def run_window"):]
        self.assertLess(body.index("ensure_app_id()"),
                        body.index("webview.create_window"))

    def test_app_id_is_stable(self):
        """改 AUMID = 任务栏固定项/通知归组全断，只能是显式决定。"""
        self.assertEqual(APP_ID, "SeatOne.LiveDesk.1")


if __name__ == "__main__":
    unittest.main()
