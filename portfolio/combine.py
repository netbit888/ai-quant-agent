"""多品种组合回测

策略：各品种独立回测（分配独立资金与风险预算），再把权益曲线按时间合并。

这是近似方案，有两个已知偏差：
  1. 忽略保证金约束（多品种同时持仓可能超出可用保证金）
  2. 忽略"同时亏损"对风控熔断的叠加影响（各品种的日熔断是独立计算的）
对 3000 USD 级别账户，第 1 条在极端行情下会低估风险；先做效果验证，上实盘前需补。

优点：简单、可解释、能快速验证"分散是否真的降回撤"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from backtest.engine import EngineConfig, run_backtest
from backtest.metrics import _drawdown, compute_metrics
from backtest.models import Bar
from risk.guard import RiskConfig


@dataclass
class PortfolioItem:
    symbol: str
    bars: Sequence[Bar]
    strategy_factory: Callable[[], object]
    cfg: EngineConfig
    risk: RiskConfig | None = None


@dataclass
class PortfolioResult:
    curve: list[tuple[int, float]]
    initial: float
    final: float
    per_symbol: dict[str, dict] = field(default_factory=dict)


def _forward_fill_combine(curves: list[list[tuple[int, float]]], initials: list[float]) -> list[tuple[int, float]]:
    """按时间并集合并多条权益曲线，缺失段用该品种的初始资金填充。"""
    times = sorted({t for c in curves for t, _ in c})
    if not times:
        return []

    idxs = [0] * len(curves)
    lasts = list(initials)
    out: list[tuple[int, float]] = []

    for t in times:
        total = 0.0
        for k, curve in enumerate(curves):
            while idxs[k] + 1 < len(curve) and curve[idxs[k] + 1][0] <= t:
                idxs[k] += 1
            # 该品种在 t 之前已有数据才更新，否则保持初始资金
            if curve and curve[0][0] <= t:
                lasts[k] = curve[idxs[k]][1]
            total += lasts[k]
        out.append((t, total))
    return out


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def run_portfolio(
    items: list[PortfolioItem], total_balance: float = 3000.0, total_risk_pct: float = 3.0
) -> dict:
    """每个品种分到 total_balance / N 的资金、total_risk_pct / N 的风险预算。

    返回普通 dict，字段：initial / final / return_pct / cagr / max_drawdown /
    max_drawdown_pct / per_symbol / corr / curve / years
    """
    n = len(items)
    if n == 0:
        raise ValueError("组合为空")

    per_balance = total_balance / n
    per_risk = total_risk_pct / n

    curves: list[list[tuple[int, float]]] = []
    per_symbol: dict[str, dict] = {}

    from risk.guard import RiskGuard

    for item in items:
        cfg = EngineConfig(
            initial_balance=per_balance,
            lots=item.cfg.lots,
            point=item.cfg.point,
            contract_size=item.cfg.contract_size,
            spread_points=item.cfg.spread_points,
        )
        guard = None
        if item.risk is not None:
            item.risk.risk_per_trade_pct = per_risk
            guard = RiskGuard(item.risk)

        result = run_backtest(item.bars, item.strategy_factory(), cfg, guard=guard)
        curves.append(result.equity_curve)
        per_symbol[item.symbol] = {
            "initial": round(per_balance, 2),
            "final": round(result.final_balance, 2),
            "return_pct": round((result.final_balance - per_balance) / per_balance * 100, 2),
            **compute_metrics(result),
        }

    combined = _forward_fill_combine(curves, [per_balance] * n)
    final = combined[-1][1]
    max_dd, max_dd_pct = _drawdown(combined)
    years = (combined[-1][0] - combined[0][0]) / 86400 / 365.25 if len(combined) > 1 else 0
    cagr = ((final / total_balance) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0

    # 品种间相关性（各自权益曲线的逐 bar 收益）
    daily: list[list[float]] = []
    for curve in curves:
        vals = [v for _, v in curve]
        daily.append([(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals)) if vals[i - 1]])
    corr: dict[str, float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            corr[f"{items[i].symbol}~{items[j].symbol}"] = round(_pearson(daily[i], daily[j]), 3)

    return {
        "initial": round(total_balance, 2),
        "final": round(final, 2),
        "return_pct": round((final - total_balance) / total_balance * 100, 2),
        "cagr": round(cagr, 2),
        "years": round(years, 1),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "per_symbol": per_symbol,
        "corr": corr,
        "curve": combined,
    }
