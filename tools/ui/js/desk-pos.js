function renderPos(a, p) {
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
    if (rFill) {
      rFill.className = 'r-fill' + (cur < 0 ? ' neg' : '');
      rFill.style.width = Math.min(100, Math.max(0, (cur / tp) * 100)).toFixed(1) + '%';
    }
    if (rPeak) {
      if (peak != null && peak > 0) {
        rPeak.classList.remove('hidden');
        rPeak.style.left = Math.min(100, (peak / tp) * 100).toFixed(1) + '%';
      } else rPeak.classList.add('hidden');
    }
    if (rBe) {
      if (beOn && beArm > 0) {
        rBe.classList.remove('hidden');
        // 未挂上时画虚线：那条线只存在于程序脑子里，不在券商端。
        rBe.className = 'r-line' + (p.be_ok ? '' : ' dash');
        const bePct = Math.min(100, (beArm / tp) * 100);
        rBe.style.left = bePct.toFixed(1) + '%';
        const cap = $('rBeCap');
        if (cap) {
          // 线贴到右端时标签会撞上止盈标签并溢出卡片（震荡单 be_arm == tp_min
          // 时两线完全重合）。此时只留线，数值看下方保本徽章的 SL。
          const crowded = bePct > 70;
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
      rTp.style.left = '100%';
      const cap = $('rTpCap');
      if (cap) cap.textContent = '止盈 ' + fmt(tp, 2);
    }
    if (rulerLb) rulerLb.textContent = '浮盈刻度 · 0 = 开仓价';
    if (rulerV) {
      const gap = tp - cur;
      rulerV.textContent = gap > 0 ? '还差 ' + fmt(gap, 2) : '已过门槛 ' + money(cur);
      rulerV.style.color = gap > 0 ? '' : 'var(--good-lit)';
    }
  }

  // ---- 保本三态徽章 ----
  const be = $('beBadge');
  if (be) {
    let bcls = 'off', btxt = '保本止损', bnote = '';
    if (!beCfg) {
      btxt = '保本止损已关闭'; bnote = '设置舱 → 出场闸门可开';
    } else if (p && !beFit) {
      // 规则开着，但这笔是震荡单：它根本没有这条线。
      btxt = '震荡单 · 不挂保本'; bnote = '只给趋势单（止盈门槛 7.00 的那一档才需要）';
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
      bnote = '本地兜底：浮盈回 0 由程序代平' + (p.be_err ? '（' + String(p.be_err).slice(0, 18) + '）' : '');
    } else {
      btxt = '保本未触发';
      bnote = '峰值 ' + (peak == null ? '—' : money(peak)) + ' / 触发线 ' + fmt(beArm, 2);
    }
    be.className = 'be-badge ' + bcls;
    const t = $('beText'), n = $('beNote');
    if (t) t.textContent = btxt;
    if (n) n.textContent = bnote;
  }
}

