"""用 SQL 查账本：日志 / 成交 / 教训三个视图，一条 SQL 出结果。

为什么单独做一个工具
--------------------
今天为了回答三个问题（趋势单均值、M5 冲突分组、峰值回吐分布），写了三段
一次性 pandas。这类脚本用完就扔、下次又重写一遍，而且每个人算的口径可能不同。
SQL 把口径固定下来：`--preset` 是可以被复述、被复查的，临时脚本不行。

    python tools/query.py --list                     # 看有哪些预设
    python tools/query.py --preset plans             # 按打法
    python tools/query.py --preset giveback           # 峰值 vs 实际（赚到又还回去）
    python tools/query.py --preset conflict           # M5 同向/反向的结果
    python tools/query.py --sql "SELECT source, count(*) FROM log WHERE kind='close' GROUP BY 1"
    python tools/query.py --preset plans --csv out.csv

视图（表名固定）：
    log      autotrade_log.jsonl     决策流水（含 feat.* 嵌套字段）
    deals    deals.jsonl             券商成交快照（没这个文件就没有这个视图）
    lessons  lessons.jsonl           教训库（同上）

**只读**：连的是内存库，`CREATE VIEW` 指向文件，不写回、不建 .db 文件。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live import paths  # noqa: E402

# 预设查询。每条都对应一个"今天真的问过"的问题，不是凑数的。
PRESETS: dict[str, tuple[str, str]] = {
    "plans": ("按打法：笔数 / 均值 / 合计 / 胜率 / 平均峰值", """
        SELECT plan,
               count(*)                          AS n,
               round(avg(pnl), 2)                AS avg_pnl,
               round(sum(pnl), 2)                AS total,
               round(100.0 * avg((pnl > 0)::int), 0) AS win_pct,
               round(avg(peak_pnl), 2)           AS avg_peak
        FROM log WHERE kind = 'close' AND plan IS NOT NULL
        GROUP BY plan ORDER BY total DESC
    """),
    "sources": ("按平仓来源：哪条出场规则在起作用", """
        SELECT source, count(*) AS n,
               round(sum(pnl), 2) AS total,
               round(avg(pnl), 2) AS avg_pnl
        FROM log WHERE kind = 'close'
        GROUP BY source ORDER BY n DESC
    """),
    "giveback": ("峰值回吐：赚到过又还回去多少（按回吐排序）", """
        SELECT ticket, plan, source,
               round(peak_pnl, 2)          AS peak,
               round(pnl, 2)               AS pnl,
               round(peak_pnl - pnl, 2)    AS giveback
        FROM log WHERE kind = 'close'
        ORDER BY giveback DESC NULLS LAST
    """),
    "conflict": ("跨周期冲突：M5 与这笔方向相反 vs 同向，结果怎么分", """
        WITH o AS (
            SELECT ticket, direction, feat.mtf.drift AS m5
            FROM log WHERE kind = 'open'
        ), c AS (
            SELECT ticket, plan, pnl, peak_pnl FROM log WHERE kind = 'close'
        )
        SELECT c.plan,
               CASE WHEN abs(o.m5) >= 5 AND o.direction * o.m5 < 0
                    THEN '冲突' ELSE '同向' END AS mtf,
               count(*)                  AS n,
               round(sum(c.pnl), 2)      AS total,
               round(avg(c.pnl), 2)      AS avg_pnl,
               round(avg(c.peak_pnl), 2) AS avg_peak
        FROM o JOIN c USING (ticket)
        GROUP BY 1, 2 ORDER BY 1, 2
    """),
    "skips": ("模型说不做的原因 Top（只看前 24 字）", """
        SELECT substr(reason, 1, 24) AS why, count(*) AS n
        FROM log WHERE kind = 'skip'
        GROUP BY why ORDER BY n DESC LIMIT 12
    """),
    "be": ("保本止损：触发时的峰值分布", """
        SELECT ticket, round(peak_pnl, 2) AS peak, mode, round(sl, 3) AS sl
        FROM log WHERE kind = 'be'
        ORDER BY peak DESC
    """),
}


def _sql_str(path: Path) -> str:
    """把路径塞进 SQL 字符串字面量：反斜杠转正斜杠、单引号翻倍。"""
    text = str(path).replace("\\", "/").replace("'", "''")
    return f"'{text}'"


def connect(account: str | None = None):
    """建三个视图。缺哪个文件就跳过哪个视图，不报错——老账户可能没有 deals。"""
    try:
        import duckdb
    except ImportError:  # pragma: no cover - 只在没装依赖时走到
        raise SystemExit(
            "没装 duckdb：`.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`")

    login = account or paths.resolve_login(None)
    log = paths.account_file(login, "autotrade_log.jsonl")
    if not log.exists() or log.stat().st_size == 0:
        raise SystemExit(f"没有流水文件（或为空）：{log}")

    con = duckdb.connect()          # 内存库，不留文件
    con.execute(f"CREATE VIEW log AS SELECT * FROM read_json_auto({_sql_str(log)})")
    for name in ("deals", "lessons"):
        p = paths.account_file(login, f"{name}.jsonl")
        if p.exists() and p.stat().st_size > 0:
            con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_json_auto({_sql_str(p)})")
    return con


def run(con, sql: str) -> tuple[list[str], list[tuple]]:
    """执行并取回结果。列名单独返回，测试和 CSV 都要用。"""
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, cur.fetchall()


def _render(cols: list[str], rows: list[tuple]) -> str:
    """对齐输出。中文按显示宽度算（一个汉字占两格），否则表格会歪。"""
    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x1100 else 1 for ch in text)

    def pad(text: str, want: int) -> str:
        return text + " " * max(0, want - width(text))

    head = [str(c) for c in cols]
    body = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [max([width(h)] + [width(r[i]) for r in body]) for i, h in enumerate(head)]
    lines = ["  ".join(pad(h, w) for h, w in zip(head, widths)),
             "-" * (sum(widths) + 2 * (len(head) - 1))]
    lines += ["  ".join(pad(v, w) for v, w in zip(r, widths)) for r in body]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用 SQL 查 AI 做单账本（只读）")
    ap.add_argument("--account", help="账户号，默认当前登录账户")
    ap.add_argument("--preset", choices=sorted(PRESETS), help="预设查询")
    ap.add_argument("--sql", help="自定义 SQL（表名 log / deals / lessons）")
    ap.add_argument("--list", action="store_true", help="列出预设")
    ap.add_argument("--csv", metavar="PATH", help="结果导出 CSV")
    args = ap.parse_args(argv)

    if args.list or not (args.preset or args.sql):
        print("可用预设：")
        for name, (desc, _) in sorted(PRESETS.items()):
            print(f"  {name:<10} {desc}")
        print("\n自定义：--sql \"SELECT ... FROM log\"（表：log / deals / lessons）")
        return 0

    sql = args.sql or PRESETS[args.preset][1]
    con = connect(args.account)
    cols, rows = run(con, sql)
    if not rows:
        print("没有匹配的行。")
        return 0

    if args.csv:
        dest = Path(args.csv)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            writer.writerows(rows)
        print(f"{len(rows)} 行 → {dest}")
        return 0

    print(_render(cols, rows))
    print(f"\n{len(rows)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
