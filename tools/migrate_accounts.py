"""把旧版「单账户」账本拆成「每账户一套」。

为什么要这个工具
----------------
在加多账户隔离之前，所有成交都写在 `data/live/deals.jsonl` 一个文件里，
而且**记录里不带账户号**。一旦在 MT5 终端里切了账户，新账户的成交会直接
追加到旧账户的账本上——净额、胜率、回撤、入金全都算错，而且事后无法自动拆分。

拆分规则（唯一可靠的办法）
--------------------------
1. 连 MT5，拿到**当前登录账户**能读到的全部 ticket。
2. 旧账本里 ticket 在这个集合里的 → 属于当前账户。
3. 其余全部 → 属于 `--other` 指定的历史账户。
4. 每条记录打上 `login` 字段，写进各自目录。以后再也不会混。

用法
----
    python -m tools.migrate_accounts --other <login>            # 自动识别当前账户
    python -m tools.migrate_accounts --current <login> --other <旧账户>
    python -m tools.migrate_accounts --dry-run                  # 只看会做什么

幂等：根目录旧文件处理后改名 `.legacy`，重复执行不会重复导入。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from live import paths

# 旧版根目录文件 → 归属。"current"= 终端当前账户，"other" = 历史账户
ROOT = Path(__file__).resolve().parent.parent

DEALS = "deals.jsonl"
DERIVED = {
    "advice.json": "other",
    "api_calls.jsonl": "other",
    "position_state.json": "other",
    "position_snapshots.jsonl": "other",
    "quality_daily.json": "other",
    "daily.json": "other",
    "account.json": "current",
    "account_daily.json": "current",
}


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def detect_current() -> int | None:
    try:
        import MetaTrader5 as mt5

        if not mt5.initialize(timeout=30_000):
            return None
        ai = mt5.account_info()
        mt5.shutdown()
        return int(ai.login) if ai else None
    except Exception:  # noqa: BLE001
        return None


def current_tickets(days: int = 365) -> set[int]:
    try:
        import MetaTrader5 as mt5
        from datetime import timedelta

        if not mt5.initialize(timeout=30_000):
            return set()
        now = datetime.now(timezone.utc)
        raw = mt5.history_deals_get(now - timedelta(days=days), now)
        mt5.shutdown()
        return {int(d.ticket) for d in (raw or [])}
    except Exception:  # noqa: BLE001
        return set()


def load_legacy_deals() -> list[dict]:
    p = paths.LIVE_DIR / DEALS
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def rebuild_daily(login: str, trades: list[dict], deposits: float) -> None:
    """从该账户自己的成交重算 daily.json（旧的那份是两个账户混在一起算的）。"""
    by_day: dict[str, dict] = {}
    for t in trades:
        if not t.get("exit_time"):
            continue
        day = datetime.fromtimestamp(t["exit_time"], timezone.utc).strftime("%Y-%m-%d")
        row = by_day.setdefault(day, {"date": day, "trades": 0, "wins": 0, "profit": 0.0, "volume": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if (t.get("profit") or 0) > 0 else 0
        row["profit"] = round(row["profit"] + (t.get("profit") or 0), 2)
        row["volume"] = round(row["volume"] + (t.get("volume") or 0), 4)
    paths.write_json(login, "daily.json", {
        "updated_at": int(datetime.now(timezone.utc).timestamp()),
        "login": login,
        "deposits": round(deposits, 2),
        "days": [by_day[k] for k in sorted(by_day)],
    })


def main() -> None:
    ap = argparse.ArgumentParser(description="拆分旧版单账户账本为多账户账本")
    ap.add_argument("--current", type=int, default=None, help="终端当前登录的账户号（默认自动检测）")
    ap.add_argument("--other", type=int, default=None, help="历史账户号（旧成交归它）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    legacy = load_legacy_deals()
    if not legacy:
        print("根目录没有旧的 deals.jsonl，无需迁移。")
        for a in paths.list_accounts():
            print(f"  已有账户 {a['login']} @ {a.get('server')}")
        return

    cur = args.current or detect_current()
    if not cur:
        print("检测不到 MT5 当前账户，请用 --current 指定。")
        raise SystemExit(2)

    tickets = current_tickets()
    if not tickets:
        print("读不到当前账户的成交，无法区分归属。请用 --current 并确保 MT5 已登录。")
        raise SystemExit(2)

    mine = [d for d in legacy if int(d["ticket"]) in tickets]
    others = [d for d in legacy if int(d["ticket"]) not in tickets]

    print(f"旧账本 {len(legacy)} 条 deal")
    print(f"  当前账户 {cur}: {len(mine)} 条")
    print(f"  历史账户 {args.other or '?'}: {len(others)} 条")
    if others and not args.other:
        print("有历史成交但未指定 --other，无法确定归属。请加 --other <旧账户号>。")
        raise SystemExit(2)
    if args.dry_run:
        print("（--dry-run，未写入）")
        return

    # 打标写入各自账本
    for login, rows in (((str(cur)), mine), (str(args.other) if args.other else None, others)):
        if not login or not rows:
            continue
        stamped = [{**d, "login": int(login)} for d in rows]
        path = paths.account_file(login, DEALS)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for d in sorted(stamped, key=lambda x: x["time"]):
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        print(f"  写入 {path}  {len(stamped)} 条")

    # 派生文件：按归属搬过去；daily.json 必须重算，旧的是混着算的
    for name, owner in DERIVED.items():
        src = paths.LIVE_DIR / name
        if not src.exists():
            continue
        login = str(cur if owner == "current" else args.other)
        if owner == "other" and not args.other:
            continue
        dst = paths.account_file(login, name)
        if dst.exists():
            src.rename(src.with_suffix(src.suffix + ".legacy"))
        else:
            shutil.move(str(src), str(dst))
        print(f"  搬移 {name} → accounts/{login}/")

    # 重算两边的 daily.json
    for login in filter(None, [str(cur), str(args.other) if args.other else None]):
        from live.journal import build_trades, load_ledger

        ledger = load_ledger(login)
        trades = [t.__dict__ for t in build_trades(list(ledger.values()))]
        dep = round(sum(d["profit"] for d in ledger.values() if d["type"] == 2), 2)
        rebuild_daily(login, trades, dep)
        print(f"  重算 accounts/{login}/daily.json  {len(trades)} 笔交易")

    # 建索引
    for login in filter(None, [str(cur), str(args.other) if args.other else None]):
        acc = paths.read_json(login, "account.json", {}) or {}
        if acc.get("login"):
            paths.touch_account(acc)

    # 根目录旧成交改名留档
    src = paths.LIVE_DIR / DEALS
    if src.exists():
        src.rename(src.with_suffix(src.suffix + ".legacy"))
        print(f"  旧 {DEALS} 改名 {DEALS}.legacy 留档")

    print("\n迁移完成：")
    for a in paths.list_accounts():
        print(f"  {a['login']} @ {a.get('server')}  余额 {a.get('last_balance')}")


if __name__ == "__main__":
    main()
