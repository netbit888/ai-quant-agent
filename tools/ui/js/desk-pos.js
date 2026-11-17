/* 刻度尺的轴上限（2026-11-09）
 *
 * 实盘反馈：0.05 手趋势单浮盈跑到 72.47（门槛 35.00），填充条和峰值线双双钉在
 * 右端——过门槛之后无论再赚多少、回吐多少，那一格长得一模一样，"这种表现都没
 * 意义了"。根因是轴上限恒等于 tp：跨过门槛是一瞬间的事，而"满格"要持续几小时。
 *
 * 轴必须随浮盈放大，但**不能用 peak*1.1 这种连续值**：那样填充条永远贴着右端
 * (cur≈peak 是常态)，只是把"满格"换了个定义。改成阶梯：上限取
 * niceCeil(峰值*1.15)，峰值涨到下一档才重排一次，两档之间条子是持续向右长的。
 */
const RULER_STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
function niceCeil(x) {
  if (!(x > 0)) return 0;
  const exp = Math.pow(10, Math.floor(Math.log10(x)));
  const base = x / exp;
  for (let i = 0; i < RULER_STEPS.length; i++) {
    if (base <= RULER_STEPS[i] + 1e-9) return RULER_STEPS[i] * exp;
  }
  return 10 * exp;
}

/* 未过门槛时轴恒为 tp（0 → 门槛，与本版之前逐像素一致，震荡单完全不受影响）；
 * 过了才放大，且留 15% 余量，峰值线永远到不了右端。 */
function rulerTop(tp, hi) {
  if (!(tp > 0)) return 0;
  if (!(hi > tp * 1.005)) return tp;
  return Math.max(tp, niceCeil(hi * 1.15));
}

function renderPos(a, p, mp) {
  const nowS = serverNow(a);
  const plan = p ? (p.plan || 'range') : '';
  const tp = Number((plan === 'trend' ? a.tp_min_trend : a.tp_min_usd) || 0);
  const holdMax = Number((plan === 'trend' ? a.max_hold_trend : a.max_hold_sec) || 0);
  const fp = (a.float_pnl == null) ? null : Number(a.float_pnl);
  const beArm = Number(a.be_arm_usd || 0);
  const beCfg = !!a.be_at_entry && beArm > 0;     // 规则总开关
  // **规则开着 ≠ 这笔有保本**。后端 _be_applies 只认 plan === 'trend'，
  // 震荡单即使峰值过线也不会挂（它的止盈门槛本来就是 3.25，摸到就该模型说话）。
  // UI 不叠这个判断的话，震荡单持仓会亮「保本 触发 3.25」并画一条不存在的线。
  const beFit = !p || String(plan) === 'trend';
  const beOn = beCfg && beFit;
  const peak = (p && p.peak_pnl != null) ? Number(p.peak_pnl) : null;

  const side = $('posSide'), planEl = $('posPlan');
  const setV = (id, text, color) => {
    const e = $(id);
    if (!e) return;
    e.textContent = text;
    e.style.color = color || '';
    e.classList.toggle('idle', text === '—');
  };

  // ---- 手工单（2026-11-16）----
  // 程序自己没仓、但品种上有仓 = 用户在 MT5 里手动开的。这类单在这个面板上
  // 是常态（magic 恒 0），以前做单台直接显示「空仓」，和复盘页的「当前持仓」
  // 互相打脸——用户原话："我自己在 mt5 开单，程序不记录？？？？"
  //
  // 只显示、不接管，所以刻度尺 / 止盈线 / 保本徽章 / 脉冲**一律不画**：
  // 那些线只存在于程序的出场闸门里，画出来等于宣称程序在管这笔单。
  if (!p && mp) {
    if (side) {
      side.className = 'pos-side ' + (mp.direction > 0 ? 'long' : 'short');
      side.textContent = (mp.direction > 0 ? '多' : '空') + ' '
        + (mp.lots == null ? '—' : mp.lots);
    }
    if (planEl) {
      planEl.className = 'pos-plan';
      planEl.textContent = '手工单 · 程序未接管'
        + ((Number(mp.count) || 0) > 1 ? '（同向共 ' + mp.count + ' 笔）' : '');
      planEl.title = '你在 MT5 里自己开的仓。程序不挂券商端止损、不管保本、'
        + '不会超时平仓；出场全靠你自己。';
    }
    setV('posEntry', fmt(mp.price_open, 3));
    const fpm = (mp.float_pnl == null) ? null : Number(mp.float_pnl);
    setV('posPnl', fpm == null ? '—' : money(fpm),
         fpm == null ? '' : (fpm > 0 ? 'var(--up)' : (fpm < 0 ? 'var(--down)' : '')));
    setV('posPeak', '—');
    const heldM = mp.opened_at ? Math.max(0, nowS - Number(mp.opened_at)) : 0;
    setV('posHold', heldM ? durText(heldM) : '—');

    // 峰值浮盈 / ATR 倍数（2026-11-17）
    // 程序仍然**不接管**（不挂券商端 SL、不保本、不超时平仓），但 MFE 必须记。
    // 那笔 4991040502 平在 3883.436，7 秒后 3886.96、当分钟高点约 3888.5，
    // 而面板上峰值是一根横杠——"我刚才是不是跑早了"当场无解。
    // 口径和后端 `_track_peak` / `_peak_atr` 完全一致，不在前端另算一套。
    const munit = Number(mp.atr_pnl || 0);      // 1 个 ATR 折多少浮盈
    const mpeak = (mp.peak_pnl == null) ? null : Number(mp.peak_pnl);
    const mfp = (mp.float_pnl == null) ? null : Number(mp.float_pnl);
    const mmae = (mp.mae_pnl == null) ? null : Number(mp.mae_pnl);
    setV('posPeak', mpeak == null ? '—'
         : money(mpeak) + (munit > 0 ? ' · ' + fmt(mpeak / munit, 1) + '×ATR' : ''),
         mpeak == null ? '' : (mpeak > 0 ? 'var(--good-lit)' : ''));

    // ---- 浮盈刻度尺 ----
    // 参照系是 **5×ATR**（后端 `_BURST_ATR`，脉冲成立的那条线），不是
    // tp_min_usd：手工单没有 plan，也就没有"止盈门槛"这回事，硬套 3.25
    // 会让一笔趋势单看起来早就该跑了。轴顶随峰值放大（rulerTop 的阶梯），
    // "满格多少"写进标签，否则线左移看起来像 bug。
    if (munit > 0) {
      const burstTop = munit * 5;
      const cur0 = (mfp == null ? 0 : mfp);
      const hi0 = Math.max(cur0, mpeak == null ? 0 : mpeak);
      const top0 = rulerTop(burstTop, hi0) || burstTop;
      const pct0 = v => Math.min(100, Math.max(0, (v / top0) * 100));
      const rFillM = $('rFill');
      if (rFillM) {
        rFillM.className = 'r-fill' + (cur0 < 0 ? ' neg' : '');
        rFillM.style.width = pct0(cur0).toFixed(1) + '%';
      }
      const rPeakM = $('rPeak');
      if (rPeakM) {
        if (mpeak != null && mpeak > 0) {
          rPeakM.classList.remove('hidden');
          rPeakM.style.left = pct0(mpeak).toFixed(1) + '%';
        } else rPeakM.classList.add('hidden');
      }
      const rBeM = $('rBe');
      if (rBeM) rBeM.classList.add('hidden');   // 手工单没有程序挂的保本线
      const rTpM = $('rTp');
      if (rTpM) {
        rTpM.classList.remove('hidden');
        const tpPct0 = pct0(burstTop);
        rTpM.style.left = tpPct0.toFixed(1) + '%';
        rTpM.style.right = (tpPct0 < 60) ? 'auto' : '4px';
        const capM = $('rTpCap');
        if (capM) {
          capM.textContent = '脉冲 ' + fmt(burstTop, 2);
          capM.title = '5 个 ATR = ' + fmt(burstTop, 2)
            + '（假设入场即最高点，入场手数下的实际浮盈）';
        }
      }
      if ($('rulerLb')) {
        $('rulerLb').textContent = '浮盈刻度 · 手工单（程序不接管）'
          + (top0 > burstTop + 1e-9 ? ' · 满格 ' + fmt(top0, 2) : '');
      }
      if ($('rulerV')) {
        // 三种状态分开说：还没到 / 到了且在峰值 / **峰值到过但已经回吐**。
        // 第三种是手工单最需要看的一格——"跑早了"和"拿过头"在这里分开：
        // 峰值过 5×ATR 而当前掉下来了，说明曾经有一波没吃满。
        const back0 = (mpeak == null ? 0 : mpeak) - cur0;
        if (cur0 >= burstTop) {
          $('rulerV').textContent = '已过 5×ATR ' + money(cur0)
            + (back0 > 0.005 ? ' · 回吐 ' + fmt(back0, 2) : ' · 在峰值');
          $('rulerV').style.color = 'var(--good-lit)';
        } else if (mpeak != null && mpeak >= burstTop) {
          $('rulerV').textContent = '峰值过 5×ATR · 回吐 ' + fmt(back0, 2);
          $('rulerV').style.color = '';
        } else {
          $('rulerV').textContent = '还差 ' + fmt(burstTop - cur0, 2) + ' 到 5×ATR';
          $('rulerV').style.color = '';
        }
      }
    } else {
      // 算不出 ATR（K 线不足 / 首次观测前）：宁可不画，也不猜一个轴出来。
      ['rBe', 'rTp', 'rPeak'].forEach(id => {
        const e = $(id);
        if (e) e.classList.add('hidden');
      });
      const rFillM = $('rFill');
      if (rFillM) { rFillM.style.width = '0'; rFillM.className = 'r-fill'; }
      if ($('rulerLb')) $('rulerLb').textContent = '浮盈刻度 · 手工单（ATR 未知）';
      if ($('rulerV')) { $('rulerV').textContent = '—'; $('rulerV').style.color = ''; }
    }

    const beEl = $('beBadge');
    // 手工单的保护状态 = 它自己的 SL。程序不接管，所以这一格只有三件事能说：
    // 有没有挂止损、挂了之后最坏亏多少、以及**人有没有把风险放大**。
    // 11-17 那笔手工单把 SL 从 3869.447 挪到 3866.022（30 → 100 美元），
    // 面板当时一个字都没提——这一格就是为那件事准备的。
    const rk = (mp.sl_risk == null) ? null : Number(mp.sl_risk);
    const rk0 = (mp.risk0 == null) ? null : Number(mp.risk0);
    const rmult = (mp.risk_mult == null) ? null : Number(mp.risk_mult);
    const wide = rmult != null && rmult > 1.2;      // 放宽 20% 以上才提示，别为 1 个点刷屏
    const slAtr = (mp.sl_atr == null) ? null : Number(mp.sl_atr);
    if (beEl) {
      beEl.className = 'be-badge ' + ((rk == null || wide) ? 'fail' : 'off');
      beEl.title = '程序不接管：不挂券商端止损、不保本、不会超时平仓'
        + (rk == null
          ? '\n当前没有止损价——打反了没有下限'
          : '\n打到当前 SL 的最坏结果 ' + money(rk)
            + (slAtr != null ? '（离入场 ' + fmt(slAtr, 1) + '×ATR）' : ''))
        + (wide && rk0 != null
          ? '\n开仓时止损 ' + fmt(mp.sl0, 3) + '，最坏 ' + money(rk0)
            + '；现在 ' + fmt(mp.sl, 3) + '，最坏 ' + money(rk) + '。'
            : '')
        + '\n这一格只读：程序不会替你改回去。';
    }
    const beT = $('beText'), beN = $('beNote');
    if (beT) beT.textContent = rk == null ? '手工单 · 无止损' : ('手工单 · 最坏 ' + money(rk));
    if (beN) {
      // 放宽时把两个数摆在一起，否则"最坏 −100"只是一个数字，看不出是谁弄的
      const head = wide && rk0 != null
        ? '⚠ 止损放宽 ' + fmt(rmult, 1) + '×：' + money(rk0) + ' → ' + money(rk)
        : ('SL ' + fmt(mp.sl, 3) + (slAtr != null ? ' · ' + fmt(slAtr, 1) + '×ATR' : ''));
      beN.textContent = head + (mmae == null ? '' : ' · 最深浮亏 ' + money(mmae));
    }

    // ---- 脉冲浮盈（手工单同样适用）----
    // 3 倍起变色、5 倍算脉冲，阈值和后端 `_mark_burst` 严格对齐。
    const burstM = $('posBurst');
    if (burstM) {
      if (munit > 0 && mpeak != null) {
        const multM = mpeak / munit;
        const t3M = (mp.t3 == null) ? null : Number(mp.t3);
        const t5M = (mp.t5 == null) ? null : Number(mp.t5);
        const hitM = t5M != null || multM >= 5;
        const nearM = !hitM && multM >= 3;
        burstM.classList.remove('hidden');
        burstM.className = 'pos-burst' + (hitM ? ' hit' : (nearM ? ' near' : ''));
        burstM.textContent = (hitM ? '脉冲 ' : '') + fmt(multM, 1) + '×ATR';
        burstM.title = '峰值 ' + fmt(multM, 2) + ' 倍 ATR'
          + (t3M == null ? ' · 还没到 3 倍' : ' · 3 倍用了 ' + fmt(t3M, 1) + ' 分')
          + (t5M == null ? ' · 5 倍算脉冲' : ' · 5 倍用了 ' + fmt(t5M, 1) + ' 分')
          + (mp.peak_ts ? '\n峰值时刻 ' + hhmmss(mp.peak_ts) : '')
          + '\n手工单：程序只记录，不平仓、不抬止损';
      } else {
        burstM.classList.add('hidden');
        burstM.textContent = '';
        burstM.title = '';
      }
    }
    return;
  }

  if (p) {
    if (side) {
      side.className = 'pos-side ' + (p.direction > 0 ? 'long' : 'short');
      side.textContent = (p.direction > 0 ? '多' : '空') + ' ' + (p.lots == null ? '—' : p.lots);
    }
    if (planEl) {
      planEl.className = 'pos-plan' + (plan === 'trend' ? ' trend' : '');
      planEl.textContent = PLAN_TXT[plan] || plan;
    }
    setV('posEntry', fmt(p.price_open, 3));
    setV('posPnl', fp == null ? '—' : money(fp),
         fp == null ? '' : (fp > 0 ? 'var(--up)' : (fp < 0 ? 'var(--down)' : '')));
    setV('posPeak', peak == null ? '—'
         : money(peak) + (tp ? ' / ' + fmt(tp, 2) : ''),
         peak == null ? '' : (peak > 0 ? 'var(--good-lit)' : ''));
    const held = p.opened_at ? Math.max(0, nowS - Number(p.opened_at)) : 0;
    setV('posHold', held ? durText(held) + (holdMax ? ' / ' + durText(holdMax) : '') : '—');
  } else {
    if (side) { side.className = 'pos-side none'; side.textContent = '空仓'; }
    if (planEl) { planEl.className = 'pos-plan hidden'; planEl.textContent = ''; }
    setV('posEntry', '—'); setV('posPnl', '—');
    setV('posPeak', '—'); setV('posHold', '—');
  }

  // ---- 脉冲浮盈（2026-11-13）----
  // 趋势单的峰值折成「几倍 ATR」。阈值跟后端 `_mark_burst` 严格对齐：
  // 3 倍开始记速度（t3）、5 倍算脉冲（t5，辅助模式弹窗 / 全自动抬锁利线）。
  // 三态颜色也照这个分：灰=还没到 3 倍、黄=到过 3 倍、亮黄实心=脉冲成立。
  //
  // 换算只用开仓时锁定的 `atr_pnl`（1 个 ATR 折多少浮盈），**不在前端用 atr
  // 现算**——那会和后端变成两套口径，用户看到的是同一笔单的两个数。
  // 旧持仓没有 atr_pnl（升级前开的）：整块隐藏，宁可不显示也不猜。
  const burstEl = $('posBurst');
  if (burstEl) {
    const unit = (p && p.atr_pnl) ? Number(p.atr_pnl) : 0;
    if (!p || String(plan) !== 'trend' || !(unit > 0)) {
      burstEl.classList.add('hidden');
      burstEl.textContent = '';
      burstEl.title = '';
    } else {
      const mult = (peak == null ? 0 : peak) / unit;
      const t3 = (p.t3 == null) ? null : Number(p.t3);
      const t5 = (p.t5 == null) ? null : Number(p.t5);
      const hit = t5 != null || mult >= 5;
      const near = !hit && (t3 != null || mult >= 3);
      burstEl.classList.remove('hidden');
      burstEl.className = 'pos-burst' + (hit ? ' hit' : (near ? ' near' : ''));
      burstEl.textContent = (hit ? '脉冲 ' : '') + fmt(mult, 1) + '×ATR';
      // 头行只放得下「几倍」，剩下的（速度、模式、锁利线在哪）进 title。
      burstEl.title = '峰值 ' + fmt(mult, 2) + ' 倍 ATR'
        + (t3 == null ? ' · 还没到 3 倍' : ' · 3 倍用了 ' + fmt(t3, 1) + ' 分')
        + (t5 == null ? ' · 5 倍算脉冲' : ' · 5 倍用了 ' + fmt(t5, 1) + ' 分')
        + '\n' + (p.assist
          ? '辅助模式：只弹窗提醒，程序既不平仓也不抬止损'
          : (p.lock_ok
            ? '全自动：锁利线已挂券商端 SL ' + fmt(Number(p.lock_sl || 0), 3)
            : '全自动：脉冲成立后把券商端 SL 抬到「峰值 − 1.5 ATR」'));
    }
  }

  // ---- 浮盈刻度尺 ----
  const rBe = $('rBe'), rTp = $('rTp'), rFill = $('rFill'), rPeak = $('rPeak');
  const rulerV = $('rulerV'), rulerLb = $('rulerLb');
  if (!p) {
    if (rulerLb) rulerLb.textContent = '浮盈刻度 · 空仓';
    if (rulerV) { rulerV.textContent = '—'; rulerV.style.color = ''; }
    if (rFill) { rFill.style.width = '0'; rFill.className = 'r-fill'; }
    if (rPeak) rPeak.classList.add('hidden');
    if (rBe) rBe.classList.add('hidden');
    if (rTp) rTp.classList.add('hidden');
  } else if (tp <= 0) {
    // 门槛被关掉时尺子没有参照系——不许画一条不存在的止盈线出来。
    if (rulerLb) rulerLb.textContent = '浮盈刻度 · 止盈门槛已关闭';
    if (rulerV) { rulerV.textContent = fp == null ? '—' : money(fp); rulerV.style.color = ''; }
    if (rFill) { rFill.style.width = '0'; rFill.className = 'r-fill'; }
    if (rPeak) rPeak.classList.add('hidden');
    if (rBe) rBe.classList.add('hidden');
    if (rTp) rTp.classList.add('hidden');
  } else {
    const cur = (fp == null ? 0 : fp);
    // 轴上限：没跨门槛就是 tp，跨了才按峰值阶梯放大（见文件头 rulerTop）
    const hi = Math.max(cur, peak == null ? 0 : peak);
    const top = rulerTop(tp, hi) || tp;
    const pct = v => Math.min(100, Math.max(0, (v / top) * 100));
    const scaled = top > tp + 1e-9;
    if (rFill) {
      rFill.className = 'r-fill' + (cur < 0 ? ' neg' : '');
      rFill.style.width = pct(cur).toFixed(1) + '%';
    }
    if (rPeak) {
      if (peak != null && peak > 0) {
        rPeak.classList.remove('hidden');
        rPeak.style.left = pct(peak).toFixed(1) + '%';
      } else rPeak.classList.add('hidden');
    }
    if (rBe) {
      if (beOn && beArm > 0) {
        rBe.classList.remove('hidden');
        // 未挂上时画虚线：那条线只存在于程序脑子里，不在券商端。
        rBe.className = 'r-line' + (p.be_ok ? '' : ' dash');
        const bePct = pct(beArm);
        rBe.style.left = bePct.toFixed(1) + '%';
        const cap = $('rBeCap');
        if (cap) {
          // 两条线（保本触发 / 复核门槛）在轴上离太近时标签会互相压住
          // （震荡单 be_arm == tp_min 时两线完全重合）。此时只留线，数值看
          // 下方保本徽章的 SL。阈值按门槛标签站在哪一侧区分，见下面 tpAlignLeft。
          const tpPct0 = pct(tp);
          const crowded = (tpPct0 - bePct) < (tpPct0 < 60 ? 12 : 22);
          cap.style.display = crowded ? 'none' : '';
          // **这条线是「触发线」，不是「保本价」**（2026-10-31 用户按刻度尺上的
          // 「保本 3.25」误读成「止损挂在 3.25」）。保本价恒等于开仓价，在浮盈
          // 轴上的位置就是 0；3.25 只决定「浮盈到多少才把 SL 抬上去」。
          // 所以标签必须带「触发」二字，实际 SL 价由下方徽章单独给出。
          cap.textContent = '保本触发 ' + fmt(beArm, 2);
          cap.title = '浮盈到 ' + fmt(beArm, 2) + ' 时把券商端 SL 抬到开仓价 '
            + fmt(Number(p.be_sl || p.price_open), 3);
        }
      } else rBe.classList.add('hidden');
    }
    if (rTp) {
      rTp.classList.remove('hidden');
      // 门槛线不再恒在右端：轴放大后它落在中间，位置必须按同一个轴换算，
      // 否则会出现"填充条已经超过门槛线"的假象（2026-11-09）。
      const tpPct = pct(tp);
      rTp.style.left = tpPct.toFixed(1) + '%';
      // 标签默认贴线左侧（.end 的 right:4px）。线落到左半边时要翻到右侧，
      // 否则会压住左边那根保本触发线的标签。
      const tpAlignLeft = tpPct < 60;
      rTp.style.right = tpAlignLeft ? 'auto' : '4px';
      const cap = $('rTpCap');
      if (cap) {
        // **趋势单这条线不是"到点就平"**：浮盈摸到 tp_min_trend 之后，闸门
        // 只是把决定交回模型、每 tp_reask_sec 秒复核一次（见 _exit_gate 的
        // tp_seen 分支）。写成「止盈 35.00」，人就会拿它当目标价——浮盈 65
        // 还拿着看起来就不正常（2026-11-09 实盘用户原话："好恐怖，这拿得住"）。
        const reask = Number(a.tp_reask_sec || 0);
        const isTrend = plan === 'trend';
        cap.textContent = (isTrend ? '复核门槛 ' : '止盈 ') + fmt(tp, 2);
        cap.title = isTrend
          ? `趋势单：浮盈到 ${fmt(tp, 2)} 不自动平仓，改为每 ${reask || 60} 秒问一次模型`
            + '要不要平（模型说要跑就继续跑，止损由保本线兜着）'
          : `浮盈到 ${fmt(tp, 2)} 才允许平仓（低于这个数不出场）`;
      }
    }
    if (rulerLb) {
      // 轴会自己放大，必须把"满格是多少"写出来，否则门槛线左移看起来像 bug。
      rulerLb.textContent = '浮盈刻度 · 0 = 开仓价'
        + (scaled ? ' · 满格 ' + fmt(top, 2) : '');
    }
    if (rulerV) {
      const gap = tp - cur;
      if (gap > 0) {
        rulerV.textContent = '还差 ' + fmt(gap, 2);
        rulerV.style.color = '';
      } else {
        // 过门槛之后最有用的数字是「从峰值回吐了多少」：保本线只保证不亏，
        // 没有任何规则锁利（trailing 被 227 笔回放否掉了），回吐全靠人看。
        // 原来只写「已过门槛 +72.47」，隔着 3 秒一次的刷新只能靠心算。
        const back = (peak == null ? 0 : peak) - cur;
        rulerV.textContent = '已过门槛 ' + money(cur)
          + (back > 0.005 ? ' · 回吐 ' + fmt(back, 2) : ' · 在峰值');
        rulerV.style.color = 'var(--good-lit)';
      }
    }
  }

  // ---- 保本三态徽章 ----
  const be = $('beBadge');
  if (be) {
    // 入场止损（2026-11-09）：开仓就把硬止损挂到券商端，**和保本无关**。
    // 保本徽章说"抬到开仓价没有"，这里补一句"入场那条线在不在"——
    // 否则用户看到「保本未触发」会以为这笔完全没有券商端保护。
    // be_ok 之后 sl_broker 会同步成保本价，那时不再重复显示。
    const sbPx = p ? Number(p.sl_broker || 0) : 0;
    // 券商端止盈（2026-11-11）：只有非趋势单有这条线（震荡单开仓即挂
    // tp_min_usd 价距）。挂上了就把线在哪写出来——用户看到「已过门槛」时
    // 需要知道是"到线就自动走"还是"还得等模型点头"，这两件事完全不同。
    const tbPx = p ? Number(p.tp_broker || 0) : 0;
    const sbNote = (sbPx > 0
      ? ' · 券商端 SL ' + fmt(sbPx, 3)
      : (p && p.sl_err ? ' · 券商端未挂（' + String(p.sl_err).slice(0, 14) + '）' : ''))
      + (tbPx > 0 ? ' · 券商端 TP ' + fmt(tbPx, 3) : '');
    let bcls = 'off', btxt = '保本止损', bnote = '';
    if (!beCfg) {
      btxt = '保本止损已关闭'; bnote = '设置舱 → 出场闸门可开';
    } else if (p && !beFit) {
      // 规则开着，但这笔是震荡单：它根本没有这条线。
      btxt = '震荡单 · 不挂保本'; bnote = '只给趋势单（止盈门槛 7.00 的那一档才需要）' + sbNote;
    } else if (!p) {
      btxt = '空仓 · 无持仓保护'; bnote = '开仓后按触发线自动挂';
    } else if (p.be_ok) {
      bcls = 'on';
      btxt = '保本已挂券商端';
      bnote = 'SL ' + fmt(Number(p.be_sl || p.price_open), 3) + ' · 最差平开仓价';
    } else if (p.be_arm_ts) {
      // 触发过但改单没成功：这段时间只有 5 秒快查在兜，用户必须知道差别。
      bcls = 'fail';
      btxt = '保本触发但没挂上';
      bnote = '本地兜底：浮盈回 0 由程序代平' + (p.be_err ? '（' + String(p.be_err).slice(0, 18) + '）' : '') + sbNote;
    } else {
      btxt = '保本未触发';
      bnote = '峰值 ' + (peak == null ? '—' : money(peak)) + ' / 触发线 ' + fmt(beArm, 2) + sbNote;
    }
    be.className = 'be-badge ' + bcls;
    const t = $('beText'), n = $('beNote');
    if (t) t.textContent = btxt;
    if (n) n.textContent = bnote;
  }
}

