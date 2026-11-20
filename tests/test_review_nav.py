"""复盘台分区导航的静态约束（2026-11-20）。

复盘台 14 张卡片竖排 4000+ 像素，页首那条锚点导航是为了「找得到」。
它靠 `data-rev-to` 到分组 id 的约定连起来，这种字符串约定最容易在
以后挪卡片时悄悄失效——按钮还在，点下去什么都不发生，且没有任何报错。

这里钉三件事：
  1. 每个按钮指向的 id 真的存在；
  2. 按钮顺序与分组在文档里的先后一致（滚动高亮按「最后一个 top 已越线
     的分组」判断，顺序错了高亮就会整段错位，比没有还误导人）；
  3. 切换子态时确实调了 showReviewTop —— 否则从做单台滚半屏切过来会
     落在复盘台中间。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "tools" / "ui"
REVIEW = UI / "html" / "view-review.html"
LIVE_CSS = UI / "css" / "live.css"
REVIEW_JS = UI / "js" / "review.js"
SIG_MODAL = UI / "js" / "sig-modal.js"


class TestReviewNav(unittest.TestCase):
    def setUp(self):
        self.html = REVIEW.read_text(encoding="utf-8")
        self.targets = re.findall(r'data-rev-to="([^"]+)"', self.html)

    def test_has_buttons(self):
        self.assertGreaterEqual(len(self.targets), 4, "复盘台导航没了？四个子态才装得下 14 张卡")

    def test_every_target_exists(self):
        for tid in self.targets:
            self.assertIn(f'id="{tid}"', self.html, f"data-rev-to=\"{tid}\" 找不到对应的分组 id")

    def test_order_matches_document(self):
        """按钮顺序必须等于分组在文档里的先后，否则滚动高亮会整段错位。"""
        pos = [self.html.index(f'id="{t}"') for t in self.targets]
        self.assertEqual(pos, sorted(pos),
                         f"nav 按钮顺序与分组出现顺序不一致：{list(zip(self.targets, pos))}")

    def test_first_group_is_session_card(self):
        """第一个分组必须是「本场复盘」——盘后打开第一眼就要看它。"""
        first = self.targets[0]
        seg = self.html.split(f'id="{first}"', 1)[1][:400]
        self.assertIn("本场复盘", seg)

    def test_nav_is_sticky_below_statusbar(self):
        css = LIVE_CSS.read_text(encoding="utf-8")
        self.assertRegex(css, r"\.rev-nav\s*\{[^}]*position:\s*sticky")
        # 吸附位置必须由 JS 量出来的变量给，写死 44px 在窄窗口折行时会被盖住
        self.assertRegex(css, r"\.rev-nav\s*\{[^}]*top:\s*var\(--revnav-top")

    def test_pane_switch_resets_scroll(self):
        self.assertIn("function showReviewTop", REVIEW_JS.read_text(encoding="utf-8"))
        self.assertRegex(SIG_MODAL.read_text(encoding="utf-8"),
                         r"s === 'review'[^\n]*showReviewTop")


if __name__ == "__main__":
    unittest.main()
