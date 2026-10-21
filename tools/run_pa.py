"""PA 参照策略回测：跑多年数据，输出指标 + 交易明细供人工核对

用法：
    python -m tools.run_pa                 # 2018-2026 全区间
    python -m tools.run_pa --year 2025     # 单年
    python -m tools.run_pa --detail 20     # 输出前 20 笔明细
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from backtest.engine import EngineConfig, run_backtest
from backtest.metrics import compute_metrics
from backtest.models import Bar
from strategies.pa_breakout_pullback import Strategy

ROOT = Path(__file__).resolve().parent.parent


def load_bars(year: int | None = None, csv_path: str | None = None) -> list[Bar]:
    bars: list[Bar] = []
    path = Path(csv_path) if csv_path else (ROOT / "data" / "XAUUSDm_H1.csv")
    with path.open(encoding="utf-8") as fh:
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
    if year is not None:
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        bars = [b for b in bars if start <= b.time < end]
    return bars


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PA 参照策略回测")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--detail", type=int, default=15, help="输出明细笔数")
    parser.add_argument("--spread", type=int, default=240)
    parser.add_argument("--csv", default=None, help="自定义 K 线 CSV")
    parser.add_argument("--param", action="append", default=[], help="策略参数 key=value")
    args = parser.parse_args()

    kwargs = {}
    for item in args.param:
        k, _, v = item.partition("=")
        low = v.lower()
        if low in ("true", "false"):
            kwargs[k.strip()] = low == "true"
        else:
            try:
                kwargs[k.strip()] = int(v)
            except ValueError:
                try:
                    kwargs[k.strip()] = float(v)
                except ValueError:
                    kwargs[k.strip()] = v

    bars = load_bars(args.year, args.csv)
    cfg = EngineConfig(initial_balance=10_000, lots=0.1, point=0.001, contract_size=100, spread_points=args.spread)
    result = run_backtest(bars, Strategy(**kwargs), cfg)
    metrics = compute_metrics(result)

    label = (f"{args.year} 年" if args.year else ("M1 全样本" if args.csv and "M1" in args.csv else "2018-2026 全区间"))
    print(f"=== PA 结构突破回踩 | {label} | {len(bars)} 根 H1 | 点差 {args.spread} ===")
    for k in ("net_profit", "profit_factor", "total_trades", "win_rate", "avg_win", "avg_loss",
              "max_drawdown", "max_drawdown_pct", "sharpe", "max_win_streak", "max_loss_streak"):
        print(f"  {k:<18} {metrics[k]}")

    print(f"\n=== 前 {args.detail} 笔明细（供人工核对结构逻辑）===")
    print(f"{'#':<3}{'方向':<5}{'入场时间':<17}{'入场价':>10}{'止损':>10}{'止盈':>10}{'出场':>10}{'盈亏':>9}{'原因':<9}")
    for idx, t in enumerate(result.trades[: args.detail], 1):
        side = "多" if t.direction > 0 else "空"
        sl = f"{t.sl:.2f}" if t.sl is not None else "-"
        tp = f"{t.tp:.2f}" if t.tp is not None else "-"
        print(
            f"{idx:<3}{side:<5}{fmt_time(t.entry_time):<17}{t.entry_price:>10.2f}"
            f"{sl:>10}{tp:>10}{t.exit_price:>10.2f}{t.profit:>9.2f}  {t.exit_reason:<9}"
        )


if __name__ == "__main__":
    main()
