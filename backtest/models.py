"""回测数据结构"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0  # points


@dataclass(slots=True)
class Trade:
    """一笔完整交易（开→平）。"""

    direction: int  # +1 多 / -1 空
    entry_time: int
    entry_price: float
    exit_time: int = 0
    exit_price: float = 0.0
    lots: float = 0.0
    profit: float = 0.0
    exit_reason: str = ""  # sl / tp / signal / end
    sl: float | None = None
    tp: float | None = None

    @property
    def is_win(self) -> bool:
        return self.profit > 0

    @property
    def rr_realized(self) -> float | None:
        """实际盈亏比（正数表示盈利）。"""
        if self.sl is None:
            return None
        risk = abs(self.entry_price - self.sl)
        if risk <= 0:
            return None
        return (self.exit_price - self.entry_price) * self.direction / risk


@dataclass
class Position:
    direction: int
    entry_time: int
    entry_price: float
    lots: float
    sl: float | None = None
    tp: float | None = None


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    initial_balance: float = 0.0
    final_balance: float = 0.0
    config: dict = field(default_factory=dict)
