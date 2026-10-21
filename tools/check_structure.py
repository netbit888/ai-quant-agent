"""结构识别自检：在真实数据上验证 swing / 趋势 / BOS / 形态识别是否合理

用法：python -m tools.check_structure
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from backtest.models import Bar
from backtest.structure import (
    detect_bos,
    detect_pattern,
    key_levels,
    swing_points,
    trend_state,
)

ROOT = Path(__file__).resolve().parent.parent


def load(year: int = 2025) -> list[Bar]:
    bars: list[Bar] = []
    with (ROOT / "data" / "XAUUSDm_H1.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            bars.append(
                Bar(
                    time=int(r["time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    tick_volume=int(r["tick_volume"]),
                    spread=int(r["spread"]),
                )
            )
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return [b for b in bars if start <= b.time < end]


def main() -> None:
    bars = load(2025)
    print(f"bars: {len(bars)}")

    swings = swing_points(bars, left=2, right=2)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    print(f"swing 点: {len(swings)}（高 {len(highs)} / 低 {len(lows)}）")

    # 1) 未来函数检查：confirmed_at 必须严格大于 index
    bad = [s for s in swings if s.confirmed_at <= s.index]
    print(f"未来函数检查: {'通过' if not bad else f'失败 {len(bad)} 个'}")

    # 2) 分形正确性抽查：取中间 5 个 swing，验证左右条件
    ok = 0
    sample = swings[len(swings) // 2 : len(swings) // 2 + 5]
    for s in sample:
        i = s.index
        if s.is_high:
            cond = all(bars[i].high > bars[i - k].high for k in (1, 2)) and all(
                bars[i].high >= bars[i + k].high for k in (1, 2)
            )
        else:
            cond = all(bars[i].low < bars[i - k].low for k in (1, 2)) and all(
                bars[i].low <= bars[i + k].low for k in (1, 2)
            )
        ok += bool(cond)
    print(f"分形抽查: {ok}/5 通过")

    # 3) 趋势分布
    states = Counter(trend_state(swings, i) for i in range(20, len(bars), 20))
    print(f"趋势分布（每 20 根采样）: {dict(states)}")

    # 4) BOS 统计
    bos_up = sum(1 for i in range(1, len(bars)) if detect_bos(bars, swings, i) == 1)
    bos_down = sum(1 for i in range(1, len(bars)) if detect_bos(bars, swings, i) == -1)
    print(f"BOS: 上破 {bos_up} 次 / 下破 {bos_down} 次")

    # 5) 形态分布
    pats = Counter(detect_pattern(bars, i) for i in range(1, len(bars)))
    print(f"形态分布: {dict(pats)}")

    # 6) 关键位（以最后时刻、容差 5 美元看）
    levels = key_levels(swings, len(bars) - 1, tolerance_price=5.0, price_min=0, price_max=10**6)
    print(f"关键位（触碰>=2 次）: {len(levels)} 个，最密集 3 个: {[(round(p, 1), c) for p, c in levels[:3]]}")


if __name__ == "__main__":
    main()
