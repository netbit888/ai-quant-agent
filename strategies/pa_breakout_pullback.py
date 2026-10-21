"""结构突破回踩（BOS + Pullback）—— 手写 PA 参照策略

逻辑（多头版，空头镜像）：
  1. 趋势向上（HH + HL）
  2. 最近 N 根内出现向上 BOS（收盘突破前一个 swing high）
  3. 价格回踩到突破位附近（触及但收盘仍在位上方）
  4. 当根出现看涨形态（pin bar / 看涨吞没）
  5. 入场 → SL 放最近 swing low 下方 → TP 按 RR 倍数

不做任何指标计算，纯结构。此策略用于验证 structure.py 的识别是否可靠，
也是 LLM 生成 PA 策略时的参照实现。
"""

from __future__ import annotations

from typing import Sequence

from backtest.indicators import atr
from backtest.models import Bar
from backtest.structure import (
    SwingIndex,
    detect_bos,
    detect_pattern,
    last_swing,
    swing_points,
    trend_state,
)


class Strategy:
    def __init__(
        self,
        left: int = 2,
        right: int = 2,
        bos_lookback: int = 48,
        pullback_tol_points: float = 400.0,
        min_stop_points: float = 300.0,
        rr_target: float = 2.0,
        point: float = 0.001,
        allow_short: bool = True,
        strict_trend: bool = False,
        min_structure_points: float = 0.0,
        max_atr_ratio: float = 0.0,
        atr_lookback: int = 120,
        macro_left: int = 0,
        macro_right: int = 0,
    ) -> None:
        self.left = left
        self.right = right
        self.bos_lookback = bos_lookback
        self.pullback_tol_points = pullback_tol_points
        self.min_stop_points = min_stop_points
        self.rr_target = rr_target
        self.point = point
        self.allow_short = allow_short
        # strict_trend：要求最近 3 个高点和低点都严格递增/递减（比默认的 2 个更严）
        self.strict_trend = strict_trend
        # min_structure_points：最近两个 swing 的幅度下限，过滤窄幅震荡
        self.min_structure_points = min_structure_points
        # max_atr_ratio：当前 ATR / 过去 atr_lookback 根 ATR 中位数 的上限。
        # 超过说明进入异常波动（抛物线行情），此时突破回踩结构失效，停止开仓。
        # 注：在干净的 H4 数据上实测该过滤几乎无效（详见 docs/validation.md 8.3），
        # 默认关闭；保留参数是为了让 LLM 迭代仍有开关可用。
        self.max_atr_ratio = max_atr_ratio
        self.atr_lookback = atr_lookback
        # macro_left/macro_right：宏观分形窗口。0 表示不做宏观趋势过滤。
        # 开启后，局部结构放行的信号还要过大级别趋势这一关。
        self.macro_left = macro_left
        self.macro_right = macro_right

        self.bars: Sequence[Bar] = []
        self.swings = []
        self.idx: SwingIndex | None = None
        self.macro_idx: SwingIndex | None = None
        self.bos: list[tuple[int, int, float]] = []  # (index, direction, 被突破的结构位)

    # ------------------------------------------------------------ 预处理

    def prepare(self, bars: Sequence[Bar]) -> None:
        self.bars = bars
        self.swings = swing_points(bars, self.left, self.right)
        self.idx = SwingIndex(self.swings)

        # 宏观结构：用更大的分形窗口再算一套 swing，得到「大级别趋势」。
        # 局部结构（left=2/right=2）在回调期会返回 range，从而放行逆势单；
        # 实测 2025 黄金单边上涨中，最差的几笔全是逆势空单。
        self.macro_idx = None
        if self.macro_left > 0 and self.macro_right > 0:
            self.macro_swings = swing_points(bars, self.macro_left, self.macro_right)
            self.macro_idx = SwingIndex(self.macro_swings)

        self.bos = []
        for i in range(1, len(bars)):
            direction = detect_bos(bars, self.idx, i, lookback=60)
            if direction == 0:
                continue
            if direction == 1:
                sw = self.idx.last("high", i - 1)
            else:
                sw = self.idx.last("low", i - 1)
            if sw is not None:
                self.bos.append((i, direction, sw.price))

        # 波动率状态：当前 ATR / 过去 atr_lookback 根 ATR 中位数
        self.atr_ratio = [1.0] * len(bars)
        if self.max_atr_ratio > 0:
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            closes = [b.close for b in bars]
            atr_series = atr(highs, lows, closes, 14)
            for i in range(self.atr_lookback, len(bars)):
                window = [v for v in atr_series[i - self.atr_lookback : i] if v is not None]
                cur = atr_series[i]
                if not window or cur is None:
                    continue
                med = sorted(window)[len(window) // 2]
                if med > 0:
                    self.atr_ratio[i] = cur / med

    # ------------------------------------------------------------ 趋势过滤

    def _trend_ok(self, i: int, kind: str) -> bool:
        """趋势过滤。

        修正（2026-10-19）：原来要求「趋势已确认」与「刚发生突破」同时成立，
        这在逻辑上矛盾——BOS 本身就是趋势启动信号，此时结构往往还是 range。
        现在改为「只禁止反向趋势」：做多时不能是 down，做空时不能是 up。
        """
        state = trend_state(self.idx, i)
        if kind == "up" and state == "down":
            return False
        if kind == "down" and state == "up":
            return False

        # 宏观趋势过滤：大级别结构明确反向时不做
        if self.macro_idx is not None:
            macro = trend_state(self.macro_idx, i)
            if kind == "up" and macro == "down":
                return False
            if kind == "down" and macro == "up":
                return False

        if not self.strict_trend and self.min_structure_points <= 0:
            return True

        highs = self.idx.recent("high", i, 3)
        lows = self.idx.recent("low", i, 3)

        if self.strict_trend:
            if len(highs) < 3 or len(lows) < 3:
                return False
            if kind == "up":
                if not (
                    highs[0].price < highs[1].price < highs[2].price
                    and lows[0].price < lows[1].price < lows[2].price
                ):
                    return False
            else:
                if not (
                    highs[0].price > highs[1].price > highs[2].price
                    and lows[0].price > lows[1].price > lows[2].price
                ):
                    return False

        if self.min_structure_points > 0:
            ref = lows if kind == "up" else highs
            if len(ref) < 2:
                return False
            if abs(ref[-1].price - ref[-2].price) / self.point < self.min_structure_points:
                return False

        return True

    # ------------------------------------------------------------ 信号

    def signal(self, i: int) -> int:
        if i < 1 or i >= len(self.bars):
            return 0

        # 异常波动期不开仓：抛物线行情里突破回踩结构失效
        if self.max_atr_ratio > 0 and self.atr_ratio[i] > self.max_atr_ratio:
            return 0

        recent = [b for b in self.bos if i - self.bos_lookback <= b[0] <= i]
        if not recent:
            return 0

        bos_index, direction, level = recent[-1]
        bar = self.bars[i]
        tol = self.pullback_tol_points * self.point

        if direction == 1:
            if not self._trend_ok(i, "up"):
                return 0
            # 回踩：最低价摸到突破位附近，但收盘仍在其上
            if not (bar.low <= level + tol and bar.close > level):
                return 0
            if detect_pattern(self.bars, i) not in ("pin_bull", "engulf_bull"):
                return 0
            sw = last_swing(self.idx, "low", i)
            if sw is None or sw.price >= bar.close:
                return 0
            if (bar.close - sw.price) / self.point < self.min_stop_points:
                return 0
            return 1

        if not self.allow_short:
            return 0

        if not self._trend_ok(i, "down"):
            return 0
        if not (bar.high >= level - tol and bar.close < level):
            return 0
        if detect_pattern(self.bars, i) not in ("pin_bear", "engulf_bear"):
            return 0
        sw = last_swing(self.idx, "high", i)
        if sw is None or sw.price <= bar.close:
            return 0
        if (sw.price - bar.close) / self.point < self.min_stop_points:
            return 0
        return -1

    # ------------------------------------------------------------ 止损止盈

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        if direction > 0:
            sw = last_swing(self.idx, "low", i)
            sl = sw.price if sw is not None else entry - 800 * self.point
        else:
            sw = last_swing(self.idx, "high", i)
            sl = sw.price if sw is not None else entry + 800 * self.point

        risk = abs(entry - sl)
        if risk <= 0:
            return None, None
        tp = entry + direction * risk * self.rr_target
        return sl, tp
