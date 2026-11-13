"""看脉冲留痕的结果：每笔单的峰值是几倍 ATR、多久走满 5 个 ATR、
平仓时保住了峰值的百分之几。

背景（2026-11-13）：用户观察到"趋势单经常几分钟冲到 70 多然后回撤"。
离线统计（tools/measure_burst.py，50 天 M1）证实了方向：3 根内走满 5 ATR
的事件之后 10 分钟中位回吐 1.27 ATR，而同样位移慢速走到（15 根）的只有
−0.20。但 n=30、p≈0.10，没到能改出场逻辑的显著度，所以先在实盘留痕
（close 行的 atr_peak / t5 / burst），攒本账户的真实样本。

这个脚本只读流水，不动仓位、不给交易建议。用法：
    .venv/Scripts/python.exe -m tools.burst_report
    .venv/Scripts/python.exe -m tools.burst_report --plan trend
    .venv/Scripts/python.exe -m tools.burst_report --factor 100   # 美分户

老单子（v1.5.27 之前）没有那几个字段，脚本会用 open 行的 lots、close 行的
atr、持仓期间的 hold 采样现场重算一遍，所以历史趋势单现在就能一起看。
但重算有个硬限制：hold 行是**决策轮**写的（60 秒一条，还不是每轮都有），
而峰值是快查循环 2~5 秒采的。峰值落在两次 hold 之间的单子，穿越时点就
复原不出来——表里显示 `?`，表示"到过这一档，但不知道用了多久"，它既不是
脉冲也不能算慢磨。新单的 t3/t5 挂在峰值采样上，精度是采样间隔而不是 60 秒，
所以**只有新单能真正回答这个问题**。
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live import paths  # noqa: E402

BURST_ATR = 5.0      # 同 live/autotrade.py::_BURST_ATR
BURST_WIN_MIN = 3.0  # 同 live/autotrade.py::_BURST_WIN_MIN


def _rows(account: str) -> list[dict]:
    p = paths.ACCOUNTS_DIR / str(account) / "autotrade_log.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _recalc(closes: list[dict], opens: dict, holds: dict, factor: float) -> None:
    """给没有脉冲字段的老单子补算。原地改 closes。

    unit（1 个 ATR 折成多少钱）= atr × 手数 × 100 × 账户系数。
    100 是 XAUUSD 的合约规模（盎司/手），只对黄金成立——这个脚本也只用来看
    黄金的账本，所以不引入 `_symbol_contract` 那套（它要连 MT5）。
    """
    for r in closes:
        if r.get("atr_peak") is not None:
            continue  # 新单自带，不覆盖（那是逐次采样记的，比这里准）
        try:
            ticket = int(r.get("ticket") or 0)
        except (TypeError, ValueError):
            continue
        o = opens.get(ticket) or {}
        atr = float(r.get("atr") or 0)
        lots = float(o.get("lots") or 0)
        peak = r.get("peak_pnl")
        if atr <= 0 or lots <= 0 or peak is None:
            continue
        unit = atr * lots * 100.0 * factor
        r["atr_peak"] = round(float(peak) / unit, 2)
        opened = int(o.get("opened_at") or o.get("ts") or 0)
        hs = sorted((h for h in holds.get(ticket, [])
                     if h.get("float_pnl") is not None),
                    key=lambda x: x.get("ts") or 0)
        for key, k in (("t3", 3.0), ("t5", BURST_ATR)):
            hit = None
            if opened:
                for h in hs:
                    if float(h.get("float_pnl") or 0) >= k * unit:
                        hit = round((float(h.get("ts")) - opened) / 60.0, 1)
                        break
            r[key] = hit
            # 峰值到过这一档，但 hold 采样（每 60 秒一条，且不是每轮都写）
            # 没落在上面 —— 真实的穿越时点**未知**，不能写成"没到过"。
            # 实测就有这种单子：峰值 6.25 ATR，全部采样里最高只有 4.05。
            if hit is None and float(peak) >= k * unit:
                r[key + "_unknown"] = True
        r["burst"] = r.get("t5") is not None and r["t5"] <= BURST_WIN_MIN
        r["_recalc"] = True


def _fmt_min(r: dict, key: str) -> str:
    """t3/t5 的显示：数值 / `?`（到过但时点未知）/ `-`（没到过）。"""
    v = r.get(key)
    if v is not None:
        return f"{float(v):.1f}"
    return "?" if r.get(key + "_unknown") else "-"


def _keep(row: dict) -> str:
    """落袋 / 峰值。峰值 <= 0 时没有意义（那是止损单）。"""
    try:
        peak, pnl = float(row.get("peak_pnl")), float(row.get("pnl"))
    except (TypeError, ValueError):
        return "-"
    if peak <= 0:
        return "-"
    return f"{100 * pnl / peak:+.0f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default=None)
    ap.add_argument("--plan", default=None, help="只看某个打法（trend/range）")
    ap.add_argument("--factor", type=float, default=1.0,
                    help="账户结算系数：美元户 1，美分户 100")
    args = ap.parse_args(argv)

    account = str(args.login or paths.current_login())
    rows = _rows(account)
    if not rows:
        print(f"账户 {account} 没有流水")
        return 1

    opens: dict[int, dict] = {}
    holds: dict[int, list[dict]] = {}
    closes: list[dict] = []
    for r in rows:
        try:
            ticket = int(r.get("ticket") or 0)
        except (TypeError, ValueError):
            continue
        kind = r.get("kind")
        if kind == "open" and ticket:
            opens[ticket] = r
        elif kind == "hold" and ticket:
            holds.setdefault(ticket, []).append(r)
        elif kind == "close" and ticket:
            closes.append(r)

    _recalc(closes, opens, holds, args.factor)
    if args.plan:
        closes = [r for r in closes if r.get("plan") == args.plan]
    rows = [r for r in closes if r.get("atr_peak") is not None]
    if not rows:
        print(f"账户 {account} 没有可用样本（缺 atr 或 lots 的单子无法判定）")
        return 1

    print(f"账户 {account}  可判定 {len(rows)} 笔"
          f"（`*` = 从 hold 采样重算，其余是新单逐次采样记录）\n")
    print("{:<12}{:<7}{:<4}{:>7}{:>7}{:>7}{:>5}{:>7}{:>7}{:>8}{:>7}{:>3}".format(
        "ticket", "plan", "dir", "峰ATR", "t3分", "t5分", "脉冲", "持仓分", "峰",
        "落袋", "保持", ""))
    for r in sorted(rows, key=lambda x: x.get("ts") or 0):
        held = r.get("held_sec")
        print("{:<12}{:<7}{:<4}{:>7.2f}{:>7}{:>7}{:>5}{:>7}{:>7.1f}{:>8.2f}{:>7}{:>3}".format(
            str(r.get("ticket")), str(r.get("plan") or "-"),
            str(r.get("direction") or "-"),
            float(r.get("atr_peak") or 0),
            _fmt_min(r, "t3"), _fmt_min(r, "t5"),
            "是" if r.get("burst") else "否",
            f"{held / 60:.1f}" if held else "-",
            float(r.get("peak_pnl") or 0), float(r.get("pnl") or 0), _keep(r),
            "*" if r.get("_recalc") else ""))

    burst = [r for r in rows if r.get("burst")]
    big_known = [r for r in rows if not r.get("burst")
                 and (r.get("atr_peak") or 0) >= BURST_ATR and not r.get("t5_unknown")]
    big_unknown = [r for r in rows if not r.get("burst")
                   and (r.get("atr_peak") or 0) >= BURST_ATR and r.get("t5_unknown")]
    rest = [r for r in rows if not r.get("burst") and (r.get("atr_peak") or 0) < BURST_ATR]

    def _sum(tag: str, group: list[dict]) -> None:
        if not group:
            print(f"\n{tag}: 无样本")
            return
        pnls = [float(r.get("pnl") or 0) for r in group]
        keeps = [float(r.get("pnl") or 0) / float(r["peak_pnl"])
                 for r in group if (r.get("peak_pnl") or 0) > 0]
        print(f"\n{tag}: n={len(group)}  合计 {sum(pnls):+.2f}  "
              f"中位 {st.median(pnls):+.2f}  "
              f"胜率 {sum(1 for p in pnls if p > 0) / len(pnls):.0%}"
              + (f"  平均保持率 {st.mean(keeps):.0%}" if keeps else ""))

    print()
    _sum("脉冲（3 分内走满 5 ATR）", burst)
    _sum(f"大浮盈 ≥{BURST_ATR:g} ATR 且 t5 > {BURST_WIN_MIN:g} 分（可确认非脉冲）", big_known)
    _sum(f"大浮盈 ≥{BURST_ATR:g} ATR 但时点未知（不参与任何比较）", big_unknown)
    _sum("小浮盈（不参与判定）", rest)
    print("\n样本不足 30 笔时任何差异都可能是运气。判据同 tools/measure_burst.py。")
    if big_unknown:
        print("`?` = 峰值到过 5 ATR，但 60 秒采样没抓到穿越时点：这类历史单"
              "**分不清脉冲和慢磨**，只能等新单（逐次采样）回答。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
