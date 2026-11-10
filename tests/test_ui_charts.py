"""图表渲染的回归测试（node 跑 bridge.js，不需要浏览器、不需要面板）。

背景一：老口径 Y 轴恒从 0 起算到最大值，于是「余额 1455~1476」这种绝对水平被
压成一条贴顶的直线，整幅图 99% 是空白——用户看到的就是一张没有信息量的图。
现在窗口按数据实际高低点定，只有零线确实贴着数据时才保留 0。

背景二：柱图只有轴刻度，读单根柱子的值必须先悬停。现在柱顶直接印数值，精度
按槽宽定一次（14 根柱子时 2 位小数放不下就整体降 1 位），相邻两格不许叠字。

这里钉住三件事：
  1. 远离 0 的数据不再含 0，且数据占到画布 70% 以上的高度（这正是原来的 bug）；
  2. 跨零 / 从 0 起算的曲线仍然保留零线（读数基准不能丢）；
  3. 刻度标签落在整齐值上、互不重复，窄区间不许印成一片 "1.5k"；
  4. 每根柱子都带符号数值，正柱标在上方、负柱标在下方，且不出画布。
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

// 折线图的峰谷标注：只取 .ext-val 的两个文本，连同画布尺寸一起给出，
// 调用方据此判断「标签在点的上方还是下方、有没有出画布」
function paintExt(id, values, opt) {
  drawArea(id, values, 'line-up', '#2ecc71', opt || {});
  const svg = made[id];
  const W = (opt && opt.W) || 620, H = (opt && opt.H) || 156;
  const padL = 44, padR = 14, padT = 14, padB = 20;
  const dom = axisDomain(values, opt || {});
  const yOf = v => H - padB - ((v - dom.lo) / (dom.hi - dom.lo)) * (H - padT - padB);
  const ext = svg.children
    .filter(c => c.tag === 'text' && /ext-val/.test(String(c.attrs.class || '')))
    .map(c => ({ t: c.textContent, x: Number(c.attrs.x), y: Number(c.attrs.y) }));
  return {
    ext, W, H, padL, padR, padT, padB,
    hiY: yOf(Math.max.apply(null, values)), loY: yOf(Math.min.apply(null, values)),
  };
}

// 柱图：数值标签（.bar-val）+ 柱体外框，按绘制顺序一一对应
function paintBars(id, labels, values, opt) {
  drawBars(id, labels, values, opt || {});
  const svg = made[id];
  const vals = svg.children.filter(c => c.tag === 'text' && /bar-val/.test(String(c.attrs.class || '')));
  const rects = svg.children.filter(c => c.tag === 'rect');
  return {
    n: vals.length,
    texts: vals.map(c => c.textContent),
    x: vals.map(c => Number(c.attrs.x)),
    y: vals.map(c => Number(c.attrs.y)),
    rects: rects.map(c => ({ y: Number(c.attrs.y), h: Number(c.attrs.height) })),
    H: (opt && opt.H) || 156,
    padT: 14, padB: 24,
  };
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

// 柱顶数值：疏（6 根，位置宽松）与密（14 根，槽只有 40px）两档，外加最大化
const sparse = paintBars('cSparse', ['a', 'b', 'c', 'd', 'e', 'f'],
                         [2.40, 0.60, -0.80, 1.20, 19.30, -12.90]);
const many = paintBars('cMany', ['1','2','3','4','5','6','7','8','9','10','11','12','13','14'],
                       [2.4, -3.1, 0.0, 5.8, -12.9, 0.6, 1.2, -0.8, 19.3, -4.4, 7.05, 0.0, -2.2, 3.6]);
const barBig = paintBars('cBig', ['a', 'b', 'c'], [12.0, -8.0, 3.0], { W: 1240, H: 600 });
out.barVals = { sparse: sparse, many: many, big: barBig };

// 峰谷标注：正常曲线、全平、单点、跨零、时间模式、最大化
out.ext = {
  bal: paintExt('cExtBal', bal),
  flat: paintExt('cExtFlat', flat),
  one: paintExt('cExtOne', [1500]),
  cross: paintExt('cExtCross', cross),
  ts: paintExt('cExtTs', [1, 2, 3], { ts: [1794300000, 1794303600, 1794307200] }),
  big: paintExt('cExtBig', [12.5, -8.0, 3.2], { W: 1240, H: 600 }),
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

    # ---- 柱顶数值 ----

    def test_every_bar_carries_its_value(self):
        """每根柱子都要有一个数值标签，一个不多一个不少。"""
        for name in ("sparse", "many", "big"):
            b = self.r["barVals"][name]
            self.assertEqual(b["n"], len(b["rects"]), name + " 的柱子与标签数量不一致")
            self.assertTrue(all(t.strip() for t in b["texts"]), name + " 有空标签")

    def test_value_labels_are_signed(self):
        """正数带 +、负数带 -，看图不用先看颜色。"""
        texts = self.r["barVals"]["sparse"]["texts"]
        self.assertEqual(texts, ["+2.40", "+0.60", "-0.80", "+1.20", "+19.30", "-12.90"])

    def test_decimal_places_consistent_within_one_chart(self):
        """同一张图里小数位必须一致，否则 +27.4 和 +0.85 看着像两种量纲。"""
        for name in ("sparse", "many", "big"):
            texts = self.r["barVals"][name]["texts"]
            decs = {len(t.split(".")[1]) if "." in t else 0 for t in texts}
            self.assertEqual(len(decs), 1, name + " 小数位不统一：" + str(texts))

    def test_dense_chart_drops_a_decimal_instead_of_overlapping(self):
        """14 根柱子时槽只有 40px，2 位小数放不下 → 整体降到 1 位，不是叠字。"""
        many = self.r["barVals"]["many"]
        decs = {len(t.split(".")[1]) if "." in t else 0 for t in many["texts"]}
        self.assertEqual(decs, {1}, many["texts"])
        # 预算：11px 等宽字宽 6.8px，标签宽度不许超过槽宽的 94%
        slot = (620 - 48 - 14) / 14
        for t in many["texts"]:
            self.assertLessEqual(len(t) * 6.8, slot * 0.94, "放不下却没降精度：" + t)

    def test_positive_above_negative_below(self):
        """正柱标在柱顶上方、负柱标在柱底下方——数字不能被柱子压住。"""
        for name in ("sparse", "many", "big"):
            b = self.r["barVals"][name]
            for i, (t, r) in enumerate(zip(b["texts"], b["rects"])):
                top, bottom = r["y"], r["y"] + r["h"]
                if t.startswith("-"):
                    self.assertGreater(b["y"][i], bottom - 1, f"{name}[{i}] 负值标签没在柱底下方")
                else:
                    self.assertLess(b["y"][i], top + 1, f"{name}[{i}] 正值标签没在柱顶上方")

    def test_labels_stay_inside_the_canvas(self):
        """贴边的柱子（最高/最低）标签要夹在画布内，不掉进日期行。"""
        for name in ("sparse", "many", "big"):
            b = self.r["barVals"][name]
            for t, y in zip(b["texts"], b["y"]):
                self.assertGreaterEqual(y, b["padT"] - 6, f"{name} 标签越出顶部：{t}")
                self.assertLessEqual(y, b["H"] - b["padB"] + 12, f"{name} 标签压到日期行：{t}")

    def test_value_labels_follow_the_bars_when_maximized(self):
        """最大化弹层重放同一份 args，标签要跟着变大的柱子走，位置关系不变。"""
        b = self.r["barVals"]["big"]
        self.assertEqual(b["n"], 3)
        smallest = min(r["h"] for r in b["rects"])
        self.assertGreater(smallest, 20, "大图里最短的柱子也该有可见高度")

    # ---- 折线图的峰谷数值 ----

    def test_only_the_two_extremes_are_labelled(self):
        """只标最高、最低两点；点数一多就全标会糊成一片。"""
        e = self.r["ext"]["bal"]
        self.assertEqual(sorted(x["t"] for x in e["ext"]), ["1455.10", "1476.40"])

    def test_high_label_above_point_low_label_below(self):
        """峰标在点上方、谷标在点下方，数字不压曲线。"""
        e = self.r["ext"]["bal"]
        hi, lo = sorted(e["ext"], key=lambda x: x["y"])   # 按位置认峰谷，不依赖绘制顺序
        self.assertLess(hi["y"], e["hiY"], "最高点的数字没在点上方")
        self.assertGreater(lo["y"], e["loY"], "最低点的数字没在点下方")

    def test_no_labels_when_there_are_no_extremes(self):
        """全平（高低同一个点）和单点都没有峰谷可言，标两个相同的数只会误导。"""
        self.assertEqual(self.r["ext"]["flat"]["ext"], [])
        self.assertEqual(self.r["ext"]["one"]["ext"], [])

    def test_extreme_labels_stay_inside_the_canvas(self):
        """首/末点常常正好是极值，标签必须夹在绘图区内，不许压到刻度或日期行。"""
        for name, e in self.r["ext"].items():
            if not e["ext"]:
                continue
            for x in e["ext"]:
                self.assertGreaterEqual(x["x"], e["padL"], f"{name} 标签压到 Y 轴刻度：{x}")
                self.assertLessEqual(x["x"], e["W"] - e["padR"], f"{name} 标签越出右边：{x}")
                self.assertGreaterEqual(x["y"], 9, f"{name} 标签越出顶部：{x}")
                self.assertLessEqual(x["y"], e["H"] - 2, f"{name} 标签越出底部：{x}")

    def test_extreme_labels_use_two_decimals(self):
        """小数位固定 2 位（金额/R 都是这个粒度）；轴步长 20 时也不许退成整数。"""
        for name in ("bal", "cross", "big"):
            for x in self.r["ext"][name]["ext"]:
                self.assertIn(".", x["t"], f"{name} 的极值没带小数：{x}")
                self.assertEqual(len(x["t"].split(".")[1]), 2, f"{name}：{x}")

    def test_ts_mode_keeps_low_label_off_the_date_row(self):
        """时间模式下底部有日期行，谷值标签要给那一行让位。"""
        e = self.r["ext"]["ts"]
        low = e["ext"][0]
        self.assertLessEqual(low["y"], e["H"] - e["padB"] - 2, "谷值标签压到底部日期行")


if __name__ == "__main__":
    unittest.main()
