/* 人工标签：这笔为什么进（2026-11-18）
 *
 * 为什么必须由人来标
 * ------------------
 * 程序能记下发生过的每一件事：开平价、峰值浮盈、SL 挪了几次、平在几点。
 * 它记不下开仓那一刻人脑子里那句话——而那句话决定复盘怎么归因：
 *
 *   标「顺势突破」的单走早了 → 执行问题（怕回撤），要治手；
 *   标「抢反弹就跑」的单走早了 → 判断本来如此，压根不该算损失。
 *
 * 两件事在流水里长得一模一样。没有标签，MFE 捕获率就是两者的混合物，
 * 拿它去优化任意一边都是在拟合噪声。
 *
 * 交互位置
 * --------
 * 持仓面板头行一个胶囊（.pos-tag），不新增行高——1120×720 下面板已经贴底。
 * 程序单和手工单共用：程序单的 regime 由 plan 预填，手工单由人选。
 * 空仓时整块隐藏；换了一笔（ticket 变了）浮层自动关掉，避免标到下一笔上。
 *
 * 标签集合**只从后端 label_tags 来**。前端写死一份、后端校验另一份，
 * 加一个标签就要改两个地方，漏一处就是"界面能选、保存报 400"。
 */

let _tagTicket = 0;      // 当前面板里那一笔的 ticket
let _tagOpen = false;    // 浮层是否开着
let _tagBusy = false;    // 请求在飞：禁止连点（重复 POST 只会互相覆盖）
let _tagOptions = null;  // 后端下发的 {tags:{trend:[],range:[]}, regime_txt:{}}
let _tagLabel = null;    // 这笔已有的标签 {regime,tag,...}

function tagRegimeTxt(regime) {
  const txt = (_tagOptions && _tagOptions.regime_txt) || {};
  return txt[regime] || (regime === 'trend' ? '趋势单' : '震荡单');
}

/* 每轮轮询都调（renderPos 开头）。只写胶囊，**不重建浮层**：
 * 1 秒一次重建会让人刚瞄上的按钮在鼠标底下换掉。 */
function renderTagChip(a, p, mp) {
  const el = $('posTag');
  if (!el) return;
  const cur = (p && p.ticket) ? p : ((mp && mp.ticket) ? mp : null);
  if (!cur) {
    el.className = 'pos-tag hidden';
    el.textContent = '';
    _tagTicket = 0;
    _tagLabel = null;
    if (_tagOpen) closeTagPicker();
    return;
  }
  const tk = Number(cur.ticket) || 0;
  // 换了一笔：浮层如果还开着，里面选中的是上一笔的状态，必须关掉。
  if (_tagOpen && _tagTicket && tk !== _tagTicket) closeTagPicker();
  _tagTicket = tk;
  _tagLabel = (cur.label && cur.label.tag) ? cur.label : null;
  _tagOptions = (a && a.label_tags) || _tagOptions;

  if (_tagLabel) {
    const trend = _tagLabel.regime === 'trend';
    el.className = 'pos-tag on' + (trend ? ' trend' : '');
    el.textContent = (trend ? '趋势' : '震荡') + ' · ' + _tagLabel.tag;
    el.title = '这笔的标签：' + tagRegimeTxt(_tagLabel.regime) + ' · ' + _tagLabel.tag
      + '\n点一下可以改。标签只用于复盘归因，不影响下单、止损、平仓。';
  } else {
    el.className = 'pos-tag todo';
    el.textContent = '待标签';
    el.title = '这笔为什么进？点一下选。'
      + '\n不选也能做单，但这笔不会进归因统计——'
      + '走早了到底是「执行不到位」还是「打法本来就是快进快出」，事后就分不开了。';
  }
}

function toggleTagPicker() {
  if (_tagOpen) closeTagPicker();
  else openTagPicker();
}

function openTagPicker() {
  const pop = $('tagPop');
  if (!pop || !_tagTicket || _tagBusy) return;
  if (!_tagOptions || !_tagOptions.tags) {
    qaToast('标签选项还没下发，等面板刷新一下再点', 'err');
    return;
  }
  const fill = (boxId, list, regime) => {
    const box = $(boxId);
    if (!box) return;
    box.innerHTML = '';
    (list || []).forEach(t => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tagopt'
        + (_tagLabel && _tagLabel.regime === regime && _tagLabel.tag === t ? ' on' : '');
      b.textContent = t;
      b.onclick = () => pickTag(regime, t);
      box.appendChild(b);
    });
  };
  fill('tagOptsTrend', _tagOptions.tags.trend, 'trend');
  fill('tagOptsRange', _tagOptions.tags.range, 'range');

  const clr = $('tagClear');
  if (clr) clr.style.display = _tagLabel ? '' : 'none';
  const hint = $('tagPopHint');
  if (hint) {
    hint.textContent = _tagLabel
      ? '已标 ' + tagRegimeTxt(_tagLabel.regime) + ' · ' + _tagLabel.tag + '，可以改'
      : '选完立刻记下 · 平仓后进归因统计';
  }
  pop.classList.remove('hidden');
  _tagOpen = true;
}

function closeTagPicker() {
  const pop = $('tagPop');
  if (pop) pop.classList.add('hidden');
  _tagOpen = false;
}

/* 点空白处关掉浮层。用 capture：面板里别的处理器会 stopPropagation，
 * 冒泡阶段可能根本收不到这次点击。 */
document.addEventListener('click', function (ev) {
  if (!_tagOpen) return;
  const pop = $('tagPop'), chip = $('posTag');
  if (pop && pop.contains(ev.target)) return;
  if (chip && chip.contains(ev.target)) return;
  closeTagPicker();
}, true);

function setTagBusy(on) {
  const pop = $('tagPop');
  if (!pop) return;
  pop.querySelectorAll('button').forEach(b => { b.disabled = !!on; });
}

async function postTag(body) {
  const r = await fetch('/api/trade_label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, account: curLogin || '' }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

async function pickTag(regime, tag) {
  if (_tagBusy || !_tagTicket) return;
  _tagBusy = true;
  setTagBusy(true);
  const tk = _tagTicket;
  try {
    const j = await postTag({ ticket: tk, regime, tag });
    _tagLabel = j.label || { regime, tag };
    // 立刻回显：轮询最长 1 秒，但点了就想看到结果——等轮询会像没反应。
    const el = $('posTag');
    if (el) {
      const trend = regime === 'trend';
      el.className = 'pos-tag on' + (trend ? ' trend' : '');
      el.textContent = (trend ? '趋势' : '震荡') + ' · ' + tag;
    }
    closeTagPicker();
    qaToast('已记下：' + tagRegimeTxt(regime) + ' · ' + tag, 'ok');
  } catch (e) {
    // 不清 _tagLabel：接口失败时这笔的标签没变，界面不该假装变了。
    qaToast('标签没存上：' + (e && e.message ? e.message : e), 'err');
  } finally {
    _tagBusy = false;
    setTagBusy(false);
  }
}

async function clearTag() {
  if (_tagBusy || !_tagTicket) return;
  _tagBusy = true;
  setTagBusy(true);
  const tk = _tagTicket;
  try {
    await postTag({ ticket: tk, clear: true });
    _tagLabel = null;
    const el = $('posTag');
    if (el) { el.className = 'pos-tag todo'; el.textContent = '待标签'; }
    closeTagPicker();
    qaToast('标签已清除', 'ok');
  } catch (e) {
    qaToast('清除失败：' + (e && e.message ? e.message : e), 'err');
  } finally {
    _tagBusy = false;
    setTagBusy(false);
  }
}
