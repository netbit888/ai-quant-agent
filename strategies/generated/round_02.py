from __future__ import annotations

from typing import Sequence

from backtest.indicators import atr, ema
from backtest.models import Bar


class Strategy:
    """趋势排列 + 回调至快 EMA 入场，3ATR 止损，保持 TP/SL 比例。

    思路：
      - 用 EMA(fast) > EMA(mid) > EMA(slow) 判定趋势排列；
      - 排列已形成至少若干根（避免刚形成就追）；
      - 价格回踩快 EMA（低点触及/接近快 EMA 且收盘仍在快 EMA 上方做多），
        避免追高，提高单笔质量；
      - 止损用 3*ATR（放大，抗噪音），止盈按 rr 倍数（默认保持约 2.25）。
    """

    def __init__(
        self,
        fast: int = 20,
        mid: int = 50,
        slow: int = 100,
        trend_lookback: int = 6,
        pull_tol_atr: float = 0.35,
        atr_period: int = 14,
        sl_atr: float = 3.0,
        rr: float = 2.25,
        point: float = 0.001,
        min_sl_points: int = 150,
        min_tp_points: int = 400,
    ) -> None:
        self.fast = fast
        self.mid = mid
        self.slow = slow
        self.trend_lookback = trend_lookback
        self.pull_tol_atr = pull_tol_atr
        self.atr_period = atr_period
        self.sl_atr = sl_atr
        self.rr = rr
        self.point = point
        self.min_sl_points = min_sl_points
        self.min_tp_points = min_tp_points

        self._ef: list[float | None] = []
        self._em: list[float | None] = []
        self._es: list[float | None] = []
        self._atr: list[float | None] = []
        self._bars: Sequence[Bar] | None = None

    def prepare(self, bars: Sequence[Bar]) -> None:
        closes = [b.close for b in bars]
        self._ef = ema(closes, self.fast)
        self._em = ema(closes, self.mid)
        self._es = ema(closes, self.slow)
        self._atr = atr(
            [b.high for b in bars],
            [b.low for b in bars],
            closes,
            self.atr_period,
        )
        self._bars = bars

    def _trend_ok(self, i: int, direction: int) -> bool:
        # 排列需在过去 trend_lookback 根内持续成立
        start = i - self.trend_lookback
        if start < 0:
            return False
        for k in range(start, i + 1):
            f, m, s = self._ef[k], self._em[k], self._es[k]
            if f is None or m is None or s is None:
                return False
            if direction > 0:
                if not (f > m > s):
                    return False
            else:
                if not (f < m < s):
                    return False
        return True

    def signal(self, i: int) -> int:
        if i < 1 or self._bars is None:
            return 0
        if i >= len(self._ef) or i >= len(self._atr):
            return 0
        ef = self._ef[i]
        a = self._atr[i]
        if ef is None or a is None or a <= 0:
            return 0

        bar = self._bars[i]
        prev = self._bars[i - 1]

        # 做多：多头排列成立 + 回踩快 EMA + 收盘重新站上快 EMA
        if self._trend_ok(i, 1):
            tol = self.pull_tol_atr * a
            touched = bar.low <= ef + tol
            reclaimed = bar.close > ef
            prev_below_or_touch = prev.low <= self._ef[i - 1] + tol if self._ef[i - 1] is not None else False
            if touched and reclaimed and prev_below_or_touch and bar.close > bar.open:
                return 1

        # 做空：空头排列成立 + 反抽快 EMA + 收盘重新跌破快 EMA
        if self._trend_ok(i, -1):
            tol = self.pull_tol_atr * a
            touched = bar.high >= ef - tol
            reclaimed = bar.close < ef
            prev_above_or_touch = prev.high >= self._ef[i - 1] - tol if self._ef[i - 1] is not None else False
            if touched and reclaimed and prev_above_or_touch and bar.close < bar.open:
                return -1

        return 0

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        a = self._atr[i] if i < len(self._atr) else None
        if a is None or a <= 0:
            return None, None

        sl_dist = self.sl_atr * a
        min_sl = self.min_sl_points * self.point
        if sl_dist < min_sl:
            sl_dist = min_sl

        tp_dist = sl_dist * self.rr
        min_tp = self.min_tp_points * self.point
        if tp_dist < min_tp:
            tp_dist = min_tp

        if direction > 0:
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist
        return sl, tp