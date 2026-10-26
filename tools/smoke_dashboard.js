/**
 * 面板渲染冒烟测试（不需要浏览器）
 *
 * 做法：用假 DOM 执行 dashboard_template.html 里的脚本，调 renderLive(真实 API 响应)，
 * 检查每个渲染分支是否抛错、关键字段是否被赋值。曾经靠它抓到
 * /api/live 漏传 trades 导致「最近成交」整块空白的问题。
 *
 * 用法：先起面板，然后
 *   curl -s --noproxy '*' "http://127.0.0.1:8765/api/live" -o live_probe.json
 *   node tools/smoke_dashboard.js            # 也可指定探针：node tools/smoke_dashboard.js other.json
 *
 * 断言原则：**期望值一律从探针现算，不写死账户数字。**
 * 以前这里写死了旧账户的 11.60 USD / -119.03，换个账户就整片报错——
 * 那种失败跟渲染对不对无关，只会让人误判。
 */
const fs = require('fs');
const src = fs.readFileSync('tools/dashboard_template.html', 'utf8');
const probePath = process.argv[2] || 'live_probe.json';

let liveJson;
try {
  liveJson = JSON.parse(fs.readFileSync(probePath, 'utf8'));
} catch (e) {
  console.log(`读不到探针 ${probePath}：${e.message}`);
  console.log('先起面板，再抓一份：curl -s --noproxy \'*\' "http://127.0.0.1:8765/api/live" -o live_probe.json');
  process.exit(2);
}

const script = src.match(/<script>([\s\S]*?)<\/script>/)[1];

const made = {};
function makeEl(id) {
  const el = {
    id, textContent: '', innerHTML: '', className: '', disabled: false, title: '',
    value: '', placeholder: '',
    dataset: {}, children: [],
    // style 必须有：renderLive 会写 syncEl.style.display，缺这个字段整块渲染直接抛错
    style: {},
    classList: {
      _s: new Set(['hidden']),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    },
    querySelectorAll() { return []; },
    setAttribute() {}, appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    focus() {}, blur() {},
    addEventListener() {},
  };
  made[id] = el;
  return el;
}
global.document = {
  getElementById: id => made[id] || makeEl(id),
  querySelectorAll: () => [],
  createElementNS: () => makeEl('svg' + Math.random()),
  createElement: tag => makeEl('created_' + tag + '_' + Math.random()),
  // body 必须存在：切换标签页会写 document.body.dataset.view（用来控制
  // 只属于某一页的顶部 pill），没有 body 整段脚本会在这里炸。
  body: makeEl('body'),
  hidden: false,
  addEventListener() {},
};
// 记下每次 fetch，用来断言「该拦的请求真的没发出去」
const fetchCalls = [];
let llmJson = null;
global.fetch = async (url, opts) => {
  const u = String(url);
  fetchCalls.push({ url: u, opts });
  if (u.includes('/api/llm/switch')) {
    return { json: async () => ({ ok: true, model: 'deepseek-chat', reply: '可用', elapsed: 1.5 }) };
  }
  if (u.includes('/api/llm')) return { json: async () => llmJson || { ok: false } };
  // 教训库：跨场次统计。返回一段真实形状的文本，用来断言面板把它渲染出来了。
  if (u.includes('/api/lessons')) {
    return { json: async () => ({
      ok: true,
      report: { lessons: 3, sessions: [{ start: 'a', end: 'b', n: 2, closes: 5 }] },
      text: '教训库 3 条 · 1 个场次\n场 1: a → b  平仓 5 笔  教训 2 条',
    }) };
  }
  return { json: async () => liveJson };
};
global.setInterval = () => 0;
global.URLSearchParams = class { constructor() {} set() {} toString() { return ''; } };

let failed = false;
(async () => {
try {
  const body = script.replace(/pollState\(\);[\s\S]*$/, '');
  const fn = new Function(body + '\n; return {renderLive, renderQuality, renderAutoTrade, atSay, atErrHtml, startAutoTrade, stopAutoTrade, resetAutoTrade, resetLessons, loadLessons, enterTradingFullscreen, exitTradingFullscreen, updateCurtain, loadLlm, onLlmProviderChange, switchLlm, closeModal, reasonCn, mins, money, fmt, ts2str, renderTradeDetail, PROG_DIR_TEXT, atSpreadCostUsd, atSymbol, atSymbolLabel, fillSymbolOptions};');
  const api = fn();
  api.renderLive(liveJson);
  console.log(`renderLive 执行成功（账户 ${liveJson.account && liveJson.account.login}）`);

  const a = liveJson.account || {};
  const s = liveJson.stats || {};
  const mode = a.trade_mode === 'real' ? '真实账户'
    : (a.trade_mode === 'demo' ? '模拟账户' : (a.trade_mode || '—'));
  const checks = [
    ['账户', made.acctLogin.textContent, a.login ? '#' + a.login : '—'],
    ['模式', made.acctMode.textContent, mode],
    ['余额', made.acctBalance.textContent, `${api.fmt(a.balance)} ${a.currency || ''}`],
    ['累计净利', made.lvNet.textContent, api.money(s.net)],
    ['回撤', made.lvDd.textContent, s.max_dd_pct != null ? api.fmt(s.max_dd_pct, 1) + '%' : '—'],
  ];
  for (const [name, val, want] of checks) {
    const ok = want ? String(val).includes(want) : true;
    console.log(`  ${ok ? '✓' : '✗'} ${name}: ${val}`);
    if (!ok) failed = true;
  }

  const q = liveJson.quality || {};
  console.log(`\n近期质量：${(q.windows || []).length} 个窗口，滚动点 ${(q.rolling || []).length}`);
  const tabsHtml = made.winTabs.innerHTML;
  const tabCount = (tabsHtml.match(/class="win-tab/g) || []).length;
  console.log(`  窗口按钮 ${tabCount} 个 ${tabCount === 5 ? '✓' : '✗'}`);
  if (tabCount !== 5) failed = true;
  const kpiCount = (made.qualKpis.innerHTML.match(/class="qcell"/g) || []).length;
  console.log(`  指标卡 ${kpiCount} 个 ${kpiCount === 6 ? '✓' : '✗'}`);
  if (kpiCount !== 6) failed = true;

  // 过程指标：行数固定 5；说明列必须来自数据，不许是写死文案
  const proc = [...made.qualProc.innerHTML.matchAll(
    /<tr><td>(.*?)<\/td><td class="mono">(.*?)<\/td><td class="mini">(.*?)<\/td><\/tr>/g)];
  console.log(`  过程指标 ${proc.length} 行 ${proc.length === 5 ? '✓' : '✗'}`);
  if (proc.length !== 5) failed = true;
  for (const [, name, val, note] of proc) console.log(`    ${name} ${val} | ${note}`);
  const c = ((q.windows || [])[0] || {}).current || {};
  if (c.low_sample && proc.some(([, , , note]) => !note.includes('样本不足'))) {
    console.log('  ✗ 窗口样本不足，但说明列仍在给结论');
    failed = true;
  }

  console.log(`  滚动图: ${made.chartRolling.children.length ? '已绘制' : '空'}`);
  const warn = made.qualWarn.innerHTML;
  console.log(`  样本护栏: ${warn.includes('warn-strip') ? '已触发（该窗口样本不足或期望为负）' : '未触发'}`);

  console.log(`\n风险标记 ${(made.flagBox.innerHTML.match(/class="flag /g) || []).length} 条`);
  const markRows = (made.rowsMark.innerHTML.match(/<tr>/g) || []).length;
  console.log(`行为标记累计 ${markRows} 种 ${(liveJson.mark_summary || []).length === markRows ? '✓' : '✗'}`);
  if ((liveJson.mark_summary || []).length !== markRows) failed = true;
  if (made.rowsMark.innerHTML.includes('undefined') || made.rowsMark.innerHTML.includes('NaN')) {
    console.log('  ✗ 行为标记表出现 undefined / NaN');
    failed = true;
  }
  console.log(`AI 建议 ${(made.adviceBox.innerHTML.match(/class="n"/g) || []).length} 条`);
  console.log(`最近交易 ${(made.rowsTrade.innerHTML.match(/<tr>/g) || []).length} 行`);
  const noteInputs = (made.rowsTrade.innerHTML.match(/class="note-in"/g) || []).length;
  const noteCount = Object.keys(liveJson.trade_notes || {}).length;
  console.log(`  进场理由输入框 ${noteInputs} 个（已填 ${noteCount} 条）${noteInputs ? '✓' : '✗'}`);
  if (!noteInputs) failed = true;
  if (/value="undefined"|value="null"/.test(made.rowsTrade.innerHTML)) {
    console.log('  ✗ 进场理由输入框出现 undefined/null');
    failed = true;
  }
  console.log(`余额图: ${made.chartEquity.children.length ? '已绘制' : '空'}  日盈亏图: ${made.chartDaily.children.length ? '已绘制' : '空'}`);
  const sc = liveJson.scoring || {};
  console.log(`\n交易评分：等级 ${made.scoreGrade.textContent} (class=${made.scoreGrade.className})`);
  const dimRows = (made.scoreDims.innerHTML.match(/class="dim-row"/g) || []).length;
  console.log(`  维度行 ${dimRows} 个 ${dimRows === 6 ? '✓' : '✗'}`);
  if (dimRows !== 6) failed = true;
  console.log(`  平衡边际: ${made.beValue.textContent} | ${made.beSub.textContent}`);
  const beRows = (made.beTable.innerHTML.match(/<tr/g) || []).length;
  console.log(`  平衡线速查表 ${beRows} 行 ${beRows === 6 ? '✓' : '✗'}`);
  if (beRows !== 6) failed = true;
  if (!made.scoreGrade.textContent || made.scoreGrade.textContent === '—') failed = true;
  console.log(`API 计费表: ${made.apiMeter.textContent || '(空)'}`);
  const adviceHtml = made.adviceBox.innerHTML;
  const adviceMode = adviceHtml.includes('已过期') ? '旧复盘已过期（等重新生成）'
    : adviceHtml.includes('尚未生成复盘') ? '等待确认（不会自动调 API）'
    : adviceHtml.includes('advice-head') ? '显示缓存结果' : '空';
  console.log(`AI 面板模式: ${adviceMode}`);
  if (adviceMode === '空' && liveJson.ok) { console.log('  ✗ AI 面板空白'); failed = true; }

  // ---- AI 实时做单：用合成数据测渲染，不依赖有没有真跑过 ----
  // 重点是钉住「开仓理由」和「平仓理由」是两列：合成一条平仓记录，
  // 它的 entry_reason 来自当初的开仓行，reason 来自这次的离场判断。
  const atSample = {
    ok: true, running: true, dry_run: true, stale: false, stop_reason: null,
    symbol: 'BTCUSDm',
    symbols: [
      { symbol: 'XAUUSDm', label: '黄金 XAUUSD', point: 0.001, contract: 100.0, spread: 240, digits: 3 },
      { symbol: 'BTCUSDm', label: '比特币 BTCUSD', point: 0.01, contract: 1.0, spread: 640, digits: 2 },
    ],
    position: { direction: 1, lots: 0.01, price_open: 4000.5, p_up: 0.62, peak_pnl: 2.2 },
    float_pnl: 1.23,
    last_decision: { ts: 1790000000, kind: 'entry', p_up: 0.62, direction: 1, act: false },
    trades: 2, closed: 1, wins: 1, pnl: 0.88,
    // 今日口径：4 笔里 1 笔硬止损，真实 3 胜 1 负。命中率是 100%（分母剔了止损），
    // 这两个数放在一起就是为了断言「界面不许只显示那个 100%」。
    closed_today: 4, wins_today: 3, losses_today: 1, pnl_today: 7.86,
    llm_closes: 1, llm_hit_rate: 1.0,
    sl_closes: 2, sl_usd: 3.0, tp_min_usd: 3.25,
    max_hold_sec: 3600, guard_sec: 5.0, cfg: { interval_sec: 60 },
    pending_reviews: 1, next_review_ts: Math.floor(Date.now() / 1000) + 240,
    dir_recent: { n: 10, long: 9, short: 1, skew: true },
    cache: { calls: 3, hit_tokens: 896, miss_tokens: 1094, hit_rate: 0.4503 },
    recent: [
      { ts: 1790000000, kind: 'open', ticket: 1001, direction: 1, p_up: 0.62,
        reason: '近 5 根连阳', source: 'llm' },
      { ts: 1790000060, kind: 'close', ticket: 1001, direction: 1, pnl: 0.88,
        peak_pnl: 2.2, entry_reason: '近 5 根连阳', reason: '动能衰竭', source: 'llm' },
      { ts: 1790000120, kind: 'skip', reason: '平仓后冷却中，还需 20 秒' },
      { ts: 1790000180, kind: 'review', direction: 1, pnl: 0.88,
        lesson: '第一根大阳线后追多容易吃回撤' },
    ],
  };
  api.renderAutoTrade(atSample);
  // 默认过滤掉「跳过 / 继续持有」：样例 4 行里有 1 行 skip，所以只该剩 3 行。
  // 这条钉住的是「一小时的流水里 90% 是 ski p行、会把平仓行冲掉」这个教训。
  const atRows = (made.rowsAuto.innerHTML.match(/<tr/g) || []).length;
  console.log(`\nAI 做单表默认 ${atRows} 行 ${atRows === 3 ? '✓' : '✗'}（跳过被过滤）`);
  if (atRows !== 3) failed = true;
  const hintTxt = String(made.atRowsHint.textContent || '');
  console.log(`  计数提示 ${hintTxt.includes('共 4 条') && hintTxt.includes('显示 3 条') ? '✓' : '✗'} (${hintTxt})`);
  if (!(hintTxt.includes('共 4 条') && hintTxt.includes('显示 3 条'))) failed = true;
  // 勾上开关必须能看到全量
  made.atShowSkip.checked = true;
  api.renderAutoTrade(atSample);
  const atRowsAll = (made.rowsAuto.innerHTML.match(/<tr/g) || []).length;
  console.log(`  勾选后显示全量 ${atRowsAll} 行 ${atRowsAll === 4 ? '✓' : '✗'}`);
  if (atRowsAll !== 4) failed = true;
  made.atShowSkip.checked = false;
  api.renderAutoTrade(atSample);
  const atCols = (made.rowsAuto.innerHTML.match(/<td/g) || []).length / Math.max(atRows, 1);
  console.log(`  每行 ${atCols} 列 ${atCols === 9 ? '✓' : '✗'}（开仓理由 / 平仓理由 / 详情 分列）`);
  if (atCols !== 9) failed = true;
  const detailBtns = (made.rowsAuto.innerHTML.match(/tradeDetailShow/g) || []).length;
  console.log(`  open/close 行带「详情」按钮 ${detailBtns >= 2 ? '✓' : '✗'}（${detailBtns} 个）`);
  if (detailBtns < 2) failed = true;

  // 品种选择：下拉框必须由后端 symbols 填充，且运行中锁定并显示实际在跑的品种
  const symHtml = String(made.atSymbol.innerHTML || '');
  const optCount = (symHtml.match(/<option/g) || []).length;
  console.log(`  品种下拉 ${optCount} 个 ${optCount === 2 ? '✓' : '✗'}`);
  if (optCount !== 2) failed = true;
  console.log(`  下拉含 BTC ${symHtml.includes('BTCUSDm') ? '✓' : '✗'}`);
  if (!symHtml.includes('BTCUSDm')) failed = true;
  console.log(`  运行中锁定品种 ${made.atSymbol.disabled === true ? '✓' : '✗'}`);
  if (made.atSymbol.disabled !== true) failed = true;
  console.log(`  对齐到实际在跑的品种 ${made.atSymbol.value === 'BTCUSDm' ? '✓' : '✗'}（${made.atSymbol.value}）`);
  if (made.atSymbol.value !== 'BTCUSDm') failed = true;
  // 点差成本必须按品种算：BTC 640×0.01×0.01×1 = 0.064，不是黄金的 0.24
  const btcCost = api.atSpreadCostUsd(0.01);
  const costOk = btcCost != null && Math.abs(btcCost - 0.064) < 1e-9;
  console.log(`  BTC 点差成本按品种计算 ${costOk ? '✓' : '✗'}（${btcCost}）`);
  if (!costOk) failed = true;
  if (made.rowsAuto.innerHTML.includes('undefined') || made.rowsAuto.innerHTML.includes('NaN')) {
    console.log('  ✗ AI 做单表出现 undefined / NaN');
    failed = true;
  }
  console.log(`  平仓行带开仓理由 ${made.rowsAuto.innerHTML.includes('近 5 根连阳') ? '✓' : '✗'}`);
  const lessonShown = made.rowsAuto.innerHTML.includes('第一根大阳线后追多容易吃回撤');
  console.log(`  复盘行显示结论 ${lessonShown ? '✓' : '✗'}`);
  if (!lessonShown) failed = true;
  const cacheTxt = String(made.atCache.textContent || '');
  console.log(`  缓存命中率显示 ${cacheTxt.includes('45.0%') ? '✓' : '✗'} (${cacheTxt.trim() || '空'})`);
  if (!cacheTxt.includes('45.0%')) failed = true;

  // 平仓后要等 7 根 K 线才复盘：等待期间面板必须说清"还要走几根"，
  // 否则用户看到平仓了却没有复盘，会以为坏了。
  const pendTxt = String(made.atPending.textContent || '');
  console.log(`  待复盘提示 ${pendTxt.includes('待复盘') ? '✓' : '✗'} (${pendTxt.trim() || '空'})`);
  if (!pendTxt.includes('待复盘')) failed = true;

  // 只做多这种偏置必须能一眼看出来：多空分布 + 偏斜警告
  const dirTxt = String(made.atDir.textContent || '');
  console.log(`  多空偏斜提示 ${dirTxt.includes('偏斜') ? '✓' : '✗'} (${dirTxt.trim() || '空'})`);
  if (!dirTxt.includes('偏斜')) failed = true;

  // ---- 单笔交易溯源弹窗：程序建议 vs 模型判断必须两列都显示 ----
  // 这两份数据钉住两个分支：(1) 完整档案（终端在线、重建出结构）
  // (2) 降级档案（终端离线，原料不可得）——降级时绝不能显示假数据，
  //     必须出现"原始行情没存盘"的说明。
  const tradeFull = {
    ok: true, ticket: 12345,
    open: { ts: 1790000000, kind: 'open', ticket: 12345, direction: -1, lots: 0.01,
            price: 4027.413, p_up: 0.2, reason: '贴上沿，窄幅震荡，考虑做空', source: 'llm' },
    close: { ts: 1790000060, kind: 'close', ticket: 12345, pnl: 0.68, hit: true,
             held_sec: 60, reason: '连续 1 根阴线', source: 'llm' },
    review: { lesson: '等反抽再空', regime: 'range' },
    result: { pnl: 0.68, hit: true, held_sec: 60, exit_source: 'llm' },
    recon: { bars: 'T-13  10-23 11:33  4022.8  4023.8  4021.8  4022.8',
             structure: '结构 ER=0.08（净移动 -0.412（下行）/ 总行程 5.100）｜位置判定：贴上沿（程序倾向：可考虑做空）',
             prog_dir: -1, ticks: 'tick 合计 1800 笔', note: null,
             feat: { ok: true, symbol: 'XAUUSDm', tf: 'M1', n: 14,
                     er: 0.08, drift: -0.412, pos: 0.92, span: 5.1,
                     streak: -3, atr: 0.412, span_atr: 12.38,
                     regime: 'range', prog_dir: -1,
                     mtf: { tf: 'M5', er: 0.62, pos: 0.88, regime: '单边上行', span_atr: 5.2 } },
             mtf: { tf: 'M5', er: 0.62, pos: 0.88, regime: '单边上行', span_atr: 5.2 } },
    judgement: { prog_dir: -1, model_dir: -1, agree_field: true, p_up: 0.2,
                 model_reason: '贴上沿，窄幅震荡，考虑做空', verdict: '一致' },
  };
  const htmlFull = api.renderTradeDetail(tradeFull);
  const fullChecks = [
    ['方向/手数', htmlFull.includes('空 0.01 手')],
    ['开仓价', htmlFull.includes('4027.413')],   // 字段名写错会退化成「@ —」
    ['盈亏金额', htmlFull.includes('0.68')],
    ['平仓方式', htmlFull.includes('模型判断平仓')],
    ['程序建议', htmlFull.includes('做空')],
    ['模型 p_up', htmlFull.includes('20%')],
    ['裁决', htmlFull.includes('一致')],
    ['结构行', htmlFull.includes('ER=0.08')],
    ['K 线原料', htmlFull.includes('T-13')],
    ['复盘教训', htmlFull.includes('等反抽再空')],
    // 特征快照（2026-10-26 起落盘）：快照优先，重建只是补充
    ['快照标注', htmlFull.includes('决策时刻落盘')],
    ['ER 数值', htmlFull.includes('0.08')],
    ['ATR 倍数', htmlFull.includes('12.4')],
    ['M5 背景', htmlFull.includes('M5 背景') && htmlFull.includes('单边上行')],
  ];
  for (const [name, ok] of fullChecks) {
    console.log(`  溯源·${name} ${ok ? '✓' : '✗'}`);
    if (!ok) failed = true;
  }
  if (htmlFull.includes('undefined') || htmlFull.includes('NaN')) {
    console.log('  ✗ 溯源弹窗出现 undefined / NaN');
    failed = true;
  }

  const tradeDegraded = JSON.parse(JSON.stringify(tradeFull));
  tradeDegraded.recon = { bars: null, structure: null, prog_dir: null, ticks: null,
                          note: '终端未连接，历史行情无法重建' };
  tradeDegraded.judgement = { prog_dir: null, model_dir: -1, agree_field: null,
                              p_up: 0.2, model_reason: '贴上沿', verdict: null };
  const htmlDeg = api.renderTradeDetail(tradeDegraded);
  console.log(`  溯源·降级说明 ${htmlDeg.includes('原始行情没存盘') ? '✓' : '✗'}`);
  if (!htmlDeg.includes('原始行情没存盘')) failed = true;
  console.log(`  溯源·降级不说假话 ${!htmlDeg.includes('ER=') && !htmlDeg.includes('T-13') ? '✓' : '✗'}`);
  if (htmlDeg.includes('ER=') || htmlDeg.includes('T-13')) failed = true;
  console.log(`  溯源·旧数据无建议时标注 ${htmlDeg.includes('旧数据未记录') ? '✓' : '✗'}`);
  if (!htmlDeg.includes('旧数据未记录')) failed = true;

  // 程序止损笔数必须显示：止损多 = 入场方向差，和"模型平得差"不是一回事
  const slTxt = String(made.atSl.textContent || '');
  const slOk = slTxt.includes('程序止损 2 笔') && slTxt.includes('3.00') && slTxt.includes('3.25');
  console.log(`  程序止损提示 ${slOk ? '✓' : '✗'} (${slTxt.trim() || '空'})`);
  if (!slOk) failed = true;

  // 出场闸门胶囊：数字必须来自实际 cfg。页面写 3.25 而实际跑 0（关掉）
  // 是最坏的一种谎——用户会以为有保护，其实没有。
  const gSl = String(made.atGateSlV.textContent || '');
  const gTp = String(made.atGateTpV.textContent || '');
  const gGuard = String(made.atGateGuardV.textContent || '');
  const gHold = String(made.atGateHoldV.textContent || '');
  const gateOk = gSl.includes('3.00') && gTp.includes('3.25')
    && gGuard.includes('5') && gHold.includes('60');
  console.log(`  出场闸门胶囊 ${gateOk ? '✓' : '✗'} (止损 ${gSl} / 快查 ${gGuard} / 止盈 ${gTp} / 上限 ${gHold})`);
  if (!gateOk) failed = true;

  // 关掉的规则必须标 off 并显示「关闭」，不能留着一个不存在的数字
  const savedAt = { ...atSample };
  api.renderAutoTrade({ ...atSample, sl_usd: 0, guard_sec: 0, tp_min_usd: 0 });
  const offOk = String(made.atGateSlV.textContent).includes('关闭')
    && String(made.atGateGuardV.textContent).includes('关闭')
    && String(made.atGateTpV.textContent).includes('关闭')
    && String(made.atGateSl.className).includes('off')
    && String(made.atGateGuard.className).includes('off');
  console.log(`  闸门关闭时显示「关闭」 ${offOk ? '✓' : '✗'}`);
  if (!offOk) failed = true;

  // 快查间隔 ≥ 决策间隔等于没开（_wait_next 会夹到 interval_sec），也要标关闭
  api.renderAutoTrade({ ...atSample, guard_sec: 90, cfg: { interval_sec: 60 } });
  const clampOk = String(made.atGateGuardV.textContent).includes('关闭');
  console.log(`  快查间隔不小于决策间隔时标关闭 ${clampOk ? '✓' : '✗'}`);
  if (!clampOk) failed = true;
  api.renderAutoTrade(savedAt);

  // 峰值浮盈：持仓卡和流水行都要有。跟实际盈亏并排看才分得清
  // 「摸到了又吐回去」和「一路没回头」——两者要改的地方完全相反。
  const posSub = String(made.atPosSub.textContent || '');
  const peakOk = posSub.includes('峰值') && posSub.includes('2.20');
  console.log(`  持仓显示峰值浮盈 ${peakOk ? '✓' : '✗'} (${posSub})`);
  if (!peakOk) failed = true;
  const peakRowOk = String(made.rowsAuto.innerHTML).includes('峰 +2.20');
  console.log(`  流水行显示峰值 ${peakRowOk ? '✓' : '✗'}`);
  if (!peakRowOk) failed = true;

  // 常驻状态带：三件事——在不在跑 / 有没有仓 / 现在多少。
  // 这条是给「瞄一眼」服务的，所以任何一处写错都等于整个面板失去意义。
  const sbState = String(made.atBarState.textContent || '');
  const sbPos = String(made.atBarPos.innerHTML || '');
  const sbStat = String(made.atBarStat.innerHTML || '');
  const sbStateOk = sbState.includes('运行中') && sbState.includes('纸面')
    && String(made.atBar.className).includes('on');
  console.log(`  状态带·运行状态 ${sbStateOk ? '✓' : '✗'} (${sbState})`);
  if (!sbStateOk) failed = true;
  const sbPosOk = sbPos.includes('多') && sbPos.includes('4000.500')
    && sbPos.includes('+1.23') && sbPos.includes('+2.20');
  console.log(`  状态带·持仓与浮盈 ${sbPosOk ? '✓' : '✗'}`);
  if (!sbPosOk) failed = true;
  const sbStatOk = sbStat.includes('已平') && sbStat.includes('累计')
    && sbStat.includes('3 胜 1 负') && !sbStat.includes('命中');
  console.log(`  状态带·战绩 ${sbStatOk ? '✓' : '✗'}`);
  if (!sbStatOk) failed = true;
  console.log(`  运行中「关闭并清仓」可用 ${made.atBarStop.disabled === false ? '✓' : '✗'}`);
  if (made.atBarStop.disabled !== false) failed = true;

  // 空仓 + 未运行：状态带不能还显示持仓，按钮必须禁用
  api.renderAutoTrade({ ...atSample, running: false, position: null, float_pnl: null });
  const sbIdle = String(made.atBarPos.innerHTML || '');
  const sbIdleState = String(made.atBarState.textContent || '');
  const sbIdleOk = sbIdle.includes('空仓')
    && !sbIdleState.includes('运行中')
    && made.atBarStop.disabled === true;
  console.log(`  状态带·未运行时空仓且禁用按钮 ${sbIdleOk ? '✓' : '✗'}`);
  if (!sbIdleOk) failed = true;
  api.renderAutoTrade(savedAt);

  // 全屏提醒幕布：按钮点了要能盖上、要显示当前持仓，Esc/退出全屏要能收掉
  api.enterTradingFullscreen();
  const curtainUp = !made.atCurtain.classList.contains('hidden');
  console.log(`  全屏幕布可打开 ${curtainUp ? '✓' : '✗'}`);
  if (!curtainUp) failed = true;
  const curtTxt = String(made.atCurtainStat.innerHTML || '');
  console.log(`  幕布显示当前状态 ${curtTxt.includes('当前持仓') ? '✓' : '✗'}`);
  if (!curtTxt.includes('当前持仓')) failed = true;

  // 三种状态的标题必须各不相同。这块屏是「路人第一眼看到的东西」，
  // 停着还写「请勿操作」、或者读不到后端就断言「AI 已停止」，都是撒谎。
  const curtTitle = () => String(made.atCurtainTitle.textContent || '');
  api.updateCurtain(atSample);
  const cRun = curtTitle().includes('请勿操作')
    && String(made.atCurtainState.textContent).includes('运行中')
    && !made.atCurtain.classList.contains('is-stopped');
  console.log(`  幕布·运行中 ${cRun ? '✓' : '✗'} (${curtTitle()})`);
  if (!cRun) failed = true;

  // 幕布上的「今日累计」必须带胜负笔数，**不许写命中率**：
  // 那个百分比的分母剔掉了硬止损，4 笔里 1 笔止损会显示成 100%。
  const curtStatHtml = String(made.atCurtainStat.innerHTML || '');
  const cTally = curtStatHtml.includes('3 胜 1 负')
    && curtStatHtml.includes('今日累计')
    && !curtStatHtml.includes('命中');
  console.log(`  幕布·今日累计写胜负不写命中率 ${cTally ? '✓' : '✗'}`);
  if (!cTally) failed = true;
  const cTodayMoney = curtStatHtml.includes('+7.86');
  console.log(`  幕布·今日累计用今日口径 ${cTodayMoney ? '✓' : '✗'}`);
  if (!cTodayMoney) failed = true;

  api.updateCurtain({ ...atSample, running: false, position: null, float_pnl: null });
  const cStop = curtTitle().includes('已停止')
    && made.atCurtain.classList.contains('is-stopped');
  console.log(`  幕布·已停止转红 ${cStop ? '✓' : '✗'} (${curtTitle()})`);
  if (!cStop) failed = true;

  api.updateCurtain({ ok: false });
  const cUnknown = curtTitle().includes('状态未知')
    && !made.atCurtain.classList.contains('is-stopped');
  console.log(`  幕布·读不到后端不冒充已停止 ${cUnknown ? '✓' : '✗'} (${curtTitle()})`);
  if (!cUnknown) failed = true;

  // 空仓时不摆一张空的「浮动盈亏」卡
  api.updateCurtain({ ...atSample, position: null, float_pnl: null });
  const curtIdle = String(made.atCurtainStat.innerHTML || '');
  const cIdle = curtIdle.includes('空仓') && !curtIdle.includes('浮动盈亏');
  console.log(`  幕布·空仓不摆浮动卡 ${cIdle ? '✓' : '✗'}`);
  if (!cIdle) failed = true;
  api.updateCurtain(atSample);

  api.exitTradingFullscreen();
  if (!made.atCurtain.classList.contains('hidden')) {
    console.log('  ✗ 幕布收不起来（按 Esc 也没用）');
    failed = true;
  }

  // act=false 时"最近一次判断"要写「不做」，不能只显示一个 62% 让人误解
  const verb = String(made.atCallSub.textContent || '');
  console.log(`  act=false 显示为「不做」 ${verb.includes('不做') ? '✓' : '✗'} (${verb})`);
  if (!verb.includes('不做')) failed = true;
  console.log(`  状态徽标: ${made.atState.textContent} | 运行中时「开始」按钮 disabled=${made.atStart.disabled}`);
  if (!made.atStart.disabled) { console.log('  ✗ 运行中却没禁用「开始做单」'); failed = true; }

  // ---- 「点了没反应」回归测试 ----
  // atNote 每 15 秒被轮询覆写一次。失败提示曾经写在 atNote 里，
  // 于是写进去的下一秒就被清空，用户看到的就是「没反应」。
  // 现在结果写在 atMsg，轮询不许碰它。
  api.atSay('<b>启动失败：终端未开启算法交易</b>', 'err');
  api.renderAutoTrade(atSample);
  const kept = String(made.atMsg.innerHTML || '');
  console.log(`\n失败提示不被轮询清空 ${kept.includes('启动失败') ? '✓' : '✗'}`);
  if (!kept.includes('启动失败')) failed = true;
  console.log(`  结果条样式: ${made.atMsg.className}`);
  if (!String(made.atMsg.className).includes('err')) { console.log('  ✗ 失败没标成 err'); failed = true; }

  const hintHtml = api.atErrHtml('终端未开启算法交易（AutoTrading 按钮）');
  console.log(`  AutoTrading 错误带操作步骤 ${hintHtml.includes('Ctrl+E') ? '✓' : '✗'}`);
  if (!hintHtml.includes('Ctrl+E')) failed = true;
  const otherHtml = api.atErrHtml('手数必须大于 0');
  console.log(`  普通错误原样展示 ${otherHtml.includes('手数必须大于 0') ? '✓' : '✗'}`);
  if (!otherHtml.includes('手数必须大于 0')) failed = true;

  // 按钮真的接上处理函数了吗？「点了没反应」有一半是 handler 没挂上/没定义。
  const wired = src.includes('onclick="startAutoTrade()"') &&
                src.includes('onclick="stopAutoTrade()"') &&
                src.includes('onclick="resetAutoTrade()"') &&
                src.includes('onclick="resetLessons()"') &&
                src.includes('onclick="enterTradingFullscreen()"') &&
                typeof api.startAutoTrade === 'function' &&
                typeof api.stopAutoTrade === 'function' &&
                typeof api.resetAutoTrade === 'function' &&
                typeof api.resetLessons === 'function' &&
                typeof api.enterTradingFullscreen === 'function';
  console.log(`  开始/关闭/重置战绩/清空经验/全屏按钮已接 handler ${wired ? '✓' : '✗'}`);
  if (!wired) failed = true;
  console.log(`  弹窗可用 ${typeof api.closeModal === 'function' ? '✓' : '✗'}`);
  if (typeof api.closeModal !== 'function') failed = true;

  // ---- 教训库（跨场次）----
  // 钉两件：折叠块真的存在、点开能拉到数据并渲染出「N 条 · M 场」。
  // 这一块要扫归档流水数平仓笔，绝不能塞进 1 秒轮询里，所以只在用户点开时请求。
  const lessonsWired = src.includes('id="atLessons"') && typeof api.loadLessons === 'function';
  await api.loadLessons();
  const lessonsText = String((made.atLessonsText || {}).textContent || '');
  const lessonsHint = String((made.atLessonsHint || {}).textContent || '');
  const lessonsOk = lessonsText.includes('教训库 3 条') && lessonsHint.includes('3 条');
  console.log(`  教训库面板点开可渲染跨场次统计 ${lessonsWired && lessonsOk ? '✓' : '✗'}`);
  if (!(lessonsWired && lessonsOk)) failed = true;

  // ---- 模型切换条 ----
  // 这块最容易出「看着切了、其实没生效」：界面显示新模型，实际还在调老的。
  // 所以钉三件：显示当前模型、换供应商自动换默认模型名、缺 key 必须拦住不发请求。
  llmJson = {
    ok: true, provider: 'zhipu', provider_name: '智谱 GLM（免费档，国内直连）',
    base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash',
    key_set: true, key_tail: '716f', key_source: 'LLM_API_KEY',
    providers: [
      { id: 'zhipu', name: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4',
        default_model: 'glm-4-flash', models: ['glm-4-flash', 'glm-4-air'], note: '' },
      { id: 'deepseek', name: 'DeepSeek', base_url: 'https://api.deepseek.com',
        default_model: 'deepseek-chat', models: ['deepseek-chat', 'deepseek-reasoner'], note: '' },
    ],
  };
  await api.loadLlm();
  const nowTxt = String(made.atLlmNow.innerHTML || '');
  const showsModel = nowTxt.includes('glm-4-flash') && nowTxt.includes('716f');
  console.log(`\n模型切换条显示当前模型 ${showsModel ? '✓' : '✗'} (${nowTxt || '空'})`);
  if (!showsModel) failed = true;

  const filled = String(made.atLlmModel.value || '') === 'glm-4-flash';
  console.log(`  输入框回填当前模型 ${filled ? '✓' : '✗'} (${made.atLlmModel.value || '空'})`);
  if (!filled) failed = true;

  // 换供应商必须换默认模型名，否则会把 glm-4-flash 发给 DeepSeek（400 模型不存在）
  made.atLlmKey.value = '';
  made.atLlmProvider.value = 'deepseek';
  api.onLlmProviderChange();
  const modelSwitched = String(made.atLlmModel.value || '') === 'deepseek-chat';
  console.log(`  换供应商自动切默认模型名 ${modelSwitched ? '✓' : '✗'} (${made.atLlmModel.value || '空'})`);
  if (!modelSwitched) failed = true;

  const callsBefore = fetchCalls.length;
  made.atMsg.innerHTML = '';
  await api.switchLlm();
  const blocked = String(made.atMsg.innerHTML || '').includes('换供应商要填 key')
    && fetchCalls.length === callsBefore;
  console.log(`  换供应商不给 key 被拦下、且没发请求 ${blocked ? '✓' : '✗'}`);
  if (!blocked) failed = true;

  const llmWired = src.includes('onclick="switchLlm()"')
    && typeof api.switchLlm === 'function'
    && typeof api.loadLlm === 'function'
    && typeof api.onLlmProviderChange === 'function';
  console.log(`  模型切换按钮已接 handler ${llmWired ? '✓' : '✗'}`);
  if (!llmWired) failed = true;
} catch (e) {
  failed = true;
  console.log('渲染抛错:', e.message);
  console.log(e.stack.split('\n').slice(0, 8).join('\n'));
}
console.log(failed ? '\n结果：失败' : '\n结果：全部通过');
process.exit(failed ? 1 : 0);
})();
