/* ==================== 全屏提醒幕布 ====================
 * 目的只有一个：人离开屏幕时，别让别人（或自己）瞎点这台机器。
 * 为什么不做成新开窗口：浏览器只允许「用户点击」的那一刻进全屏，
 * 新窗口里的 requestFullscreen 没有这个手势，会被直接拒绝。
 * 所以用当前页面全屏 + 一层盖住一切的幕布，效果一样、还不会被弹窗拦截。
 */
function enterTradingFullscreen() {
  const el = $('atCurtain');
  if (el) el.classList.remove('hidden');
  // 原生窗口：让宿主窗口真全屏（铺满显示器、盖住任务栏）。
  // DOM 的 requestFullscreen 在 WebView2 里只铺满控件边界，标题栏还在。
  // 注意 pywebview 的 api 调用返回 Promise —— 不要拿返回值做真假判断。
  if (isNative()) {
    nativeApi('set_fullscreen', true);
    updateCurtain(lastAutoData);
    return;
  }
  const root = (typeof document !== 'undefined' && document.documentElement)
    || (typeof document !== 'undefined' && document.body) || null;
  const req = root && (root.requestFullscreen || root.webkitRequestFullscreen);
  if (req) {
    try {
      const p = req.call(root);
      if (p && typeof p.catch === 'function') p.catch(() => {});   // 被拒就只留幕布
    } catch (e) { /* 老浏览器没有全屏 API：幕布照样能用 */ }
  }
  updateCurtain(lastAutoData);
}

function exitTradingFullscreen() {
  const el = $('atCurtain');
  if (el) el.classList.add('hidden');
  if (isNative()) { nativeApi('set_fullscreen', false); return; }
  if (typeof document === 'undefined') return;
  const d = document;
  if (d.fullscreenElement && d.exitFullscreen) {
    try { d.exitFullscreen(); } catch (e) { /* 忽略 */ }
  }
}

/* 幕布的更新逻辑。只摆三个数 + 一句状态，别把分析塞进来。
 * 每个元素都判空：轮询还没拿到数据时 updateCurtain 会被调用，
 * 少一个 id 就整块不刷新，那才是最糟的情况（幕布上写着上一分钟的状态）。 */
function curCard(label, value, sub, cls) {
  return `<div class="cur-card"><i>${esc(label)}</i>`
    + `<b class="${cls || ''}">${value}</b>`
    + (sub ? `<s>${esc(sub)}</s>` : '') + `</div>`;
}

function updateCurtain(a) {
  const root = $('atCurtain');
  if (!root) return;
  const ok = !!(a && a.ok);
  const running = !!(ok && a.running);
  const stopped = !!ok && !running;

  // 停止 = 出事了，整块屏转红。这是这块屏上红色唯一的含义。
  root.classList.toggle('is-stopped', stopped);

  const set = (id, txt) => { const e = $(id); if (e) e.textContent = txt; };

  set('atCurtainState', !ok ? '状态未知'
    : running ? (a.dry_run ? '运行中 · 纸面' : '运行中 · 真实')
    : (a.stale ? '已停止（面板重启过）' : 'AI 已停止'));
  set('atCurtainSym', (a && a.symbol) || 'XAUUSD · M1');
  set('atCurtainClock', new Date().toLocaleTimeString('zh-CN', { hour12: false }));

  // 三种状态三句话。停着还写「请勿操作」是骗人；读不到后端也不是「已停止」。
  if (!ok) {
    set('atCurtainTitle', '状态未知');
    set('atCurtainLead', '连不上面板后端，无法确认它是否还在跑。请回面板看一眼再动。');
    set('atCurtainStat', '—');
    return;
  }
  set('atCurtainTitle', running ? '请勿操作这台电脑' : 'AI 已停止');
  set('atCurtainLead', running
    ? '手动开平仓、切走终端、让电脑睡眠，都会打乱它正在做的事。'
    : '可以正常使用这台电脑了。仓位不会自己动，请回终端确认。');

  const p = a.position;
  const cards = [];
  if (p) {
    const dir = p.direction > 0 ? '多' : '空';
    cards.push(curCard('当前持仓', `${dir} ${p.lots} 手`, `@ ${fmt(p.price_open, 3)}`,
      p.direction > 0 ? 'pos-long' : 'pos-short'));
    if (a.float_pnl != null) {
      const pk = (p.peak_pnl == null) ? ''
        : `峰值 ${money(Number(p.peak_pnl))}`
          + (a.tp_min_usd ? ` / ${fmt(a.tp_min_usd, 2)}` : '');
      cards.push(curCard('浮动盈亏', money(a.float_pnl), pk, signClass(a.float_pnl)));
    } else {
      cards.push(curCard('浮动盈亏', '—', '报价读取中'));
    }
  } else {
    cards.push(curCard('当前持仓', '空仓', '等下一次信号', 'idle'));
  }
  // **不显示命中率**。这个数在这套架构下是失真的：分母只算「模型自己平的」，
  // 硬止损平掉的那笔不进分母。实测 15:15:58 那笔 -3.42 是 sl 平的，于是
  // 3 胜 0 负显示成 100%，真实胜率是 3/4=75%。给路过的人看一个剔除了亏损的
  // 百分比，比什么都不显示更糟。改成「W 胜 L 负」——样本量自己会说话。
  const cToday = (a.closed_today == null) ? (a.closed || 0) : a.closed_today;
  const pnlToday = (a.pnl_today == null) ? (a.pnl || 0) : a.pnl_today;
  const tally = (a.wins_today == null || a.losses_today == null)
    ? `已平 ${cToday} 笔`
    : `已平 ${cToday} 笔 · ${a.wins_today} 胜 ${a.losses_today} 负`;
  cards.push(curCard('今日累计', money(pnlToday), tally, signClass(pnlToday)));

  const el = $('atCurtainStat');
  if (el) el.innerHTML = `<div class="cur-cards">${cards.join('')}</div>`;
}

