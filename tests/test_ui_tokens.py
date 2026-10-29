"""UI 表现层的硬约束：token 之外不许出现裸值、对比度不许退回。

和 tools/ui_lint.py 同源（这里直接调它的 main，不复制规则），
目的只有一个：收敛完的 token 层不能漂回去。
"""
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ui_lint  # noqa: E402

TPL = Path(__file__).resolve().parents[1] / "tools" / "dashboard_template.html"


def _lum(rgb):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _root():
    src = TPL.read_text(encoding="utf-8")
    i = src.index(":root {")
    return src[i:src.index("\n  }", i)]


class TestUiTokens(unittest.TestCase):
    def test_no_bare_font_size_or_radius(self):
        """裸 font-size / border-radius / 内联字号 一律不许回。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ui_lint.main([])
        self.assertEqual(rc, 0, "token 层出现裸值：\n" + buf.getvalue())

    def test_token_scale_is_defined(self):
        """新增的 --fs-* / --r-* 必须真的定义过，否则 var() 全落空。"""
        root = _root()
        for name in ("--fs-1", "--fs-4", "--fs-10", "--r-xs", "--r-lg",
                     "--ctrl-h", "--info-lit", "--bad-lit", "--good-lit"):
            self.assertIn(f"{name}:", root, f"{name} 没在 :root 里定义")

    def test_dim_meets_wcag_on_l1_surface(self):
        """--dim 必须对 .card.l1 的底色达标。

        上一版注释声称 4.7:1，实际只有 4.02:1——那是对 --panel 算的，
        而 .mini 几乎都出现在 l1 的 rgb(22,31,44) 上。这里按真实底色算。
        """
        root = _root()
        m = re.search(r"--dim:\s*(#[0-9a-fA-F]{6})", root)
        self.assertIsNotNone(m, "--dim 没有字面值，算不出对比度")
        ratio = _ratio(_hex(m.group(1)), (22, 31, 44))   # .card.l1 实际底色
        self.assertGreaterEqual(
            ratio, 4.5,
            f"--dim {m.group(1)} 对 l1 底只有 {ratio:.2f}:1，WCAG AA 要 4.5:1")
