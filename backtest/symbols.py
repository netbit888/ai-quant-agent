"""品种规格：point / contract / 实测点差 / ATR。

这份数据原来内联在 aqa.py 里。实盘成本核算（live/quality.py）要用同一套
点差，两处各存一份必然漂移——回测按 240 points 算成本、实盘按 180 算，
「回测赚钱实盘亏」就有一半是这种对不上造成的。所以抽成模块，双方共用。

`atr_pts` = 该品种 H4 上 ATR(14) 的中位数，**以 point 计**。策略的结构阈值
一律按 ATR 的倍数给出，而不是硬编码 point 数——否则同一组参数在黄金上成立、
换到指数上就差了 100 倍。下面三个系数是用黄金标定的：
    min_structure_points = 1.63 × ATR   （实测最优值 14000 / 8583 = 1.63）
    min_stop_points      = 0.93 × ATR   （8000 / 8583）
    pullback_tol_points  = 0.05 × ATR   （400 / 8583）
黄金上正好还原为 14000 / 8000 / 400，即已验证通过 Gate 2 的那组。

`spread` = 实测点差中位数与当前值里较大的那个（保守估计）。
已排除的品种：UK100m / AUS200m / HK50m —— 点差占 ATR 的 15% 以上，
H4 级别每笔开仓先亏 0.15 ATR，结构策略无法覆盖这个成本。
"""

from __future__ import annotations

SYMBOLS = {
    "XAUUSDm": {"point": 0.001, "contract": 100.0, "spread": 240, "atr_pts": 8583},
    "USDJPYm": {"point": 0.001, "contract": 100000.0, "spread": 10, "atr_pts": 318, "quote_from_price": True},
    "EURUSDm": {"point": 0.00001, "contract": 100000.0, "spread": 8, "atr_pts": 259},
    "GBPUSDm": {"point": 0.00001, "contract": 100000.0, "spread": 10, "atr_pts": 333},
    "JP225m": {"point": 0.1, "contract": 1.0, "spread": 123, "atr_pts": 2350, "quote_rate": 158.0},
    "US30_x10m": {"point": 0.1, "contract": 10.0, "spread": 19, "atr_pts": 1924},
    "US500_x100m": {"point": 0.01, "contract": 100.0, "spread": 64, "atr_pts": 2679},
}

STRUCT_ATR = 1.63
STOP_ATR = 0.93
TOL_ATR = 0.05


def quote_rate_of(spec: dict, price: float | None = None) -> float:
    """1 USD 等于多少报价货币。语义与 backtest/engine.py 的 _quote_rate 一致：
    quote_from_price=True 的品种（USDJPY 这类）用行情价当汇率，其余用固定值。
    """
    if spec.get("quote_from_price"):
        return price if price and price > 0 else 1.0
    rate = spec.get("quote_rate") or 1.0
    return rate if rate > 0 else 1.0


def spread_cost_usd(symbol: str | None, volume: float, price: float | None = None) -> float | None:
    """一笔往返的点差成本（USD）。

    公式与 backtest/engine.py 的成本模型逐字对齐：
        spread_points × point × volume × contract_size ÷ quote_rate
    实盘账本的 profit 不含点差（券商把点差做进成交价），所以这个数只能
    按规格表估——估值用同一套公式，实盘和回测的成本口径才可比。

    未知品种返回 None，绝不返回 0.0：没数据不等于没成本，
    把「没测过点差」显示成「零成本」会直接误导仓位决策。
    """
    spec = SYMBOLS.get(symbol or "")
    if not spec or volume <= 0:
        return None
    rate = quote_rate_of(spec, price)
    return spec["spread"] * spec["point"] * volume * spec["contract"] / rate
