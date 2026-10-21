"""技术指标实现（纯 Python，无 numpy 依赖也可用）

所有函数输入价格序列，返回与输入等长的列表（前 period-1 个为 None）。
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """EMA，首值用前 period 个的 SMA 作种子（与 MT5 行为接近）。"""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(
    values: list[float], fast: int = 12, slow: int = 24, signal: int = 9
) -> tuple[list[float | None], list[float | None]]:
    """返回 (macd 主线, 信号线)。"""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)

    main: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            main[i] = ema_fast[i] - ema_slow[i]

    # 信号线：对主线做 EMA（跳过前面的 None）
    valid = [v for v in main if v is not None]
    offset = len(main) - len(valid)
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * len(values)
    for i, v in enumerate(sig_valid):
        if v is not None:
            sig[offset + i] = v
    return main, sig


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float | None]:
    """平均真实波幅（简单平均版，与 MT5 的 iATR 有细微差异）。"""
    trs: list[float] = []
    for i in range(len(highs)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
    return sma(trs, period)
