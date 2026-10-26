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

from live import appdirs

DATA_DIR = appdirs.data_dir()

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


def _to_rows(rates) -> list[dict]:
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


def _timeframe(timeframe: str) -> int:
    tf = TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        raise ValueError(f"不支持的时间周期: {timeframe}，可选 {list(TIMEFRAMES)}")
    return tf


def get_rates(symbol: str, timeframe: str = "H1", count: int = 500) -> list[dict]:
    """拉最近 count 根 K 线，按时间正序返回。

    必须配 `mt5_session()` 用：退出时那个上下文管理器会 `mt5.shutdown()`。
    常驻进程（比如面板里的 AI 做单线程）不要调这个，用 `get_rates_live`。
    """
    tf = _timeframe(timeframe)
    ensure_symbol(symbol)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"取 K 线失败 {symbol} {timeframe}: {mt5.last_error()}")
    return _to_rows(rates)


def get_rates_live(symbol: str, timeframe: str = "M1", count: int = 120) -> list[dict]:
    """在**已经连上的终端**上取 K 线：不 initialize、不 shutdown。

    为什么不复用 `get_rates` + `mt5_session`
    ----------------------------------------
    MT5 的 initialize/shutdown 是进程级的引用计数。一次性脚本用完就关没问题，
    但面板里的 AI 做单是常驻线程：取一次 K 线就把整个进程的连接关掉，
    下一轮报价直接拿不到，日志里只会看到「拿不到报价」——
    看起来像行情断了，实际上是自己的代码把连接掐了。
    """
    from live.journal import _mt5  # 借用账本模块的连接保活逻辑

    tf = _timeframe(timeframe)
    conn = _mt5()          # 未连接时会 initialize，已连接就复用
    ensure_symbol(symbol)
    rates = conn.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"取 K 线失败 {symbol} {timeframe}: {conn.last_error()}")
    return _to_rows(rates)


def get_ticks_live(symbol: str, from_ts: int, to_ts: int) -> list[dict]:
    """取 tick 级数据：逐笔的 bid/ask 变动流（市场报价里那两根线的原始数据）。

    和 K 线不同，tick 没有"这根"的概念——它就是每一次报价变化。
    1 分钟黄金常常有几十到几百个 tick，所以**不要让模型直接读原始流**，
    先按分钟聚合（见 live/autotrade.py 的 summarize_ticks）。

    时区陷阱（实测踩过，别再踩）
    ----------------------------
    `copy_ticks_range` 的入参 datetime 被 MT5 按**本机本地时间**解释，
    但它返回的 `time` 是 UTC 纪元秒（和 `copy_rates` / `symbol_info_tick` 同一套）。
    所以传 UTC-naive 会整体错开本机时区（这台机器 UTC+8，错了 8 小时），
    查出来的 tick 全部落在窗口外，表现为"tick 数 = 0"，看着像券商不给数据。
    做法：用 `datetime.fromtimestamp()`（本地 naive）传参；
    万一某个环境不吃这套，再退回 UTC-naive 试一次。

    同样不 initialize/shutdown，理由见 get_rates_live。
    """
    from datetime import datetime, timezone

    from live.journal import _mt5

    conn = _mt5()
    ensure_symbol(symbol)
    flags = getattr(conn, "COPY_TICKS_ALL", 0)

    def local(ts: int) -> datetime:
        return datetime.fromtimestamp(int(ts))

    def as_utc(ts: int) -> datetime:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)

    ticks = None
    for build in (local, as_utc):
        ticks = conn.copy_ticks_range(symbol, build(from_ts), build(to_ts), flags)
        if ticks is not None and len(ticks):
            break
    if ticks is None:
        raise RuntimeError(f"取 tick 失败 {symbol}: {conn.last_error()}")

    out = []
    for t in ticks:
        names = t.dtype.names or ()
        out.append(
            {
                "time": int(t["time"]),
                "time_msc": int(t["time_msc"]) if "time_msc" in names else 0,
                "bid": float(t["bid"]),
                "ask": float(t["ask"]),
                "last": float(t["last"]) if "last" in names else 0.0,
                "volume": int(t["volume"]) if "volume" in names else 0,
            }
        )
    return out


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
