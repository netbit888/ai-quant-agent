/* ==================== 退出程序 ====================
 * 这是唯一可控的退出路径。
 *   浏览器模式：关标签页 ≠ 退出（服务端还在跑），所以只能靠这个按钮。
 *   原生窗口：点窗口 X 会由 Python 侧调 onCloseRequested() 走同一条路，
 *            所以确认逻辑只有这一份，不会分叉。
 */
async function quitApp() {
  // 第二个窗口（主程序在别人那里）：这里的按钮会打到对方进程，
  // 一点就把正在跑的那份关掉 —— 必须拦住。
  if (typeof window !== 'undefined' && window.__AQA_ATTACH__) {
    qaToast('这份面板是第二个窗口，主程序在另一个窗口里运行。\n要退出请回到那个窗口。', 'err');
    return;
  }
  let p = null;
  try {
    p = await (await fetch('/api/app/quit-preview', { cache: 'no-store' })).json();
  } catch (e) {
    p = { ok: false, count: 0, error: String(e) };
  }
  if (!p.count) {
    // 原生窗口：没仓就不必再问一遍，直接退（点 X 的语义就是"我要关"）。
    // 但读不到持仓（p.ok=false）时仍要确认 —— 那可能是 MT5 断开，
    // 不能因为 count 默认是 0 就静默退出。
    if (isNative() && p.ok) { postQuit(false); return; }
    const go = await confirmApiCall({
      title: '退出程序？',
      desc: p.mt5
        ? '当前账户没有持仓。AI 线程会先停下，然后关闭程序。'
        : '读不到持仓（MT5 未连接）。如果终端里还有仓，请先去 MT5 处理。',
      cost: '不会下任何单',
      okText: '退出',
    });
    if (go) postQuit(false);
    return;
  }
  const f = Number(p.float_pnl || 0);
  $('quitDesc').textContent =
    `账户上有 ${p.count} 笔持仓，浮动盈亏 ${f >= 0 ? '+' : ''}${f.toFixed(2)} ${AT_MONEY}。`;
  // 原来的文案是「这套策略不挂止盈止损」——过期了。开仓成交后 _attach_entry_sl
  // 会把硬止损挂到券商端（只不挂 TP），所以「强制退出」不等于让仓位裸奔。
  // 但挂载失败的那笔确实没有，两种情形必须分开说：用户是拿这句话决定要不要
  // 先平仓的，说错方向比不说更糟。bare_count 缺失（老后端）时退回中性表述。
  const bare = Number(p.bare_count || 0);
  const unknown = (p.bare_count === undefined || p.bare_count === null);
  $('quitCost').textContent = p.dry_run === false
    ? (unknown
        ? '强制退出会把仓位留在券商那边——券商端止损（若已挂上）仍然有效，但没有止盈，退出后没人看着。'
        : bare > 0
          ? `强制退出会把仓位留在券商那边——其中 ${bare} 笔没有券商端止损，退出后没人看着。`
          : '强制退出会把仓位留在券商那边——券商端止损仍在生效（不挂止盈），但退出后没人看着。')
    : '纸面仓位只存在本地，强制退出等于直接丢弃。';
  $('quitForce').disabled = false;
  $('quitFlatten').disabled = false;
  // 焦点落在「取消」上：退出是不可逆操作，Enter 误触的代价必须最小
  maskOpen($('quitMask'), { focus: $('quitMask').querySelector('.btn-cancel') });
}

function quitMaskClose() {
  maskClose($('quitMask'));
  nativeApi('quit_dismissed');   // 窗口模式下告诉 Python：这次没退，别关窗
}

async function postQuit(flatten) {
  $('quitForce').disabled = true;
  $('quitFlatten').disabled = true;
  if (isNative()) {
    // 窗口马上就要消失了，别再写"可以关闭这个页面了"这种浏览器时代的话
    $('quitDesc').textContent = flatten ? '正在平掉所有仓位并退出…' : '正在退出…';
    $('quitCost').textContent = '';
  }
  let failed = 0;
  try {
    const r = await fetch('/api/app/quit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, flatten }),
    });
    const j = await r.json();
    failed = (j.failed || []).length;
  } catch (e) {
    /* 进程死了会让 fetch 抛错——这恰恰说明退出成功 */
  }
  // 窗口模式：关窗由 Python 侧做（先 destroy 再清进程），
  // 这里改 body 只会闪一下，反而像崩溃。
  if (isNative()) return;
  document.body.innerHTML =
    '<div style="padding:56px;font:15px/1.8 system-ui,-apple-system,sans-serif;color:#e8eef6">'
    + '<h3 style="margin:0 0 8px">程序已退出</h3>'
    + (failed
        ? `<p style="color:#ff8b8d">有 ${failed} 笔平仓失败，请去 MT5 终端确认。</p>`
        : '<p>可以关闭这个页面了。</p>')
    + '</div>';
}

// ---------------------------------------------------------------- 应用内更新
// 前端只是状态机：检测/下载/安装全部在后端 updater。mode!=='full' 时
// 整块 UI 隐藏（源码跑 git pull 重启即更新，绿色版没有升级通道）。

function updateShow() {
  // 焦点落在「暂不」上：更新不着急，Enter 误触不该触发下载
  maskOpen($('updateMask'), { focus: $('updateLater') });
}
function updateLater() { maskClose($('updateMask')); }

async function updateProgress() {
  try {
    return await (await fetch('/api/app/update/progress', { cache: 'no-store' })).json();
  } catch (e) { return null; }
}

// 下载速率采样：后端只给 received 累计值，速率在前端两次采样间算。
// 采样间隔太短（<400ms）会算出抖动很大的假数字，所以不够 400ms 就沿用上次。
let _dlPrev = { t: 0, b: 0, kbps: 0 };

function dlRate(received) {
  const now = Date.now();
  if (!_dlPrev.t) { _dlPrev = { t: now, b: received, kbps: 0 }; return 0; }
  const dt = (now - _dlPrev.t) / 1000;
  if (dt >= 0.4) {
    _dlPrev = { t: now, b: received, kbps: (received - _dlPrev.b) / dt / 1024 };
  }
  return _dlPrev.kbps;
}

function kbpsText(k) {
  if (!k || k < 1) return '停滞…';
  if (k >= 1024) return (k / 1024).toFixed(1) + ' MB/s';
  return k.toFixed(0) + ' KB/s';
}

// 按 snapshot 驱动弹窗按钮与进度文案。返回 snapshot 供调用方读 check。
async function updateRender() {
  const j = await updateProgress();
  if (!j) return null;
  const d = j.download || {};
  const go = $('updateGo'), later = $('updateLater'), prog = $('updateProgress');
  if (d.status !== 'downloading') _dlPrev = { t: 0, b: 0, kbps: 0 };
  if (d.status === 'downloading') {
    go.disabled = true; later.disabled = true;
    const mb = (d.received / 1048576).toFixed(1), tot = (d.total / 1048576).toFixed(1);
    // 分片/重试数字全部来自后端真实值，不在前端编
    const seg = d.segments > 1 ? ` · ${d.segments} 连接` : '';
    const retry = d.retries > 0 ? ` · 重试 ${d.retries} 次` : '';
    // 一个字节都没到手 = 还卡在探测/握手。这时报「停滞」是错的：真实状态是
    // 「连不上，正在重试」，和「下着但没速度」是两回事，必须分开说。
    const tryNo = d.probe_try > 1 ? `（第 ${d.probe_try} 次尝试）` : '';
    const speed = d.received > 0
      ? kbpsText(dlRate(d.received)) : `正在连接…${tryNo}`;
    prog.textContent = `下载中 ${d.pct}% (${mb}/${tot} MB) · ${speed}${seg}${retry}`;
  } else if (d.status === 'done') {
    go.disabled = false; later.disabled = false;
    go.textContent = '立即升级并重启'; go.onclick = applyUpdate;
    prog.textContent = '下载完成。点击按钮后程序会退出、静默安装、然后自动重启。';
  } else if (d.status === 'error') {
    go.disabled = false; later.disabled = false;
    go.textContent = '重试下载'; go.onclick = startUpdateDownload;
    prog.textContent = '下载失败：' + (d.error || '未知错误');
  } else {
    go.disabled = false; later.disabled = false;
    go.textContent = '下载并安装'; go.onclick = startUpdateDownload;
    prog.textContent = '';
  }
  return j;
}

async function updateWaitCheck(ms) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    const j = await updateRender();
    if (j && j.check && j.check.status !== 'checking') return j;
    await new Promise(r => setTimeout(r, 400));
  }
  return await updateRender();
}

// 增量/完整包的尺寸文案。数字缺失显示占位符，绝不显示 NaN——
// 后端字段是渐进增强（老版本快照里没有 plan），前端必须兜底。
function mbText(n) {
  return (typeof n === 'number' && n > 0) ? (n / 1048576).toFixed(1) : '—';
}

function updatePlanLine(c) {
  if (!c || !c.newer) return '';
  if (c.plan === 'delta') {
    return `本次为增量更新：仅需 ${mbText(c.delta_size)} MB（完整包 ${mbText(c.full_size)} MB）`;
  }
  return `本次需下载完整安装包 ${mbText(c.full_size)} MB（当前版本没有匹配的增量包）`;
}

// release notes 是 markdown（GitHub release body 原文），这个弹窗用纯文本渲染，
// 直接塞进去满屏都是 ** 和 | 表格线。这里做最轻量的降级：去掉装饰符、表格压成一行。
// 只产出文本、不产生 HTML——远程内容一律不进 innerHTML。
function notesPlain(md) {
  const out = [];
  for (const raw of String(md || '').replace(/\r/g, '').split('\n')) {
    let s = raw.trim();
    if (!s) { if (out.length && out[out.length - 1] !== '') out.push(''); continue; }
    if (/^\|?[\s:|-]+\|[\s:|-]*$/.test(s)) continue;      // 表格分隔行
    if (s.startsWith('|')) {                              // 表格数据行 → a · b · c
      const cells = s.split('|').map(x => x.trim()).filter(Boolean);
      if (cells.length) out.push(cells.join(' · '));
      continue;
    }
    s = s.replace(/^#{1,6}\s*/, '')
         .replace(/\*\*(.+?)\*\*/g, '$1')
         .replace(/`([^`]+)`/g, '$1')
         .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
    out.push(s);
  }
  return out.join('\n').trim();
}

function updatePopup(j) {
  if (!j || !j.check || !j.check.newer) return;
  $('updateTitle').textContent = `发现新版本 v${j.check.latest}（当前 v${j.check.current}）`;
  $('updatePlan').textContent = updatePlanLine(j.check);
  const notes = notesPlain(j.check.notes);
  const desc = $('updateDesc'), wrap = $('updateDescWrap');
  desc.textContent = (notes.length > 6000 ? notes.slice(0, 6000) + '\n……' : notes) || '无更新日志';
  desc.scrollTop = 0;
  const syncFade = () => wrap.classList.toggle(
    'more', desc.scrollTop + desc.clientHeight < desc.scrollHeight - 4);
  desc.onscroll = syncFade;          // 用 onscroll 而不是 addListener：反复弹只留一个
  requestAnimationFrame(syncFade);   // 排版完才知道有没有溢出
  updateShow();
}

// 页面加载时静默检查一次（走后端 60 秒节流，不在前端做频率控制）
async function startupUpdateCheck() {
  const j0 = await updateProgress();
  if (!j0) return;                       // 后端都没连上，别谈更新
  const btn = $('appUpdate');
  if (j0.mode !== 'full') {
    // 源码/绿色版没有升级通道：藏按钮，留一句话给好奇的人
    if (btn) {
      btn.style.display = 'none';
      if (j0.mode === 'source') btn.title = '源码运行：git pull 后重启生效';
    }
    return;
  }
  try { await fetch('/api/app/update/check', { cache: 'no-store' }); } catch (e) { return; }
  updatePopup(await updateWaitCheck(8000));
}

async function manualCheckUpdate() {
  const b = $('appUpdate');
  const old = b.textContent;
  b.disabled = true; b.textContent = '检查中…';
  try {
    await fetch('/api/app/update/check?fresh=1', { cache: 'no-store' });
    const j = await updateWaitCheck(15000);
    if (j && j.check && j.check.newer) {
      updatePopup(j);
    } else if (j && j.check && j.check.status === 'ok') {
      b.textContent = `已是最新 v${j.check.current}`;
      b.title = '';
    } else {
      // 后端带回了具体原因（限流/404/网络），按钮上只放短话，悬停看全文
      const err = (j && j.check && j.check.error) || '';
      b.textContent = err.includes('限流') ? '检查失败（限流）'
        : err.includes('404') ? '检查失败（仓库不可达）' : '检查失败（网络）';
      b.title = err;
    }
  } catch (e) {
    b.textContent = '检查失败';
  }
  setTimeout(() => { b.textContent = old; b.disabled = false; }, 2500);
}

async function startUpdateDownload() {
  try {
    await fetch('/api/app/update/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
  } catch (e) { /* 由下面的进度轮询兜错 */ }
  // 原来这里硬编码 10 分钟，到点就停刷新——但后端还在下。慢网下用户以为卡死，
  // 关窗口重来，白白丢掉已下的字节。现在只在终态或用户关掉弹窗时退出，
  // 60 分钟只是个安全阀（后端自己会重试续传，轮询不该比它先放弃）。
  const t0 = Date.now();
  while (Date.now() - t0 < 60 * 60 * 1000) {
    const j = await updateRender();
    if (!j) break;
    if ($('updateMask').classList.contains('hidden')) break;
    const st = (j.download || {}).status;
    if (st === 'done' || st === 'error') break;
    await new Promise(r => setTimeout(r, 500));
  }
}

async function applyUpdate() {
  const go = $('updateGo'), later = $('updateLater');
  go.disabled = true; later.disabled = true;
  $('updateProgress').textContent = '正在退出并启动安装程序，装完后应用会自动重启…';
  try {
    await fetch('/api/app/update/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
  } catch (e) { /* fetch 断开 = 进程按计划退出，不是失败 */ }
}

/* 窗口 X 被点时由 Python 侧调用。复用 quitApp，不另写一套。
   连点 X 会重复进来，但 quitApp 的 preview 是只读的、mask 重复显示无害。 */
function onNativeCloseRequested() {
  quitApp();
}

if (typeof window !== 'undefined') {
  window.aqaNative = { onCloseRequested: onNativeCloseRequested };
}

