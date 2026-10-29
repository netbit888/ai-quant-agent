"""流水表「开仓理由 / 平仓理由」不许被硬截断。

2026-10-29 截图：两列都停在半句上——开仓理由写到「…且 MA」、复盘教训写到
「（反事实校正：平仓后回」。原因是渲染时写的是 String(whyIn).slice(0, 80)：
80 个字符直接砍，**既没有省略号提示被截断，也没有任何地方能看全**（详情弹窗
里其实有全文，但表格读者不知道还有这一层）。

改成整段文本进 DOM，视觉折行交给 .why-clamp（限 6 行、超出由浏览器出省略号），
再加 title 让鼠标悬停就能读全文。这三条断言锁住修法，别让谁把 slice 加回来。
"""

from __future__ import annotations

import unittest
from pathlib import Path


class TestReasonCells(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1]
                    / "tools" / "dashboard_template.html").read_text(encoding="utf-8")

    def test_no_hard_slice_on_reason(self):
        """曾经的 80 字砍断：`String(whyIn).slice(0, 80)`，一个都不许回。"""
        for name in ("whyIn", "whyOut"):
            self.assertNotIn(f"{name}).slice(", self.html,
                             f"{name} 又被硬截断了：表格里会停在半句话上")

    def test_cell_keeps_the_whole_text(self):
        """全文必须真的进 DOM：class 负责折行，title 负责悬停可读。"""
        self.assertIn('class="why-clamp"', self.html)
        self.assertIn('title="${esc(whyIn)}"', self.html)
        self.assertIn('title="${esc(whyOut)}"', self.html)

    def test_clamp_is_visual_only(self):
        """限行只为压住行高，不能变成另一种隐藏：不许 display:none。"""
        box = self.html.split(".why-clamp {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp", box)
        self.assertIn("overflow: hidden", box)
        self.assertNotIn("display: none", box)


if __name__ == "__main__":
    unittest.main()
