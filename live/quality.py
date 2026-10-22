"""交易质量：滚动窗口下的近期表现，回答「最近做得怎么样」。

和 `live.stats` 的分工
----------------------
- `stats.py` 是**全账户总览**：从开户到现在的累计表现。
- `quality.py` 是**滚动窗口**：只看最近一段时间/最近若干笔，并和它前面那个
  等长窗口对比，判断是在变好还是变差。

两个轴，别混在一起
------------------
- **时间窗**（近 7 天 / 30 天 / 1 年）回答「这段时间状态如何」——
  策略在当前行情里还灵不灵。
- **笔数窗**（近 20 / 100 笔）回答「统计上能不能信」——样本够不够下结论。

同一周可能做了 26 笔也可能只做 1 笔，只看一个轴会得出自相矛盾的结论。

小样本护栏
----------
近 10 笔胜率 50%，二项分布 95% 置信区间是 [24%, 76%]，什么都判断不了。
所以每个窗口都给出 Wilson 区间，且 n < MIN_SAMPLE 时标记 `low_sample`，
面板必须显示「样本不足，不可据此决策」。没有这条护栏，这个模块是在生产错误决策。

口径约定
--------
- 一笔交易归属哪个窗口，按**平仓时间**（`exit_time`）算，不是开仓时间。
- R = 该笔盈亏 / 全历史平均亏损。用固定基准而不是窗口内基准，
  这样不同窗口的 R 可以横向比较。
- 窗口内回撤用**累计 R** 的峰谷差，不受手数变化影响；同时给美元值供参考。

过程指标（manual/sl/tp、持仓时长、频率）
--------------------------------------
这些数字不随行情运气漂移，所以容易被拿来当「诊断结论」。三条红线：

1. **比例一律带 Wilson 区间**。n=9 时止损率 44.4% 的区间是 [18.9%, 73.3%]，
   点估计本身没有信息量。没有 CI 的比例禁止写进任何结论。
2. **归因必须用窗口内的真实数据算**，不许写死文案。「止盈占比低说明拿不住」
   这类话把一种可能印成了唯一解释；本模块只回答可证伪的问题——
   各离场方式各赚亏多少、点差成本占多少、高频日与低频日的日均盈亏差多少。
3. **成本按 backtest/symbols.py 的实测点差 + 回测引擎同款公式估算**。
   账本 profit 不含点差（点差做在成交价里），不估就等于把成本记成 0。
   未知品种没有点差数据时标记 spread_unknown，不猜 0。
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone

from live.cost import cost_summary

MIN_SAMPLE = 20

WINDOWS: list[dict] = [
    {"key": "7d", "label": "近 7 天", "kind": "time", "days": 7},
    {"key": "30d", "label": "近 30 天", "kind": "time", "days": 30},
    {"key": "1y", "label": "近 1 年", "kind": "time", "days": 365},
    {"key": "20n", "label": "近 20 笔", "kind": "count", "n": 20},
    {"key": "100n", "label": "近 100 笔", "kind": "count", "n": 100},
]


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项分布比例的 Wilson 置信区间（百分比）。

    比正态近似可靠：n 小或比例接近 0/1 时不会算出负的下界。
    """
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = max(0.0, (center - half) / denom)
    hi = min(1.0, (center + half) / denom)
    return (round(lo * 100, 1), round(hi * 100, 1))


def _closed(trades: list[dict]) -> list[dict]:
    rows = [t for t in trades if t.get("exit_time")]
    rows.sort(key=lambda t: t["exit_time"])
    return rows


def _risk_unit(all_trades: list[dict]) -> float:
    """全历史平均亏损，作为 R 的基准。窗口之间共用，保证可比。"""
    losses = [abs(t["profit"]) for t in all_trades if t["profit"] < 0]
    return sum(losses) / len(losses) if losses else 1.0


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _reason_net(rows: list[dict]) -> dict:
    """各平仓方式的笔数与净盈亏（净盈亏 = 账本 profit，不含点差）。

    回答的是「这个窗口里钱是从哪种离场方式上来/下去的」，
    而不是给离场方式贴「拿不住」「入场偏远」这类标签。
    """
    out: dict[str, dict] = {}
    for t in rows:
        r = t.get("close_reason") or "manual"
        b = out.setdefault(r, {"trades": 0, "net": 0.0})
        b["trades"] += 1
        b["net"] = round(b["net"] + t["profit"], 2)
    return out


def _freq(rows: list[dict]) -> dict:
    """按自然日分高低频组，检验「高频日往往是亏损日」。

    这句话以前是模板里的写死文案，现在由数据说：
    交易日按当日笔数排序，下半部分为低频日、上半为高频日（奇数天时
    中间那天不进任何一组），比较两组日均盈亏；日笔数-当日盈亏的
    Pearson 相关只在 ≥5 个交易日时给（3 个点能拟合出任何结论）。
    天数不够时对应字段留 None，面板显示「分不出高低频」，不硬给结论。
    """
    days: dict[str, dict] = {}
    for t in rows:
        b = days.setdefault(_day(t["exit_time"]), {"trades": 0, "profit": 0.0})
        b["trades"] += 1
        b["profit"] = round(b["profit"] + t["profit"], 2)
    ordered = sorted(days.values(), key=lambda r: r["trades"])
    n = len(ordered)
    out: dict = {"days": n, "median_trades": None, "hi_mean": None, "lo_mean": None, "corr": None}
    if n >= 4:
        mid = n // 2
        lo, hi = ordered[:mid], ordered[n - mid:]
        out["median_trades"] = statistics.median(r["trades"] for r in ordered)
        out["lo_mean"] = round(sum(r["profit"] for r in lo) / len(lo), 2)
        out["hi_mean"] = round(sum(r["profit"] for r in hi) / len(hi), 2)
    if n >= 5:
        # 少于 5 个交易日时相关系数基本是噪声（3 个点能拟合出任何结论），不算
        xs = [r["trades"] for r in ordered]
        ys = [r["profit"] for r in ordered]
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        # 笔数没有波动（每天都一样多）或盈亏没有波动时，相关无定义
        if sxx > 0 and syy > 0:
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            out["corr"] = round(sxy / math.sqrt(sxx * syy), 2)
    return out


def _pick(closed: list[dict], window: dict, now: int) -> tuple[list[dict], list[dict]]:
    """返回 (当前窗口的交易, 前序等长窗口的交易)。"""
    if window["kind"] == "count":
        n = int(window["n"])
        cur = closed[-n:]
        prev = closed[-2 * n : -n]
        return cur, prev

    days = int(window["days"])
    span = days * 86400
    lo, hi = now - span, now
    cur = [t for t in closed if lo <= t["exit_time"] <= hi]
    prev_lo, prev_hi = lo - span, lo
    prev = [t for t in closed if prev_lo <= t["exit_time"] < prev_hi]
    return cur, prev


def _metrics(rows: list[dict], risk_unit: float) -> dict:
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "low_sample": True,
            "net": 0.0,
            "expectancy": 0.0,
            "expectancy_r": 0.0,
            "pf": None,
            "win_rate": 0.0,
            "win_ci": [0.0, 0.0],
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "rr": None,
            "dd_r": 0.0,
            "dd_usd": 0.0,
            "hold_median_min": None,
            "trades_per_day": 0.0,
            "span_days": 0.0,
            "manual_pct": 0.0,
            "sl_pct": 0.0,
            "tp_pct": 0.0,
            "manual_ci": [0.0, 0.0],
            "sl_ci": [0.0, 0.0],
            "tp_ci": [0.0, 0.0],
            "reason_net": {},
            "cost": {
                "spread_usd": 0.0, "fee_usd": 0.0, "total_usd": 0.0,
                "per_trade_usd": 0.0, "gross_abs": 0.0, "cost_pct": None,
                "spread_unknown": False, "symbols": [],
            },
            "freq": {"days": 0, "median_trades": None, "hi_mean": None, "lo_mean": None, "corr": None},
        }

    wins = [t for t in rows if t["profit"] > 0]
    losses = [t for t in rows if t["profit"] < 0]
    gross_win = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))
    net = gross_win - gross_loss
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if len(losses) else 0.0

    # 窗口内累计 R 的回撤：不受手数变化影响，可跨窗口比较
    cum = 0.0
    peak = 0.0
    dd_r = 0.0
    dd_usd = 0.0
    for t in rows:
        cum += t["profit"] / risk_unit
        peak = max(peak, cum)
        if peak - cum > dd_r:
            dd_r = peak - cum
            dd_usd = (peak - cum) * risk_unit

    holds = sorted((t["exit_time"] - t["entry_time"]) / 60 for t in rows)
    span_days = max((rows[-1]["exit_time"] - rows[0]["entry_time"]) / 86400, 1 / 24)

    n_manual = sum(1 for t in rows if (t.get("close_reason") or "manual") == "manual")
    n_sl = sum(1 for t in rows if t.get("close_reason") == "sl")
    n_tp = sum(1 for t in rows if t.get("close_reason") == "tp")

    return {
        "n": n,
        "low_sample": n < MIN_SAMPLE,
        "net": round(net, 2),
        "expectancy": round(net / n, 2),
        "expectancy_r": round((net / n) / risk_unit, 3),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "win_rate": round(len(wins) / n * 100, 1),
        "win_ci": wilson_ci(len(wins), n),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "rr": round(avg_win / avg_loss, 2) if avg_loss else None,
        "dd_r": round(dd_r, 2),
        "dd_usd": round(dd_usd, 2),
        "hold_median_min": round(statistics.median(holds), 1),
        "trades_per_day": round(n / span_days, 2),
        "span_days": round(span_days, 1),
        "manual_pct": round(n_manual / n * 100, 1),
        "sl_pct": round(n_sl / n * 100, 1),
        "tp_pct": round(n_tp / n * 100, 1),
        # 比例必须带区间：n=9 时点估计的方向都可能是反的
        "manual_ci": wilson_ci(n_manual, n),
        "sl_ci": wilson_ci(n_sl, n),
        "tp_ci": wilson_ci(n_tp, n),
        "reason_net": _reason_net(rows),
        "cost": cost_summary(rows),
        "freq": _freq(rows),
    }


def _delta(cur: dict, prev: dict) -> dict:
    """和前序窗口比。prev 样本为 0 时返回 None。"""
    if not prev.get("n"):
        return {}
    out = {}
    for key in ("expectancy_r", "pf", "win_rate", "dd_r"):
        a, b = cur.get(key), prev.get(key)
        if a is None or b is None:
            out[key] = None
        else:
            out[key] = round(a - b, 3)
    return out


def _verdict(cur: dict, prev: dict) -> str:
    """一句话判定：变好 / 变差 / 持平 / 样本不足。"""
    if not cur.get("n"):
        return "no_data"
    if cur.get("low_sample"):
        return "low_sample"
    d = _delta(cur, prev)
    er = d.get("expectancy_r")
    if er is None:
        return "no_baseline"
    if er >= 0.05:
        return "better"
    if er <= -0.05:
        return "worse"
    return "flat"


def rolling_expectancy(trades: list[dict], span: int = 20) -> list[dict]:
    """滚动期望 R 序列。X 是平仓时间，Y 是该时点往前 span 笔的期望 R。

    这一张图比所有表格加起来都有用——能直接看出从哪天开始失效。
    """
    closed = _closed(trades)
    if len(closed) < span:
        return []
    unit = _risk_unit(closed)
    out = []
    for i in range(span - 1, len(closed)):
        window = closed[i - span + 1 : i + 1]
        net = sum(t["profit"] for t in window)
        out.append(
            {
                "ts": closed[i]["exit_time"],
                "r": round((net / span) / unit, 3),
                "n": span,
            }
        )
    return out


def compute(trades: list[dict], now: int | None = None) -> dict:
    """所有窗口的近期质量快照。"""
    now = int(now or time.time())
    closed = _closed(trades)
    if not closed:
        return {"empty": True, "windows": [], "rolling": [], "min_sample": MIN_SAMPLE}

    unit = _risk_unit(closed)
    windows = []
    for spec in WINDOWS:
        cur_rows, prev_rows = _pick(closed, spec, now)
        cur = _metrics(cur_rows, unit)
        prev = _metrics(prev_rows, unit)
        windows.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "kind": spec["kind"],
                "current": cur,
                "previous": prev,
                "delta": _delta(cur, prev),
                "verdict": _verdict(cur, prev),
            }
        )

    return {
        "empty": False,
        "min_sample": MIN_SAMPLE,
        "risk_unit": round(unit, 2),
        "total": len(closed),
        "windows": windows,
        "rolling": rolling_expectancy(closed, 20),
        "generated_at": now,
    }


# ------------------------------------------------------------------ 每日快照
#
# 滚动窗口的历史值不可重现（「上周的近 10 笔」早被新交易顶掉了），
# 所以每天存一行，才能画出「质量随时间」的真趋势。

def snapshot_row(quality: dict, day: str | None = None) -> dict:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {"date": day}
    for w in quality.get("windows", []):
        c = w["current"]
        row[w["key"]] = {
            "n": c["n"],
            "expectancy_r": c["expectancy_r"],
            "pf": c["pf"],
            "win_rate": c["win_rate"],
            "net": c["net"],
        }
    return row


def merge_snapshot(existing: list[dict], row: dict) -> list[dict]:
    """同一天只保留一条。"""
    rows = {r["date"]: r for r in existing if isinstance(r, dict) and "date" in r}
    rows[row["date"]] = row
    return [rows[k] for k in sorted(rows)]
