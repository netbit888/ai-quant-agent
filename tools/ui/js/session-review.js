/* ==================== 行为标记累计 ====================
 * 单场的标记只是「今天手痒了一次」，跨场累计才能回答
 * 「这个习惯一共让我亏了多少」——手动交易者唯一能直接改的东西。
 */
function renderMarks(d) {
  const rows = d.mark_summary || [];
  $('rowsMark').innerHTML = rows.length ? rows.map(m => `
    <tr>
      <td>${esc(m.label)}<div class="mini">${esc(m.hint || '')}</div></td>
      <td class="mono">${m.count} ${m.unit || '笔'}</td>
      <td class="mono ${m.net >= 0 ? 'pos' : 'neg'}">${money(m.net)}</td>
      <td class="mono">${m.win_rate == null ? '—' : fmt(m.win_rate, 1) + '%'}</td>
      <td class="mono">${m.sessions} 场</td>
    </tr>`).join('') : '<tr><td colspan="5" class="empty">还没有触发任何行为标记</td></tr>';
  if (!rows.length) { $('markNote').textContent = ''; return; }
  const total = rows.reduce((a, m) => a + m.count, 0);
  const worst = rows.filter(m => m.net < 0).sort((a, b) => a.net - b.net)[0];
  $('markNote').textContent = `${total} 次标记`
    + (worst ? ` · 最亏的是「${worst.label}」${money(worst.net)}` : '');
}

/* 逐笔进场理由：失焦/回车即保存，不打断看盘。
 * 它是 AI 复盘唯一能看到「动机」的输入——只有价格和盈亏，
 * 模型只能说「减少交易频率」这类正确但没用的话。
 */
async function saveTradeNote(el) {
  const pid = el.dataset.notePid;
  const note = (el.value || '').trim();
  el.classList.remove('saved', 'failed');
  try {
    const r = await fetch('/api/trade_note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ position_id: pid, note, account: curLogin || undefined }),
    });
    const j = await r.json();
    if (j.ok) {
      el.value = j.note || '';
      el.classList.add('saved');
      setTimeout(() => el.classList.remove('saved'), 1200);
      // 理由变了，上一份复盘就过期了（后端缓存键含理由签名）
      const btn = $('btnAi');
      if (btn) btn.textContent = '生成复盘';
    } else {
      el.classList.add('failed');
    }
  } catch (e) {
    el.classList.add('failed');
  }
}

/* ==================== 本场复盘 ====================
   切场口径：上一笔平仓 → 下一笔开仓 超过 30 分钟断场（后端 live/sessions.py）。
   场次只用来「读」和「分析」，不用来算胜率——单笔场次很常见。 */
const SESS_TAGS = ['计划内', '手痒', '状态差'];
let sessSel = null;

/* 备注草稿
   --------
   面板每 20 秒自动刷新一次，renderSessions 会用「已保存的备注」重建 textarea。
   正在写、还没点保存的文字会被直接冲掉（这个问题真实发生过）。
   所以：输入即记草稿（内存 + localStorage），刷新时优先用草稿，保存成功才清掉。
   另外，textarea 处于焦点时**完全不重建**本场内容——否则中文输入法的
   候选状态（拼音还没上屏）会跟着 DOM 一起没掉。 */
const noteDrafts = {};
let draftStoreKey = null;

function loadDrafts(login) {
  const key = 'aqa.notes.' + (login || 'anon');
  if (key === draftStoreKey) return;
  draftStoreKey = key;
  Object.keys(noteDrafts).forEach(k => delete noteDrafts[k]);
  try {
    const raw = localStorage.getItem(key);
    if (raw) Object.assign(noteDrafts, JSON.parse(raw) || {});
  } catch (e) { /* 隐私模式 / 配额满：草稿只在内存里，不影响使用 */ }
}

function persistDrafts() {
  if (!draftStoreKey) return;
  try { localStorage.setItem(draftStoreKey, JSON.stringify(noteDrafts)); }
  // 不静默：localStorage 写不进去（隐私模式 / 配额满 / profile 被清）时
  // 用户只会觉得"我写的东西怎么没了"，留一行痕迹至少能查。
  catch (e) { if (typeof console !== 'undefined' && console.warn) console.warn('[drafts] 落盘失败：', e && e.message); }
}

function setDraft(sid, value, tags) {
  noteDrafts[sid] = { value: value, tags: tags };
  persistDrafts();
}

function clearDraft(sid) {
  delete noteDrafts[sid];
  persistDrafts();
}

function isEditingNote() {
  const el = document.activeElement;
  return !!(el && el.id === 'sessNote');
}

function captureCaret() {
  const ta = $('sessNote');
  if (!ta) return null;
  return {
    sid: sessSel,
    focused: document.activeElement === ta,
    start: ta.selectionStart,
    end: ta.selectionEnd,
  };
}

function restoreCaret(snap) {
  if (!snap || snap.sid !== sessSel || !snap.focused) return;
  const ta = $('sessNote');
  if (!ta) return;
  ta.focus();
  try { ta.setSelectionRange(snap.start, snap.end); } catch (e) { /* 忽略 */ }
}

const FLAG_CN = {
  revenge: '报复', itchy: '手痒', wide_stop: '宽止损',
  oversize: '超仓', one_bad_trade: '一笔定输赢',
};

function sessionAdviceOf(d, sid) {
  const x = (d.session_advice || {})[sid];
  return (x && x.advice) ? x : null;
}

function renderSessions(d, force = false) {
  const tabs = $('sessTabs');
  const body = $('sessBody');
  const sessions = d.sessions || [];
  loadDrafts(d.login);

  if (!sessions.length) {
    tabs.innerHTML = '';
    body.innerHTML = `<div class="mini">${d.sessions_error
      ? '场次切分失败：' + d.sessions_error
      : '还没有已平仓交易。打一场之后，这里会出现「本场复盘」。'}</div>`;
    return;
  }
  if (!sessSel || !sessions.some(s => s.id === sessSel)) sessSel = sessions[0].id;

  tabs.innerHTML = sessions.slice(0, 6).map(s => {
    const st = s.stats || {};
    const cls = s.id === sessSel ? 'win-tab active' : 'win-tab';
    const t = new Date(s.start * 1000);
    const hh = String(t.getHours()).padStart(2, '0');
    const mm = String(t.getMinutes()).padStart(2, '0');
    const dot = noteDrafts[s.id] ? '<span class="cnt" style="color:var(--warn)">•</span>' : '';
    return `<button class="${cls}" data-sid="${s.id}">${s.date} ${hh}:${mm}${dot}<span class="cnt">${st.n}</span></button>`;
  }).join('');
  tabs.querySelectorAll('.win-tab').forEach(b => {
    b.addEventListener('click', () => {
      sessSel = b.dataset.sid;
      renderSessions(d, true);   // 切场必须重建，不管焦点在哪
    });
  });

  // 正在写备注就别动 DOM：会让输入法候选词丢字
  if (!force && isEditingNote()) return;

  const s = sessions.find(x => x.id === sessSel);
  const snap = captureCaret();
  body.innerHTML = sessionHtml(d, s);
  bindSession(d);
  restoreCaret(snap);
}

function sessionHtml(d, s) {
  const st = s.stats || {};
  const net = st.net || 0;
  const note = (d.session_notes || {})[s.id] || {};
  const draft = noteDrafts[s.id];       // 有未保存的草稿时，草稿优先
  const noteVal = draft ? (draft.value || '') : (note.note || '');
  const tags = draft ? (draft.tags || []) : (note.tags || []);
  const flags = s.flags || [];
  const ai = sessionAdviceOf(d, s.id);

  const kpi = (k, v, sub) =>
    `<div class="sess-kpi"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;

  const riskSub = st.median_risk_pct != null ? `中位 ${fmt(st.median_risk_pct, 1)}%` : '需要止损记录';
  const slSub = (st.sl_points || []).length ? st.sl_points.join(' / ') + ' 点' : '无止损记录';

  const tradeRows = (s.trades || []).slice().reverse().map(t => {
    const marks = flags.filter(f => String(f.pid) === String(t.position_id))
      .map(f => `<span class="mini" style="color:var(--warn)" title="${esc(f.title)}">${esc(FLAG_CN[f.code] || f.code)}</span>`)
      .join(' ');
    return `<tr>
      <td class="mono">${ts2str(t.entry_time)}</td>
      <td class="${t.direction > 0 ? 'pos-long' : 'pos-short'}">${t.direction > 0 ? '多' : '空'}</td>
      <td class="mono"${t.partial ? ` title="部分平仓：已平 ${t.volume_closed} / 开仓 ${t.volume}"` : ''}>${t.partial ? `${t.volume_closed}/${t.volume}` : t.volume}</td>
      <td class="mono ${signClass(t.profit)}">${money(t.profit)}</td>
      <td class="mono">${t.r != null ? fmt(t.r, 2) + 'R' : '—'}</td>
      <td class="mono">${mins(t.hold_min)}</td>
      <td class="mono">${t.risk_pct != null ? fmt(t.risk_pct, 1) + '%' : '—'}</td>
      <td class="mono">${t.sl_points ? t.sl_points + ' pt' : '—'}</td>
      <td class="mini">${reasonCn(t.close_reason)}</td>
      <td>${marks || '<span class="mini">—</span>'}</td>
    </tr>`;
  }).join('');

  const sessFlags = flags.filter(f => !f.pid);
  const flagHtml = sessFlags.map(f => `
    <div class="flag ${f.level}"><div>
      <div class="t">${esc(f.title)}</div><div class="d">${esc(f.detail)}</div>
    </div></div>`).join('');

  // 下一场规则：只在本场处于规则之后时才判定
  let ruleHtml = '';
  const rc = d.rule_check;
  if (d.rule) {
    const applies = rc && rc.session_id === s.id;
    const cls = applies ? (rc.status === 'pass' ? 'pass' : rc.status === 'fail' ? 'fail' : '') : '';
    const verdict = applies
      ? (rc.status === 'pass' ? `✅ 遵守（${rc.label} = ${rc.value}，目标 ${rc.target}）`
        : rc.status === 'fail' ? `❌ 违反（${rc.label} = ${rc.value}，目标 ${rc.target}）`
        : '无法判定')
      : '';
    ruleHtml = `<div class="sess-rule ${cls}">
      <div class="t">下一场规则</div>
      <div>${esc(d.rule.text) || '—'}</div>
      ${verdict ? `<div class="mini" style="margin-top:6px">${esc(verdict)}</div>` : ''}
    </div>`;
  }

  const aiHtml = ai
    ? `<div class="sess-ai">
         <div class="hl">${esc(ai.advice.headline) || '—'}</div>
         ${(ai.advice.evidence || []).map(e => `<div class="ev"><b>${esc(e.claim)}</b>
           <span class="num">${esc(e.numbers)}</span></div>`).join('')}
         <div class="mini" style="margin-top:9px">生成于 ${new Date(ai.generated_at * 1000).toLocaleString('zh-CN')}</div>
       </div>`
    : '';

  const aiBtn = d.offline ? '' :
    `<button class="btn-ghost" id="btnSessAi">${ai ? '重新分析本场' : 'AI 分析本场'}</button>`;

  return `
    <div class="sess-top">
      <div class="big ${signClass(net)}">${money(net)}</div>
      <div class="sub2">
        第 ${s.index} 场 · ${ts2str(s.start)} → ${ts2str(s.end)} · 历时 ${fmt(s.span_min, 0)} 分钟<br>
        ${st.n} 笔（多 ${st.long} / 空 ${st.short}）· ${st.wins} 胜 ${st.losses} 负
        ${st.n >= 5 && st.rr ? ` · 盈亏比 ${fmt(st.rr)} → 需要胜率 ${fmt(st.need_win_rate, 1)}%，边际 ${fmt(st.margin_pp, 1)} pp` : ''}
      </div>
    </div>

    <div class="sess-kpis">
      ${kpi('笔数', st.n, st.n < 5 ? '样本太少，只看过程' : '')}
      ${kpi('持仓中位', mins(st.median_hold_min), '最长 ' + mins(st.max_hold_min))}
      ${kpi('最大单笔风险', st.max_risk_pct != null ? fmt(st.max_risk_pct, 1) + '%' : '—', riskSub)}
      ${kpi('止损宽度', st.sl_width_ratio ? fmt(st.sl_width_ratio, 2) + '×' : '—', slSub)}
      ${kpi('手动平仓', st.n ? fmt(st.manual / st.n * 100, 0) + '%' : '—', `${st.manual} / ${st.n} 笔`)}
    </div>

    ${flagHtml}

    <div style="margin-top:14px">
      <table>
        <thead><tr><th>开仓</th><th>方向</th><th>手数</th><th>盈亏</th><th>R</th>
          <th>持仓</th><th>风险</th><th>止损</th><th>离场</th><th>标记</th></tr></thead>
        <tbody>${tradeRows || '<tr><td colspan="10" class="empty">无交易</td></tr>'}</tbody>
      </table>
    </div>

    <div class="sess-note">
      <textarea id="sessNote" placeholder="这一场一句话：当时在干什么 / 为什么开始 / 为什么停（10 秒，可跳过）">${esc(noteVal)}</textarea>
      <div class="sess-tags">
        ${SESS_TAGS.map(t => `<button class="sess-tag ${tags.includes(t) ? 'on' : ''}" data-tag="${t}">${t}</button>`).join('')}
      </div>
    </div>
    <div class="sess-foot">
      <button class="btn-ghost" id="btnSessSave">保存备注</button>
      ${aiBtn}
      <span class="mini" id="sessSaveHint">${draft
        ? '未保存的修改'
        : (note.updated_at ? '上次保存 ' + ts2str(note.updated_at) : '')}</span>
    </div>

    ${aiHtml}
    ${ruleHtml}
  `;
}

function bindSession(d) {
  const ta = $('sessNote');
  const tagNames = () => Array.from(document.querySelectorAll('.sess-tag.on')).map(b => b.dataset.tag);
  const markDirty = () => {
    setDraft(sessSel, ta ? ta.value : '', tagNames());
    const hint = $('sessSaveHint');
    if (hint) hint.textContent = '未保存';
  };

  // 边打边存草稿：这样 20 秒刷新、切场、甚至刷新页面都不会把没写完的字弄丢
  if (ta) ta.addEventListener('input', markDirty);
  document.querySelectorAll('.sess-tag').forEach(b => {
    b.addEventListener('click', () => { b.classList.toggle('on'); markDirty(); });
  });

  const save = $('btnSessSave');
  if (save) save.addEventListener('click', () => saveSessionNote(d));
  const ai = $('btnSessAi');
  if (ai) ai.addEventListener('click', () => genSessionAi(d));
}

async function saveSessionNote(d) {
  const ta = $('sessNote');
  if (!ta) return;
  const tags = [...document.querySelectorAll('.sess-tag.on')].map(b => b.dataset.tag);
  const payload = { session_id: sessSel, note: ta.value, tags };
  if (d.offline) payload.account = d.login;
  try {
    const r = await fetch('/api/session_note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) { $('sessSaveHint').textContent = j.error || '保存失败'; return; }
    d.session_notes = j.session_notes || {};
    clearDraft(sessSel);                  // 存住了才丢草稿
    const hint = $('sessSaveHint');
    if (hint) hint.textContent = '已保存 ' + new Date().toLocaleTimeString('zh-CN');
  } catch (e) {
    $('sessSaveHint').textContent = String(e);
  }
}

async function genSessionAi(d) {
  if (d.offline) return;
  const s = (d.sessions || []).find(x => x.id === sessSel);
  if (!s) return;
  const has = !!sessionAdviceOf(d, sessSel);
  const ok = await confirmApiCall({
    title: has ? '确认重新分析这一场？' : '确认分析这一场？',
    desc: `把本场 ${s.stats.n} 笔的切片数据（方向、持仓时长、止损宽度、单笔风险、离场方式）发给 DeepSeek。`,
    cost: '会产生 API 费用（约一两千 token）。只有你点确认之后才会真正发起请求。',
    okText: has ? '重新分析' : '确认分析',
  });
  if (!ok) return;
  try {
    const r = await fetch('/api/session_advice', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessSel, confirm: true, force: true }),
    });
    const j = await r.json();
    const parsed = j.advice && j.advice.advice;
    if (j.ok && parsed) {
      d.session_advice = d.session_advice || {};
      d.session_advice[sessSel] = { advice: parsed, generated_at: j.advice.generated_at };
      if (j.rule) d.rule = j.rule;
      if (j.api_calls) renderApiMeter(j.api_calls);
      renderSessions(d);
    } else {
      qaToast(j.error || '生成失败', 'err');
    }
  } catch (e) {
    qaToast(String(e), 'err');
  }
}

/* ---------------- 删除归档账户 ---------------- */
function syncDeleteButton(d) {
  const btn = $('btnDelAcct');
  const acc = (acctList || []).find(a => String(a.login) === String(d.login));
  const can = !!(acc && !acc.current);
  btn.classList.toggle('hidden', !can);
  btn.title = can ? '把本地账本移到 data/live/_trash/，不是真删除' : '当前登录的账户不能删除';
}

async function deleteAccount() {
  const login = $('acctPick').value;
  const acc = (acctList || []).find(a => String(a.login) === String(login));
  if (!acc || acc.current) return;
  const ok = await confirmApiCall({
    title: `删除账户 #${login}？`,
    desc: `本地账本 ${acc.deals != null ? acc.deals + ' 条成交' : ''}、余额 ${acc.last_balance != null ? fmt(acc.last_balance) : '—'}。删除后它不再出现在下拉列表里。`,
    cost: '账本会移到 data/live/_trash/ 下，不是真删除——需要时可以手动移回来。',
    okText: '删除',
  });
  if (!ok) return;
  try {
    const r = await fetch('/api/accounts/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: login, confirm: true }),
    });
    const j = await r.json();
    if (!j.ok) { qaToast(j.error || '删除失败', 'err'); return; }
    sessSel = null;
    liveData = null;
    await refreshLive(true);
  } catch (e) {
    qaToast(String(e), 'err');
  }
}

function reasonCn(k) {
  return { tp: '止盈', sl: '止损', stop_out: '强平', close_by: '对冲平仓', manual: '手动', open: '持仓中' }[k] || k || '—';
}

