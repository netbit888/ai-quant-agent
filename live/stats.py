"""实盘统计：把逐笔交易汇总成可操作的数字。

所有指标都有明确的定义，不做模糊表述：

  - 盈利因子 PF = 总盈利 / |总亏损|
  - 期望 R = 平均每笔盈亏 / 平均每笔亏损（用平仓单的均价做风险单位）
  - **最大回撤按「账户余额曲线」算**，不是按「累计交易盈亏」算。
    余额曲线 = 出入金流水 + 逐笔平仓盈亏，按时间累积。
    两者差别很大：累计盈亏从 0 起算，一旦某段时间盈利后回吐，
    (峰值-谷值)/峰值 会算出几百 %，那不是回撤，是算术错误。
  - 余额下限取 0：券商有负余额保护，穿仓后账户会被抹平到 0，
    不可能真的亏到负数。因此回撤百分比的上限是 100%。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone

from live.cost import cost_summary


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _hour(ts: int) -> int:
    return datetime.fromtimestamp(ts, timezone.utc).hour


def _weekday(ts: int) -> int:
    return datetime.fromtimestamp(ts, timezone.utc).weekday()  # 0=周一


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def build_balance_curve(trades: list[dict], flows: list[dict] | None, deposits: float) -> dict:
    """按时间累积出账户余额曲线，并算出真实的峰值回撤。

    flows: [{"time": ts, "amount": 正数入金/负数出金}, ...]
           为 None 或空时，退化为「起始本金在首笔交易前一次性到账」。
    """
    closed = sorted([t for t in trades if t.get("exit_time")], key=lambda t: t["exit_time"])

    events: list[tuple[int, float, int]] = []  # (时间, 金额, 排序权重)
    if flows:
        for f in flows:
            events.append((int(f["time"]), float(f["amount"]), 0))
    elif deposits:
        # 没有流水明细时，把净入金当作交易开始前到账
        start = closed[0]["entry_time"] if closed else 0
        events.append((int(start), float(deposits), 0))
    for t in closed:
        events.append((int(t["exit_time"]), float(t["profit"]), 1))

    # 同一时刻先到账再交易
    events.sort(key=lambda e: (e[0], e[2]))

    # 按时间戳分组：同一秒内的多笔流水必须一起结算。
    # 否则「穿仓 -0.94 → 券商补 0.94 归零」会被拆成两步，
    # 中间那笔补款反而被当成新的峰值。
    groups: list[tuple[int, list[tuple[float, int]]]] = []
    for ts, amount, kind in events:
        if groups and groups[-1][0] == ts:
            groups[-1][1].append((amount, kind))
        else:
            groups.append((ts, [(amount, kind)]))

    balance = 0.0
    # peak：用于计算回撤的水位线，出金时会同步下调
    # peak_high：历史最高余额，只增不减，用于展示
    peak = 0.0
    peak_high = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0
    max_dd_at = 0
    max_dd_peak = 0.0
    max_dd_trough = 0.0
    climax_at = 0
    wipeouts = 0
    series: list[dict] = []

    for ts, items in groups:
        prev = balance
        trade_delta = 0.0
        flow_delta = 0.0
        for amount, kind in items:
            if kind == 1:
                trade_delta += amount
            else:
                flow_delta += amount
        balance += trade_delta + flow_delta

        # 出金不是亏损：把峰值同步下调，避免"取钱"被算成回撤
        if flow_delta < 0:
            peak = max(balance, peak + flow_delta)

        # 负余额保护：券商在穿仓后会把余额抹到 0
        if balance < 0:
            balance = 0.0

        # 只有「交易造成的」清零才算爆仓，出金取空不算
        if trade_delta < 0 and prev > 0 and balance <= 1e-9:
            wipeouts += 1

        if balance > peak:
            peak = balance
            climax_at = ts
        if balance > peak_high:
            peak_high = balance

        dd = peak - balance
        if dd > max_dd:
            max_dd = dd
            max_dd_at = ts
            max_dd_peak = peak
            max_dd_trough = balance
            max_dd_pct = (dd / peak * 100) if peak > 0 else 0.0

        if trade_delta or flow_delta:
            series.append({"ts": ts, "balance": round(balance, 2), "trade": round(trade_delta, 2)})

    return {
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(min(max_dd_pct, 100.0), 1),
        "peak_balance": round(peak_high, 2),
        "dd_peak": round(max_dd_peak, 2),
        "trough_balance": round(max_dd_trough, 2),
        "dd_at": max_dd_at,
        "climax_at": climax_at,
        "final_balance": round(balance, 2),
        "wipeouts": wipeouts,
        "series": series,
    }


def compute(
    trades: list[dict],
    account: dict | None = None,
    deposits: float = 0.0,
    flows: list[dict] | None = None,
) -> dict:
    closed = [t for t in trades if t.get("exit_time")]
    closed.sort(key=lambda t: t["exit_time"])

    if not closed:
        return {
            "empty": True,
            "count": 0,
            "note": "账本里还没有已平仓的交易",
            "cost": cost_summary([]),
            "daily": [],
            "by_symbol": [],
            "by_direction": [],
            "by_weekday": [],
            "by_hour": [],
            "by_reason": [],
            "equity": [],
        }

    wins = [t for t in closed if t["profit"] > 0]
    losses = [t for t in closed if t["profit"] < 0]
    flats = [t for t in closed if t["profit"] == 0]

    gross_win = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))
    net = gross_win - gross_loss

    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    risk_unit = avg_loss or 1e-9

    # ---- 累计交易盈亏（不含出入金，用于曲线展示）----
    equity = []
    cum = 0.0
    for t in closed:
        cum += t["profit"]
        equity.append({"ts": t["exit_time"], "cum": round(cum, 2), "profit": t["profit"]})

    # ---- 真实回撤：基于账户余额曲线 ----
    curve = build_balance_curve(closed, flows, deposits)

    # ---- 连续盈亏 ----
    max_win_streak = max_loss_streak = cur_w = cur_l = 0
    for t in closed:
        if t["profit"] > 0:
            cur_w += 1
            cur_l = 0
        elif t["profit"] < 0:
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        max_win_streak = max(max_win_streak, cur_w)
        max_loss_streak = max(max_loss_streak, cur_l)

    # ---- 持仓时长 ----
    holds = [(t["exit_time"] - t["entry_time"]) / 60 for t in closed]
    holds.sort()

    # ---- 分组 ----
    def group(key_fn, label_fn=None):
        bucket: dict = defaultdict(list)
        for t in closed:
            bucket[key_fn(t)].append(t)
        rows = []
        for k, items in bucket.items():
            w = [x for x in items if x["profit"] > 0]
            gw = sum(x["profit"] for x in items if x["profit"] > 0)
            gl = abs(sum(x["profit"] for x in items if x["profit"] < 0))
            rows.append(
                {
                    "key": label_fn(k) if label_fn else k,
                    "trades": len(items),
                    "wins": len(w),
                    "win_rate": round(len(w) / len(items) * 100, 1),
                    "net": round(sum(x["profit"] for x in items), 2),
                    "pf": round(gw / gl, 2) if gl > 0 else None,
                }
            )
        return sorted(rows, key=lambda r: r["net"], reverse=True)

    by_symbol = group(lambda t: t["symbol"])
    by_direction = group(lambda t: "多" if t["direction"] > 0 else "空")
    by_weekday = group(lambda t: _weekday(t["entry_time"]), lambda k: WEEKDAY_CN[k])
    by_hour = group(lambda t: _hour(t["entry_time"]), lambda k: f"{k:02d}:00")
    by_reason = group(lambda t: t.get("close_reason") or "manual", _reason_cn)

    # ---- 逐日 ----
    day_map: dict[str, dict] = {}
    for t in closed:
        d = _day(t["exit_time"])
        row = day_map.setdefault(d, {"date": d, "trades": 0, "wins": 0, "profit": 0.0, "volume": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if t["profit"] > 0 else 0
        row["profit"] = round(row["profit"] + t["profit"], 2)
        row["volume"] = round(row["volume"] + t["volume"], 4)
    daily = [day_map[k] for k in sorted(day_map)]
    for row in daily:
        row["win_rate"] = round(row["wins"] / row["trades"] * 100, 1) if row["trades"] else 0.0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_row = day_map.get(today)
    active_days = len(daily)
    span_days = (
        (closed[-1]["exit_time"] - closed[0]["entry_time"]) / 86400 if len(closed) > 1 else 1.0
    )

    return {
        "empty": False,
        "count": len(closed),
        "open_count": len([t for t in trades if not t.get("exit_time")]),
        "net": round(net, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(-gross_loss, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "expectancy": round(net / len(closed), 2),
        "expectancy_r": round((net / len(closed)) / risk_unit, 3) if avg_loss else None,
        "risk_reward": round(avg_win / avg_loss, 2) if avg_loss else None,
        "max_dd": curve["max_dd"],
        "max_dd_pct": curve["max_dd_pct"],
        "peak_balance": curve["peak_balance"],
        "dd_peak": curve["dd_peak"],
        "trough_balance": curve["trough_balance"],
        "dd_at": curve["dd_at"],
        "wipeouts": curve["wipeouts"],
        "balance_curve": curve["series"],
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "risk_unit": round(avg_loss, 2) if avg_loss else None,
        "cur_streak": cur_w if cur_w else -cur_l,
        "hold_median_min": round(statistics.median(holds), 1) if holds else None,
        "hold_max_min": round(holds[-1], 1) if holds else None,
        "first_trade": closed[0]["entry_time"],
        "last_trade": closed[-1]["exit_time"],
        "active_days": active_days,
        "span_days": round(span_days, 1),
        "trades_per_day": round(len(closed) / max(span_days, 1), 2),
        "today": today_row or {"date": today, "trades": 0, "wins": 0, "profit": 0.0, "win_rate": 0.0},
        "deposits": round(deposits, 2),
        "return_pct": round(net / deposits * 100, 1) if deposits > 0 else None,
        "account": account,
        # 点差按实测规格表补算：账本 profit 不含点差，不补就等于成本记 0
        "cost": cost_summary(closed),
        "daily": daily,
        "equity": equity,
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "by_weekday": by_weekday,
        "by_hour": by_hour,
        "by_reason": by_reason,
        "best": max(closed, key=lambda t: t["profit"]),
        "worst": min(closed, key=lambda t: t["profit"]),
    }


def _reason_cn(k: str) -> str:
    return {
        "tp": "止盈",
        "sl": "止损",
        "stop_out": "强制平仓",
        "close_by": "对冲平仓",
        "manual": "手动平仓",
        "open": "未平仓",
    }.get(k, k)


def risk_flags(stats: dict) -> list[dict]:
    """把统计里明显危险的地方标出来。不依赖 LLM，纯规则，确保每次都跑。"""
    flags: list[dict] = []
    if stats.get("empty"):
        return flags

    acct = stats.get("account") or {}
    balance = acct.get("balance") or 0
    deposits = stats.get("deposits") or 0

    if deposits > 0 and balance > 0 and balance < deposits * 0.3:
        flags.append(
            {
                "level": "high",
                "title": "本金已亏损超过 70%",
                "detail": f"净入金 {deposits:.2f}，当前余额 {balance:.2f}，剩余 {balance / deposits * 100:.1f}%。",
            }
        )

    pf = stats.get("pf")
    if pf is not None and pf < 1:
        flags.append(
            {
                "level": "high",
                "title": f"盈利因子 {pf} < 1",
                "detail": f"总盈利 {stats['gross_win']} 小于总亏损 {stats['gross_loss']}，长期必亏。",
            }
        )
    elif pf is not None and pf < 1.3:
        flags.append(
            {
                "level": "mid",
                "title": f"盈利因子 {pf} 偏低",
                "detail": "低于 1.3 意味着扣掉滑点和隔夜费后大概率转负。",
            }
        )

    if stats.get("max_loss_streak", 0) >= 5:
        flags.append(
            {
                "level": "mid",
                "title": f"最长连亏 {stats['max_loss_streak']} 笔",
                "detail": "连亏 5 笔以上通常意味着策略在某个行情阶段失效，而不是运气差。",
            }
        )

    hold = stats.get("hold_median_min")
    if hold is not None and hold < 10 and stats.get("count", 0) >= 20:
        # 成本必须按本账户真实成交（品种规格 + 实际手数）算，
        # 以前这里写死「240 points 的黄金、2.4 USD/0.1 手」，
        # 换个手数或品种就报错十倍，还当成结论印在面板上。
        c = stats.get("cost") or {}
        if c.get("spread_unknown"):
            detail = (
                f"持仓中位数只有 {hold} 分钟；部分品种没有实测点差数据，"
                "成本未计入，先补 backtest/symbols.py 的规格再判断。"
            )
        elif c.get("cost_pct") is not None:
            detail = (
                f"{stats.get('count', 0)} 笔合计成本 {c['total_usd']:.2f} USD"
                f"（点差 {c['spread_usd']:.2f} + 费用 {c['fee_usd']:.2f}），"
                f"占毛盈亏规模 {c['cost_pct']}%，平均每笔 {c['per_trade_usd']:.2f} USD。"
                "持仓越短，这笔成本越难被单笔盈亏覆盖。"
            )
        else:
            detail = (
                f"持仓中位数只有 {hold} 分钟；全部交易盈亏为 0，"
                "摊不出成本占比。"
            )
        flags.append(
            {
                "level": "mid",
                "title": f"持仓中位数只有 {hold} 分钟",
                "detail": detail,
            }
        )

    so = next((r for r in stats.get("by_reason", []) if r["key"] == "强制平仓"), None)
    wipes = stats.get("wipeouts") or 0
    if wipes > 0:
        flags.append(
            {
                "level": "high",
                "title": f"账户被清零 {wipes} 次",
                "detail": (
                    f"余额曾 {wipes} 次跌到 0（负余额保护抹平）。峰值余额 {stats.get('peak_balance')}，"
                    f"最大回撤 {stats.get('max_dd')}（占峰值 {stats.get('max_dd_pct')}%）。"
                    "这种情况下已经不是策略问题，是仓位管理与账户规模不匹配。"
                ),
            }
        )
    elif so and so["trades"] > 0:
        flags.append(
            {
                "level": "high",
                "title": f"出现 {so['trades']} 次强制平仓（爆仓）",
                "detail": f"这部分合计 {so['net']}。说明仓位管理已经失效，不是策略问题而是资金问题。",
            }
        )

    if acct.get("margin_level") and 0 < acct["margin_level"] < 200:
        flags.append(
            {
                "level": "high",
                "title": f"保证金水平仅 {acct['margin_level']:.0f}%",
                "detail": "低于 200% 已进入危险区，一次反向跳空就可能被强平。",
            }
        )

    if not flags:
        flags.append({"level": "ok", "title": "未触发风险规则", "detail": "当前统计没有命中硬性风险阈值。"})
    return flags
