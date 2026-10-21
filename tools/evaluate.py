"""策略评估：逐年表现 + 成本压力 + Gate 2 判定

用法：
    python -m tools.evaluate --strategy strategies.pa_breakout_pullback
    python -m tools.evaluate --strategy strategies.pa_breakout_pullback --skip-stress

输出：
    - 逐年独立回测（walk-forward 的简化版：每年重置资金，检验稳定性）
    - 成本压力（点差 240 / 400 / 600）
    - Gate 2 判定：是否具备进入实盘验证的资格
"""

from __future__ import annotations

import csv
import importlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest.engine import EngineConfig, run_backtest
from risk import RiskConfig, RiskGuard
from backtest.metrics import compute_metrics
from backtest.models import Bar

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "evaluations"


def load_all() -> list[Bar]:
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
    return bars


def slice_year(bars: list[Bar], year: int) -> list[Bar]:
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return [b for b in bars if start <= b.time < end]


def load_strategy(spec: str):
    module_name, _, cls_name = spec.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, cls_name or "Strategy")


def parse_value(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def make_cfg(spread: int, balance: float = 10_000) -> EngineConfig:
    return EngineConfig(
        initial_balance=balance,
        lots=0.1,
        point=0.001,
        contract_size=100,
        spread_points=spread,
    )


def eval_years(bars: list[Bar], make_strategy, spread: int, risk_pct: float = 2.0, balance: float = 10_000) -> list[dict]:
    """逐年独立回测。默认带风控（risk 2%）——不传风控等于评估"固定手数会怎样"，
    那不是策略能力，而是仓位失控的后果。"""
    years = sorted({datetime.fromtimestamp(b.time, timezone.utc).year for b in bars})
    rows: list[dict] = []
    for year in years:
        subset = slice_year(bars, year)
        if len(subset) < 300:
            continue
        guard = RiskGuard(RiskConfig(risk_per_trade_pct=risk_pct, max_drawdown_pct=30)) if risk_pct > 0 else None
        metrics = compute_metrics(run_backtest(subset, make_strategy(), make_cfg(spread, balance), guard=guard))
        rows.append({"year": year, "bars": len(subset), **metrics})
    return rows


def eval_stress(bars: list[Bar], make_strategy, spreads: tuple[int, ...], risk_pct: float = 2.0, balance: float = 10_000) -> list[dict]:
    rows: list[dict] = []
    for spread in spreads:
        guard = RiskGuard(RiskConfig(risk_per_trade_pct=risk_pct, max_drawdown_pct=30)) if risk_pct > 0 else None
        metrics = compute_metrics(run_backtest(bars, make_strategy(), make_cfg(spread, balance), guard=guard))
        rows.append({"spread": spread, **metrics})
    return rows


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="策略评估")
    parser.add_argument("--strategy", required=True, help="模块:类名，如 strategies.pa_breakout_pullback")
    parser.add_argument("--spread", type=int, default=240)
    parser.add_argument("--skip-stress", action="store_true")
    parser.add_argument("--risk", type=float, default=2.0, help="单笔风险 %%，0=关闭风控")
    parser.add_argument("--balance", type=float, default=3000)
    parser.add_argument("--years", help="逗号分隔指定年份，如 2024,2025")
    parser.add_argument("--param", action="append", default=[], help="策略参数 key=value，可多次")
    args = parser.parse_args()

    strategy_cls = load_strategy(args.strategy)
    kwargs = {}
    for item in args.param:
        key, _, value = item.partition("=")
        kwargs[key.strip()] = parse_value(value.strip())

    def make_strategy():
        return strategy_cls(**kwargs)

    bars = load_all()
    if args.years:
        keep = {int(y) for y in args.years.split(",")}
        bars = [b for b in bars if datetime.fromtimestamp(b.time, timezone.utc).year in keep]

    t0 = time.time()
    print(f"策略：{args.strategy}\n数据：{len(bars)} 根 H1\n")

    print("=== 逐年表现（每年重置资金，检验稳定性）===")
    print(f"{'年份':<7}{'bars':>6}{'净盈利':>10}{'笔数':>7}{'胜率':>8}{'PF':>7}{'回撤%':>8}")
    yearly = eval_years(bars, make_strategy, args.spread, args.risk, args.balance)
    profitable_years = 0
    for row in yearly:
        pf = row["profit_factor"] or 0
        if row["net_profit"] > 0:
            profitable_years += 1
        print(
            f"{row['year']:<7}{row['bars']:>6}{row['net_profit']:>10}{row['total_trades']:>7}"
            f"{row['win_rate']:>8}{pf:>7}{row['max_drawdown_pct']:>8}"
        )

    stress = []
    if not args.skip_stress:
        print("\n=== 成本压力（全区间）===")
        print(f"{'点差':<7}{'净盈利':>10}{'笔数':>7}{'胜率':>8}{'PF':>7}{'回撤%':>8}")
        stress = eval_stress(bars, make_strategy, (240, 400, 600, 1000), args.risk, args.balance)
        for row in stress:
            pf = row["profit_factor"] or 0
            print(
                f"{row['spread']:<7}{row['net_profit']:>10}{row['total_trades']:>7}"
                f"{row['win_rate']:>8}{pf:>7}{row['max_drawdown_pct']:>8}"
            )

    # ---- Gate 2 判定 ----
    total_years = len(yearly)
    win_year_ratio = profitable_years / total_years if total_years else 0
    avg_pf = sum((r["profit_factor"] or 0) for r in yearly) / total_years if total_years else 0
    worst_dd = max((r["max_drawdown_pct"] for r in yearly), default=100)
    stress_ok = True
    if stress:
        s600 = next((r for r in stress if r["spread"] == 600), None)
        stress_ok = bool(s600 and s600["net_profit"] > 0)

    checks = {
        "盈利年份占比 >= 70%": win_year_ratio >= 0.7,
        "逐年平均 PF >= 1.2": avg_pf >= 1.2,
        "最差年度回撤 <= 30%": worst_dd <= 30,
        "点差 600 仍盈利": stress_ok,
    }

    print("\n=== Gate 2 判定 ===")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    verdict = all(checks.values())
    print(f"\n结论：{'通过，可进入实盘验证阶段' if verdict else '未通过，需要改策略逻辑（不是调参数）'}")
    print(f"总耗时 {time.time() - t0:.0f}s")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.strategy.replace(':', '_').replace('.', '_')}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {"strategy": args.strategy, "yearly": yearly, "stress": stress, "checks": checks, "verdict": verdict},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"结果已保存：{out}")


if __name__ == "__main__":
    main()
