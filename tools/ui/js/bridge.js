/* ==================== 原生窗口桥（pywebview）====================
   打包版会把页面装进一个原生窗口，由 Python 侧通过 js_api 暴露几个方法。
   浏览器模式 / Node 假 DOM（tools/smoke_dashboard.js）里 window.pywebview
   不存在，下面每个函数都必须退化成 no-op，而且**绝不抛**——
   这一段在冒烟测试里是被真执行的，抛一下整条冒烟就红了。

   另外不能在解析期判断桥是否就绪：pywebview 的注入晚于本脚本执行。 */
function isNative() {
  try {
    return !!(typeof window !== 'undefined' && window.pywebview && window.pywebview.api);
  } catch (e) { return false; }
}

/* 调桥。没桥 / 方法不存在 / 序列化失败 → 一律返回 null，调用方据此降级。 */
function nativeApi(name) {
  try {
    if (!isNative() || typeof window.pywebview.api[name] !== 'function') return null;
    return window.pywebview.api[name].apply(
      window.pywebview.api, Array.prototype.slice.call(arguments, 1));
  } catch (e) { return null; }
}

const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);
const signClass = v => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
// epoch 秒 → 「MM-DD HH:mm」（本地时间）。图表时间轴和 hover 浮窗共用。
function fmtTsMin(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

let gradSeq = 0;

// 最近一次 /api/autotrade 的结果。全屏幕布用它显示状态——
// 幕布自己的刷新跟着 1 秒轮询走，不额外发请求。
let lastAutoData = null;

function ensureGradient(svg, color, opacityTop) {
  const id = 'g' + (++gradSeq);
  const defs = svgEl('defs', {});
  const lg = svgEl('linearGradient', { id, x1: '0', y1: '0', x2: '0', y2: '1' });
  lg.appendChild(svgEl('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': opacityTop }));
  lg.appendChild(svgEl('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': '0' }));
  defs.appendChild(lg);
  svg.appendChild(defs);
  return id;
}

// 图表数据登记表：drawArea/drawBars 每次渲染时把入参原样存进来，
// 最大化弹层用同一份 args 换个大 svgId/宽高重放——与原渲染同源同口径，
// 7 处调用点零改动。数据新鲜度 = 最近一次轮询渲染。
const CHART_REG = {};
let chartTipEl = null;
function tipEl() {
  if (!chartTipEl) {
    chartTipEl = document.createElement('div');
    chartTipEl.className = 'chart-tip hidden';
    document.body.appendChild(chartTipEl);
  }
  return chartTipEl;
}

function drawArea(svgId, values, colorClass, color, opt) {
  CHART_REG[svgId] = { kind: 'area', args: [values, colorClass, color, opt] };
  const svg = $(svgId);
  const W = (opt && opt.W) || 620, H = (opt && opt.H) || 156;
  const padL = 44, padR = 14, padT = 14, padB = 20;
  svg.innerHTML = '';
  if (!values.length) return;

  // 多系列（opt.series）是可选增强；没传时退化为单系列，行为与老版一致。
  // series 顺序 = 绘制顺序（后画的在上层）。
  const hasEnh = !!(opt && (opt.series || opt.ts || opt.hover));
  const series = hasEnh && opt.series && opt.series.length
    ? opt.series
    : [{ values, colorClass, color, area: true, ts: hasEnh ? opt.ts : null }];
  const primary = series.find(s => s.primary) || series[series.length - 1];

  // Y 轴范围取所有系列的并集，仍含 0（与老口径一致）
  let max = 0, min = 0;
  for (const s of series) for (const v of s.values) { if (v > max) max = v; if (v < min) min = v; }
  const span = (max - min) || 1;
  const y = v => H - padB - ((v - min) / span) * (H - padT - padB);

  // X 映射：系列带 ts（且首尾可分）时按时间对齐——20 笔线和 100 笔线点数不同、
  // 终点都是最近一笔，按索引映射会把两条线的日期对错位；时间映射才能纵向对齐。
  let tsMin = Infinity, tsMax = -Infinity;
  for (const s of series) {
    if (s.ts && s.ts.length === s.values.length) {
      if (s.ts[0] < tsMin) tsMin = s.ts[0];
      if (s.ts[s.ts.length - 1] > tsMax) tsMax = s.ts[s.ts.length - 1];
    }
  }
  const timeMode = tsMax > tsMin;
  const xIdx = (i, n) => n === 1 ? (padL + W - padR) / 2
              : padL + i * (W - padL - padR) / (n - 1);
  const xOfTs = t => padL + (t - tsMin) / (tsMax - tsMin) * (W - padL - padR);
  const xAt = (s, i) => (timeMode && s.ts) ? xOfTs(s.ts[i]) : xIdx(i, s.values.length);

  for (let g = 0; g <= 3; g++) {
    const val = min + span * g / 3;
    svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y(val), y2: y(val), class: 'grid-line' }));
    const t = svgEl('text', { x: 4, y: y(val) + 3, class: 'lbl' });
    t.textContent = Math.abs(val) >= 1000 ? (val / 1000).toFixed(1) + 'k' : val.toFixed(val % 1 ? 1 : 0);
    svg.appendChild(t);
  }
  svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y(0), y2: y(0), class: 'zero-line' }));

  for (const s of series) {
    if (!s.values.length) continue;
    const x = i => xAt(s, i);
    if (s.area) {
      const gid = ensureGradient(svg, s.color, .22);
      const line = s.values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
      const y0 = y(0);
      const area = `${line} L${x(s.values.length - 1).toFixed(1)},${y0.toFixed(1)} L${x(0).toFixed(1)},${y0.toFixed(1)} Z`;
      svg.appendChild(svgEl('path', { d: area, fill: `url(#${gid})`, stroke: 'none' }));
    }
    const lp = s.values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    svg.appendChild(svgEl('path', { d: lp, class: s.colorClass }));
  }

  // X 轴日期刻度：只在时间模式下画，放底部 padB 空带里
  if (timeMode) {
    const k = (opt && opt.ticks) || 5;
    const p2 = n => String(n).padStart(2, '0');
    for (let j = 0; j < k; j++) {
      const t = tsMin + (tsMax - tsMin) * j / (k - 1);
      const tx = xOfTs(t);
      const d = new Date(t * 1000);
      svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: H - padB, y2: H - padB + 4, class: 'grid-line' }));
      const txt = svgEl('text', {
        x: tx, y: H - 6, class: 'lbl',
        'text-anchor': j === 0 ? 'start' : (j === k - 1 ? 'end' : 'middle')
      });
      txt.textContent = `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
      svg.appendChild(txt);
    }
  }

  // 全量 hover：透明 rect 盖住绘图区（必须最后 append 保证在顶层），
  // mousemove 反查最近点 → 竖向参考线 + fixed 定位浮窗。
  // 浮窗不进 svg：preserveAspectRatio="none" 会拉伸 svg 内的文字。
  if (hasEnh && opt.hover && primary.values.length) {
    const pv = primary.values;
    const pts = pv.map((v, i) => [xAt(primary, i), y(v), i]);
    const hline = svgEl('line', {
      x1: -9, x2: -9, y1: padT, y2: H - padB, class: 'hover-line',
      visibility: 'hidden', 'pointer-events': 'none'
    });
    const hit = svgEl('rect', {
      x: padL, y: padT, width: W - padL - padR, height: H - padT - padB, fill: 'transparent'
    });
    hit.addEventListener('mousemove', e => {
      const r = svg.getBoundingClientRect();
      const mx = (e.clientX - r.left) / r.width * W;
      let best = 0, bd = Infinity;
      for (const p of pts) { const d = Math.abs(p[0] - mx); if (d < bd) { bd = d; best = p[2]; } }
      hline.setAttribute('x1', pts[best][0]);
      hline.setAttribute('x2', pts[best][0]);
      hline.setAttribute('visibility', 'visible');
      const tip = tipEl();
      tip.innerHTML = opt.hover(best).map(s => `<div>${s}</div>`).join('');
      tip.classList.remove('hidden');
      const tw = tip.offsetWidth || 120;
      let lx = e.clientX + 14;
      if (lx + tw > window.innerWidth - 8) lx = e.clientX - tw - 14;
      tip.style.left = lx + 'px';
      tip.style.top = Math.max(8, e.clientY + 14) + 'px';
    });
    hit.addEventListener('mouseleave', () => {
      hline.setAttribute('visibility', 'hidden');
      tipEl().classList.add('hidden');
    });
    svg.appendChild(hline);
    svg.appendChild(hit);
  } else if (values.length <= 40) {
    values.forEach((v, i) => {
      const c = svgEl('circle', { cx: xAt(primary, i), cy: y(v), r: 3, class: 'pt' });
      const title = svgEl('title', {});
      title.textContent = v;
      c.appendChild(title);
      svg.appendChild(c);
    });
  }
}

function drawBars(svgId, labels, values, opt) {
  CHART_REG[svgId] = { kind: 'bars', args: [labels, values, opt] };
  const svg = $(svgId);
  // 宽高参数化：最大化弹层重绘时传大尺寸；缺省 620x156 与老版一致
  const W = (opt && opt.W) || 620, H = (opt && opt.H) || 156,
        padL = 48, padR = 14, padT = 14, padB = 24;
  svg.innerHTML = '';
  if (!values.length) return;

  const max = Math.max(...values, 0), min = Math.min(...values, 0);
  const span = (max - min) || 1;
  const y = v => H - padB - ((v - min) / span) * (H - padT - padB);
  const y0 = y(0);
  const slot = (W - padL - padR) / values.length;
  const bw = Math.min(slot * 0.6, 40);

  for (let g = 0; g <= 3; g++) {
    const val = min + span * g / 3;
    svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y(val), y2: y(val), class: 'grid-line' }));
    const t = svgEl('text', { x: 4, y: y(val) + 3, class: 'lbl' });
    t.textContent = Math.abs(val) >= 1000 ? (val / 1000).toFixed(1) + 'k' : val.toFixed(0);
    svg.appendChild(t);
  }
  svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y0, y2: y0, class: 'zero-line' }));

  const upGrad = ensureGradient(svg, '#2ecc71', .75);
  const downGrad = ensureGradient(svg, '#ff4d4f', .75);

  values.forEach((v, i) => {
    const cx = padL + slot * (i + 0.5);
    const yv = y(v);
    const rect = svgEl('rect', {
      x: cx - bw / 2, y: Math.min(yv, y0), width: bw,
      height: Math.max(Math.abs(yv - y0), 1.5),
      fill: v >= 0 ? `url(#${upGrad})` : `url(#${downGrad})`,
      rx: 2
    });
    const title = svgEl('title', {});
    title.textContent = `${labels[i]}: ${Number(v).toFixed(2)}`;
    rect.appendChild(title);
    svg.appendChild(rect);

    const t = svgEl('text', { x: cx, y: H - 6, class: 'lbl', 'text-anchor': 'middle' });
    t.textContent = labels[i];
    svg.appendChild(t);
  });
}

