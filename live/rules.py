"""下一场规则：把复盘结论变成「下一场能自动检查的一条」 。

为什么只留一条
--------------
一次复盘给五条建议，等于一条都没给。人只会执行一件新增的事，
所以每场只产出一条，下一场自动检查「遵守了没有」。

为什么规则要带 metric
---------------------
LLM 的自由文本没法自动验证。所以要求它从**固定指标表**里挑一个，
给出阈值；这样下一场能用同一口径重算，直接显示「遵守 / 违反」。
数量指标一律「≤」，质量指标一律「≥」，方向由指标表决定，不让模型乱选。

规则只对**产生它的那一场之后**的场次生效，否则会拿未来的数据回头判历史。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from live import paths

# 指标表：能稳定算出来的，才允许进规则。
# op 写死方向，避免 LLM 给出「持仓中位数 ≤ 5 分钟」这种反向规则。
METRICS: dict[str, dict] = {
    "trades": {"label": "本场笔数", "unit": "笔", "op": "<="},
    "max_same_direction_streak": {"label": "同向连做最多笔数", "unit": "笔", "op": "<="},
    "trades_under_60s": {"label": "持仓 <1 分钟笔数", "unit": "笔", "op": "<="},
    "share_under_60s": {"label": "持仓 <1 分钟占比", "unit": "%", "op": "<="},
    "max_risk_pct": {"label": "最大单笔风险", "unit": "% 余额", "op": "<="},
    "manual_share": {"label": "手动平仓占比", "unit": "%", "op": "<="},
    "sl_width_ratio": {"label": "止损宽度最大/最小", "unit": "倍", "op": "<="},
    "median_hold_min": {"label": "持仓时长中位数", "unit": "分", "op": ">="},
}


def rule_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "rules.json")


def load(login: int | str | None = None) -> dict:
    p = rule_path(login)
    if not p.exists():
        return {"rule": None, "history": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("rule", None)
            data.setdefault("history", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"rule": None, "history": []}


def _save(data: dict, login: int | str | None = None) -> None:
    paths.write_json(paths.resolve_login(login), "rules.json", data)


def validate(raw: dict | None) -> dict | None:
    """把 LLM 给的规则规整成可检验的形式。不合格返回 None（不猜）。"""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    metric = str(raw.get("metric") or "").strip()
    if metric not in METRICS:
        return None
    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        return None
    spec = METRICS[metric]
    return {
        "text": text[:120],
        "metric": metric,
        "op": spec["op"],          # 方向由指标表决定，不采信模型给的方向
        "value": round(value, 2),
        "label": spec["label"],
        "unit": spec["unit"],
    }


def save(rule: dict, after_ts: int, session_id: str | None = None,
         login: int | str | None = None) -> dict:
    """存一条新规则。`after_ts` 之前的场次不受它约束。"""
    data = load(login)
    entry = {
        **rule,
        "after_ts": int(after_ts),
        "from_session": session_id,
        "created_at": int(time.time()),
    }
    history = data.get("history") or []
    history.append(entry)
    _save({"rule": entry, "history": history[-20:]}, login)
    return entry


def clear(login: int | str | None = None) -> None:
    data = load(login)
    _save({"rule": None, "history": data.get("history") or []}, login)


def applies_to(rule: dict | None, session: dict) -> bool:
    """规则只约束它之后开始的场次。"""
    if not rule:
        return False
    return int(session.get("start") or 0) > int(rule.get("after_ts") or 0)


def check(rule: dict | None, session: dict) -> dict | None:
    """按规则检查一场，返回 {pass, value, target, text}；不适用返回 None。"""
    if not applies_to(rule, session):
        return None
    value = (session.get("metrics") or {}).get(rule.get("metric"))
    if value is None:
        return {
            "status": "unknown",
            "value": None,
            "text": rule.get("text"),
            "label": rule.get("label"),
            "target": f"{rule.get('op')} {rule.get('value')}",
        }
    ok = value <= rule["value"] if rule.get("op") == "<=" else value >= rule["value"]
    return {
        "status": "pass" if ok else "fail",
        "value": value,
        "text": rule.get("text"),
        "label": rule.get("label"),
        "unit": rule.get("unit"),
        "target": f"{rule.get('op')} {rule.get('value')}",
    }


def summarize(rule: dict | None, sessions: list[dict]) -> dict | None:
    """规则在**最近一场**上的执行情况，供面板顶部显示。"""
    if not rule:
        return None
    checked = [s for s in sessions if applies_to(rule, s)]
    if not checked:
        return {
            "status": "pending",
            "text": rule.get("text"),
            "label": rule.get("label"),
            "target": f"{rule.get('op')} {rule.get('value')}",
            "note": "还没有完成它之后的任何一场",
        }
    out = check(rule, checked[0])
    if out:
        out["session_id"] = checked[0]["id"]
    return out
