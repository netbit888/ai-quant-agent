"""release_notes.py：从 CHANGELOG 抽 release notes。

两条规矩各有来历，别随手改：
1. 找不到该版本段落必须失败——发版 notes 缺失比发全量历史更糟。
2. 段落里有 ``<!-- notes -->`` 标记块时只取块内——v1.5.3 就是把整篇日志摊进
   更新弹窗，把「下载并安装」顶到视口外，更新通道被自己的日志堵死。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools import release_notes


DOC = """# 变更记录

## v1.5.5 — 2026-10-27

<!-- notes -->
短日志：一句结论。
<!-- /notes -->

### 详细说明

这里可以写两千字，弹窗不该看到。

## v1.5.4 — 2026-10-27

### 老版本没有标记块

整段都当 notes。
"""


class TestExtract(unittest.TestCase):
    def test_marked_block_wins(self):
        out = release_notes.extract(DOC, "1.5.5")
        self.assertEqual(out.strip(), "短日志：一句结论。")
        self.assertNotIn("两千字", out)
        self.assertNotIn("v1.5.4", out)

    def test_section_without_marker_is_whole_section(self):
        out = release_notes.extract(DOC, "1.5.4")
        self.assertIn("老版本没有标记块", out)
        self.assertNotIn("短日志", out)

    def test_missing_version_is_empty(self):
        self.assertEqual(release_notes.extract(DOC, "9.9.9"), "")

    def test_never_bleeds_into_next_version(self):
        """抽 1.5.4 时不许把 1.5.5 的内容带进来（下一节起点就到文件尾）。"""
        out = release_notes.extract(DOC, "1.5.4")
        self.assertNotIn("详细说明", out)


class TestRealChangelog(unittest.TestCase):
    """真实 CHANGELOG 的约束：弹窗显示的那份必须够短。"""

    @classmethod
    def setUpClass(cls):
        cls.path = (Path(__file__).resolve().parents[1] / "CHANGELOG.md")
        cls.text = cls.path.read_text(encoding="utf-8")

    def test_current_version_notes_are_short_enough(self):
        from app_version import APP_VERSION

        out = release_notes.extract(self.text, APP_VERSION)
        self.assertTrue(out.strip(), f"CHANGELOG 里缺 v{APP_VERSION} 的段落")
        self.assertLessEqual(len(out), release_notes.MAX_NOTES_CHARS,
                             "release notes 太长，更新弹窗会被顶爆")


if __name__ == "__main__":
    unittest.main()
