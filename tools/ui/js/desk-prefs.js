/* 开始 / 关闭。
 * 两个动作都会真动钱（模型调用、下单），所以都走同一套确认弹窗，
 * 而且后端还会再校验一次 confirm —— 前端弹窗只是让人看清后果，
 * 不是安全边界。安全边界在 /api/autotrade/* 那两个 handler 里。 */
function atMode() {
  if ($('atModeAssist') && $('atModeAssist').checked) return 'assist';
  if ($('atModePaper') && $('atModePaper').checked) return 'paper';
  return 'live';
}
function atLiveMode() {
  return atMode() === 'live';
}
function atAssistMode() {
  return atMode() === 'assist';
}
/* 辅助模式下是否「演练」（点同意也只记账）。默认勾上吗？
   不。默认不勾 = 辅助就是真单，这是用户选第三档时的本意；
   想演练要自己勾，勾错了最多白弹几次窗，不会以为是练手结果下了真单。 */
function atAssistPaper() {
  return !!(atAssistMode() && $('atAssistPaper') && $('atAssistPaper').checked);
}
function atDryRun() {
  return atAssistMode() ? atAssistPaper() : atMode() === 'paper';
}

function atIntervalSec() {
  const v = parseInt(($('atInterval') || {}).value, 10);
  // 兜底要和 Cfg.interval_sec 一致（60）。写 5 的话，选择器一旦读不到值，
  // 界面显示 60 秒、实际按 5 秒连打模型——数字对不上是最难查的那类 bug。
  return Number.isFinite(v) && v >= 3 ? v : 60;
}

/* 开仓手数。兜底必须和 Cfg.lots / <select> 的 selected 一致（0.01）：
   控件读不到却按 0.05 发单，界面和账本就会对不上，而且不报错。
   合法档位由后端 live/prefs.py 的 LOTS 定，这里只把选中的值原样送过去。 */
function atLots() {
  const v = parseFloat(($('atLots') || {}).value);
  return Number.isFinite(v) && v > 0 ? v : 0.01;
}

/* 把代价直接写在选择器旁边。
 * 周期一缩短，模型调用次数线性上涨；真实模式下点差成本也跟着上涨。
 * 这些数字不摆出来，用户只会把间隔调到 3 秒然后奇怪为什么账户在流血。 */
function updateModeHint() {
  const el = $('atModeHint');
  if (!el) return;
  // 「纸面演练」复选框只在辅助模式下露出
  const wrap = $('atAssistPaperWrap');
  if (wrap) wrap.style.display = atAssistMode() ? 'flex' : 'none';
  const iv = atIntervalSec();
  const lots = atLots();
  const calls = Math.round(3600 / iv);
  // 点差成本估算：平仓后冷却 60 秒 ⇒ 最好情况下每小时 ~60 笔往返。
  // 这是**上限**不是预期——模型可以 act=false 跳过，实际笔数通常远低于此；
  // 经验库在的时候尤其如此。列上限是为了把"最坏要付多少点差"摆上台面。
  const trips = Math.min(calls, 60);
  const perTrip = atSpreadCostUsd(lots);
  if (atAssistMode()) {
    // 辅助模式必须写清两件事：等的是你，以及**硬止损不等你**。
    // 不写后半句，用户会以为"人确认"包含"人决定什么时候止损"。
    const paper = atAssistPaper();
    el.textContent = `辅助${paper ? '·演练' : ''} · ${atSymbolLabel()} · 每 ${iv} 秒判断 · `
      + `${lots} 手 · `
      + `开/平仓前弹窗等你点（入场 90 秒 / 出场 120 秒，超时按默认处理）· `
      + `硬止损与持仓上限由程序直接执行${paper ? ' · 不发真单' : ''}`;
    el.className = paper ? 'mini' : 'mini red';
  } else if (atLiveMode()) {
    // × AT_FACTOR：perTrip 是美元，界面统一按账户货币显示。
    const total = perTrip == null ? '未知'
      : fmt(trips * perTrip * AT_FACTOR, 2) + ' ' + AT_MONEY + '/小时（上限）';
    el.textContent = `真实 · ${atSymbolLabel()} · 每 ${iv} 秒判断 · ${lots} 手 · 约 ${calls} 次调用/小时，`
      + `往返上限 ${trips} 笔 ≈ 点差上限 ${total}`;
    el.className = 'mini red';
  } else {
    el.textContent = `纸面 · ${atSymbolLabel()} · 每 ${iv} 秒判断 · ${lots} 手 · 约 ${calls} 次模型调用/小时`;
    el.className = 'mini';
  }
}

/* 品种规格表：从 /api/autotrade 的 symbols 字段来，前端不另存白名单——
   写死一份、后端校验另一份，加品种时必然漂移。 */
let atSymbolSpecs = {};

function atSymbol() {
  const el = $('atSymbol');
  return (el && el.value) || 'XAUUSDm';
}

function atSymbolLabel() {
  const s = atSymbolSpecs[atSymbol()];
  return s ? s.label : atSymbol();
}

/* 一笔往返的点差成本（USD），公式与后端 spread_cost_usd 逐字一致：
   spread_points × point × volume × contract。拿不到规格就返回 null，
   绝不返回 0——把「没测过」显示成「零成本」会直接误导。 */
function atSpreadCostUsd(lots) {
  const spec = atSymbolSpecs[atSymbol()];
  if (!spec || spec.spread == null || spec.point == null || spec.contract == null) return null;
  // 手数变了，点差成本线性变。旧版这里写死 0.01，选 0.05 手时确认弹窗
  // 会把最坏点差低估 5 倍——那正是这个数字唯一的作用。
  const raw = Number(lots);
  const v = (lots == null || !Number.isFinite(raw) || raw <= 0) ? atLots() : raw;
  return spec.spread * spec.point * v * spec.contract;
}

function fillSymbolOptions(a) {
  const el = $('atSymbol');
  if (!el || !Array.isArray(a.symbols) || !a.symbols.length) return;
  const cur = a.symbol || 'XAUUSDm';
  a.symbols.forEach(s => { atSymbolSpecs[s.symbol] = s; });
  // 运行中要把下拉框对齐到实际在跑的品种，否则界面说黄金、实际在跑 BTC。
  // 没在跑时的优先级：用户当前的选中 > 上次记住的（prefs）> state 里上一场跑过的。
  // 首次渲染时 el.value 还是空串，所以会落到 prefs——这正是「记住上次选择」的落点。
  const pref = (a.prefs && a.prefs.symbol) || cur;
  const want = a.running ? cur : (el.value || pref);
  el.innerHTML = a.symbols.map(s =>
    `<option value="${esc(s.symbol)}">${esc(s.label)}</option>`).join('');
  if (a.symbols.some(s => s.symbol === want)) el.value = want;
}

/* 交易参数的「上次选择」回填（品种在 fillSymbolOptions 里单独处理）。

   只回填一次。之后每 1 秒的轮询不再碰控件——用户刚点完「AI 辅助」、保存请求
   还在路上时，轮询拿到的还是旧值，每次都覆盖就会变成「点了自己跳回去」。

   期间若已经在跑（running），renderAutoTrade 走的是另一条分支：按实际在跑的
   cfg 对齐，那才是真相，不许被 prefs 盖掉。 */
let atPrefsApplied = false;

function applyPrefs(a) {
  const p = a && a.prefs;
  if (!p || atPrefsApplied) return;
  atPrefsApplied = true;

  // 间隔：只认下拉框里真有的档位。赋一个不存在的 value 会让 select 变空白，
  // 界面看起来像「参数丢了」，比没记住更糟。
  const ivEl = $('atInterval');
  const iv = String(p.interval_sec == null ? '' : p.interval_sec);
  const hasIv = !!(ivEl && ivEl.options && Array.from(ivEl.options)
    .some(o => o.value === iv));
  if (hasIv) ivEl.value = iv;

  // 手数同样只认下拉框里真有的档位：记一个界面上没有的值回来，
  // select 会变空白，用户以为参数丢了，实际是记了个没人认识的数。
  const lotsEl = $('atLots');
  const lots = String(p.lots == null ? '' : p.lots);
  const hasLots = !!(lotsEl && lotsEl.options && Array.from(lotsEl.options)
    .some(o => o.value === lots));
  if (hasLots) lotsEl.value = lots;

  const mode = p.mode || 'live';
  const setChecked = (id, on) => { const e = $(id); if (e) e.checked = !!on; };
  // 先全部落定再 updateModeHint：三条 radio 是同一组，逐条改会让 hint 拿到
  // 中间态（比如先勾了纸面、又勾辅助，hint 会闪一下纸面）。
  if (mode === 'assist') {
    setChecked('atModeAssist', true);
    setChecked('atAssistPaper', !!p.assist_paper);
  } else if (mode === 'paper') {
    setChecked('atModePaper', true);
    setChecked('atAssistPaper', false);
  } else {
    setChecked('atModeLive', true);
    setChecked('atAssistPaper', false);
  }
  updateModeHint();
}

/* 参数一改就记下来（防抖 300ms：拖下拉/连点 radio 时不会连发几次）。
   失败静默——这是「下次帮我填好」，不是业务动作，弹个红条只会让人以为
   做单出了问题。真正的兜底在后端：启动成功时按实际 cfg 再写一次。 */
let atPrefsTimer = null;

function saveAtPrefs() {
  if (atPrefsTimer) clearTimeout(atPrefsTimer);
  atPrefsTimer = setTimeout(() => {
    atPrefsTimer = null;
    try {
      fetch('/api/autotrade/prefs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: atSymbol(), interval_sec: atIntervalSec(), lots: atLots(),
          mode: atMode(), assist_paper: atAssistPaper(),
        }),
      }).catch(() => {});
    } catch (e) { /* 老 WebView / 假 DOM 里没有 fetch：不影响任何操作 */ }
  }, 300);
}

/* 全局 toast：alert() 的替代。区别：不打断操作流、样式随主题、
   出现在任何 tab 都看得见的右下角，几秒后自己消失。 */
let toastTimer = null;
function qaToast(msg, kind) {
  let el = $('qaToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'qaToast';
    document.body.appendChild(el);
  }
  el.className = 'qa-toast ' + (kind === 'ok' ? 'ok' : 'err');
  el.textContent = msg;   // textContent：消息常含后端返回的原文，不能进 innerHTML
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'qa-toast hidden'; }, 6000);
}

/* 操作结果条。和 atNote 分开：atNote 每 15 秒被轮询覆写一次，
   把「启动失败」写在那里等于没写——用户只会看到「点了没反应」。 */
function atSay(msg, kind) {
  const el = $('atMsg');
  if (!el) return;
  if (!msg) { el.className = 'at-msg hidden'; el.innerHTML = ''; return; }
  el.className = 'at-msg ' + (kind === 'ok' ? 'ok' : 'err');
  el.innerHTML = msg;
}

// 后端把 retcode 翻成了人话，这里再补一句「怎么解决」
function atErrHtml(err) {
  const raw = String(err || '未知错误');
  if (/算法交易|AutoTrading|10027/i.test(raw)) {
    return `<b>启动失败：终端未开启「算法交易」</b><br>
      MT5 工具栏的「算法交易 / Algo Trading」按钮（快捷键 <b>Ctrl+E</b>）要先点亮，
      再点一次「开始做单」。<br>如果按钮是灰的：工具 → 选项 → 专家顾问 → 勾选「允许算法交易」。`;
  }
  return '<b>启动失败：</b>' + esc(raw);
}

async function startAutoTrade() {
  const mode = atMode();
  const live = mode === 'live';
  const assist = mode === 'assist';
  const paper = atDryRun();
  const iv = atIntervalSec();
  const lots = atLots();
  const calls = Math.round(3600 / iv);
  const sym = atSymbol();
  const perTrip = atSpreadCostUsd(lots);
  let ok = false;
  try {
    ok = await confirmApiCall({
      title: assist
        ? `AI 辅助${paper ? '（演练）' : ''}：${atSymbolLabel()} ${lots} 手，每 ${iv} 秒判断一次，开始？`
        : (live ? `真实下单：${atSymbolLabel()} ${lots} 手，每 ${iv} 秒判断一次，开始？`
                : `纸面：${atSymbolLabel()} ${lots} 手，每 ${iv} 秒判断一次，开始？`),
      desc: assist
        ? `模型出信号后弹窗等你点：入场等 90 秒（不点＝不做），`
          + `出场等 120 秒（不点＝按模型决定平仓）。`
          + `硬止损和持仓上限仍由程序直接执行，不会等你；`
          + `同意时若价格已朝不利方向跑过 0.80 美元，信号作废。`
          + (paper ? `当前是演练：同意也只记账，不发真单。`
                   : `当前会向券商发真实市价单（${lots} 手）。`)
          + `每小时约 ${calls} 次模型调用。`
        : (live
            ? `会在 ${atSymbolLabel()}（${sym}）上向券商发真实市价单，每笔 ${lots} 手，不挂止损止盈。`
              + `每小时约 ${calls} 次模型调用；平仓后冷却 60 秒、同向重进 180 秒，往返上限 60 笔/小时`
              + `（模型可跳过，实际通常更少），`
              + `最坏点差约 ${perTrip == null ? '未知'
                  : fmt(60 * perTrip * AT_FACTOR, 2) + ' ' + AT_MONEY}。`
            : `只调模型、只在本地记账，不发单给券商。品种 ${atSymbolLabel()}，`
              + `每小时约 ${calls} 次模型调用。`),
      cost: assist ? (paper ? '辅助演练 + 模型调用' : '弹窗确认 + 真实下单')
                   : (live ? '真实下单 + 模型调用' : '仅模型调用'),
      okText: assist ? (paper ? '开始（辅助演练）' : '开始（辅助·真单）')
                     : (live ? '真实开始' : '开始（纸面）'),
    });
  } catch (e) {
    // 弹窗本身炸了也要让用户看见，否则又是「点了没反应」
    atSay('<b>确认弹窗没能打开：</b>' + esc(String(e)), 'err');
    return;
  }
  if (!ok) return;

  atSay('');
  $('atStart').disabled = true;
  try {
    const r = await fetch('/api/autotrade/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        confirm: true, dry_run: paper, symbol: sym, lots: lots,
        interval_sec: iv,
        assist: assist,
        account: curLogin || undefined,
      }),
    });
    const j = await r.json();
    if (!j.ok) {
      atSay(atErrHtml(j.error), 'err');
    } else {
      const what = j.assist ? (j.dry_run ? '（辅助演练）' : '（辅助·真单）')
                            : (j.dry_run ? '（纸面）' : '（真实做单）');
      atSay(`<b>已启动${what}</b>：${esc(j.symbol)} ${j.lots} 手，
             每 ${j.interval_sec} 秒判断一次。`
             + (j.assist ? '有信号会弹窗，<b>硬止损不等你</b>。'
                         : '第一笔决策马上会出现在下面的表里。'), 'ok');
    }
  } catch (e) {
    atSay('<b>启动请求失败：</b>' + esc(String(e)), 'err');
  } finally {
    $('atStart').disabled = false;
    loadAutoTrade();
  }
}

// 重置：清空战绩。经验（教训库）**默认保留**——它存在 lessons.jsonl 里，
// 不随流水归档。清经验是旁边那个按钮的活。
// 为什么需要重置：战绩会拼进提示词，之前的亏损样本会让它越来越保守
// （实测空单 1 胜 8 负 → 两条铁律 → 干脆不做空）。换参数前要个干净起点。
// 旧流水不是删除，是改名归档，随时能翻。
async function resetAutoTrade() {
  let ok = false;
  try {
    ok = await confirmApiCall({
      title: '清空战绩（经验保留）？',
      desc: '会停掉 AI，把「战绩统计 / 待复盘队列」清零，新流水从零开始。'
        + '旧流水改名成 autotrade_log.<时间戳>.jsonl 留在账户目录里，不删除。'
        + 'AI 攒的复盘教训**不受影响**（要清经验用旁边的「清空经验」）。'
        + '已经平掉的仓位记录不受影响。',
      cost: '会停掉 AI（不平仓）',
      okText: '重置战绩',
    });
  } catch (e) {
    atSay('<b>确认弹窗没能打开：</b>' + esc(String(e)), 'err');
    return;
  }
  if (!ok) return;

  atSay('');
  $('atReset').disabled = true;
  try {
    const r = await fetch('/api/autotrade/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, account: curLogin || undefined }),
    });
    const j = await r.json();
    if (j.ok) {
      const c = j.cleared || {};
      const a = j.archived || {};
      atSay(`<b>战绩已重置</b>：清掉 ${c.rows || 0} 条流水（含 ${c.closes || 0} 笔平仓记录）。`
        + `旧文件已归档：${esc(a.log || '—')} / ${esc(a.state || '—')}。`
        + `经验保留 ${j.lessons || 0} 条，下一笔决策就是干净的起点。`, 'ok');
    } else {
      atSay('<b>重置失败：</b>' + esc(j.error || '未知错误'), 'err');
    }
  } catch (e) {
    atSay('<b>重置请求失败：</b>' + esc(String(e)), 'err');
  } finally {
    $('atReset').disabled = false;
    loadAutoTrade();
  }
}

// 只清经验：教训库里一条不留，战绩和持仓都不动。
// 单独一个按钮的理由：清战绩只损失统计样本，清经验丢掉的是模型唯一的历史，
// 代价差一个量级，不能绑在一起误触。
async function resetLessons() {
  let ok = false;
  try {
    ok = await confirmApiCall({
      title: '清空 AI 攒的经验？',
      desc: '会把教训库里所有复盘教训归档清空（改名成 lessons.<时间戳>.jsonl，不删除）。'
        + '战绩、持仓、流水一律不动，AI 可以继续跑。'
        + '清空后注入的会变成「暂无复盘笔记」，直到攒出新教训。',
      cost: '不花钱、不下单',
      okText: '清空经验',
    });
  } catch (e) {
    atSay('<b>确认弹窗没能打开：</b>' + esc(String(e)), 'err');
    return;
  }
  if (!ok) return;

  atSay('');
  $('atResetLessons').disabled = true;
  try {
    const r = await fetch('/api/autotrade/reset_lessons', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    const j = await r.json();
    if (j.ok) {
      atSay(`<b>经验已清空</b>：归档 ${j.cleared || 0} 条教训`
        + (j.archived ? `（${esc(j.archived)}）` : '') + '。战绩不受影响。', 'ok');
    } else {
      atSay('<b>清空经验失败：</b>' + esc(j.error || '未知错误'), 'err');
    }
  } catch (e) {
    atSay('<b>清空经验请求失败：</b>' + esc(String(e)), 'err');
  } finally {
    $('atResetLessons').disabled = false;
    loadAutoTrade();
  }
}

