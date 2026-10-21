"""回测包：纯 Python 回测引擎，不依赖 MT5 测试器"""

from .engine import EngineConfig, Strategy, run_backtest
from .indicators import atr, ema, macd, sma
from .metrics import compute_metrics
from .models import Bar, BacktestResult, Position, Trade

__all__ = [
    "EngineConfig",
    "Strategy",
    "run_backtest",
    "compute_metrics",
    "Bar",
    "Trade",
    "Position",
    "BacktestResult",
    "sma",
    "ema",
    "macd",
    "atr",
]
