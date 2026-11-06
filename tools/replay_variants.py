"""出场规则反事实回放：同一批真实入场，换五种出场，看单笔期望怎么变。

它回答的问题
------------
今天两笔趋势单峰值 4.75 / 4.88，结果都是 0.00（保本线在 3.25 被抬到开仓价）。
这是运气还是规则决定的？把历史所有真实入场点重放一遍就知道：
同一批入场、同样的 K 线，只换出场规则，单笔期望差多少。

**这不是回测**：入场点是当时真实成交的，不重新生成；出场变长会让后续入场
变少，所以"做几笔"不能外推。它只回答"同样这批入场质量，换出场规则值不值"。

五个版本
--------
    A 现状   sl 3.0 / 峰值到 3.25 把止损抬到开仓价 / tp 7.0 / 180 分钟
    B 抬保本 sl 3.0 / 峰值到 5.0 才抬 / tp 7.0 / 180 分钟
    C 跟踪   峰值回吐 1.5×ATR 走（不低于初始止损）
    D 跟踪   峰值回吐 2.0×ATR 走（同上）
    E 不保本 sl 3.0 / tp 7.0 / 180 分钟（只靠硬止损和时间）

口径与假设（都会影响结果，必须知道）
------------------------------------
1. **一根 K 线内的先后无法还原**：同一根里既碰止损又碰止盈时，一律按
   **先碰止损**处理——对"放宽出场"的方案是偏保守的估计。
2. 成交价按触发价算（止损单按 stop 价成交），忽略跳空滑点。
3. 保本线在"峰值达标的那根之后"才生效，不在同一根里立刻生效。
4. 盈亏单位：`--mult`（账户货币 / 价格美元）。本账户 0.01 手 XAUUSDc 实测
   = 1.0（peak 4.88 ≈ 3.15 × ATR 1.55，两者同数），默认 1.0。
5. 纸面单（dry_run）默认跳过：它们没有真实成交。

用法
----
    python tools/replay_variants.py                 # 全部历史入场点
    python tools/replay_variants.py --only-plan trend
    python tools/replay_variants.py --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYMBOL = "XAUUSDc"
ATR_N = 14
HOLD_CAP_SEC = 180 * 60          # 与 max_hold_trend 对齐
SL_USD = 3.0
TP_USD = 7.0


# ------------------------------------------------------------------ 读样本


def load_trades(only_plan: str | None = None) -> list[dict]:
    """扫描所有流水（含归档），按 ticket 配对开平仓。"""
    files = sorted((ROOT / "data" / "live" / "accounts").glob("*/autotrade_log*.jsonl"))
    trades = []
    for path in files:
        if path.name.endswith(".bak-pnlfix") or path.name.endswith(".jsonl.tmp"):
            continue
        opens: dict[str, dict] = {}
        closes: dict[str, dict] = {}
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = str(r.get("ticket") or "")
            if not t:
                continue
            if r.get("kind") == "open":
                opens[t] = r
            elif r.get("kind") == "close":
                closes[t] = r
        for ticket, o in opens.items():
            c = closes.get(ticket)
            if not c:
                continue
            if o.get("dry_run"):
                continue
            plan = o.get("plan")
            if only_plan and plan != only_plan:
                continue
            trades.append({
                "ticket": ticket,
                "file": path.name,
                "ts_open": int(o.get("ts") or 0),
                "ts_close": int(c.get("ts") or 0),
                "direction": int(o.get("direction") or 1),
                "price_open": float(o.get("price") or 0.0),
                "lots": float(o.get("lots") or 0.0),
                "pnl_actual": float(c.get("pnl") or 0.0),
                "source_actual": c.get("source"),
                "plan": plan,
                "symbol": o.get("symbol") or SYMBOL,
            })
    trades.sort(key=lambda t: t["ts_open"])
    return trades


# ------------------------------------------------------------------ K 线


def fetch_bars(symbol: str, ts_from: int, ts_to: int) -> list[dict]:
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f"MT5 连接失败：{mt5.last_error()}")
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, ts_from, ts_to)
    if rates is None:
        return []
    return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])} for r in rates]


def atr_at(bars: list[dict], idx: int, n: int = ATR_N) -> float:
    """入场时刻的 ATR：只用**入场前**的 K 线，不许偷看后面。"""
    seg = bars[max(0, idx - n):idx]
    if len(seg) < 3:
        return 0.0
    ranges = []
    for i in range(1, len(seg)):
        prev = seg[i - 1]["close"]
        hi, lo = seg[i]["high"], seg[i]["low"]
        ranges.append(max(hi - lo, abs(hi - prev), abs(lo - prev)))
    return sum(ranges) / len(ranges) if ranges else 0.0


# ------------------------------------------------------------------ 出场变体


def _stop_for(variant: dict, peak: float, atr: float, stopped: bool) -> float:
    """当前这一根适用的止损位（正数=favorable 方向的浮盈）。"""
    stop = -SL_USD
    if variant["kind"] == "be":
        if peak >= variant["arm"]:
            stop = max(stop, 0.0)
    elif variant["kind"] == "trail":
        if atr > 0:
            stop = max(stop, peak - variant["k"] * atr)
    return stop


VARIANTS = {
    "A 现状": {"kind": "be", "arm": 3.25},
    "B 保本抬到5": {"kind": "be", "arm": 5.0},
    "C 跟踪1.5ATR": {"kind": "trail", "k": 1.5},
    "D 跟踪2.0ATR": {"kind": "trail", "k": 2.0},
    "E 不保本": {"kind": "none"},
}


def simulate(bars: list[dict], start_idx: int, tr: dict, variant: dict,
             mult: float) -> dict:
    """从入场后第一根开始逐根推进，返回这一笔的结果。"""
    d = tr["direction"]
    entry = tr["price_open"]
    atr = atr_at(bars, start_idx)
    peak = 0.0
    mfe = 0.0
    mae = 0.0
    stop = -SL_USD
    deadline = tr["ts_open"] + HOLD_CAP_SEC

    for i in range(start_idx, len(bars)):
        b = bars[i]
        if b["time"] > deadline:
            return _out(peak, peak, mfe, mae, -SL_USD, "timeout", b, tr, entry, mult, i - start_idx)
        # 上一根结尾的止损位，先用来判这一根的低点（保守：先碰止损）
        fav_hi = (b["high"] - entry) * d
        adv_lo = (b["low"] - entry) * d
        mfe = max(mfe, fav_hi)
        mae = min(mae, adv_lo)

        if adv_lo <= stop:
            kind = "be" if stop >= 0 else "stop"
            return _out(peak, stop, mfe, mae, stop, kind, b, tr, entry, mult, i - start_idx)
        if TP_USD > 0 and fav_hi >= TP_USD:
            return _out(peak, TP_USD, mfe, mae, TP_USD, "tp", b, tr, entry, mult, i - start_idx)
        peak = max(peak, fav_hi)
        stop = _stop_for(variant, peak, atr, False)

    last = bars[-1] if bars else {"close": entry}
    pnl = (last["close"] - entry) * d
    return _out(peak, pnl, mfe, mae, pnl, "eod", last, tr, entry, mult, len(bars) - start_idx)


def _out(peak, exit_val, mfe, mae, pnl, reason, bar, tr, entry, mult, held) -> dict:
    return {"pnl": round(pnl * mult, 2), "reason": reason,
            "mfe": round(mfe * mult, 2), "mae": round(mae * mult, 2),
            "peak": round(peak * mult, 2),
            "held_bars": held}


# ------------------------------------------------------------------ 汇总


def summarize(name: str, rows: list[dict]) -> dict:
    pnls = sorted(r["pnl"] for r in rows)
    n = len(pnls)
    if not n:
        return {"name": name, "n": 0}
    mean = sum(pnls) / n
    cum = peak = 0.0
    ddown = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        ddown = max(ddown, peak - cum)
    zeros = sum(1 for p in pnls if p == 0)
    return {
        "name": name, "n": n,
        "mean": round(mean, 2),
        "median": round(st.median(pnls), 2),
        "p25": round(pnls[max(0, int(n * 0.25) - 1)], 2),
        "worst": round(pnls[0], 2),
        "zero_pct": round(100 * zeros / n, 1),
        "total": round(sum(pnls), 2),
        "max_dd": round(ddown, 2),
        "tp_pct": round(100 * sum(1 for r in rows if r["reason"] == "tp") / n, 1),
        "stop_pct": round(100 * sum(1 for r in rows if r["reason"] == "stop") / n, 1),
        "be_pct": round(100 * sum(1 for r in rows if r["reason"] == "be") / n, 1),
        "mfe325": round(100 * sum(1 for r in rows if r["mfe"] >= 3.25) / n, 1),
        "mfe5": round(100 * sum(1 for r in rows if r["mfe"] >= 5.0) / n, 1),
        "mfe7": round(100 * sum(1 for r in rows if r["mfe"] >= 7.0) / n, 1),
        "mfE": round(sum(r["mfe"] for r in rows) / n, 2),
        "mae": round(sum(r["mae"] for r in rows) / n, 2),
        "capture": _capture(rows),
    }


def _capture(rows: list[dict]) -> float:
    """捕获率 = 落袋 / 最大浮盈（只统计 MFE > 0 的单）。"""
    num = den = 0.0
    for r in rows:
        if r["mfe"] > 0:
            num += r["pnl"]
            den += r["mfe"]
    return round(100 * num / den, 1) if den > 0 else 0.0


COLS = ("name", "n", "mean", "median", "p25", "worst", "zero_pct", "tp_pct",
        "stop_pct", "be_pct", "mfE", "mfe325", "mfe5", "mfe7", "mae",
        "capture", "total", "max_dd")


def _print(rows: list[dict]) -> None:
    head = ["版本", "笔数", "均值", "中位", "25分位", "最差", "归零%", "止盈%",
            "止损%", "保本%", "平均MFE", "MFE≥3.25", "≥5", "≥7",
            "平均MAE", "捕获%", "合计", "最大回撤"]
    body = [[r["name"], r["n"], r["mean"], r["median"], r["p25"], r["worst"],
             r["zero_pct"], r["tp_pct"], r["stop_pct"], r["be_pct"], r["mfE"],
             r["mfe325"], r["mfe5"], r["mfe7"], r["mae"],
             r["capture"], r["total"], r["max_dd"]] for r in rows]
    widths = [max(len(str(h)), *(len(str(b[i])) for b in body)) for i, h in enumerate(head)]
    print("  ".join(str(h).ljust(w) for h, w in zip(head, widths)))
    print("-" * (sum(widths) + 2 * (len(head) - 1)))
    for b in body:
        print("  ".join(str(v).ljust(w) for v, w in zip(b, widths)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出场规则反事实回放（只读，不改实盘）")
    ap.add_argument("--only-plan", choices=["trend", "range", "mid"],
                    help="只回放某一种打法（老场次没有 plan 字段，会被排除）")
    ap.add_argument("--mult", type=float, default=1.0,
                    help="账户货币 / 价格美元；本账户 0.01 手 XAUUSDc = 1.0")
    ap.add_argument("--csv", metavar="PATH", help="把逐笔明细导出 CSV")
    args = ap.parse_args(argv)

    trades = load_trades(args.only_plan)
    if not trades:
        print("没有可回放的成交（需要同一 ticket 的 open+close，且非纸面）。")
        return 0
    print(f"样本：{len(trades)} 笔真实成交"
          f"（{time.strftime('%m-%d', time.gmtime(trades[0]['ts_open']))} ~ "
          f"{time.strftime('%m-%d', time.gmtime(trades[-1]['ts_open']))}）\n")

    # K 线按天缓存，避免每笔单独请求
    cache: dict[tuple, list[dict]] = {}
    details = []
    results: dict[str, list[dict]] = {k: [] for k in VARIANTS}
    skipped = 0

    for tr in trades:
        t0 = tr["ts_open"] - 30 * 60
        t1 = tr["ts_open"] + HOLD_CAP_SEC + 5 * 60
        key = (tr["symbol"], t0 // 3600)
        if key not in cache:
            cache[key] = fetch_bars(tr["symbol"], t0, t1)
        bars = cache[key]
        idx = next((i for i, b in enumerate(bars) if b["time"] > tr["ts_open"]), None)
        if idx is None or len(bars) - idx < 5:
            skipped += 1
            continue
        row = {"ticket": tr["ticket"], "ts_open": tr["ts_open"],
               "plan": tr["plan"] or "", "dir": tr["direction"],
               "actual": tr["pnl_actual"]}
        for name, v in VARIANTS.items():
            out = simulate(bars, idx, tr, v, args.mult)
            results[name].append(out)
            row[name] = out["pnl"]
        details.append(row)

    if skipped:
        print(f"跳过 {skipped} 笔（K 线不足：可能是历史未下载或周末）\n")
    summary = [summarize(name, rows) for name, rows in results.items()]
    _print([s for s in summary if s["n"]])

    print("\n实际成交（同一批入场，用作对照）")
    actual = [{"pnl": d["actual"], "reason": "", "mfe": 0.0, "mae": 0.0} for d in details]
    a = summarize("实际", actual)
    print(f"  笔数 {a['n']}  均值 {a['mean']:+.2f}  中位 {a['median']:+.2f}  "
          f"合计 {a['total']:+.2f}  最差 {a['worst']:+.2f}")

    if args.csv:
        dest = Path(args.csv)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(details[0]))
            w.writeheader()
            w.writerows(details)
        print(f"\n逐笔明细 → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
