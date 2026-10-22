"""交易评分：把「打得怎么样」变成可复现、可分解、可比较的数字。

设计原则（为什么不是简单加总）
------------------------------
1. **可复现**：纯规则计算，同一份数据永远得同一个分数。LLM 的 verdict 不进分数，
   只作为文字解释——它两次可能给不同结论。
2. **可分解**：总分能拆到维度，知道扣分扣在哪一项。
3. **可比较**：用 R 归一化，不受手数、账户规模影响。
4. **有基线**：核心维度是「离盈亏平衡线还有多远」——这个基准由你自己的盈亏比
   和品种点差结构决定，**不需要外部数据**。市面上任何"M1 剥头皮 PF 1.5~2.0"
   的说法都没有可验证的成交记录，不能当基准。
5. **抗操纵**：
   - 用 R 不用美元 → 加大仓位提不了分
   - 笔数多不加分 → 刷单提不了分
   - 小样本降级为 `insufficient` 而不是给低分 → 样本不足是「不知道」，不是「差」

两层评分
--------
- **单笔（0-100）**：复盘用，找出坏交易。四个维度加权，权重写在常量里可审计。
- **账户（A~F）**：六个维度各自定级，**整体取最差的一项**（木桶原理），
  不做加权平均——「期望 0.3R」和「回撤 50%」不可交换，平均成 72 分是伪科学。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from live.profile import breakeven_win_rate, load_profile

ROOT = Path(__file__).resolve().parent.parent

# 单笔评分的维度权重（合计 100）。改这里必须同步改文档，否则分数不可比。
TRADE_WEIGHTS = {"result": 40, "discipline": 30, "risk": 20, "duration": 10}

LETTER_SCORE = {"A": 100, "B": 85, "C": 70, "D": 55, "F": 30}


# ------------------------------------------------------------------ 定级工具


def _grade_high(v: float, cuts: list[tuple[float, str]]) -> str:
    """越大越好的指标。cuts 从高到低，低于最后一档为 F。"""
    for thr, g in cuts:
        if v >= thr:
            return g
    return "F"


def _grade_low(v: float, cuts: list[tuple[float, str]]) -> str:
    """越小越好的指标。"""
    for thr, g in cuts:
        if v <= thr:
            return g
    return "F"


# ------------------------------------------------------------------ 单笔评分


def _plan_rr(plan: dict | None) -> float | None:
    """计划盈亏比 = |TP − 入场| / |入场 − SL|。需要持仓快照里有开仓时的 SL/TP。"""
    if not plan:
        return None
    entry = plan.get("price_open")
    sl = plan.get("sl")
    tp = plan.get("tp")
    if not entry or not sl or not tp:
        return None
    risk = abs(float(entry) - float(sl))
    reward = abs(float(tp) - float(entry))
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 2)


def score_trade(
    t: dict, risk_unit: float, balance_before: float | None, plan: dict | None = None
) -> dict:
    """给一笔已平仓交易打分，返回维度明细。

    维度：
      result      结果：按 R 分档。这是唯一和运气直接相关的维度，所以权重不是最高。
      discipline  纪律：按离场方式。计划内止盈 > 按计划止损 > 手动 = 中性 >
                  穿仓强平。手动平仓不扣分——手动交易者市价进出是常态。
      risk        风险：亏损幅度按 R 分档（>2R 才算失控）。盈利单没有超风险证据，给满分。
      duration    时长：过短的持仓在黄金点差上成本占比过高。
    """
    profit = float(t.get("profit") or 0.0)
    r = profit / risk_unit if risk_unit else 0.0

    if r >= 2.0:
        result = TRADE_WEIGHTS["result"]
    elif r >= 1.0:
        result = 34
    elif r > 0:
        result = 24
    elif r > -1.0:
        result = 12
    else:
        result = 4

    reason = (t.get("close_reason") or "manual").lower()
    if reason == "tp":
        discipline = 30
    elif reason == "sl":
        discipline = 22
    elif reason == "close_by":
        discipline = 15
    elif reason == "stop_out":
        discipline = 0
    else:
        # 手动平仓本身不扣分。用户是手动交易者，市价进出就是常态——
        # 以前赢 12 输 8（满分 30），等于给所有手动单预扣一半分。
        # 真正该扣的是失控证据：穿仓（0 分）和单笔风险超限（risk 维度）。
        discipline = 20

    flags: list[str] = []
    risk_pct = None
    if profit < 0 and balance_before and balance_before > 0:
        risk_pct = abs(profit) / balance_before * 100

    # 亏损幅度按 R 分档（与账户纪律维度同口径）。
    # 不能用「余额 5%」：12 USD 的微型账户做 0.01 手黄金，每笔亏损必然 >8%，
    # 按余额百分比定档会把这类账户的每一笔都判 0 分——那是账户规模问题，
    # 不是单笔失控。真正的失控是「这一笔亏得远超你平时的单笔亏损」。
    if profit < 0:
        r_loss = abs(r)
        if r_loss <= 1.0:
            risk = 20
        elif r_loss <= 1.5:
            risk = 15
        elif r_loss <= 2.0:
            risk = 8
        else:
            risk = 0
        if risk_pct is not None and risk_pct > 10:
            flags.append(f"单笔亏损占余额 {risk_pct:.1f}%（账户规模与最小手数不匹配）")
    else:
        risk = 20

    if reason == "stop_out":
        flags.append("穿仓强平")

    hold = None
    if t.get("exit_time") and t.get("entry_time"):
        hold = (t["exit_time"] - t["entry_time"]) / 60
    if hold is None:
        duration = 10
    elif hold >= 5:
        duration = 10
    elif hold >= 1:
        duration = 5
    else:
        duration = 0
        flags.append("持仓不足 1 分钟")

    total = result + discipline + risk + duration
    grade = (
        "A" if total >= 85 else
        "B" if total >= 70 else
        "C" if total >= 55 else
        "D" if total >= 40 else "F"
    )

    planned_rr = _plan_rr(plan)
    if planned_rr is None:
        # 说得中性一点：这对手动交易者是常态，不是缺陷。
        flags.append("手动单：无挂单 SL/TP（常态，不扣分）")
    elif reason == "manual":
        flags.append(f"计划盈亏比 {planned_rr}，手动离场未按计划")

    return {
        "position_id": t.get("position_id"),
        "symbol": t.get("symbol"),
        "direction": t.get("direction"),
        "exit_time": t.get("exit_time"),
        "profit": round(profit, 2),
        "r": round(r, 3),
        "close_reason": reason,
        "planned_rr": planned_rr,
        "score": total,
        "grade": grade,
        "dims": {
            "result": result,
            "discipline": discipline,
            "risk": risk,
            "duration": duration,
        },
        "risk_pct": round(risk_pct, 2) if risk_pct is not None else None,
        "hold_min": round(hold, 1) if hold is not None else None,
        "flags": flags,
    }


def _balance_before_map(stats: dict) -> list[tuple[int, float]]:
    curve = stats.get("balance_curve") or []
    return [(int(x["ts"]), float(x["balance"])) for x in curve]


def _balance_before(curve: list[tuple[int, float]], ts: int, profit: float) -> float | None:
    lo, hi = 0, len(curve) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if curve[mid][0] <= ts:
            found = curve[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    if found is None:
        return None
    return max(found - profit, 0.0)


def score_trades(
    trades: list[dict],
    stats: dict,
    limit: int | None = None,
    plans: dict[str, dict] | None = None,
) -> list[dict]:
    closed = sorted([t for t in trades if t.get("exit_time")], key=lambda t: t["exit_time"])
    risk_unit = float(stats.get("risk_unit") or 0.0)
    if not risk_unit:
        losses = [abs(t["profit"]) for t in closed if t["profit"] < 0]
        risk_unit = sum(losses) / len(losses) if losses else 1.0

    curve = _balance_before_map(stats)
    plans = plans or {}
    rows = [
        score_trade(
            t,
            risk_unit,
            _balance_before(curve, int(t["exit_time"]), float(t["profit"])),
            plans.get(str(t.get("position_id"))),
        )
        for t in closed
    ]
    return rows[-limit:] if limit else rows


# ------------------------------------------------------------------ 账户评分


def score_account(
    stats: dict,
    quality: dict,
    trades: list[dict],
    profile: dict | None = None,
    plans: dict[str, dict] | None = None,
) -> dict:
    profile = profile or load_profile()
    if stats.get("empty"):
        return {
            "grade": "insufficient", "score": None, "dimensions": [],
            "red_flags": [], "breakeven": None, "trades": [],
        }

    n = int(stats.get("count") or 0)
    actual = float(stats.get("expectancy_r") or 0.0)
    rr = stats.get("risk_reward")
    win_rate = float(stats.get("win_rate") or 0.0)

    red: list[str] = []

    # 逐笔评分先算出来：纪律维度要用它里面的 risk_pct（超风险笔数）
    per_trade = score_trades(trades, {**stats, "risk_unit": stats.get("risk_unit")},
                             limit=100, plans=plans)

    # ---- 盈亏平衡边际（核心维度，不需要外部基准）----
    need = breakeven_win_rate(rr)
    margin = (win_rate - need) if need is not None else None

    dims: list[dict] = []

    dims.append({
        "key": "expectancy",
        "label": "期望",
        "value": actual,
        "display": f"{actual:+.3f} R",
        "target": "≥ +0.10 R",
        "grade": _grade_high(actual, [(0.20, "A"), (0.10, "B"), (0.0, "C"), (-0.10, "D")]),
        "note": "每笔按风险单位归一化后的平均盈亏；基准是 0，不假设任何行业标准",
    })

    if margin is None:
        margin_grade = "F"
        margin_display = "—"
        margin_note = "盈亏比缺失，无法算平衡线"
    else:
        margin_grade = _grade_high(margin, [(10, "A"), (5, "B"), (0, "C"), (-5, "D")])
        margin_display = f"{margin:+.1f} pp"
        margin_note = f"盈亏比 {rr:.2f} → 需要胜率 {need:.1f}%，实际 {win_rate:.1f}%"
        if margin < 0:
            red.append(
                f"胜率 {win_rate:.1f}% 低于平衡线 {need:.1f}%（差 {abs(margin):.1f} 个百分点）——"
                f"要么把盈亏比提到 ≥{win_rate / (100 - win_rate):.2f}，要么把胜率提上去"
            )
    dims.append({
        "key": "margin",
        "label": "盈亏平衡边际",
        "value": margin,
        "display": margin_display,
        "target": "≥ 0 pp",
        "grade": margin_grade,
        "note": margin_note,
    })

    wins = quality.get("windows") or []
    neg_ratio = 0.0
    if wins:
        neg = sum(1 for w in wins if (w["current"].get("expectancy_r") or 0) < 0)
        neg_ratio = neg / len(wins)
    dims.append({
        "key": "stability",
        "label": "稳定性",
        "value": neg_ratio,
        "display": f"{neg_ratio * 100:.0f}% 窗口为负",
        "target": "≤ 20%",
        "grade": _grade_low(neg_ratio, [(0.0, "A"), (0.2, "B"), (0.4, "C"), (0.6, "D")]),
        "note": f"{len(wins)} 个时间/笔数窗口里期望为负的比例",
    })

    dd_pct = float(stats.get("max_dd_pct") or 0.0)
    wipeouts = int(stats.get("wipeouts") or 0)
    risk_grade = _grade_low(dd_pct, [(10, "A"), (20, "B"), (30, "C"), (50, "D")])
    if wipeouts > 0:
        risk_grade = "F"
        red.append(f"账户被清零 {wipeouts} 次 — 仓位与账户规模不匹配，其余维度已失去意义")
    dims.append({
        "key": "risk",
        "label": "风险",
        "value": dd_pct,
        "display": f"回撤 {dd_pct:.1f}%",
        "target": "≤ 20%",
        "grade": risk_grade,
        "note": f"峰值余额 {stats.get('peak_balance')} → 谷值 {stats.get('trough_balance')}",
    })

    # ---- 纪律：只看有没有失控，不看手动平仓占比 ----
    # 手动交易者本来 100% 手动平仓，拿占比定级等于给这类账户统一判 F。
    # 「失控」用账户自身的平均亏损归一化（>2R），不用「余额 5%」——
    # 12 USD 的微型账户每笔都亏 20%+，固定百分比会把所有人判死，
    # 这正是本文件开头说的「用 R 归一化，不受规模影响」。
    stop_outs = sum(1 for t in trades if (t.get("close_reason") or "") == "stop_out")
    risk_unit = float(stats.get("risk_unit") or 0.0)
    over_risk = 0
    if risk_unit > 0:
        over_risk = sum(
            1 for t in trades
            if float(t.get("profit") or 0.0) < 0 and abs(float(t.get("profit") or 0.0)) > 2 * risk_unit
        )
    over_risk_pct = round(over_risk / n * 100, 1) if n else 0.0
    disc_grade = _grade_low(over_risk_pct, [(0, "A"), (5, "B"), (15, "C"), (30, "D")])
    if stop_outs > 0:
        disc_grade = "F"
        red.append(f"出现 {stop_outs} 次穿仓强平 — 仓位与账户规模不匹配")
    dims.append({
        "key": "discipline",
        "label": "纪律",
        "value": over_risk_pct,
        "display": f"超风险 {over_risk} 笔" + (f" · 穿仓 {stop_outs} 次" if stop_outs else ""),
        "target": "0 次穿仓",
        "grade": disc_grade,
        "note": "只看失控证据：穿仓强平、单笔亏损超过你平均亏损的 2 倍。手动平仓占比不计入——手动交易者本来就是手动平仓。",
    })

    sample_grade = _grade_high(n, [(100, "A"), (50, "B"), (30, "C"), (20, "D")])
    dims.append({
        "key": "sample",
        "label": "样本",
        "value": n,
        "display": f"{n} 笔",
        "target": "≥ 50 笔",
        "grade": sample_grade,
        "note": "低于 20 笔时任何结论都不成立",
    })

    # ---- 整体等级：取最差，不取平均 ----
    order = ["A", "B", "C", "D", "F"]
    worst = max((d["grade"] for d in dims), key=order.index)
    grade = worst
    if sample_grade == "F":
        grade = "insufficient"
        red.append(f"样本只有 {n} 笔，不足以下结论")

    scored = [LETTER_SCORE[d["grade"]] for d in dims if d["key"] != "sample"]
    score = min(scored) if scored else None

    avg_trade = round(statistics.mean([x["score"] for x in per_trade]), 1) if per_trade else None

    planned = [x["planned_rr"] for x in per_trade if x.get("planned_rr")]
    plan_stats = {
        "with_plan": len(planned),
        "avg_planned_rr": round(statistics.mean(planned), 2) if planned else None,
        "realized_rr": rr,
        "note": ("持仓快照里还没有开仓时的 SL/TP，计划盈亏比暂时算不出来。"
                 "从下一次开仓起会自动记录。") if not planned else None,
    }

    return {
        "grade": grade,
        "score": score,
        "avg_trade_score": avg_trade,
        "dimensions": dims,
        "red_flags": red,
        "breakeven": {
            "rr": rr,
            "required_win_rate": round(need, 1) if need is not None else None,
            "actual_win_rate": round(win_rate, 1),
            "margin_pp": round(margin, 1) if margin is not None else None,
        },
        "plan": plan_stats,
        "profile": profile,
        "trades": per_trade,
        "weights": TRADE_WEIGHTS,
    }


def compute(
    stats: dict,
    quality: dict,
    trades: list[dict],
    profile: dict | None = None,
    plans: dict[str, dict] | None = None,
) -> dict:
    return score_account(stats, quality, trades, profile, plans)


def grade_text(g: str) -> str:
    return {
        "A": "达标", "B": "接近达标", "C": "一项不达标",
        "D": "多项不达标", "F": "严重不达标", "insufficient": "样本不足",
    }.get(g, g)


if __name__ == "__main__":
    from live.journal import collect
    from live.quality import compute as quality_compute
    from live.stats import compute as stats_compute

    info = collect(force=True)
    if not info.get("ok"):
        print("读取失败:", info.get("error"))
        raise SystemExit(1)
    st = stats_compute(info["trades"], info["account"], info["deposits"], info.get("flows"))
    q = quality_compute(info["trades"])
    res = compute(st, q, info["trades"], plans=info.get("plans"))

    print(f"综合等级：{res['grade']}（{grade_text(res['grade'])}）  参考分 {res['score']}")
    print(f"单笔均分：{res['avg_trade_score']}")
    print()
    for d in res["dimensions"]:
        print(f"  {d['label']:<10} {d['grade']}  {d['display']:<18} 目标 {d['target']}")
        print(f"             {d['note']}")
    be = res["breakeven"]
    print()
    print(f"盈亏平衡：盈亏比 {be['rr']} → 需要胜率 {be['required_win_rate']}%  "
          f"实际 {be['actual_win_rate']}%  边际 {be['margin_pp']:+} pp" if be["margin_pp"] is not None else "")
    if res["red_flags"]:
        print("\n红旗：")
        for f in res["red_flags"]:
            print(f"  ⚠ {f}")
