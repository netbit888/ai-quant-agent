/* ==================== 单笔交易溯源 ====================
 * 点「详情」按钮后展示一笔交易的全链路：
 * 原料（14 根 K 线 / tick）→ 结构（ER/位置判定）→ 程序建议 vs 模型判断
 * → 执行结果 → 复盘。数据来自 GET /api/trade?ticket=，只读、不花钱。
 * 终端离线时原料部分会如实标注「无法重建」，不冒充。 */
const PROG_DIR_TEXT = { 1: '做多', '-1': '做空', 0: '不动手（无建议）' };

function tradeMaskClose() {
  maskClose($('tradeMask'));
}

/* ==================== 图表最大化查看 ====================
 * 7 张图在渲染时已把入参登记进 CHART_REG（drawArea/drawBars 入口），
 * 打开弹层 = 拿同一份 args 换个大 svg 重放，与原渲染同源同口径。
 * 关键：viewBox 必须等于实际渲染像素（1:1 映射）。模板全局
 * svg { height:156px } + preserveAspectRatio="none" 会按视口缩放坐标，
 * 固定 viewBox 的文字在大图上会被拉花——动态设置 viewBox 后映射比恒为 1。 */
const CHART_TITLES = {
  chartRolling: '滚动期望（近 20 笔 · 100 笔对照）',
  chartBalance: '账户余额（每日快照）',
  chartEquity: '账户余额曲线（含出入金）',
  chartDaily: '逐日盈亏',
  chartProfit: '净盈利（每轮）',
  chartDd: '最大回撤 %（每轮）',
  chartYear: '逐年净盈利',
};
let chartMaxId = null;

function chartMaskDraw() {
  const reg = chartMaxId && CHART_REG[chartMaxId];
  if (!reg) return;
  const wrap = $('chartBigWrap');
  // 弹层刚从 display:none 里放出时同步读 clientWidth 即可拿到真实尺寸
  const cw = Math.max(320, wrap.clientWidth || 0);
  const ch = Math.max(200, wrap.clientHeight || 0);
  $('chartBig').setAttribute('viewBox', `0 0 ${cw} ${ch}`);
  if (reg.kind === 'bars') {
    const [labels, values, opt] = reg.args;
    drawBars('chartBig', labels, values, { W: cw, H: ch });
  } else {
    const [values, colorClass, color, opt] = reg.args;
    drawArea('chartBig', values, colorClass, color, Object.assign({}, opt, { W: cw, H: ch }));
  }
}

function openChartMax(id) {
  const reg = CHART_REG[id];
  if (!reg || !reg.args) return;
  chartMaxId = id;
  $('chartMaskTitle').textContent = CHART_TITLES[id] || id;
  $('chartMaskLegend').textContent = (id === 'chartRolling')
    ? '实线 = 近 20 笔 · 虚线 = 近 100 笔（对照） · 悬停看每个时点'
    : '数据截至最近一次刷新';
  $('chartMask').classList.remove('hidden');
  chartMaskDraw();
  maskOpen($('chartMask'), { focus: $('chartMask').querySelector('.btn-cancel') });
}

function chartMaskClose() {
  maskClose($('chartMask'));
  chartMaxId = null;
  $('chartBig').innerHTML = '';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('chartMask').classList.contains('hidden')) chartMaskClose();
});
// 弹层可见时窗口尺寸变化要重画（viewBox 跟着视口走）。
// 冒烟环境的假 DOM 没有 window，顶层代码不能裸引用。
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('resize', () => {
    if (chartMaxId && !$('chartMask').classList.contains('hidden')) chartMaskDraw();
  });
}

function tradeRow(label, val, cls) {
  return `<div><span class="mini">${esc(label)}　</span><span class="${cls || ''}">${val}</span></div>`;
}

function tradeDetailShow(ticket) {
  $('tradeMask').classList.remove('hidden');
  maskOpen($('tradeMask'), { focus: $('tradeMask').querySelector('.btn-cancel') });
  $('tdTitle').textContent = `交易详情 · #${ticket}`;
  $('tdBody').textContent = '读取中…';
  fetch(`/api/trade?ticket=${ticket}`)
    .then(r => r.json())
    .then(d => { $('tdBody').innerHTML = d.ok ? renderTradeDetail(d) : `读取失败：${esc(d.error)}`; })
    .catch(e => { $('tdBody').textContent = `请求失败：${e}`; });
}

function renderTradeDetail(d) {
  const o = d.open || {}, c = d.close || {}, res = d.result || {},
        j = d.judgement || {}, rc = d.recon || {}, rv = d.review;
  const dirText = o.direction > 0 ? '多' : '空';
  // 日志里 open 行的价格字段叫 price；state.position 里才叫 price_open。
  // 只认一个会显示成「@ —」（真实账本踩过，别再用合成数据掩盖）。
  const openPx = o.price_open != null ? o.price_open : o.price;

  // 1. 执行结果
  let html = `<div class="td-sec"><b>执行结果</b><div class="td-grid">`
    + tradeRow('方向 / 手数', `${dirText} ${o.lots} 手 @ ${fmt(openPx, 3)}`)
    + tradeRow('盈亏', money(res.pnl), res.pnl > 0 ? 'pos' : (res.pnl < 0 ? 'neg' : ''))
    + tradeRow('持仓时长', res.held_sec != null ? `${res.held_sec} 秒` : '—')
    + tradeRow('平仓方式', res.exit_source === 'llm' ? '模型判断平仓'
        : res.exit_source === 'sl' ? '程序硬止损'
        : res.exit_source === 'timeout' ? '持仓超时'
        : res.exit_source === 'external' ? '外部平仓（手动/强平）'
        // 保本止损（2026-10-29）：券商端那条线打的，成交价应在开仓价附近。
        : res.exit_source === 'be' ? '保本止损（券商端 SL）'
        : res.exit_source === 'be_local' ? '保本止损（程序代平）'
        : esc(res.exit_source || '—'))
    + `</div></div>`;

  // 2. 程序判断 vs 模型判断
  const pd = j.prog_dir, md = j.model_dir;
  const pdText = pd == null ? '—（旧数据未记录）' : (PROG_DIR_TEXT[pd] || pd);
  const vdCls = j.verdict === '一致' ? 'td-verdict-ok' : 'td-verdict-dif';
  html += `<div class="td-sec"><b>程序判断 vs 模型判断</b><div class="td-grid">`
    + tradeRow('程序建议', pdText)
    + tradeRow('模型方向', md == null ? '—' : (md > 0 ? '做多' : '做空'))
    + tradeRow('模型 p_up', j.p_up != null ? fmt(j.p_up * 100, 0) + '%' : '—')
    + tradeRow('模型自称同意', j.agree_field === true ? '是' : j.agree_field === false ? '<b style="color:#f0b429">否</b>' : '未答')
    + `</div>`
    + (j.verdict ? `<div style="margin-top:6px">裁决：<span class="${vdCls}">${esc(j.verdict)}</span></div>` : '')
    + `<div class="mini" style="margin-top:5px">开仓理由：${esc(o.reason || '—')}</div>`
    + `</div>`;

  // 3. 特征与结构。快照优先——它是决策当下落的盘，比重建可靠；
  //    重建只是补 K 线原文，补不到也不影响这些数。
  html += `<div class="td-sec"><b>决策时的特征与结构</b>`;
  const ft = rc.feat;
  if (ft && ft.ok) {
    html += `<div class="mini" style="margin-top:4px">决策时刻落盘（非重建）· 窗口 ${ft.n} 根 · ${esc(ft.symbol || '')} ${esc(ft.tf || '')}</div>`;
    html += tradeRow('ER / 净移动',
      `${fmt(ft.er, 2)}　${ft.drift > 0 ? '+' : ''}${fmt(ft.drift, 3)}`);
    html += tradeRow('收盘位置', ft.pos == null ? '—' : (ft.pos * 100).toFixed(0) + '%');
    html += tradeRow('振幅',
      `${fmt(ft.span, 3)}（${fmt(ft.span_atr, 1)}× 单根均幅，ATR=${fmt(ft.atr, 3)}）`);
    html += tradeRow('连阳连阴 / 状态', `${ft.streak} 根　${esc(ft.regime || '—')}`);
    html += tradeRow('程序建议',
      PROG_DIR_TEXT[ft.prog_dir] != null ? PROG_DIR_TEXT[ft.prog_dir] : '—');
  }
  if (rc.mtf) {
    html += tradeRow('M5 背景',
      `ER=${fmt(rc.mtf.er, 2)}　位置 ${rc.mtf.pos == null ? '—' : (rc.mtf.pos * 100).toFixed(0) + '%'}`
      + `　${fmt(rc.mtf.span_atr, 1)}× 均幅　${esc(rc.mtf.regime || '')}`);
  }
  if (!ft && rc.note) html += `<div class="td-note">${esc(rc.note)}——原始行情没存盘，这笔也没有特征快照（2026-10-26 之前的老单），只能靠终端重建。</div>`;
  if (rc.structure) html += `<div class="mini" style="margin-top:6px">重建的结构行：</div><pre>${esc(rc.structure)}</pre>`;
  if (rc.prog_dir != null && rc.structure && !ft) html += `<div class="mini" style="margin-top:4px">由以上结构算出程序建议：${PROG_DIR_TEXT[rc.prog_dir] || rc.prog_dir}</div>`;
  if (rc.bars) html += `<details style="margin-top:8px"><summary class="mini" style="cursor:pointer">重建的 14 根 K 线</summary><pre>${esc(rc.bars)}</pre></details>`;
  if (rc.ticks && !rc.ticks.startsWith('（')) html += `<details style="margin-top:8px"><summary class="mini" style="cursor:pointer">tick 聚合数据</summary><pre>${esc(rc.ticks)}</pre></details>`;
  html += `</div>`;

  // 4. 平仓与复盘
  if (c && c.ts) {
    html += `<div class="td-sec"><b>平仓与复盘</b><div class="mini" style="margin-top:4px">平仓理由：${esc(c.reason || '—')}</div>`;
    if (rv && rv.lesson) html += `<div class="mini" style="margin-top:4px">复盘教训（${esc(rv.regime || '通用')}）：${esc(rv.lesson)}</div>`;
    html += `</div>`;
  }
  return html;
}

async function stopAutoTrade() {
  let ok = false;
  try {
    ok = await confirmApiCall({
      title: '关闭 AI 并平掉所有仓位？',
      desc: '停掉 AI 做单，然后向券商发送平仓单：账户上所有持仓都会平掉，包括你自己手动开的那些。平完不可撤销。',
      cost: '会立刻平仓',
      okText: '关闭并平仓',
    });  } catch (e) {
    atSay('<b>确认弹窗没能打开：</b>' + esc(String(e)), 'err');
    return;
  }
  if (!ok) return;

  atSay('');
  $('atStop').disabled = true;
  try {
    const r = await fetch('/api/autotrade/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, flatten: true, account: curLogin || undefined }),
    });
    const j = await r.json();
    if (j.ok) {
      atSay('<b>已停止</b>：' + esc(j.note || `平掉 ${j.count || 0} 笔`), 'ok');
    } else {
      atSay('<b>关闭失败：</b>' + esc(j.error ||
            ('有 ' + (j.failed || []).length + ' 笔平仓失败，请回终端确认')), 'err');
    }
  } catch (e) {
    atSay('<b>关闭请求失败：</b>' + esc(String(e)), 'err');
  } finally {
    $('atStop').disabled = false;
    loadAutoTrade();
    refreshLive(true);
  }
}

