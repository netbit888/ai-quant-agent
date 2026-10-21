from __future__ import annotations

from typing import Sequence

from backtest.indicators import ema, atr
from backtest.models import Bar


class Strategy:
    """黄金 H1 趋势跟踪：EMA 排列 + ATR 波动过滤 + ATR 止损止盈

    逻辑：
      - 快/中/慢三条 EMA 排列判断趋势方向（多头：fast>mid>slow；空头：fast<mid<slow）
      - 用 ATR 相对其均值过滤：波动过低（盘整）或过高（极端）都不开仓
      - 排列刚刚形成时入场，排列破坏时离场（信号返回反手）
      - SL/TP 以 ATR 倍数设定，保证满足最小点位约束
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_mid: int = 50,
        ema_slow: int = 100,
        atr_period: int = 14,
        atr_ma_period: int = 50,
        atr_min_ratio: float = 0.8,
        atr_max_ratio: float = 2.5,
        sl_atr: float = 2.0,
        tp_atr: float = 5.0,
        point: float = 0.001,
        min_sl_points: int = 150,
        min_tp_points: int = 400,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_mid = ema_mid
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.atr_ma_period = atr_ma_period
        self.atr_min_ratio = atr_min_ratio
        self.atr_max_ratio = atr_max_ratio
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self.point = point
        self.min_sl_points = min_sl_points
        self.min_tp_points = min_tp_points

        self._ef: list[float | None] = []
        self._em: list[float | None] = []
        self._es: list[float | None] = []
        self._atr: list[float | None] = []
        self._atr_ma: list[float | None] = []
        self._cur_atr: float = 0.0

    def prepare(self, bars: Sequence[Bar]) -> None:
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        self._ef = ema(closes, self.ema_fast)
        self._em = ema(closes, self.ema_mid)
        self._es = ema(closes, self.ema_slow)
        self._atr = atr(highs, lows, closes, self.atr_period)

        # ATR 的简单移动平均（用已经算好的 atr 序列做均值）
        n = len(self._atr)
        atr_ma: list[float | None] = [None] * n
        window: list[float] = []
        for i in range(n):
            v = self._atr[i]
            if v is None:
                atr_ma[i] = None
                continue
            window.append(v)
            if len(window) > self.atr_ma_period:
                window.pop(0)
            if len(window) == self.atr_ma_period:
                atr_ma[i] = sum(window) / len(window)
        self._atr_ma = atr_ma

    def _trend(self, i: int) -> int:
        ef, em, es = self._ef[i], self._em[i], self._es[i]
        if ef is None or em is None or es is None:
            return 0
        if ef > em > es:
            return 1
        if ef < em < es:
            return -1
        return 0

    def signal(self, i: int) -> int:
        if i < 1 or i >= len(self._ef):
            return 0
        t_now = self._trend(i)
        t_prev = self._trend(i - 1)
        if t_now == 0:
            return 0
        # 需要刚形成排列（前一根不是同方向趋势）
        if t_now == t_prev:
            return 0

        # 波动过滤
        a = self._atr[i]
        am = self._atr_ma[i]
        if a is None or am is None or am <= 0:
            return 0
        ratio = a / am
        if ratio < self.atr_min_ratio or ratio > self.atr_max_ratio:
            return 0

        self._cur_atr = a
        return t_now

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        a = self._atr[i] if i < len(self._atr) else None
        if a is None or a <= 0:
            a = self._cur_atr if self._cur_atr > 0 else 5.0

        sl_dist = max(a * self.sl_atr, self.min_sl_points * self.point)
        tp_dist = max(a * self.tp_atr, self.min_tp_points * self.point)

        sl = entry - direction * sl_dist
        tp = entry + direction * tp_dist
        return sl, tp