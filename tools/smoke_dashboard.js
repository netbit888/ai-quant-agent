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
  return { json: async () => liveJson };
};
global.setInterval = () => 0;
global.URLSearchParams = class { constructor() {} set() {} toString() { return ''; } };

let failed = false;
(async () => {
try {
  const body = script.replace(/pollState\(\);[\s\S]*$/, '');
  const fn = new Function(body + '\n; return {renderLive, renderQuality, renderAutoTrade, atSay, atErrHtml, startAutoTrade, stopAutoTrade, resetAutoTrade, enterTradingFullscreen, exitTradingFullscreen, loadLlm, onLlmProviderChange, switchLlm, closeModal, reasonCn, mins, money, fmt, ts2str};');
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
    position: { direction: 1, lots: 0.01, price_open: 4000.5, p_up: 0.62 },
    last_decision: { ts: 1790000000, kind: 'entry', p_up: 0.62, direction: 1, act: false },
    trades: 2, closed: 1, wins: 1, pnl: 0.88,
    llm_closes: 1, llm_hit_rate: 1.0,
    sl_closes: 2, sl_usd: 3.0, tp_min_usd: 3.25,
    pending_reviews: 1, next_review_ts: Math.floor(Date.now() / 1000) + 240,
    dir_recent: { n: 10, long: 9, short: 1, skew: true },
    cache: { calls: 3, hit_tokens: 896, miss_tokens: 1094, hit_rate: 0.4503 },
    recent: [
      { ts: 1790000000, kind: 'open', direction: 1, p_up: 0.62,
        reason: '近 5 根连阳', source: 'llm' },
      { ts: 1790000060, kind: 'close', direction: 1, pnl: 0.88,
        entry_reason: '近 5 根连阳', reason: '动能衰竭', source: 'llm' },
      { ts: 1790000120, kind: 'skip', reason: '平仓后冷却中，还需 20 秒' },
      { ts: 1790000180, kind: 'review', direction: 1, pnl: 0.88,
        lesson: '第一根大阳线后追多容易吃回撤' },
    ],
  };
  api.renderAutoTrade(atSample);
  const atRows = (made.rowsAuto.innerHTML.match(/<tr/g) || []).length;
  console.log(`\nAI 做单表 ${atRows} 行 ${atRows === 4 ? '✓' : '✗'}`);
  if (atRows !== 4) failed = true;
  const atCols = (made.rowsAuto.innerHTML.match(/<td/g) || []).length / Math.max(atRows, 1);
  console.log(`  每行 ${atCols} 列 ${atCols === 8 ? '✓' : '✗'}（开仓理由 / 平仓理由 分列）`);
  if (atCols !== 8) failed = true;
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

  // 程序止损笔数必须显示：止损多 = 入场方向差，和"模型平得差"不是一回事
  const slTxt = String(made.atSl.textContent || '');
  const slOk = slTxt.includes('程序止损 2 笔') && slTxt.includes('3.00') && slTxt.includes('3.25');
  console.log(`  程序止损提示 ${slOk ? '✓' : '✗'} (${slTxt.trim() || '空'})`);
  if (!slOk) failed = true;

  // 全屏提醒幕布：按钮点了要能盖上、要显示当前持仓，Esc/退出全屏要能收掉
  api.enterTradingFullscreen();
  const curtainUp = !made.atCurtain.classList.contains('hidden');
  console.log(`  全屏幕布可打开 ${curtainUp ? '✓' : '✗'}`);
  if (!curtainUp) failed = true;
  const curtTxt = String(made.atCurtainStat.innerHTML || '');
  console.log(`  幕布显示当前状态 ${curtTxt.includes('当前持仓') ? '✓' : '✗'}`);
  if (!curtTxt.includes('当前持仓')) failed = true;
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
                src.includes('onclick="enterTradingFullscreen()"') &&
                typeof api.startAutoTrade === 'function' &&
                typeof api.stopAutoTrade === 'function' &&
                typeof api.resetAutoTrade === 'function' &&
                typeof api.enterTradingFullscreen === 'function';
  console.log(`  开始/关闭/重置/全屏按钮已接 handler ${wired ? '✓' : '✗'}`);
  if (!wired) failed = true;
  console.log(`  弹窗可用 ${typeof api.closeModal === 'function' ? '✓' : '✗'}`);
  if (typeof api.closeModal !== 'function') failed = true;

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
