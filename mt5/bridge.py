"""MT5 行情桥接：连接、品种查询、K 线拉取、CSV 导出

用法：
    python -m mt5.bridge --symbol EURUSDm --tf H1 --count 500

注意：本模块只读，不会影响正在运行的终端。
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path

import MetaTrader5 as mt5

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


@contextmanager
def mt5_session(terminal_path: str | None = None, timeout_ms: int = 60_000):
    """连接 MT5，退出时自动 shutdown。"""
    kwargs: dict = {"timeout": timeout_ms}
    if terminal_path:
        kwargs["path"] = terminal_path
    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"MT5 连接失败: {mt5.last_error()}")
    try:
        yield
    finally:
        mt5.shutdown()


def get_symbols(pattern: str = "") -> list[str]:
    """品种列表，pattern 支持通配（如 '*USDm'）。"""
    syms = mt5.symbols_get(pattern or None) or []
    return [s.name for s in syms]


def ensure_symbol(symbol: str) -> None:
    """确保品种在 Market Watch 中，否则 copy_rates 会失败。"""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"品种不存在: {symbol}（用 get_symbols() 查看可用品种）")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"无法选中品种: {symbol}")


def get_rates(symbol: str, timeframe: str = "H1", count: int = 500) -> list[dict]:
    """拉最近 count 根 K 线，按时间正序返回。"""
    tf = TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        raise ValueError(f"不支持的时间周期: {timeframe}，可选 {list(TIMEFRAMES)}")

    ensure_symbol(symbol)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"取 K 线失败 {symbol} {timeframe}: {mt5.last_error()}")

    return [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
            "spread": int(r["spread"]),
        }
        for r in rates
    ]


def save_csv(rows: list[dict], path: Path) -> Path:
    if not rows:
        raise ValueError("没有数据可写")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="拉取 MT5 K 线并存 CSV")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--tf", default="H1")
    parser.add_argument("--count", type=int, default=500)
    args = parser.parse_args()

    with mt5_session():
        rows = get_rates(args.symbol, args.tf, args.count)
        out = save_csv(rows, DATA_DIR / f"{args.symbol}_{args.tf.upper()}.csv")
        print(f"{args.symbol} {args.tf}: {len(rows)} bars -> {out}")
        print(f"  首根: {rows[0]}")
        print(f"  末根: {rows[-1]}")
        acct = mt5.account_info()
        if acct:
            print(f"  账户: {acct.login} @ {acct.server}  balance={acct.balance} {acct.currency}")


if __name__ == "__main__":
    main()
