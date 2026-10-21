"""PA（Price Action）结构识别

全部基于裸 K 结构，不依赖指标。核心约束：**禁止未来函数**。
每个 swing 点都有 `confirmed_at` —— 只有索引 >= confirmed_at 的时刻，策略才允许使用它，
因为它需要右侧 right 根 K 线来确认。

性能：所有查询都走 `SwingIndex`（按 confirmed_at 二分），单次 O(log n)。
不要在循环里直接把 list 传给 detect_bos —— 那会退化成 O(n²)。

主要能力：
  - swing_points      分形高低点
  - SwingIndex        预索引，供高频查询
  - trend_state       趋势状态（up / down / range）
  - detect_bos        结构突破（Break of Structure）
  - detect_pattern    K 线形态（pin bar / engulfing / inside bar）
  - key_levels        关键位聚类（支撑阻力）
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from .models import Bar


@dataclass(slots=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" / "low"
    confirmed_at: int  # index + right，确认时刻（含）

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


class SwingIndex:
    """swing 点的预索引：highs / lows 各自按 confirmed_at 单调递增，可二分。"""

    __slots__ = ("all", "highs", "lows", "_hc", "_lc")

    def __init__(self, swings: Sequence[Swing]) -> None:
        self.all = list(swings)
        self.highs = [s for s in self.all if s.kind == "high"]
        self.lows = [s for s in self.all if s.kind == "low"]
        self._hc = [s.confirmed_at for s in self.highs]
        self._lc = [s.confirmed_at for s in self.lows]

    def last(self, kind: str, at_index: int) -> Swing | None:
        bucket, conf = (self.highs, self._hc) if kind == "high" else (self.lows, self._lc)
        pos = bisect_right(conf, at_index) - 1
        return bucket[pos] if pos >= 0 else None

    def recent(self, kind: str, at_index: int, n: int = 2) -> list[Swing]:
        bucket, conf = (self.highs, self._hc) if kind == "high" else (self.lows, self._lc)
        end = bisect_right(conf, at_index)
        return bucket[max(0, end - n) : end]

    def state(self, at_index: int) -> str:
        """HH+HL → up；LH+LL → down；其它 → range。"""
        highs = self.recent("high", at_index, 2)
        lows = self.recent("low", at_index, 2)
        if len(highs) < 2 or len(lows) < 2:
            return "range"
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
        if hh and hl:
            return "up"
        if lh and ll:
            return "down"
        return "range"


# ---------------------------------------------------------------- swing

def swing_points(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[Swing]:
    """分形法识别 swing 高低点。

    条件：bars[i].high 严格高于左侧 left 根、且不低于右侧 right 根（低点镜像）。
    用「左侧严格、右侧非严格」避免平台顶被重复识别。
    """
    out: list[Swing] = []
    n = len(bars)
    if n < left + right + 1:
        return out

    for i in range(left, n - right):
        if all(bars[i].high > bars[i - k].high for k in range(1, left + 1)) and all(
            bars[i].high >= bars[i + k].high for k in range(1, right + 1)
        ):
            out.append(Swing(index=i, price=bars[i].high, kind="high", confirmed_at=i + right))

        if all(bars[i].low < bars[i - k].low for k in range(1, left + 1)) and all(
            bars[i].low <= bars[i + k].low for k in range(1, right + 1)
        ):
            out.append(Swing(index=i, price=bars[i].low, kind="low", confirmed_at=i + right))

    out.sort(key=lambda s: s.index)
    return out


def _as_index(swings: Sequence[Swing] | SwingIndex) -> SwingIndex:
    return swings if isinstance(swings, SwingIndex) else SwingIndex(swings)


def last_swing(swings: Sequence[Swing] | SwingIndex, kind: str, at_index: int) -> Swing | None:
    return _as_index(swings).last(kind, at_index)


def trend_state(swings: Sequence[Swing] | SwingIndex, at_index: int) -> str:
    return _as_index(swings).state(at_index)


# ---------------------------------------------------------------- 结构

def detect_bos(
    bars: Sequence[Bar],
    swings: Sequence[Swing] | SwingIndex,
    at_index: int,
    lookback: int = 60,
) -> int:
    """结构突破：+1 向上、-1 向下、0 无。

    判定：当根收盘越过最近一个已确认 swing 点（需前一根在其另一侧），
    且该 swing 点距今不超过 lookback 根。
    """
    if at_index < 1 or at_index >= len(bars):
        return 0
    idx = _as_index(swings)
    bar = bars[at_index]
    prev_close = bars[at_index - 1].close

    sh = idx.last("high", at_index - 1)
    if sh and at_index - sh.index <= lookback and bar.close > sh.price and prev_close <= sh.price:
        return 1

    sl = idx.last("low", at_index - 1)
    if sl and at_index - sl.index <= lookback and bar.close < sl.price and prev_close >= sl.price:
        return -1

    return 0


def detect_choch(swings: Sequence[Swing] | SwingIndex, at_index: int) -> int:
    """性格转变：上升结构中出现更低的 low，或下降结构中出现更高的 high。"""
    idx = _as_index(swings)
    state = idx.state(at_index)
    highs = idx.recent("high", at_index, 2)
    lows = idx.recent("low", at_index, 2)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if state == "up" and lows[-1].price < lows[-2].price:
        return -1
    if state == "down" and highs[-1].price > highs[-2].price:
        return 1
    return 0


# ---------------------------------------------------------------- K 线形态

def bar_body(bar: Bar) -> float:
    return abs(bar.close - bar.open)


def bar_range(bar: Bar) -> float:
    return max(bar.high - bar.low, 1e-9)


def upper_wick(bar: Bar) -> float:
    return bar.high - max(bar.open, bar.close)


def lower_wick(bar: Bar) -> float:
    return min(bar.open, bar.close) - bar.low


def detect_pattern(bars: Sequence[Bar], i: int) -> str | None:
    """识别单根/双根 K 线形态：pin_bull / pin_bear / engulf_bull / engulf_bear / inside。"""
    if i < 1 or i >= len(bars):
        return None
    bar, prev = bars[i], bars[i - 1]
    body, rng = bar_body(bar), bar_range(bar)

    if body <= rng * 0.4:
        if lower_wick(bar) >= body * 2 and lower_wick(bar) / rng >= 0.55:
            return "pin_bull"
        if upper_wick(bar) >= body * 2 and upper_wick(bar) / rng >= 0.55:
            return "pin_bear"

    if body > bar_body(prev) * 1.2:
        if bar.close > bar.open and prev.close < prev.open and bar.close >= prev.open and bar.open <= prev.close:
            return "engulf_bull"
        if bar.close < bar.open and prev.close > prev.open and bar.close <= prev.open and bar.open >= prev.close:
            return "engulf_bear"

    if bar.high <= prev.high and bar.low >= prev.low:
        return "inside"

    return None


# ---------------------------------------------------------------- 关键位

def key_levels(
    swings: Sequence[Swing] | SwingIndex,
    at_index: int,
    tolerance_price: float,
    price_min: float,
    price_max: float,
) -> list[tuple[float, int]]:
    """把已确认 swing 点按价格聚类，返回 [(价格, 触碰次数)]，只保留 >= 2 次的。"""
    idx = _as_index(swings)
    prices = [s.price for s in idx.all if s.confirmed_at <= at_index and price_min <= s.price <= price_max]
    if not prices:
        return []

    clusters: list[list[float]] = []
    for p in sorted(prices):
        if clusters and p - clusters[-1][-1] <= tolerance_price:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    levels = [(sum(c) / len(c), len(c)) for c in clusters if len(c) >= 2]
    levels.sort(key=lambda x: x[1], reverse=True)
    return levels
