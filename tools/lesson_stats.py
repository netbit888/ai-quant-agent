"""教训库的跨场次统计：这个错误到底还在不在犯？

为什么单独一个工具
------------------
`recent_lessons()` 只负责"把最反复的几条塞进提示词"，它不回答
"经验到底有没有用"。要回答这个，只能看**同一个主题在不同场次里的复发次数**：
首场犯 8 次、次场犯 3 次 = 在收敛；一直每场 6~8 次 = 提示词没压住。

口径（每个数字都能说清怎么来的）
--------------------------------
- 数据源：`live/autotrade.lesson_rows()`（`lessons.jsonl`，全量，不随流水归档）。
- 场次切分：按教训的 `closed_at` 排序，相邻两笔间隔 > `--gap` 分钟就断场。
  和 `live/sessions.py` 的 30 分钟断场同口径，只是这里作用在复盘行上。
- 主题 / 端：`row_theme()` / `row_scope()`——新数据用模型自己标的标签，
  老数据退回关键词推断（推断结果在输出里标 `*`）。

用法
----
    .venv/Scripts/python.exe -m tools.lesson_stats
    .venv/Scripts/python.exe -m tools.lesson_stats --login <login> --json
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter
from datetime import datetime

from live import autotrade, paths

GAP_MIN_DEFAULT = 30

# 平仓笔数少于这个数的场次，不做「每 10 笔」归一化：
# 1 笔里犯 1 次算成 10.0/10 笔，会把小样本读成灾难。
MIN_CLOSES_FOR_NORM = 5


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格：中文占 2 格，len() 补出来的表会歪。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - w)


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")


def _read_jsonl(p) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def closes_of(login: str | None) -> list[int]:
    """所有归档 + 当前流水里的平仓时刻。

    为什么要它：场 3 有 187 条教训、场 1 只有 21 条，但场 3 做了 192 笔、
    场 1 只做了 20 出头——直接比次数会把"场次更长"读成"错得更多"。
    """
    acc = paths.account_dir(paths.resolve_login(login))
    out: list[int] = []
    for f in sorted(acc.glob("autotrade_log*.jsonl")):
        out += [int(r.get("ts") or 0) for r in _read_jsonl(f)
                if r.get("kind") == "close"]
    return sorted(out)


def sessions_of(rows: list[dict], gap_min: int = GAP_MIN_DEFAULT) -> list[dict]:
    """按 closed_at 断场。返回 [{start, end, rows}]。"""
    keyed = sorted(rows, key=lambda r: int(r.get("closed_at") or r.get("ts") or 0))
    out: list[dict] = []
    cur: dict | None = None
    prev = None
    for r in keyed:
        t = int(r.get("closed_at") or r.get("ts") or 0)
        if cur is None or prev is None or (t - prev) > gap_min * 60:
            cur = {"start": t, "end": t, "rows": []}
            out.append(cur)
        cur["rows"].append(r)
        cur["end"] = max(cur["end"], t)
        prev = t
    return out


def _trend(series: list[float]) -> str:
    """前后半段均值比：<0.7 收敛、>1.3 恶化，其余持平。"""
    if len(series) < 2:
        return ""
    mid = max(1, len(series) // 2)
    head = sum(series[:mid]) / mid
    tail = sum(series[mid:]) / max(1, len(series) - mid)
    if head <= 0:
        return ""
    if tail < head * 0.7:
        return "收敛"
    if tail > head * 1.3:
        return "恶化"
    return "持平"


def build_report(login: str | None, gap_min: int = GAP_MIN_DEFAULT) -> dict:
    rows = autotrade.lesson_rows(login)
    if not rows:
        return {"login": login, "lessons": 0, "sessions": [], "themes": [],
                "note": "教训库是空的（lessons.jsonl 不存在或已清空）"}

    for r in rows:
        # 走 effective_theme：出场三兄弟要按方向拆（平太早/平太晚/平得对），
        # 否则报表会把三种相反的东西报成同一个主题，和注入口径对不上。
        r["_theme"] = autotrade.effective_theme(r) or "其他"
        r["_scope"] = autotrade.row_scope(r)
        r["_inferred"] = not (r.get("theme") in autotrade.LESSON_THEME_ENUM)
        r["_t"] = int(r.get("closed_at") or r.get("ts") or 0)

    sess = sessions_of(rows, gap_min)
    closes = closes_of(login)

    # 每笔平仓归给"距离最近"的场次，且只归一次：
    #   教训的 closed_at 和流水的 close ts 不一定严丝合缝（复盘可能延后跑、
    #   老数据可能缺 closed_at），按区间硬切会把边界上的笔数漏掉。
    for s in sess:
        s["closes"] = 0
    gap_sec = gap_min * 60
    for t in closes:
        best, best_d = None, gap_sec + 1
        for s in sess:
            if s["start"] <= t <= s["end"]:
                d = 0
            else:
                d = min(abs(t - s["start"]), abs(t - s["end"]))
            if d < best_d:
                best, best_d = s, d
        if best is not None:
            best["closes"] += 1

    # 先按场次统计每个 (主题, 端) 的次数，再决定序列的起止——
    # 只在"首次出现 → 最后一次出现"之间补 0，尾部不补：尾部 0 会让趋势看着像收敛。
    counts: dict[tuple, dict[int, int]] = {}
    meta: dict[tuple, dict] = {}
    for i, s in enumerate(sess):
        c = Counter((r["_theme"], r["_scope"]) for r in s["rows"])
        for key, n in c.items():
            counts.setdefault(key, {})[i] = n
            m = meta.setdefault(key, {"inferred": 0, "first": None, "last": None})
            m["first"] = s["start"] if m["first"] is None else min(m["first"], s["start"])
            m["last"] = s["end"] if m["last"] is None else max(m["last"], s["end"])
    for r in rows:
        key = (r["_theme"], r["_scope"])
        meta[key]["inferred"] += 1 if r["_inferred"] else 0

    out_themes = []
    for key, by_idx in counts.items():
        name, scope = key
        idxs = sorted(by_idx)
        span = list(range(idxs[0], idxs[-1] + 1))
        series = [by_idx.get(i, 0) for i in span]
        norm = [round(by_idx.get(i, 0) * 10 / sess[i]["closes"], 1)
                if sess[i]["closes"] >= MIN_CLOSES_FOR_NORM else None for i in span]
        usable = [x for x in norm if x is not None]
        use = usable if len(usable) >= 2 else series
        out_themes.append({
            "theme": name, "scope": scope, "count": sum(by_idx.values()),
            "sessions": len(idxs), "per_session": series, "per_10_closes": norm,
            "trend": _trend([float(x) for x in use]),
            "trend_basis": "每10笔" if len(usable) >= 2 else "原始次数",
            "inferred": meta[key]["inferred"],
            "first": _fmt(meta[key]["first"]), "last": _fmt(meta[key]["last"]),
        })
    out_themes.sort(key=lambda t: -t["count"])

    return {
        "login": paths.resolve_login(login),
        "lessons": len(rows),
        "sessions": [{"start": _fmt(s["start"]), "end": _fmt(s["end"]),
                      "n": len(s["rows"]), "closes": s["closes"]} for s in sess],
        "themes": out_themes,
        "gap_min": gap_min,
    }


def render(rep: dict) -> str:
    if not rep.get("lessons"):
        return rep.get("note") or "教训库是空的。"

    lines = [f"教训库 {rep['lessons']} 条 · {len(rep['sessions'])} 个场次"
             f"（断场 {rep['gap_min']} 分钟，按 closed_at）", ""]
    lines.append("场次（笔数取自流水里的 kind=close，含归档）：")
    for i, s in enumerate(rep["sessions"], 1):
        lines.append(f"  场 {i}: {s['start']} → {s['end']}  平仓 {s['closes']} 笔  "
                     f"教训 {s['n']} 条")

    lines.append("")
    lines.append("主题 × 端（* = 老数据靠关键词推断，不是模型自己标的）：")
    lines.append("  左边原始次数，右边按每 10 笔平仓归一化 —— 场次长短差 10 倍，"
                 "直接比次数会把「做得久」读成「错得多」")
    lines.append(f"  {_pad('主题', 20)}{_pad('端', 7)}{_pad('总', 4)}{_pad('场', 4)}"
                 f"{_pad('次数序列', 14)}{_pad('每10笔', 16)}趋势")
    for t in rep["themes"]:
        if t["count"] < 2 and t["sessions"] < 2:
            continue
        name = t["theme"] + ("*" if t["inferred"] else "")
        series = "/".join(str(x) for x in t["per_session"])
        norm = "/".join("—" if x is None else str(x) for x in t["per_10_closes"])
        lines.append(f"  {_pad(name, 20)}{_pad(t['scope'], 7)}"
                     f"{_pad(str(t['count']), 4)}{_pad(str(t['sessions']), 4)}"
                     f"{_pad(series, 14)}{_pad(norm, 16)}{t['trend'] or '—'}")

    singles = sum(1 for t in rep["themes"] if t["count"] == 1)
    lines.append("")
    lines.append(f"只出现一次的主题 {singles} 个（占 {singles / max(1, len(rep['themes'])):.0%}），"
                 f"共 {len(rep['themes'])} 个主题×端组合。")
    lines.append(f"读法：趋势按归一化序列算（<0.7 收敛、>1.3 恶化）；平仓不足 "
                 f"{MIN_CLOSES_FOR_NORM} 笔的场次不给归一化（标 —），"
                 f"场次不足 2 场时趋势标 —，这时任何「有用/没用」的结论都是外推。")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="教训库跨场次统计")
    ap.add_argument("--login", default=None, help="账户号，默认当前登录")
    ap.add_argument("--gap", type=int, default=GAP_MIN_DEFAULT, help="断场间隔（分钟）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    rep = build_report(args.login, args.gap)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))


if __name__ == "__main__":
    main()
