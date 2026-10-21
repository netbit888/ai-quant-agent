"""从行情数据重算交易画像（`data/live/profile.json`）。

用途
----
评分里的「盈亏平衡边际」需要知道点差相对波动有多大。这个比例会随波动率变化，
所以要能从数据重算，而不是写死在代码里。

用法
----
    python -m tools.profile                      # 默认 XAUUSDm M1
    python -m tools.profile --tf H4              # 换周期
    python -m tools.profile --dry-run
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import aqa
from backtest.indicators import atr

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "live" / "profile.json"


def measure(symbol: str, tf: str) -> dict:
    path = aqa.data_path(symbol, tf)
    if not path.exists():
        raise SystemExit(f"缺少数据：{path.name}（先跑 aqa.py data pull）")
    bars = aqa.read_bars(path)
    if len(bars) < 100:
        raise SystemExit(f"{path.name} 只有 {len(bars)} 根，太少")

    point = aqa.SYMBOLS.get(symbol, {}).get("point", 0.001)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    ranges = [(b.high - b.low) / point for b in bars]
    atrs = [v for v in atr(highs, lows, closes, 14) if v]
    spreads = sorted(b.spread for b in bars)

    return {
        "symbol": symbol,
        "timeframe": tf.upper(),
        "style": "scalping" if tf.upper() in ("M1", "M5") else "intraday",
        "point": point,
        "spread_points": int(statistics.median(spreads)),
        "range_points": round(statistics.median(ranges)),
        "atr_points": round(statistics.median(atrs) / point) if atrs else None,
        "bars": len(bars),
        "from": datetime.fromtimestamp(bars[0].time, timezone.utc).strftime("%Y-%m-%d"),
        "to": datetime.fromtimestamp(bars[-1].time, timezone.utc).strftime("%Y-%m-%d"),
        "measured_from": f"data/{path.name}",
        "measured_at": int(time.time()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="重算交易画像")
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--tf", default="M1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    prof = measure(args.symbol, args.tf)
    rng = prof["range_points"] or 0
    atr_pts = prof["atr_points"] or 0
    prof["note"] = (
        f"点差占 {args.tf.upper()} 振幅约 {prof['spread_points'] / rng * 100:.1f}%。"
        "波动越低占比越高，低波动期可能无法盈利。"
    ) if rng else ""

    print(f"{prof['symbol']} {prof['timeframe']}  {prof['from']} ~ {prof['to']}  {prof['bars']} 根")
    print(f"  点差中位   {prof['spread_points']} pt")
    print(f"  振幅中位   {rng} pt")
    print(f"  ATR(14)    {atr_pts} pt")
    if rng:
        print(f"  点差/振幅  {prof['spread_points'] / rng * 100:.1f}%")
    if atr_pts:
        print(f"  点差/ATR   {prof['spread_points'] / atr_pts * 100:.1f}%")

    if args.dry_run:
        print("\n--dry-run：未写入")
        return
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT_FILE}")


if __name__ == "__main__":
    main()
