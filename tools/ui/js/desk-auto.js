/* 决策流表格上一份 HTML。每秒轮询时用它判断"内容有没有变"，
   相同就跳过 innerHTML 重建（理由见下面的 rowsHtml）。 */
let atRowsHtml = '';

/* 冷静期剩余秒数（辅助模式专用）。
 *
 * 超时 / 否决之后有一段静默期（assist_deny_cooldown，默认 180 秒），
 * 但两个槽位的含义完全不同，混着写会让用户以为"程序全停了"：
 *   entry —— 不弹窗，**也不调模型**（省 token，也避免每分钟重弹一次铃声）
 *   exit  —— 只是不弹窗，模型照问（仓位不能 3 分钟没人评估）
 * 基准一律用 server_ts：客户端时钟偏几分钟会让倒计时凭空多出或少掉一截。
 */
function atCooldown(a) {
  const raw = (a && a.assist_deny_until) || {};
  const nowS = Number(a && a.server_ts) || (Date.now() / 1000);
  const left = k => Math.max(0, Math.round(Number(raw[k] || 0) - nowS));
  return { entry: left('entry'), exit: left('exit') };
}

/* 秒 → m:ss。倒计时必须带冒号格式：写"还剩 125 秒"人得自己换算。 */
function atMmSs(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function renderAutoTrade(a) {
  renderStatusBar(a);
  const st = $('atState');
  st.style.display = '';
  if (!a || !a.ok) {
    st.textContent = '读取失败';
    st.className = 'badge-off';
    return;
  }
  if (a.running) {
    st.textContent = a.dry_run ? '运行中 · 纸面' : '运行中 · 真实';
    st.className = 'badge-live';
  } else if (a.stale) {
    // 面板重启过，磁盘上还留着 running=true 但线程早没了。必须说清楚，
    // 否则用户会以为 AI 还在做单，其实一笔都不会开。
    st.textContent = '已停止（面板曾重启）';
    st.className = 'badge-off';
  } else if (a.stop_reason) {
    st.textContent = '已停止';
    st.className = 'badge-off';
  } else {
    st.textContent = '未启动';
    st.className = 'badge-off';
  }
  if (a.stale) {
    $('atNote').className = 'mini at-head-note red';
    $('atNote').textContent = '上次的线程已不在，请重新点「开始做单」';
  } else {
    // 经验条数写在常驻位：重置战绩之后这里不清零，才看得出经验真的留住了。
    const bits = [];
    if (typeof a.lessons === 'number') bits.push('经验库 ' + a.lessons + ' 条');
    if (a.stop_reason) bits.push('停止原因：' + a.stop_reason);
    $('atNote').className = 'mini at-head-note';
    $('atNote').textContent = bits.join(' · ');
  }

  // 做单台操作条右侧的模式摘要。设置舱里那条 atModeHint 写得很长（含成本
  // 估算），做单时只需要确认"现在发的是真单还是纸面"这一件事。
  const nowMode = $('atNowMode');
  if (nowMode) {
    const ivSec = Number((a.cfg && a.cfg.interval_sec) || 0) || 60;
    const modeTxt = a.assist ? (a.dry_run ? '辅助演练' : '辅助')
      : (a.dry_run ? '纸面' : '真实');
    nowMode.textContent = modeTxt + ' · ' + (a.symbol || '—') + ' · ' + ivSec + 's';
    nowMode.className = 'at-now' + (a.assist ? ' red' : '');
  }

  // ---- 做单台 v2：决策环 / 持仓面板 / 闸门灯 ----
  // 三块都只读这一份 a，不另开请求。原来的 5 个 KPI 卡和 6 个参数胶囊
  // 由它们取代：前者把「这一轮想了什么」压成一行副标题，后者把整个交易日
  // 都不变的阈值当常驻信息占掉一整行。
  fillFeedback(a);
  const p = a.position;
  renderRing(a, p);
  renderPos(a, p);
  renderGateLeds(a, p);

  const ld = a.last_decision;

  // 常驻状态带的判断标签。原话 / 锚点 / 程序建议全部搬进了决策环——
  // 上一版这条带子挂着 3 行、104px 高，而决策环里有更大的版面写同一件事。
  // 这里只留一个标签，因为「模型不做」「程序拦下」「模型否决建议」这三件事
  // 该找的人不一样：前者调提示词，中间调开关，后者看事后分桶。
  const whyTag = $('atWhyTag');
  if (whyTag) {
    let tag = '—', cls = '', tip = '';
    // 静默期文案要能对上"上一轮到底发生了什么"（否决 / 超时 / 作废）。
    const ANS_TAG = { deny: '已否决', expired: '超时未答', cancelled: '已取消',
                      blocked: '信号作废·已有持仓', void_slip: '价格已跑·作废',
                      void_unknown: '读不到持仓·作废', void_state: '已不可开仓·作废' };
    if (ld) {
      // 这一轮最后没进场（否决 / 超时 / 作废）时，标签必须**盖过** act=true 的
      // 「模型开仓」。last_decision 是轮首写的，应答发生在它之后，后端会用
      // answer 打标；不认这个字段的话，同一块屏幕上会同时出现「模型开仓」
      // 和弹窗那句「已否决」——10-29 用户就是这么发现的。
      if (ld.answer) {
        tag = ANS_TAG[ld.answer] || '未进场';
        cls = 'is-deny';
        // 冷静期还没结束时把**剩余时间**接在标签后面。以前写的是「静默到 21:57」，
        // 是个绝对时刻——人得自己减一遍才知道还要等多久，而且手边没有一眼可读的
        // 参照点，看起来就像"这条状态已经过期了"（11-09 用户原话）。
        const nowS = Number(a.server_ts) || (Date.now() / 1000);
        const until = Number(ld.deny_until) || 0;
        if (a.running && until > nowS) tag += ' · 冷静期 ' + atMmSs(until - nowS);
      }
      else if (ld.veto) { tag = '程序拦下'; cls = 'is-veto'; }
      // 2026-10-28 起程序默认只建议不拦：有建议却没拦，说明模型否决了它。
      // 这一格是事后分桶的关键——「听了程序」和「没听」要能分开算期望。
      else if (ld.advice) { tag = '模型否决建议'; cls = 'is-override'; }
      else if (ld.kind === 'entry') {
        if (ld.act === false) { tag = '模型不做'; cls = 'is-skip'; }
        else if (ld.act === true) { tag = '模型开仓'; cls = 'is-act'; }
        else { tag = '模型偏' + (ld.direction > 0 ? '多' : '空'); cls = 'is-skip'; }
      } else { tag = ld.close ? '平仓' : '持仓中'; }
      // 到过止盈门槛的单子（2026-10-29）：直接标出「每 N 秒复核」。
      // 不标的话，用户看到 reason 里一句「暂不调模型」会以为程序把这笔忘了。
      // 判据取 position.tp_reached_ts 而不是 ld.source：闸门 hold 那轮的
      // last_decision.source 是 tp_recheck，但模型答完一轮后 source 变成 llm，
      // 只有 pos 上的标记是贯穿整笔的。
      const reaskSec = Number(a.tp_reask_sec || 0);
      if (!ld.close && reaskSec > 0 && a.position && a.position.tp_reached_ts) {
        tag = '持仓中 · ' + reaskSec + 's 复核';
      }
    }

    // 冷静期盖在最后：它描述的是**当下还要等多久**，比上一轮结论更新。
    // 以前这里只写「超时未答」，用户看到的是"过期了"，于是以为线程死了
    // （11-09 实盘：静默 180 秒里模型一次都没被调用，那是设计如此）。
    // `a.running` 是必要条件：线程已经停了就没有"还在等"这回事，那时显示
    // 倒计时等于骗人——state 里的 assist_deny_until 不会被清掉。
    const cool = atCooldown(a);
    if (a.running && cool.entry > 0 && !a.position) {
      tag = '冷静期 · 还剩 ' + atMmSs(cool.entry);
      cls = 'is-cool';
      tip = (ANS_TAG[(ld && ld.answer)] || '上一条入场信号未成') + '，按不做处理。'
        + '冷静期内不调模型、不弹窗；倒计时结束自动恢复判断。';
    } else if (a.running && cool.exit > 0 && a.position) {
      tag = '持仓中 · 暂停弹窗 ' + atMmSs(cool.exit);
      cls = 'is-cool';
      tip = '你刚否决了平仓信号：这段时间不再弹窗，但模型仍在评估这笔持仓，硬止损照旧。';
    }
    whyTag.textContent = tag;
    whyTag.className = 'sb-why-tag ' + cls;
    whyTag.title = tip || (ld && ld.advice_text) || '';
  }


  // 模型自己说"不做"的次数。它和"被闸门挡掉"是两回事：
  // 如果这个数一直涨、开仓一直是 0，说明它变胆小了，那是另一种失败。
  $('atDecline').textContent = a.model_declines
    ? `模型主动放弃 ${a.model_declines} 次` : '';

  // 调用失败（429/超时/key 错）和"它主动不做"是两回事：
  // 前者是 API 挂了，这时候一笔不做是故障，不是稳健。
  $('atFail').textContent = a.model_failures
    ? `⚠ 模型调用失败 ${a.model_failures} 次（免费额度/限速？）` : '';
  $('atFail').style.color = a.model_failures ? 'var(--bad)' : '';

  // 程序止损笔数：这个数和"模型平仓"要分开看。止损多说明入场方向差，
  // 模型平得差是另一回事——两者的处理方式完全不同，混在一起看不出问题。
    const slN = Number(a.sl_closes || 0);
    // 保本止损（2026-10-29）单独点名：它和硬止损混在一起会看不出规则效果。
    // 「硬止损 8 笔 + 保本 3 笔」= 方向错得多，但至少 3 次没让它变成亏损单。
    const beN = Number(a.be_closes || 0);
    const beTxt = beN ? `（保本止损 ${beN} 笔）` : '';
    if (slN) {
      // 门槛分两档（2026-10-28 起趋势单 7.00 / 震荡 3.25）。这里只写 3.25 的话，
      // 持仓是趋势单时用户会以为「趋势单到 3.25 就要平」——而事实是 7.00。
      // 统计本身不分 plan，所以两个档位都写出来，不替他选。
      const tpTrend = Number(a.tp_min_trend || 0);
      // 金额单位跟着账户币种走（USD / USC）。美分户的 3.00 是 3 分，
      // 不标单位就会和美元户的 3.00 看起来一样，实际差 100 倍。
      const mu = a.money_unit || 'USD';
      const tpTxt = (a.tp_min_usd == null) ? ''
        : (tpTrend > 0 && tpTrend !== Number(a.tp_min_usd)
            ? `（门槛 震荡 ${fmt(a.tp_min_usd, 2)} / 趋势 ${fmt(tpTrend, 2)} ${mu}）`
            : `（止盈门槛 ${fmt(a.tp_min_usd, 2)} ${mu}）`);
      $('atSl').textContent = `程序止损 ${slN} 笔`
        + (a.sl_usd ? `（−${fmt(a.sl_usd, 2)} ${mu}）` : '')
        + beTxt
        + (a.tp_min_usd ? ` · 未达止盈门槛不平${tpTxt}` : '');
      $('atSl').style.color = 'var(--warn)';
    } else if (beN) {
      $('atSl').textContent = `保本止损 ${beN} 笔（摸到触发线后回到开仓价出场）`;
      $('atSl').style.color = 'var(--info)';
    } else {
      $('atSl').textContent = '';
    }

  // 最近 10 笔多空分布：只看盈亏发现不了"只会做多"这种偏置，必须单独显示。
  const dr = a.dir_recent || {};
  if (dr.n) {
    $('atDir').textContent = `最近 ${dr.n} 笔：多 ${dr.long} / 空 ${dr.short}`
      + (dr.skew ? '（⚠ 方向偏斜，检查是不是被教训锁死了）' : '');
    $('atDir').style.color = dr.skew ? 'var(--warn)' : '';
  } else {
    $('atDir').textContent = '';
  }

  // 已平仓但还没到复盘时机：平仓后要等 7 根 K 线才复盘。
  // 不显示的话用户会以为复盘坏了（平了却没看到复盘）。
  const pend = Number(a.pending_reviews || 0);
  if (pend && a.next_review_ts) {
    const left = Math.max(0, Math.ceil((a.next_review_ts - Date.now() / 1000) / 60));
    $('atPending').textContent = left
      ? `待复盘 ${pend} 笔（最早再走 ${left} 根 K 线）`
      : `待复盘 ${pend} 笔（下一轮就复盘）`;
  } else {
    $('atPending').textContent = '';
  }

  // 前缀缓存命中率：静态规则全在 system、动态数据放末尾，命中率本该很高。
  // 拿不到服务端计量时就不显示百分比，别编。
  const ck = a.cache || {};
  $('atCache').textContent = ck.hit_rate == null ? ''
    : `缓存命中 ${fmt(ck.hit_rate * 100, 1)}%（命中 ${ck.hit_tokens} / 未命中 ${ck.miss_tokens} tokens）`;

  // 用的是哪个模型。换供应商（DeepSeek → 智谱免费档）后必须看得见，
  // 否则 401/404 的时候分不清是 key 没换还是模型名写错。
  $('atModel').textContent = a.model ? `模型 ${a.model}` : '';

  // 五项全是空串时（刚装上、一笔没跑过）整张卡收起来——
  // 一个只有标题的空白卡片比没有卡片更像坏了。
  const runCard = $('runCount');
  if (runCard) {
    const anyVal = ['atSl', 'atDir', 'atPending', 'atCache', 'atModel']
      .some(id => { const e = $(id); return !!(e && e.textContent.trim()); });
    runCard.classList.toggle('hidden', !anyVal);
  }

  // 幕布上的状态跟着刷新：它每 1 秒拿到的就是这份数据，不用另开请求
  lastAutoData = a;
  updateCurtain(a);
  // 人工确认弹窗。**复用这一份 summary**，不另开请求：
  // 多一个请求就多一处会不同步的地方，而弹窗最怕的就是"信号已经失效了还在显示"。
  sigPoll(a);

  // 按钮：跑着的时候不许再点「开始」，避免起两个线程互相抢仓位
  // 跑起来就把「开始做单」收走：留一颗灰着的「运行中…」和左边那句
  // .at-now 的「运行中·实盘」同义，一行里出现两次"运行中"只会让人多读一遍。
  $('atStart').disabled = !!a.running;
  $('atStart').style.display = a.running ? 'none' : '';
  $('atStart').textContent = '开始做单';

  // 跑起来之后模式锁死：中途切换会造成「界面说真实、实际发纸面」的错位
  const lockMode = !!a.running;
  $('atModePaper').disabled = lockMode;
  $('atModeLive').disabled = lockMode;
  if ($('atModeAssist')) $('atModeAssist').disabled = lockMode;
  if ($('atAssistPaper')) $('atAssistPaper').disabled = lockMode;
  $('atInterval').disabled = lockMode;
  // 手数也锁死：跑起来之后改手数，持仓还是旧手数，出场阈值却换了一套
  if ($('atLots')) $('atLots').disabled = lockMode;
  // 品种同样锁死：跑起来之后换品种等于换了一个策略，仓位还在旧品种上
  fillSymbolOptions(a);
  if ($('atSymbol')) $('atSymbol').disabled = lockMode;
  if (lockMode) {
    const isAssist = !!a.assist;
    if ($('atModeAssist')) $('atModeAssist').checked = isAssist;
    $('atModePaper').checked = !isAssist && !!a.dry_run;
    $('atModeLive').checked = !isAssist && !a.dry_run;
    if ($('atAssistPaper')) $('atAssistPaper').checked = isAssist && !!a.dry_run;
    // 把选择器对齐到实际在跑的那个周期，别让界面说 60 秒而实际跑 5 秒
    const runningIv = a.cfg && a.cfg.interval_sec;
    if (runningIv) $('atInterval').value = String(runningIv);
    // 手数同样对齐到实际在跑的那一档：界面说 0.01、实际发 0.05 手，
    // 是最贵也最难查的一类错位（点差和止损都按实际手数走）。
    const runningLots = a.cfg && a.cfg.lots;
    if (runningLots && $('atLots')) $('atLots').value = String(runningLots);
    updateModeHint();
    // 正在跑的这条 cfg 是最新的事实：停止之后界面就该停在它上面，
    // 不许下一秒被 prefs 改回上一场的旧选择。
    atPrefsApplied = true;
  } else {
    // 没在跑：把上次的选择填回来（只填一次，之后以用户手动改动为准）
    applyPrefs(a);
  }

  // 默认只显示真执行过的行。60 秒一轮 = 一小时 60 条，其中绝大多数是
  // 「跳过 / 继续持有」——它们把真正的平仓行冲掉了，等于没得看。
  const allRows = (a.recent || []).slice().reverse();
  const showSkip = !!($('atShowSkip') && $('atShowSkip').checked);
  // 'be' / 'be_fail'（2026-10-29）默认可见：那是程序真的动了券商端的单，
  // 而且一笔持仓最多出现一次——藏进"显示跳过"里，用户就没法一眼确认
  // 「这笔的保本线到底挂上没挂上」。失败尤其要看得见。
  const EXEC_KINDS = ['open', 'close', 'review', 'open_failed', 'stop', 'error',
                      'be', 'be_fail'];
  // 被否决/超时/作废的信号要留在默认视图里（那是"为什么没做"的答案）；
  // 同意的那些不重复占行——open/close 行里已经有 signal_id。
  const rows = showSkip ? allRows : allRows.filter(
    r => EXEC_KINDS.includes(r.kind) || (r.kind === 'signal' && r.answer !== 'approve'));
  if ($('atRowsHint')) {
    $('atRowsHint').textContent = allRows.length
      ? `共 ${allRows.length} 条 · 显示 ${rows.length} 条` : '';
  }
  // ---- 决策流：6 列（时刻 · 动作 · 方向 · p_up 微条 · 盈亏 · 理由）----
  // 原来 11 列里「来源 / 开仓理由 / 平仓理由 / 详情」占 4 列，1280 宽下必须
  // 横向滚才看得到盈亏。来源并进动作徽章的 title（它只在出事时才需要看），
  // 两条理由合成一列的两行，各自带 title 全文。
  const SRC = {
    llm: '模型', fallback: '规则兜底', external: '终端外平仓', panel: '面板',
    sl: '硬止损', be: '券商保本', be_local: '程序保本', tp: '券商止盈',
  };
  const badgeCls = k => (k === 'open' ? 'open'
    : k === 'close' ? 'close'
    : k === 'review' ? 'review'
    : (k === 'open_failed' || k === 'close_failed' || k === 'error') ? 'bad'
    : '');
  // 决策流表格：内容没变就不重画。
  //
  // 为什么：这是每秒一次的轮询，而决策间隔是 60 秒——一分钟里有 59 轮数据
  // 一模一样。原来每轮都 `innerHTML =` 重建 30 行 × 6 列，代价不只是 CPU：
  // 整树重建会清掉文字选中、把滚动位置弹回顶部、鼠标悬停的浮窗当场消失。
  // 判据直接用生成好的 HTML 本身，不另立一份"哪些字段算变化"的规则——
  // 那样两处迟早不一致，而症状是"数字变了界面没变"，比多画几次难查得多。
  const rowsHtml = rows.length ? rows.map(r => {
    const dirTxt = r.direction ? (r.direction > 0 ? '多' : '空') : '—';
    const dirCls = r.direction > 0 ? 'pos-long' : (r.direction < 0 ? 'pos-short' : '');
    const pnl = r.pnl == null ? '—' : money(r.pnl);
    const peakLine = (r.peak_pnl == null) ? ''
      : `<div class="mini" style="opacity:.7">峰 ${money(r.peak_pnl)}</div>`;
    // p_up 微条：中轴 0.50，往哪边偏就往哪边填。数字保留（要能对上账本），
    // 但「偏了多少」交给条——读 0.62 再心算减 0.5 是每轮都要做一遍的负担。
    let putCell = '<span class="mini">—</span>';
    if (r.p_up != null) {
      const d = Number(r.p_up) - 0.5;
      const w = Math.min(50, Math.abs(d) * 100);
      const left = (d >= 0 ? 50 : 50 - w).toFixed(1);
      putCell = `<span class="mini-put"><i style="left:${left}%;width:${w.toFixed(1)}%;`
        + `background:${d >= 0 ? 'var(--up)' : 'var(--down)'}"></i></span>`
        + `<span class="putnum">${fmt(Number(r.p_up), 2)}</span>`;
    }
    // 开仓理由在 open 行；平仓行会把当初的进场理由一起带上，
    // 这样一笔单的「为什么进 / 为什么出」在同一行能看完。
    // 跳过/继续持有行没有 entry_reason，它们的理由就在 reason 里
    // （「浮盈 +6.23 未达止盈门槛 7.00，继续持有」），原来这里只读
    // entry_reason，于是决策流里占绝大多数的这些行理由全是「—」——
    // 表格再高也是空的。平仓行不走这个兜底，否则进场理由和平仓理由会重复。
    const whyIn = r.kind === 'open' ? (r.reason || '')
      : (r.entry_reason
         || ((r.kind === 'skip' || r.kind === 'hold') ? (r.reason || '') : ''));
    let whyOut = '';
    if (r.kind === 'close') whyOut = r.reason || '';
    else if (r.kind === 'review') whyOut = '💡 ' + (r.lesson || r.error || '');
    else if (r.kind === 'be' || r.kind === 'be_fail') whyOut = r.reason || r.error || '';
    const isSkip = r.kind === 'skip' || r.kind === 'hold';
    const srcTitle = [SRC[r.source] || r.source || '', r.ticket ? 'ticket ' + r.ticket : '']
      .filter(Boolean).join(' · ');
    const tCell = (r.ticket && (r.kind === 'open' || r.kind === 'close'))
      ? `<button class="link-btn" onclick="tradeDetailShow(${r.ticket})"
                 title="看这一笔的完整档案">${hhmmss(r.ts)}</button>`
      : hhmmss(r.ts);
    return `<tr${isSkip ? ' class="at-skip"' : ''}>
      <td class="mono">${tCell}</td>
      <td class="tl"><span class="act-badge ${badgeCls(r.kind)}" title="${esc(srcTitle)}">${
        AT_KIND[r.kind] || r.kind || '—'}</span></td>
      <td class="tl ${dirCls}">${dirTxt}</td>
      <td>${putCell}</td>
      <td class="mono ${r.pnl == null ? '' : signClass(r.pnl)}">${pnl}${peakLine}</td>
      <td class="mini why-cell">
        <div class="why-clamp" title="${esc(whyIn)}">${esc(whyIn || '—')}</div>
        ${whyOut ? `<div class="why-clamp why-out" title="${esc(whyOut)}">${esc(whyOut)}</div>` : ''}
        ${r.error ? `<div class="red">${esc(String(r.error).slice(0, 60))}</div>` : ''}
      </td>
    </tr>`;
  }).join('') : `<tr><td colspan="6" class="empty">${
    allRows.length
      ? '这段记录里没有执行过的单。点下面的「跳过 · 继续持有」带子可以看完整决策流水。'
      : '还没有决策记录。点「开始做单」，或先跑 <code>python -m live.autotrade --once</code> 试一轮。'
  }</td></tr>`;

  if (rowsHtml !== atRowsHtml) {
    atRowsHtml = rowsHtml;
    $('rowsAuto').innerHTML = rowsHtml;
  }

  // 跳过行折叠带：固定在滚动区**外面**，永远可见。
  // 放进 <tbody> 会被解析器提到表头下面（<tr> 里不许放 div）；放进滚动区
  // 则滚到底也看不见——两条都试过才定成现在这个位置。
  const band = $('atSkipBand'), skipBtn = $('atSkipShow');
  if (band) {
    const skipN = allRows.filter(r => r.kind === 'skip' || r.kind === 'hold').length;
    const cnt = $('atSkipCnt');
    if (cnt) cnt.textContent = skipN + ' 次';
    band.classList.toggle('hidden', showSkip || !skipN);
    if (skipBtn) {
      skipBtn.textContent = showSkip ? '收起跳过' : '看全量';
      skipBtn.disabled = !skipN;
    }
  }

  // 内容高度会随持仓/空仓变化（差 ~9px），每次渲染后重新分配一次富余空间，
  // 否则开仓那一刻决策流会短一截，底部留一条缝。
  // 决策流搬走之后这个函数在两条页面上都跑：当前是哪个 pane 就量哪个。
  fitActive();
}

