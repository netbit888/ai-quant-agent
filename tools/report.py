"""账户报告：把逐笔盈亏变成净值曲线 + 绩效指标（quantstats）。

和 `tools/query.py` 的分工
--------------------------
query 回答"某一格数字是多少"（按打法分组、峰值回吐、冲突分组）；
report 回答"整体像不像一个能活的策略"（回撤、盈亏比、连亏、曲线形状）。

两种输出，对应两种诚实程度
--------------------------
1. **绝对口径**（不需要任何假设）：笔数、胜率、盈亏比、最大回撤（USD）、
   最长连亏。这些只需要 pnl 序列，永远可信。
2. **相对口径**（需要 `--balance`）：把每笔盈亏除以账户净值，得到收益率序列，
   再交给 quantstats 算 Sharpe / Sortino / Calmar / 月度表 / HTML 报告。
   **没有净值基数时这些指标没有意义**，所以不给就明确不输出，而不是拿 1.0
   当基数编一个数出来。

关于"一笔 = 一个交易日"
-----------------------
quantstats 的年化假设是按周期数（默认 252）。这里把**每一笔平仓**当成一个
周期传进去，所以 Sharpe 的含义是"每笔单的风险调整收益"，不是"每天"。
对日内策略这比硬套日频更接近事实，但别拿它和基金的 Sharpe 直接比。

样本要求
--------
`--min-trades`（默认 30）以下会打一条醒目警告：指标只用来验证流程通不通，
不用来做决策。5 笔单算出来的 Sharpe 是噪声。

用法
----
    python tools/report.py --balance 1000
    python tools/report.py --balance 1000 --html report.html
    python tools/report.py --account 165106125 --balance 500 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import inspect_log  # noqa: E402  同目录工具，复用"读日志 + 过滤"

# 把"一笔单"当一个周期；见模块 docstring 里那段说明。
PERIODS_PER_YEAR = 252


def _closes(rows: list[dict]) -> list[dict]:
    """只留平仓行，按时间排序，丢掉没有 pnl 的坏行。"""
    out = [r for r in rows
           if r.get("kind") == "close" and r.get("pnl") is not None]
    out.sort(key=lambda r: int(r.get("ts") or 0))
    return out


def absolute_stats(closes: list[dict]) -> dict:
    """不需要净值基数就能算的口径。"""
    pnls = [float(r.get("pnl") or 0.0) for r in closes]
    if not pnls:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flats = [p for p in pnls if p == 0]
    # 累计盈亏曲线的最大回撤：从任一历史高点跌下来的最大值
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    # 最长连亏（0.00 既不算赢也不算亏，按"中断"处理）
    worst = cur = 0
    for p in pnls:
        cur = cur + 1 if p < 0 else 0
        worst = max(worst, cur)
    return {
        "n": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": round(len(wins) / len(pnls), 4),
        "total": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) else None),
        "max_drawdown": round(max_dd, 2),
        "max_win": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
        "longest_loss_streak": worst,
        "flat_trades": len(flats),
    }


def build_series(closes: list[dict], balance: float):
    """净值曲线与收益率序列。索引用真实平仓时间（本地时区）。"""
    import pandas as pd

    if balance <= 0:
        raise ValueError("--balance 必须是正数")
    if not closes:
        raise ValueError("没有可用的平仓记录")
    idx = pd.to_datetime([int(r.get("ts") or 0) for r in closes], unit="s")
    # 同一秒两笔（几乎不会发生）也要能建索引：去掉重复时间戳的冲突
    pnl = pd.Series([float(r.get("pnl") or 0.0) for r in closes], index=idx)
    pnl = pnl.groupby(level=0).sum()
    equity = balance + pnl.cumsum()
    returns = pnl / balance
    return equity, returns


def print_absolute(stats: dict) -> None:
    def line(label: str, value) -> None:
        print(f"  {label:<12} {value}")
    print("绝对口径（不需要净值基数）")
    if not stats.get("n"):
        print("  没有平仓记录。")
        return
    line("笔数", stats["n"])
    line("胜/负/平", f"{stats['wins']} / {stats['losses']} / {stats['flats']}")
    line("胜率", f"{stats['win_rate']:.1%}")
    line("总盈亏", f"{stats['total']:+.2f}")
    line("单笔均值", f"{stats['avg']:+.2f}")
    line("平均盈利", f"{stats['avg_win']:+.2f}")
    line("平均亏损", f"{stats['avg_loss']:+.2f}")
    line("盈亏比", stats["profit_factor"] if stats["profit_factor"] is not None
         else "无亏损单")
    line("最大回撤", f"-{stats['max_drawdown']:.2f}")
    line("最大单笔", f"{stats['max_win']:+.2f} / {stats['max_loss']:+.2f}")
    line("最长连亏", f"{stats['longest_loss_streak']} 笔")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="账户绩效报告（逐笔 pnl → 净值曲线）")
    ap.add_argument("--account", help="账户号，默认当前登录账户")
    ap.add_argument("--balance", type=float,
                    help="账户净值基数（决定收益率与 Sharpe 的量纲，必填才能出相对指标）")
    ap.add_argument("--html", metavar="PATH", help="生成 quantstats HTML 报告（需要 --balance）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（绝对口径）")
    ap.add_argument("--min-trades", type=int, default=30,
                    help="样本低于这个数就打警告（默认 30）")
    args = ap.parse_args(argv)

    if args.html and not args.balance:
        print("生成 HTML 需要 --balance：没有净值基数，收益率与回撤都是没有意义的数。",
              file=sys.stderr)
        return 2

    rows = inspect_log.load_rows(args.account, kind="close")
    closes = _closes(rows)
    if not closes:
        print("没有平仓记录。")
        return 0

    stats = absolute_stats(closes)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return 0

    if stats["n"] < args.min_trades:
        print(f"！样本 {stats['n']} 笔 < {args.min_trades}：下面的指标只用于验证流程，"
              f"不要用来做决策。\n")
    print_absolute(stats)

    if not args.balance:
        print("\n（加 --balance <净值> 才有 Sharpe / 回撤率 / 月度表；"
              "没基数时不编这些数。）")
        return 0

    try:
        import quantstats as qs
    except ImportError:
        print("\n没装 quantstats：pip install -r requirements-dev.txt", file=sys.stderr)
        return 2

    _, returns = build_series(closes, args.balance)
    print(f"\n相对口径（净值基数 {args.balance:g}，把每笔当 1 个周期，"
          f"年化按 {PERIODS_PER_YEAR} 周期算）")
    try:
        table = qs.reports.metrics(returns, mode="basic", display=False,
                                   periods=PERIODS_PER_YEAR)
        print(table.to_string())
    except Exception as exc:  # noqa: BLE001
        print(f"  指标算不出来（样本太少？）：{type(exc).__name__}: {exc}")

    if args.html:
        dest = Path(args.html)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            qs.reports.html(returns, output=str(dest), periods=PERIODS_PER_YEAR,
                            title="SeatOne 账户报告")
            print(f"\nHTML → {dest}")
        except Exception as exc:  # noqa: BLE001
            print(f"  HTML 生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
