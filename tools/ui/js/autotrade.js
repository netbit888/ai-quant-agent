/* ==================== AI 交易员 ====================
 * 这块和后端 live/autotrade.py 是一对：那边每 60 秒做一次决策，这边只读结果。
 * 关键是把「模型判断」和「规则兜底」分开显示——模型挂掉时会退回动量规则，
 * 两种来源的命中率混在一起算，就永远看不出模型到底有没有用。
 */
let atLoading = false;

// 当前账户的金额单位（USD / USC）。美分账户的一切金额都是「分」，
// 界面上每一处提到钱的地方都要带上它，否则数字看着一样、实际差 100 倍。
let AT_MONEY = 'USD';
// 1 USD 折多少账户货币（美元户 1，美分户 100）。成本估算内部按美元算，
// 只有乘上它才能和「止损 3.00」这种账户货币阈值摆在一起比较。
let AT_FACTOR = 1;

async function loadAutoTrade() {
  if (atLoading) return;          // 慢响应时别把请求堆起来
  atLoading = true;
  try {
    const r = await fetch('/api/autotrade');
    const d = await r.json();
    if (d && d.money_unit) AT_MONEY = d.money_unit;
    if (d && d.acct_factor) AT_FACTOR = Number(d.acct_factor) || 1;
    renderAutoTrade(d);
    // 同意的结局要回到操作条上（弹窗那边起的头，见 sig-modal.js 的
    // sigVerdictCheck）。挂在轮询这里 = 零额外请求。
    if (typeof sigVerdictCheck === 'function') sigVerdictCheck(d);
    // 脉冲浮盈提醒（趋势单）：数据本来就在 position.burst_alert 里，
    // 同样零额外请求。见 burst-alert.js。
    if (typeof burstCheck === 'function') burstCheck(d);
  } catch (e) {
    // 静默失败：这块挂了不该把整个实盘面板拖下水
    const st = $('atState');
    st.style.display = '';
    st.textContent = '读取失败';
    st.className = 'badge-off';
  } finally {
    atLoading = false;
  }
}

const AT_KIND = {
  open: '开仓', close: '平仓', hold: '持有', skip: '跳过',
  stop: '停止', open_failed: '开仓失败', close_failed: '平仓失败',
  review: '复盘', flatten: '清仓', error: '异常',
  signal: '人工确认',
  // 保本止损（2026-10-29）：这一行记的是"把券商端 SL 改到开仓价"这件事，
  // 不是成交。单独一个 kind，免得和开平仓混在一起数笔数。
  be: '保本止损', be_fail: '保本没挂上',
};

/* 人工确认的结案原因。和 Python 侧 _fmt 的 ANS 表同源，
 * 两边不一致的话面板和文本流水会各说各话。 */
const AT_ANSWER = {
  approve: '同意', deny: '否决', expired: '超时未答', cancelled: '已取消',
  blocked: '等待期已有持仓', gone: '仓已不在', sl: '被硬止损抢占',
  timeout: '被持仓上限抢占', void_slip: '价格漂移作废',
  void_unknown: '读不到持仓作废', void_state: '此刻不可开仓',
  orphan: '孤儿信号（已清理）',
};

/* 常驻状态带。和 AI 卡片同源渲染，两处数字不可能打架。
 * 这里只放三个问题：在不在跑 / 有没有仓 / 现在多少。
 * 其余一律留给下面的卡片——状态带一长就没人看了。 */
function renderStatusBar(a) {
  const bar = $('atBar');
  if (!bar) return;
  let state = '未启动', cls = '';
  if (!a || !a.ok) {
    state = '数据读取失败';
  } else if (a.running) {
    // 辅助模式必须一眼看出来：同样是「运行中」，真实做单会自己下单，
    // 辅助模式在等你看弹窗——这两种状态的"现在需要你做什么"完全不同。
    state = a.assist ? (a.dry_run ? '运行中 · 辅助演练' : '运行中 · 辅助')
                     : (a.dry_run ? '运行中 · 纸面' : '运行中 · 真实');
    cls = (a.dry_run && !a.assist) ? 'on' : 'warn';
  } else if (a.stale) {
    state = '已停止（面板曾重启）';
    cls = 'warn';
  } else if (a.stop_reason) {
    state = '已停止 · ' + String(a.stop_reason).slice(0, 22);
    cls = 'warn';
  }
  bar.className = 'statusbar' + (cls ? ' ' + cls : '');
  // 待确认信号：弹窗可能被最小化/切到别的窗口，状态带上留一个永不消失的标记。
  const sig = a && a.pending_signal;
  $('atBarState').textContent = state
    + (sig ? ` · 待确认 ${Math.max(0, Number(sig.remain_sec) || 0)}s` : '');

  const p = a && a.position;
  if (p) {
    const dir = p.direction > 0 ? '多' : '空';
    const fp = (a.float_pnl == null) ? '—' : money(a.float_pnl);
    const pk = (p.peak_pnl == null) ? null : money(Number(p.peak_pnl));
    const fpCls = (a.float_pnl == null) ? '' : signClass(a.float_pnl);
    // 峰值分母必须跟着这一笔的 plan 走：趋势单的门槛是 tp_min_trend（7.00），
    // 用震荡档的 tp_min_usd（3.25）会写成「峰 +3.62/3.25」——和持仓面板的
    // 「+3.62 / 7.00」自相矛盾，还会被读成「保本是 3.25」（2026-10-31 报障）。
    const barTp = Number((String(p.plan || '') === 'trend' ? a.tp_min_trend : a.tp_min_usd) || 0);
    $('atBarPos').innerHTML =
      `<span class="sb-v ${p.direction > 0 ? 'pos-long' : 'pos-short'}">` +
      `${dir} ${p.lots}</span>` +
      `<span class="sb-k">@</span><span class="sb-v">${fmt(p.price_open, 3)}</span>` +
      `<span class="sb-k">浮盈</span><span class="sb-v ${fpCls}">${fp}</span>` +
      (pk ? `<span class="sb-k">峰</span><span class="sb-v">${pk}` +
            (barTp ? '/' + fmt(barTp, 2) : '') + `</span>` : '');
  } else {
    const pend = Number(a && a.pending_reviews || 0);
    $('atBarPos').innerHTML = `<span class="sb-k">空仓` +
      (pend ? ` · ${pend} 笔待复盘` : '') + `</span>`;
  }

  if (a && a.ok) {
    // 状态带同理：写胜/负，不写命中率（分母剔了硬止损，天生接近 100%）。
    const cT = (a.closed_today == null) ? (a.closed || 0) : a.closed_today;
    const pT = (a.pnl_today == null) ? (a.pnl || 0) : a.pnl_today;
    const tally = (a.wins_today == null || a.losses_today == null) ? ''
      : `${a.wins_today} 胜 ${a.losses_today} 负 · `;
    $('atBarStat').innerHTML =
      `今日 已平 <b class="sb-v">${cT}</b> 笔 · ${tally}累计 ` +
      `<b class="sb-v ${signClass(pT)}">${money(pT)}</b>`;
  } else {
    $('atBarStat').textContent = '—';
  }
  // 停线程 / 清仓都要能用：stale 状态（面板重启过、磁盘还留着 running=true）
  // 也得让人点得到，否则界面永远卡在「运行中」而线程早没了。
  $('atBarStop').disabled = !(a && (a.running || a.stale));
}

/* ---- 成交反馈：开/平仓是"钱动了"的时刻 ----
   原来一笔真实单成交只在日志流里多一行字（日志还默认隐藏跳过行）——
   盯盘时"我的单到底成没成"缺少强反馈。这里盯 /api/autotrade 的 recent 日志：
   用每行 ts 去重，1 秒轮询下同一条只会被看到一次；开/平仓各响一声
   （开仓中音、平仓低音，和信号铃的两声上行区分开）+ 写结果条。
   首次加载只建基线不响：面板重启前攒下的旧成交翻出来再响一遍只会吓人。 */
let fillLastTs = 0;
function fillFeedback(a) {
  const rows = (a && a.recent) || [];
  if (!rows.length) return;
  const ts = r => Number(r && r.ts) || 0;
  if (!fillLastTs) {
    fillLastTs = rows.reduce((m, r) => Math.max(m, ts(r)), 0);
    return;
  }
  const fresh = rows.filter(r => ts(r) > fillLastTs);
  if (!fresh.length) return;
  fillLastTs = fresh.reduce((m, r) => Math.max(m, ts(r)), fillLastTs);
  const opens = fresh.filter(r => r.kind === 'open');
  const closes = fresh.filter(r => r.kind === 'close');
  const fails = fresh.filter(r => r.kind === 'open_failed' || r.kind === 'close_failed');
  if (fails.length) {
    const f = fails[fails.length - 1];
    atSay('<b>下单失败：</b>' + esc(String(f.error || f.reason || '未知原因').slice(0, 120)), 'err');
  }
  if (opens.length) {
    const o = opens[opens.length - 1];
    beepTrade(false);
    atSay(`<b>已开仓</b> ${o.direction > 0 ? '多' : '空'} ${esc(String(o.lots || ''))} 手 @ ${fmt(o.price, 3)}`,
          'ok', AT_VERDICT_TTL_MS);
  }
  if (closes.length) {
    beepTrade(true);
    const total = closes.reduce((s, r) => s + (Number(r.pnl) || 0), 0);
    atSay(closes.length > 1
      ? `<b>已平仓 ${closes.length} 笔</b>，合计 ${money(total)}`
      : `<b>已平仓</b> ${closes[0].direction > 0 ? '多' : '空'}单 ${money(closes[0].pnl)}`,
      'ok', AT_VERDICT_TTL_MS);
  }
}

