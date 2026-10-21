"""多品种组合回测

用法：
    python -m tools.run_portfolio
    python -m tools.run_portfolio --risk 4
    python -m tools.run_portfolio --symbols XAUUSDm,EURUSDm

品种参数按各自的 H4 结构尺度预设（min_structure_points 约为 swing 距离中位数的 0.8 倍）。
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from backtest.engine import EngineConfig
from backtest.models import Bar
from portfolio import PortfolioItem, run_portfolio
from risk import RiskConfig
from strategies.pa_breakout_pullback import Strategy

ROOT = Path(__file__).resolve().parent.parent

# symbol -> (point, contract_size, spread, 策略参数)
PRESETS = {
    "XAUUSDm": (0.001, 100.0, 180, dict(min_structure_points=15000, min_stop_points=8000, pullback_tol_points=400)),
    "EURUSDm": (0.00001, 100000.0, 12, dict(min_structure_points=250, min_stop_points=150, pullback_tol_points=20)),
    "GBPUSDm": (0.00001, 100000.0, 12, dict(min_structure_points=300, min_stop_points=200, pullback_tol_points=25)),
}


def load(path: Path) -> list[Bar]:
    bars: list[Bar] = []
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
    return bars


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="多品种组合回测")
    parser.add_argument("--symbols", default="XAUUSDm,EURUSDm,GBPUSDm")
    parser.add_argument("--balance", type=float, default=3000)
    parser.add_argument("--risk", type=float, default=3.0, help="组合总风险 %，按品种均分")
    parser.add_argument("--rr", type=float, default=3.0)
    parser.add_argument("--json", default=None, help="结果保存路径")
    args = parser.parse_args()

    items: list[PortfolioItem] = []
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        if sym not in PRESETS:
            print(f"跳过未知品种 {sym}（预设里没有）")
            continue
        point, contract, spread, params = PRESETS[sym]
        path = ROOT / "data" / f"{sym}_H4.csv"
        if not path.exists():
            print(f"跳过 {sym}：缺少 {path.name}")
            continue
        bars = load(path)
        # point 必须传进策略：策略内部用 point 把价格换算成 points，
        # 各品种 point 不同（黄金 0.001 / 外汇 0.00001 / 指数 0.1），漏传会让结构阈值差 100 倍
        params = {**params, "rr_target": args.rr, "point": point}

        items.append(
            PortfolioItem(
                symbol=sym,
                bars=bars,
                strategy_factory=lambda p=params: Strategy(**p),
                cfg=EngineConfig(initial_balance=0, lots=0.1, point=point, contract_size=contract, spread_points=spread),
                risk=RiskConfig(risk_per_trade_pct=args.risk, max_drawdown_pct=30),
            )
        )

    if not items:
        print("没有可用品种")
        return

    t0 = time.time()
    print(f"组合：{', '.join(i.symbol for i in items)}")
    print(f"资金 {args.balance:.0f} USD，总风险 {args.risk}%（每品种 {args.risk / len(items):.2f}%）\n")

    result = run_portfolio(items, total_balance=args.balance, total_risk_pct=args.risk)

    print("=== 各品种单独表现（分到的资金独立计算）===")
    print(f"{'品种':<12}{'初始':>8}{'最终':>9}{'收益%':>9}{'PF':>7}{'回撤%':>8}{'笔数':>6}{'胜率':>7}")
    for sym, m in result["per_symbol"].items():
        print(
            f"{sym:<12}{m['initial']:>8.0f}{m['final']:>9.0f}{m['return_pct']:>9.1f}"
            f"{m['profit_factor'] or 0:>7.2f}{m['max_drawdown_pct']:>8.1f}{m['total_trades']:>6}{m['win_rate']:>7.1f}"
        )

    print("\n=== 组合表现 ===")
    print(f"  区间        {result['years']} 年")
    print(f"  初始资金    {result['initial']:.0f}")
    print(f"  最终资金    {result['final']:.0f}")
    print(f"  总收益      {result['return_pct']:.1f}%")
    print(f"  年化 (CAGR) {result['cagr']:.2f}%")
    print(f"  最大回撤    {result['max_drawdown']:.2f} ({result['max_drawdown_pct']:.1f}%)")

    print("\n=== 品种相关性（逐 bar 收益）===")
    for pair, c in result["corr"].items():
        tag = "低相关 ✓" if abs(c) < 0.3 else "中等" if abs(c) < 0.6 else "高相关 ✗"
        print(f"  {pair:<24} {c:>7.3f}  {tag}")

    print(f"\n耗时 {time.time() - t0:.0f}s")

    if args.json:
        out = {k: v for k, v in result.items() if k != "curve"}
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存：{args.json}")


if __name__ == "__main__":
    main()
