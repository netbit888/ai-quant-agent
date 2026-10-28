"""A/B：出场决策点重放——只换 structure 片段、只换唤醒时点，看模型会不会更早落袋。

为什么要这个工具
----------------
2026-10-29 02:00（北京）FOMC 插针单 ticket 2760800958：0.01 多单 4039.624 入场，
plan=range（tp 3.25）。02:00:12 插针把价格顶到 4047.9（浮盈 8.30），程序采到
4.86，tp_wake 在 13 秒里连问模型三次，三次都判「继续持有」；02:00:25 之后浮盈
掉回门槛以下，系统转入 tp_wait（不问模型），一直到 02:03:45 才再问一次——那时
浮盈已是 -1.31，且要走 assist 人工确认，等 10.3 秒后硬止损抢先成交，最终 -3.90。

要分开验的是两件事，所以这里能分别切：

  1. **prompt 片段**：出场端的 structure 里原有一句「趋势市·上行 → 顺势做多，
     不等回踩」，和 plan_text（「震荡单别指望变成趋势」）方向相反。改成只讲
     结构（`for_exit=True`）之后，模型还会不会说「仍有空间」？
  2. **唤醒时点**：峰值之后多久把决定权交给模型。生产是 stale_sec=180 秒 +
     300 秒防抖；这笔如果 40 秒就问，浮盈还有 2~3，落点完全不同。

用法
----
    .venv/Scripts/python.exe -m tools.ab_exit                      # 最近 8 笔，三档时点
    .venv/Scripts/python.exe -m tools.ab_exit --offsets 20,40,90,180 --last 12
    .venv/Scripts/python.exe -m tools.ab_exit --no-tick             # 不重建半根（快，有偏差）
    .venv/Scripts/python.exe -m tools.ab_exit --dump out.json

已知偏差（读结论之前必须知道）
----------------------------
· **没有 tick 文本**：生产 prompt 里有一段逐笔 tick 描述，而插针的可测量特征
  恰恰是「最高价只停留 1~2 个 tick」。日志没落盘，回放里两臂都缺 → 对 A/B 公平，
  但绝对值偏保守（模型只能靠 K 线形态）。
· **没有教训库**：`lessons` 传空，两臂一样。
· 决策时点按「峰值 + 固定偏移」构造，**不是**当时的真实决策时刻。所以这里验证的
  是「如果在这个时刻问它，它会怎么说」，不是「它当时为什么那么说」。
· 默认用 tick 重建最后一根到决策时刻；`--no-tick` 时用走完的整根，等于让模型
  偷看未来（会系统性偏袒更会利用形态的那一臂，不要用它下结论）。
· 样本只有十来笔。这里的输出是**分布**，不是显著性结论。
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import autotrade as at          # noqa: E402
from agent.llm import _client, current_config   # noqa: E402

SPREAD = 0.24        # XAUUSDm 实测点差（价格单位），与 replay_exit / ab_thinking 同口径
CONTRACT = 100.0     # XAUUSD 每手 100 盎司：0.01 手时 1 美元价差 = 1 美元盈亏
WARMUP_SEC = 9000    # 往前取多久的 M1（MA28 + M5 背景 + 14 根窗口）

# 单价（USD / 1M tokens），只用来给量级
PRICE_HIT, PRICE_MISS, PRICE_OUT = 0.003, 0.15, 0.6


# ------------------------------------------------------------------ 读历史
def load_trades() -> list[dict]:
    """open / close 按 ticket 配对，只留带 peak_ts 的（peak 埋点是 2026-10-29 加的）。"""
    rows: list[dict] = []
    for p in glob.glob(str(ROOT / "data/live/accounts/*/autotrade_log*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: r.get("ts") or 0)

    opens = {str(r.get("ticket")): r for r in rows if r.get("kind") == "open"}
    out: list[dict] = []
    for c in rows:
        if c.get("kind") != "close":
            continue
        if c.get("peak_ts") is None or c.get("peak_pnl") is None:
            continue                      # 老单没有峰值埋点，构造不出时点
        o = opens.get(str(c.get("ticket")))
        if not o:
            continue
        out.append({
            "ticket": c.get("ticket"),
            "opened_at": int(o["ts"]),
            "closed_at": int(c["ts"]),
            "entry": float(o.get("price") or 0.0),
            "direction": int(o.get("direction") or 1),
            "lots": float(o.get("lots") or 0.01),
            "plan": c.get("plan") or "range",
            "entry_reason": o.get("reason") or "",
            "peak_pnl": float(c["peak_pnl"]),
            "peak_ts": int(c["peak_ts"]),
            "pnl": float(c.get("pnl") or 0.0),
            "atr": c.get("atr"),
        })
    return out


def _rates(symbol: str, tf: int, t0: int, t1: int) -> list[dict]:
    import MetaTrader5 as mt5
    from datetime import datetime, timezone

    arr = mt5.copy_rates_range(
        symbol, tf,
        datetime.fromtimestamp(t0, timezone.utc),
        datetime.fromtimestamp(t1, timezone.utc),
    )
    if arr is None:
        return []
    return [
        {"time": int(b["time"]), "open": float(b["open"]), "high": float(b["high"]),
         "low": float(b["low"]), "close": float(b["close"]),
         "tick_volume": int(b["tick_volume"])}
        for b in arr
    ]


def partial_bar(symbol: str, bar: dict, dec_ts: int, mt5) -> dict | None:
    """把决策时刻那根还没走完的 M1 重建出来（口径与生产 `_bars` 一致：用 bid）。

    为什么必须做：`_track_peak` 在快查里按 2~5 秒采一次，峰值时刻往往落在某根
    M1 的中间。拿历史接口给的**整根**去重放，等于让模型看到它当时看不到的收盘，
    而插针那根「收在哪」正是判断的关键——这会把结论系统地推向「早该看出来」。
    """
    from datetime import datetime, timezone

    arr = mt5.copy_ticks_range(
        symbol,
        datetime.fromtimestamp(int(bar["time"]), timezone.utc),
        datetime.fromtimestamp(int(dec_ts) + 1, timezone.utc),
        mt5.COPY_TICKS_ALL,
    )
    if arr is None or not len(arr):
        return None
    bids = [float(t["bid"]) for t in arr if float(t["bid"]) > 0]
    if not bids:
        return None
    return {"time": int(bar["time"]), "open": bids[0], "high": max(bids),
            "low": min(bids), "close": bids[-1], "tick_volume": len(bids)}


def rebuild(symbol: str, ts: int, use_tick: bool = True) -> dict | None:
    """重建 ts 时刻模型该看到的：14 根窗口（最后一根是半根）、bid/ask、M5 背景。"""
    import MetaTrader5 as mt5

    m1 = _rates(symbol, mt5.TIMEFRAME_M1, ts - WARMUP_SEC, ts + 120)
    if len(m1) < 40:
        return None
    idx = bisect.bisect_right([b["time"] for b in m1], ts) - 1
    if idx < 30:
        return None

    rows = m1[: idx + 1]
    if use_tick:
        half = partial_bar(symbol, rows[-1], ts, mt5)
        if half:
            rows = rows[:-1] + [half]
        # tick 取不到就退回整根——比整条样本作废好，但要在结果里标出来
        else:
            use_tick = False

    bid = float(rows[-1]["close"])
    m5 = _rates(symbol, mt5.TIMEFRAME_M5, ts - 7200, ts + 120)
    return {"rows": rows, "bid": bid, "ask": bid + SPREAD,
            "m5": m5, "half": use_tick}


# ------------------------------------------------------------------ 构造 prompt
class _Clock:
    """把 build_exit_prompt 里的「现在几点 / 持有多少秒」冻在该决策时刻。"""

    def __init__(self, ts: int) -> None:
        self._ts = int(ts)

    def time(self) -> int:
        return self._ts

    def gmtime(self, t=None):
        return time.gmtime(self._ts if t is None else t)

    def strftime(self, fmt: str, t=None) -> str:
        return time.strftime(fmt, self.gmtime() if t is None else t)


@contextmanager
def _structure_arm(for_exit: bool):
    """把 `_structure_text` 钉在某一臂上。

    `build_exit_prompt` 里是 `_structure_text(w, zone_edge, dg, trend_follow,
    for_exit=True)` —— `for_exit` 走**位置参数**，所以补丁必须能吃 *args。
    """
    orig = at._structure_text

    def patched(rows, zone_edge=0.35, digits=3, trend_follow=True, *a, **kw):
        return orig(rows, zone_edge, digits, trend_follow, for_exit=for_exit)

    at._structure_text = patched
    try:
        yield
    finally:
        at._structure_text = orig


def build_prompt(trade: dict, ts: int, bk: dict, cfg) -> str:
    pos = {
        "ticket": trade["ticket"],
        "direction": trade["direction"],
        "lots": trade["lots"],
        "price_open": trade["entry"],
        "opened_at": trade["opened_at"],
        "plan": trade["plan"],
        "entry_reason": trade["entry_reason"],
    }
    orig = at.time
    at.time = _Clock(ts)
    try:
        return at.build_exit_prompt(
            bk["rows"], pos, bk["bid"], bk["ask"],
            tf=cfg.tf, contract=CONTRACT,
            symbol=cfg.symbol, lessons="",
            zone_edge=cfg.zone_edge,
            mtf_text=_mtf_text(bk["m5"], ts),
            trend_follow=cfg.trend_follow,
            ticks_text="",       # 日志没落盘 tick 文本，两臂一样缺
        )
    finally:
        at.time = orig


def _mtf_text(m5: list[dict], ts: int) -> str:
    """复刻 `_mtf_bundle` 的文案，与 ab_thinking 同一实现。"""
    if not m5:
        return ""
    idx = bisect.bisect_right([b["time"] for b in m5], ts) - 1
    if idx < 0:
        return ""
    w = m5[max(0, idx + 1 - at.MTF_BARS): idx + 1]
    if len(w) < at.MTF_MIN_BARS:
        return ""
    s = at._structure(w)
    if not s.get("ok"):
        return ""
    a = at._atr(w)
    drift = s["drift"]
    dir_text = "上行" if drift > 0 else ("下行" if drift < 0 else "走平")
    has_atr = bool(a.get("ok") and a.get("atr", 0) > 0)
    atr_part = (f"｜振幅 {s['span']:.3f}（{a['span_atr']:.1f} 倍单根均幅）"
                if has_atr else f"｜振幅 {s['span']:.3f}")
    return (f"【背景】M5 {len(w)} 根（约 {len(w) * 5} 分钟）："
            f"ER={s['er']:.2f}（净移动 {drift:+.3f} {dir_text}）"
            f"｜收盘位置 {s['pos']:.0%}{atr_part}"
            f"｜判定：{at._regime_word(s['er'], drift)}")


# ------------------------------------------------------------------ 调用
def call(client, system: str, prompt: str, model: str) -> dict:
    """一次出场决策调用。思考关掉（与生产当前行为一致）。"""
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "latency": time.time() - t0}
    text = resp.choices[0].message.content or ""
    parsed = at.parse_exit(text) or {}
    u = resp.usage
    return {
        "ok": True,
        "close": parsed.get("close"),
        "reason": (parsed.get("reason") or "")[:160],
        "latency": round(time.time() - t0, 2),
        "out": getattr(u, "completion_tokens", 0) or 0,
        "hit": getattr(u, "prompt_cache_hit_tokens", 0) or 0,
        "miss": getattr(u, "prompt_cache_miss_tokens", 0) or 0,
        "raw": text[:400],
    }


def pnl_at(trade: dict, bid: float) -> float:
    """按当时报价平掉的净盈亏（美元）。与 `_realized_pnl` 的价差口径一致。"""
    return (bid - trade["entry"]) * trade["direction"] * CONTRACT * trade["lots"]


# ------------------------------------------------------------------ 主流程
def main() -> None:
    ap = argparse.ArgumentParser(description="出场决策点 A/B（prompt 片段 × 唤醒时点）")
    ap.add_argument("--last", type=int, default=8, help="取最近多少笔（默认 8）")
    ap.add_argument("--offsets", default="0,40,180",
                    help="峰值之后多少秒唤醒，逗号分隔（默认 0,40,180）")
    ap.add_argument("--no-tick", action="store_true", help="不重建半根（有未来信息泄漏，别下结论）")
    ap.add_argument("--dump", help="把逐点明细写成 JSON")
    args = ap.parse_args()

    offsets = [int(x) for x in args.offsets.split(",") if x.strip()]
    cfg = at.Cfg()
    trades = load_trades()[-args.last:]
    if not trades:
        print("没有带 peak_ts 的成交（peak 埋点是 2026-10-29 才加的，老单重放不了）")
        return

    conf = current_config()
    client = _client()
    model = conf.get("model")

    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize 失败：{mt5.last_error()}")

    print(f"模型 {model} · 取 {len(trades)} 笔 · 时点 {offsets} 秒 · "
          f"半根重建 {'关（有泄漏）' if args.no_tick else '开'}")
    print(f"{'ticket':>12} {'峰值时刻':>9} {'偏移':>5} {'当时浮盈':>9}  "
          f"{'旧prompt':>18}  {'新prompt':>18}  {'实际':>7}")
    print("-" * 104)

    detail: list[dict] = []
    cost = 0.0
    for t in trades:
        system = at.system_for(at.EXIT_SYSTEM, cfg, t["plan"])
        peak_bj = time.strftime("%H:%M:%S", time.gmtime(t["peak_ts"] + 8 * 3600))
        for off in offsets:
            ts = min(t["peak_ts"] + off, t["closed_at"] - 1)
            bk = rebuild(cfg.symbol, ts, use_tick=not args.no_tick)
            if bk is None:
                print(f"{t['ticket']:>12} {peak_bj:>9} {off:>5}  重建失败（M1 不够）")
                continue
            fp = pnl_at(t, bk["bid"])
            rec = {"ticket": t["ticket"], "ts": ts, "offset": off,
                   "fp": round(fp, 2), "plan": t["plan"],
                   "peak_pnl": t["peak_pnl"], "actual": t["pnl"],
                   "half": bk["half"]}
            for arm, for_exit in (("old", False), ("new", True)):
                with _structure_arm(for_exit):
                    prompt = build_prompt(t, ts, bk, cfg)
                r = call(client, system, prompt, model)
                if r.get("ok"):
                    cost += (r["hit"] * PRICE_HIT + r["miss"] * PRICE_MISS
                             + r["out"] * PRICE_OUT) / 1e6
                rec[arm] = {"close": r.get("close"), "reason": r.get("reason"),
                            "err": r.get("error"), "out": r.get("out")}
            detail.append(rec)
            o, n = rec["old"], rec["new"]
            print(f"{t['ticket']:>12} {peak_bj:>9} {off:>5} {fp:>+9.2f}  "
                  f"{_fmt(o):>18}  {_fmt(n):>18}  {t['pnl']:>+7.2f}")
            if not bk["half"] and not args.no_tick:
                print(f"{'':>12} {'':>9} {'':>5}   ↑ tick 取不到，这根用了整根（有泄漏）")

    # ---------------------------------------------------------------- 汇总
    if not detail:
        print("\n没有可用样本")
        return

    print("\n" + "=" * 104)
    print("汇总（口径：该臂在那个时点判平，就按当时浮盈落袋；否则算到实际结果）")
    print(f"{'臂':<10} {'判平点':>8} {'占全部点':>9} {'最早判平偏移':>13} "
          f"{'逐笔落点合计':>13} {'vs 实际':>10}")
    actual_total = sum(t["pnl"] for t in trades)
    for arm in ("old", "new"):
        closes = [d for d in detail if (d[arm].get("close") is True)]
        firsts = {}
        for d in detail:
            if d[arm].get("close") is True and d["ticket"] not in firsts:
                firsts[d["ticket"]] = d
        total = 0.0
        for t in trades:
            hit = firsts.get(t["ticket"])
            total += hit["fp"] if hit else t["pnl"]
        median_off = (sorted(int(h["offset"]) for h in firsts.values())
                      if firsts else [])
        off_text = (f"{median_off[len(median_off) // 2]}s({len(firsts)}笔)"
                    if median_off else "从不")
        print(f"{arm:<10} {len(closes):>4}/{len(detail):<3} "
              f"{len(closes) / len(detail):>8.0%} {off_text:>13} "
              f"{total:>+13.2f} {total - actual_total:>+10.2f}")
    print(f"{'实际':<10} {'':>8} {'':>9} {'':>13} {actual_total:>+13.2f}")
    print(f"\n调用 {len(detail) * 2} 次 · 成本约 {cost:.3f} 元 · "
          f"（样本 {len(trades)} 笔，只当分布看，别当显著性）")

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细 -> {args.dump}")


def _fmt(r: dict) -> str:
    if r.get("err"):
        return "错误"
    if r.get("close") is True:
        return "平"
    if r.get("close") is False:
        return "持有"
    return "未解析"


if __name__ == "__main__":
    main()
