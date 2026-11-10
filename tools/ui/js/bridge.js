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

/* ==================== 坐标轴（area / bars 共用） ====================
 * 老口径是「Y 轴从 0 起算到最大值」。余额这种绝对水平（1490~1520）于是被压成
 * 一条贴顶的直线，整幅图 99% 的面积是空的——波动读不出来，图就白画了。
 *
 * 现在按窗口内实际高低点定窗口，只有零线**确实贴着数据**时才保留 0：
 *   - 跨零的曲线（累计盈亏、滚动期望）→ 0 是读数基准，必须留；
 *   - 从 0 起算的累计曲线 → 0 本来就在数据里；
 *   - 远离 0 的绝对水平（余额、权益）→ 丢掉 0，让波动铺满整幅。
 * 三档都由数据自己决定，不在调用点手工指定；实在要覆盖就传 opt.zero。
 */
function axisDomain(values, opt) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) {
    if (typeof v !== 'number' || !isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  // 全平（只有一笔、或数值完全不动）：给一个人工跨度，否则除零
  if (hi - lo < 1e-12) {
    const d = Math.abs(hi) > 1e-9 ? Math.abs(hi) * 0.02 : 1;
    lo -= d; hi += d;
  }
  const rawSpan = hi - lo;
  // 0 与数据最近一端的距离 ≤ 跨度 1/4 → 零线还看得见，保留它（有基准更好读）
  const zero = (opt && opt.zero != null) ? !!opt.zero
    : (lo <= 0 && hi >= 0) || Math.min(Math.abs(lo), Math.abs(hi)) <= rawSpan * 0.25;
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  const pad = (hi - lo) * 0.12;   // 上下留白：曲线不贴边
  return { lo: lo - pad, hi: hi + pad, zero };
}

// 刻度步长取 1/2/2.5/5×10^k——整齐且跨度不夸张（10→1.5k 那种四等分均值
// 落在窄区间里会印出 1490/1500 都变成 "1.5k" 的废标签）
function axisStep(lo, hi, intervals) {
  const raw = (hi - lo) / Math.max(1, intervals);
  if (!(raw > 1e-12)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return m * mag;
}

// 步长需要几位小数：2.5 → 1 位，0.25 → 2 位，20 → 0 位
function axisDecimals(step) {
  for (let d = 0; d <= 6; d++) {
    const s = step * Math.pow(10, d);
    if (Math.abs(s - Math.round(s)) < 1e-9 * Math.max(1, Math.abs(s))) return d;
  }
  return 6;
}

function axisLabel(v, step) {
  const clean = Math.abs(v) < step * 1e-6 ? 0 : v;
  // 千位左右不用 k：1490 印成 "1.5k" 就看不出 10 美元的差别了
  if (step >= 1000) {
    const k = clean / 1000;
    return k.toFixed(axisDecimals(step / 1000)) + 'k';
  }
  return clean.toFixed(axisDecimals(step));
}

function axisTicks(dom, step) {
  const out = [];
  const from = Math.ceil(dom.lo / step - 1e-9), to = Math.floor(dom.hi / step + 1e-9);
  for (let i = from; i <= to; i++) out.push(i * step);   // 用乘法不用累加，避免浮点漂移
  return out;
}

// 步长 + 落在窗口内的刻度。跨度刚好卡在两档之间时只会剩 1~2 根参考线，
// 那就细分一档——150px 高的图上只有两条线等于没画。
function axisScale(dom, intervals) {
  let step = axisStep(dom.lo, dom.hi, intervals);
  let ticks = axisTicks(dom, step);
  if (ticks.length < 3) {
    step = axisStep(dom.lo, dom.hi, intervals * 2);
    ticks = axisTicks(dom, step);
  }
  return { step, ticks };
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

  // Y 轴范围取所有系列的并集，但不再强制含 0（见 axisDomain）
  const dom = axisDomain(series.reduce((a, s) => a.concat(s.values), []), opt);
  const span = dom.hi - dom.lo;
  const y = v => H - padB - ((v - dom.lo) / span) * (H - padT - padB);
  // 面积填充/零线的基线：0 在窗口外时落到绘图区边缘，不让路径拖出画布
  const yBase = Math.max(padT, Math.min(H - padB, y(0)));

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

  // 网格：刻度落在整齐值上；刻度数量跟高度走（小图 4 格，最大化后最多 8 格），
  // 但窗口本身只由数据决定——同一份数据在大小图上形状必须一致。
  const intervals = H >= 320 ? Math.min(8, Math.round((H - padT - padB) / 70)) : 4;
  const ax = axisScale(dom, intervals);
  for (const val of ax.ticks) {
    svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y(val), y2: y(val), class: 'grid-line' }));
    const t = svgEl('text', { x: 4, y: y(val) + 3, class: 'lbl' });
    t.textContent = axisLabel(val, ax.step);
    svg.appendChild(t);
  }
  // 零线只在 0 真在窗口里时画；否则它会跑到画布外（余额这类图根本没有 0）
  if (dom.zero) svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: yBase, y2: yBase, class: 'zero-line' }));

  for (const s of series) {
    if (!s.values.length) continue;
    const x = i => xAt(s, i);
    if (s.area) {
      const gid = ensureGradient(svg, s.color, .22);
      const line = s.values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
      const area = `${line} L${x(s.values.length - 1).toFixed(1)},${yBase.toFixed(1)} L${x(0).toFixed(1)},${yBase.toFixed(1)} Z`;
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

// 带符号的柱顶数值。d = 小数位，由槽宽统一决定（同一张图里小数位必须一致，
// 否则一行 +27.4 一行 +0.85 看着像两种量纲）。
function barText(v, d) {
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

function drawBars(svgId, labels, values, opt) {
  CHART_REG[svgId] = { kind: 'bars', args: [labels, values, opt] };
  const svg = $(svgId);
  // 宽高参数化：最大化弹层重绘时传大尺寸；缺省 620x156 与老版一致
  const W = (opt && opt.W) || 620, H = (opt && opt.H) || 156,
        padL = 48, padR = 14, padT = 14, padB = 24;
  svg.innerHTML = '';
  if (!values.length) return;

  // 柱长 = 数值大小，零基线**不能**丢（截断的柱图是骗人的），所以这里不做
  // 「丢掉零」那套；但刻度要走整齐值，且只在有柱子那一侧留白——否则最高的
  // 柱子永远贴着顶边，读不出还有多少余量。
  let blo = Math.min(...values, 0), bhi = Math.max(...values, 0);
  if (bhi - blo < 1e-12) bhi = blo + 1;
  const bpad = (bhi - blo) * 0.08;
  if (bhi > 0) bhi += bpad;
  if (blo < 0) blo -= bpad;
  const bspan = bhi - blo;
  const y = v => H - padB - ((v - blo) / bspan) * (H - padT - padB);
  const y0 = y(0);
  const slot = (W - padL - padR) / values.length;
  const bw = Math.min(slot * 0.6, 40);

  // 柱顶数值：一眼能读，不必悬停。精度先按槽宽定一次——14 根柱子时槽只有
  // 40px，2 位小数放不下就整体降到 1 位，绝不出现相邻两格数字叠在一起。
  // 6.8px = 11px 等宽字体的字宽估算（--fs-1），只用来看放不放得下。
  const CHW = 6.8, budget = slot * 0.94;
  let vdec = 2;
  while (vdec > 0 && Math.max(...values.map(v => barText(v, vdec).length)) * CHW > budget) vdec--;

  const bint = H >= 320 ? Math.min(8, Math.round((H - padT - padB) / 70)) : 4;
  const bax = axisScale({ lo: blo, hi: bhi }, bint);
  for (const val of bax.ticks) {
    svg.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y(val), y2: y(val), class: 'grid-line' }));
    const t = svgEl('text', { x: 4, y: y(val) + 3, class: 'lbl' });
    t.textContent = axisLabel(val, bax.step);
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

    // 正柱标在柱顶上方、负柱标在柱底下方；贴边时夹进绘图区，
    // 不许压住上面那根柱子，也不许掉进日期行。
    const up = v >= 0;
    const vy = up ? Math.max(padT + 2, Math.min(yv, y0) - 5)
                  : Math.min(H - padB + 9, Math.max(yv, y0) + 12);
    const vt = svgEl('text', {
      x: cx, y: vy, class: 'bar-val ' + (up ? 'up' : 'down'), 'text-anchor': 'middle'
    });
    vt.textContent = barText(v, vdec);
    svg.appendChild(vt);
  });
}

