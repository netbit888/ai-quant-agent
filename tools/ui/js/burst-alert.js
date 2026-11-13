/* ==================== 脉冲浮盈提醒 ====================
 * 后端 `_mark_burst` 在趋势单浮盈**首次**走满 5 个 ATR 时，往 state 的持仓里
 * 写一条 `burst_alert`。这里只负责把它弹出来。
 *
 * 三条硬约束：
 *   1. **不调用任何交易接口**。程序不因为这条提醒改变任何行为——趋势单本来
 *      就不挂 tp，平不平仓仍然由 _exit_gate 判。这是提醒人，不是替人决定。
 *   2. **不额外发请求**。挂在 loadAutoTrade 的轮询回调里，数据是现成的
 *      （和 sigVerdictCheck 同一个位置、同一个道理）。
 *   3. **同一条只弹一次**。判据是 alert 的 ts；用户点过「知道了」之后写进
 *      sessionStorage，页面刷新也不再骚扰——但换一笔单（新 ts）会重新弹。
 *
 * 为什么单独一个遮罩、不复用 #sigMask：信号弹窗停 120 秒等答复，这条只是通知，
 * 两者可能同时出现（信号刚发出、价格又冲了一波）。共用一个遮罩会互相盖掉。
 */
let burstShownTs = 0;   // 上一次弹过的 alert ts，防同一轮轮询反复弹
let burstCurKey = '';   // 当前弹窗对应哪一条，关掉时写 ack 要用

function burstAckKey(al) {
  return 'aqa.burstAck.' + String(al.ticket || '') + '.' + String(al.ts || '');
}

/* 一格统计。数字统一 tabular-nums，和卡片上其它数字对齐。 */
function burstCell(k, v, hot) {
  return '<div class="cell"><span class="k">' + k + '</span>'
    + '<span class="v' + (hot ? ' hot' : '') + '">' + v + '</span></div>';
}

function burstCheck(a) {
  const pos = a && a.position;
  const al = pos && pos.burst_alert;
  if (!al || !al.ts) return;
  if (Number(al.ts) === burstShownTs) return;   // 同一条，轮询每秒都会再读到
  burstShownTs = Number(al.ts);
  burstCurKey = burstAckKey(al);
  if (sessionStorage.getItem(burstCurKey)) return;

  const dir = Number(al.direction) > 0 ? '多' : '空';
  const money = atMoneyTag();   // 美分户口径，钱数不能裸写

  $('burstDesc').textContent =
    '#' + String(al.ticket || '-') + ' · ' + dir
    + (al.lots == null ? '' : ' ' + al.lots + ' 手')
    + (al.price_open == null ? '' : ' · 开仓 ' + fmt(al.price_open, 3))
    + (al.dry_run ? ' · 纸面单' : '');

  const heldMin = pos.opened_at && a.server_ts
    ? Math.max(0, (Number(a.server_ts) - Number(pos.opened_at)) / 60) : null;
  const floatNow = a.float_pnl == null ? null : Number(a.float_pnl);

  $('burstGrid').innerHTML =
    burstCell('走满 5 个 ATR 用了', al.t5 == null ? '—' : fmt(al.t5, 1) + ' 分钟', true)
    + burstCell('峰值浮盈', al.fp == null ? '—' : fmt(al.fp, 2) + money, true)
    + burstCell('峰值 = 几倍 ATR', al.atr_peak == null ? '—' : fmt(al.atr_peak, 2) + ' 倍')
    + burstCell('已回吐',
      (al.fp == null || floatNow == null) ? '—' : fmt(Number(al.fp) - floatNow, 2) + money)
    + burstCell('当前浮盈', floatNow == null ? '—' : fmt(floatNow, 2) + money)
    + burstCell('已持有', heldMin == null ? '—' : fmt(heldMin, 1) + ' 分钟');

  $('burstHint').textContent =
    '离线统计（50 天 M1）：3 分钟内走满 5 个 ATR 的事件，之后 10 分钟中位回吐 '
    + '1.27 个 ATR，15 分钟内最大反向 2.33 个 ATR。程序不会因此平仓——'
    + '要不要落袋由你看盘决定。';
  $('burstMask').classList.remove('hidden');
  // 窗口在后台时也要把人叫醒：和信号弹窗同一套（提窗口 + 提示音）。
  if (typeof raiseWindowForSignal === 'function') raiseWindowForSignal();
  if (typeof beepSignal === 'function') beepSignal();
}

function burstClose() {
  if (burstCurKey) sessionStorage.setItem(burstCurKey, '1');
  $('burstMask').classList.add('hidden');
}

/* 金额单位后缀：美分户的一切金额都是「分」，不标出来会和美元户看成一个数。
   AT_MONEY 由轮询写（见 autotrade.js），读不到就退回空串——宁可少一个后缀，
   也不能让一个异常打断整个轮询。 */
function atMoneyTag() {
  try {
    return AT_MONEY === 'USD' ? '' : ' ' + AT_MONEY;
  } catch (e) {
    return '';
  }
}

document.addEventListener('DOMContentLoaded', function () {
  const btn = $('burstOk');
  if (btn) btn.addEventListener('click', burstClose);
});
