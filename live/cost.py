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

佣金/库存费带符号处理：负数 = 成本，正数 = 收入。当前账户的券商是
**免库存费**（swap 恒为 0）且无佣金，所以 fee_usd 一直是 0，成本就等于点差；
但换到收费平台或持仓过夜的账户，这条逻辑必须成立，不能把收入算成成本。
"""

from __future__ import annotations

from backtest.symbols import spread_cost_usd


def cost_summary(rows: list[dict]) -> dict:
    """一组交易的汇总成本。rows 可以是窗口内的，也可以是全账户的。

    fee_usd 是**净**费用，带符号：佣金/库存费为负（成本）时是正数，
    拿到正库存费（收入）时会变小甚至为负。total_usd = 点差 + 净费用。
    """
    spread = 0.0
    fee = 0.0
    spread_unknown = False
    symbols: set[str] = set()
    for t in rows:
        sym = t.get("symbol") or ""
        if sym:
            symbols.add(sym)
        # 部分平仓时只结算了 volume_closed，点差也只该按这部分算一次
        vol = t.get("volume_closed") or t.get("volume") or 0.0
        c = spread_cost_usd(sym, vol, t.get("entry_price"))
        if c is None:
            spread_unknown = True
        else:
            spread += c
        # 佣金/库存费带符号累加：负数 = 成本，正数 = 收入（隔夜利息可能为正是收入）。
        # 以前用 abs() 会把收入也算成成本，持仓跨结算日时成本被高估。
        fee -= float(t.get("commission") or 0.0) + float(t.get("swap") or 0.0)

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
