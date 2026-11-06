/* ==================== 人工确认弹窗（AI 辅助做单）====================
   数据来源只有一份：/api/autotrade 每秒回来的 summary.pending_signal。
   不另开接口，避免"信号已经失效、弹窗还在"这种最伤信任的错位。

   三条硬规矩：
   1. 不响应 Esc、不点遮罩关闭。误按一次＝否决一次，白丢一个算好的信号。
   2. 同一个 id 只响一次铃、只置顶一次。面板每秒轮询，不去重就是连续响铃。
   3. 倒计时基准用 server_ts（服务端时钟），客户端时钟偏几分钟也不会显示负数。
*/
let lastSigId = null;
let answeredSigId = null;
let sigRemain = 0;
let sigTimer = null;
let sigBusy = false;
let titleTimer = null;
let titleBase = '';
let audioCtx = null;

/* WebView2 的 WebAudio 必须先由用户手势解锁，否则 resume() 挂起、一声不响。
   挂在首次 pointerdown/keydown 上——用户点了页面任何地方就算解锁。 */
function unlockAudio() {
  try {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      audioCtx = new AC();
    }
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) { /* 没声音也要能用，绝不在这里抛 */ }
}
document.addEventListener('pointerdown', unlockAudio, { once: false });
document.addEventListener('keydown', unlockAudio, { once: false });

/* 提示音静音开关。半夜挂着软件的人没法靠系统音量单独关这一个铃——
   以前只能整机静音。状态持久化在 localStorage，重启面板保留。
   localStorage 可能不可用（隐私模式/冒烟环境的假 DOM），全程 try 兜底。 */
const MUTE_KEY = 'aqa_mute';
function isMuted() {
  try { return typeof localStorage !== 'undefined' && localStorage.getItem(MUTE_KEY) === '1'; }
  catch (e) { return false; }
}
function toggleMute() {
  const now = !isMuted();
  try { if (typeof localStorage !== 'undefined') localStorage.setItem(MUTE_KEY, now ? '1' : '0'); }
  catch (e) { /* 写不进去也只影响本次会话 */ }
  syncMuteButton();
  if (!now) beepTone(880, 0, 0.12, 0.2);   // 打开时给一声确认，知道开关确实生效了
}
function syncMuteButton() {
  const b = $('atBarMute');
  if (!b) return;
  const m = isMuted();
  b.textContent = m ? '提示音：关' : '提示音：开';
  b.classList.toggle('off', m);
}

/* 单音合成基元：hz 频率、dt 相对起始秒、dur 时长、vol 峰值增益。 */
function beepTone(hz, dt, dur, vol) {
  try {
    unlockAudio();
    if (!audioCtx) return;
    const t0 = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = hz;
    gain.gain.setValueAtTime(0.0001, t0 + dt);
    gain.gain.exponentialRampToValueAtTime(vol, t0 + dt + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + dur);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t0 + dt);
    osc.stop(t0 + dt + dur + 0.02);
  } catch (e) { /* 声音是锦上添花 */ }
}

/* 信号铃：两声上行短音，够醒。静音时跳过（置顶和标题闪烁照常，视觉通道不缺）。 */
function beepSignal() {
  if (isMuted()) return;
  beepTone(880, 0, 0.16, 0.26);
  beepTone(1320, 0.18, 0.16, 0.26);
}

/* 成交反馈音：开仓一声中音，平仓一声低音，和信号铃（两声上行）明显不同。
   真实单成交原来只在日志流里多一行字——"我的单到底成没成"缺少强反馈。 */
function beepTrade(isExit) {
  if (isMuted()) return;
  beepTone(isExit ? 620 : 780, 0, 0.14, 0.22);
}

function flashTitle(on) {
  if (!on) {
    if (titleTimer) { clearInterval(titleTimer); titleTimer = null; }
    if (titleBase) document.title = titleBase;
    return;
  }
  if (titleTimer) return;
  if (!titleBase) titleBase = document.title;
  let flip = false;
  titleTimer = setInterval(() => {
    flip = !flip;
    document.title = flip ? '🔔 有信号待确认' : titleBase;
  }, 900);
}

/* 把窗口还原并置顶 5 秒。浏览器模式下没有桥，靠标题闪烁 + 声音兜底。 */
function raiseWindowForSignal() {
  try {
    const p = nativeApi('raise_window', 5);
    if (p && typeof p.catch === 'function') p.catch(() => {});
  } catch (e) { /* 桥挂了不影响弹窗 */ }
}

function sigFmtTags(sig) {
  const f = sig.feat || {};
  const mtf = f.mtf || {};
  const out = [];
  const push = (txt, bad) => { if (txt) out.push({ txt, bad: !!bad }); };
  if (f.pos != null) {
    const pos = Number(f.pos);
    push(`位置 ${(pos * 100).toFixed(0)}%`, pos >= 0.65 || pos <= 0.35);
  }
  if (f.er != null) push(`ER ${Number(f.er).toFixed(2)}`);
  if (f.atr != null) push(`ATR ${Number(f.atr).toFixed(2)}`);
  if (f.streak != null) push(`连${Number(f.streak) > 0 ? '阳' : '阴'} ${Math.abs(Number(f.streak))}`);
  if (mtf.pos != null) push(`M5位置 ${(Number(mtf.pos) * 100).toFixed(0)}%`);
  if (mtf.er != null) push(`M5 ER ${Number(mtf.er).toFixed(2)}`);
  if (f.regime) push(String(f.regime));
  const box = $('sigTags');
  box.innerHTML = '';
  out.forEach(o => {
    const sp = document.createElement('span');
    if (o.bad) sp.className = 'bad';
    sp.textContent = o.txt;
    box.appendChild(sp);
  });
}

function sigShow(sig) {
  const isExit = sig.kind === 'exit';
  const dir = Number(sig.direction) > 0 ? 1 : -1;
  const long = dir > 0;
  const pUp = sig.p_up == null ? null : Number(sig.p_up);
  const pShort = pUp == null ? null : 1 - pUp;

  $('sigTitle').textContent = isExit ? '平仓信号 · 需要你确认' : '开仓信号 · 需要你确认';
  $('sigSub').textContent = isExit
    ? `持仓 ${long ? '多' : '空'} · 已持 ${Math.round(Number(sig.held_sec || 0) / 60)} 分钟`
    : `${long ? '做多' : '做空'} · ${sig.price == null ? '' : fmt(sig.price, 3)}`;

  // 最终裁决：入场看 act/方向，出场就是"平"。
  // 颜色走全站方向色（--up 绿涨 / --down 红跌），和标签行、概率条同一套语义。
  const v = $('sigVerdict');
  if (isExit) {
    v.textContent = `平掉 ${long ? '多单' : '空单'}`;
    v.className = 'sig-verdict';
  } else {
    v.textContent = long ? '做多' : '做空';
    v.className = 'sig-verdict ' + (long ? 'long' : 'short');
  }

  if (pUp == null) {
    $('sigBarLong').style.width = '50%';
    $('sigBarShort').style.width = '50%';
    $('sigLabLong').textContent = '看涨 —';
    $('sigLabShort').textContent = '看跌 —';
  } else {
    $('sigBarLong').style.width = (pUp * 100).toFixed(1) + '%';
    $('sigBarShort').style.width = (pShort * 100).toFixed(1) + '%';
    $('sigLabLong').textContent = `看涨 ${(pUp * 100).toFixed(0)}%`;
    $('sigLabShort').textContent = `看跌 ${(pShort * 100).toFixed(0)}%`;
  }
  $('sigPriceLab').textContent = sig.price == null ? '' : `触发价 ${fmt(sig.price, 3)}`;

  sigFmtTags(sig);

  // 大模型判断 = 它自己的原话 + p_up。原话最能看出它到底在赌什么。
  $('sigModel').textContent = (sig.reason || '（模型没给理由）');

  // 程序判断：方向倾向 + 位置判定/建议。这里是"仅供参考"的那一半，
  // 所以要写清它并不拦单，否则用户会以为程序已经点头了。
  const bits = [];
  const pd = Number(sig.prog_dir || 0);
  bits.push(pd > 0 ? '方向倾向：做多' : pd < 0 ? '方向倾向：做空' : '方向倾向：无（不给建议）');
  // 「与程序一致」只比方向（prog_dir vs direction），**不比位置、措辞、跨周期**。
  // 2026-10-29 20:14 实盘：贴着上沿追多（pos 0.84），程序给出 top_long_ban，
  // 弹窗同时写着「模型自称与程序一致」和「程序意见：此处追多＝接最后一棒」——
  // 两句话并排，读者只会记住前一句，12.7 秒后点了同意，-3.11。
  // 所以有建议时不再说「一致」，按建议类型说清是哪儿不一致。
  const advCode = String(sig.advice || '');
  const advText = sig.advice_text ? String(sig.advice_text) : '';
  if (sig.agree === true) {
    bits.push(!advCode && !advText ? '模型自称与程序一致'
      : advCode === 'act_guard' ? '方向一致，但理由在说别进'
      : '方向一致，但位置有问题');
  }
  if (sig.agree === false) bits.push('模型自称与程序分歧');
  if (advText) bits.push('程序意见：' + advText);
  else if (advCode) bits.push('程序意见：' + advCode);
  $('sigProg').textContent = bits.join('\n');

  $('sigYes').textContent = isExit ? '同意平仓' : `同意${long ? '做多' : '做空'}`;
  $('sigNo').textContent = isExit ? '继续持有' : '不做';
  $('sigYes').disabled = false;
  $('sigNo').disabled = false;
  sigBusy = false;

  const waitSec = Math.max(5, Number(sig.wait_sec || 0));
  $('sigHint').textContent = isExit
    ? `等 ${sigRemain} 秒；不点＝按模型决定平仓。硬止损/持仓上限期间照常生效，不受这个弹窗影响。`
    : `等 ${sigRemain} 秒；不点＝不做。同意时若价格朝不利方向跑过 ${
        (lastAutoData && lastAutoData.assist_cfg && lastAutoData.assist_cfg.max_slip_usd) || 0.8
      } 美元，信号自动作废。`;

  // 内容每次重填，滚动位置回顶（最终裁决永远在第一屏），并按实际溢出程度
  // 切换"↓ 向下滚动查看全部"渐隐提示。提示是锦上添花，全程不抛。
  try {
    const sb = $('sigBody');
    sb.scrollTop = 0;
    const wrap = sb.parentElement;
    if (wrap && wrap.classList) {
      const syncFade = () => wrap.classList.toggle('more',
        sb.scrollTop + sb.clientHeight < sb.scrollHeight - 4);
      sb.onscroll = syncFade;            // onscroll 而不是 addListener：反复弹只留一个
      requestAnimationFrame(syncFade);   // 排版完才知道有没有溢出
    }
  } catch (e) { /* 没提示也要能用 */ }

  $('sigMask').classList.remove('hidden');
  $('sigCard').classList.add('flash');
  beepSignal();
  flashTitle(true);
  raiseWindowForSignal();
  // 焦点落在「不做」上——和"超时不点=不做"的默认语义一致，Enter 误触不会开仓。
  maskOpen($('sigMask'), { focus: $('sigNo') });
}

function sigHide() {
  maskClose($('sigMask'));
  $('sigCard').classList.remove('flash');
  flashTitle(false);
  lastSigId = null;
}

function sigTick() {
  if (sigRemain > 0) sigRemain -= 1;
  const el = $('sigClock');
  if (!el) return;
  el.textContent = sigRemain > 0 ? `${sigRemain}s` : '已过期';
  el.className = 'sig-clock' + (sigRemain <= 10 ? ' danger' : (sigRemain <= 30 ? ' warn' : ''));
  const hint = $('sigHint');
  if (hint && !sigBusy) {
    const isExit = $('sigYes').textContent.indexOf('平仓') >= 0;
    hint.textContent = isExit
      ? `等 ${Math.max(0, sigRemain)} 秒；不点＝按模型决定平仓。硬止损/持仓上限期间照常生效。`
      : `等 ${Math.max(0, sigRemain)} 秒；不点＝不做。`;
  }
}

async function sigAnswer(approve) {
  if (sigBusy || !lastSigId) return;
  sigBusy = true;
  const sid = lastSigId;
  $('sigYes').disabled = true;
  $('sigNo').disabled = true;
  try {
    const r = await fetch('/api/autotrade/signal', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: sid, approve: !!approve, confirm: true,
                             account: curLogin || undefined }),
    });
    const j = await r.json();
    if (!j.ok) {
      // 过期/已处理：关掉弹窗并说清原因，别让用户以为点坏了
      atSay('<b>这条信号没能提交：</b>' + esc(j.error || '已失效'), 'err');
      sigHide();
      loadAutoTrade();
      return;
    }
    if (!approve) {
      atSay('<b>已否决</b>，这一轮不进场。', 'ok');
    } else {
      // 点了同意 ≠ 进了场：后台还要过一遍复核（价格漂移 / 叠仓 / 刹车 /
      // 同向冷却），结论最快也要下一次轮询才拿得到。这里先说"复核中"，
      // 同时盯住流水把真结局补上——只写"正在执行"就撒手的话，遇到
      // void_slip（价已跑、按规则作废）操作条会永远停在执行中，看着就是
      // 卡死了。2026-11-06 实盘发生过一次，用户当场就问"是不是没成功"。
      atSay('<b>已同意</b>，正在按当前价复核（漂移 / 叠仓 / 刹车）…', 'ok');
      sigWatchId = sid;
      sigWatchUntil = Date.now() + 25000;
    }
    answeredSigId = sid;
    sigHide();
    // 立刻拉一次：等下一次 1 秒轮询也能看到，但用户点了就想马上有反馈
    loadAutoTrade();
  } catch (e) {
    sigBusy = false;
    $('sigYes').disabled = false;
    $('sigNo').disabled = false;
    atSay('<b>提交失败：</b>' + esc(String(e)), 'err');
  }
}

/* ---- 同意的结局：盯住流水把它落到操作条上 ----
   不另开接口、不额外发请求：面板本来就每秒拉一次 /api/autotrade，
   这里只是在 renderAutoTrade 之后看一眼 summary.recent 里有没有
   signal_id == 这次点同意的那条记录。四种真实结局：
     open / close      —— 成交了（带成交价和单号）
     skip              —— 没进场，reason 就是原因（漂移作废 / 冷却 / 已不可开仓）
     open_failed       —— 发单被券商拒了，reason 是拒单理由
     close_failed      —— 平仓发单失败（线程随后会自己停）
   超时（25 秒）还没对上号，就明说"去流水里看"，不装作还在执行。 */
let sigWatchId = null;
let sigWatchUntil = 0;

function sigVerdictCheck(a) {
  if (!sigWatchId || !a || !a.ok) return;
  const rows = Array.isArray(a.recent) ? a.recent : [];
  let hit = null;
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    // signal 行是"发出信号"本身，不是结局；answered 的 id 也不可能对不上
    if (r && r.signal_id === sigWatchId && r.kind && r.kind !== 'signal') {
      hit = r;
      break;
    }
  }
  if (hit) {
    sigWatchId = null;
    if (hit.kind === 'open') {
      const dir = Number(hit.direction) > 0 ? '做多' : '做空';
      const px = hit.price == null ? '' : ' ' + fmt(hit.price, 3);
      atSay(`<b>已成交：</b>${dir}${px}，单号 ${hit.ticket}。之后由程序看仓、模型决定出场。`, 'ok');
    } else if (hit.kind === 'close') {
      const pnl = Number(hit.pnl);
      const kind = Number.isFinite(pnl) && pnl > 0 ? 'ok' : 'err';
      const pnlTxt = Number.isFinite(pnl) ? `，盈亏 ${pnl.toFixed(2)}` : '';
      atSay(`<b>已平仓</b>${pnlTxt}。`, kind);
    } else if (hit.kind === 'open_failed' || hit.kind === 'close_failed') {
      // 这两种行里 reason 是**模型的原话**，券商拒单的理由在 error 上，
      // 所以这里先取 error——用户关心的是"为什么没成"，不是模型怎么想的。
      atSay(`<b>${hit.kind === 'open_failed' ? '发单被券商拒了' : '平仓没发出去'}：</b>`
            + esc(hit.error || hit.reason || '见下方流水')
            + (hit.kind === 'close_failed' ? '（做单线程已停，持仓还在）' : ''), 'err');
    } else {
      // 后端 reason 本身就写成「已同意，但…」，这里只补一句定性，
      // 不重复"已同意"——否则同一句话会连着出现两遍。
      atSay('<b>这笔没进场：</b>' + esc(hit.reason || hit.error || '见下方流水'), 'err');
    }
    return;
  }
  if (Date.now() > sigWatchUntil) {
    sigWatchId = null;
    atSay('<b>已提交</b>，几秒内没等到结论——直接看下方流水最新一条（开仓 / skip / 失败）。', 'err');
  }
}

function sigPoll(a) {
  const sig = a && a.ok ? a.pending_signal : null;
  if (!sig || !sig.id) {
    if (lastSigId) sigHide();
    return;
  }
  // 已经回答过的 id 不再弹：worker 清掉 pending_signal 有 0.2 秒空窗，
  // 这期间面板可能又轮询到一次，不挡住就是"点了同意、弹窗又闪回来"。
  if (sig.id === answeredSigId) {
    if (lastSigId) sigHide();
    return;
  }
  const remain = Math.max(0, Number(sig.remain_sec) || 0);
  if (remain <= 0) {                 // 服务端已判过期，下一次轮询就会清掉
    if (lastSigId) sigHide();
    return;
  }
  if (sig.id !== lastSigId) {        // 新信号：只在这时响铃/置顶
    lastSigId = sig.id;
    answeredSigId = null;
    sigRemain = remain;
    sigShow(sig);
    if (sigTimer) clearInterval(sigTimer);
    sigTimer = setInterval(sigTick, 1000);
    sigTick();
  } else {
    // 同一信号：只把倒计时对齐服务端（防本地定时器漂移），不重复响铃
    if (Math.abs(remain - sigRemain) > 2) sigRemain = remain;
  }
}

$('sigYes').addEventListener('click', () => sigAnswer(true));
$('sigNo').addEventListener('click', () => sigAnswer(false));
// **不注册** Esc / 遮罩点击关闭：这两个在弹窗语境下等于"否决"，
// 而人按 Esc 的意图通常是"关掉这个挡住我的东西"。

function renderApiMeter(info) {
  if (!info) return;
  $('apiMeter').textContent = info.calls
    ? `已调用 ${info.calls} 次 · ${(info.total_tokens || 0).toLocaleString()} tokens`
    : '尚未调用过 API';
}

/* ---- 实盘视图三态切换（S1 骨架）----
   纯 display 切换：不重绘、不清数据、不发请求。
   各 render 函数继续往隐藏的 DOM 里写，切回来看到的就是最新一轮。
   刻意不动 pollVisible()——三个 pane 都在 #view-live 内，任何一态都该继续轮询。 */
document.querySelectorAll('.live-sub').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.live-sub').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const s = b.dataset.sub;
    // 挂到 body 上：做单台要压紧账户条/内边距（一屏装完），别的态不用
    document.body.dataset.sub = s;
    $('live-desk').classList.toggle('hidden', s !== 'desk');
    $('live-flow').classList.toggle('hidden', s !== 'flow');
    $('live-review').classList.toggle('hidden', s !== 'review');
    $('live-setup').classList.toggle('hidden', s !== 'setup');
    if (s === 'desk' || s === 'flow') fitActive();
  });
});

/* 做单台 / 决策流都是「撑满剩余视口」：卡片高度 = 视口底 − 卡片顶 − 呼吸位。
   上面的 sticky 状态带 / 账户条 / 子标签高度都不是常数（窄窗口会折行），
   所以这个数只能量，不能用 calc() 猜。
   量不出来（隐藏态 getBoundingClientRect 全 0）就直接跳过。

   两条页面的区别只在卡片里装什么：
   - 做单台：决策环 + 持仓面板，at-grid 是 flex:1，多出来的高度由它吸收
     （2026-11-04 决策流搬走之前，这里是 flex:none，被压的永远是决策流）；
   - 决策流：一张表，.stream-body 内部滚，所以量最小高度时要先把它的高度夹掉。
   装不下的时候卡片按「最小可行高度」自然撑开、页面滚——滚动条可以忍，
   把列表压成一条缝不能忍。 */
function fitPane(paneId) {
  const card = document.querySelector('#' + paneId + ' > .card.l1');
  if (!card || !card.offsetParent) return;
  const pane = document.getElementById(paneId);
  const top = card.getBoundingClientRect().top + window.scrollY;
  // 页脚现在挂在设置舱里，做单台 / 决策流态下它是 display:none，
  // getBoundingClientRect 全 0。只有真的占位置时才扣它的高度，
  // 否则每条页面都白扣 12px——而做单台的预算是按像素算的。
  const foot = document.querySelector('#live-setup footer.foot');
  const footH = (foot && foot.offsetParent)
    ? Math.round(foot.getBoundingClientRect().height) + 12 : 0;
  // 22 = body 的 16px 底边距 + 6px 呼吸位。
  const avail = window.innerHeight - top - 22 - footH;
  const sb = card.querySelector('.stream-body');
  const keep = sb ? sb.scrollTop : 0;
  // 先退回宽松版再量：上一轮可能挂过 .tight，带着它量出来的
  // 「最小高度」是紧凑值，窗口拉大之后就再也不会换回宽松排版了。
  pane.classList.remove('tight');
  card.style.height = '';
  card.style.maxHeight = '';
  // 量「最小可行高度」而不是「内容全展开的高度」：决策流 flex:1 且内容在内部滚，
  // 不夹住的话 card.scrollHeight 会把全部历史行都算进去（实测 688 vs 505）。
  // 夹到 0 之后剩下的部分 = 卡片头 + 决策环/持仓面板 + 决策流自己的 min-height。
  // 做单台没有滚动区（sb 为 null），它的最小高度就是 at-grid 的自然高度。
  let minH;
  if (sb) {
    const keepMax = sb.style.maxHeight;
    sb.style.maxHeight = '0px';
    minH = card.scrollHeight;
    sb.style.maxHeight = keepMax;
  } else {
    minH = card.scrollHeight;
  }
  if (avail > minH) {
    // 富余空间交给卡片自己：决策流给 .stream-body（列表变长）。
    // 做单台例外：它只有「环 + 持仓」两块固定内容，撑满视口只会把空白摊进
    // 卡片内部（1600×900 实测：环里一条 96px 空洞，或持仓面板被 space-between
    // 撑散）；完全不撑又会在 1080 高的屏上留下三分之一屏的空白背景。
    // 折中：最多再长 80px（一行呼吸位），其余富余留给页面。
    const capH = paneId === 'live-desk' ? Math.min(avail, minH + 80) : avail;
    card.style.height = capH + 'px';
    card.style.maxHeight = capH + 'px';
    // 自校验：撑满后文档还是比视口高，说明 top 量早了（账户条/标题还没渲染出来），
    // 或者内容确实装不下——退回最小高度让页面滚，绝不把卡片撑到视口外。
    if (document.documentElement.scrollHeight > window.innerHeight + 1) {
      card.style.height = '';
      card.style.maxHeight = minH + 'px';
    }
  } else if (paneId === 'live-desk') {
    // 宽松版装不下：换成紧凑版再量一次。做单台的硬约束是一屏不滚
    // （1120×720 就是窗口下限），而宽松版那 88px 全是拉伸出来的空档，
    // 压掉之后内容一个字不少。紧凑版仍装不下才允许滚。
    pane.classList.add('tight');
    const minTight = card.scrollHeight;
    if (avail > minTight) {
      const capTight = Math.min(avail, minTight + 80);
      card.style.height = capTight + 'px';
      card.style.maxHeight = capTight + 'px';
      if (document.documentElement.scrollHeight > window.innerHeight + 1) {
        card.style.height = '';
        card.style.maxHeight = minTight + 'px';
      }
    } else {
      card.style.maxHeight = minTight + 'px';
    }
  } else {
    // 装不下：不压缩任何东西，按最小可行高度撑开，页面滚。
    card.style.maxHeight = minH + 'px';
  }
  if (sb) sb.scrollTop = keep;
}
/* 做单台和决策流都是「撑满剩余视口」的页面，量法完全一样，区别只在卡片里
   装的是 at-grid 还是 stream。两条页面各自量自己那张卡，隐藏态直接返回。 */
function fitDesk() { fitPane('live-desk'); }
function fitFlow() { fitPane('live-flow'); }
/* 轮询每轮都会重渲决策流，渲完要重新分配富余空间（内容高度会变）。 */
function fitActive() {
  const s = document.body.dataset.sub || 'desk';
  if (s === 'desk') fitDesk();
  else if (s === 'flow') fitFlow();
}
window.addEventListener('resize', fitActive);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitActive);

// 流水过滤开关：勾上立刻重渲染，不用等下一轮轮询
if ($('atShowSkip')) {
  $('atShowSkip').addEventListener('change', () => {
    if (lastAutoData) renderAutoTrade(lastAutoData);
  });
}
// 底部那条「跳过 · 继续持有」折叠带：点一下等于勾上开关。
// 它自己不带 checkbox（checkbox 在表头），所以这里替它改状态再重渲染。
if ($('atSkipShow')) {
  $('atSkipShow').addEventListener('click', () => {
    const cb = $('atShowSkip');
    if (!cb) return;
    cb.checked = !cb.checked;
    if (lastAutoData) renderAutoTrade(lastAutoData);
  });
}

// 子态默认做单台。首帧就要打上——不然账户条会先按舒展版渲染一帧再跳。
document.body.dataset.sub = 'desk';
window.addEventListener('load', fitDesk);
setTimeout(fitDesk, 60);

// 实盘视图：进入页面先加载一次，之后每 20 秒刷新（只在实盘页可见时）
// 刷新分三档，别再整体 20 秒一次——盯着做单的人等不起。
//   1 秒：AI 交易员（只读 state + 日志，6KB，完全不碰 MT5）
//   3 秒：账本/持仓/账户数字（服务端缓存 TTL 也是 3 秒，读一次 MT5 约 33ms）
//  20 秒：账户下拉列表（只在你于终端里切账户时才变）
// 页面不可见时不发请求，免得长期开着白烧 MT5。
function pollVisible() {
  return !document.hidden;
}

refreshLive();
loadAutoTrade();
loadLlm();
updateModeHint();
syncMuteButton();   // 提示音开关的初始文案/样式（静音状态从 localStorage 恢复）
// 启动后查一次要不要弹配置向导。放在 loadLlm 之后：向导要用到供应商列表。
checkSetup();
// 经验库选择器：列表 + 切换事件。切换是原子改 index，下一次决策就生效。
loadLessonPacks();
// 版本号常驻 header。失败就保持隐藏，别让一个展示性元素报错。
loadAppVersion();
loadShellQuit();
// 更新检测：安装版启动时静默查一次，有新版才弹窗；其他形态自动藏按钮
startupUpdateCheck();
(() => {
  const sel = $('lpSelect');
  if (sel) sel.addEventListener('change', lpSelectChanged);
})();

// 教训库：点开才拉（每次打开都重新扫，数据本来就该是最新的）
(() => {
  const lb = $('atLessons');
  if (lb) lb.addEventListener('toggle', () => { if (lb.open) loadLessons(); });
})();

setInterval(() => {
  if (pollVisible()) loadAutoTrade();
}, 1000);

setInterval(() => {
  if (pollVisible()) loadLive();
}, 3000);

setInterval(() => {
  if (pollVisible()) refreshAccounts();
}, 20000);

$('acctPick').addEventListener('change', () => { sessSel = null; loadLive(true); });
$('btnDelAcct').addEventListener('click', deleteAccount);

// 幕布要能出得来：Esc 直接收，退出全屏也自动收。
// 少这一条就成了"打开就关不掉"，只能刷新页面。
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') exitTradingFullscreen();
});
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement) {
    const el = $('atCurtain');
    if (el) el.classList.add('hidden');
  }
});

