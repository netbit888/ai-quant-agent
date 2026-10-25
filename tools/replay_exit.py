"""出场规则回放器：用真实 M1 K 线回答「如果当时不止盈会怎样」。

为什么必须有这个东西
--------------------
第 3 场实盘（2026-10-23，191 笔）的病根是：avgW 1.011 / avgL 1.026，
盈亏比 0.99，胜率 35.6%，要打平需要 50.4%。而每笔 0.24 的点差占了
平均波动（1.02）的 23.5%——每笔都在交 23.5% 的过路费。

「让利润跑、亏损截断」能不能救，取决于一个账本里**没有**的量：
持仓期间价格有没有冲到 2.5 又回落。平仓时的 pnl 只告诉你结局，
不告诉你路径。所以只能拿 K 线重放。

严格说这不是回测，是**反事实估计**：
  · 入场点是当时那 191 个真实成交，不重新生成；
  · 出场变长会让后续入场变少，所以「做几笔」这个数不能直接外推；
  · 它回答的是：同样这批入场质量，换出场规则后单笔期望变成多少。

用法
----
    python -m tools.replay_exit                 # 默认网格扫一遍
    python -m tools.replay_exit --sl 1.5 --tp 2.5   # 只测一组，看细节
    python -m tools.replay_exit --hours-back 1  # 只回放最近 1 小时的入场

时间口径：MT5 的 bar.time 是 UTC 纪元，日志里的 ts 也是 time.time()，
可以直接比。K 线时间戳是**这根 K 线的开始时间**，所以从
`time > 开仓时刻` 的第一根开始算，避免把开仓前走完的行情算进来。
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.symbols import SYMBOLS  # noqa: E402

DEFAULT_SYMBOL = "XAUUSDm"


def spec_of(symbol: str) -> tuple[float, float, float]:
    """(点差价格, 合约大小, point)。单位换算全靠这三个数。"""
    s = SYMBOLS.get(symbol) or {}
    point = float(s.get("point") or 0.001)
    contract = float(s.get("contract") or 100.0)
    spread_price = float(s.get("spread") or 240) * point
    return spread_price, contract, point


# ------------------------------------------------------------------ 数据


def load_trades(login: str | None, hours_back: float | None = None) -> list[dict]:
    """从 autotrade_log.jsonl 里配对 open / close，拿到真实入场点。"""
    from live import paths

    log = paths.account_file(paths.resolve_login(login), "autotrade_log.jsonl")
    if not log.exists():
        raise SystemExit(f"找不到流水：{log}")
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]

    opens = {r.get("ticket"): r for r in rows if r.get("kind") == "open"}
    closes = {r.get("ticket"): r for r in rows
              if r.get("kind") == "close" and r.get("source") != "panel"}

    trades = []
    for ticket, o in opens.items():
        c = closes.get(ticket)
        if not c:
            continue
        trades.append({
            "ticket": ticket,
            "ts": int(o["ts"]),
            "price_open": float(o["price"]),
            "direction": int(o["direction"]),
            "lots": float(o.get("lots") or 0.01),
            "actual_pnl": float(c.get("pnl") or 0.0),
            "actual_held": int(c.get("held_sec") or 0),
        })
    trades.sort(key=lambda t: t["ts"])
    if hours_back:
        cutoff = trades[-1]["ts"] - int(hours_back * 3600)
        trades = [t for t in trades if t["ts"] >= cutoff]
    return trades


def load_bars(symbol: str, ts_from: int, ts_to: int) -> list[dict]:
    """拉 M1 K 线。返回按时间升序的 [{time, open, high, low, close}]。"""
    import datetime as dt

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit("没装 MetaTrader5 包")

    if not mt5.initialize():
        raise SystemExit(
            f"连不上 MT5 终端：{mt5.last_error()}\n"
            "  先把终端打开（并登录账户），再跑这个脚本。"
        )
    try:
        r = mt5.copy_rates_range(
            symbol, mt5.TIMEFRAME_M1,
            dt.datetime.fromtimestamp(ts_from - 3600),
            dt.datetime.fromtimestamp(ts_to + 3600),
        )
    finally:
        # 这个脚本是一次性的，用完就断，不影响终端
        mt5.shutdown()

    if r is None or not len(r):
        raise SystemExit(f"取不到 K 线（可能超出终端历史范围）：{mt5.last_error()}")
    return [{"time": int(b["time"]), "open": float(b["open"]),
             "high": float(b["high"]), "low": float(b["low"]),
             "close": float(b["close"])} for b in r]


# ------------------------------------------------------------------ 回放


def replay_one(bars: list[dict], start_idx: int, price_open: float, direction: int,
               sl: float, tp: float, spread: float, unit: float,
               max_bars: int = 720) -> tuple[float, str, int]:
    """从 start_idx 起逐根走，返回 (pnl_usd, 出场类型, 走过几根)。

    `unit` = 合约 × 手数，把价格差换成美元。0.01 手 XAUUSD = 1.0，
    也就是「价格走 1 美元 = 盈亏 1 美元」，所以 sl/tp 可以直接当价格用。

    同一根 K 线里同时碰到止损和止盈时，**按先止损处理**。没有 tick 数据
    就永远不知道那根里是先高还是先低，只能取对策略不利的那个——
    宁可低估，不要自己骗自己。
    """
    stop_price = sl / unit
    tp_price = tp / unit
    last = bars[-1]["close"]
    end = min(len(bars), start_idx + max_bars)

    for i in range(start_idx, end):
        b = bars[i]
        if direction > 0:
            # 多头：开在 ask，平在 bid，pnl = (bid - open) * unit
            if b["low"] - price_open <= -stop_price:
                return -sl, "sl", i - start_idx + 1
            if b["high"] - price_open >= tp_price:
                return tp, "tp", i - start_idx + 1
        else:
            # 空头：开在 bid，平在 ask → pnl = (open - bid - spread) * unit
            if price_open - b["high"] - spread <= -stop_price:
                return -sl, "sl", i - start_idx + 1
            if price_open - b["low"] - spread >= tp_price:
                return tp, "tp", i - start_idx + 1

    pnl = ((last - price_open) if direction > 0
           else (price_open - last - spread)) * unit
    return round(pnl, 2), "timeout", end - start_idx


def summarize(name: str, pnls: list[float], kinds: dict, holds: list[int]) -> dict:
    win = [x for x in pnls if x > 0]
    loss = [x for x in pnls if x < 0]
    n = len(pnls)
    avg_w = st.mean(win) if win else 0.0
    avg_l = st.mean(loss) if loss else 0.0
    ratio = abs(avg_w / avg_l) if avg_l else None
    need = (abs(avg_l) / (avg_w + abs(avg_l)) * 100) if (avg_w and avg_l) else None
    return {
        "name": name, "n": n,
        "win": len(win), "loss": len(loss),
        "win_rate": round(len(win) / n * 100, 1) if n else 0.0,
        "avg_w": round(avg_w, 3), "avg_l": round(avg_l, 3),
        "ratio": round(ratio, 2) if ratio else None,
        "need_rate": round(need, 1) if need else None,
        "total": round(sum(pnls), 2),
        "expect": round(sum(pnls) / n, 3) if n else 0.0,
        "max_loss": round(min(pnls), 2) if pnls else 0.0,
        "kinds": kinds,
        "hold_med": int(st.median(holds)) if holds else 0,
    }


def run_grid(trades: list[dict], bars: list[dict], symbol: str,
             sl_list: list[float], tp_list: list[float]) -> list[dict]:
    spread, contract, _ = spec_of(symbol)
    idx = [b["time"] for b in bars]
    import bisect

    out = []
    # 基线：账本里实际发生的结果
    out.append(summarize(
        "实际出场（账本）",
        [t["actual_pnl"] for t in trades],
        {"实际": len(trades)},
        [t["actual_held"] for t in trades],
    ))

    for sl in sl_list:
        for tp in tp_list:
            if tp <= sl:
                continue
            pnls, kinds, holds = [], {}, []
            for t in trades:
                unit = contract * t["lots"]
                # 从「开仓时刻之后的第一根完整 K 线」开始，理由见模块 docstring
                start = bisect.bisect_right(idx, t["ts"])
                if start >= len(bars):
                    continue
                pnl, kind, nb = replay_one(bars, start, t["price_open"],
                                           t["direction"], sl, tp, spread, unit)
                pnls.append(pnl)
                kinds[kind] = kinds.get(kind, 0) + 1
                holds.append(nb * 60)
            if pnls:
                out.append(summarize(f"止损 {sl:.2f} / 止盈 {tp:.2f}", pnls, kinds, holds))
    return out


def fmt_table(rows: list[dict]) -> str:
    head = (f"{'参数':22} {'笔数':>4} {'胜率':>6} {'avgW':>6} {'avgL':>6} "
            f"{'盈亏比':>6} {'需胜率':>7} {'合计':>9} {'期望/笔':>8} {'止损':>4} {'超时':>4}")
    lines = [head, "-" * len(head.encode("gbk", "ignore"))]
    for r in rows:
        lines.append(
            f"{r['name']:22} {r['n']:4d} {r['win_rate']:5.1f}% "
            f"{r['avg_w']:6.3f} {r['avg_l']:6.3f} "
            f"{(r['ratio'] if r['ratio'] else 0):6.2f} "
            f"{(r['need_rate'] if r['need_rate'] else 0):6.1f}% "
            f"{r['total']:9.2f} {r['expect']:8.3f} "
            f"{r['kinds'].get('sl', 0):4d} {r['kinds'].get('timeout', 0):4d}"
        )
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="用真实 M1 K 线回放出场规则")
    p.add_argument("--account", help="账户号，默认用终端当前登录的")
    p.add_argument("--symbol", default=DEFAULT_SYMBOL)
    p.add_argument("--sl", type=float, nargs="*", default=[1.0, 1.5, 2.0, 2.5, 3.0],
                   help="止损候选（USD）")
    p.add_argument("--tp", type=float, nargs="*", default=[2.0, 2.5, 3.0, 4.0, 5.0],
                   help="止盈门槛候选（USD）")
    p.add_argument("--hours-back", type=float, help="只回放最近 N 小时的入场")
    args = p.parse_args()

    trades = load_trades(args.account, args.hours_back)
    if not trades:
        raise SystemExit("没有可回放的成交（open/close 配不上对）")
    bars = load_bars(args.symbol, trades[0]["ts"], trades[-1]["ts"])
    print(f"入场 {len(trades)} 笔 | K 线 {len(bars)} 根 | {args.symbol}")
    print(f"点差按规格表算：{spec_of(args.symbol)[0]:.3f} 价格单位")
    print()
    rows = run_grid(trades, bars, args.symbol, args.sl, args.tp)
    print(fmt_table(rows))
    print()
    best = max(rows[1:], key=lambda r: r["expect"], default=None)
    if best and best["expect"] > 0:
        print(f"最好的一组：{best['name']}，期望 {best['expect']:+.3f}/笔，"
              f"合计 {best['total']:+.2f}（{best['n']} 笔）")
    elif best:
        print(f"网格里最好的一组也是负期望（{best['name']}：{best['expect']:+.3f}/笔）。"
              "说明光改出场救不回来，入场方向也得动。")
    print()
    print("注意：入场点是当时那 191 笔真实成交，不是重新生成的。")
    print("出场变长会让后续入场变少，所以「笔数」不能外推成「一下午会做几笔」。")


if __name__ == "__main__":
    main()
