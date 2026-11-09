/* ==================== 实盘复盘 ==================== */
let liveData = null;
let liveTimer = null;
let liveLoading = false;
let acctList = [];        // /api/accounts 的原始结果，删除按钮靠它判断是不是当前账户
let curLogin = null;      // 当前查看的账户，逐笔理由要用它决定写进哪个账本

const money = (v, d = 2) => (v === null || v === undefined) ? '—'
  : (v > 0 ? '+' : '') + Number(v).toFixed(d);
// 备注是用户写的、AI 结论是模型写的，两者都可能带 < > &，必须转义后再拼 HTML
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ts2str = ts => {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
/* 只要时分秒。ts2str 只到分钟（11 个字符），原先调用处用它的 [11:] 取秒，
 * 等于永远取到空字符串——AI 流水表的「时间」列因此长期整列为空，
 * 「最近一次判断」后面也只剩一个孤零零的「 · 」。 */
const hhmmss = ts => {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
const mins = m => {
  if (m === null || m === undefined) return '—';
  if (m < 60) return `${m.toFixed(m < 10 ? 1 : 0)} 分`;
  if (m < 1440) return `${(m / 60).toFixed(1)} 时`;
  return `${(m / 1440).toFixed(1)} 天`;
};

// 账户列表：current 是 MT5 终端当前登录的（能实时同步），其余是归档的（只能离线看）
async function loadAccounts() {
  try {
    const r = await fetch('/api/accounts');
    const d = await r.json();
    if (!d.ok) return;
    acctList = d.accounts || [];
    const sel = $('acctPick');
    const prev = sel.value;
    sel.innerHTML = (d.accounts || []).map(a => {
      const tag = a.current ? ' ● 当前' : ' · 归档';
      const bal = a.last_balance != null ? fmt(a.last_balance) : '—';
      return `<option value="${a.login}">#${a.login} ${a.server || '?'} ${bal}${tag}</option>`;
    }).join('') || '<option value="">无归档账户</option>';
    // 默认看当前账户；用户手动选过就尊重他的选择
    const want = (prev && (d.accounts || []).some(a => String(a.login) === prev))
      ? prev : (d.current != null ? String(d.current) : '');
    if (want) sel.value = want;
    return want;
  } catch (e) {
    $('acctPick').innerHTML = '<option value="">读取失败</option>';
    return '';
  }
}

// 只读接口：不会触发任何付费调用，AI 建议只在有缓存时返回
async function loadLive(force = false) {
  if (liveLoading) return;
  liveLoading = true;
  try {
    const qs = new URLSearchParams();
    if (force) qs.set('force', '1');
    const acct = $('acctPick').value;
    if (acct) qs.set('account', acct);
    const r = await fetch('/api/live' + (qs.toString() ? '?' + qs : ''));
    liveData = await r.json();
    renderLive(liveData);
  } catch (e) {
    $('acctLogin').textContent = '连接失败';
    $('acctServer').textContent = String(e);
  } finally {
    liveLoading = false;
  }
}

// 只刷账户下拉：在 MT5 终端里切了账户，最多 20 秒后这里能看到。
// 和 loadLive 拆开，是因为账户列表很少变，没必要每 3 秒重建一次 DOM。
async function refreshAccounts() {
  const want = await loadAccounts();
  if (want) $('acctPick').value = want;
}

async function refreshLive(force = false) {
  await refreshAccounts();
  await loadLive(force);
}

// 账户条右上角那个「实时 · 时:分:秒」（2026-11-09）
// ------------------------------------------------------------------
// 原来这格的时间是在 renderLive 里取的墙钟，而 loadLive 每 3 秒才跑一次，
// 所以秒数一次跳 3——盯着看就是"不实时"。更糟的是它只有一个含义：
// 「上一次刷新在几点」。要当成"面板还活着"的证据，就必须走两次心跳：
//   · 秒数：本地计时器每秒重画，是**本机时钟**，证明页面没卡死；
//   · 新鲜度：记最近一次成功渲染的时刻，超过 8 秒没有新数据就改口
//     「滞后 Ns」+ 琥珀色——这才是原来那行时间真正想防的事
//     （进程被 MT5 卡住、接口挂了，数字停住但页面还在动）。
// 只动秒数不动数据是**骗人的**：所以宁可多写一句滞后判定。
let acctLiveAt = 0;          // 最近一次成功渲染 /api/live 的时刻；0 = 不在实时态

function paintAcctClock() {
  // 归档 / 未连接 / 出错这三种状态由 renderLive 自己写，别抢它的文案
  if (!acctLiveAt) return;
  const el = $('acctSync');
  if (!el) return;
  const lag = Math.round((Date.now() - acctLiveAt) / 1000);
  if (lag > 8) {
    el.textContent = '滞后 ' + lag + 's';
    el.className = 'badge-off';
    el.title = '面板已经 ' + lag + ' 秒没拿到新的账户数据（正常每 3 秒一次）。'
      + '数字停在上一次成功刷新的那一刻，不是当前值。';
    return;
  }
  const hh = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  el.textContent = '实时 · ' + hh;
  el.className = 'badge-live';
  el.title = '秒数每秒走一次（本机时钟）；余额/净值/持仓每 3 秒从 MT5 拉一次，'
    + '服务端缓存也是 3 秒——数字不会有比这更细的变化。';
}

function renderLive(d) {
  curLogin = (d && d.login) || null;
  if (!d || !d.ok) {
    acctLiveAt = 0;                  // 退出实时态：秒针别再往上写「实时」
    const raw = String((d && d.error) || '未知错误').replace(/^RuntimeError:\s*/, '');
    const noTerminal = /终端未运行|未连接/.test(raw);
    $('acctLogin').textContent = noTerminal ? 'MT5 未连接' : '读取失败';
    $('acctServer').textContent = raw;
    const syncEl = $('acctSync');
    syncEl.style.display = '';
    syncEl.textContent = noTerminal ? '未连接' : '出错';
    syncEl.className = 'badge-off';
    $('acctNote').textContent = noTerminal
      ? '面板会自动尝试启动 MetaTrader 5（最多等 20 秒），成功后这里自动恢复。若一直起不来，请手动双击 MT5。'
      : '';
    renderAdvice({ ok: false, error: (d && d.error) || '账户读取失败，无法生成复盘' });
    // 正在写备注时不要清空，否则连「写了一半的字」也一起没了
    if (!isEditingNote()) {
      $('sessTabs').innerHTML = '';
      $('sessBody').innerHTML = '<div class="mini">账户未连接，读不到场次数据。</div>';
    }
    return;
  }
  const a = d.account || {};
  const s = d.stats || {};

  // 归档账户是离线读磁盘：不连 MT5，也不给生成新复盘（生成了没法验证）
  const syncEl = $('acctSync');
  syncEl.style.display = '';
  if (d.offline) {
    acctLiveAt = 0;                  // 归档页不刷新，秒针也不该走
    syncEl.textContent = '归档 · 离线';
    syncEl.className = 'badge-off';
    $('acctNote').textContent = '这是已归档账户的历史数据（本地账本），不连 MT5、不刷新、不能生成新的 AI 复盘。';
  } else {
    // 记下"这次真的拿到了数据"，具体文案交给 paintAcctClock：
    // 秒数由 1 秒一次的心跳重画，这里只负责证明心跳有东西可证明。
    acctLiveAt = Date.now();
    paintAcctClock();
    $('acctNote').textContent = '切换账户请在 MT5 终端里操作，这里会自动识别并归档到独立账本。';
  }

  $('acctLogin').textContent = '#' + (a.login || '—');
  $('acctServer').textContent = `${a.server || '—'} · ${a.name || ''}`;
  const modeEl = $('acctMode');
  const isReal = a.trade_mode === 'real';
  // 美分账户（Exness 的 USC 等）：balance / profit / 阈值全部以「分」计。
  // **只标注单位，不折算成美元**（2026-11-02 用户定的）：写 "1453.00 USC"
  // 就够让人知道这不是 1453 美元，再挂一个（≈ 14.53 USD）等于同一格里塞两种
  // 口径——屏幕上两个不一样的数字，看哪个都对，看哪个也都不对。
  const curCode = String(a.currency || '').toUpperCase();
  const curSuffix = curCode ? ' ' + curCode : '';
  const isCentAcct = !!curCode && curCode !== 'USD' && Number(a.usd_rate || 1) < 1;
  modeEl.textContent = (isReal ? '真实账户' : (a.trade_mode === 'demo' ? '模拟账户' : (a.trade_mode || '—')))
    + (isCentAcct ? ' · 美分账户' : '');
  modeEl.className = 'badge-mode ' + (isReal ? 'badge-real' : 'badge-demo');
  $('acctBalance').textContent = fmt(a.balance) + curSuffix;
  $('acctEquity').textContent = fmt(a.equity) + (isCentAcct ? curSuffix : '');
  $('acctFree').textContent = fmt(a.margin_free) + (isCentAcct ? curSuffix : '');
  // 浮动盈亏不在账户条上显示（2026-11-02 用户定的）：它是余额/净值能直接
  // 看出来的差值，而且每次 tick 都在跳——常驻账户条上属于噪音。
  const sy = d.sync || {};
  $('acctDep').textContent = fmt(s.deposits);
  $('acctDep').title = sy.gross_in != null ? `入金 ${sy.gross_in} − 出金 ${sy.gross_out}` : '';
  $('acctLev').textContent = '1:' + (a.leverage || '—');

  // ---- KPI ----
  const today = s.today || {};
  $('lvToday').textContent = money(today.profit);
  $('lvToday').className = 'v mono ' + signClass(today.profit);
  $('lvTodaySub').textContent = today.trades ? `${today.trades} 笔 · 胜率 ${today.win_rate}%` : '今日无平仓';

  $('lvNet').textContent = money(s.net);
  $('lvNet').className = 'v mono ' + signClass(s.net);
  $('lvNetSub').textContent = s.deposits
    ? `${s.count} 笔 · 相对入金 ${fmt(s.return_pct, 1)}%`
    : `${s.count || 0} 笔`;

  $('lvWin').textContent = s.win_rate != null ? fmt(s.win_rate, 1) + '%' : '—';
  $('lvWin').className = 'v mono neut';
  $('lvWinSub').textContent = s.count
    ? `${s.wins} 胜 / ${s.losses} 负` + (s.flats ? ` / ${s.flats} 平` : '')
    : '—';

  $('lvPf').textContent = s.pf != null ? fmt(s.pf) : '—';
  $('lvPf').className = 'v mono ' + (s.pf != null && s.pf < 1 ? 'txt-bad' : '');

  // 回撤按「账户余额曲线」算，分母是峰值余额（不是净入金，也不是累计盈亏）
  $('lvDd').textContent = s.max_dd_pct != null ? fmt(s.max_dd_pct, 1) + '%' : '—';
  $('lvDdSub').textContent = s.dd_peak != null
    ? `${fmt(s.dd_peak)} → ${fmt(s.trough_balance)}${s.wipeouts ? ' · 清零 ' + s.wipeouts + ' 次' : ''}`
    : '占峰值余额';
  $('lvHold').textContent = s.hold_median_min != null ? mins(s.hold_median_min) : '—';

  // ---- 图表 ----
  // 余额曲线含出入金，与回撤口径一致，可以从图上直接核对
  const bc = (s.balance_curve || []).map(x => x.balance);
  drawArea('chartEquity', bc, bc.length && bc[bc.length - 1] >= bc[0] ? 'line-up' : 'line-down',
    bc.length && bc[bc.length - 1] >= bc[0] ? '#2ecc71' : '#ff4d4f');
  $('balNote').textContent = bc.length
    ? `峰值 ${fmt(s.peak_balance)} · 谷值 ${fmt(s.trough_balance)}` + (s.wipeouts ? ` · 清零 ${s.wipeouts} 次` : '')
    : '';

  const daily = (s.daily || []).slice(-14);
  drawBars('chartDaily', daily.map(x => x.date.slice(5)), daily.map(x => x.profit));

  // ---- 每日余额快照 ----
  const hist = d.account_daily || [];
  const bal = hist.map(x => x.balance);
  drawArea('chartBalance', bal, 'line-info', '#4a9eff', { W: 1240, H: 150 });
  $('histNote').textContent = hist.length
    ? `已记录 ${hist.length} 天 · 首日 ${fmt(bal[0])} → 最新 ${fmt(bal[bal.length - 1])}`
    : '尚无快照，服务运行中会每天自动记录一条';

  // ---- 风险标记 ----
  $('flagBox').innerHTML = (d.flags || []).map(f => `
    <div class="flag ${f.level}">
      <div>
        <div class="t">${f.title}</div>
        <div class="d">${f.detail}</div>
      </div>
    </div>`).join('') || '<div class="mini">无</div>';

  // ---- AI 复盘 ----
  renderAdvice(d.advice);
  renderApiMeter(d.api_calls);

  // ---- 交易评分 ----
  renderScoring(d.scoring);

  // ---- 近期交易质量 ----
  renderQuality(d.quality || {});

  // ---- 表格 ----
  const tbody = (id, rows, keyName) => {
    $(id).innerHTML = rows && rows.length ? rows.map(r => `
      <tr>
        <td>${r.key}</td>
        <td class="mono">${r.trades}</td>
        <td class="mono">${fmt(r.win_rate, 1)}%</td>
        <td class="mono ${signClass(r.net)}">${money(r.net)}</td>
        <td class="mono">${r.pf != null ? fmt(r.pf) : '—'}</td>
      </tr>`).join('') : `<tr><td colspan="5" class="empty">无数据</td></tr>`;
  };
  tbody('rowsDir', s.by_direction);
  tbody('rowsReason', s.by_reason);
  tbody('rowsSym', s.by_symbol);
  tbody('rowsWeek', s.by_weekday);

  // ---- 持仓 ----
  const pos = d.positions || [];
  $('posCount').textContent = pos.length ? `（${pos.length}）` : '（无）';
  $('rowsPos').innerHTML = pos.length ? pos.map(p => `
    <tr>
      <td class="mono">${ts2str(p.open_time)}</td>
      <td>${p.symbol}</td>
      <td class="${p.direction > 0 ? 'pos-long' : 'pos-short'}">${p.direction > 0 ? '多' : '空'}</td>
      <td class="mono">${p.volume}</td>
      <td class="mono">${fmt(p.price_open, 3)}</td>
      <td class="mono">${fmt(p.price_current, 3)}</td>
      <td class="mono">${p.sl ? fmt(p.sl, 3) : '—'}</td>
      <td class="mono">${p.tp ? fmt(p.tp, 3) : '—'}</td>
      <td class="mono ${signClass(p.profit)}">${money(p.profit)}</td>
    </tr>`).join('') : '<tr><td colspan="9" class="empty">当前无持仓</td></tr>';

  // ---- 最近交易 ----
  const tradeScores = {};
  ((d.scoring && d.scoring.trades) || []).forEach(x => { tradeScores[String(x.position_id)] = x; });

  const trades = (d.trades || []).filter(t => t.exit_time).slice(-15).reverse();
  const tradeNotes = d.trade_notes || {};
  // 正在写理由时不重建表格：20 秒轮询会把输入冲掉（场次备注踩过这个坑）
  const editingNote = document.activeElement && document.activeElement.dataset
    && document.activeElement.dataset.notePid;
  if (!editingNote) {
    $('rowsTrade').innerHTML = trades.length ? trades.map(t => {
      const sc = tradeScores[String(t.position_id)];
      const scoreCell = sc
        ? `<span class="chip c-${sc.grade}" title="${sc.flags.join('；') || '无异常'}">${sc.score}</span>`
        : '<span class="mini">—</span>';
      const note = tradeNotes[String(t.position_id)] || '';
      return `
    <tr>
      <td class="mono">${ts2str(t.exit_time)}</td>
      <td>${t.symbol}</td>
      <td class="${t.direction > 0 ? 'pos-long' : 'pos-short'}">${t.direction > 0 ? '多' : '空'}</td>
      <td class="mono"${t.partial ? ` title="部分平仓：已平 ${t.volume_closed} / 开仓 ${t.volume}"` : ''}>${t.partial ? `${t.volume_closed}/${t.volume}` : t.volume}</td>
      <td class="mono">${fmt(t.entry_price, 3)}</td>
      <td class="mono">${fmt(t.exit_price, 3)}</td>
      <td class="mono ${signClass(t.profit)}">${money(t.profit)}</td>
      <td class="mini">${reasonCn(t.close_reason)}</td>
      <td class="mono">${mins((t.exit_time - t.entry_time) / 60)}</td>
      <td>${scoreCell}</td>
      <td><input class="note-in" data-note-pid="${t.position_id}" value="${esc(note)}"
                 placeholder="为什么进？" title="写一句进场理由，AI 复盘会读它"
                 onchange="saveTradeNote(this)"></td>
    </tr>`;
    }).join('') : '<tr><td colspan="11" class="empty">暂无平仓记录</td></tr>';
  }

  // ---- 本场复盘（盘后第一眼要看的） ----
  renderSessions(d);
  renderMarks(d);
  syncDeleteButton(d);
  // AI 卡片不在这里刷：它有自己 1 秒一次的独立循环（见文件末尾），
  // 否则账本一刷新就顺带拉一次，两边节奏互相打乱。
}

