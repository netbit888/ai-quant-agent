"""场次（session）切分与「本场复盘」所需要的数据。

为什么需要「场次」
------------------
盘后复盘真正想看的不是「账户三个月怎么样」，而是「我刚坐下来这一场怎么样」。
账户级统计把几十场混在一起，任何一场的教训都被平均掉了。

切分口径
--------
**上一笔平仓 → 下一笔开仓** 的间隔超过 30 分钟就断场。这个间隔是
「我真的站起来歇了一会儿」，比「开盘到开盘」或「平仓到平仓」更接近直觉；
持仓重叠（对冲）时间隔为负，自然算同一场。

**场次不用于统计胜率。** 单笔场次很常见（做一笔就停），按场次算胜率只会得到
0% 或 100%。场次是「阅读单元」和「AI 分析单元」，统计仍然走全账户
（`live/stats.py`）。

派生字段
--------
每笔交易补三个复盘必须、但 MT5 成交记录里没有的数：

  - `hold_min`   持仓分钟数
  - `sl_points`  止损宽度（points）。优先取持仓快照里开仓时的 SL；
                 没有快照时，止损离场的单子用成交价反推（含滑点，偏保守）
  - `risk_pct`   单笔计划风险占**开仓时余额**的百分比

`risk_pct` 是这一轮最重要的数字：同样是 0.01 手，止损宽 3 倍就意味着
单笔风险宽 3 倍。账户被这种方式打死，跟行情没关系。
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

# 断场间隔：上一笔平仓到下一笔开仓超过这个秒数，就认为「这一场结束了」
GAP_SECONDS = 30 * 60

# 「亏损后立刻再入场」的判定窗口
REVENGE_GAP = 5 * 60

# 「手痒」：持仓不到 1 分钟
ITCHY_SECONDS = 60

_SPEC_CACHE: dict[str, dict | None] = {}


# ------------------------------------------------------------------ 品种规格


def symbol_spec(symbol: str) -> dict | None:
    """拿到 point 与合约大小，用来把价格距离换算成 money。

    权威来源是 `backtest/symbols.py`（回测与实盘共用一份）。
    不在表里的品种返回 None：宁可这一笔不给风险百分比，也不能拿黄金的
    规格去猜——猜出来的数会静默错掉，而且看起来跟真的一样。
    """
    if symbol in _SPEC_CACHE:
        return _SPEC_CACHE[symbol]
    spec: dict | None = None
    try:
        from backtest.symbols import SYMBOLS

        s = SYMBOLS.get(symbol)
        if s:
            spec = {"point": float(s["point"]), "contract": float(s["contract"])}
    except Exception:  # noqa: BLE001
        pass
    if spec is None:
        # 退回画像里的 point（只在品种与画像一致时成立）
        try:
            from live.profile import load_profile

            p = load_profile()
            if p.get("symbol") == symbol:
                spec = {"point": float(p.get("point") or 0.001), "contract": 100.0}
        except Exception:  # noqa: BLE001
            pass
    _SPEC_CACHE[symbol] = spec
    return spec


# ------------------------------------------------------------------ 余额时间线


def balance_before(trades: list[dict], flows: list[dict] | None = None,
                   deposits: float = 0.0) -> dict[str, float]:
    """每笔交易**开仓那一刻**的账户余额。

    用「出入金流水 + 逐笔平仓盈亏」重建，和 stats.build_balance_curve 同源，
    只是这里关心的是时间线上某个点的值，而不是峰值回撤。

    负数按负余额保护截到 0——券商不会让余额变成负数。
    """
    events: list[tuple[int, int, str | None, float]] = []
    for f in flows or []:
        events.append((int(f["time"]), 0, None, float(f["amount"])))
    if not flows and deposits:
        start = min((int(t["entry_time"]) for t in trades if t.get("entry_time")), default=0)
        events.append((start, 0, None, float(deposits)))
    for t in trades:
        if t.get("exit_time"):
            events.append((int(t["exit_time"]), 1, None, float(t["profit"])))
    for t in trades:
        if t.get("entry_time"):
            events.append((int(t["entry_time"]), 2, str(t.get("position_id")), 0.0))

    # 同一时刻：先入金，再平仓，最后开仓
    events.sort(key=lambda e: (e[0], e[1]))

    balance = 0.0
    out: dict[str, float] = {}
    for _, kind, pid, amount in events:
        balance += amount
        if balance < 0:
            balance = 0.0
        if kind == 2 and pid is not None:
            out[pid] = round(balance, 2)
    return out


# ------------------------------------------------------------------ 单笔派生


def enrich(trades: list[dict], *, flows: list[dict] | None = None, deposits: float = 0.0,
           plans: dict[str, dict] | None = None, risk_unit: float | None = None) -> list[dict]:
    """补上复盘需要的派生字段。返回新列表，不改入参。"""
    plans = plans or {}
    bal = balance_before(trades, flows, deposits)
    out: list[dict] = []

    for t in trades:
        row = dict(t)
        pid = str(t.get("position_id"))
        entry = float(t.get("entry_price") or 0.0)
        # 部分平仓只结算了 volume_closed，风险金额按已平部分算
        volume = float(t.get("volume_closed") or t.get("volume") or 0.0)
        spec = symbol_spec(str(t.get("symbol") or ""))

        hold = None
        if t.get("exit_time") and t.get("entry_time"):
            hold = round((t["exit_time"] - t["entry_time"]) / 60.0, 2)
        row["hold_min"] = hold

        # ---- 止损宽度 ----
        plan = plans.get(pid) or {}
        sl_price = float(plan.get("sl") or 0.0)
        sl_points = None
        if spec and sl_price > 0 and entry > 0:
            sl_points = abs(entry - sl_price) / spec["point"]
        elif spec and t.get("close_reason") == "sl" and t.get("exit_price") and entry > 0:
            # 没有快照时用止损成交价反推：价格差等于止损距离（滑点让结果偏保守）
            sl_points = abs(float(t["exit_price"]) - entry) / spec["point"]
        row["sl_price"] = round(sl_price, 6) if sl_price > 0 else None
        row["sl_points"] = round(sl_points) if sl_points else None

        # ---- 单笔风险占余额 ----
        before = bal.get(pid)
        risk_amount = None
        risk_pct = None
        if spec and sl_points and volume > 0:
            risk_amount = sl_points * spec["point"] * volume * spec["contract"]
            if before and before > 0:
                risk_pct = round(risk_amount / before * 100, 1)
        row["balance_before"] = before
        row["risk_amount"] = round(risk_amount, 2) if risk_amount is not None else None
        row["risk_pct"] = risk_pct

        # ---- R 倍数（相对全账户平均亏损，与 quality.py 同口径）----
        if risk_unit:
            row["r"] = round(float(t.get("profit") or 0.0) / risk_unit, 2)
        else:
            row["r"] = None

        out.append(row)
    return out


# ------------------------------------------------------------------ 切分


def split(trades: list[dict], gap_seconds: int = GAP_SECONDS) -> list[list[dict]]:
    """按「上一笔平仓 → 下一笔开仓」的间隔切场。只切已平仓的交易。"""
    closed = [t for t in trades if t.get("exit_time")]
    closed.sort(key=lambda t: (t["exit_time"], t["entry_time"]))

    groups: list[list[dict]] = []
    cur: list[dict] = []
    prev_exit: int | None = None
    for t in closed:
        if cur and prev_exit is not None and t["entry_time"] - prev_exit > gap_seconds:
            groups.append(cur)
            cur = []
        cur.append(t)
        prev_exit = t["exit_time"]
    if cur:
        groups.append(cur)
    return groups


def session_id(items: list[dict]) -> str:
    """稳定 id：用本场第一笔的开仓时间。同一批数据永远得到同一个 id，
    这样备注和 AI 缓存不会因为重算而错位。"""
    return f"S{int(items[0]['entry_time'])}"


# ------------------------------------------------------------------ 统计


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _stats(items: list[dict]) -> dict:
    n = len(items)
    profits = [float(t.get("profit") or 0.0) for t in items]
    net = round(sum(profits), 2)
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    rr = round(avg_win / avg_loss, 2) if avg_loss else None

    holds = [t["hold_min"] for t in items if t.get("hold_min") is not None]
    risks = [t["risk_pct"] for t in items if t.get("risk_pct") is not None]
    sls = [t["sl_points"] for t in items if t.get("sl_points")]

    # 盈亏平衡线（事后口径）：p = 1/(1+rr)
    need = round(100.0 / (1.0 + rr), 1) if rr else None
    actual = round(len(wins) / n * 100, 1) if n else 0.0
    margin = round(actual - need, 1) if need is not None else None

    return {
        "n": n,
        "net": net,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": actual,
        "pf": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "rr": rr,
        "need_win_rate": need,
        "margin_pp": margin,
        "median_hold_min": round(_median(holds), 2) if holds else None,
        "max_hold_min": round(max(holds), 2) if holds else None,
        "max_risk_pct": max(risks) if risks else None,
        "median_risk_pct": round(_median(risks), 1) if risks else None,
        "sl_points": sls,
        "sl_width_ratio": round(max(sls) / min(sls), 2) if len(sls) >= 2 and min(sls) > 0 else None,
        "long": sum(1 for t in items if t.get("direction", 0) > 0),
        "short": sum(1 for t in items if t.get("direction", 0) < 0),
        "manual": sum(1 for t in items if (t.get("close_reason") or "manual") == "manual"),
    }


def _flags(items: list[dict], st: dict) -> list[dict]:
    """自动行为标记。全部由时间戳和价格算出来，不需要用户打标签。

    这正是「打完交易很累、不想打标签」的解法：能算的都不问人。
    """
    flags: list[dict] = []
    med_sl = st["sl_points"] and statistics.median(st["sl_points"])
    med_risk = st["median_risk_pct"]

    for i, t in enumerate(items):
        pid = str(t.get("position_id"))
        # 手痒：持仓不到 1 分钟
        if t.get("hold_min") is not None and t["hold_min"] * 60 < ITCHY_SECONDS:
            flags.append({"code": "itchy", "level": "mid", "pid": pid,
                          "title": "持仓不到 1 分钟",
                          "detail": f"持仓 {t['hold_min'] * 60:.0f} 秒。黄金点差 240 points，"
                                    "开平各一次的成本很难在这么短时间内赚回来。"})
        # 报复单：上一笔亏损后 5 分钟内再入场
        if i > 0:
            prev = items[i - 1]
            gap = t["entry_time"] - prev["exit_time"]
            if prev.get("profit", 0) < 0 and 0 <= gap <= REVENGE_GAP:
                flags.append({"code": "revenge", "level": "high", "pid": pid,
                              "title": "亏损后 5 分钟内再入场",
                              "detail": f"上一笔 {prev['profit']:+.2f} 平仓后 {gap // 60} 分 "
                                        f"{gap % 60} 秒就开了这一单。"})
        # 止损过宽：宽过本场中位数 2 倍
        if med_sl and t.get("sl_points") and t["sl_points"] > med_sl * 2:
            flags.append({"code": "wide_stop", "level": "high", "pid": pid,
                          "title": f"止损 {t['sl_points']:.0f} 点，是本场中位数的 "
                                   f"{t['sl_points'] / med_sl:.1f} 倍",
                          "detail": "手数没变、止损放宽，等于这一笔的风险是别的好几倍。"})
        # 超仓：风险超过本场中位数 2 倍
        if med_risk and t.get("risk_pct") and t["risk_pct"] > med_risk * 2:
            flags.append({"code": "oversize", "level": "high", "pid": pid,
                          "title": f"单笔风险 {t['risk_pct']}% 余额",
                          "detail": f"本场中位数 {med_risk}%。同样的手数，风险差这么多说明"
                                    "止损宽度在变，不是仓位在变。"})

    # 场次级：一笔定生死
    if st["n"] >= 3 and st["avg_loss"] < 0:
        worst = min(float(t.get("profit") or 0) for t in items)
        if worst < st["avg_loss"] * 2:
            flags.append({"code": "one_bad_trade", "level": "mid", "pid": None,
                          "title": f"有一笔亏 {worst:.2f}，是本场平均亏损的 "
                                   f"{worst / st['avg_loss']:.1f} 倍",
                          "detail": "这一笔的结果基本决定了本场，其余笔数接近打平。"})
    return flags


# 行为标记的中文名与一句话解释，面板和 CLI 共用
MARK_LABELS: dict[str, tuple[str, str]] = {
    "revenge": ("报复单", "上一笔亏损后 5 分钟内再入场"),
    "itchy": ("手痒", "持仓不到 1 分钟"),
    "wide_stop": ("止损过宽", "止损宽过本场中位数 2 倍"),
    "oversize": ("超仓", "单笔风险超本场中位数 2 倍"),
    "one_bad_trade": ("一笔定输赢", "单笔亏损 ≥ 本场平均亏损 2 倍"),
}


def mark_summary(sessions: list[dict]) -> list[dict]:
    """跨场累计行为标记：出现次数、涉及净盈亏、胜率。

    单场看一次标记没有意义（「今天手痒一次」）。累计起来才能回答
    「这个习惯一共让我亏了多少」——对手动交易者，这是唯一能直接改的东西，
    比任何策略参数都值钱。

    口径：
    - 笔级标记（pid 有值）：次数 = 笔数，净盈亏/胜率取该笔交易
    - 场级标记（pid 为 None，如一笔定输赢）：次数 = 场数，净盈亏取本场
    """
    agg: dict[str, dict] = {}
    for s in sessions:
        trades = {str(t.get("position_id")): t for t in (s.get("trades") or [])}
        st = s.get("stats") or {}
        for f in s.get("flags") or []:
            code = f.get("code") or "?"
            row = agg.setdefault(code, {"sessions": set(), "count": 0, "net": 0.0, "wins": 0})
            row["sessions"].add(s.get("id"))
            if f.get("pid"):
                t = trades.get(str(f["pid"]))
                row["count"] += 1
                if t is not None:
                    p = float(t.get("profit") or 0.0)
                    row["net"] += p
                    row["wins"] += 1 if p > 0 else 0
            else:
                row["count"] += 1
                p = float(st.get("net") or 0.0)
                row["net"] += p
                row["wins"] += 1 if p > 0 else 0

    out: list[dict] = []
    for code, r in agg.items():
        label, hint = MARK_LABELS.get(code, (code, ""))
        out.append({
            "code": code,
            "label": label,
            "hint": hint,
            "count": r["count"],
            "unit": "场" if code == "one_bad_trade" else "笔",
            "sessions": len(r["sessions"]),
            "net": round(r["net"], 2),
            "win_rate": round(r["wins"] / r["count"] * 100, 1) if r["count"] else None,
        })
    # 次数多的在前；次数相同按亏得多在前
    return sorted(out, key=lambda x: (x["count"], -x["net"]), reverse=True)


def metrics(items: list[dict], st: dict | None = None) -> dict:
    """喂给「下一场规则」的可检验指标。只放能稳定算出来的东西。"""
    st = st or _stats(items)
    streak = best = 0
    prev_dir = 0
    for t in items:
        d = 1 if (t.get("direction") or 0) > 0 else -1
        streak = streak + 1 if d == prev_dir else 1
        prev_dir = d
        best = max(best, streak)
    under = sum(1 for t in items if (t.get("hold_min") or 0) * 60 < ITCHY_SECONDS)
    return {
        "trades": st["n"],
        "net": st["net"],
        "max_same_direction_streak": best,
        "trades_under_60s": under,
        "share_under_60s": round(under / st["n"] * 100, 1) if st["n"] else 0.0,
        "max_risk_pct": st["max_risk_pct"],
        "median_hold_min": st["median_hold_min"],
        "manual_share": round(st["manual"] / st["n"] * 100, 1) if st["n"] else 0.0,
        "sl_width_ratio": st["sl_width_ratio"],
    }


def build(trades: list[dict], *, flows: list[dict] | None = None, deposits: float = 0.0,
          plans: dict[str, dict] | None = None, risk_unit: float | None = None,
          gap_seconds: int = GAP_SECONDS) -> list[dict]:
    """切场并算好卡片数据。返回**按时间倒序**（最新一场在前）。

    每项的 `index` 是时间正序编号，用来显示「第 N 场」。

    risk_unit 不给时按全账户平均亏损算（与 stats / quality 同口径），
    这样跨场次的 R 可以直接横向比较。
    """
    if risk_unit is None:
        losses = [abs(float(t.get("profit") or 0)) for t in trades
                  if t.get("exit_time") and float(t.get("profit") or 0) < 0]
        risk_unit = (sum(losses) / len(losses)) if losses else None

    rows = enrich(trades, flows=flows, deposits=deposits, plans=plans, risk_unit=risk_unit)
    groups = split(rows, gap_seconds)

    sessions: list[dict] = []
    for i, items in enumerate(groups, start=1):
        st = _stats(items)
        start = int(items[0]["entry_time"])
        end = int(max(t["exit_time"] for t in items))
        sessions.append({
            "id": session_id(items),
            "index": i,
            "start": start,
            "end": end,
            "span_min": round((end - start) / 60.0, 1),
            "date": datetime.fromtimestamp(end, timezone.utc).strftime("%m-%d"),
            "stats": st,
            "trades": items,
            "flags": _flags(items, st),
            "metrics": metrics(items, st),
        })
    sessions.reverse()
    return sessions
