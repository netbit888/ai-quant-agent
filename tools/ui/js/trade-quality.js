/* ==================== 近期交易质量 ==================== */
let qualData = null;
let qualKey = '20n';   // 默认看近 20 笔：时间窗在低频时样本太少

const VC_TEXT = {
  better: '变好', worse: '变差', flat: '持平',
  low_sample: '样本不足', no_baseline: '无前序窗口', no_data: '无交易'
};

function renderQuality(q) {
  qualData = q;
  const tabs = $('winTabs');
  if (!q || q.empty || !(q.windows || []).length) {
    tabs.innerHTML = '';
    $('qualWarn').innerHTML = '<div class="warn-strip">还没有已平仓交易，无法统计近期质量。</div>';
    $('qualKpis').innerHTML = '';
    $('qualCmp').innerHTML = '';
    $('qualProc').innerHTML = '';
    $('rollNote').textContent = '';
    drawArea('chartRolling', [], 'line-info', '#4a9eff', { W: 1240, H: 150 });
    return;
  }

  if (!q.windows.some(w => w.key === qualKey)) qualKey = q.windows[0].key;

  tabs.innerHTML = q.windows.map(w => `
    <button class="win-tab ${w.key === qualKey ? 'active' : ''}" data-wk="${w.key}">
      ${w.label}<span class="cnt">${w.current.n}</span>
    </button>`).join('');
  tabs.querySelectorAll('.win-tab').forEach(b => {
    b.addEventListener('click', () => { qualKey = b.dataset.wk; renderQuality(qualData); });
  });

  const w = q.windows.find(x => x.key === qualKey) || q.windows[0];
  const c = w.current, p = w.previous, d = w.delta || {};

  // 样本护栏：这是整个模块最重要的一行
  if (c.n === 0) {
    $('qualWarn').innerHTML = `<div class="warn-strip">该窗口内没有平仓交易。</div>`;
  } else if (c.low_sample) {
    $('qualWarn').innerHTML = `<div class="warn-strip">
      <span>⚠</span><span><b>样本不足：只有 ${c.n} 笔（门槛 ${q.min_sample} 笔）。</b>
      胜率 95% 置信区间是 [${c.win_ci[0]}%, ${c.win_ci[1]}%]，这个宽度说明什么都判断不了。
      不要据此改策略或加减仓。</span></div>`;
  } else if (c.expectancy_r < 0) {
    $('qualWarn').innerHTML = `<div class="warn-strip">
      <span>⚠</span><span><b>该窗口期望为负（${fmt(c.expectancy_r, 3)} R）。</b>
      样本 ${c.n} 笔足够，说明这一段确实在亏，不是运气。</span></div>`;
  } else {
    $('qualWarn').innerHTML = '';
  }

  const chip = (v) => `<span class="verdict-chip vc-${v}">${VC_TEXT[v] || v}</span>`;
  const sign = v => v == null ? '' : (v > 0 ? 'delta-up' : v < 0 ? 'delta-down' : '');

  $('qualKpis').innerHTML = `
    <div class="qcell"><i>期望 R / 笔</i>
      <b class="${c.expectancy_r >= 0 ? 'pos' : 'neg'}">${fmt(c.expectancy_r, 3)}</b>
      <s>${money(c.expectancy)} / 笔 · ${chip(w.verdict)}</s></div>
    <div class="qcell"><i>盈利因子</i>
      <b class="${c.pf != null && c.pf < 1 ? 'neg' : ''}">${c.pf != null ? fmt(c.pf) : '—'}</b>
      <s>${d.pf != null ? '前序 ' + fmt(p.pf) + '（' + (d.pf > 0 ? '+' : '') + fmt(d.pf) + '）' : '无前序'}</s></div>
    <div class="qcell"><i>胜率</i>
      <b>${fmt(c.win_rate, 1)}%</b>
      <s>95% CI [${c.win_ci[0]}%, ${c.win_ci[1]}%]${c.flats ? ` · 含 ${c.flats} 平` : ''}</s></div>
    <div class="qcell"><i>盈亏比</i>
      <b>${c.rr != null ? fmt(c.rr) : '—'}</b>
      <s>均盈 ${fmt(c.avg_win)} / 均亏 ${fmt(c.avg_loss)}</s></div>
    <div class="qcell"><i>窗口内回撤</i>
      <b class="neut">${fmt(c.dd_r, 2)} R</b>
      <s>约 ${fmt(c.dd_usd)} USD</s></div>
    <div class="qcell"><i>样本 / 天数</i>
      <b>${c.n}</b>
      <s>${c.active_days} 个交易日 · ${c.trades_per_day} 笔/天</s></div>
  `;

  const row = (name, a, b, dv, fmtFn, suffix = '') => `
    <tr>
      <td>${name}</td>
      <td class="mono">${a == null ? '—' : fmtFn(a) + suffix}</td>
      <td class="mono muted">${b == null || !p.n ? '—' : fmtFn(b) + suffix}</td>
      <td class="mono ${sign(dv)}">${dv == null ? '—' : (dv > 0 ? '+' : '') + fmtFn(dv)}</td>
    </tr>`;

  $('qualCmp').innerHTML = p.n ? [
    row('期望 R', c.expectancy_r, p.expectancy_r, d.expectancy_r, v => fmt(v, 3)),
    row('盈利因子', c.pf, p.pf, d.pf, v => fmt(v)),
    row('胜率', c.win_rate, p.win_rate, d.win_rate, v => fmt(v, 1), '%'),
    row('回撤 R', c.dd_r, p.dd_r, d.dd_r, v => fmt(v, 2)),
  ].join('') : '<tr><td colspan="4" class="empty">该窗口之前没有等长样本可对比</td></tr>';

  const proc = (name, val, note) => `<tr><td>${name}</td><td class="mono">${val}</td><td class="mini">${note}</td></tr>`;
  // 过程指标：说明列只讲这个窗口里真实发生过的事。
  // 样本不足（n < min_sample）时整列退化为「样本不足」——n=9 时止损率 44.4%
  // 的 Wilson 区间是 [18.9%, 73.3%]，点估计连方向都可能是反的，
  // 这时候写「止损占比高说明入场偏远」不是诊断，是编故事。
  const lowS = !!c.low_sample;
  const samp = `样本不足 ${c.n}/${q.min_sample} 笔，此列无数值意义`;
  const rn = c.reason_net || {};
  const reason = k => rn[k] || { trades: 0, net: 0.0 };
  const earners = Object.keys(rn).filter(k => rn[k].net > 0);
  const ci = pair => `CI [${fmt(pair[0], 1)}, ${fmt(pair[1], 1)}]%`;
  const cost = c.cost || {};
  const freq = c.freq || {};
  const onlyEarner = k => (earners.length === 1 && earners[0] === k) ? ' · 窗口内唯一赚钱的离场' : '';
  $('qualProc').innerHTML = [
    proc('手动平仓', fmt(c.manual_pct, 1) + '%', lowS ? samp
      : `${ci(c.manual_ci)} · ${reason('manual').trades} 笔 ${money(reason('manual').net)}${onlyEarner('manual')}`),
    proc('止损离场', fmt(c.sl_pct, 1) + '%', lowS ? samp
      : `${ci(c.sl_ci)} · ${reason('sl').trades} 笔 ${money(reason('sl').net)}${onlyEarner('sl')}`),
    proc('止盈离场', fmt(c.tp_pct, 1) + '%', lowS ? samp
      : `${ci(c.tp_ci)} · ${reason('tp').trades} 笔 ${money(reason('tp').net)}${onlyEarner('tp')}`),
    proc('持仓中位数', mins(c.hold_median_min), lowS ? samp
      : cost.spread_unknown
        ? `佣金/库存费 ${fmt(cost.fee_usd)} USD；无实测点差数据，点差成本未计入`
        : `点差 ${fmt(cost.spread_usd)} + 费用 ${fmt(cost.fee_usd)} USD，占毛盈亏 ${fmt(cost.cost_pct, 1)}%`),
    proc('交易频率', fmt(c.trades_per_day, 1) + ' 笔/天', lowS ? samp
      : freq.days >= 4
        ? `高频日均 ${money(freq.hi_mean)} vs 低频日均 ${money(freq.lo_mean)}（${freq.days} 个交易日）`
          + (freq.corr != null ? ` · 日笔数与当日盈亏相关 ${Number(freq.corr).toFixed(2)}` : ' · 日笔数无波动，相关测不出')
        : `${freq.days} 个交易日，分不出高低频`),
  ].join('');

  // 滚动期望曲线：近 20 笔主线（语义色+渐变面积）+ 近 100 笔对照线（灰蓝虚线，低权重）。
  // series 顺序 = 绘制顺序：对照线先画（垫底），主线和面积压在上面。
  // 按平仓时间对齐 X 轴——两条线点数不同、终点都是最近一笔，按索引映射日期会错位。
  const rollRaw = q.rolling || [];
  const roll = rollRaw.map(x => x.r);
  const rollTs = rollRaw.map(x => x.ts);
  // 旧后端/旧缓存可能没有 rolling100：兜底为空 = 自动退化为单线图，不报错
  const roll100Raw = q.rolling100 || [];
  const roll100 = roll100Raw.map(x => x.r);
  const roll100Ts = roll100Raw.map(x => x.ts);
  const rollUp = roll.length && roll[roll.length - 1] >= 0;
  const mainCls = rollUp ? 'line-up' : 'line-down';
  const mainColor = rollUp ? '#2ecc71' : '#ff4d4f';
  drawArea('chartRolling', roll, mainCls, mainColor, {
    W: 1240, H: 150, ts: rollTs,
    series: [
      ...(roll100.length ? [{ values: roll100, colorClass: 'line-ghost', color: '#7f93a8', area: false, ts: roll100Ts }] : []),
      { values: roll, colorClass: mainCls, color: mainColor, area: true, primary: true, ts: rollTs },
    ],
    hover: i => [
      `${fmtTsMin(rollTs[i])} · 窗口 ${rollRaw[i].n} 笔`,
      `近20笔 ${roll[i] >= 0 ? '+' : ''}${fmt(roll[i], 3)} R`,
      roll100[i] != null ? `近100笔 ${roll100[i] >= 0 ? '+' : ''}${fmt(roll100[i], 3)} R` : '近100笔 无数据',
    ],
  });
  $('rollNote').textContent = roll.length
    ? `近20笔 最新 ${fmt(roll[roll.length - 1], 3)} R（区间 ${fmt(Math.min(...roll), 2)} ~ ${fmt(Math.max(...roll), 2)}）`
      + (roll100.length ? ` · 近100笔 最新 ${fmt(roll100[roll100.length - 1], 3)} R` : '')
    : `交易数不足 20 笔，无法计算滚动期望`;
}

let aiBusy = false;       // 请求飞行中：任何入口都点不动，防连点重复扣费
let adviceState = null;   // 最近一次复盘结果，按钮的文案/可用性靠它判断

function syncAiButton() {
  const btn = $('btnAi');
  if (!btn) return;
  if (aiBusy) {
    btn.disabled = true;
    btn.textContent = '分析中…';
    return;
  }
  if (liveData && liveData.offline) {
    btn.disabled = true;
    btn.textContent = '归档账户不支持';
    btn.title = '归档账户已不在 MT5 终端里，生成的复盘无法验证';
    return;
  }
  if (liveData && liveData.stats && liveData.stats.empty) {
    btn.disabled = true;
    btn.textContent = '无可复盘交易';
    btn.title = '这个账户还没有已平仓交易，没有可分析的东西';
    return;
  }
  const has = !!(adviceState && adviceState.advice);
  btn.disabled = false;
  btn.textContent = has ? '重新生成' : '生成复盘';
  btn.title = has
    ? '忽略缓存重新调用 DeepSeek，会产生一笔新费用'
    : '调用 DeepSeek 生成复盘，会产生费用（点击后需确认）';
}

function renderAdvice(box) {
  const el = $('adviceBox');
  adviceState = box || null;
  syncAiButton();

  if (!box || !box.advice) {
    // 没确认过就绝不自己调 API：这里只放说明，按钮统一收在标题栏，避免两处重复
    if (box && box.need_confirm) {
      el.innerHTML = `
        <div class="note-box">
          <b>尚未生成复盘。</b>点右上角「生成复盘」开始。
          生成需要把账户统计发给 DeepSeek API，会产生费用，点击后会先弹窗让你确认。
        </div>`;
      return;
    }
    const err = box && box.error ? `<div class="mini">AI 复盘不可用：${box.error}</div>` : '';
    el.innerHTML = err || '<div class="mini">暂无 AI 复盘</div>';
    return;
  }
  const a = box.advice;
  const v = String(a.verdict || '');
  const vcls = /严重|亏损/.test(v) ? 'bad' : /打平/.test(v) ? 'warn' : 'good';
  // 补写了进场理由后，旧复盘是「过期」而不是「没有」——别把上一份结论藏起来
  const badge = box.stale ? '已过期' : (box.cached ? '已缓存' : '新生成');
  const when = box.generated_at ? new Date(box.generated_at * 1000).toLocaleString('zh-CN') : '';

  const problems = (a.problems || []).map(p => `
    <div class="prob">
      <div class="ph"><span class="sev ${p.severity || '中'}">${p.severity || '中'}</span><span class="pt">${p.title || ''}</span></div>
      <dl>
        <dt>证据</dt><dd>${p.evidence || '—'}</dd>
        <dt>成因</dt><dd>${p.root_cause || '—'}</dd>
      </dl>
    </div>`).join('');

  const actions = (a.actions || [])
    .slice().sort((x, y) => (x.priority || 99) - (y.priority || 99))
    .map(x => `
    <div class="act">
      <div class="n">${x.priority || '·'}</div>
      <div class="body">
        <div class="a">${x.action || ''}</div>
        <div class="w">${x.why || ''}</div>
        <div class="v"><b>验证：</b>${x.how_to_verify || '—'}</div>
      </div>
    </div>`).join('');

  el.innerHTML = `
    ${box.stale ? `<div class="note-box" style="border-color:rgba(240,180,41,.35)">
      <b>这份复盘已过期。</b>你补写或修改了进场理由，旧结论没把这些理由算进去；
      点「生成复盘」会按新输入重算。</div>` : ''}
    <div class="advice-head">
      <div class="hl">${a.headline || '—'}</div>
      <span class="verdict ${vcls}">${v || '—'}</span>
    </div>
    <div class="mini" style="margin-bottom:10px">${badge}${when ? ' · ' + when : ''}</div>
    ${problems ? `<h3 style="font-size:var(--fs-2);color:var(--muted);margin:12px 0 8px;font-weight:600">问题</h3>${problems}` : ''}
    ${actions ? `<h3 style="font-size:var(--fs-2);color:var(--muted);margin:16px 0 8px;font-weight:600">建议动作</h3>${actions}` : ''}
    ${a.note ? `<div class="note-box" style="margin-top:12px"><b>样本说明：</b>${a.note}</div>` : ''}
  `;
}

async function refreshAi() {
  if (aiBusy) return;                       // 上一笔还没回来，别开第二个请求
  // 归档账户连不上 MT5，生成了也没法验证，纯烧钱
  if (liveData && liveData.offline) {
    renderAdvice({ ok: false, error: '归档账户不支持生成新的 AI 复盘（它已不在终端里，无法验证）' });
    return;
  }

  const has = !!(adviceState && adviceState.advice);
  const ok = await confirmApiCall({
    title: has ? '确认重新生成 AI 复盘？' : '确认生成 AI 复盘？',
    desc: '将把当前账户统计发送给 DeepSeek API 并请求一份复盘报告。',
    cost: has
      ? '这次会忽略已有缓存、重新调用一次，产生一笔新费用（约几千 token）。'
      : '这会产生 API 费用（约几千 token）。只有你点确认之后才会真正发起请求。',
    okText: has ? '重新生成' : '确认生成',
  });
  if (!ok) return;

  aiBusy = true;
  syncAiButton();
  try {
    const r = await fetch('/api/advice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, force: true }),
    });
    const j = await r.json();
    if (j.ok && j.advice) {
      // 记住新结论，后续自动刷新时按钮保持「重新生成」
      if (liveData) liveData.advice = j.advice;
      renderAdvice(j.advice);
    } else {
      renderAdvice({ ok: false, error: j.error || '生成失败' });
    }
    if (j.api_calls) renderApiMeter(j.api_calls);
  } catch (e) {
    renderAdvice({ ok: false, error: String(e) });
  } finally {
    aiBusy = false;
    syncAiButton();                         // renderAdvice 已更新 adviceState，这里只重算按钮
  }
}

/* ---------------- 弹窗焦点管理 ----------------
   为什么要补这个：弹窗只是普通 div 盖着页面，Tab 顺序会落到遮罩背后的
   页面元素上；信号弹窗又不响应 Esc，键盘用户会陷入"焦点在弹窗后面、
   弹窗又关不掉"的状态。约定：
     maskOpen  = 打开 + 记录来源焦点 + 把焦点移进弹窗（默认安全的那个按钮）
     maskClose = 关闭 + 还原来源焦点
   Tab 在弹窗内循环（栈顶弹窗生效）；Esc 语义各弹窗维持原样，不在这里统一。 */
const modalStack = [];
function focusablesIn(root) {
  if (!root || !root.querySelectorAll) return [];
  return Array.from(root.querySelectorAll('button, input, select, textarea, a[href], [tabindex]'))
    .filter(el => el && !el.disabled && !el.classList.contains('hidden'));
}
function maskOpen(mask, opts) {
  if (!mask) return;
  mask.classList.remove('hidden');
  modalStack.push({ mask, prev: document.activeElement });
  const dlg = (mask.querySelector && mask.querySelector('[role="dialog"]')) || mask;
  const target = (opts && opts.focus) || (dlg.querySelector && dlg.querySelector('[autofocus]'))
    || focusablesIn(dlg)[0];
  if (target && target.focus) setTimeout(() => { try { target.focus(); } catch (e) {} }, 30);
}
function maskClose(mask) {
  if (!mask) return;
  mask.classList.add('hidden');
  const i = modalStack.findIndex(m => m.mask === mask);
  if (i >= 0) {
    const prev = modalStack.splice(i, 1)[0].prev;
    if (prev && prev.focus && (!document.contains || document.contains(prev))) {
      try { prev.focus(); } catch (e) {}
    }
  }
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Tab' || !modalStack.length) return;
  const top = modalStack[modalStack.length - 1].mask;
  const dlg = (top.querySelector && top.querySelector('[role="dialog"]')) || top;
  const f = focusablesIn(dlg);
  if (!f.length) { e.preventDefault(); return; }
  const first = f[0], last = f[f.length - 1];
  const cur = document.activeElement;
  if (e.shiftKey && (cur === first || !dlg.contains || !dlg.contains(cur))) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && cur === last) {
    e.preventDefault(); first.focus();
  }
});

/* ---------------- 付费 API 确认弹窗 ---------------- */
let modalResolve = null;

function confirmApiCall(opts) {
  return new Promise(resolve => {
    modalResolve = resolve;
    $('modalTitle').textContent = opts.title || '确认调用付费 API？';
    $('modalDesc').textContent = opts.desc || '';
    $('modalCost').textContent = opts.cost || '';
    $('modalOk').textContent = opts.okText || '继续';
    $('modalOk').disabled = false;
    $('modalCancel').disabled = false;
    // 焦点落在「取消」上：Enter 误触的代价最小（确认类弹窗的统一约定）
    maskOpen($('modalMask'), { focus: $('modalCancel') });
  });
}

function closeModal(result) {
  maskClose($('modalMask'));
  const fn = modalResolve;
  modalResolve = null;
  if (fn) fn(result);
}

$('modalOk').addEventListener('click', () => closeModal(true));
$('modalCancel').addEventListener('click', () => closeModal(false));
$('modalMask').addEventListener('click', e => {
  if (e.target === $('modalMask')) closeModal(false);   // 点遮罩等于取消
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('modalMask').classList.contains('hidden')) closeModal(false);
});

