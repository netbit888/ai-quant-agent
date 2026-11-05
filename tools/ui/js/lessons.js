/* ==================== 教训库（跨场次） ====================
   为什么要它：`recent_lessons()` 只回答"现在塞哪几条进提示词"，
   不回答"经验到底有没有用"。后者只能看同一主题在不同场次里的复发次数：
   首场 8 次、次场 3 次 = 在收敛；一直每场 6~8 次 = 提示词没压住。
   默认收起 + 点开才拉：这个接口要扫所有归档流水数平仓笔，比状态查询重。 */
async function loadLessons() {
  const box = $('atLessonsText');
  const hint = $('atLessonsHint');
  if (!box) return;
  box.textContent = '加载中…';
  try {
    const r = await fetch('/api/lessons?account=' + encodeURIComponent(curLogin || ''));
    const j = await r.json();
    if (!j.ok) {
      box.textContent = '读取失败：' + (j.error || '未知错误');
      return;
    }
    box.textContent = j.text || '（教训库是空的）';
    const rep = j.report || {};
    hint.textContent = `${rep.lessons || 0} 条 · ${(rep.sessions || []).length} 场`
      + '（每次点开都重新扫一遍）';
  } catch (e) {
    box.textContent = '请求失败：' + String(e);
  }
}

/* ==================== 经验库包 ====================
 * 像选 EA 一样选一套经验。全局共享（不跟账户走）：
 * AI 做单的提示词注入和复盘写入都用当前选中的这个。
 * 「没有缓存」是这套机制的根基——切换是原子改 index.json，
 * 下一次决策读到的就是新包，不用任何失效逻辑。 */
let lpPacks = [];

async function loadLessonPacks() {
  const sel = $('lpSelect');
  if (!sel) return;
  try {
    const j = await (await fetch('/api/lesson_packs', { cache: 'no-store' })).json();
    if (!j.ok) { sel.innerHTML = '<option value="">读取失败</option>'; return; }
    lpPacks = j.packs || [];
    const prev = sel.value;
    sel.innerHTML = (j.packs || []).map(p =>
      `<option value="${esc(p.id)}">${esc(p.name)}（${p.count} 条）</option>`
    ).join('') || '<option value="">（还没有经验库）</option>';
    const want = (prev && lpPacks.some(p => p.id === prev)) ? prev : (j.active || '');
    if (want) sel.value = want;
  } catch (e) {
    sel.innerHTML = '<option value="">读取失败</option>';
  }
}

async function lpSelectChanged() {
  const id = $('lpSelect').value;
  if (!id) return;
  try {
    const j = await (await fetch('/api/lesson_packs/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    })).json();
    if (!j.ok) atSay('<b>切换失败：</b>' + esc(j.error || ''), 'err');
  } catch (e) { atSay('<b>切换请求失败：</b>' + String(e), 'err'); }
}

async function lpScanAndImport() {
  // 原生窗口：点「导入」直接弹系统文件选择框，不用先拷文件到 import 目录。
  if (isNative() && typeof window.pywebview.api.pick_lesson_file === 'function') {
    let picked;
    try { picked = await nativeApi('pick_lesson_file'); }
    catch (e) { qaToast('打开文件选择器失败：' + String(e), 'err'); return; }
    if (!picked || !picked.ok) {
      if (picked && picked.error) qaToast('无法打开文件选择器：' + picked.error, 'err');
      return;
    }
    if (!picked.path) return;   // 用户在对话框里点了取消，不是错误
    const name = prompt('给要导入的经验包取个名字：', picked.name || '');
    if (name === null) return;
    try {
      const j = await (await fetch('/api/lesson_packs/import_path', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: picked.path, name: name || picked.name }),
      })).json();
      if (!j.ok) { qaToast('导入失败：' + (j.error || ''), 'err'); return; }
      qaToast(`导入完成：${j.imported} 条经验${j.skipped_bad ? `（跳过 ${j.skipped_bad} 行坏数据）` : ''}，已设为当前使用。`, 'ok');
      await loadLessonPacks();
      loadLessons();
    } catch (e) { qaToast('导入请求失败：' + String(e), 'err'); }
    return;
  }
  // 浏览器模式拿不到系统对话框：走 import 目录的老流程。
  let scan;
  try {
    scan = await (await fetch('/api/lesson_packs/scan', { cache: 'no-store' })).json();
  } catch (e) { qaToast('扫描请求失败：' + String(e), 'err'); return; }
  if (!scan.ok) { qaToast('扫描失败：' + (scan.error || ''), 'err'); return; }
  const dir = scan.import_dir || '';
  if (!(scan.files || []).length) {
    qaToast('import 目录里还没有 .jsonl 文件。\n\n把经验库文件（.jsonl）放进：\n' + dir
      + '\n\n然后再点一次「导入」。', 'err');
    return;
  }
  const list = scan.files.map(f => `${f.name}（${f.count} 条）`).join('\n');
  const name = prompt(
    'import 目录里发现：\n\n' + list + '\n\n给要导入的经验包取个名字：', '');
  if (name === null) return;
  const file = scan.files[0].name;   // 一次装一份；多了再点一次
  try {
    const j = await (await fetch('/api/lesson_packs/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file, name: name || file }),
    })).json();
    if (!j.ok) { qaToast('导入失败：' + (j.error || ''), 'err'); return; }
    qaToast(`导入完成：${j.imported} 条经验${j.skipped_bad ? `（跳过 ${j.skipped_bad} 行坏数据）` : ''}，已设为当前使用。`, 'ok');
    await loadLessonPacks();
    loadLessons();
  } catch (e) { qaToast('导入请求失败：' + String(e), 'err'); }
}

async function loadAppVersion() {
  try {
    const j = await (await fetch('/api/app/version', { cache: 'no-store' })).json();
    if (j.ok && j.version) {
      $('appVersion').textContent = j.version;
      $('verPill').style.display = '';
    }
  } catch (e) { /* 展示性元素，静默失败 */ }
}

/* 页脚「退出」只在没有窗口外壳的场景出现（浏览器模式 / --web）。
   原生窗口有标题栏的 X，而 X 走的是同一条关闭流程（_on_window_closing →
   onCloseRequested → quitApp），再摆一个按钮就是同一个动作两个入口。
   但浏览器模式关标签页 ≠ 退出：服务端和 AI 线程会继续跑，必须留出口。
   拿不到 shell 时按原生处理——宁可少一个按钮，也不要在窗口里冒出重复的退出。 */
async function loadShellQuit() {
  try {
    const j = await (await fetch('/api/app/info', { cache: 'no-store' })).json();
    if (j && j.shell !== 'native') $('footQuit').classList.remove('hidden');
  } catch (e) { /* 静默：这个按钮是兜底，不是主路径 */ }
}

async function lpExportActive() {
  const sel = $('lpSelect');
  const id = (sel || {}).value || '';
  if (!id) { qaToast('先选一个要导出的经验库', 'err'); return; }

  // 原生窗口：先弹系统「另存为」，用户选哪里就写哪里——和「导入」对称。
  // 浏览器模式拿不到原生对话框，退回后端 export/ 目录（老行为不能丢）。
  let dest = '';
  let native = false;
  if (isNative() && typeof window.pywebview.api.pick_export_path === 'function') {
    const pack = (lpPacks || []).find(p => p.id === id) || {};
    let picked;
    // 文件名清洗交给后端：那边已经有一份规则，前端再写一遍必然分叉
    try { picked = await nativeApi('pick_export_path', pack.name || 'lesson_pack'); }
    catch (e) { qaToast('打开保存对话框失败：' + String(e), 'err'); return; }
    if (!picked || !picked.ok) {
      if (picked && picked.error) qaToast('无法打开保存对话框：' + picked.error, 'err');
      return;
    }
    if (!picked.path) return;   // 用户在对话框里点了取消，不是错误
    dest = picked.path;
    native = true;
  }

  try {
    const j = await (await fetch('/api/lesson_packs/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(native ? { id, path: dest } : { id }),
    })).json();
    if (!j.ok) { qaToast('导出失败：' + (j.error || ''), 'err'); return; }
    // 不能用浏览器下载（WebView2 里 ALLOW_DOWNLOADS=False），路径就是交付物
    confirmApiCall({
      title: '导出完成',
      desc: native
        ? `已导出 ${j.count} 条到：\n${j.path}`
        : `已导出 ${j.count} 条到：\n${j.path}\n\n用文件管理器打开这个位置就能拿到 .jsonl 文件。`,
      okText: '知道了',
    });
  } catch (e) { qaToast('导出请求失败：' + String(e), 'err'); }
}

async function lpCreate() {
  const name = prompt('给新的经验库起个名字：', '');
  if (!name) return;
  try {
    const j = await (await fetch('/api/lesson_packs/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })).json();
    if (!j.ok) { qaToast('创建失败：' + (j.error || ''), 'err'); return; }
    await loadLessonPacks();
  } catch (e) { qaToast('创建请求失败：' + String(e), 'err'); }
}

