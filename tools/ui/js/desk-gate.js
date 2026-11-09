/* 闸门指示灯 + 设置舱参数表。
   做单台那 6 盏灯只回答「有没有逼近」；设置舱那张表显示实际生效值。
   两处都读同一份 cfg——页面写 3.25 而实际跑 0（关掉）是最坏的一种谎。 */
function renderGateLeds(a, p) {
  const nowS = serverNow(a);
  const plan = p ? (p.plan || 'range') : 'range';
  const sl = Number(a.sl_usd || 0);
  const tp = Number((plan === 'trend' ? a.tp_min_trend : a.tp_min_usd) || 0);
  const holdMax = Number((plan === 'trend' ? a.max_hold_trend : a.max_hold_sec) || 0);
  const guard = Number(a.guard_sec || 0);
  const iv = Number((a.cfg && a.cfg.interval_sec) || 0) || 60;
  const staleOn = !!a.stale_wake && Number(a.stale_sec || 0) > 0;
  const beArm = Number(a.be_arm_usd || 0);
  const beCfg = !!a.be_at_entry && beArm > 0;
  // 空仓时保留灰态常驻（规则开着）；持仓才按 plan 判这一笔有没有这条线。
  const beFit = !p || String(plan) === 'trend';
  const beOn = beCfg && beFit;
  const fp = (a.float_pnl == null) ? null : Number(a.float_pnl);
  const peak = (p && p.peak_pnl != null) ? Number(p.peak_pnl) : null;
  const held = (p && p.opened_at) ? Math.max(0, nowS - Number(p.opened_at)) : 0;
  const touched = Number(a.tp_reask_sec || 0);

  const near = [];
  const hit = [];
  const mark = (key, state, label, value, bad) => {
    setLed(key, state, value, bad);
    if (state === 'near') near.push(label);
    if (state === 'hit') hit.push(label);
  };

  // 硬止损：浮亏到线由程序直接市价平，不问模型。
  if (sl > 0) {
    if (p && fp != null && fp <= -sl) mark('sl', 'hit', '硬止损', '−' + fmt(sl, 2), true);
    else if (p && fp != null && fp <= -sl * 0.7) mark('sl', 'near', '硬止损', '−' + fmt(sl, 2));
    else mark('sl', 'on');
  } else mark('sl', 'off');

  // 止损快查：配得 >= 决策间隔时 _wait_next 会夹到间隔，等于每轮只查一次，
  // 那就是旧行为——显示「关闭」才诚实。
  const guardOn = guard > 0 && guard < iv;
  mark('guard', guardOn ? 'on' : 'off', '止损快查', fmt(guard, 0) + 's');

  // 保本：已挂 = 券商端有保护；触发未挂上 = 只有本地兜底在撑。
  // 值一律写**状态**，不写 3.25：这一格写数字时会被读成「保本价 = 3.25」
  // （2026-10-31 用户就是这么读的）。触发线只在未触发态出现，且必须带「触发」。
  if (beOn) {
    if (p && p.be_ok) mark('be', 'hit', '保本', '已挂');
    else if (p && p.be_arm_ts) mark('be', 'near', '保本', '未挂上');
    else if (p && peak != null && peak >= beArm * 0.9) mark('be', 'near', '保本', '触发 ' + fmt(beArm, 2));
    else mark('be', 'on', '保本');
  } else if (beCfg) {
    // 规则开着但这笔是震荡单：这条线对这笔单不成立，不能写「关闭」。
    mark('be', 'off', '保本', '不适用');
  } else mark('be', 'off', '保本');

  // 止盈门槛：过线只有一件事——模型被问过要不要落袋。
  if (tp > 0) {
    if (p && p.tp_reached_ts) mark('tp', 'hit', '止盈门槛', fmt(tp, 2));
    else if (p && fp != null && fp >= tp * 0.9) mark('tp', 'near', '止盈门槛', fmt(tp, 2));
    else mark('tp', 'on');
  } else mark('tp', 'off');

  // 停滞唤醒：峰值停住且吐回一截 → 该把决定权交回模型了。
  const frac = Number(a.stale_frac || 0) || 0.6;
  if (staleOn) {
    const stalled = p && peak != null && fp != null && peak > 0
      && (peak - fp) >= peak * frac;
    mark('stale', stalled ? 'near' : 'on', '停滞唤醒',
         fmt(Math.round(Number(a.stale_sec) / 60), 0) + ' 分');
  } else mark('stale', 'off', '停滞唤醒', fmt(Math.round(Number(a.stale_sec) / 60), 0) + ' 分');

  // 持仓上限：到点无条件平。剩 20% 时转黄。
  if (holdMax > 0) {
    const left = holdMax - held;
    if (p && held >= holdMax) mark('hold', 'hit', '持仓上限', '到点', true);
    else if (p && held >= holdMax * 0.8) mark('hold', 'near', '持仓上限', '剩 ' + durText(left));
    else mark('hold', 'on');
  } else mark('hold', 'off');

  // 环脚：一眼看到"有没有闸门在逼近"
  const fGate = $('fGate');
  if (fGate) {
    fGate.textContent = hit.length ? hit.join('·') + ' 已触发'
      : (near.length ? near.join('·') + ' 逼近' : '未触发');
    fGate.style.color = hit.length ? 'var(--warn)' : (near.length ? 'var(--warn)' : '');
  }

  // ---- 设置舱那张参数表：显示实际生效值，0 一律写「关闭」----
  // 金额一律带货币单位（2026-11-09）：手数可选之后这几格会变（0.01 手 −3.00、
  // 0.05 手 −15.00），而美分户的 15.00 是 15 分不是 15 美元。只写数字时
  // 「−15.00」会被读成美元，差 100 倍；单位必须和数字一起出现。
  const mu = a.money_unit || 'USD';
  const put = (id, txt, on) => {
    const e = $(id);
    if (e) e.textContent = txt;
  };
  put('atGateSlV', sl > 0 ? '−' + fmt(sl, 2) + ' ' + mu : '关闭');
  if ($('atGateSl')) $('atGateSl').className = 'at-gate sl' + (sl > 0 ? '' : ' off');
  put('atGateGuardV', guardOn ? '每 ' + fmt(guard, 0) + ' 秒' : '关闭');
  if ($('atGateGuard')) $('atGateGuard').className = 'at-gate guard' + (guardOn ? '' : ' off');
  // 保本止损这一行的生效值是**触发线**，不是保本价。写「3.25 · 仅趋势单」时
  // 用户会读成「止损挂在 3.25」（2026-10-31 原话）。所以把两件事分开写：
  // 触发条件（浮盈到 3.25）+ 落点（SL 抬到开仓价，有持仓时就是这笔的实际价）。
  const beSlNow = (p && Number(p.be_sl || 0) > 0) ? Number(p.be_sl)
    : ((p && Number(p.price_open || 0) > 0) ? Number(p.price_open) : 0);
  put('atGateBeV', !beCfg ? '关闭'
    : (beFit
        ? '触发 ' + fmt(beArm, 2) + ' ' + mu + ' → SL ' + (beSlNow > 0 ? fmt(beSlNow, 3) : '开仓价')
        // 规则开着、当前这笔是震荡单：写清楚是「不适用」，不是「关闭」。
        : '触发 ' + fmt(beArm, 2) + ' ' + mu + ' · 仅趋势单'));
  if ($('atGateBe')) {
    $('atGateBe').className = 'at-gate be' + (beCfg ? '' : ' off');
    $('atGateBe').title = '趋势单浮盈摸到 ' + fmt(beArm, 2) + ' 就把券商端硬止损抬到'
      + '开仓价（不是抬到 ' + fmt(beArm, 2) + '——那是触发线，单位是美元浮盈）。'
      + '挂在券商那边，进程崩了、面板关了都还有效，成交就在开仓价上——'
      + '轮询式止损实测超调 13%。挂不上时由 5 秒快查在浮盈回到 0 时代平';
  }
  put('atGateTpV', tp > 0 ? fmt(tp, 2) + ' ' + mu : '关闭');
  if ($('atGateTp')) $('atGateTp').className = 'at-gate tp' + (tp > 0 ? '' : ' off');
  const staleSec = Number(a.stale_sec || 0);
  put('atGateStaleV', staleOn ? '每 ' + fmt(Math.round(staleSec / 60), 0) + ' 分钟' : '关闭');
  if ($('atGateStale')) $('atGateStale').className = 'at-gate' + (staleOn ? '' : ' off');
  put('atGateHoldV', holdMax > 0 ? fmt(Math.round(holdMax / 60), 0) + ' 分钟' : '关闭');
  if ($('atGateHold')) $('atGateHold').className = 'at-gate' + (holdMax > 0 ? '' : ' off');
  // 趋势单那套参数单独列出来：不写的话「震荡单 3.25 / 趋势单 7.0」这件事
  // 只能靠记忆，而持仓面板显示的永远是当前这一笔的那一档。
  put('atGateTpTrend', Number(a.tp_min_trend || 0) > 0
      ? '趋势单 ' + fmt(a.tp_min_trend, 2) + ' ' + mu : '趋势单已关闭');
  put('atGateHoldTrend', Number(a.max_hold_trend || 0) > 0
      ? '趋势单 ' + fmt(Math.round(Number(a.max_hold_trend) / 60), 0) + ' 分钟'
      : '趋势单已关闭');
  if ($('fGate') && touched && p && p.tp_reached_ts && !hit.length && !near.length) {
    $('fGate').textContent = '过门槛 · ' + touched + 's 复核';
  }
}

