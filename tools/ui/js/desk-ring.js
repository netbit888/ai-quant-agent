/* ============ 做单台 v2 的三个渲染块 ============
   全部只读 a（/api/autotrade 的原样响应），不新增请求、不改 payload 字段。
   它们取代了原来的 5 个 KPI 卡 + 6 个参数胶囊：
   KPI 把「这一轮想了什么」压成一行 16px 副标题；胶囊把整个交易日都不会变的
   阈值当常驻信息占掉一整行——两者都不是做单时真要看的东西。 */
const PLAN_TXT = { trend: '趋势单', range: '震荡单', mid: '方向不明' };
const WAIT_FLAG_TXT = {
  set: '刚定', keep: '继续等', move: '下调', drift: '想上移·已拦',
  expired: '已作废', consumed: '到点未进·作废', cleared_open: '已进场',
  disabled: '趋势市·停用',
};
const GATE_LED_ID = {
  sl: 'ledSl', guard: 'ledGuard', be: 'ledBe',
  tp: 'ledTp', stale: 'ledStale', hold: 'ledHold',
};

function durText(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
  return h ? h + ':' + mm + ':' + ss : m + ':' + ss;
}
function relAge(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分前';
  return Math.floor(sec / 3600) + ' 小时前';
}
function serverNow(a) {
  return Number(a && a.server_ts) || (Date.now() / 1000);
}

/* 一条闸门灯：state = 'on' | 'near' | 'hit' | 'off'。
   正常态**不显示数值**——参数整个交易日不变，显示出来只会把
   「现在有没有在逼近」这件事淹掉。关掉的规则必须显式写「关闭」，
   不许画成一个存在的阈值（这是这个面板最容易撒的谎）。 */
function setLed(key, state, value, bad) {
  const box = $(GATE_LED_ID[key]);
  if (!box) return;
  box.className = 'gate-led' + (state === 'on' ? '' : ' ' + state) + (bad ? ' bad' : '');
  const v = $('led' + key.charAt(0).toUpperCase() + key.slice(1) + 'V');
  if (!v) return;
  if (state === 'on') v.textContent = '';
  // off 态默认写「关闭」；传了 value 就用 value（例如「不适用」——规则开着但
  // 这笔单没有这条线，写「关闭」会让人去设置舱里找一个根本没关的开关）。
  else if (state === 'off') v.textContent = (value == null ? '关闭' : value);
  else v.textContent = (value == null ? '' : value);
}

function renderRing(a, p) {
  const ld = a.last_decision;
  const ring = $('ring');
  const nowS = serverNow(a);

  // ---- 动作大字 ----
  let word = '—', cls = 'hold', sub = '还没跑过';
  if (ld) {
    if (ld.kind === 'entry') {
      if (ld.act === false) {
        word = '不做'; cls = 'hold'; sub = '模型主动放弃';
      } else if (ld.act === true) {
        word = ld.direction > 0 ? '做多' : '做空';
        cls = ld.direction > 0 ? 'long' : 'short';
        sub = 'p_up ' + fmt(Number(ld.p_up) * 100, 0) + '%';
      } else {
        // act 缺失是旧 state 的形态：只有 p_up 没有明确表态。
        word = ld.direction > 0 ? '偏多' : '偏空';
        cls = 'hold'; sub = '未明确表态';
      }
    } else {
      word = ld.close ? '平仓' : '继续持有';
      cls = 'hold'; sub = '出场评估';
    }
  }
  const actEl = $('ringAct');
  if (actEl) actEl.className = 'ring-act ' + cls;
  const wordEl = $('ringWord');
  if (wordEl) wordEl.textContent = word;
  const subEl = $('ringActSub');
  if (subEl) subEl.textContent = sub;

  // ---- 时刻：相对时间 + 绝对时间做 title ----
  const tsEl = $('ringTs');
  if (tsEl) {
    if (ld && ld.ts) {
      const t = Number(ld.ts);
      tsEl.textContent = relAge(nowS - t) + ' · ' + hhmmss(t);
      tsEl.title = new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
    } else {
      tsEl.textContent = '—';
      tsEl.title = '';
    }
  }

  // ---- p_up 双向条：中轴 0.50 + 两条阈值刻线 ----
  const minEdge = Number((a.cfg && a.cfg.min_edge) || 0.06) || 0.06;
  const thrLo = $('putThrLo'), thrHi = $('putThrHi');
  if (thrLo) thrLo.style.left = ((0.5 - minEdge) * 100).toFixed(1) + '%';
  if (thrHi) thrHi.style.left = ((0.5 + minEdge) * 100).toFixed(1) + '%';
  const fill = $('putFill'), vv = $('putV'), edge = $('putEdge');
  const pu = (ld && ld.p_up != null) ? Number(ld.p_up) : null;
  if (fill && vv && edge) {
    if (pu == null || Number.isNaN(pu)) {
      fill.style.left = '50%'; fill.style.width = '0';
      vv.textContent = '—'; vv.style.color = '';
      edge.textContent = '';
    } else {
      const dev = pu - 0.5;
      const w = Math.min(50, Math.abs(dev) * 100);
      fill.style.left = (dev >= 0 ? 50 : 50 - w) + '%';
      fill.style.width = w + '%';
      fill.className = 'put-fill' + (dev < 0 ? ' bear' : '');
      vv.textContent = (dev >= 0 ? '+' : '−') + fmt(Math.abs(dev), 2);
      vv.style.color = dev >= 0 ? 'var(--up)' : 'var(--down)';
      edge.textContent = (Math.abs(dev) >= minEdge ? '过阈值 ' : '未达阈值 ')
        + fmt(minEdge, 2);
    }
  }

  // ---- 理由原话（视觉限 3 行，title 给全文）----
  const why = $('ringWhy');
  if (why) {
    const reason = ld ? String(ld.reason || '').replace(/\s+/g, ' ').trim() : '';
    why.textContent = reason
      || (ld ? '（模型没给理由）' : '还没跑过。点「开始做单」后这一格会显示这一轮的理由原话。');
    why.title = reason;
  }

  // ---- 程序建议 / 否决留痕 ----
  // 2026-10-28 起程序默认只建议不拦：有建议却没拦 = 模型否决了它。
  // 这一格是事后按「听了 / 没听」分桶算期望的唯一依据，不能只在 title 里。
  const tagEl = $('ringTag'), adv = $('ringAdv'), advTxt = $('ringAdvTxt');
  const adviceText = ld ? String(ld.advice_text || '').replace(/\s+/g, ' ').trim() : '';
  if (tagEl) {
    if (ld && ld.veto) {
      tagEl.textContent = '程序拦下 · ' + String(ld.veto);
      tagEl.className = 'ring-tag bad';
    } else if (ld && ld.advice) {
      tagEl.textContent = '模型否决建议 · 程序未拦';
      tagEl.className = 'ring-tag';
    } else {
      tagEl.textContent = '';
      tagEl.className = 'ring-tag hidden';
    }
  }
  if (ring) ring.classList.toggle('veto', !!(ld && ld.advice && !ld.veto));
  if (adv && advTxt) {
    if (adviceText && ld && (ld.advice || ld.veto)) {
      // 正文和尾部结论之间必须有分隔：原来「…再进，别追高未采纳（当前配置
      // 只建议、不拦截）」在窄屏会连成一句，读不出哪部分是程序的话。
      advTxt.innerHTML = '<b>程序建议：</b>' + esc(adviceText)
        + (ld.veto ? '<b> · 已拦截</b>'
                   : '<b> · 未采纳</b>（当前配置只建议、不拦截）');
      adv.classList.remove('hidden');
    } else {
      advTxt.innerHTML = '';
      adv.classList.add('hidden');
    }
  }

  // ---- 等待锚点 ----
  const anchor = $('ringAnchor');
  const w = ld && ld.wait;
  if (anchor) {
    if (w && w.level) {
      anchor.classList.remove('hidden');
      const lv = $('anchorLv');
      if (lv) lv.textContent = Number(w.level).toFixed(3);
      const ago = $('anchorAgo');
      if (ago) {
        const flag = WAIT_FLAG_TXT[ld.wait_flag] || '';
        const locked = w.ts ? '已锁 ' + durText(nowS - Number(w.ts)) : '';
        ago.textContent = [flag, locked].filter(Boolean).join(' · ');
      }
      const drift = $('anchorDrift');
      const isDrift = ld.wait_flag === 'drift';
      if (drift) drift.classList.toggle('hidden', !isDrift);
      anchor.classList.toggle('drift', isDrift);
    } else {
      anchor.classList.add('hidden');
    }
  }

  // ---- 本轮上下文：当前态势大字 + 模型/我方向对照（2026-11-05）----
  // 旧直方图删了：柱高被 min_edge 截断后八成信号同高，且决策环已有本轮
  // p_up、决策流页已有逐行微条，第三份重复不改变任何决定。
  // 打法由后端按 regime + cfg 现算（空仓也有具体文案），前端不拼口径。
  const pb = a.playbook || null;
  const stateEl = $('ctxState');
  if (stateEl) {
    const regEl = $('ctxRegime'), playEl = $('ctxPlay');
    const staleEl = $('ctxStale'), evEl = $('ctxEv');
    if (pb) {
      if (regEl) regEl.textContent = pb.name || '待判定';
      if (playEl) playEl.textContent = pb.action || '等待首轮判定';
      if (evEl) evEl.textContent = (pb.detail || '')
        + ' · 间隔 ' + (Number(pb.interval_sec) || 60) + 's';
      stateEl.classList.toggle('stale', !!pb.stale);
      if (staleEl) {
        const age = (ld && ld.ts) ? relAge(nowS - Number(ld.ts)) : '—';
        staleEl.textContent = (pb.stale ? '状态已过期 · ' : '本轮 ') + age;
        staleEl.className = 'ctx-stale' + (pb.stale ? '' : ' fresh');
      }
    } else {
      if (regEl) regEl.textContent = '待判定';
      if (playEl) playEl.textContent = '等待首轮判定';
      if (evEl) evEl.textContent = '还没跑过 · 点「开始做单」后这里显示当前打法';
      stateEl.classList.remove('stale');
      if (staleEl) staleEl.className = 'ctx-stale fresh hidden';
    }
  }

  // 「模型 / 我」8 个固定格位：格位不随数据量伸缩，缺轮留空（右对齐）。
  // 色块只编码方向（多/空/无），不做长度比较；逆模型格描金边。
  // title 带时刻 / p_up / ticket / 手动或程序 / 盈亏，每个数字都能对账。
  const vs = a.vs_actual || null;
  const modelGrid = $('cmpModel'), mineGrid = $('cmpMine'), cmpNote = $('cmpNote');
  const CMP_N = 8;
  if (modelGrid && mineGrid) {
    const slots = (vs && vs.slots) ? vs.slots.slice(-CMP_N) : [];
    const pad = CMP_N - slots.length;
    const build = (mine) => {
      let out = '';
      for (let i = 0; i < CMP_N; i++) {
        const s = i >= pad ? slots[i - pad] : null;
        if (!s) {
          out += '<div class="cmp-cell miss" title="这一轮没有决策记录"></div>';
          continue;
        }
        const dir = Number(mine ? s.actual_dir : s.model_dir) || 0;
        const kind = dir > 0 ? 'up' : (dir < 0 ? 'down' : 'none');
        const against = (mine && s.against) ? ' against' : '';
        let tip = fmtTsMin(Number(s.ts)) + ' · p_up ' + fmt(s.p_up, 2);
        if (mine) {
          tip += dir
            ? ' · 成交' + (dir > 0 ? '多' : '空')
              + ' · ticket ' + (s.ticket || '—')
              + ' · ' + (Number(s.magic || 0) === 0 ? '手动' : '程序')
              + (s.pnl == null ? ' · 未平仓' : ' · 盈亏 ' + s.pnl)
            : ' · 未成交';
        } else {
          tip += ' · 模型' + (dir > 0 ? '看多' : (dir < 0 ? '看空' : '中性'));
        }
        out += '<div class="cmp-cell ' + kind + against
          + '" title="' + esc(tip) + '"></div>';
      }
      return out;
    };
    modelGrid.innerHTML = build(false);
    mineGrid.innerHTML = build(true);
  }
  if (cmpNote) {
    if (vs && vs.slots && vs.slots.length) {
      const n = Number(vs.against || 0);
      cmpNote.className = 'cmp-note' + (n ? '' : ' zero');
      cmpNote.innerHTML = n
        ? '<b>逆模型 ' + n + ' 笔</b> · 净 <b>' + fmt(vs.net, 2)
          + '</b> · 最近 ' + vs.slots.length + ' 轮成交 '
          + Number(vs.trades || 0) + ' 笔'
        : '最近 ' + vs.slots.length + ' 轮 · 成交 '
          + Number(vs.trades || 0) + ' 笔 · <b>无逆模型</b>';
    } else {
      cmpNote.className = 'cmp-note zero';
      cmpNote.textContent = '还没有决策记录';
    }
  }

  // ---- 环头两个计数 ----
  // act 2026-11-05 不再上环头：右栏的 24px「不做」+ 副标题已经表达，三份同一信息。
  const setTxt = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  const pend = Number(a.pending_reviews || 0);
  setTxt('fPend', pend ? pend + ' 笔' : '—');
}

