"""成本核算：点差（实测规格表）+ 佣金 + 库存费。

为什么要有这个模块
------------------
MT5 账本里的 `profit` **不含点差**——点差是做进成交价里的，所以逐笔盈亏
看起来比真实好。不把点差补回来的话，「持仓 1.9 分钟、每笔赚 1.5」这种
数字会让人误以为策略有边际，实际上 0.01 手黄金每笔光点差就 0.24 USD。

点差从 `backtest/symbols.py` 的实测规格表取，公式与 `backtest/engine.py`
的成本模型逐字一致（spread_points × point × volume × contract ÷ quote_rate），
这样实盘与回测的成本口径可比。规格表里没有的品种返回 None 并标记
`spread_unknown`——没数据不等于没成本，绝不记 0。
"""

from __future__ import annotations

from backtest.symbols import spread_cost_usd


def cost_summary(rows: list[dict]) -> dict:
    """一组交易的汇总成本。rows 可以是窗口内的，也可以是全账户的。"""
    spread = 0.0
    fee = 0.0
    spread_unknown = False
    symbols: set[str] = set()
    for t in rows:
        sym = t.get("symbol") or ""
        if sym:
            symbols.add(sym)
        c = spread_cost_usd(sym, t.get("volume") or 0.0, t.get("entry_price"))
        if c is None:
            spread_unknown = True
        else:
            spread += c
        fee += abs(t.get("commission") or 0.0) + abs(t.get("swap") or 0.0)

    total = spread + fee
    # 毛盈亏规模：|总盈利| + |总亏损|，等价于逐笔 |profit| 求和
    gross_abs = sum(abs(t.get("profit") or 0.0) for t in rows)
    n = len(rows)
    return {
        "spread_usd": round(spread, 2),
        "fee_usd": round(fee, 2),
        "total_usd": round(total, 2),
        "per_trade_usd": round(total / n, 2) if n else 0.0,
        "gross_abs": round(gross_abs, 2),
        "cost_pct": round(total / gross_abs * 100, 1) if gross_abs > 0 else None,
        "spread_unknown": spread_unknown,
        "symbols": sorted(symbols),
    }
