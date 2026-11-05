/* ==================== 交易评分 ==================== */
const GRADE_TEXT = {
  A: '达标', B: '接近达标', C: '一项不达标', D: '多项不达标',
  F: '严重不达标', insufficient: '样本不足',
};

function renderScoring(s) {
  if (!s || !s.dimensions || !s.dimensions.length) {
    $('scoreGrade').className = 'grade-badge g-insufficient';
    $('scoreGrade').textContent = '—';
    $('scoreLabel').textContent = s && s.error ? '评分不可用' : '暂无评分';
    $('scoreSub').textContent = (s && s.error) || '';
    $('scoreRed').innerHTML = '';
    $('beValue').textContent = '—';
    $('beSub').textContent = '';
    $('scoreDims').innerHTML = '';
    $('scoreNote').textContent = '';
    return;
  }

  const g = s.grade || 'insufficient';
  $('scoreGrade').className = 'grade-badge g-' + g;
  $('scoreGrade').textContent = g === 'insufficient' ? 'N/A' : g;
  $('scoreLabel').textContent = `${GRADE_TEXT[g] || g}　·　整体取最差维度（木桶原理，不做加权平均）`;

  const parts = [];
  if (s.score != null) parts.push(`参考分 ${s.score}`);
  if (s.avg_trade_score != null) parts.push(`单笔均分 ${fmt(s.avg_trade_score, 1)}`);
  if (s.profile) parts.push(`${s.profile.symbol || ''} ${s.profile.timeframe || ''}`);
  $('scoreSub').textContent = parts.join(' · ');

  $('scoreRed').innerHTML = (s.red_flags || []).map(f => `⚠ ${f}`).join('<br>');

  // 盈亏平衡边际：不需要外部基准，由你自己的盈亏比决定
  const be = s.breakeven || {};
  if (be.margin_pp != null) {
    $('beValue').textContent = `${be.margin_pp > 0 ? '+' : ''}${fmt(be.margin_pp, 1)} pp`;
    $('beValue').className = 'v ' + (be.margin_pp >= 0 ? 'pos' : 'neg');
    $('beSub').textContent =
      `盈亏比 ${be.rr} → 需胜率 ${fmt(be.required_win_rate, 1)}%，实际 ${fmt(be.actual_win_rate, 1)}%`;
  } else {
    $('beValue').textContent = '—';
    $('beValue').className = 'v';
    $('beSub').textContent = '缺少盈亏比，无法计算平衡线';
  }

  $('scoreDims').innerHTML = (s.dimensions || []).map(x => `
    <div class="dim-row">
      <span class="chip c-${x.grade}">${x.grade}</span>
      <span class="nm">${x.label}</span>
      <span class="val">${x.display}</span>
      <span class="tg">目标 ${x.target}</span>
      <span class="nt">${x.note || ''}</span>
    </div>`).join('');

  const plan = s.plan || {};
  let planTxt = '';
  if (plan.avg_planned_rr) {
    planTxt = `计划盈亏比均值 ${plan.avg_planned_rr} · 实际实现 ${plan.realized_rr}`;
  } else if (plan.note) {
    planTxt = plan.note;
  }
  $('scoreNote').textContent = planTxt;

  renderBeTable(be);
}

// 事后口径的平衡线速查：不同盈亏比需要多少胜率
function renderBeTable(be) {
  const box = $('beTable');
  if (!box) return;
  const rows = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0].map(rr => {
    const need = 100 / (1 + rr);
    const diff = (be.actual_win_rate || 0) - need;
    const cls = diff >= 0 ? 'pos' : 'neg';
    return `<tr class="${Math.abs((be.rr || 0) - rr) < 1e-6 ? 'current' : ''}">
      <td class="mono">${rr.toFixed(2)}</td>
      <td class="mono">${need.toFixed(1)}%</td>
      <td class="mono ${cls}">${diff > 0 ? '+' : ''}${diff.toFixed(1)} pp</td>
    </tr>`;
  }).join('');
  box.innerHTML = rows;
}


