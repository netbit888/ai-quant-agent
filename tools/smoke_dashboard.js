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
    addEventListener() {},
  };
  made[id] = el;
  return el;
}
global.document = {
  getElementById: id => made[id] || makeEl(id),
  querySelectorAll: () => [],
  createElementNS: () => makeEl('svg' + Math.random()),
  hidden: false,
  addEventListener() {},
};
global.fetch = async () => ({ json: async () => liveJson });
global.setInterval = () => 0;
global.URLSearchParams = class { constructor() {} set() {} toString() { return ''; } };

let failed = false;
try {
  const body = script.replace(/pollState\(\);[\s\S]*$/, '');
  const fn = new Function(body + '\n; return {renderLive, renderQuality, reasonCn, mins, money, fmt, ts2str};');
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
  console.log(`AI 建议 ${(made.adviceBox.innerHTML.match(/class="n"/g) || []).length} 条`);
  console.log(`最近交易 ${(made.rowsTrade.innerHTML.match(/<tr>/g) || []).length} 行`);
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
  console.log(`AI 面板模式: ${adviceHtml.includes('生成 AI 复盘') ? '等待确认（不会自动调 API）' : adviceHtml.includes('advice-head') ? '显示缓存结果' : '空'}`);
} catch (e) {
  failed = true;
  console.log('渲染抛错:', e.message);
  console.log(e.stack.split('\n').slice(0, 8).join('\n'));
}
console.log(failed ? '\n结果：失败' : '\n结果：全部通过');
process.exit(failed ? 1 : 0);
