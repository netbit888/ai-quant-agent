"""AI 复盘：把实盘统计喂给 LLM，产出可执行的改进建议。

原则
----
1. **规则先行，LLM 补充**。`live.stats.risk_flags()` 的硬阈值判断每次都跑，
   不依赖 API 可用性；LLM 只负责解释"为什么"和"下一步做什么"。
2. **缓存**。相同交易集合不重复调用，避免每 15 秒烧一次 token。
   缓存键 = 已平仓笔数 + 最后一笔平仓时间。
3. **只读诊断**。本模块不下单、不改参数。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from live import paths  # noqa: E402


def advice_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "advice.json")


def call_log_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "api_calls.jsonl")

SYSTEM = """你是一名严格的交易绩效分析师，服务对象是一位零售交易者。
你的任务不是安慰他，而是根据数据指出问题并给出可执行的修改。

要求：
1. 所有结论必须引用给出的具体数字，不许泛泛而谈。
2. 区分「策略问题」和「资金管理问题」。仓位过大、爆仓属于后者。
3. 建议必须具体到可执行（例如"把单笔风险降到账户的 1%"），
   不要说"加强风控"这种空话。
4. 如果数据显示样本太少（<30 笔），必须明确说明结论不可靠。
5. 禁止任何形式的收益承诺或鼓励加大投入。
6. 最大回撤按「账户余额曲线」计算，分母是当时的峰值余额；出金不算亏损，
   穿仓被券商抹平到 0 算一次「清零」。不要把出金说成爆仓。
7. 用中文输出。"""

PROMPT = """以下是一个真实账户的交易统计，请做复盘诊断。

## 账户
{account}

## 总体统计
{overall}

## 分方向
{directions}

## 分平仓原因
{reasons}

## 分时段（按开仓时间的 UTC 小时）
{hours}

## 分星期
{weekdays}

## 逐日盈亏
{daily}

## 最近 20 笔交易
{recent}

## 规则引擎已标记的风险
{flags}

## 规则引擎给出的评级（你要在解释里对齐这些数字，不要另起一套结论）
{scoring}

## 输出格式（严格遵守 JSON，不要加任何解释文字）
{{
  "headline": "一句话结论，30 字以内，直接点出最致命的问题",
  "verdict": "整体评级，只能是 严重亏损 / 亏损 / 勉强打平 / 微利 / 盈利 之一",
  "problems": [
    {{"severity": "高|中|低", "title": "问题标题", "evidence": "引用具体数字", "root_cause": "机制层面的原因"}}
  ],
  "actions": [
    {{"priority": 1, "action": "具体动作", "why": "为什么这条能解决问题", "how_to_verify": "怎么验证改完有效"}}
  ],
  "note": "关于样本量和结论可靠性的说明"
}}"""


def _fmt_money(v) -> str:
    return f"{v:+.2f}" if isinstance(v, (int, float)) else str(v)


def build_prompt(stats: dict, flags: list[dict], trades: list[dict], scoring: dict | None = None) -> str:
    acct = stats.get("account") or {}
    account = (
        f"账户 {acct.get('login', '?')} @ {acct.get('server', '?')}，"
        f"类型 {acct.get('trade_mode', '?')}，杠杆 1:{acct.get('leverage', '?')}\n"
        f"余额 {acct.get('balance')} {acct.get('currency')}，净值 {acct.get('equity')}，"
        f"可用保证金 {acct.get('margin_free')}\n"
        f"累计入金 {_fmt_money(stats.get('deposits'))}，当前余额占入金 "
        f"{(acct.get('balance') or 0) / (stats.get('deposits') or 1) * 100:.1f}%"
    )

    overall = "\n".join(
        [
            f"已平仓 {stats.get('count')} 笔（未平仓 {stats.get('open_count', 0)} 笔），"
            f"跨 {stats.get('span_days')} 天，实际有交易 {stats.get('active_days')} 天，"
            f"日均 {stats.get('trades_per_day')} 笔",
            f"净盈亏 {_fmt_money(stats.get('net'))}，相对入金 {stats.get('return_pct')}%",
            f"胜率 {stats.get('win_rate')}%（{stats.get('wins')} 胜 / {stats.get('losses')} 负）",
            f"盈利因子 PF {stats.get('pf')}",
            f"平均盈利 {_fmt_money(stats.get('avg_win'))}，平均亏损 {_fmt_money(stats.get('avg_loss'))}，"
            f"盈亏比 {stats.get('risk_reward')}",
            f"单笔期望 {_fmt_money(stats.get('expectancy'))}（{stats.get('expectancy_r')} R）",
            f"最大回撤 {stats.get('max_dd')}（从峰值余额 {stats.get('dd_peak')} 跌到 "
            f"{stats.get('trough_balance')}，占峰值 {stats.get('max_dd_pct')}%）",
            f"历史峰值余额 {stats.get('peak_balance')}，账户被清零 {stats.get('wipeouts')} 次",
            f"最长连胜 {stats.get('max_win_streak')} / 最长连亏 {stats.get('max_loss_streak')}",
            f"持仓时长中位数 {stats.get('hold_median_min')} 分钟，最长 {stats.get('hold_max_min')} 分钟",
        ]
    )

    def table(rows: list[dict], key_name: str) -> str:
        if not rows:
            return "（无）"
        lines = [f"{key_name} | 笔数 | 胜率 | 净盈亏 | PF"]
        for r in rows:
            lines.append(f"{r['key']} | {r['trades']} | {r['win_rate']}% | {_fmt_money(r['net'])} | {r['pf']}")
        return "\n".join(lines)

    daily_rows = stats.get("daily") or []
    daily_txt = "\n".join(
        f"{r['date']} | {r['trades']} 笔 | 胜率 {r['win_rate']}% | {_fmt_money(r['profit'])}"
        for r in daily_rows[-15:]
    ) or "（无）"

    recent = [t for t in trades if t.get("exit_time")][-20:]
    recent_txt = "\n".join(
        f"{datetime.fromtimestamp(t['exit_time'], timezone.utc).strftime('%m-%d %H:%M')} | "
        f"{t['symbol']} | {'多' if t['direction'] > 0 else '空'} | {t['volume']}手 | "
        f"{_fmt_money(t['profit'])} | {t.get('close_reason')} | "
        f"持仓 {(t['exit_time'] - t['entry_time']) / 60:.0f} 分钟"
        for t in recent
    ) or "（无）"

    flag_txt = "\n".join(f"[{f['level']}] {f['title']}：{f['detail']}" for f in flags) or "（无）"

    if scoring and scoring.get("dimensions"):
        lines = [
            f"综合等级 {scoring['grade']}（整体取最差维度）  参考分 {scoring.get('score')}",
            f"单笔均分 {scoring.get('avg_trade_score')}",
        ]
        for d in scoring["dimensions"]:
            lines.append(f"  {d['label']}: {d['display']} → {d['grade']}（目标 {d['target']}）")
            if d.get("note"):
                lines.append(f"      {d['note']}")
        be = scoring.get("breakeven") or {}
        if be.get("required_win_rate") is not None:
            lines.append(
                f"盈亏平衡线: 盈亏比 {be.get('rr')} → 需要胜率 {be['required_win_rate']}%，"
                f"实际 {be.get('actual_win_rate')}%，边际 {be.get('margin_pp')} 个百分点"
            )
        plan = scoring.get("plan") or {}
        if plan.get("avg_planned_rr"):
            lines.append(
                f"计划盈亏比均值 {plan['avg_planned_rr']}，实际实现 {plan.get('realized_rr')}"
                "（差得越多说明越没守住计划）"
            )
        elif plan.get("note"):
            lines.append(f"计划盈亏比: {plan['note']}")
        for f in scoring.get("red_flags") or []:
            lines.append(f"  红旗: {f}")
        scoring_txt = "\n".join(lines)
    else:
        scoring_txt = "（本次没有评分数据）"

    return PROMPT.format(
        account=account,
        overall=overall,
        directions=table(stats.get("by_direction") or [], "方向"),
        reasons=table(stats.get("by_reason") or [], "平仓原因"),
        hours=table(stats.get("by_hour") or [], "小时"),
        weekdays=table(stats.get("by_weekday") or [], "星期"),
        daily=daily_txt,
        recent=recent_txt,
        flags=flag_txt,
        scoring=scoring_txt,
    )


def _cache_key(stats: dict, trades: list[dict]) -> str:
    closed = [t for t in trades if t.get("exit_time")]
    last = max((t["exit_time"] for t in closed), default=0)
    return f"{len(closed)}:{last}"


def load_cached(login: int | str | None = None) -> dict | None:
    p = advice_path(login)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _log_call(ok: bool, usage: dict | None, error: str | None = None,
              login: int | str | None = None) -> None:
    """每调一次外部 API 就记一行，方便事后核对花了多少钱。"""
    p = call_log_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": int(time.time()),
        "provider": "deepseek",
        "purpose": "live_advice",
        "login": paths.resolve_login(login),
        "ok": ok,
        "usage": usage or {},
    }
    if error:
        row["error"] = error
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def advise(
    stats: dict,
    flags: list[dict],
    trades: list[dict],
    force: bool = False,
    allow_call: bool = False,
    scoring: dict | None = None,
    login: int | str | None = None,
) -> dict:
    """返回 AI 复盘结果。失败时返回带 error 的字典，不抛异常——面板不能因此挂掉。

    allow_call=False 时**绝不发起外部请求**：缓存命中就返回缓存，没命中就返回
    `need_confirm=True`。这是硬约束——GET 请求不能产生费用，生成必须由用户显式确认。
    """
    if stats.get("empty"):
        return {"ok": False, "error": "还没有已平仓交易，无法复盘", "advice": None, "flags": flags}

    key = _cache_key(stats, trades)
    cached = load_cached(login)
    if not force and cached and cached.get("key") == key and cached.get("advice"):
        return {"ok": True, "cached": True, **cached, "flags": flags}

    if not allow_call:
        return {
            "ok": False,
            "need_confirm": True,
            "error": "尚未生成复盘。调用 DeepSeek API 会产生费用，需要你确认。",
            "advice": (cached or {}).get("advice"),
            "generated_at": (cached or {}).get("generated_at"),
            "stale": bool(cached),
            "flags": flags,
        }

    usage: dict = {}
    try:
        from agent.sandbox import extract_json
        from agent.llm import chat_ex

        text, usage = chat_ex(build_prompt(stats, flags, trades, scoring), system=SYSTEM)
        parsed = extract_json(text)
    except Exception as exc:  # noqa: BLE001
        _log_call(False, usage, f"{type(exc).__name__}: {exc}", login=login)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "advice": (cached or {}).get("advice"),
            "stale": bool(cached),
            "flags": flags,
        }

    _log_call(True, usage, login=login)
    payload = {
        "key": key,
        "generated_at": int(time.time()),
        "login": paths.resolve_login(login),
        "advice": parsed,
        "usage": usage,
        "raw": text,
    }
    p = advice_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "cached": False, **payload, "flags": flags}


def api_call_summary(login: int | str | None = None) -> dict:
    """统计历史 API 调用次数与 token 消耗。

    login=None 时汇总**所有账户**——花钱的总数跟账户无关，要看一起看。
    """
    if login is None:
        logs = sorted(paths.ACCOUNTS_DIR.glob("*/api_calls.jsonl")) if paths.ACCOUNTS_DIR.exists() else []
        total = {"calls": 0, "total_tokens": 0, "last_at": None}
        for p in logs:
            part = _summarize_log(p)
            total["calls"] += part["calls"]
            total["total_tokens"] += part["total_tokens"]
            total["last_at"] = max(filter(None, [total["last_at"], part["last_at"]]), default=None)
        return total
    return _summarize_log(call_log_path(login))


def _summarize_log(path: Path) -> dict:
    if not path.exists():
        return {"calls": 0, "total_tokens": 0, "last_at": None}
    calls = 0
    tokens = 0
    last_at = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        calls += 1
        tokens += int((row.get("usage") or {}).get("total_tokens") or 0)
        last_at = row.get("ts") or last_at
    return {"calls": calls, "total_tokens": tokens, "last_at": last_at}


if __name__ == "__main__":
    from live.journal import collect
    from live.stats import compute, risk_flags

    info = collect(force=True)
    if not info["ok"]:
        print("读取失败:", info["error"])
        raise SystemExit(1)
    st = compute(info["trades"], info["account"], info["deposits"], info.get("flows"))
    fl = risk_flags(st)
    login = info.get("login")
    force = "--force" in __import__("sys").argv
    res = advise(st, fl, info["trades"], force=force, allow_call=False,
                 scoring=info.get("scoring"), login=login)
    if res.get("need_confirm"):
        print("需要确认后才会调用 DeepSeek API（会产生费用）。")
        try:
            ans = input("确认继续？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消，未调用 API。")
            raise SystemExit(0)
        res = advise(st, fl, info["trades"], force=force, allow_call=True,
                     scoring=info.get("scoring"), login=login)
    if not res["ok"]:
        print("AI 复盘失败:", res.get("error"))
        raise SystemExit(1)
    adv = res["advice"]
    print(f"结论：{adv.get('headline')}")
    print(f"评级：{adv.get('verdict')}")
    print("\n问题：")
    for p in adv.get("problems", []):
        print(f"  [{p.get('severity')}] {p.get('title')}")
        print(f"     证据：{p.get('evidence')}")
        print(f"     成因：{p.get('root_cause')}")
    print("\n动作：")
    for a in sorted(adv.get("actions", []), key=lambda x: x.get("priority", 99)):
        print(f"  {a.get('priority')}. {a.get('action')}")
        print(f"     为什么：{a.get('why')}")
        print(f"     怎么验证：{a.get('how_to_verify')}")
    print(f"\n样本说明：{adv.get('note')}")
