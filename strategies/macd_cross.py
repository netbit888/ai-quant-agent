"""MACD 交叉策略——用于校准 Python 引擎与 MT5 结果

复现 MT5 官方 ExpertMACD 的核心逻辑：
  - MACD(12, 24, 9)，主线上穿信号线做多，下穿做空
  - 反向信号反手
  - TP/SL 以 points 计（默认 50 / 20）

MT5 基准（XAUUSDm H1, 2025.01.01-2025.12.31, 模型=仅开盘价）：
  净盈利 136.32 / 1136 笔 / 胜率 45.86% / 盈利因子 2.10 / 最大回撤 39.43
"""

from __future__ import annotations

from typing import Sequence

from backtest.indicators import macd
from backtest.models import Bar


class MacdCross:
    def __init__(
        self,
        fast: int = 12,
        slow: int = 24,
        sig: int = 9,
        tp_points: int = 50,
        sl_points: int = 20,
        point: float = 0.001,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.sig = sig
        self.tp_points = tp_points
        self.sl_points = sl_points
        self.point = point
        self._main: list[float | None] = []
        self._signal: list[float | None] = []

    def prepare(self, bars: Sequence[Bar]) -> None:
        closes = [b.close for b in bars]
        main, sig = macd(closes, self.fast, self.slow, self.sig)
        self._main, self._signal = main, sig

    def signal(self, i: int) -> int:
        if i < 1 or i >= len(self._main):
            return 0
        m0, m1 = self._main[i - 1], self._main[i]
        s0, s1 = self._signal[i - 1], self._signal[i]
        if m0 is None or m1 is None or s0 is None or s1 is None:
            return 0
        if m0 <= s0 and m1 > s1:
            return 1
        if m0 >= s0 and m1 < s1:
            return -1
        return 0

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        tp = entry + direction * self.tp_points * self.point
        sl = entry - direction * self.sl_points * self.point
        return sl, tp
