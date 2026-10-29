"""入场方向回放器：回答「模型给的 p_up 到底有没有方向信息量」。

为什么必须有这个东西
--------------------
10-29 的实测账：282 笔胜率 36.2%，均价 -0.283 USD/笔，做多做空各 36%。
要判断这是「p_up 没信息」还是「p_up 有信息但被闸门/出场吃掉」，靠账本
是回答不了的——账本里只有**真开了的**那 55~282 笔，而模型每轮都给了
p_up（包括说 act=false 的 470 多个点）。样本少一个数量级，任何按
|p_up-0.5| 分桶的结论都会是噪声。

所以这里换个问法：把**每一个决策点**（不只是开仓点）都当成一次假想
入场，方向取 p_up 那一边，出场用现行规则，看单点期望。
这样样本从 55 扩到 527，才能谈「置信度分桶有没有单调性」。

严格说这不是回测，和 replay_exit 一样是**反事实估计**：
  · 入场点是当时真实发生过的模型决策，不重新生成；
  · 这些点里大多数原本没开仓，彼此是同时刻重叠的，**不能把 PnL 相加
    当成总盈亏**，只能看单点期望；
  · 它回答的是：给定这批决策质量，按 p_up 的方向无条件做，期望是多少。

口径说明（和 replay_exit 保持一致，别自己发明第二套）
------------------------------------------------------
  · K 线时间戳是这根 K 线的**开始时间**，所以从 `time > 决策 ts` 的第一根
    起算，避免把决策前走完的行情算进来。
  · 决策点的入场价：开仓行用日志里的真实成交价；跳过行没有价格，只能取
    「决策时刻所在那根 K 线的收盘价」近似——误差不超过一根 M1 的波动。
  · replay_one 对空头扣点差（开在 bid 平在 ask）、对多头不扣，这是它的
    历史口径。这里统一补扣多头那 0.24，让两个方向可比；因此本工具的数
    与 replay_exit 的基线**不可直接比**。
  · 出场参数按决策时刻的 regime 推 plan（趋势 7.0/3h，其余 3.25/1h），
    与 exit_params 一致。plan 是开仓时锁的，跳过行没有 plan，只能用
    regime 推——这是本工具最大的假设，trend 桶的数字要按此打折看。

用法
----
    python -m tools.replay_entry                       # 全样本
    python -m tools.replay_entry --min-edge 0.10       # 只看够强的信号
    python -m tools.replay_entry --reverse             # 反向做（验证是否负相关）
    python -m tools.replay_entry --account 277733809
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.replay_exit import load_bars, replay_one, spec_of  # noqa: E402

# 与 live/autotrade.Cfg 的默认值对齐。刻意不 import Cfg：这个工具要能在
# 没有 openai / 没有面板依赖的机器上跑，import live.autotrade 会拖进整条链。
SL_USD = 3.0
TP_RANGE = 3.25
TP_TREND = 7.0
HOLD_RANGE = 3600
HOLD_TREND = 10800
LOTS = 0.01


def load_points(login: str | None, hours_back: float | None = None) -> list[dict]:
    """从流水里取出所有「模型给过方向」的决策点。

    读的是**全部轮转文件**（autotrade_log*.jsonl），因为 reset_history 会把
    主文件截断，只读主文件等于把历史样本全丢掉。同一秒的同一行会在轮转
    文件里重复出现，按 (ts, kind, reason) 去重。

    p_up=0 的行必须剔除：那是旧版提示词示例 {"p_up": 0.00} 被照抄的占位值，
    clamp 之后当 0.20 用（= 最强看空），混进来会把空头样本灌成两倍。
    """
    from live import paths

    account = paths.resolve_login(login)
    files = sorted(glob.glob(str(paths.account_file(account, "autotrade_log*.jsonl"))))
    if not files:
        raise SystemExit(f"找不到流水：{paths.account_dir(account)}/autotrade_log*.jsonl")

    seen: set[tuple] = set()
    rows: list[dict] = []
    for f in files:
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r.get("ts"), r.get("kind"), r.get("reason"), r.get("ticket"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)

    points = []
    for r in rows:
        if r.get("kind") not in ("open", "skip"):
            continue
        feat = r.get("feat") or {}
        if not feat.get("ok"):
            continue
        p_up = r.get("p_up")
        if p_up is None or float(p_up) <= 0:
            continue
        p_up = float(p_up)
        direction = 1 if p_up > 0.5 else -1
        regime = str(feat.get("regime") or "mid")
        plan = "trend" if regime == "trend" else "range"
        points.append({
            "ts": int(r["ts"]),
            "kind": r["kind"],
            "p_up": p_up,
            "direction": direction,
            "edge": abs(p_up - 0.5),
            "regime": regime,
            "plan": plan,
            "tp": TP_TREND if plan == "trend" else TP_RANGE,
            "hold": HOLD_TREND if plan == "trend" else HOLD_RANGE,
            # 开仓行有真实成交价，跳过行只能回落到 K 线收盘（见 docstring）
            "price": float(r["price"]) if r.get("kind") == "open" and r.get("price") else None,
            "actual_pnl": None,
        })

    # 给开仓点补上真实结果，便于「同一样本内」对照回放与实际
    closes = {r.get("ticket"): r for r in rows
              if r.get("kind") == "close" and r.get("ticket")}
    ticket_of = {(r.get("ts"), r.get("kind")): r.get("ticket") for r in rows}
    for p in points:
        if p["kind"] != "open":
            continue
        t = ticket_of.get((p["ts"], "open"))
        c = closes.get(t) if t else None
        if c and c.get("pnl") is not None:
            p["actual_pnl"] = float(c["pnl"])

    points.sort(key=lambda x: x["ts"])
    if hours_back and points:
        cutoff = points[-1]["ts"] - int(hours_back * 3600)
        points = [p for p in points if p["ts"] >= cutoff]
    return points


def replay_points(points: list[dict], bars: list[dict], symbol: str,
                  reverse: bool = False) -> list[dict]:
    """逐点回放。返回带 pnl/kind/hold 的点列表（丢弃取不到起始下标的）。"""
    spread, contract, _ = spec_of(symbol)
    unit = contract * LOTS
    idx = [b["time"] for b in bars]

    out = []
    for p in points:
        start = bisect.bisect_right(idx, p["ts"])
        if start <= 0 or start >= len(bars):
            continue
        price = p["price"] or bars[start - 1]["close"]
        direction = -p["direction"] if reverse else p["direction"]
        pnl, kind, held = replay_one(
            bars, start, price, direction,
            SL_USD, p["tp"], spread, unit,
            max_bars=max(1, p["hold"] // 60),
        )
        # replay_one 只对空头扣了点差，多头补上，否则「做多看起来更划算」
        if direction > 0:
            pnl = round(pnl - spread * unit, 2)
        out.append({**p, "pnl": pnl, "exit": kind, "held": held,
                    "dir_used": direction})
    return out


def bucket(edge: float) -> str:
    if edge < 0.04:
        return "<0.04"
    if edge < 0.06:
        return "0.04-0.06"
    if edge < 0.08:
        return "0.06-0.08"
    if edge < 0.10:
        return "0.08-0.10"
    if edge < 0.14:
        return "0.10-0.14"
    return ">=0.14"


def stat_row(name: str, rows: list[dict]) -> str:
    v = [r["pnl"] for r in rows]
    if not v:
        return f"{name:16s} n=0"
    wins = sum(1 for x in v if x > 0)
    return (f"{name:16s} n={len(v):4d}  胜率 {wins/len(v):5.1%}  "
            f"均值 {sum(v)/len(v):+6.2f}  中位 {st.median(v):+6.2f}  "
            f"合计 {sum(v):+8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="入场方向反事实回放")
    ap.add_argument("--account", default=None, help="账户号，默认取当前账户")
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--hours-back", type=float, default=None)
    ap.add_argument("--min-edge", type=float, default=0.0,
                    help="只保留 |p_up-0.5| >= 此值的点")
    ap.add_argument("--reverse", action="store_true", help="反向做，验证是否负相关")
    args = ap.parse_args()

    points = load_points(args.account, args.hours_back)
    if not points:
        raise SystemExit("没有带特征的决策点（feat.ok 且 p_up>0）")
    if args.min_edge > 0:
        points = [p for p in points if p["edge"] >= args.min_edge]
        if not points:
            raise SystemExit(f"|p_up-0.5| >= {args.min_edge} 的样本为 0")

    ts_all = [p["ts"] for p in points]
    bars = load_bars(args.symbol, min(ts_all) - 600, max(ts_all) + 4000)
    res = replay_points(points, bars, args.symbol, reverse=args.reverse)

    tag = "反向" if args.reverse else "顺 p_up 方向"
    print(f"\n决策点 {len(points)} 个，可回放 {len(res)} 个（{tag}入场，"
          f"sl={SL_USD} tp={TP_RANGE}/{TP_TREND}）")
    print(f"时间范围 {min(r['ts'] for r in res)} ~ {max(r['ts'] for r in res)}"
          f"  K 线 {len(bars)} 根")

    print("\n=== 总体 ===")
    print(stat_row("全部", res))

    print("\n=== 按 |p_up-0.5| 分桶（核心：看单调性）===")
    groups: dict[str, list[dict]] = {}
    for r in res:
        groups.setdefault(bucket(r["edge"]), []).append(r)
    for k in ("<0.04", "0.04-0.06", "0.06-0.08", "0.08-0.10", "0.10-0.14", ">=0.14"):
        if k in groups:
            print(stat_row(k, groups[k]))

    print("\n=== 按 regime（plan 由 regime 推，trend 桶的 tp 是 7.0）===")
    groups2: dict[str, list[dict]] = {}
    for r in res:
        groups2.setdefault(r["regime"], []).append(r)
    for k in sorted(groups2):
        print(stat_row(k, groups2[k]))

    print("\n=== 按 p_up 方向 ===")
    for label, want in (("做多(p_up>0.5)", 1), ("做空(p_up<0.5)", -1)):
        sub = [r for r in res if (r["p_up"] > 0.5) == (want == 1)]
        print(stat_row(label, sub))

    # 有真实成交的那部分：回放 vs 实际，检验回放口径有没有系统性偏差
    real = [r for r in res if r["actual_pnl"] is not None]
    if real:
        print("\n=== 只在「真开了仓」的点上，校验回放口径 ===")
        print(f"实际成交 {len(real)} 笔  均值 {sum(r['actual_pnl'] for r in real)/len(real):+6.2f}")
        print(f"同点回放      均值 {sum(r['pnl'] for r in real)/len(real):+6.2f}"
              "  （回放假设满仓持到 sl/tp，实际由模型决定出场，不等价）")

    print("\n=== 出场类型分布 ===")
    kinds: dict[str, int] = {}
    for r in res:
        kinds[r["exit"]] = kinds.get(r["exit"], 0) + 1
    print("  " + "  ".join(f"{k}:{v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
