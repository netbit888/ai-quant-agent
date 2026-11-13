"""面板用的只读视图计算：符号下拉、模型判定与实际结果对照。

为什么单独一层
--------------
这三块是**纯展示**：不写盘、不读 MT5、不改 state，输入全在参数里。
放在 `autotrade` 里时，`tools/dashboard.py` 为了拿一个下拉框数据
也得 import 整个交易模块。

`summary()` / `trade_detail()` **故意不在这里**：它们要串行情 + 流水 +
state + prefs + WORKER 实时对象，是组合根，搬走只会把组合根换个地方，
还会因为 `WORKER` 单例出现两个副本。
"""

from live.cfg import TRADABLE_SYMBOLS
from live.ta import SYMBOL_LABELS


def symbol_options() -> list[dict]:
    """面板下拉框的数据源：只给白名单，附 point/contract 供前端显示成本。"""
    from backtest.symbols import SYMBOLS

    out = []
    for s in TRADABLE_SYMBOLS:
        spec = SYMBOLS.get(s) or {}
        out.append({
            "symbol": s,
            "label": SYMBOL_LABELS.get(s, s),
            "point": spec.get("point"),
            "contract": spec.get("contract"),
            "spread": spec.get("spread"),
            "digits": spec.get("digits"),
        })
    return out


def model_vs_actual(rows: list[dict], deals: dict[int, dict],
                    interval_sec: int = 60, symbol: str = "",
                    limit: int = 8) -> dict:
    """最近 N 轮：模型方向 vs 账户实际成交方向。

    面板是个人复盘工具（手动做单、magic=0 常态），所以「我」这一行取账本里
    **真实发生的开仓**，不是模型写了什么。轮次只认 skip / signal 决策行：
    同一笔单实测会留下 4 条 p_up 相同的流水（signal/open/signal/close），
    不合并就会把一次决策画成四格，历史序列直接失真。
    """
    iv = max(1, int(interval_sec or 60))
    dec = [r for r in rows
           if r.get("kind") in ("skip", "signal")
           and r.get("p_up") is not None and r.get("ts")]
    dec.sort(key=lambda r: int(r["ts"]))
    kept: list[dict] = []
    for r in dec:
        ts = int(r["ts"])
        # 半个间隔内的重复行算同一轮；signal 优先（它带 act/advice）。
        if kept and ts - int(kept[-1]["ts"]) < iv * 0.5:
            if r.get("kind") == "signal":
                kept[-1] = r
            continue
        kept.append(r)
    kept = kept[-limit:]

    # 已实现盈亏按 position_id 归集：开仓那笔 profit=0，钱在平仓那笔上。
    pnl_by_pos: dict = {}
    for d in deals.values():
        if int(d.get("entry", -1)) != 1:
            continue
        try:
            val = (float(d.get("profit") or 0) + float(d.get("commission") or 0)
                   + float(d.get("swap") or 0) + float(d.get("fee") or 0))
        except (TypeError, ValueError):
            continue
        pid = d.get("position_id")
        pnl_by_pos[pid] = pnl_by_pos.get(pid, 0.0) + val

    # 一笔成交只归到「时间上最近的那一轮」，避免落在相邻两轮之间被画两次。
    assign: dict[int, dict] = {}
    for d in deals.values():
        if int(d.get("entry", -1)) != 0 or float(d.get("volume") or 0) <= 0:
            continue
        if symbol and str(d.get("symbol") or "") != symbol:
            continue
        t = int(d.get("time") or 0)
        best = None
        for k in kept:
            kt = int(k["ts"])
            gap = abs(t - kt)
            if gap <= iv and (best is None
                              or gap < abs(t - int(best["ts"]))):
                best = k
        if best is not None:
            assign[int(best["ts"])] = d

    slots = []
    for k in kept:
        kt = int(k["ts"])
        pu = float(k.get("p_up") or 0.5)
        mdir = 1 if pu > 0.5 else (-1 if pu < 0.5 else 0)
        hit = assign.get(kt)
        adir, ticket, magic, pnl = 0, None, None, None
        if hit is not None:
            typ = int(hit.get("type", -1))
            adir = 1 if typ == 0 else (-1 if typ == 1 else 0)
            ticket = hit.get("ticket")
            magic = int(hit.get("magic") or 0)
            pid = hit.get("position_id")
            if pid in pnl_by_pos:
                pnl = round(pnl_by_pos[pid], 2)
        slots.append({
            "ts": kt, "p_up": pu, "model_dir": mdir, "actual_dir": adir,
            "ticket": ticket, "magic": magic, "pnl": pnl,
            "against": bool(adir and mdir and adir != mdir),
        })

    against = [s for s in slots if s["against"]]
    return {
        "slots": slots,
        "against": len(against),
        "net": round(sum(s["pnl"] for s in against
                         if s["pnl"] is not None), 2),
        "trades": sum(1 for s in slots if s["actual_dir"]),
        "interval_sec": iv,
    }
