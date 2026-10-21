"""数据质量检查：粒度一致性、缺口、异常值。

背景（实测）：券商 `XAUUSDm_H4` 的历史库存里，2014-01 ~ 2016-12 只有日线级
数据（相邻 bar 中位间隔 24h），2017-01 起才是真正的 4h。如果直接拿来跑回测，
「逐年表现」会把日线和 H4 两种粒度的年份放进同一个分母，Gate 2 的
「盈利年份占比」结论就没有意义。

本模块在读数时做一次自检并切掉不一致的头尾段，保证下游拿到的每一根 bar
都来自同一个周期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Bar

# 各周期名义间隔（小时）。只用于识别粒度，不做重采样。
TF_HOURS = {
    "M1": 1 / 60,
    "M5": 5 / 60,
    "M15": 0.25,
    "M30": 0.5,
    "H1": 1.0,
    "H4": 4.0,
    "D1": 24.0,
    "W1": 168.0,
}


@dataclass
class QualityReport:
    """一份数据的体检结果。"""

    bars: int
    first: str
    last: str
    expected_hours: float | None
    segments: list[dict]  # [{"start": 年, "end": 年, "bars": n, "median_gap_h": x}]
    dropped_head: int
    dropped_tail: int
    uniform: bool

    def summary(self) -> str:
        parts = [f"{self.bars} 根 {self.first}~{self.last}"]
        if self.segments:
            seg = " / ".join(f"{s['start']}-{s['end']}: {s['median_gap_h']:.0f}h×{s['bars']}" for s in self.segments)
            parts.append(f"粒度 {seg}")
        if self.dropped_head or self.dropped_tail:
            parts.append(f"已剔除 头{self.dropped_head}根 尾{self.dropped_tail}根")
        return " | ".join(parts)


def _median_gap_hours(times: list[int]) -> float:
    if len(times) < 2:
        return 0.0
    diffs = sorted((times[i + 1] - times[i]) / 3600 for i in range(len(times) - 1))
    return diffs[len(diffs) // 2]


def _year_of(ts: int) -> int:
    return datetime.fromtimestamp(ts, timezone.utc).year


def scan_segments(bars: list[Bar]) -> list[dict]:
    """按年切段，报出每段的根数与中位间隔，用来暴露粒度漂移。"""
    by_year: dict[int, list[int]] = {}
    for b in bars:
        by_year.setdefault(_year_of(b.time), []).append(b.time)
    segs = []
    for year in sorted(by_year):
        ts = sorted(by_year[year])
        segs.append({"start": year, "end": year, "bars": len(ts), "median_gap_h": _median_gap_hours(ts)})
    return segs


def trim_to_timeframe(bars: list[Bar], timeframe: str, tolerance: float = 0.5) -> tuple[list[Bar], QualityReport]:
    """切掉与目标周期粒度不符的头尾段。

    tolerance 表示允许的相对偏差（0.5 = 中位间隔在 0.5x~1.5x 名义值内视为一致）。
    只切头尾，不切中间——中间出现粒度漂移说明数据本身坏了，应由调用方拒绝使用。
    """
    tf = TF_HOURS.get(timeframe.upper())
    if not tf or len(bars) < 10:
        return bars, QualityReport(
            bars=len(bars),
            first=_fmt(bars[0].time) if bars else "-",
            last=_fmt(bars[-1].time) if bars else "-",
            expected_hours=tf,
            segments=scan_segments(bars),
            dropped_head=0,
            dropped_tail=0,
            uniform=True,
        )

    lo, hi = tf * (1 - tolerance), tf * (1 + tolerance)

    def ok(seg: dict) -> bool:
        return lo <= seg["median_gap_h"] <= hi

    segs = scan_segments(bars)
    head_bad = 0
    while head_bad < len(segs) and not ok(segs[head_bad]):
        head_bad += 1
    tail_bad = 0
    while tail_bad < len(segs) - head_bad and not ok(segs[-(tail_bad + 1)]):
        tail_bad += 1

    start_year = segs[head_bad]["start"] if head_bad < len(segs) else None
    end_year = segs[len(segs) - 1 - tail_bad]["end"] if tail_bad < len(segs) and start_year else None

    if start_year is None or end_year is None:
        # 整份数据都不匹配该周期
        return [], QualityReport(
            bars=0, first="-", last="-", expected_hours=tf, segments=segs,
            dropped_head=len(bars), dropped_tail=0, uniform=False,
        )

    kept = [b for b in bars if start_year <= _year_of(b.time) <= end_year]
    head_dropped = sum(1 for b in bars if _year_of(b.time) < start_year)
    tail_dropped = sum(1 for b in bars if _year_of(b.time) > end_year)

    uniform = all(ok(s) for s in segs[head_bad : len(segs) - tail_bad])
    return kept, QualityReport(
        bars=len(kept),
        first=_fmt(kept[0].time) if kept else "-",
        last=_fmt(kept[-1].time) if kept else "-",
        expected_hours=tf,
        segments=segs[head_bad : len(segs) - tail_bad],
        dropped_head=head_dropped,
        dropped_tail=tail_dropped,
        uniform=uniform,
    )


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
