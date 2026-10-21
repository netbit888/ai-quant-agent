"""持仓快照：记录持仓期间止损/止盈的变化。

为什么需要
----------
MT5 的 deal 只给结果：`[tp ...]` / `[sl ...]` / 无注释（手动平仓）。
**中间改过几次止损、有没有移到保本、有没有被自己调宽**，成交记录里完全看不到。

没有这段历史，"执行纪律"就只能评到「离场方式分布」这一层，
分不清「计划内主动离场」和「害怕了砍仓」。

做法
----
每次同步时对比当前持仓与上次记录的状态，**只在 SL/TP/手数变化或开平仓时**
追加一行到该账户目录下的 `position_snapshots.jsonl`。
不改就不写，避免每 15 秒一条把文件撑爆。

按账户隔离：每个账户有自己的 `position_state.json`，切账户时不会把
上一个账户的持仓误判成「已平仓」。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from live import paths

ROOT = Path(__file__).resolve().parent.parent


def state_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "position_state.json")


def snapshots_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "position_snapshots.jsonl")


def _load_state(login: int | str | None = None) -> dict:
    p = state_path(login)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, login: int | str | None = None) -> None:
    paths.write_json(paths.resolve_login(login), "position_state.json", state)


def _fingerprint(p: dict) -> dict:
    return {
        "sl": round(float(p.get("sl") or 0.0), 6),
        "tp": round(float(p.get("tp") or 0.0), 6),
        "volume": round(float(p.get("volume") or 0.0), 4),
    }


def record_positions(positions: list[dict], now: int | None = None,
                     login: int | str | None = None) -> int:
    """对比并记录变化。返回新写入的行数。"""
    now = int(now or time.time())
    state = _load_state(login)
    live: dict[str, dict] = {}
    rows: list[dict] = []

    for p in positions:
        key = str(p.get("position_id") or p.get("ticket"))
        fp = _fingerprint(p)
        prev = state.get(key)
        live[key] = {"fp": fp, "since": (prev or {}).get("since", now)}

        if prev is None:
            rows.append(
                {
                    "ts": now, "event": "open", "key": key,
                    "symbol": p.get("symbol"), "direction": p.get("direction"),
                    "volume": p.get("volume"), "price_open": p.get("price_open"),
                    "price_current": p.get("price_current"),
                    "sl": p.get("sl"), "tp": p.get("tp"),
                }
            )
        elif prev.get("fp") != fp:
            old, new = prev["fp"], fp
            if old.get("sl") != new.get("sl"):
                event = "sl_moved"
            elif old.get("tp") != new.get("tp"):
                event = "tp_moved"
            else:
                event = "volume_changed"
            rows.append(
                {
                    "ts": now, "event": event, "key": key,
                    "symbol": p.get("symbol"),
                    "sl_before": old.get("sl"), "sl_after": new.get("sl"),
                    "tp_before": old.get("tp"), "tp_after": new.get("tp"),
                    "volume_before": old.get("volume"), "volume_after": new.get("volume"),
                }
            )

    # 消失的持仓 = 已平仓
    for key, prev in state.items():
        if key not in live:
            rows.append({"ts": now, "event": "close", "key": key})

    if rows:
        path = snapshots_path(login)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        _save_state(live, login)

    return len(rows)


def load_snapshots(login: int | str | None = None) -> list[dict]:
    path = snapshots_path(login)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def sl_moves_by_position(login: int | str | None = None) -> dict[str, list[dict]]:
    """按仓位聚合止损变动记录，供评分使用。"""
    by_key: dict[str, list[dict]] = {}
    for r in load_snapshots(login):
        if r.get("event") in ("open", "sl_moved", "tp_moved", "volume_changed"):
            by_key.setdefault(str(r.get("key")), []).append(r)
    return by_key


def initial_plans(login: int | str | None = None) -> dict[str, dict]:
    """每个仓位**刚开仓时**的 SL/TP，用于算「计划盈亏比」。

    这是评分里唯一能区分「我计划赚多少」和「市场给了我多少」的数据来源。
    MT5 的成交记录里没有它，只能靠持仓快照补。
    """
    out: dict[str, dict] = {}
    for r in load_snapshots(login):
        if r.get("event") != "open":
            continue
        key = str(r.get("key"))
        if key in out:
            continue
        out[key] = {
            "sl": r.get("sl"),
            "tp": r.get("tp"),
            "volume": r.get("volume"),
            "price_open": r.get("price_open"),
            "symbol": r.get("symbol"),
            "ts": r.get("ts"),
        }
    return out


def summary(login: int | str | None = None) -> dict:
    rows = load_snapshots(login)
    return {
        "records": len(rows),
        "sl_moves": sum(1 for r in rows if r.get("event") == "sl_moved"),
        "plans": len(initial_plans(login)),
        "first_at": rows[0]["ts"] if rows else None,
        "note": "无记录时纪律分只能看离场方式分布" if not rows else None,
    }


if __name__ == "__main__":
    from live.journal import collect

    info = collect(force=True)
    if not info.get("ok"):
        print("读取失败:", info.get("error"))
        raise SystemExit(1)
    login = info.get("login")
    n = record_positions(info["positions"], login=login)
    print(f"账户 {login}：持仓 {len(info['positions'])} 个，新增快照 {n} 行")
    print(summary(login))
