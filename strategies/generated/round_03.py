from __future__ import annotations

import math
from typing import Sequence

from backtest.indicators import atr, ema, sma
from backtest.models import Bar


class Strategy:
    def __init__(
        self,
        fast_ema: int = 20,
        slow_ema: int = 50,
        trend_ema: int = 200,
        atr_period: int = 14,
        sl_atr_mult: float = 2.5,
        rr: float = 2.0,
        break_buffer_points: int = 30,
        min_tp_points: int = 400,
        min_sl_points: int = 150,
        point: float = 0.001,
    ) -> None:
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.trend_ema = trend_ema
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.rr = rr
        self.break_buffer_points = break_buffer_points
        self.min_tp_points = min_tp_points
        self.min_sl_points = min_sl_points
        self.point = point

        self._fast: list[float | None] = []
        self._slow: list[float | None] = []
        self._trend: list[float | None] = []
        self._atr: list[float | None] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._opens: list[float] = []

        # 用于 breakthrough 确认：记录上一根已收盘 bar 的 signal 状态
        self._pending_dir: int = 0
        self._pending_high: float = 0.0
        self._pending_low: float = 0.0

    def prepare(self, bars: Sequence[Bar]) -> None:
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        opens = [b.open for b in bars]

        self._fast = ema(closes, self.fast_ema)
        self._slow = ema(closes, self.slow_ema)
        self._trend = ema(closes, self.trend_ema)
        atr_vals = atr(highs, lows, closes, self.atr_period)

        # 回填 ATR 的 None/0/NaN，用前一个有效值
        filled: list[float | None] = []
        last_valid: float | None = None
        for v in atr_vals:
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) or v <= 0:
                filled.append(last_valid)
            else:
                filled.append(float(v))
                last_valid = float(v)
        # 若开头都是无效值，用第一个有效值回填（若有）
        first_valid = next((x for x in filled if x is not None), None)
        if first_valid is not None:
            filled = [first_valid if x is None else x for x in filled]
        self._atr = filled

        self._highs = highs
        self._lows = lows
        self._opens = opens

    def _atr_at(self, i: int) -> float | None:
        if i < 0 or i >= len(self._atr):
            return None
        v = self._atr[i]
        if v is None or v <= 0:
            return None
        return v

    def signal(self, i: int) -> int:
        # i 是当前已收盘 bar；信号基于 i 的收盘（不看未来）
        # 概念：如果 i 满足趋势+突破条件，返回方向；
        # 但我们用「下一根开盘价突破前一根高点」的确认入场——
        # 回测引擎以当前 bar 开盘价成交，signal(i) 基于 i-1 的收盘决定，
        # 所以这里判断 i-1 是否是触发 bar，且 i 的开盘要突破 i-1 的高/低点。
        # 由于 signal(i) 返回后引擎按 bar i 的 open 成交，我们无法修改 entry，
        # 但可以要求 bar i 的 open 已突破 bar i-1 的 high/low 才发信号。
        if i < 2 or i >= len(self._fast):
            return 0

        f1 = self._fast[i - 1]
        s1 = self._slow[i - 1]
        t1 = self._trend[i - 1]
        a1 = self._atr_at(i - 1)
        if f1 is None or s1 is None or t1 is None or a1 is None:
            return 0

        # 趋势排列（快 > 慢 > 长）做多方向
        trend_up = f1 > s1 and s1 > t1
        trend_dn = f1 < s1 and s1 < t1

        prev_high = self._highs[i - 1]
        prev_low = self._lows[i - 1]
        cur_open = self._opens[i]
        buf = self.break_buffer_points * self.point

        if trend_up and cur_open > prev_high + buf:
            # 同时要求前一根不是极端小实体（避免噪音）—用 atr 过滤
            body = abs(self._opens[i - 1] - self._highs[i - 1]) + 1e-12
            return 1
        if trend_dn and cur_open < prev_low - buf:
            return -1
        return 0

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        # 用 i-1 的 ATR（signal 触发 bar 的 ATR），避免当前 bar 未收盘污染
        a = self._atr_at(i - 1) if i - 1 >= 0 else None
        if a is None:
            a = self._atr_at(i)
        if a is None or a <= 0:
            # 最后兜底：按固定点数，确保永不返回 None
            sl_points = max(self.min_sl_points, 300)
            tp_points = max(self.min_tp_points, int(sl_points * self.rr))
            sl = entry - direction * sl_points * self.point
            tp = entry + direction * tp_points * self.point
            return sl, tp

        sl_dist = a * self.sl_atr_mult
        sl_points = sl_dist / self.point
        if sl_points < self.min_sl_points:
            sl_points = self.min_sl_points
            sl_dist = sl_points * self.point

        tp_dist = sl_dist * self.rr
        tp_points = tp_dist / self.point
        if tp_points < self.min_tp_points:
            tp_points = self.min_tp_points
            tp_dist = tp_points * self.point

        # 硬约束：TP > 360 points，SL > 100 points
        if tp_points <= 360:
            tp_points = 400
            tp_dist = tp_points * self.point
        if sl_points <= 100:
            sl_points = 150
            sl_dist = sl_points * self.point

        sl = entry - direction * sl_dist
        tp = entry + direction * tp_dist
        return sl, tp