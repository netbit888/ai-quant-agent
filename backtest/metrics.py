"""指标计算：字段名对齐 MT5 策略测试报告，便于与 MT5 结果直接对比"""

from __future__ import annotations

import math

from .models import BacktestResult


def _drawdown(equity: list[tuple[int, float]]) -> tuple[float, float]:
    """返回 (最大绝对回撤, 最大百分比回撤)。"""
    peak = -math.inf
    max_abs = 0.0
    max_pct = 0.0
    for _, value in equity:
        peak = max(peak, value)
        drop = peak - value
        if drop > max_abs:
            max_abs = drop
        if peak > 0 and drop / peak * 100 > max_pct:
            max_pct = drop / peak * 100
    return max_abs, max_pct


def compute_metrics(result: BacktestResult) -> dict:
    trades = result.trades
    wins = [t for t in trades if t.profit > 0]
    losses = [t for t in trades if t.profit <= 0]

    gross_profit = sum(t.profit for t in wins)
    gross_loss = sum(t.profit for t in losses)
    net = result.final_balance - result.initial_balance

    max_dd_abs, max_dd_pct = _drawdown(result.equity_curve)
    total = len(trades)

    win_rate = len(wins) / total * 100 if total else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    largest_win = max((t.profit for t in trades), default=0.0)
    largest_loss = min((t.profit for t in trades), default=0.0)

    # 连续胜负
    streak_win = streak_loss = cur_win = cur_loss = 0
    for t in trades:
        if t.profit > 0:
            cur_win += 1
            cur_loss = 0
            streak_win = max(streak_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            streak_loss = max(streak_loss, cur_loss)

    profit_factor = gross_profit / abs(gross_loss) if gross_loss else float("inf")
    expected = net / total if total else 0.0
    recovery = net / max_dd_abs if max_dd_abs else float("inf")

    # 简化夏普：按 bar 收益的均值/标准差，年化（H1 按 24*252 根/年）
    returns = []
    prev = result.equity_curve[0][1] if result.equity_curve else 0
    for _, value in result.equity_curve[1:]:
        if prev:
            returns.append((value - prev) / prev)
        prev = value
    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(24 * 252)) if std else 0.0
    else:
        sharpe = 0.0

    return {
        "net_profit": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "expected_payoff": round(expected, 2),
        "recovery_factor": round(recovery, 2) if recovery != float("inf") else None,
        "sharpe": round(sharpe, 2),
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "max_drawdown": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_win_streak": streak_win,
        "max_loss_streak": streak_loss,
        "initial_balance": round(result.initial_balance, 2),
        "final_balance": round(result.final_balance, 2),
    }
