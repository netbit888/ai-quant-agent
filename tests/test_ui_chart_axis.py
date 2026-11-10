"""图表坐标轴自适应的回归测试（node 跑 bridge.js，不需要浏览器、不需要面板）。

背景：老口径 Y 轴恒从 0 起算到最大值，于是「余额 1455~1476」这种绝对水平被压成
一条贴顶的直线，整幅图 99% 是空白——用户看到的就是一张没有信息量的图。
现在窗口按数据实际高低点定，只有零线确实贴着数据时才保留 0。

这里钉住三件事：
  1. 远离 0 的数据不再含 0，且数据占到画布 70% 以上的高度（这正是原来的 bug）；
  2. 跨零 / 从 0 起算的曲线仍然保留零线（读数基准不能丢）；
  3. 刻度标签落在整齐值上、互不重复，窄区间不许印成一片 "1.5k"。
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools" / "ui" / "js" / "bridge.js"

# 假 DOM 只覆盖 drawArea/drawBars 实际用到的几个方法（照 tools/smoke_dashboard.js 的写法）
HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');

const made = {};
function mk(tag) {
  return {
    tag, children: [], attrs: {}, textContent: '',
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    set innerHTML(v) { this.children = []; },
    addEventListener() {},
    classList: { add() {}, remove() {}, contains() { return false; } },
  };
}
global.document = {
  getElementById(id) { return made[id] || (made[id] = mk('svg')); },
  createElement() { return mk('div'); },
  createElementNS(ns, tag) { return mk(tag); },
  body: { appendChild() {} },
};

eval(code);

function paint(id, values, opt) {
  drawArea(id, values, 'line-up', '#2ecc71', opt || {});
  const svg = made[id];
  const labels = svg.children.filter(c => c.tag === 'text').map(c => c.textContent);
  const zeroLine = svg.children.some(c => c.attrs && c.attrs.class === 'zero-line');
  const grid = svg.children.filter(c => c.attrs && c.attrs.class === 'grid-line').length;
  return { labels, zeroLine, grid };
}
function span(values, opt) {
  const d = axisDomain(values, opt || {});
  const lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
  return { lo: d.lo, hi: d.hi, zero: d.zero, fill: (hi - lo) / (d.hi - d.lo) };
}

const out = {};
const bal = [1455.8, 1456.2, 1455.1, 1456.8, 1476.4, 1464.6];
const cross = [-0.3, 0.5, 0.1];
const cum = [0, 2.5, 5.9];
const flat = [1500, 1500];
const big = [100000, 104000];

out.balance = Object.assign(span(bal), paint('cBal', bal));
out.balanceWin = Object.assign(span(bal), paint('cBalWin', bal, { W: 1240, H: 150 }));
out.cross = Object.assign(span(cross), paint('cCross', cross));
out.cum = Object.assign(span(cum), paint('cCum', cum));
out.flat = Object.assign(span(flat), paint('cFlat', flat));
out.big = span(big);
out.forced = span([100, 110], { zero: true });

drawBars('cBars', ['a', 'b', 'c', 'd'], [27.4, -15.0, 0.0, -6.7]);
const bars = made.cBars;
out.bars = {
  labels: bars.children.filter(c => c.tag === 'text').map(c => c.textContent),
  zeroLine: bars.children.some(c => c.attrs && c.attrs.class === 'zero-line'),
  rects: bars.children.filter(c => c.tag === 'rect')
    .map(c => ({ y: parseFloat(c.attrs.y), h: parseFloat(c.attrs.height) })),
  H: 156, padT: 14, padB: 24,
};

console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "没有 node，跳过图表轴测试")
class ChartAxisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = cls._run()

    @staticmethod
    def _run():
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(HARNESS)
            path = fh.name
        try:
            out = subprocess.run([shutil.which("node"), path, str(BRIDGE)],
                                 cwd=str(ROOT), capture_output=True, text=True,
                                 timeout=60)
        finally:
            try:
                pathlib.Path(path).unlink()
            except OSError:
                pass
        if out.returncode != 0:
            raise AssertionError("node 执行失败：" + (out.stderr or "")[:800])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_balance_does_not_start_at_zero(self):
        """余额 1455~1476 不许把 0 拉进窗口——那正是这条 bug 的现场。"""
        b = self.r["balance"]
        self.assertFalse(b["zero"], "余额曲线的窗口里不该有 0")
        self.assertGreater(b["lo"], 1000)

    def test_balance_data_fills_the_canvas(self):
        """数据要占到画布七成以上；老口径只有 21/1476 ≈ 1.4%。"""
        self.assertGreater(self.r["balance"]["fill"], 0.7,
                           "数据只占画布的 %.0f%%，曲线还是平的" % (self.r["balance"]["fill"] * 100))

    def test_balance_has_no_zero_line(self):
        """0 不在窗口里就不该画零线（画了会落到画布外）。"""
        self.assertFalse(self.r["balance"]["zeroLine"])
        self.assertFalse(self.r["balanceWin"]["zeroLine"])
        self.assertEqual(self.r["balance"]["labels"], self.r["balanceWin"]["labels"],
                         "同一份数据在大小图上的轴必须一致")

    def test_balance_labels_are_distinct_and_exact(self):
        """1455/1460/1465… 而不是四行一模一样的 "1.5k"。"""
        labels = self.r["balance"]["labels"]
        self.assertGreaterEqual(len(labels), 3, labels)
        self.assertEqual(len(set(labels)), len(labels), "刻度标签重复：" + str(labels))
        self.assertFalse([s for s in labels if s.endswith("k")],
                         "窄区间的标签不该缩写成 k：" + str(labels))

    def test_zero_kept_for_crossing_series(self):
        """跨零曲线（滚动期望）必须留零线，且上下都要留出边。"""
        c = self.r["cross"]
        self.assertTrue(c["zero"])
        self.assertTrue(c["zeroLine"], "跨零曲线丢了零线")
        self.assertLess(c["lo"], 0)
        self.assertGreater(c["hi"], 0.5)

    def test_zero_kept_for_series_starting_at_zero(self):
        """从 0 起算的累计盈亏：0 本来就在数据里。"""
        self.assertTrue(self.r["cum"]["zero"])

    def test_flat_series_does_not_collapse(self):
        """全平时给人工跨度，否则 y 映射直接除零。"""
        f = self.r["flat"]
        self.assertGreater(f["hi"] - f["lo"], 0)
        self.assertGreaterEqual(len(set(self.r["flat"]["labels"])), 2)

    def test_thousands_still_use_k(self):
        self.assertGreaterEqual(self.r["big"]["fill"], 0.7)
        self.assertGreater(self.r["big"]["lo"], 90000)

    def test_bars_keep_zero_baseline(self):
        """柱长 = 数值大小，零基线不能丢（截断的柱图会骗人）。"""
        b = self.r["bars"]
        self.assertTrue(b["zeroLine"], "柱图丢了零基线")
        self.assertEqual(len(b["rects"]), 4)
        self.assertIn("0", b["labels"], "零基线没有刻度标签：" + str(b["labels"]))
        # 柱子不许画到画布外（负数那根最容易越界）
        top, bottom = b["padT"] - 0.5, b["H"] - b["padB"] + 0.5
        for r in b["rects"]:
            self.assertGreaterEqual(r["y"], top, r)
            self.assertLessEqual(r["y"] + r["h"], bottom, r)

    def test_domain_can_be_forced(self):
        self.assertLessEqual(self.r["forced"]["lo"], 0)


if __name__ == "__main__":
    unittest.main()
