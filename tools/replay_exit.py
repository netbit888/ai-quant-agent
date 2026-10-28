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


def _atr_at(bars: list[dict], end_idx: int, n: int = 14) -> float:
    """入场前 n 根的真实波幅均值（**价格**单位，不是美元）。

    口径必须和 live 的 `autotrade._atr` 一模一样：14 根产出 13 个 TR，
    `TR = max(high-low, |high-prev_close|, |low-prev_close|)`，简单均值。
    两套算法各写一份，算出来的 k 用到生产上就是错位。

    为什么用 K 线现算、不读日志里的 feat.atr：日志 feat 只有 30/259 笔
    （2026-10-26 21:39 才开始落盘），10-23 那 191 笔**全无**。走日志等于
    把主样本整块扔掉。

    `end_idx` 是"开仓之后第一根"的下标（bisect_right 的返回值），所以
    `[end_idx-n, end_idx)` 正好是入场那一刻能看见的 n 根，不含未来。
    """
    lo = max(0, end_idx - n)
    w = bars[lo:end_idx]
    if len(w) < 2:
        return 0.0
    trs = []
    for i in range(1, len(w)):
        h, l = float(w[i]["high"]), float(w[i]["low"])
        pc = float(w[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def replay_one(bars: list[dict], start_idx: int, price_open: float, direction: int,
               sl: float, tp: float, spread: float, unit: float,
               max_bars: int = 720, arm: float | None = None,
               frac: float | None = None,
               peak_out: dict | None = None) -> tuple[float, str, int]:
    """从 start_idx 起逐根走，返回 (pnl_usd, 出场类型, 走过几根)。

    `unit` = 合约 × 手数，把价格差换成美元。0.01 手 XAUUSD = 1.0，
    也就是「价格走 1 美元 = 盈亏 1 美元」，所以 sl/tp 可以直接当价格用。

    同一根 K 线里同时碰到止损和止盈时，**按先止损处理**。没有 tick 数据
    就永远不知道那根里是先高还是先低，只能取对策略不利的那个——
    宁可低估，不要自己骗自己。

    新增的两个可选参数（2026-10-29，全 keyword-default，返回值仍是 3 元组，
    所以 `tests/test_exit_rules.py` 里那些位置调用不受影响）：

    · `arm` / `frac`：峰值回吐保护。浮盈摸到 `arm` 之后，只要回撤
      `frac`（比例）就按 `peak×(1-frac)` 结算，kind="giveback"。
      两个都给了才启用，只给一个当没给。
    · `peak_out`：传一个 dict 进来，会写入 `peak_out["peak"]`——MFE 只能
      在逐根循环里累积，又不想为了它把返回值改成 4 元组（会打断所有
      解包 3 元组的调用方）。

    判定顺序 **sl → tp → giveback**：tp 是硬阈值穿越（像限价单），
    giveback 是路径依赖的次级规则。同一根里既冲到 tp 又回撤到线，
    取 tp——否则新规则会把"本来吃到 3.25"的单系统性改写成小赢，等于
    用回放工具自己制造一个更差的规则。

    另外修了一个老 bug：超时结算原来取 `bars[-1]["close"]`（整段数据的
    最后一根，可能是几小时之后的价格），现在取 `bars[end-1]`（第
    max_bars 根）。这会让 timeout 那一类的合计和旧基线**不可直接比**，
    跑对比时要重新打基线。
    """
    stop_price = sl / unit
    tp_price = tp / unit
    end = min(len(bars), start_idx + max_bars)
    last = bars[end - 1]["close"] if end > 0 else 0.0
    use_giveback = arm is not None and frac is not None and arm > 0 and frac > 0
    peak = float("-inf")

    for i in range(start_idx, end):
        b = bars[i]
        if direction > 0:
            # 多头：开在 ask，平在 bid，pnl = (bid - open) * unit
            best = (b["high"] - price_open) * unit
            now = (b["close"] - price_open) * unit
        else:
            # 空头：开在 bid，平在 ask → pnl = (open - bid - spread) * unit
            best = (price_open - b["low"] - spread) * unit
            now = (price_open - b["close"] - spread) * unit
        if best > peak:
            peak = best

        if direction > 0:
            if b["low"] - price_open <= -stop_price:
                return -sl, "sl", i - start_idx + 1
            if b["high"] - price_open >= tp_price:
                return tp, "tp", i - start_idx + 1
        else:
            if price_open - b["high"] - spread <= -stop_price:
                return -sl, "sl", i - start_idx + 1
            if price_open - b["low"] - spread >= tp_price:
                return tp, "tp", i - start_idx + 1

        if use_giveback and peak >= arm:
            floor = peak * (1.0 - frac)
            # 用**收盘价**判，不用本根最低/最高：生产里 giveback 是 5 秒快查
            # 拿实时价判的，不是限价单。拿极值判等于假设"每根都先高后低"的
            # 极端路径，会把触发次数系统性放大，回放出来的结果比实际更激进。
            if now <= floor:
                return round(floor, 2), "giveback", i - start_idx + 1

    pnl = ((last - price_open) if direction > 0
           else (price_open - last - spread)) * unit
    if peak_out is not None and peak != float("-inf"):
        peak_out["peak"] = peak
    return round(pnl, 2), "timeout", end - start_idx


def summarize(name: str, pnls: list[float], kinds: dict, holds: list[int],
              peaks: list[float] | None = None) -> dict:
    win = [x for x in pnls if x > 0]
    loss = [x for x in pnls if x < 0]
    n = len(pnls)
    avg_w = st.mean(win) if win else 0.0
    avg_l = st.mean(loss) if loss else 0.0
    ratio = abs(avg_w / avg_l) if avg_l else None
    need = (abs(avg_l) / (avg_w + abs(avg_l)) * 100) if (avg_w and avg_l) else None
    out = {
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
    # MFE 是"只加键、不改名"：老的 keys 一个都不能动，TestSummarize 钉着它们。
    # 没有 peak 数据（纯 grid 模式）就不加这两个键，免得画出一个假的 0.0。
    if peaks:
        out["mfe"] = round(st.mean(peaks), 3)
        out["mfe_med"] = round(st.median(peaks), 3)
    return out


def _clamp(v: float, lo: float, hi: float | None) -> float:
    if hi is not None and hi > 0:
        v = min(v, hi)
    return max(v, lo) if lo else v


def run_grid(trades: list[dict], bars: list[dict], symbol: str,
             sl_list: list[float], tp_list: list[float],
             mode: str = "grid", atr_k_list: list[float] | None = None,
             atr_floor: float = 0.0, atr_cap: float | None = None,
             atr_n: int = 14,
             giveback_list: list[tuple[float, float]] | None = None) -> list[dict]:
    """跑参数网格。

    `mode="grid"`（默认，老行为）：固定 tp 网格，`tp_list` 是美元。
    `mode="atr"`：每笔用自己的 ATR 算门槛
        `tp = clamp(k × atr × unit, floor, cap)`
        —— atr 是**价格**、tp 是**美元**，必须乘 unit（0.01 手 XAUUSD = 1.0，
           漏乘会让门槛变成 1/100，结果看起来"巨好"，其实是每笔都秒进秒出）。
        `sl_list` 照旧，`atr_k_list` 替代 `tp_list`。

    `giveback_list` 是 [(arm, frac), ...]，和 grid/atr 正交：
        arm 和 frac 都按 **USD** 记，arm 是"摸到多少才武装"，
        frac 是"从峰值回撤几成"。
    """
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

    # 每笔的起始下标 / unit / ATR 一次算好，网格里复用：
    # bisect 和 _atr_at 都是纯计算，但网格是几百次循环，没必要重复。
    plans = []
    for t in trades:
        start = bisect.bisect_right(idx, t["ts"])
        if start >= len(bars):
            continue
        plans.append((t, start, contract * t["lots"], _atr_at(bars, start, atr_n)))

    gb = giveback_list or [(None, None)]

    def run_one(sl: float, tp_of, label: str, arm, frac) -> None:
        pnls, kinds, holds, peaks = [], {}, [], []
        for t, start, unit, atr in plans:
            tp = tp_of(atr, unit) if callable(tp_of) else tp_of
            # grid 模式 tp 是常量，tp<=sl 的组和别的组重复，照旧跳过。
            # **atr 模式不能跳**：波动小的单算出来的 tp 会低于 sl（盈亏比 <1），
            # 那正是要拿数据回答的问题（低盈亏比能不能靠高胜率补回来）。
            # 悄悄 skip 掉等于只回放"结果好看的那一半"，是自欺。
            if mode == "grid" and tp <= sl:
                continue
            pk: dict = {}
            pnl, kind, nb = replay_one(
                bars, start, t["price_open"], t["direction"], sl, tp,
                spread, unit, arm=arm, frac=frac, peak_out=pk)
            pnls.append(pnl)
            kinds[kind] = kinds.get(kind, 0) + 1
            holds.append(nb * 60)
            if "peak" in pk:
                peaks.append(pk["peak"])
        if pnls:
            out.append(summarize(label, pnls, kinds, holds, peaks))

    def gb_tag(arm, frac) -> str:
        return "" if arm is None else f" 回吐{frac:.0%}@{arm:.1f}"

    cap_txt = f"{atr_cap:g}" if atr_cap else "不限"
    for sl in sl_list:
        if mode == "atr":
            for k in (atr_k_list or []):
                for arm, frac in gb:
                    label = (f"止损 {sl:.2f} / 止盈 {k:.1f}×ATR"
                             f"({atr_floor:g}~{cap_txt}){gb_tag(arm, frac)}")
                    run_one(sl, lambda a, u, k=k: _clamp(k * a * u, atr_floor, atr_cap),
                            label, arm, frac)
        else:
            for tp in tp_list:
                for arm, frac in gb:
                    label = f"止损 {sl:.2f} / 止盈 {tp:.2f}{gb_tag(arm, frac)}"
                    run_one(sl, tp, label, arm, frac)
    return out


def fmt_table(rows: list[dict]) -> str:
    head = (f"{'参数':30} {'笔数':>4} {'胜率':>6} {'avgW':>6} {'avgL':>6} "
            f"{'盈亏比':>6} {'需胜率':>7} {'合计':>9} {'期望/笔':>8} "
            f"{'MFE':>6} {'止损':>4} {'超时':>4}")
    lines = [head, "-" * len(head.encode("gbk", "ignore"))]
    for r in rows:
        mfe = f"{r['mfe']:6.2f}" if "mfe" in r else f"{'-':>6}"
        lines.append(
            f"{r['name'][:30]:30} {r['n']:4d} {r['win_rate']:5.1f}% "
            f"{r['avg_w']:6.3f} {r['avg_l']:6.3f} "
            f"{(r['ratio'] if r['ratio'] else 0):6.2f} "
            f"{(r['need_rate'] if r['need_rate'] else 0):6.1f}% "
            f"{r['total']:9.2f} {r['expect']:8.3f} {mfe} "
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
                   help="止盈门槛候选（USD，仅 --mode grid）")
    p.add_argument("--mode", choices=["grid", "atr"], default="grid",
                   help="grid=固定 tp 网格（老行为）；atr=每笔按开仓时 ATR 归一化")
    p.add_argument("--atr-k", type=float, nargs="*",
                   default=[1.0, 1.2, 1.5, 1.8],
                   help="ATR 倍数候选（仅 --mode atr）")
    p.add_argument("--atr-floor", type=float, default=1.6,
                   help="ATR 门槛下限（USD）。再低点差占比就压不住了")
    p.add_argument("--atr-cap", type=float, default=None,
                   help="ATR 门槛上限（USD）。默认不限；设 3.25 = 只降不升")
    p.add_argument("--atr-n", type=int, default=14,
                   help="ATR 窗口根数，必须和 live 的 _atr 一致（默认 14）")
    p.add_argument("--arm", type=float, nargs="*", default=None,
                   help="回吐保护的武装线（USD），浮盈摸到它才开始盯回撤")
    p.add_argument("--frac", type=float, nargs="*", default=None,
                   help="回撤比例，如 0.4 = 从峰值回落 40%% 就走")
    p.add_argument("--hours-back", type=float, help="只回放最近 N 小时的入场")
    args = p.parse_args()

    if args.arm and args.frac:
        giveback_list = [(a, f) for a in args.arm for f in args.frac]
    elif args.arm or args.frac:
        raise SystemExit("--arm 和 --frac 必须一起给（只给一个等于没开回吐保护）")
    else:
        giveback_list = None

    trades = load_trades(args.account, args.hours_back)
    if not trades:
        raise SystemExit("没有可回放的成交（open/close 配不上对）")
    bars = load_bars(args.symbol, trades[0]["ts"], trades[-1]["ts"])
    print(f"入场 {len(trades)} 笔 | K 线 {len(bars)} 根 | {args.symbol}")
    print(f"点差按规格表算：{spec_of(args.symbol)[0]:.3f} 价格单位")
    if args.mode == "atr":
        print(f"ATR 口径：入场前 {args.atr_n} 根、13 个 TR 的简单均值"
              f"（与 live._atr 同源）；门槛 = clamp(k×ATR, "
              f"{args.atr_floor:g}, {args.atr_cap or '不限'})")
    print()
    rows = run_grid(trades, bars, args.symbol, args.sl, args.tp,
                    mode=args.mode, atr_k_list=args.atr_k,
                    atr_floor=args.atr_floor, atr_cap=args.atr_cap,
                    atr_n=args.atr_n, giveback_list=giveback_list)
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
    print("注意：入场点是账本里的真实成交，不是重新生成的。")
    print("出场变长会让后续入场变少，所以「笔数」不能外推成「一下午会做几笔」。")
    print("基线「实际出场」含账本滑点；网格行是理想成交，两者不能逐笔对，只看期望。")


if __name__ == "__main__":
    main()
