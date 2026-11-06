"""账本浏览器：把 `autotrade_log.jsonl` 摊平成一张可排序/筛选/透视的表。

为什么需要它
------------
决策流水是**嵌套 JSON**（`feat` 里还有 `shape`/`mtf`/`ma28`），用 pandas 临时
读一次就得写一串 `json_normalize`；想换个维度看又得重写一遍。这个脚本把
"读日志 → 摊平 → 过滤"固定下来，剩下的交给 dtale 的图形界面。

dtale 是**可选**的：没装就退化成终端表格 / CSV，不阻塞任何分析——它只是
省掉反复写 pandas 的时间，不是运行依赖（见 requirements-dev.txt）。

用法
----
    python tools/inspect_log.py                      # 当前账户，全部行，开界面
    python tools/inspect_log.py --kind close         # 只看平仓行
    python tools/inspect_log.py --ticket 4925197542  # 单笔全流程
    python tools/inspect_log.py --limit 200          # 只读尾部 200 行（大文件必用）
    python tools/inspect_log.py --no-gui             # 终端打表，不启服务
    python tools/inspect_log.py --csv out.csv        # 导出，不需要 dtale

它**只读**日志，不碰 state、不下单、不改任何交易数据。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live import autotrade, paths  # noqa: E402

# 这些列放在最前面：摊平后列会很多，先看到最常看的那几个。
HEAD_COLS = ("time", "kind", "ticket", "direction", "pnl", "peak_pnl",
             "plan", "source", "reason")


def _flatten(row: dict, prefix: str = "") -> dict:
    """把嵌套 dict 摊成 `a_b_c` 这样的扁平键，供 DataFrame / CSV 使用。

    只摊一层嵌套不够：`feat.mtf.drift` 是两层，而"按 M5 drift 分组看结果"
    正是最想做的分析之一。列表（目前只有 `recent` 这种）保留成 JSON 字符串，
    展开成列会让列数爆炸且几乎没人用。
    """
    out: dict = {}
    for key, val in row.items():
        name = f"{prefix}{key}"
        if isinstance(val, dict):
            out.update(_flatten(val, f"{name}_"))
        elif isinstance(val, (list, tuple)):
            out[name] = json.dumps(val, ensure_ascii=False) if val else ""
        else:
            out[name] = val
    return out


def _fmt_time(ts) -> str:
    """日志里的 ts 是 UTC 秒；面板和人的直觉都是本地时间（UTC+8）。"""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def load_rows(account: str | None = None, limit: int = 0,
              kind: str | None = None, ticket: int | None = None) -> list[dict]:
    """读流水并过滤。`limit` 走尾部倒读——流水只增不减，全量读会很慢。"""
    login = account or paths.resolve_login(None)
    p = paths.account_file(login, "autotrade_log.jsonl")
    if not p.exists():
        raise SystemExit(f"没有流水文件：{p}\n（账户 {login} 还没跑过？）")
    lines = (autotrade._tail_lines(p, limit) if limit
             else p.read_text(encoding="utf-8").splitlines())
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue          # 进程被杀时写了一半的行，跳过
        if kind and r.get("kind") != kind:
            continue
        if ticket is not None and int(r.get("ticket") or 0) != ticket:
            continue
        rows.append(r)
    return rows


def to_records(rows: list[dict]) -> list[dict]:
    """摊平 + 补一列本地时间，并把常看的列排到前面。

    **列顺序按全体行的并集算，不按第一行**：`open` 行没有 `pnl`/`exit_price`，
    `close` 行没有 `p_up` 以外的进场字段，按第一行裁剪会把另一半字段整批丢掉
    （第一次跑就踩了：导出的 CSV 里没有 pnl 列）。
    """
    flat_rows = []
    for r in rows:
        flat = _flatten(r)
        flat["time"] = _fmt_time(flat.get("ts"))
        flat_rows.append(flat)
    if not flat_rows:
        return []
    order: list[str] = [c for c in HEAD_COLS if any(c in r for r in flat_rows)]
    for r in flat_rows:
        for key in r:
            if key not in order:
                order.append(key)
    return [{c: r.get(c) for c in order} for r in flat_rows]


def _cell(val) -> str:
    """单元格文本。0 / 0.0 是**合法值**，不能像 `or ''` 那样被吞掉——
    `pnl=0.00` 是保本止损的正常结果（2026-11-06 刚修过同一个坑）。"""
    if val is None:
        return ""
    return str(val)


def _print_table(records: list[dict], cols: tuple[str, ...] | None = None) -> None:
    """没有 pandas/dtale 时的兜底：终端打一张对齐的表。

    只挑几列——终端里塞 40 列没法看，想全看就导 CSV 或装 dtale。
    """
    cols = cols or tuple(c for c in HEAD_COLS if c in records[0])
    widths = {c: max(len(str(c)), *(len(_cell(r.get(c))) for r in records))
              for c in cols}
    line = "  ".join(str(c).ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in records:
        print("  ".join(_cell(r.get(c))[:widths[c]].ljust(widths[c])
                        for c in cols))


def _write_csv(records: list[dict], dest: Path) -> None:
    """导出 CSV。列取**所有行的并集**，不是第一行的键：
    第一行往往是 open 行，没有 `exit_price` / `pnl_src` / `peak_pnl`——
    按它定表头会把 close 行的字段整批丢掉（第一次跑就踩到了）。"""
    fields: list[str] = []
    for r in records:
        for key in r:
            if key not in fields:
                fields.append(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore",
                                restval="")
        writer.writeheader()
        writer.writerows(records)


def _show_dtale(records: list[dict], port: int, open_browser: bool) -> None:
    """交给 dtale。服务起在本机 127.0.0.1，**不要点 Share** 把它暴露出去。"""
    import pandas as pd          # 延迟导入：没装也不影响其他路径
    import dtale

    df = pd.DataFrame(records)
    inst = dtale.show(df, port=port, open_browser=open_browser,
                      host="127.0.0.1", reaper_on=False)
    print(f"\n{len(df)} 行 × {len(df.columns)} 列")
    print(f"界面：{getattr(inst, '_url', '') or '已尝试打开浏览器'}")
    print("Ctrl+C 结束（关掉终端等于关掉服务，不改任何数据）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="查看 AI 做单流水（只读）")
    ap.add_argument("--account", help="账户号，默认当前登录账户")
    ap.add_argument("--kind", help="只看某一类行：open / close / hold / skip / be")
    ap.add_argument("--ticket", type=int, help="只看某一笔单的全部行")
    ap.add_argument("--limit", type=int, default=0,
                    help="只读尾部 N 行（0=全量；大文件建议 200~2000）")
    ap.add_argument("--csv", metavar="PATH", help="导出 CSV 后退出")
    ap.add_argument("--no-gui", action="store_true", help="终端打表，不启 dtale")
    ap.add_argument("--port", type=int, default=40000, help="dtale 端口")
    ap.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    args = ap.parse_args(argv)

    rows = load_rows(args.account, args.limit, args.kind, args.ticket)
    if not rows:
        print("没有匹配的行。试试放宽 --kind / --ticket，或加 --limit 0")
        return 0
    records = to_records(rows)

    if args.csv:
        dest = Path(args.csv)
        _write_csv(records, dest)
        print(f"{len(records)} 行 → {dest}")
        return 0

    if args.no_gui:
        _print_table(records)
        print(f"\n共 {len(records)} 行（--no-gui 只显示常用列；全字段用 --csv）")
        return 0

    try:
        _show_dtale(records, args.port, not args.no_browser)
    except ImportError:
        print("没装 dtale，先退回终端表格。安装：\n"
              "  .venv/Scripts/python.exe -m pip install -r requirements-dev.txt\n")
        _print_table(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
