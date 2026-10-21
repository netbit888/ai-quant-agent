"""参数稳健性扫描：训练段选参数，测试段验证。

要回答的问题不是「哪组参数最赚钱」，而是：
    当前这组参数是不是踩在一个孤立的尖峰上（数据窥探的产物）？

判定方式：
    在训练段（默认 2017-2023）上跑完整网格，再按训练段表现挑出最优，
    观察它在测试段（默认 2024-至今）是否仍然有效；同时统计整个网格里
    训练/测试**双绿**的比例——比例高说明这是一个稳定区域，不是运气。

用法：
    python -m tools.sweep --strategy pa_breakout_pullback --symbol XAUUSDm --tf H4
    python -m tools.sweep --grid max_atr_ratio=0,2,2.5,3 --grid rr_target=1.5,2,2.5
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aqa
from backtest.engine import run_backtest
from backtest.metrics import compute_metrics
from risk import RiskConfig, RiskGuard

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "evaluations"

DEFAULT_GRID = {
    "max_atr_ratio": [0.0, 1.5, 2.0, 2.5, 3.0, 4.0],
    "min_structure_points": [0.0, 10_000.0, 15_000.0, 20_000.0, 25_000.0],
    "rr_target": [1.5, 2.0, 2.5, 3.0],
}


def year_slice(bars: list, year_from: int, year_to: int) -> list:
    lo = int(datetime(year_from, 1, 1, tzinfo=timezone.utc).timestamp())
    hi = int(datetime(year_to + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return [b for b in bars if lo <= b.time < hi]


def run_one(cls, params: dict, bars: list, cfg, risk_pct: float) -> dict:
    guard = RiskGuard(RiskConfig(risk_per_trade_pct=risk_pct, max_drawdown_pct=30)) if risk_pct > 0 else None
    m = compute_metrics(run_backtest(bars, cls(**params), cfg, guard=guard))
    return {
        "net": round(m["net_profit"], 2),
        "trades": m["total_trades"],
        "pf": round(m["profit_factor"] or 0, 3),
        "dd": round(m["max_drawdown_pct"], 2),
        "win_rate": round(m["win_rate"], 1),
    }


def parse_grid(items: list[str]) -> dict:
    grid = {}
    for item in items:
        key, _, raw = item.partition("=")
        vals = []
        for v in raw.split(","):
            v = v.strip()
            try:
                vals.append(int(v))
            except ValueError:
                vals.append(float(v))
        grid[key.strip()] = vals
    return grid


def main() -> None:
    ap = argparse.ArgumentParser(description="参数稳健性扫描")
    ap.add_argument("--strategy", default="pa_breakout_pullback")
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--tf", default="H4")
    ap.add_argument("--balance", type=float, default=3000)
    ap.add_argument("--risk", type=float, default=2.0)
    ap.add_argument("--train", default="2017-2023", help="训练段 起-止 年")
    ap.add_argument("--test", default="2024-2026", help="测试段 起-止 年")
    ap.add_argument("--grid", action="append", default=[], help="key=v1,v2,...，可多次；不给则用内置网格")
    ap.add_argument("--spread", type=int, default=0, help="0=按品种默认点差")
    args = ap.parse_args()

    grid = parse_grid(args.grid) if args.grid else DEFAULT_GRID
    train_from, train_to = (int(x) for x in args.train.split("-"))
    test_from, test_to = (int(x) for x in args.test.split("-"))

    bars_all = aqa.read_bars(aqa.data_path(args.symbol, args.tf))
    train = year_slice(bars_all, train_from, train_to)
    test = year_slice(bars_all, test_from, test_to)
    if len(train) < 500 or len(test) < 300:
        print(f"样本不足：train {len(train)} / test {len(test)}")
        return

    cls = aqa.load_strategy_class(args.strategy)
    spec = aqa.SYMBOLS.get(args.symbol, {})
    cfg = aqa.engine_cfg(args.symbol, args.balance)
    if args.spread:
        cfg = type(cfg)(**{**cfg.__dict__, "spread_points": args.spread})
    base = aqa.strategy_params(args.symbol, {})

    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"策略 {args.strategy} | {args.symbol} {args.tf} | 点差 {cfg.spread_points} points")
    print(f"训练 {train_from}-{train_to}（{len(train)} 根） | 测试 {test_from}-{test_to}（{len(test)} 根）")
    print(f"网格 {len(combos)} 组：{', '.join(f'{k}={len(grid[k])}' for k in keys)}\n")

    rows = []
    t0 = time.time()
    for n, combo in enumerate(combos, 1):
        override = dict(zip(keys, combo))
        params = {**base, **override}
        tr = run_one(cls, params, train, cfg, args.risk)
        te = run_one(cls, params, test, cfg, args.risk)
        rows.append({"params": override, "train": tr, "test": te})
        print(f"[{n:>3}/{len(combos)}] {override}  train {tr['net']:>8.0f}/PF{tr['pf']:.2f}/{tr['trades']}笔  "
              f"test {te['net']:>8.0f}/PF{te['pf']:.2f}/{te['trades']}笔", flush=True)

    # ---- 稳健性判定 ----
    both_ok = [r for r in rows if r["train"]["pf"] >= 1.2 and r["test"]["pf"] >= 1.2
               and r["train"]["net"] > 0 and r["test"]["net"] > 0]
    train_ok = [r for r in rows if r["train"]["pf"] >= 1.2 and r["train"]["net"] > 0]
    ratio_all = len(both_ok) / len(rows)
    ratio_cond = len(both_ok) / len(train_ok) if train_ok else 0

    print("\n=== 稳健性 ===")
    print(f"训练段达标（PF>=1.2 且盈利）: {len(train_ok)}/{len(rows)}")
    print(f"其中测试段仍然达标        : {len(both_ok)}/{len(train_ok)} = {ratio_cond:.0%}")
    print(f"全网格双绿比例            : {ratio_all:.0%}")

    best = max(rows, key=lambda r: r["train"]["net"])
    print("\n=== 训练段最优（这就是『调参调出来的答案』）===")
    print(f"参数 {best['params']}")
    print(f"训练 净利 {best['train']['net']:.0f} PF {best['train']['pf']:.2f} 回撤 {best['train']['dd']}% {best['train']['trades']}笔")
    print(f"测试 净利 {best['test']['net']:.0f} PF {best['test']['pf']:.2f} 回撤 {best['test']['dd']}% {best['test']['trades']}笔")

    cur = {"max_atr_ratio": 2.5, "min_structure_points": 15_000.0, "rr_target": 2.0}
    current = next((r for r in rows if all(abs(r["params"].get(k, v) - v) < 1e-9 for k, v in cur.items() if k in r["params"])), None)
    if current:
        print("\n=== 当前生产参数 ===")
        print(f"参数 {current['params']}")
        print(f"训练 净利 {current['train']['net']:.0f} PF {current['train']['pf']:.2f} 回撤 {current['train']['dd']}% {current['train']['trades']}笔")
        print(f"测试 净利 {current['test']['net']:.0f} PF {current['test']['pf']:.2f} 回撤 {current['test']['dd']}% {current['test']['trades']}笔")
        rank = sorted(rows, key=lambda r: -r["train"]["net"]).index(current) + 1
        print(f"训练段排名 {rank}/{len(rows)}")
        nbrs = neighbor_avg(rows, current, keys)
        if nbrs:
            print(f"邻域平均（相邻参数格） 训练 PF {nbrs['train_pf']:.2f} / 测试 PF {nbrs['test_pf']:.2f}"
                  f" → {'平滑过渡，非尖峰' if nbrs['test_pf'] >= 1.1 else '存在塌陷，参数敏感'}")

    out = OUT_DIR / f"sweep_{args.strategy}_{args.symbol}_{args.tf}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "strategy": args.strategy, "symbol": args.symbol, "tf": args.tf,
        "train_window": [train_from, train_to], "test_window": [test_from, test_to],
        "spread": cfg.spread_points, "grid": grid, "rows": rows,
        "robustness": {"train_ok": len(train_ok), "both_ok": len(both_ok),
                       "ratio_of_train_ok": round(ratio_cond, 3), "ratio_all": round(ratio_all, 3)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n耗时 {time.time() - t0:.0f}s → {out.name}")


def neighbor_avg(rows: list[dict], current: dict, keys: list[str]) -> dict | None:
    """当前格在每一维上下相邻一格的平均表现。全维度各取 ±1 一档。"""
    import itertools as it

    cur_vals = current["params"]
    vals_by_key = {k: sorted({r["params"][k] for r in rows}) for k in keys}

    def idx(key, v):
        vs = vals_by_key[key]
        return vs.index(v) if v in vs else -1

    combos = []
    for k in keys:
        i = idx(k, cur_vals[k])
        opts = [cur_vals[k]]
        if i > 0:
            opts.append(vals_by_key[k][i - 1])
        if i >= 0 and i < len(vals_by_key[k]) - 1:
            opts.append(vals_by_key[k][i + 1])
        combos.append(opts)

    picked = []
    for combo in it.product(*combos):
        if combo == tuple(cur_vals[k] for k in keys):
            continue
        for r in rows:
            if all(abs(r["params"][k] - v) < 1e-9 for k, v in zip(keys, combo)):
                picked.append(r)
                break
    if not picked:
        return None
    return {
        "n": len(picked),
        "train_pf": sum(r["train"]["pf"] for r in picked) / len(picked),
        "test_pf": sum(r["test"]["pf"] for r in picked) / len(picked),
    }


if __name__ == "__main__":
    main()
