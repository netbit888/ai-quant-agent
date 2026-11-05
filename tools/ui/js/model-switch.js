/* ==================== 模型切换 ====================
   为什么放到面板上：换模型是常用动作（免费额度用完、想对比判断质量），
   以前要改 .env 再重启面板——三步，还可能忘第三步，然后对着「模型没换」
   的界面怀疑代码。现在一步，且后端会真调一次模型验证、失败自动回滚
   文件和进程内存，所以这个按钮不会把人留在坏配置上。

   key 留空 = 沿用当前 key：同一个 key 不能跨供应商用，所以换家时必须填。 */
let llmCfg = null;

function renderLlmState(j) {
  const tail = j.key_set ? `key …${esc(j.key_tail)}` : '<span style="color:#ff9b9d">⚠ 没有 key</span>';
  // 思考状态要显示「实际会怎样」，不能只显示配置里写的。服务端拒过这个字段
  // 之后进程会静默关掉思考（`_disable_thinking_for_process`），请求照样成功——
  // 界面要是还写「深度思考」，人就会按 6 秒延迟的预期去等一个 1 秒的响应。
  let th = '';
  if (j.thinking) {
    th = j.thinking_effective
      ? ' · <span style="color:#7fd4a0">深度思考</span>'
      : ' · <span style="color:#ffc66d">深度思考（未生效）</span>';
  }
  $('atLlmNow').innerHTML = `当前 <b>${esc(j.model)}</b> · ${esc(j.provider_name || '未知')}${th} · ${tail}`;
}

async function loadLlm() {
  try {
    const r = await fetch('/api/llm', { cache: 'no-store' });
    const j = await r.json();
    if (!j.ok) { $('atLlmNow').textContent = '读取失败：' + (j.error || '未知错误'); return; }
    llmCfg = j;
    const sel = $('atLlmProvider');
    sel.innerHTML = (j.providers || []).map(p =>
      `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    if (j.provider) {
      sel.value = j.provider;
    } else {
      // base_url 不在已知列表里（手动改过 .env）：给一个不可选的占位，
      // 别假装它是这几家之一。
      const o = document.createElement('option');
      o.value = ''; o.textContent = '当前：' + (j.provider_name || '自定义');
      o.disabled = true; o.selected = true;
      sel.insertBefore(o, sel.firstChild);
    }
    renderLlmState(j);
    // 当前模型项由后端算好下标（DeepSeek 两项模型名相同，前端没法自己判断）
    fillLlmModels(j.provider || '', false, j.variant_index);
  } catch (e) {
    $('atLlmNow').textContent = '读取失败：' + String(e);
  }
}

function curProvider() {
  const pid = $('atLlmProvider').value;
  return ((llmCfg && llmCfg.providers) || []).find(x => x.id === pid) || null;
}

// 「模型」下拉里当前选中的那一项。value 是下标，不是模型名——
// DeepSeek 的两项模型名一模一样，差别只在 thinking。
function curVariant(p) {
  if (!p) return null;
  const vs = p.variants || [];
  if (!vs.length) return null;
  const i = parseInt($('atLlmModel').value || '0', 10) || 0;
  return vs[Math.max(0, Math.min(i, vs.length - 1))];
}

// 换供应商时模型清单跟着换，默认落在该家第一项。
// 免得把 glm-4-flash 发给 DeepSeek（那会 400 模型不存在）。
function fillLlmModels(pid, resetModel, index) {
  if (!llmCfg) return;
  const p = (llmCfg.providers || []).find(x => x.id === pid);
  const sel = $('atLlmModel');
  if (!p) { sel.innerHTML = ''; return; }
  const vs = p.variants || [];
  sel.innerHTML = vs.map((v, i) =>
    `<option value="${i}">${esc(v.label || v.model)}</option>`).join('');
  let want = (typeof index === 'number' && index >= 0) ? index : 0;
  if (resetModel) want = 0;
  sel.value = String(Math.min(want, Math.max(vs.length - 1, 0)));
  onLlmModelChange();
}

function onLlmProviderChange() {
  fillLlmModels($('atLlmProvider').value, true);
}

// 换模型项只改提示文案，**不偷偷提交**：写入仍然要按「应用」+ 确认弹窗。
function onLlmModelChange() {
  if (!llmCfg) return;
  const p = curProvider();
  if (!p) return;
  // 标签列已经写了「API key」，占位符不再重复这三个字。
  $('atLlmKey').placeholder = (p.id === llmCfg.provider)
    ? '沿用当前就留空'
    : `必须填 ${p.name} 的 key`;
}

async function switchLlm() {
  if (!llmCfg) { atSay('<b>配置还没读出来</b>，稍等一下再点。', 'err'); return; }
  const p = curProvider();
  if (!p) { atSay('<b>先选一个供应商。</b>', 'err'); return; }
  const pid = p.id;
  const v = curVariant(p) || { model: p.default_model, thinking: false };
  const model = v.model;
  const thinking = !!v.thinking;
  const key = ($('atLlmKey').value || '').trim();
  // 同一家 = 同一把 key。换供应商才需要重填，换模型项（思考 ↔ 不思考）不用。
  const sameProvider = (p.id === llmCfg.provider);

  if (!key && !sameProvider) {
    atSay(`<b>换供应商要填 key</b>：当前这把 key 只对 ${esc(llmCfg.provider_name || '原供应商')} 有效。`, 'err');
    $('atLlmKey').classList.add('bad');
    $('atLlmKey').focus();
    return;
  }

  // 确认弹窗是 textContent 渲染，纯文本，换行会被折叠——一句话说完。
  const desc = '会先真调一次模型验证连通，成功才写进 .env；失败自动回滚，'
    + '不会把你留在坏配置上。切换后不用重启面板，下一次决策就用新模型。'
    + (thinking
      ? ' 深度思考会先把整段想完再答，实测首字 6.11 秒（默认 0.95 秒）。'
        + '超短线决策落在每根 M1 的第 28~52 秒，多等 6 秒等于把后半根也赌进去；'
        + '60 个真实决策点的 A/B 也没量出精度增益——想要「想清楚再答」再选它。'
      : '');

  let go = false;
  try {
    go = await confirmApiCall({
      title: thinking ? `切换到深度思考（${model}）？` : `切换到 ${model}？`,
      desc,
      cost: thinking ? '一次验证调用（约 6 秒，输出几百 token）'
                     : '一次调用（十几个 token，失败不计费）',
      okText: '切换',
    });
  } catch (e) {
    atSay('<b>确认弹窗没能打开：</b>' + esc(String(e)), 'err');
    return;
  }
  if (!go) return;

  $('atLlmSave').disabled = true;
  atSay('正在验证（真调一次模型，约 2 秒）…');
  try {
    const r = await fetch('/api/llm/switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, provider: pid, model, thinking,
                             key: key || undefined }),
    });
    const j = await r.json();
    if (j.ok) {
      const secs = (typeof j.elapsed === 'number') ? j.elapsed.toFixed(2) + 's' : '—';
      if (j.warning) {
        // 切成功了但模式没生效——必须说，别让人按 6 秒延迟的预期等 1 秒的响应
        atSay(`<b>已切到 ${esc(j.model)}，但状态不对：</b>${esc(j.warning)}`, 'err');
      } else {
        const th = thinking ? '深度思考已生效 · ' : '';
        atSay(`<b>已切到 ${esc(j.model)}</b> · ${th}验证 ${secs} 回「${esc(j.reply || '')}」· `
          + '已生效，不用重启面板。', 'ok');
      }
      $('atLlmKey').value = '';
      $('atLlmKey').classList.remove('bad');
      await loadLlm();
      loadAutoTrade();
    } else if (j.need_key) {
      atSay('<b>要填 key：</b>' + esc(j.error || ''), 'err');
      $('atLlmKey').classList.add('bad');
      $('atLlmKey').focus();
    } else {
      atSay('<b>切换失败，已回滚：</b>' + esc(j.error || '未知错误'), 'err');
    }
  } catch (e) {
    atSay('<b>切换请求失败：</b>' + esc(String(e)), 'err');
  } finally {
    $('atLlmSave').disabled = false;
  }
}

