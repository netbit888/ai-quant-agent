"""模板分片的结构约束：拼装得起来、两边拼得一样、index.html 保持"目录"。

背景：面板模板从单文件 7132 行拆成 tools/ui/ 下的 36 个分片（见 live/uiparts.py）。
拆分本身是一次性的（tools/ui_split.py 已经验过「拼装结果与拆分前逐字节相同」），
这个文件锁的是**拆分之后的长期约束**：

  1. index.html 只能是指令 + 骨架，不能再长出代码 —— 否则拆分白做。
  2. 每个分片都必须被 include，没有孤儿文件 —— 否则改了没生效，人会以为是缓存。
  3. Python 拼装（发给浏览器）和 node 拼装（冒烟测试用）必须逐字节一致 ——
     否则冒烟绿了不等于页面对。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from live import uiparts

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "tools" / "ui"
INCLUDE = re.compile(r"^<!--@include\s+([^>\s]+)\s*-->\s*$", re.M)


class TestPartsLayout(unittest.TestCase):
    def setUp(self):
        self.index = (UI / "index.html").read_text(encoding="utf-8")
        self.includes = INCLUDE.findall(self.index)

    def test_ui_dir_exists(self):
        self.assertTrue(UI.is_dir(), f"缺模板分片目录 {UI}")

    def test_index_is_a_table_of_contents(self):
        """index.html 只许是指令 + 骨架：不超 100 行，且没有函数/规则体。"""
        lines = self.index.count("\n") + 1
        self.assertLess(lines, 100, f"index.html 长到 {lines} 行了，代码该进分片")
        self.assertNotIn("function ", self.index)
        # 骨架里允许 <style>/<script> 这类标签，但不该有成片的 CSS 声明。
        self.assertNotIn("margin:", self.index)
        self.assertNotIn("=>", self.index)

    def test_every_include_has_a_file(self):
        for rel in self.includes:
            self.assertTrue((UI / rel).is_file(), f"include 指向不存在的分片：{rel}")

    def test_no_orphan_parts(self):
        """目录里有、但没被 include 的文件 = 改了不生效。"""
        on_disk = {str(p.relative_to(UI)).replace("\\", "/")
                   for p in UI.rglob("*") if p.suffix in (".css", ".js", ".html")}
        on_disk.discard("index.html")
        orphans = on_disk - set(self.includes)
        self.assertEqual(orphans, set(), f"这些分片没有被 index.html 引用：{orphans}")

    def test_include_appears_once(self):
        dup = {p for p in self.includes if self.includes.count(p) > 1}
        self.assertEqual(dup, set(), f"同一分片被 include 了两次：{dup}")

    def test_no_part_is_git_ignored(self):
        """分片不能被 .gitignore 吃掉 —— 否则提交后模板整段消失。

        2026-11-05 的真实坑：.gitignore 有 `*.html`，例外只写了 `!tools/*.html`，
        而模板块搬进了 tools/ui/html/ 子目录。`git add --dry-run` 数出来只有
        29 个 css/js，7 个 html 分片全没进去——本地一切正常，别人拉下来就是
        500 template missing。
        """
        git = shutil.which("git")
        if not git:
            self.skipTest("没有 git")
        files = [str(p.relative_to(ROOT)).replace("\\", "/")
                 for p in UI.rglob("*") if p.is_file()]
        out = subprocess.run([git, "check-ignore", "--no-index", *files],
                             cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8")
        self.assertEqual(out.stdout.strip(), "",
                         "这些分片被 .gitignore 忽略，提交会丢：\n" + out.stdout)

    def test_css_before_script(self):
        """CSS 全在 <script> 之前 —— 否则 FOUC，且层叠顺序会变。"""
        style_end = self.index.index("</style>")
        script_start = self.index.index("<script>")
        last_css = max(self.index.index(f"<!--@include {p}-->")
                       for p in self.includes if p.startswith("css/"))
        self.assertLess(last_css, style_end)
        self.assertLess(style_end, script_start)


class TestAssemble(unittest.TestCase):
    def test_output_has_no_include_directive(self):
        html = uiparts.load_template().decode("utf-8")
        self.assertNotIn("<!--@include", html, "拼装漏了指令")

    def test_output_is_a_whole_document(self):
        html = uiparts.load_template().decode("utf-8")
        for tag in ("<!DOCTYPE html>", "<style>", "</style>", "<script>",
                    "</script>", "</body>", "</html>"):
            self.assertIn(tag, html)
        self.assertEqual(html.count("<script>"), 1,
                         "应当只有一段脚本：分片之间共享作用域")

    def test_missing_part_says_which_one(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            with self.assertRaises(uiparts.TemplateMissing) as cm:
                uiparts.assemble("<!--@include css/nope.css-->\n", base)
            self.assertIn("css/nope.css", str(cm.exception))

    def test_node_and_python_agree(self):
        """两边拼装必须给出同一个字符串。

        node 版给 tools/smoke_dashboard.js 用（假 DOM 里跑页面脚本）。两份实现
        分头改过、只改一边，冒烟会安静地测着上一版页面 —— 这种绿最危险。
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node")
        py = hashlib.sha256(uiparts.load_template()).hexdigest()
        code = (
            "const c=require('crypto');"
            "const {assemble}=require('./tools/ui_assemble');"
            "process.stdout.write(c.createHash('sha256')"
            ".update(assemble('tools/ui'),'utf8').digest('hex'))"
        )
        out = subprocess.run([node, "-e", code], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), py,
                         "node 与 python 拼装结果不一致：" + out.stdout[:80])
