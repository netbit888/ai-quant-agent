/* ==================== 首启配置向导 ====================
 * 没配 API key 时自动弹出，也能从模型条手动打开。
 * 「要不要弹」由后端 /api/setup 的 needed 决定，判据是「模型能不能真调」，
 * 不是「.env 存不存在」——半截配置恰恰最需要引导。
 */
async function checkSetup() {
  try {
    const j = await (await fetch('/api/setup', { cache: 'no-store' })).json();
    if (!j || !j.ok || !j.needed) return;
    openSetup(j);
  } catch (e) {
    /* 向导本身失败绝不能挡住面板 */
  }
}

function openSetup(payload) {
  if (!llmCfg) { loadLlm().then(() => openSetup(payload)); return; }
  const sel = $('setupProvider');
  if (sel && !sel.options.length) {
    sel.innerHTML = (llmCfg.providers || []).map(p =>
      `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    if (llmCfg.entry || llmCfg.provider) sel.value = llmCfg.entry || llmCfg.provider;
  }
  $('setupModel').value = (payload && payload.config && payload.config.model)
    || (sel && sel.value && autoModel(sel.value)) || '';
  $('setupKey').value = '';
  $('setupKey').classList.remove('bad');
  $('setupMsg').textContent = '';
  $('setupMsg').className = 'mini';
  maskOpen($('setupMask'), { focus: $('setupKey') });
}

function autoModel(pid) {
  const p = ((llmCfg && llmCfg.providers) || []).find(x => x.id === pid);
  return (p && p.default_model) || '';
}

function onSetupProviderChange() {
  // 换供应商要把模型名换成该家的默认，否则会拿 A 家的模型名去调 B 家，必然 400
  $('setupModel').value = autoModel($('setupProvider').value);
  $('setupMsg').textContent = '';
  $('setupMsg').className = 'mini';
}

async function skipSetup() {
  try {
    await fetch('/api/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skip: true }),
    });
  } catch (e) { /* 标记写不上也不该拦住用户 */ }
  maskClose($('setupMask'));
  loadLlm();
}

async function saveSetup() {
  const pid = $('setupProvider').value;
  const key = ($('setupKey').value || '').trim();
  const model = ($('setupModel').value || '').trim();
  const msg = $('setupMsg');
  if (!key) {
    msg.textContent = '请先填 API key';
    msg.className = 'mini setup-err';
    $('setupKey').classList.add('bad');
    $('setupKey').focus();
    return;
  }
  msg.textContent = '正在真调一次模型验证（约 2 秒）…';
  msg.className = 'mini';
  try {
    const r = await fetch('/api/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // 向导里没有「思考」这一项：同一家就沿用当前模式，别因为补填 key
      // 把深度思考悄悄关掉；换家则回默认（关）。
      body: JSON.stringify({
        confirm: true, provider: pid, key, model,
        thinking: !!(llmCfg && llmCfg.thinking && pid === llmCfg.provider),
      }),
    });
    const j = await r.json();
    if (j.ok) {
      const secs = (typeof j.elapsed === 'number') ? j.elapsed.toFixed(2) + 's' : '—';
      msg.textContent = `连通成功（${secs}），回「${j.reply || ''}」`;
      msg.className = 'mini setup-ok';
      $('setupKey').value = '';
      setTimeout(() => {
        maskClose($('setupMask'));
        loadLlm();
        loadAutoTrade();
      }, 1100);
    } else {
      msg.textContent = '失败：' + (j.error || '未知错误') + '（已回滚，没留下坏配置）';
      msg.className = 'mini setup-err';
    }
  } catch (e) {
    msg.textContent = '请求失败：' + String(e);
    msg.className = 'mini setup-err';
  }
}

