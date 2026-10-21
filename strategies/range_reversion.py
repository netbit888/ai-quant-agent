"""均值回归策略：通道边缘反弹（PA 语境）

逻辑（多头版，空头镜像）：
  1. 用最近 N 根的滚动高低点构成"区间通道"
  2. 通道宽度足够（过滤窄幅噪音）
  3. 当前处于 range 状态（HH/HL 交替）——避开单边趋势
  4. 价格触及通道下沿附近，收盘仍回到通道内
  5. 出现看涨反转形态（pin bar / 看涨吞没）
  6. 入场 → SL 放在通道下沿外侧 → TP 回通道中位

设计意图：与趋势突破策略互补。
趋势策略在 2020/2024-2026 单边行情赚钱，2022-2023 震荡市亏损；
本策略应当反过来——震荡市赚钱，单边市被 range 过滤掉。

不做指标计算，纯价格结构与形态。
"""

from __future__ import annotations

from typing import Sequence

from backtest.models import Bar
from backtest.structure import SwingIndex, detect_pattern, swing_points


class Strategy:
    def __init__(
        self,
        channel: int = 50,
        edge_ratio: float = 0.15,
        buffer_points: float = 600.0,
        min_channel_points: float = 4000.0,
        min_rr: float = 1.0,
        range_only: bool = True,
        point: float = 0.001,
    ) -> None:
        self.channel = channel
        self.edge_ratio = edge_ratio  # 距边缘多少比例内算"触及"
        self.buffer_points = buffer_points  # 止损放在通道外的缓冲
        self.min_channel_points = min_channel_points  # 通道最小宽度
        self.min_rr = min_rr
        self.range_only = range_only
        self.point = point

        self.bars: Sequence[Bar] = []
        self.idx: SwingIndex | None = None
        self.hh: list[float] = []
        self.ll: list[float] = []

    # ------------------------------------------------------------ 预处理

    def prepare(self, bars: Sequence[Bar]) -> None:
        self.bars = bars
        self.idx = SwingIndex(swing_points(bars, 2, 2))

        n = self.channel
        hh = [0.0] * len(bars)
        ll = [0.0] * len(bars)
        for i in range(len(bars)):
            start = max(0, i - n + 1)
            window = bars[start : i + 1]
            hh[i] = max(b.high for b in window)
            ll[i] = min(b.low for b in window)
        self.hh, self.ll = hh, ll

    # ------------------------------------------------------------ 信号

    def signal(self, i: int) -> int:
        if i < self.channel + 1 or i >= len(self.bars):
            return 0

        hi, lo = self.hh[i - 1], self.ll[i - 1]
        width = hi - lo
        if width / self.point < self.min_channel_points:
            return 0  # 区间太窄，噪音

        if self.range_only and self.idx.state(i) != "range":
            return 0  # 单边趋势里不做逆势

        bar = self.bars[i]
        edge = width * self.edge_ratio
        pattern = detect_pattern(self.bars, i)

        # 触及下沿 → 做多
        if bar.low <= lo + edge and bar.close > lo:
            if pattern in ("pin_bull", "engulf_bull"):
                if self._rr_ok(i, bar.close, 1):
                    return 1

        # 触及上沿 → 做空
        if bar.high >= hi - edge and bar.close < hi:
            if pattern in ("pin_bear", "engulf_bear"):
                if self._rr_ok(i, bar.close, -1):
                    return -1

        return 0

    def _rr_ok(self, i: int, entry: float, direction: int) -> bool:
        sl, tp = self.sl_tp(i, entry, direction)
        if sl is None or tp is None:
            return False
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        return risk > 0 and reward / risk >= self.min_rr

    # ------------------------------------------------------------ 止损止盈

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        hi, lo = self.hh[i], self.ll[i]
        mid = (hi + lo) / 2
        buf = self.buffer_points * self.point

        if direction > 0:
            sl = lo - buf
            tp = mid
        else:
            sl = hi + buf
            tp = mid

        # 目标价必须在正确一侧
        if direction > 0 and (tp <= entry or sl >= entry):
            return None, None
        if direction < 0 and (tp >= entry or sl <= entry):
            return None, None
        return sl, tp
