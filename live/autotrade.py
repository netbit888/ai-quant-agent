"""AI 实时做单：每分钟让模型猜方向、自己决定何时平仓。

规则（写死在代码里，不是配置项）
--------------------------------
1. 每 60 秒一次决策，**必须给出结论**：空仓时给方向（多/空），持仓时给去留。
2. 同一品种上同时只允许 1 笔持仓，**任何来源的持仓都算**（包括你手动开的）。
3. 平仓后 30 秒内不开新仓。
4. **不挂止盈、不挂止损**。退出只有一种方式：AI 主动发市价平仓单。
   在 MT5 账本里这就是「手动平仓」（deal 无 `[sl]`/`[tp]` 注释），
   不是被止盈止损打掉的——这两件事在统计口径上必须分得开。
5. 开仓 = 让模型估「未来 1~3 分钟上涨概率 p_up」，p_up > 0.5 做多，否则做空。

一次 tick 只花一次模型调用
---------------------------
空仓就调「进场判断」，持仓就调「平仓判断」，两者二选一。
不会为了记账多打一次 API，也不会在持仓期间白烧调用。

模块本身**不做风控**（不设止损是明确要求），但有三个刹车：
`max_trades` / `max_loss_usd` / `max_minutes`，触发即停，
写入 `autotrade_state.json` 的 `stop_reason`，重启不会自动恢复。

数据落在账户目录下：
    autotrade_state.json   运行状态（是否在仓、上次开平仓时间、累计笔数）
    autotrade_log.jsonl    逐 tick 决策流水（open / skip / close / stop）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from live import paths


def _llm():
    """延迟导入：没装 openai 的机器上，纯函数单测也要能跑。"""
    from agent import llm

    return llm


# ------------------------------------------------------------------ 配置


@dataclass
class Cfg:
    """运行参数。规则类的东西（单笔、30 秒冷却、不挂 SL/TP）不在这里。"""

    symbol: str = "XAUUSDm"
    lots: float = 0.01
    interval_sec: int = 5
    cooldown_sec: int = 30
    magic: int = 20261022
    dry_run: bool = True
    tf: str = "M1"
    # 只取 14 根就够：AI 只允许看当前 + 前 13 根。多取没意义，
    # 反正 prompt 层还会再截一次（见 visible_bars）。
    bars: int = 14
    # 平仓后让 AI 回看一段做复盘，并把结论带进下一次进场判断
    review: bool = True
    # 用户明确要求「无视点差成本」：成本由系统承担，不让模型拿它当拒绝的理由。
    # 打开后（1）进场数据里不再给成本数字（2）系统提示禁止以成本为由拒绝
    # （3）注入经验时过滤掉「点差/成本过高」这一类教训。
    # 不这么做的话，模型会一直说"点差240吃掉微利，不做"，结果一笔都不开。
    ignore_spread: bool = True
    # 最短持仓秒数：刚开仓就平等于白送点差。60 秒内除浮亏超过豁免线，一律不平。
    # 2026-10-23 起这条只在 tp_min_usd=0 时才起作用（见下面的止盈门槛）。
    min_hold_sec: int = 60
    # 浮亏超过这个额度才允许提前平仓（保护，别真扛爆）
    min_hold_exempt_usd: float = 0.5
    # ---------------------------------------------------------------
    # 出场规则（2026-10-23 第 3 场复盘后加，专治「1:1 盈亏比 + 24 点差」的必输结构）
    #
    # 那一场的账：191 笔，胜率 35.6%，avgW 1.011 / avgL 1.026 → 盈亏比 0.99，
    # 要打平需要胜率 50.4%。点差 0.24/笔 × 191 = 45.84，占总亏损 80%；
    # 扣点差后判断失误只有 -11.59。也就是说**问题不在判断准不准，在每笔抓得太少**：
    # 平均只抓 1.02 USD 的波动，光过路费就吃掉 23.5%，做的越多亏得越快。
    #
    # 参数不是拍的，是用 `tools/replay_exit.py` 拿那 191 个真实入场点
    # × 当天真实 M1 K 线扫出来的（36 组组合）。结论：
    #   实际（60 秒就跑）        -57.43   期望 -0.301   胜率 35.6%
    #   止损 1.0 / 止盈 2.0      -29.00   期望 -0.152   胜率 28.3%
    #   止损 3.0 / 止盈 3.25     -16.75   期望 -0.088   胜率 46.6%  ← 拐点
    #   止损 4.0 / 止盈 4.5      -87.11   期望 -0.456   胜率 41.4%  （再宽就崩）
    # 全部 36 组都是负期望。所以这两个参数**把亏损砍掉 71%，但救不活**——
    # 最好的一组仍需 48.0% 胜率，实际只有 46.6%，差 1.4 个百分点。
    # 剩下那 1.4pp 只能靠改入场方向（p_up 有一半是饱和的 0.0，见下）。
    #
    # 起作用的是什么，跟直觉相反：不是"让利润跑"，而是**给够空间**。
    # 胜率从 35.6% 涨到 46.6%，是因为短期噪音不再把方向正确的单子打成亏损；
    # 而 3.25/3.0 的盈亏比只有 1.08，几乎对称。真正压低点差占比的是
    # 平均波动从 1.02 变成 3.1（点差占比 23.5% → 7.7%）。
    #
    # 硬止损：浮亏到这个额度**由程序直接市价平**，不问模型。
    # 为什么不信模型：前四大亏（-6.08/-3.51/-2.70/-2.49）全是 p_up=0.0 的单子，
    # 它最自信的时候亏得最多，指望它自己认错不现实。
    # 注意这不是给订单挂 SL——账本里仍然是「手动平仓」，只是触发者是代码。
    # 这样复盘口径、K 线判定都不用改。
    sl_usd: float = 3.0
    # 止盈门槛：浮盈没到这个数，模型说平也不平，继续持有（也不调模型，省 token）。
    # 设 0 关掉，回到「60 秒一到就随便平」的老行为。
    tp_min_usd: float = 3.25
    # 持仓上限。拉长持仓必须有上限，否则震荡行情里会变成无限扛单：
    # 回放里 3.0/3.25 这组的中位持仓已经 480 秒、最长 4560 秒（76 分钟），
    # 再不封顶就成隔夜了——1 分钟周期的策略持有到隔夜已经不是同一个策略。
    # 到点无条件市价平，盈亏都平。
    max_hold_sec: int = 3600
    # 位置阈值：收盘位置 ≤ 这个值算"贴下沿"、≥ 1-这个值算"贴上沿"，中间不动手。
    # 从 0.20 放宽到 0.35——原来太严，死水行情里几乎永远轮不到它出手。
    zone_edge: float = 0.35
    # 是否把 tick 级（逐笔 bid/ask）数据喂给模型。关掉可省 token 和一次数据查询。
    ticks: bool = True
    # 信号强度门槛：|p_up - 0.5| 低于这个值就不开仓。
    # 为什么必须有：p_up=0.51 和 0.5 在统计上没区别，但开仓要先付 0.24 点差。
    # 高频下单时这种"微弱优势单"一分钟能有十几笔，数学上必被磨死。
    # 设 0 可关闭（回到"只要不等于 0.5 就开"的旧行为）。
    min_edge: float = 0.06
    # 刹车。0 = 不限。这是「跑飞了自动停」，不是风控策略。
    max_trades: int = 0
    max_loss_usd: float = 0.0
    max_minutes: int = 0
    comment: str = "aqa_ai"


# ------------------------------------------------------------------ 存储


def state_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "autotrade_state.json")


def log_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "autotrade_log.jsonl")


def _empty_state() -> dict:
    return {
        "running": False,
        "started_at": None,
        "last_tick_ts": None,
        "last_open_ts": None,
        "last_close_ts": None,
        "trades": 0,       # 已开仓笔数
        "closed": 0,       # 已平仓笔数
        "wins": 0,
        # 其中有多少笔是程序硬止损平的（source="sl"）。与「模型主动平」分开数，
        # 否则命中率会被止损单污染。
        "sl_closes": 0,
        "pnl": 0.0,
        "position": None,
        "stop_reason": None,
        "last_decision": None,
        # 出场端连续模型调用失败次数：≥3 次自动停（仓位没人管比少做一笔危险）
        "model_errors": 0,
        # 已平仓、但还没到复盘时机的单子。平仓后要等 7 根 K 线走完才复盘，
        # 这段时间里系统照常做下一笔——复盘不能挡住交易。
        "pending_reviews": [],
        # 谁在跑：panel = 面板线程，cli = 命令行进程。面板重启后 state 里可能
        # 还留着 running=True，靠这两个字段判断那条记录是不是已经作废了。
        "owner": None,
        "pid": None,
        # 最近一次运行的配置（含 dry_run）。面板「关闭并清仓」时线程可能已经
        # 不在了，必须能还原「刚才到底是纸面还是真实」，否则会把真单当成
        # 纸面单跳过，用户以为平了其实没平。
        "cfg": None,
    }


def load_state(login: int | str | None = None) -> dict:
    p = state_path(login)
    base = _empty_state()
    if not p.exists():
        return base
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if isinstance(data, dict):
        base.update(data)
    return base


def save_state(state: dict, login: int | str | None = None) -> None:
    paths.write_json(paths.resolve_login(login), "autotrade_state.json", state)


def cfg_to_dict(cfg: Cfg) -> dict:
    return {k: getattr(cfg, k) for k in Cfg.__dataclass_fields__}


def cfg_from_state(login: int | str | None = None) -> Cfg | None:
    """还原上次运行的配置。没有记录返回 None。"""
    raw = load_state(login).get("cfg")
    if not isinstance(raw, dict):
        return None
    clean = {k: v for k, v in raw.items() if k in Cfg.__dataclass_fields__}
    try:
        return Cfg(**clean)
    except (TypeError, ValueError):
        return None


def append_log(row: dict, login: int | str | None = None) -> None:
    p = log_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_log(login: int | str | None = None, limit: int | None = None) -> list[dict]:
    p = log_path(login)
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
    return out[-limit:] if limit else out


# ------------------------------------------------------------------ 解析模型输出
#
# 模型不保证严格 JSON，所以解析必须是「宽进严出」：能从各种烂格式里抠出数字，
# 但抠不出来就返回 None，由调用方退回规则兜底。绝不能猜一个数字糊过去——
# 猜出来的概率和模型给的没区别，事后就没法判断到底是模型的锅还是解析的锅。

_JSON_RE = re.compile(r"\{[^{}]*\}", re.S)
_PUP_RE = re.compile(r'"?p_?up"?\s*[:=]\s*([0-9]+\.?[0-9]*)', re.I)
_CLOSE_RE = re.compile(r'"?close"?\s*[:=]\s*(true|false|1|0|yes|no)', re.I)


def _norm_prob(val) -> float | None:
    """把各种写法的概率归一化到 [0,1]。62 / 0.62 / "62%" 都收。"""
    try:
        num = float(str(val).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if num > 1.0:
        if num > 100.0:
            return None
        num = num / 100.0
    if num < 0.0 or num > 1.0:
        return None
    return num


P_UP_FLOOR, P_UP_CEIL = 0.20, 0.80


def clamp_p_up(p: float) -> float:
    """把 p_up 压回 [0.20, 0.80]。

    实测 46% 的 p_up 精确等于 0.0（标定崩坏），|p_up-0.5|≥min_edge 的闸门
    对它们形同虚设。0/1 是伪精确，不是概率估计；截断后摆烂值和中等置信
    都落在有效区间里，min_edge 才有意义。system 已同步要求模型只在这个
    区间输出，这里是代码层兜底——模型守不守规矩不该靠它自觉。
    """
    return max(P_UP_FLOOR, min(P_UP_CEIL, float(p)))


def _norm_bool(raw) -> bool | None:
    """把模型写的 act 归一成布尔。认不出来就返回 None（交给下游按旧逻辑处理）。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "y", "是", "做", "开仓", "trade"):
            return True
        if s in ("false", "0", "no", "n", "否", "不做", "不交易", "观望", "等", "wait", "skip"):
            return False
    return None


def parse_p_up(text: str) -> dict | None:
    """从模型回复里抠出 {act, p_up, reason}。失败返回 None。

    `act` 是"这笔到底做不做"的显式开关。以前只能靠 p_up≈0.50 间接表达"不做"，
    结果模型经常一边写"不追"一边给 0.38 —— 说不做，数值却在开仓。
    多一个布尔字段，两者就不会再打架。老格式没有 act 时返回 None，下游按旧逻辑走。
    """
    if not text or not text.strip():
        return None

    data: dict | None = None
    m = _JSON_RE.search(text)
    if m:
        try:
            loaded = json.loads(m.group(0))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = None

    if data is not None:
        raw = data.get("p_up", data.get("pUp", data.get("prob")))
        p_up = _norm_prob(raw)
        if p_up is not None:
            reason = str(data.get("reason") or data.get("why") or "").strip()
            act = _norm_bool(data.get("act", data.get("trade", data.get("action"))))
            # agree：模型自己声明是否同意程序的位置判定。
            # None = 没答（老格式/解析失败），下游不强行推断。
            agree = _norm_bool(data.get("agree"))
            return {"p_up": clamp_p_up(p_up), "reason": reason[:200],
                    "act": act, "agree": agree}

    # JSON 烂了，退一步直接抓数字
    mm = _PUP_RE.search(text)
    if mm:
        p_up = _norm_prob(mm.group(1))
        if p_up is not None:
            return {"p_up": p_up, "reason": "", "act": None}

    return None


def parse_exit(text: str) -> dict | None:
    """从模型回复里抠出 {close, reason}。失败返回 None。"""
    if not text or not text.strip():
        return None

    data: dict | None = None
    m = _JSON_RE.search(text)
    if m:
        try:
            loaded = json.loads(m.group(0))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = None

    if data is not None:
        raw = data.get("close", data.get("exit", data.get("act")))
        if isinstance(raw, bool):
            return {"close": raw, "reason": str(data.get("reason") or "").strip()[:200]}
        if isinstance(raw, (int, float)):
            return {"close": bool(raw), "reason": str(data.get("reason") or "").strip()[:200]}
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("true", "1", "yes", "close", "exit", "平", "平仓"):
                return {"close": True, "reason": str(data.get("reason") or "").strip()[:200]}
            if low in ("false", "0", "no", "hold", "keep", "持", "持有"):
                return {"close": False, "reason": str(data.get("reason") or "").strip()[:200]}

    mm = _CLOSE_RE.search(text)
    if mm:
        val = mm.group(1).lower()
        return {"close": val in ("true", "1", "yes"), "reason": ""}

    return None


# ------------------------------------------------------------------ 规则兜底
#
# 模型挂了 / 没配 key / 输出没法解析时用。存在的意义不是"替代 AI"，
# 而是让链路能继续跑并且**在日志里明确标成 fallback**，
# 这样事后统计命中率时可以把兜底的那部分单独挑出来，不污染对模型的评价。


# 这里原来有 fallback_p_up / fallback_exit 两个「规则兜底」：模型调不通时用
# 最近 10 根动量猜个概率、或按浮亏直接平仓，照样能真下单。
# 已删除（2026-10-23）：免费 API 的限速 429 会让它频繁触发，等于一条三行的
# 动量规则在替我下真单。现在模型答不上来就这一轮不做，见 _entry_skip / _exit_hold。


# ------------------------------------------------------------------ tick 级数据
#
# 用户要的是「市场报价」里那两根线背后的原始数据：逐笔 bid/ask 变化。
# 但黄金 1 分钟就有几十到几百个 tick，14 分钟几千笔——原始流直接塞给模型
# 既贵又没用（它读不出规律，只会稀释注意力）。所以：
#   1. 按分钟聚合成表：tick 数 / 买价区间 / 卖价区间 / 点差均值与最宽；
#   2. 再附最后十几笔原始 tick，让它看见"刚刚这一下"。
# 时间边界与 K 线**完全一致**：只用这 14 根覆盖的那段时间，不额外偷看未来。

TICK_RECENT = 10


def fetch_ticks(cfg: Cfg, bars: list[dict]) -> list[dict]:
    """取与可见 K 线同一时间段的 tick。任何一种失败都返回 []，不打断决策。"""
    if not cfg.ticks:
        return []
    w = visible_bars(bars)
    if not w:
        return []

    # 时间一律走 K 线自己的服务器时间轴，不要混进本机 time.time()：
    # 券商服务器时区和本机不一定一致，混用会把区间算歪（查不到 tick 或查错段）。
    from_ts = int(w[0].get("time") or 0)
    to_ts = int(w[-1].get("time") or 0) + 120

    try:
        from mt5.bridge import get_ticks_live

        ticks = get_ticks_live(cfg.symbol, from_ts, to_ts)
    except Exception:  # noqa: BLE001
        return []

    # 只留落在可见窗口内的
    return [t for t in ticks if from_ts <= int(t.get("time") or 0) <= to_ts]


def _spread_points(tick: dict, point: float) -> float:
    if not point:
        return 0.0
    return (float(tick.get("ask") or 0.0) - float(tick.get("bid") or 0.0)) / point


def summarize_ticks(ticks: list[dict], bars: list[dict],
                    point: float = 0.001, recent: int = TICK_RECENT) -> dict:
    """按可见 K 线分桶统计。数据不足时返回 {"ok": False}。"""
    w = visible_bars(bars)
    if not ticks or not w:
        return {"ok": False, "reason": "没有 tick 数据"}

    # 只统计可见窗口内的 tick。不然"合计/平均点差"会把窗口外的数据算进来，
    # 数字和上面的 K 线对不上，模型会据此做错误判断。
    t_start = int(w[0].get("time") or 0)
    t_end = int(w[-1].get("time") or 0) + 60
    ticks = [t for t in ticks if t_start <= int(t.get("time") or 0) < t_end]
    if not ticks:
        return {"ok": False, "reason": "窗口内没有 tick"}

    rows = []
    for i, b in enumerate(w):
        t0 = int(b.get("time") or 0)
        t1 = t0 + 60
        inside = [t for t in ticks if t0 <= int(t.get("time") or 0) < t1]
        if not inside:
            rows.append({"tag": "当前" if i == len(w) - 1 else f"T-{len(w) - 1 - i}",
                         "n": 0})
            continue
        bids = [float(t.get("bid") or 0.0) for t in inside]
        asks = [float(t.get("ask") or 0.0) for t in inside]
        sp = [_spread_points(t, point) for t in inside]
        rows.append({
            "tag": "当前" if i == len(w) - 1 else f"T-{len(w) - 1 - i}",
            "n": len(inside),
            "bid_lo": min(bids), "bid_hi": max(bids),
            "ask_lo": min(asks), "ask_hi": max(asks),
            "spread_avg": sum(sp) / len(sp) if sp else 0.0,
            "spread_max": max(sp) if sp else 0.0,
        })

    spreads = [_spread_points(t, point) for t in ticks]
    # 相邻 tick 的最大单步跳动：能看出有没有瞬间插针
    jump = 0.0
    prev = None
    for t in ticks:
        cur = float(t.get("bid") or 0.0)
        if prev is not None:
            jump = max(jump, abs(cur - prev) / point if point else 0.0)
        prev = cur

    return {
        "ok": True,
        "rows": rows,
        "total": len(ticks),
        "spread_avg": sum(spreads) / len(spreads) if spreads else 0.0,
        "spread_max": max(spreads) if spreads else 0.0,
        "max_jump": jump,
        "recent": ticks[-recent:],
        "point": point,
    }


def _fmt_tick_block(summary: dict) -> str:
    if not summary or not summary.get("ok"):
        return "（tick 数据暂不可用，只能依据上面的 K 线判断）"

    lines = ["分钟   tick数  买价区间                  卖价区间                  点差 均/最大"]
    for r in summary["rows"]:
        if not r.get("n"):
            lines.append(f"{r['tag']:<6} {0:>6}  —")
            continue
        lines.append(
            f"{r['tag']:<6} {r['n']:>6}  "
            f"{r['bid_lo']:.3f}-{r['bid_hi']:.3f}          "
            f"{r['ask_lo']:.3f}-{r['ask_hi']:.3f}          "
            f"{r['spread_avg']:.0f}/{r['spread_max']:.0f}"
        )

    recent = summary.get("recent") or []
    if recent:
        lines.append("")
        lines.append(f"最近 {len(recent)} 笔 tick（时间 UTC，买价/卖价/点差）：")
        for t in recent:
            ts = int(t.get("time") or 0)
            clock = time.strftime("%H:%M:%S", time.gmtime(ts)) if ts else "--"
            lines.append(
                f"  {clock}  {float(t.get('bid') or 0):.3f}/{float(t.get('ask') or 0):.3f}"
                f"  点差 {_spread_points(t, summary.get('point') or 0.001):.0f}"
            )

    lines.append("")
    lines.append(
        f"tick 合计 {summary['total']} 笔；点差 平均 {summary['spread_avg']:.0f} 点、"
        f"最宽 {summary['spread_max']:.0f} 点；相邻 tick 最大单步跳动 {summary['max_jump']:.0f} 点。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ 提示词
#
# 信息边界（用户指定的硬约束）
# ---------------------------
# AI 交易员**只能看到当前这根 K 线 + 之前 13 根**，共 14 根，四价全给。
# 这不是在提示词里客气地写一句"请你只看这些"，而是在构造 prompt 时**物理截断**：
# 就算上游传进来 120 根，`visible_bars()` 也只会输出最后 14 根。
# 模型守不守规矩不该靠它自觉——它能看到的和它被告知能看到的一致，才叫约束。
#
# 唯一的例外是**平仓之后**：允许回看「平仓前 13 根 → 开仓之前」那一段，
# 用来复盘。那时仓位已经没了，回看不会让它改主意，所以不违反约束。

VISIBLE_BARS = 14      # 当前 1 根 + 之前 13 根
# 复盘窗口：**平仓前 7 根 + 平仓这根 + 平仓后 7 根 = 15 根**（用户指定）。
# 为什么要等平仓后 7 根走完才复盘：刚平掉那一刻还没有"平仓后走势"，
# 只能得出"赚了/亏了"；等 7 根之后才看得出「平太早」还是「跑得及时」。
REVIEW_BEFORE_BARS = 7
REVIEW_AFTER_BARS = 7


def visible_bars(rows: list[dict]) -> list[dict]:
    """裁到 AI 允许看到的 14 根。所有喂给模型的 prompt 都必须过这一道。"""
    return rows[-VISIBLE_BARS:] if rows else []


ENTRY_SYSTEM = """你是 XAUUSD（现货黄金）的超短线交易员，看 M1 图。
你赚的是 1~3 分钟内的那一小段价差——不是趋势、不是波段、更不是隔夜。
目标是尽可能多地把市场里真实存在的利润拿下来：单笔目标小、出手频次高，
所以每一笔都必须干净利落，不要指望"大行情"来救你。

【信息边界】
你只能看到当前 1 根 K 线和之前 13 根，共 14 根，以及这 14 分钟内的逐笔 tick。
没有更多数据，也不要假装有：不要引用均线、技术指标、更高周期、新闻、成交量、
市场情绪，这些你根本看不到，编出来就是幻觉。

【主次不能反】
以上面 1 分钟蜡烛图为主：结构、连阳连阴、实体与影线、收盘在区间里的位置，
这些才是你做判断的依据。
tick 级数据是辅助，只用来确认两件事：这一下是真突破还是插针，点差有没有异常放大。
tick 不得单独构成开仓理由；K 线与 tick 冲突时，以 K 线为准。

【先判行情，再选打法（两套打法，别用反）】
用「结构 ER（净移动 ÷ 总行程，0~1）」判断这 14 根的性质，再选对应打法：
- ER < 0.30 → 窄幅震荡（小波动）：低买高卖。
  贴近区间下沿做多、贴近上沿做空（以数据里的「位置判定」为准），中间区域不动手；
  不要追着单根大阳线/大阴线跑，那通常是震荡里的假突破。
- ER > 0.50 → 单边趋势（大波动）：不要逆势抄底摸顶。
  等回踩到均势位置再顺势进——回踩位参考前一根实体的中位、
  或最近 3~4 根的低点（上升趋势）/高点（下降趋势）。没回踩就等。
- 0.30 ~ 0.50 → 方向不明确：只做位置明显的单（收盘位置贴近区间边缘），
  中间位置给 0.50，不开仓。
  数据里的「位置判定」已经替你算好了是否够边缘（默认阈值 35%/65%），直接按它走。

【多空对称，这条不许违反】
- **贴下沿 ≠ 买点，贴上沿 ≠ 卖点。** 下跌趋势里贴下沿是弱势延续，不是超卖，
  程序倾向不接多；上涨趋势里贴上沿是强势延续，不是超买，程序倾向不追多。
- 多空都要有确认：做多要看到止跌（收阳，或收盘回到区间上半），
  做空要看到滞涨（收阴，或收盘回落到区间下半）。没有确认就等。
- **两个方向的机会是对等的。** 不要因为"最近做空老是亏"就只做多——
  那是样本偏差，不是规则。
tick 的辅证作用：tick 数骤增 + 相邻跳动变大 = 真的有资金在推，此时的突破更可信；
tick 稀疏而安静 = 突破多半是假的。

【程序建议 vs 你的判断】
数据里的「位置判定」是程序按规则算出的**建议**，不是命令。你可以不同意——
但不同意时，reason 里必须说清你看到的证据（K 线形态、tick 辅证）和程序的
判定差在哪。同意时直接按建议做。你的 p_up 就是你的判断本身，别拿它复读
程序的建议：程序给的是方向，你对时机的把握和置信度才是你真正的贡献。

【输入格式（顺序固定）】
用户消息按这个顺序给，内容只有数据，规则全在本段：
  1) 品种与周期
  2) 【主】14 根 M1 K 线：序号（T-13…T-1、当前）/ 时间 / 开 / 高 / 低 / 收
  3) 【主】结构指标：ER、振幅、收盘位置、连阳连阴、行情判定
  4) 【辅】tick 数据：每根内的笔数与买卖价区间、点差，以及最近若干笔原始 tick
  5) 短周期变化与振幅
  6) 你的历史战绩与最近复盘
  7) 当前报价与来回成本
  8) 当前时间（最后一行）

【输出】
只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"act": true, "p_up": 0.00, "agree": true, "reason": ""}
act —— 这笔到底做不做。false 就是"不做"，这是允许的、也是常见的选择。
p_up —— 未来 1~3 分钟内价格上涨的概率，0 到 1 之间的小数。
        即使 act=false 也要给，用来记录你当时的方向倾向。
        **只在 0.20 ~ 0.80 之间取值**：0/1 是伪精确（没人能百分百确定 1 分钟后
        的方向），写 0.20 就代表你最强的看空，0.80 代表最强的看多。
agree —— 你的方向判断是否与数据里「位置判定」的程序倾向一致。
        一致 true，不一致 false（此时 reason 必须给出你反对的依据）。
reason —— 不超过 40 字的中文依据，必须引用输入里的具体数字。

注意 act 和 reason 必须一致：写了"不追""等确认"，act 就是 false。
不要出现理由说不做、act 却给 true 的情况。

【判断原则】
- **不做交易是允许的，而且经常是对的。** 给出 `"act": false` 即可，
  这是正常决策，不是失误，不需要找补。
- 反过来，结构确实给出方向时就敢做（act=true，且 p_up ≥0.60 或 ≤0.40）。
  不要为了"有单可做"把 0.50 抬到 0.56，也不要在明显趋势里用 0.50 逃避判断。
- 即使 act=true，p_up 与 0.50 的差距不到 0.06 时系统仍会跳过——
  那是因为成本不值，不是你的判断有问题。
- 上面的教训是你自己的历史，不是摆设：当前情形如果和某条教训说的是同一回事，
  就按它做——包括"这种情况不要做"。
- **成本不由你负责。** 点差和手续费由系统承担，你已经知道这一点，不需要计算它。
  不要以"点差太高""成本吃掉利润""利润空间不够覆盖成本"为由拒绝开仓，
  也不要因此偏移方向判断。你只回答一件事：接下来 1~3 分钟往哪边走。"""

ENTRY_PROMPT = """品种 {symbol} · 周期 {tf} · 窗口 14 根（当前 + 前 13 根）
【主】K线（序号 / 时间 UTC / 开 / 高 / 低 / 收）
{bars}
【主】{structure}
【辅】Tick 级数据（逐笔 bid/ask，时间段与上面 14 根完全相同；只用于确认突破真假与点差）
{ticks}
短周期变化 r1={r1:+.3%}  r5={r5:+.3%}  r13={r13:+.3%}  振幅={span:.3f}
战绩 样本={n_closed} 笔  命中率={hit}
{lessons}
报价 bid={bid:.3f}  ask={ask:.3f}  点差={spread_pts} 点
时间 {clock} UTC"""

EXIT_SYSTEM = """你是 XAUUSD 超短线交易员的持仓管理模块，只决定「继续持有」还是「立刻平仓」。
这套打法的钱来自「方向对的时候拿够一段」，不是靠秒级周转——实测 60 秒就跑的
时候平均只抓 1 美元，光点差就吃掉四分之一，做的越多亏得越快。

【信息边界】
你只能看到当前 1 根 K 线和之前 13 根，共 14 根，以及这 14 分钟内的逐笔 tick。
没有更多数据。

【输入格式（顺序固定）】
  1) 品种与周期
  2) 【主】14 根 M1 K 线：序号 / 时间 / 开 / 高 / 低 / 收
  3) 【主】结构指标：ER、振幅、收盘位置、连阳连阴、行情判定
  4) 【辅】tick 数据（只用于确认突破真假与点差异常）
  5) 短周期变化
  6) 当前持仓：方向 / 手数 / 开仓价 / 已持仓时间 / 浮动盈亏 / 当初开仓依据
  7) 你从自己交易里总结的教训（按重复次数排序）
  8) 当前报价
  9) 当前时间（最后一行）

出场同样要看教训：像「止跌没延续就该立刻走」这种话，只有出现在出场判断里才有用。

【输出】
只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"close": false, "reason": ""}
close —— true 表示立刻市价平仓，false 表示继续持有。
reason —— 不超过 40 字的中文依据，必须引用具体数字。

【系统的自动规则（不用你判断，也不要跟它较劲）】
- 浮亏达到 3.0 USD：系统直接市价平，不会问你；
- 持仓满 60 分钟：系统无条件平掉，不管盈亏；
- 浮盈不到 3.25 USD：系统也不问你要不要平，直接继续持有。
所以**你被问到，就说明这笔单已经浮盈 ≥ 3.25 USD**，而且还没到 60 分钟。

【判断原则】
- 主看 K 线结构，tick 只做辅证；两者冲突时以 K 线为准。
- 止损和超时已经由系统兜住了，你不用考虑「万一反手怎么办」。
- 你唯一的任务是判断**这波行情还有没有余量**：
  · 结构没走完（仍贴趋势方向、ER 在走高、连续阴/阳没被打断）→ 继续持有；
  · 结构已经走完（贴到反向沿、连续形态被打断、ER 掉回震荡区）→ 落袋。
- 成本不由你负责：点差由系统承担，不要用"点差吃掉利润"当平仓理由。
- 必须二选一，不存在"再等等看情况"。
- 别因为"已经赚了 3 块多"就急着落袋，也别死拿等它翻倍——看结构，不看金额。"""

EXIT_PROMPT = """品种 {symbol} · 周期 {tf} · 窗口 14 根（当前 + 前 13 根）
【主】K线（序号 / 时间 UTC / 开 / 高 / 低 / 收）
{bars}
【主】{structure}
【辅】Tick 级数据（逐笔 bid/ask，时间段与上面 14 根完全相同）
{ticks}
短周期变化 r1={r1:+.3%}  r5={r5:+.3%}  r13={r13:+.3%}
持仓 {direction_text} {lots} 手  开仓 {entry:.3f}  已持仓 {held_text}  浮动 {pnl_text}
开仓依据 {entry_reason}
你从自己交易里总结的教训
{lessons}
报价 bid={bid:.3f}  ask={ask:.3f}
时间 {clock} UTC"""

REVIEW_SYSTEM = """你是一名 XAUUSD 超短线交易员的复盘教练。仓位已经平掉了，这一笔的钱已经落袋，
所以你现在可以看比平时更多的 K 线：**平仓前 7 根 + 平仓这根 + 平仓后 7 根，共 15 根**。
平仓后那 7 根是你判断"这笔平得对不对"的唯一依据：
  - 平完行情继续顺着原方向走 → 平早了，该多拿一会儿；
  - 平完行情掉头 → 平得对，这个离场信号要记住；
  - 平完来回磨 → 无关紧要，重点看进场那一端。
开仓和平仓是两个独立决策，哪个错就骂哪个，别混成一句"方向看错了"。

只输出一句话，不超过 60 字，说清「主要因为什么赚/亏，下次同样情形该怎么做」。
不要客套，不要复述数字以外的事实。"""

REVIEW_PROMPT = """这一笔刚平掉。
方向：{direction_text}　手数：{lots}　开仓 {entry:.3f} → 平仓 {exit_price:.3f}
最终盈亏：{pnl:+.2f} USD　持仓 {held_text}
开仓理由：{why_in}
平仓理由：{why_out}

你被允许回看的区间：平仓前 {before} 根 → 平仓这根 → 平仓后 {after} 根（共 {n} 根 {tf}）
序号  时间(UTC)      开      高      低      收
{bars}

只输出一句复盘结论。"""


# 行情状态分桶阈值。_structure_text 的「行情判定」和教训的 regime 标签
# 必须用同一套数，各写一份必然漂移。
ER_RANGE_MAX = 0.30   # ER 低于此 = 震荡 → 低买高卖
ER_TREND_MIN = 0.50   # ER 高于此 = 趋势 → 等回踩顺势


def regime_of(rows: list[dict]) -> str:
    """把这堆 K 线归到三种行情状态之一："range" / "trend" / "mid"。

    用途是给复盘教训打标签：在震荡里攒的教训，优先在震荡里拿出来用。
    数据不足返回空串，归「通用」。
    """
    s = _structure(rows)
    if not s.get("ok"):
        return ""
    if s["er"] < ER_RANGE_MAX:
        return "range"
    if s["er"] > ER_TREND_MIN:
        return "trend"
    return "mid"


def _structure(rows: list[dict]) -> dict:
    """把这 14 根 K 线的"性质"算成几个数，让模型不用凭感觉猜行情。

    ER（净移动 ÷ 总行程，0~1）是这里的关键：
      - 净移动 = 最后一根收盘 - 第一根收盘（走了多远）
      - 总行程 = 相邻收盘价变动绝对值之和（绕了多远）
      - ER 接近 1 = 一路直行（趋势）；接近 0 = 来回磨（震荡）
    这个数的好处是可解释：两个价格相除，没有黑箱参数。
    "每个数字都能说清怎么来的"是这个项目的卖点，不能用拍脑袋的阈值。
    """
    w = visible_bars(rows)
    if len(w) < 2:
        return {"ok": False}

    closes = [float(r.get("close") or 0.0) for r in w]
    highs = [float(r.get("high") or 0.0) for r in w]
    lows = [float(r.get("low") or 0.0) for r in w]

    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = (net / path) if path else 0.0

    hi, lo = max(highs), min(lows)
    span = hi - lo
    pos = ((closes[-1] - lo) / span) if span else 0.5

    # 末段连续同向根数：连阳为正、连阴为负
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        step = closes[i] - closes[i - 1]
        if step > 0 and streak >= 0:
            streak += 1
        elif step < 0 and streak <= 0:
            streak -= 1
        else:
            break

    return {"ok": True, "er": er, "net": net, "path": path,
            "span": span, "pos": pos, "streak": streak,
            # 带符号的净移动：ER 只回答"走得多直"，不回答"往哪边走"。
            # 位置判定必须两者都有，否则跌势里每次贴下沿都会被读成买点。
            "drift": closes[-1] - closes[0]}


def _zone_text(pos: float, er: float, drift: float, edge: float) -> str:
    """位置判定：同一个「贴下沿」，在震荡里是买点，在下跌趋势里是弱势延续。

    为什么必须分方向（实测教训）：以前这里只输出「贴下沿（可考虑做多）」，
    完全不看趋势方向。价格从 4030 一路跌到 4021 的十几分钟里，14 根窗口
    永远处于区间下部 → 永远显示"可考虑做多"；同一时刻想做空又被
    「追破位」铁律按住。结果 18 笔里最近 4 笔全做多，而且全是在跌势里买跌。

    2026-10-25 话术降级：从「不许/唯一合法方向」改成「程序建议」。
    旧话术把模型逼成二选一（顺从→p_up 钉死 0.0/1.0，反抗→也是极端值，
    实测 46% 的 p_up 精确等于 0.0，30% 的单方向与理由矛盾）。
    现在给方向倾向但明确告知可以不同意——中间概率才有机制上的存在空间。
    判定本身（ER+drift 的组合）不变，变的是语气和权限。
    """
    edge = max(0.0, min(0.49, float(edge)))
    lo, hi = edge, 1 - edge
    downtrend = er > ER_TREND_MIN and drift < 0
    uptrend = er > ER_TREND_MIN and drift > 0
    if pos <= lo:
        if downtrend:
            return ("贴下沿＋下跌趋势（程序倾向：弱势延续，不是超卖，倾向等反抽到上沿做空）——"
                    "你可以不同意，但理由里必须说清为什么")
        if uptrend:
            return "贴下沿＋上涨趋势（程序倾向：回踩到位，倾向做多，需止跌确认）"
        return f"贴下沿（程序倾向：可考虑做多，阈值 {edge:.0%}）"
    if pos >= hi:
        if uptrend:
            return ("贴上沿＋上涨趋势（程序倾向：强势延续，不是超买，倾向等回踩到下沿低吸）——"
                    "你可以不同意，但理由里必须说清为什么")
        if downtrend:
            return "贴上沿＋下跌趋势（程序倾向：反抽到位，倾向做空，需滞涨确认）"
        return f"贴上沿（程序倾向：可考虑做空，阈值 {1 - edge:.0%}）"
    return f"中间地带（{edge:.0%}~{1 - edge:.0%}，程序倾向不动手）"


def _prog_direction(rows: list[dict], zone_edge: float = 0.35) -> int:
    """程序的位置判定折算成方向建议：+1 多 / -1 空 / 0 不动手。

    与 _zone_text 用同一套数（_structure + zone_edge），保证「模型看到的建议」
    和「落盘的 prog_dir」永远一致——各算各的必然漂移。
    返回 0（中间地带）时模型任何方向都算"分歧"，这是设计使然：程序没建议，
    模型自己选的方向就是纯模型判断，记账时单独看这一格的期望。
    """
    s = _structure(rows)
    if not s.get("ok"):
        return 0
    edge = max(0.0, min(0.49, float(zone_edge)))
    pos = s["pos"]
    if pos > edge and pos < 1 - edge:
        return 0
    downtrend = s["er"] > ER_TREND_MIN and s["drift"] < 0
    uptrend = s["er"] > ER_TREND_MIN and s["drift"] > 0
    if pos <= edge:
        if downtrend:
            return -1          # 跌势贴下沿：倾向等反抽做空
        return 1               # 震荡或涨势贴下沿：倾向做多
    # pos >= 1-edge
    if uptrend:
        return 1               # 涨势贴上沿：倾向等回踩低吸
    return -1                  # 震荡或跌势贴上沿：倾向做空


def _structure_text(rows: list[dict], zone_edge: float = 0.35) -> str:
    s = _structure(rows)
    if not s.get("ok"):
        return "结构 数据不足"
    if s["er"] < ER_RANGE_MAX:
        regime = "窄幅震荡（小波动）→ 低买高卖"
    elif s["er"] > ER_TREND_MIN:
        regime = ("单边趋势（大波动）→ 等回踩再顺势"
                  if s["drift"] > 0 else
                  "单边趋势（大波动）→ 等反抽再顺势做空")
    else:
        regime = "方向不明确 → 只做位置明显的单"

    # 位置判定直接算好给模型，不让它自己套阈值：
    # 阈值改一次就得让它跟着变，各算各的必然对不上。
    pos = s["pos"]
    edge = max(0.0, min(0.49, float(zone_edge)))
    zone = _zone_text(pos, s["er"], s["drift"], edge)

    streak = s["streak"]
    streak_text = f"连阳 {streak} 根" if streak > 0 else (
        f"连阴 {-streak} 根" if streak < 0 else "无连阳连阴")
    # 净移动带符号：只给 0.12 这种绝对值，模型分不清是在跌还是在涨
    drift = s["drift"]
    dir_text = "上行" if drift > 0 else ("下行" if drift < 0 else "走平")
    return (f"结构 ER={s['er']:.2f}（净移动 {drift:+.3f}（{dir_text}）/ 总行程 {s['path']:.3f}）"
            f"｜振幅 {s['span']:.3f}｜当前收盘位置 {pos:.0%}（0%=区间最低 100%=最高）"
            f"｜{streak_text}｜位置判定：{zone}｜行情判定：{regime}")


def _fmt_bar_lines(rows: list[dict]) -> str:
    """K 线表格。序号列用 T-13…T-1 / 当前，让模型一眼知道"只有这么点数据"。"""
    lines = []
    n = len(rows)
    for i, r in enumerate(rows):
        ts = int(r.get("time") or 0)
        clock = time.strftime("%m-%d %H:%M", time.gmtime(ts)) if ts else "--"
        tag = "当前" if i == n - 1 else f"T-{n - 1 - i:<2}"
        lines.append(
            f"{tag}  {clock}  {float(r.get('open') or 0):.3f}  "
            f"{float(r.get('high') or 0):.3f}  "
            f"{float(r.get('low') or 0):.3f}  "
            f"{float(r.get('close') or 0):.3f}"
        )
    return "\n".join(lines)


def _fmt_bars(rows: list[dict]) -> str:
    """喂给决策提示词的 K 线。**永远只有 14 根**，不接受"多给一点"。"""
    return _fmt_bar_lines(visible_bars(rows))


def _returns(rows: list[dict]) -> tuple[float, float, float, float]:
    """返回 (r1, r5, r13, 振幅)，全部只在可见的 14 根内计算。

    以前这里用的是 15 根窗口——那已经越过信息边界了，等于偷偷多看一眼。
    """
    w = visible_bars(rows)
    if not w:
        return 0.0, 0.0, 0.0, 0.0

    def chg(n: int) -> float:
        if len(w) <= n:
            return 0.0
        base = float(w[-n - 1].get("close") or 0.0)
        cur = float(w[-1].get("close") or 0.0)
        return (cur - base) / base if base else 0.0

    highs = [float(r.get("high") or 0.0) for r in w]
    lows = [float(r.get("low") or 0.0) for r in w]
    span = (max(highs) - min(lows)) if highs else 0.0
    return chg(1), chg(5), chg(13), span


def _point(symbol: str) -> float:
    try:
        from backtest.symbols import SYMBOLS

        return float((SYMBOLS.get(symbol) or {}).get("point") or 0.001)
    except Exception:  # noqa: BLE001
        return 0.001


def _spread_cost(symbol: str, lots: float) -> float:
    try:
        from backtest.symbols import spread_cost_usd

        return float(spread_cost_usd(symbol, lots) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _ticks_text(cfg: Cfg, bars: list[dict]) -> str:
    """把 tick 聚合表准备好。拿不到就返回一句说明，绝不让 prompt 缺字段。"""
    if not cfg.ticks:
        return "（tick 数据已关闭，只能依据 K 线判断）"
    ticks = fetch_ticks(cfg, bars)
    return _fmt_tick_block(summarize_ticks(ticks, bars, _point(cfg.symbol)))


def build_entry_prompt(rows: list[dict], bid: float, ask: float,
                       symbol: str = "XAUUSDm", tf: str = "M1",
                       point: float = 0.001, stats: dict | None = None,
                       lots: float = 0.01, lessons: str = "",
                       ticks_text: str = "", zone_edge: float = 0.35) -> str:
    w = visible_bars(rows)
    r1, r5, r13, span = _returns(w)
    st = stats or {}
    n_closed = int(st.get("closed") or 0)
    if n_closed:
        hit = f"{st.get('wins', 0) / n_closed:.0%}"
    else:
        hit = "暂无（这是头几笔）"

    spread_pts = int(round((ask - bid) / point)) if point else 0
    clock = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    return ENTRY_PROMPT.format(
        symbol=symbol, clock=clock, bid=bid, ask=ask, spread_pts=spread_pts,
        n=len(w), tf=tf, bars=_fmt_bar_lines(w),
        structure=_structure_text(w, zone_edge),
        r1=r1, r5=r5, r13=r13, span=span,
        n_closed=n_closed, hit=hit,
        lessons=lessons or "暂无复盘笔记。",
        ticks=ticks_text or "（tick 数据暂不可用，只能依据上面的 K 线判断）",
    )


def build_exit_prompt(rows: list[dict], position: dict, bid: float, ask: float,
                      tf: str = "M1", contract: float = 100.0,
                      ticks_text: str = "", symbol: str = "XAUUSDm",
                      lessons: str = "", zone_edge: float = 0.35) -> str:
    w = visible_bars(rows)
    r1, r5, r13, _ = _returns(w)
    direction = int(position.get("direction") or 1)
    entry = float(position.get("price_open") or 0.0)
    lots = float(position.get("lots") or 0.0)
    cur = bid if direction > 0 else ask  # 平仓价：多头看 bid，空头看 ask
    pnl = (cur - entry) * direction * contract * lots

    held = int(time.time()) - int(position.get("opened_at") or time.time())
    held_text = f"{held // 60} 分 {held % 60} 秒" if held >= 60 else f"{max(held, 0)} 秒"
    pnl_text = f"{pnl:+.2f} USD（{cur - entry:+.3f}）"

    return EXIT_PROMPT.format(
        symbol=symbol, clock=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        direction_text="多" if direction > 0 else "空",
        lots=lots, entry=entry, held_text=held_text,
        bid=bid, ask=ask, pnl_text=pnl_text,
        entry_reason=(position.get("entry_reason") or "未记录")[:120],
        n=len(w), tf=tf, bars=_fmt_bar_lines(w),
        structure=_structure_text(w, zone_edge),
        r1=r1, r5=r5, r13=r13,
        lessons=lessons or "暂无复盘笔记。",
        ticks=ticks_text or "（tick 数据暂不可用，只能依据上面的 K 线判断）",
    )


# ------------------------------------------------------------------ 平仓复盘
#
# 允许 AI 在**平仓之后**回看「平仓前 13 根 → 开仓之前」。
# 为什么这时候可以多看：仓位已经没了，回看不可能改变这一笔的结果，
# 只影响下一笔。信息边界是防止它偷看未来做决策，不是禁止它学习。


def review_window(rows: list[dict], close_ts: int, tf_sec: int = 60,
                  before: int = REVIEW_BEFORE_BARS,
                  after: int = REVIEW_AFTER_BARS) -> list[dict]:
    """复盘窗口：平仓前 before 根 + 平仓这根 + 平仓后 after 根 = 15 根。

    起点必须从**平仓那根 K 线的实际时间**往前推，不能拿平仓时刻去对齐 60 取整：
    MT5 的 K 线时间戳不保证是整分钟的（服务器时区偏移可能是 20 秒这类），
    按 60 取整会切掉半根，15 根变 14 根。

    持仓超过 before 根（>7 分钟）时，开仓那一段就不在窗口里了——开仓价、
    开仓理由在正文里，K 线只负责看清出场前后的走势。实测持仓是秒级，
    这个边界基本碰不到。
    """
    if not rows:
        return []
    tf_sec = int(tf_sec or 60)
    close_ts = int(close_ts)
    idx = None
    for i, r in enumerate(rows):
        if int(r.get("time") or 0) <= close_ts:
            idx = i                       # 最后一个不晚于平仓时刻的，就是平仓那根
    if idx is None:                       # 数据比平仓时刻还新，说明历史没取到
        return []
    close_bar = int(rows[idx].get("time") or 0)
    start = close_bar - max(0, int(before)) * tf_sec
    end = close_bar + max(0, int(after)) * tf_sec
    return [r for r in rows if start <= int(r.get("time") or 0) <= end]


def _fmt_review_bars(window: list[dict], close_ts: int) -> str:
    """复盘用的 K 线表，额外标出「哪根是平仓那根」。

    不标的话模型看到 20 根只会笼统点评，分不清"平仓后走势"和"持仓中走势"，
    复盘就会退化成"这笔赚了/亏了，下次注意"这种废话。
    """
    if not window:
        return ""
    idx = 0
    for i, r in enumerate(window):
        if int(r.get("time") or 0) <= int(close_ts):
            idx = i                        # 最后一个不晚于平仓时刻的，就是平仓那根
    lines = _fmt_bar_lines(window).splitlines()
    lines[idx] = lines[idx] + "  ← 平仓"
    return "\n".join(lines) + ("\n（「← 平仓」之后的是平仓后的走势）"
                               if idx < len(lines) - 1 else "")


def build_review_prompt(rows: list[dict], position: dict, exit_price: float,
                        pnl: float, held_sec: int, tf: str = "M1",
                        close_ts: int | None = None,
                        after: int = REVIEW_AFTER_BARS) -> str:
    direction = int(position.get("direction") or 1)
    held_text = f"{held_sec // 60} 分 {held_sec % 60} 秒" if held_sec >= 60 else f"{max(held_sec, 0)} 秒"
    close_ts = int(close_ts if close_ts is not None else (rows[-1].get("time") if rows else 0))
    return REVIEW_PROMPT.format(
        direction_text="多" if direction > 0 else "空",
        lots=float(position.get("lots") or 0.0),
        entry=float(position.get("price_open") or 0.0),
        exit_price=exit_price, pnl=pnl, held_text=held_text,
        why_in=(position.get("entry_reason") or "未记录")[:120],
        why_out=(position.get("exit_reason") or "外部平仓")[:120],
        before=REVIEW_BEFORE_BARS, after=after, n=len(rows), tf=tf,
        bars=_fmt_review_bars(rows, close_ts),
    )


def ask_review(cfg: Cfg, position: dict, exit_price: float, pnl: float,
               opened_at: int, closed_at: int, login=None) -> dict:
    """平仓满 7 根之后要一句复盘。失败就返回空，绝不影响主流程。"""
    tf_sec = 60
    held_sec = max(0, int(closed_at) - int(opened_at or closed_at))
    # 要覆盖「平仓前 7 根 + 平仓后 7 根」，再留几根余量。
    # 还要覆盖「平仓到现在」——复盘可能被延迟跑（进程重启、面板关过），
    # 只按持仓时长要 K 线的话，隔半小时再复盘会拿到一段不含平仓时刻的数据，
    # 切片直接为空，白跑一次。
    span = REVIEW_BEFORE_BARS + REVIEW_AFTER_BARS + 4
    back_sec = max(0, int(time.time()) - (int(closed_at) - REVIEW_BEFORE_BARS * tf_sec))
    need = min(1200, max(VISIBLE_BARS + span, held_sec // tf_sec + span,
                         back_sec // tf_sec + 4))
    rows = _bars(cfg.symbol, cfg.tf, need)
    window = review_window(rows, closed_at, tf_sec)
    if not window:
        return {"ok": False, "error": "复盘区间没有 K 线数据"}

    prompt = build_review_prompt(window, position, exit_price, pnl, held_sec,
                                 cfg.tf, close_ts=closed_at,
                                 after=REVIEW_AFTER_BARS)
    try:
        text, usage = _chat(prompt, REVIEW_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        _log_call(False, None, f"{type(exc).__name__}: {exc}", login,
                  purpose="autotrade_review")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _log_call(True, usage, login=login, purpose="autotrade_review")
    lesson = " ".join((text or "").split())[:120]
    if not lesson:
        return {"ok": False, "error": "模型复盘返回空"}
    # 给这条教训打上「当时是什么行情」的标签：震荡里攒的教训，
    # 下次震荡时优先拿出来。**只用平仓前那一段算 ER**——平仓后的 7 根
    # 是给复盘判断用的，不属于这笔单子进场时面对的行情，混进来会串味。
    pre_close = [r for r in window if int(r.get("time") or 0) <= int(closed_at)]
    return {"ok": True, "lesson": lesson, "bars": len(window),
            "regime": regime_of(pre_close)}


def _norm_lesson(text: str) -> str:
    """去掉数字和标点，只留汉字与字母。

    "点差 240 点" 和 "点差 250 点" 是同一类教训，不归一化就会被当成两条，
    3 个名额立刻被同义句占满。
    """
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z]", "", text or "")


# 教训的主题词典。按「特征词特异性」从强到弱排，一条教训只归第一个匹配上的主题。
#
# 为什么不用语义相似度：实测过字符二元组 Jaccard，14 条复盘里多数讲的是
# "别逆势抢反弹"，但措辞各不相同（"逆势接刀""抢反抽""反弹末端追多"），
# 相似度全部低于阈值，聚成了 14 类——等于没聚。
# 为什么不允许多标签：试过，像"自相矛盾…光点差就吃掉0.18"这种顺带提了点差的句子，
# 会同时计入「方向矛盾」和「点差」两类，还因为最新而抢走「点差类」的代表位，
# 计数虚高、代表句串味。一题一类才能说清"14 条复盘里有几条是同一类问题"。
LESSON_THEMES: list[tuple[str, tuple[str, ...]]] = [
    ("方向与依据矛盾", ("自相矛盾", "依据相", "说不通")),
    ("逆势抢反弹/接刀", ("逆势", "接刀", "抄底", "摸顶", "抢反")),
    ("追破位/假突破", ("追破", "追空", "追多", "追单", "追高", "假突破", "插针")),
    ("进场时点不对", ("太晚", "太早", "止跌", "确认再", "等回踩", "等反抽", "等确认")),
    ("点差/成本过高", ("点差", "成本", "吃掉", "吃光", "吞掉")),
]


def lesson_theme(text: str) -> str:
    """归一个主题；都不匹配返回空串（归到"其他"）。"""
    for name, kws in LESSON_THEMES:
        if any(k in text for k in kws):
            return name
    return ""


def lesson_clusters(login=None, limit: int = 400) -> list[dict]:
    """把复盘教训按主题归档并计次，按出现次数排序。

    每个桶额外记 `regimes`：这条教训分别是在哪种行情状态下犯的
    （"range"/"trend"/"mid"，旧数据没有标签归 ""=通用）。
    """
    rows = [r for r in read_log(login, limit=limit)
            if r.get("kind") == "review" and r.get("lesson")]

    buckets: dict[str, dict] = {}
    for r in rows:
        text = r["lesson"]
        ts = int(r.get("ts") or 0)
        name = lesson_theme(text)
        key = name or ("其他:" + _norm_lesson(text)[:24])
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"name": name, "count": 0, "lesson": text,
                                "ts": ts, "regimes": {}}
        b["count"] += 1
        rg = r.get("regime") or ""
        b["regimes"][rg] = b["regimes"].get(rg, 0) + 1
        if ts >= b["ts"]:          # 用最新的一条作为代表表述
            b["ts"] = ts
            b["lesson"] = text

    out = list(buckets.values())
    # 两趟稳定排序：先按时间倒序，再按次数倒序 ⇒ 次数优先，同次数新的在前
    out.sort(key=lambda c: c["ts"], reverse=True)
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


# 出现次数达到这个值的主题升级为「铁律」：从"参考教训"变成"先自查再动手"的禁令。
# 定 5 的依据：实测 16 笔里「追破位」犯了 7 次还在犯——到 5 次说明提示词里
# 普通一行字的剂量已经压不住这个错误，必须换更重的说法。
HARD_RULE_MIN = 5


def recent_lessons(login=None, n: int = 3, ignore_cost: bool = True,
                   regime: str = "") -> str:
    """取最反复出现的几条教训，塞进提示词（进场和出场都用）。

    为什么按次数排序而不是按时间：
    复盘写了 14 条、多数在说"别逆势抢反弹"，那它就是真规律；
    只取最近 3 条（旧写法）会把这十几次重复的信息量全部丢掉。

    以前这里写"仅供参考，样本很少，别当规律"——等于自己把经验废掉，
    模型当然照犯。现在只给次数，让重复本身说话。

    `ignore_cost=True` 时剔掉「点差/成本过高」这一类：用户要求无视成本，
    再把"点差太高别做"喂进去，等于左手说无障碍右手设路障。

    `regime` 是当前行情状态（"range"/"trend"/"mid"）：同状态下犯过的
    教训排前面。**软匹配，不排除其它状态**——16 条复盘切成 3 桶每桶才
    几条，硬排除会经常没东西可注。没打标签的旧教训（regime=""）任何
    状态都参与排序。

    count >= HARD_RULE_MIN 的主题升级为「铁律」，单独成块放最前面，
    不占 n 个普通名额。
    """
    clusters = lesson_clusters(login)
    if ignore_cost:
        clusters = [c for c in clusters
                    if not any(k in (c.get("name") or "") for k in ("点差", "成本"))]
    if not clusters:
        return "暂无复盘笔记。"

    if regime:
        # 同状态优先的两趟稳定排序：先按总次数，再按当前状态次数。
        # 没标签（""）的旧教训 regimes 里没有这个键，按 0 计、自然靠后，
        # 但仍会因为总次数高而补位进来——这就是软匹配。
        clusters = sorted(clusters, key=lambda c: c["count"], reverse=True)
        clusters.sort(key=lambda c: c["regimes"].get(regime, 0), reverse=True)

    hard = [c for c in clusters if c["count"] >= HARD_RULE_MIN]
    soft = [c for c in clusters if c["count"] < HARD_RULE_MIN][:n]

    blocks = []
    if hard:
        lines = []
        for c in hard:
            name = c["name"] or "反复犯的错误"
            lines.append(f"- 【铁律 · 已犯 {c['count']} 次】{name}：{c['lesson']}")
        blocks.append("铁律（以下错误你已各犯多次，先自查这一笔是不是又要犯，"
                      "是就直接不做/立刻平，再考虑别的）：\n"
                      # 教训文本是模型自己写的，天然偏空（空单亏得多），
                      # 不加这句它会把"别追破位/别接刀"读成"只能做多"。
                      "（这些禁令对多空同等生效。不要把「不追空」读成「只能做多」，"
                      "也不要把「不接多」读成「只能做空」。）\n" + "\n".join(lines))

    if soft:
        lines = []
        for c in soft:
            same = c["regimes"].get(regime, 0) if regime else 0
            tag = f"，本状态 {same} 次" if same else ""
            if c["count"] >= 2:
                head = (f"[{c['name']} · 已出现 {c['count']} 次{tag}]" if c["name"]
                        else f"[已出现 {c['count']} 次{tag}]")
            else:
                head = (f"[{c['name']} · 只出现 1 次，证据弱]" if c["name"]
                        else "[只出现 1 次，证据弱]")
            lines.append(f"- {head} {c['lesson']}")
        blocks.append("你从自己的交易里总结出的教训，按重复次数排序（次数越多越可能是"
                      "真规律，只出现一次的别当铁律）：\n" + "\n".join(lines))

    return "\n\n".join(blocks) if blocks else "暂无复盘笔记。"


def _exit_price(pos: dict, bid: float, ask: float) -> float:
    """平仓成交价：多头平在 bid，空头平在 ask。"""
    return bid if int(pos.get("direction") or 1) > 0 else ask


def _do_review(cfg: Cfg, pos: dict, exit_price: float, pnl: float,
               closed_at: int, login=None) -> dict | None:
    """跑一条复盘并落盘。`closed_at` 是平仓时刻，不是现在——
    调用方（process_due_reviews）已经保证它到期了。失败也只记一行，不能影响主流程。"""
    if not cfg.review:
        return None
    closed_at = int(closed_at)
    try:
        res = ask_review(cfg, pos, exit_price, pnl,
                         int(pos.get("opened_at") or closed_at), closed_at, login)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    row = {"ts": int(time.time()), "kind": "review", "closed_at": closed_at,
           "ticket": pos.get("ticket"),
           "direction": pos.get("direction"), "pnl": round(pnl, 2)}
    if res.get("ok"):
        row["lesson"] = res.get("lesson")
        row["bars"] = res.get("bars")
        row["regime"] = res.get("regime") or ""
    else:
        row["error"] = res.get("error")
    append_log(row, login)
    return row


def review_due_ts(item: dict) -> int:
    """这条待复盘什么时候到期：平仓时刻 + 7 根 K 线。"""
    return int(item.get("closed_at") or 0) + REVIEW_AFTER_BARS * 60


def queue_review(state: dict, cfg: Cfg, pos: dict, exit_price: float,
                 pnl: float, closed_at: int) -> dict:
    """把刚平掉的一笔排进复盘队列，等 7 根 K 线之后再复盘。

    以前是平仓当场就复盘，问题是那时候"平仓后走势"还不存在，复盘只能
    退化成"赚了/亏了，下次注意"。等 7 根之后，模型能看见平仓后行情往哪走，
    才判断得出「止盈太早」还是「跑得及时」。
    """
    if not cfg.review:
        return state
    pending = list(state.get("pending_reviews") or [])
    pending.append({
        "ticket": pos.get("ticket"),
        "direction": pos.get("direction"),
        "lots": pos.get("lots"),
        "price_open": pos.get("price_open"),
        "entry_reason": pos.get("entry_reason"),
        "exit_reason": pos.get("exit_reason"),
        "opened_at": pos.get("opened_at"),
        "closed_at": int(closed_at),
        "exit_price": float(exit_price),
        "pnl": round(float(pnl), 2),
    })
    state["pending_reviews"] = pending[-20:]     # 队列上限，防止异常情况无限涨
    return state


def process_due_reviews(cfg: Cfg, now: int, login=None) -> list[dict]:
    """把到期的复盘跑掉，跑完从队列里删。**不挡交易**：调用方做完它继续判断行情。

    必须在 tick 里 load_state 之前调用——它自己读写 state，如果放在后面，
    那一轮末尾的 save_state 会把已经复盘掉的条目又写回去，同一条反复复盘。
    """
    state = load_state(login)
    pending = list(state.get("pending_reviews") or [])
    if not pending:
        return []
    now = int(now)
    due = [p for p in pending if review_due_ts(p) <= now]
    if not due:
        return []

    done = []
    for item in due:
        pos = {"ticket": item.get("ticket"), "direction": item.get("direction"),
               "lots": item.get("lots"), "price_open": item.get("price_open"),
               "entry_reason": item.get("entry_reason"),
               "exit_reason": item.get("exit_reason"),
               "opened_at": item.get("opened_at")}
        row = _do_review(cfg, pos, float(item.get("exit_price") or 0.0),
                         float(item.get("pnl") or 0.0),
                         int(item.get("closed_at") or now), login)
        if row is not None:
            done.append(row)

    state["pending_reviews"] = [p for p in pending if review_due_ts(p) > now]
    save_state(state, login)
    return done


# ------------------------------------------------------------------ 调模型

# JSON mode 一旦失败就永久关掉：每次决策都白打一次失败请求太浪费。
_JSON_MODE_OK = True


def _chat(prompt: str, system: str):
    """调模型。优先要求 JSON mode（省输出 token、免解析失败），不支持就退回。"""
    global _JSON_MODE_OK
    if _JSON_MODE_OK:
        try:
            return _llm().chat_ex(prompt, system=system, json_mode=True)
        except TypeError:
            _JSON_MODE_OK = False       # 老版本 chat_ex 没这个参数
        except Exception:  # noqa: BLE001
            _JSON_MODE_OK = False       # 模型不支持 response_format
    return _llm().chat_ex(prompt, system=system)


def _log_call(ok: bool, usage: dict | None, error: str | None = None,
              login: int | str | None = None, purpose: str = "autotrade") -> None:
    """和 live/advisor.py 记在同一份 api_calls.jsonl 里，花费可以一起看。"""
    p = paths.account_file(paths.resolve_login(login), "api_calls.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": int(time.time()),
        "provider": "deepseek",
        "purpose": purpose,
        "login": paths.resolve_login(login),
        "ok": ok,
        "usage": usage or {},
    }
    if error:
        row["error"] = error
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def ask_entry(rows: list[dict], bid: float, ask: float, cfg: Cfg,
              stats: dict | None = None, login: int | str | None = None,
              ticks_text: str = "") -> dict:
    """让模型给上涨概率。**模型没答上来就不做**，不拿规则替它下单。"""
    prompt = build_entry_prompt(rows, bid, ask, cfg.symbol, cfg.tf,
                                _point(cfg.symbol), stats,
                                lots=cfg.lots,
                                lessons=recent_lessons(
                                    login, ignore_cost=cfg.ignore_spread,
                                    regime=regime_of(rows)),
                                zone_edge=cfg.zone_edge,
                                ticks_text=ticks_text)
    try:
        text, usage = _chat(prompt, ENTRY_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        _log_call(False, None, f"{type(exc).__name__}: {exc}", login)
        return _entry_skip(f"模型调用失败（{type(exc).__name__}: {exc}）")

    _log_call(True, usage, login=login)
    parsed = parse_p_up(text)
    if parsed is None:
        return _entry_skip(f"模型输出无法解析: {text[:120]}")
    parsed["source"] = "llm"
    return parsed


def _entry_skip(why: str) -> dict:
    """进场端拿不到模型结论时的统一答复：不做。

    以前这里退回 `fallback_p_up`（最近 10 根动量算个概率），它照样能过闸门、
    真开单——等于模型没参与也在下单。换成免费 API 之后，限速 429 会频繁触发，
    让一条三行的动量规则替我下真单，风险远大于少做一笔。
    """
    return {"act": False, "p_up": 0.5, "source": "error",
            "reason": f"{why}，本轮不做", "error": why}


def ask_exit(rows: list[dict], position: dict, bid: float, ask: float,
             cfg: Cfg, contract: float = 100.0,
             login: int | str | None = None, ticks_text: str = "") -> dict:
    prompt = build_exit_prompt(rows, position, bid, ask, cfg.tf, contract,
                               ticks_text=ticks_text, symbol=cfg.symbol,
                               lessons=recent_lessons(
                                   login, ignore_cost=cfg.ignore_spread,
                                   regime=regime_of(rows)),
                               zone_edge=cfg.zone_edge)
    held = int(time.time()) - int(position.get("opened_at") or time.time())
    try:
        text, usage = _chat(prompt, EXIT_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        _log_call(False, None, f"{type(exc).__name__}: {exc}", login)
        return _exit_hold(f"模型调用失败（{type(exc).__name__}: {exc}）")

    _log_call(True, usage, login=login)
    parsed = parse_exit(text)
    if parsed is None:
        return _exit_hold(f"模型输出无法解析: {text[:120]}")
    parsed["source"] = "llm"
    return parsed


def _exit_hold(why: str) -> dict:
    """出场端拿不到模型结论时：继续持有。

    出场端不兜底平仓——"不知道"就乱平会把点差白送出去；但连续失败要停线程，
    否则仓位会一直挂着没人管（见 tick 里的 model_errors 计数）。
    """
    return {"close": False, "source": "error",
            "reason": f"{why}，本轮继续持有", "error": why}


# ------------------------------------------------------------------ 开仓闸门
#
# 抽成纯函数，是为了能在没有 MT5 的机器上把「单笔 / 冷却 / 刹车」这三条
# 规则直接测掉。这几条是用户明确指定的，写错了整个模块就没意义。


# 同一次 tick 里重复开仓的兜底间隔。**不是**交易节奏——
# "平仓后 1 分钟内必须重新开仓"这条要求下，用 interval_sec(60) 当守卫
# 会把重开直接顶到 60 秒开外，自己把自己锁死。真正的间隔由 cooldown_sec 管。
MIN_OPEN_GAP_SEC = 3


def can_open(state: dict, now: int, has_position: bool, cfg: Cfg) -> tuple[bool, str]:
    if state.get("stop_reason"):
        return False, f"已停止：{state['stop_reason']}"
    if has_position:
        return False, "品种上已有持仓，不叠加"

    if cfg.max_trades and int(state.get("trades") or 0) >= cfg.max_trades:
        return False, f"已达开仓上限 {cfg.max_trades} 笔"

    if cfg.max_loss_usd and float(state.get("pnl") or 0.0) <= -abs(cfg.max_loss_usd):
        return False, f"累计亏损已达 {cfg.max_loss_usd:.2f} USD 上限"

    last_close = state.get("last_close_ts")
    if last_close:
        left = cfg.cooldown_sec - (now - int(last_close))
        if left > 0:
            return False, f"平仓后冷却中，还需 {left} 秒"

    last_open = state.get("last_open_ts")
    if last_open and now - int(last_open) < MIN_OPEN_GAP_SEC:
        left = MIN_OPEN_GAP_SEC - (now - int(last_open))
        return False, f"距上次开仓不足 {MIN_OPEN_GAP_SEC} 秒，还需 {left} 秒"

    return True, "可开仓"


# ------------------------------------------------------------------ 终端读写


def _positions(symbol: str) -> list[dict] | None:
    """该品种的持仓。**读失败返回 None，不是空列表**。

    这个区分很关键：返回 [] 表示"确实没持仓"，返回 None 表示"不知道"。
    如果读失败也返回 []，模块会误判「刚才那笔已经平了」，然后马上再开一笔——
    终端抖一下就能变成连续开仓。
    """
    try:
        from live.journal import open_positions

        rows = open_positions()
    except Exception:  # noqa: BLE001
        return None
    return [p for p in rows if str(p.get("symbol")) == symbol]


def _symbol_contract(symbol: str) -> float:
    try:
        import MetaTrader5 as mt5

        info = mt5.symbol_info(symbol)
        if info is not None and getattr(info, "trade_contract_size", 0):
            return float(info.trade_contract_size)
    except Exception:  # noqa: BLE001
        pass
    try:
        from backtest.symbols import SYMBOLS

        return float((SYMBOLS.get(symbol) or {}).get("contract") or 100.0)
    except Exception:  # noqa: BLE001
        return 100.0


def _quote(symbol: str) -> tuple[float, float] | None:
    # 走 journal 的连接保活，不要直接 mt5.symbol_info_tick：
    # 没 initialize 过的话它只会返回 None，看起来像行情断了。
    try:
        from live.journal import _mt5

        tick = _mt5().symbol_info_tick(symbol)
        if tick is None:
            return None
        return float(tick.bid), float(tick.ask)
    except Exception:  # noqa: BLE001
        return None


def _bars(symbol: str, tf: str, count: int) -> list[dict]:
    # 用 get_rates_live：不 initialize、不 shutdown。
    # 老写法 mt5_session() 会在退出时 shutdown，把整个面板进程的连接一起关掉，
    # 下一轮就只剩「拿不到报价」。
    try:
        from mt5.bridge import get_rates_live

        return get_rates_live(symbol, tf, count)
    except Exception:  # noqa: BLE001
        return []


def _executor(cfg: Cfg):
    from execution.live import ExecConfig, Executor

    # keep_alive：面板里的做单线程不能每发一单就把终端连接关掉
    return Executor(ExecConfig(symbol=cfg.symbol, magic=cfg.magic,
                               dry_run=cfg.dry_run, keep_alive=True))


# ------------------------------------------------------------------ 开仓前置体检
#
# 真单失败十次有九次不是代码问题，而是终端那个「算法交易」按钮没点开
# （retcode 10027 AutoTrading disabled by client）。不提前查的话，
# 用户看到的只是一笔笔「开仓失败」，完全不知道要去点按钮。


_RETCODE_HINT = {
    10027: "MT5 终端的「算法交易」按钮没打开（工具栏那个 AutoTrading 开关）",
    10019: "可用保证金不足",
    10018: "市场已关闭（休市或该品种当前不可交易）",
    10030: "券商不支持请求里的成交方式",
    10014: "手数不合法（可能小于最小手数或精度不对）",
    10013: "请求参数无效",
    10009: "成交成功",
}


def retcode_hint(rc: int | None) -> str | None:
    return _RETCODE_HINT.get(int(rc)) if rc is not None else None


def precheck(cfg: Cfg) -> dict:
    """发真单之前的体检。纸面模式直接放行。"""
    if cfg.dry_run:
        return {"ok": True, "errors": []}

    issues: list[str] = []
    try:
        from execution.live import ExecConfig, Executor

        ex = Executor(ExecConfig(symbol=cfg.symbol, magic=cfg.magic,
                                 dry_run=False, keep_alive=True))
        if not ex.connect():
            issues.append("MT5 连接失败（终端没开或没登录）")
        else:
            allowed, why = ex.check_trading_allowed()
            if not allowed:
                issues.append(why)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"{type(exc).__name__}: {exc}")
    return {"ok": not issues, "errors": issues}


def send_open(cfg: Cfg, direction: int) -> dict:
    """市价开仓，**不挂 SL/TP**。失败时返回 ok=False，不抛异常。"""
    ex = _executor(cfg)
    try:
        if not ex.connect():
            return {"ok": False, "reason": "MT5 连接失败"}
        res = ex.open_market(direction=direction, lots=cfg.lots, sl=None, tp=None,
                             comment=cfg.comment)
        if not res.get("ok"):
            # 把 retcode 翻成人话，别让「10027」这种数字直接甩到面板上
            hint = retcode_hint(res.get("retcode"))
            res["reason"] = hint or res.get("comment") or f"retcode={res.get('retcode')}"
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            ex.shutdown()
        except Exception:  # noqa: BLE001
            pass


def send_close(cfg: Cfg, ticket: int) -> dict:
    ex = _executor(cfg)
    try:
        if not ex.connect():
            return {"ok": False, "reason": "MT5 连接失败"}
        res = ex.close_position(ticket)
        if not res.get("ok"):
            hint = retcode_hint(res.get("retcode"))
            res["reason"] = hint or res.get("comment") or f"retcode={res.get('retcode')}"
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            ex.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 一次 tick


def _stop(state: dict, reason: str, now: int, login=None) -> dict:
    state["stop_reason"] = reason
    state["running"] = False
    state["last_tick_ts"] = now
    save_state(state, login)
    row = {"ts": now, "kind": "stop", "reason": reason}
    append_log(row, login)
    return {"kind": "stop", "stop": True, "reason": reason}


def tick(cfg: Cfg, login=None, now: int | None = None,
         quote: tuple[float, float] | None = None,
         rows: list[dict] | None = None,
         positions: list[dict] | None = None) -> dict:
    """跑一轮决策。所有外部依赖都可注入，方便在没有终端的机器上测。"""
    now = int(now or time.time())

    # 到期的复盘先跑掉（平仓后 7 根 K 线才到期）。必须在 load_state 之前：
    # 它自己读写 state，放在后面会被这一轮末尾的 save_state 覆盖回旧队列，
    # 同一条复盘就会反复重跑。
    process_due_reviews(cfg, now, login)

    state = load_state(login)

    if state.get("stop_reason"):
        return {"kind": "stop", "stop": True, "reason": state["stop_reason"]}

    # --- 刹车：跑飞了自动停 -------------------------------------------------
    if cfg.max_minutes and state.get("started_at"):
        if now - int(state["started_at"]) >= cfg.max_minutes * 60:
            return _stop(state, f"到达运行上限 {cfg.max_minutes} 分钟", now, login)
    if cfg.max_loss_usd and float(state.get("pnl") or 0.0) <= -abs(cfg.max_loss_usd):
        return _stop(state, f"累计亏损 {state['pnl']:.2f} USD 触发上限", now, login)

    # --- 行情 ------------------------------------------------------------
    q = quote if quote is not None else _quote(cfg.symbol)
    if not q:
        row = {"ts": now, "kind": "skip", "reason": "拿不到报价"}
        append_log(row, login)
        state["last_tick_ts"] = now
        save_state(state, login)
        return row
    bid, ask = q

    bars = rows if rows is not None else _bars(cfg.symbol, cfg.tf, cfg.bars)

    pos = state.get("position")
    # 纸面仓位只存在于 state 里，券商那边什么都没有。拿它去和 MT5 持仓比对
    # 会在下一轮立刻判成「已平仓」，然后每轮开一笔再"平"一笔——
    # 纸面模式下必须完全跳过终端比对。
    paper = bool(pos and pos.get("dry_run"))

    if paper:
        live_positions = None
    else:
        live_positions = positions if positions is not None else _positions(cfg.symbol)
        if live_positions is None:
            row = {"ts": now, "kind": "skip", "reason": "读不到持仓（MT5 未连接？），本轮不动"}
            state["last_tick_ts"] = now
            append_log(row, login)
            save_state(state, login)
            return row

    # --- 持仓已经没了（AI 平的 / 你手动平的 / 券商砍的）---------------------
    if pos and not paper and not any(
        int(p.get("ticket") or 0) == int(pos.get("ticket") or -1) for p in live_positions
    ):
        pnl = _realized_pnl(cfg, pos, bid, ask)
        state["closed"] = int(state.get("closed") or 0) + 1
        if pnl > 0:
            state["wins"] = int(state.get("wins") or 0) + 1
        state["pnl"] = round(float(state.get("pnl") or 0.0) + pnl, 2)
        state["last_close_ts"] = now
        state["position"] = None
        row = {
            "ts": now, "kind": "close", "ticket": pos.get("ticket"),
            "direction": pos.get("direction"), "pnl": round(pnl, 2),
            "p_up": pos.get("p_up"), "hit": pnl > 0,
            "held_sec": now - int(pos.get("opened_at") or now),
            # 两个理由分别存：开仓理由来自当初的进场判断，平仓理由来自这次离场判断。
            # 合成一个字段就再也分不清「为什么进」和「为什么出」，复盘直接废掉。
            "entry_reason": pos.get("entry_reason"),
            "reason": "外部平仓：模型没发平仓单，仓位已在终端消失",
            "source": "external",
        }
        append_log(row, login)
        # 复盘排进队列：等 7 根 K 线走完再复盘，这期间照常判断下一笔。
        # 平仓当刻就复盘看不出「平完之后怎么走」，等于白问。
        pos["exit_reason"] = "外部平仓（模型没发平仓单）"
        queue_review(state, cfg, pos, _exit_price(pos, bid, ask), pnl, now)
        save_state(state, login)
        return {**row, "detected": True}

    # --- 有仓：先过程序闸门，再问模型该不该平 --------------------------------
    if pos:
        contract = _symbol_contract(cfg.symbol)
        held_sec = now - int(pos.get("opened_at") or now)
        float_pnl = _realized_pnl(cfg, pos, bid, ask)

        gate = _exit_gate(cfg, held_sec, float_pnl)

        # 闸门说不平：直接记 hold，不调模型（省 token，也断了"每轮问一次要不要跑"）
        if gate is not None and not gate.get("close"):
            row = {"ts": now, "kind": "hold", "reason": gate["reason"],
                   "source": gate["source"], "held_sec": held_sec,
                   "float_pnl": round(float_pnl, 2)}
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": False,
                "reason": gate["reason"], "source": gate["source"],
            }
            state["last_tick_ts"] = now
            append_log(row, login)
            save_state(state, login)
            return row

        if gate is not None:
            # 止损：程序直接平，不问模型，也不占用 model_errors 计数
            decision = gate
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": True,
                "reason": gate["reason"], "source": gate["source"],
            }
        else:
            decision = ask_exit(bars, pos, bid, ask, cfg, contract, login,
                                ticks_text=_ticks_text(cfg, bars))
            # 模型失败要计数：进场失败只是少做一笔，出场失败意味着仓位没人管，
            # 连错 3 次必须停下来让人接手，不能让它一直挂在那儿。
            if decision.get("source") == "error":
                state["model_errors"] = int(state.get("model_errors") or 0) + 1
            else:
                state["model_errors"] = 0
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": decision.get("close"),
                "reason": decision.get("reason"), "source": decision.get("source"),
            }
            if not decision.get("close"):
                row = {"ts": now, "kind": "hold", "reason": decision.get("reason"),
                       "source": decision.get("source")}
                if decision.get("error"):
                    row["error"] = decision["error"]
                state["last_tick_ts"] = now
                append_log(row, login)
                save_state(state, login)
                if int(state.get("model_errors") or 0) >= 3:
                    return _stop(state,
                                 f"模型连续 {state['model_errors']} 次调用失败，"
                                 "持仓无人管理，已自动停止（请回终端手动处理）",
                                 now, login)
                return row

        # 纸面模式没有真实的平仓单可发，直接本地结算。
        # 不去调 send_close：券商那边根本没这个 ticket，只会得到「持仓不存在」，
        # 然后仓位永远卡在 state 里，冷却也永远不触发。
        if cfg.dry_run:
            res = {"ok": True, "dry_run": True}
        else:
            res = send_close(cfg, int(pos["ticket"]))
        if not res.get("ok"):
            row = {"ts": now, "kind": "close_failed", "ticket": pos.get("ticket"),
                   "reason": decision.get("reason"), "error": res.get("reason")}
            state["last_tick_ts"] = now
            append_log(row, login)
            save_state(state, login)
            # 平不掉还继续每分钟调模型问一次，只会白花钱；
            # 仓位还在手上，必须停下来让人去终端处理。
            return _stop(state, f"平仓失败（{res.get('reason')}），已自动停止", now, login)

        pnl = _realized_pnl(cfg, pos, bid, ask)
        state["closed"] = int(state.get("closed") or 0) + 1
        if pnl > 0:
            state["wins"] = int(state.get("wins") or 0) + 1
        state["pnl"] = round(float(state.get("pnl") or 0.0) + pnl, 2)
        state["last_close_ts"] = now
        state["position"] = None
        row = {
            "ts": now, "kind": "close", "ticket": pos.get("ticket"),
            "direction": pos.get("direction"), "pnl": round(pnl, 2),
            "p_up": pos.get("p_up"), "hit": pnl > 0,
            "held_sec": held_sec,
            "entry_reason": pos.get("entry_reason"),
            "reason": decision.get("reason"), "source": decision.get("source"),
            "dry_run": cfg.dry_run,
        }
        # 止损是程序发的，不是模型判断。必须能单独数出来，
        # 否则「模型平仓命中率」会被一堆止损单污染。
        if decision.get("source") == "sl":
            state["sl_closes"] = int(state.get("sl_closes") or 0) + 1
        if decision.get("error"):
            row["error"] = decision["error"]
        append_log(row, login)
        # 平仓之后才允许回看更长的一段，但**要等 7 根 K 线**才复盘：
        # 排进队列，这期间照常分析行情、照常开下一笔。
        pos["exit_reason"] = decision.get("reason") or ""
        queue_review(state, cfg, pos, _exit_price(pos, bid, ask), pnl, now)
        save_state(state, login)
        return row

    # --- 空仓：先看闸门，过不了就不花 API 的钱 -------------------------------
    ok, why = can_open(state, now, bool(live_positions), cfg)
    if not ok:
        row = {"ts": now, "kind": "skip", "reason": why}
        state["last_tick_ts"] = now
        append_log(row, login)
        save_state(state, login)
        return row

    # 程序建议方向：与喂给模型的位置判定同源（同 _structure + zone_edge），
    # 落盘后才能事后对账「程序对 vs 模型对」。
    prog_dir = _prog_direction(bars, cfg.zone_edge)
    decision = ask_entry(bars, bid, ask, cfg, state, login,
                         ticks_text=_ticks_text(cfg, bars))
    p_up = float(decision.get("p_up") if decision.get("p_up") is not None else 0.5)
    act = decision.get("act")
    direction = 1 if p_up > 0.5 else -1
    agree = decision.get("agree")
    state["last_decision"] = {
        "ts": now, "kind": "entry", "p_up": p_up, "direction": direction,
        "act": act, "prog_dir": prog_dir, "agree": agree,
        "reason": decision.get("reason"), "source": decision.get("source"),
    }

    # 模型明确说不做，就别做。以前只能靠 p_up≈0.5 暗示，结果常常
    # "理由写不追、数值 0.38 照开"——现在听它自己的。
    if act is False:
        failed = decision.get("source") == "error"
        row = {"ts": now, "kind": "skip", "p_up": p_up, "act": False,
               "reason": (decision.get("reason") if failed else
                          f"模型判定不做：{decision.get('reason') or '未给理由'}")}
        if failed:
            # 调用失败不等于"它主动放弃"，两者混在一起看会把故障当成保守
            row["source"] = "error"
            row["error"] = decision.get("error")
        state["last_tick_ts"] = now
        append_log(row, login)
        save_state(state, login)
        return row

    # 信号强度闸门。p_up 贴着 0.50 时模型自己也没把握，而开一单要先付 0.24 点差。
    # 5 秒周期下每分钟能出现十几次这种"微弱优势"，不挡住就是稳定亏损。
    edge = abs(p_up - 0.5)
    if cfg.min_edge > 0 and edge < cfg.min_edge:
        row = {"ts": now, "kind": "skip", "p_up": p_up,
               "reason": (f"信号不够强：p_up={p_up:.2f}，偏离 0.50 仅 {edge:.2f}，"
                          f"低于门槛 {cfg.min_edge:.2f}（开仓要先付点差）")}
        state["last_tick_ts"] = now
        append_log(row, login)
        save_state(state, login)
        return row

    res = send_open(cfg, direction)
    if not res.get("ok"):
        row = {"ts": now, "kind": "open_failed", "p_up": p_up, "direction": direction,
               "reason": decision.get("reason"), "error": res.get("reason")}
        state["last_tick_ts"] = now
        append_log(row, login)
        save_state(state, login)
        # 被券商拒绝（没开算法交易 / 保证金不足 / 休市 / 手数非法）时，
        # 下一分钟重试也只会同样被拒，还会再花一次模型调用。
        if res.get("retcode") in (10027, 10018, 10019, 10014, 10013):
            return _stop(state, f"开仓被拒：{res.get('reason')}，已自动停止", now, login)
        return row

    price = float(res.get("price") or (ask if direction > 0 else bid))
    # 纸面单券商不给 order 号，用负数编号，日志里一眼能看出这条不是真单
    if cfg.dry_run:
        ticket = -(int(state.get("trades") or 0) + 1)
    else:
        ticket = int(res.get("order") or 0)
    state["position"] = {
        "ticket": ticket, "direction": direction, "lots": cfg.lots,
        "price_open": price, "opened_at": now, "p_up": p_up,
        "entry_reason": decision.get("reason"), "dry_run": cfg.dry_run,
        # 分歧记账：程序建议方向 + 模型是否自称同意。
        # 事后按 (prog_dir, direction) 分格算期望，谁该拥有判断权让数据判。
        "prog_dir": prog_dir, "agree": agree,
    }
    state["trades"] = int(state.get("trades") or 0) + 1
    state["last_open_ts"] = now
    state["last_tick_ts"] = now

    row = {
        "ts": now, "kind": "open", "ticket": ticket, "direction": direction,
        "lots": cfg.lots, "price": price, "p_up": p_up,
        "confidence": round(max(p_up, 1 - p_up), 4),
        "prog_dir": prog_dir, "agree": agree,
        "reason": decision.get("reason"), "source": decision.get("source"),
        "dry_run": cfg.dry_run,
    }
    if decision.get("error"):
        row["error"] = decision["error"]
    append_log(row, login)
    save_state(state, login)
    return row


def _exit_gate(cfg: Cfg, held_sec: int, float_pnl: float) -> dict | None:
    """持仓期间的**程序闸门**。返回 dict = 本轮结论已定；返回 None = 交给模型判断。

    这个函数只有三行逻辑，但顺序是致命的：**止损必须排在止盈门槛之前**。
    反过来写的话，一笔浮亏 -5 USD 的单子会因为「浮盈没到 2.50」被判成继续持有，
    止损永远不触发——参数越保守亏得越多。所以单独抽出来 + 单测钉住顺序。

    三条规则的来源（2026-10-23 第 3 场 191 笔的账）：
      · 60 秒中位持仓 = min_hold 从下限变成了目标，150 笔 ≤90 秒平仓吃掉 90% 的亏损；
      · avgW 1.011 / avgL 1.026 → 盈亏比 0.99，扣掉每笔 0.24 点差和 35.6% 胜率，必亏；
      · 最大单笔 -6.08 是平均亏损的 5.9 倍，且它的 p_up=0.0（它最自信时亏最多）。
    """
    if cfg.sl_usd > 0 and float_pnl <= -abs(cfg.sl_usd):
        return {"close": True, "source": "sl",
                "reason": (f"硬止损：浮亏 {float_pnl:+.2f} 触及 -{abs(cfg.sl_usd):.2f}")
                }

    # 持仓上限排在止盈门槛之前：否则一笔卡在 +2.0（没到门槛 3.25）的单子
    # 可以一直挂到天荒地老，把「超短线」拖成隔夜单。
    if cfg.max_hold_sec > 0 and held_sec >= cfg.max_hold_sec:
        return {"close": True, "source": "timeout",
                "reason": (f"持仓超上限：已 {held_sec}s / 上限 {cfg.max_hold_sec}s，"
                           f"浮盈亏 {float_pnl:+.2f}，无条件市价平")
                }

    if cfg.tp_min_usd > 0:
        if float_pnl < cfg.tp_min_usd:
            return {"close": False, "source": "tp_wait",
                    "reason": (f"浮盈 {float_pnl:+.2f} 未达止盈门槛 "
                               f"{cfg.tp_min_usd:.2f}，继续持有（本轮不调模型）")
                    }
        return None   # 到门槛了，让模型决定平还是继续跑

    # tp_min_usd=0：回退到旧的最短持仓保护
    guard = abs(cfg.min_hold_exempt_usd)
    if cfg.min_hold_sec > 0 and held_sec < cfg.min_hold_sec and float_pnl > -guard:
        return {"close": False, "source": "min_hold",
                "reason": (f"最短持仓中：已 {held_sec}s / 需 {cfg.min_hold_sec}s，"
                           f"浮盈亏 {float_pnl:+.2f} USD（未触及 -{guard:.2f} 豁免线），"
                           f"本轮不调模型")
                }
    return None


def _realized_pnl(cfg: Cfg, pos: dict, bid: float, ask: float) -> float:
    """这一笔的净盈亏。优先读券商成交记录，读不到就用价差估。

    估算不含点差和佣金，只能看方向对不对；真实数字以 MT5 账本为准。
    """
    try:
        import MetaTrader5 as mt5

        deals = mt5.history_deals_get(position=int(pos.get("ticket") or 0)) or []
        if deals:
            total = sum(
                float(getattr(d, "profit", 0.0))
                + float(getattr(d, "commission", 0.0))
                + float(getattr(d, "swap", 0.0))
                + float(getattr(d, "fee", 0.0))
                for d in deals
                if int(getattr(d, "entry", 1)) == 1  # 1 = 平仓成交
            )
            if total:
                return round(total, 2)
    except Exception:  # noqa: BLE001
        pass

    direction = int(pos.get("direction") or 1)
    entry = float(pos.get("price_open") or 0.0)
    cur = bid if direction > 0 else ask  # 平仓价：多头看 bid，空头看 ask
    lots = float(pos.get("lots") or 0.0)
    if not entry or not lots:
        return 0.0
    return round((cur - entry) * direction * _symbol_contract(cfg.symbol) * lots, 2)


# ------------------------------------------------------------------ 主循环


def _fmt(row: dict) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(int(row.get("ts") or 0)))
    kind = row.get("kind")
    if kind == "open":
        arrow = "多" if int(row.get("direction") or 0) > 0 else "空"
        return (f"{clock} 开仓 {arrow} {row.get('lots')} @ {row.get('price')} "
                f"p_up={row.get('p_up')} [{row.get('source')}] {row.get('reason') or ''}")
    if kind == "close":
        return (f"{clock} 平仓 #{row.get('ticket')} 盈亏 {row.get('pnl'):+.2f} USD "
                f"[{row.get('source')}] {row.get('reason') or ''}")
    if kind == "hold":
        return f"{clock} 持有 {row.get('reason') or ''}"
    if kind == "skip":
        return f"{clock} 跳过：{row.get('reason')}"
    if kind == "stop":
        return f"{clock} 停止：{row.get('reason')}"
    return f"{clock} {kind} {row.get('reason') or row.get('error') or ''}"


def validate_cfg(cfg: Cfg) -> list[str]:
    """配置自检。返回问题列表，空 = 通过。

    这里放的是**逻辑上不可能成立**的配置：与其让它跑起来后悄悄违反规则，
    不如启动时就说清楚。
    """
    issues: list[str] = []
    # 3 秒是物理下限：一次模型调用实测 ~2.6 秒，再短线程就在堆请求，
    # 上一轮没回来下一轮已经开始，决策全挤在一起。
    if cfg.interval_sec < 3:
        issues.append(
            f"决策间隔不能小于 3 秒（模型单次调用约 2.6 秒，再快只会堆积请求）"
        )
    if cfg.lots <= 0:
        issues.append("手数必须大于 0")
    if cfg.cooldown_sec >= 60:
        issues.append(
            f"冷却 {cfg.cooldown_sec} 秒做不到「平仓后 1 分钟内重新开仓」，必须小于 60 秒"
        )
    # 止损/止盈这对参数是配套的，单独调一个只会换一种亏法：
    #   止盈门槛 ≤ 止损 → 盈亏比还在 1 附近甚至更差，白加止损；
    #   止损太接近点差 → 直接被点差打掉，等于每开一单就主动交一次过路费。
    if cfg.sl_usd > 0 and cfg.sl_usd < 0.5:
        issues.append(
            f"止损 {cfg.sl_usd:.2f} 太小：单笔点差就 0.24，"
            "止损不到 0.5 等于一开仓就被点差打掉"
        )
    if cfg.tp_min_usd > 0 and cfg.sl_usd > 0 and cfg.tp_min_usd <= cfg.sl_usd:
        issues.append(
            f"止盈门槛 {cfg.tp_min_usd:.2f} 不高于止损 {cfg.sl_usd:.2f}，"
            "盈亏比仍 ≤ 1，这套参数没解决必亏问题"
        )
    return issues


def next_wait_sec(row: dict, cfg: Cfg) -> float:
    """这一轮跑完，下一次决策该等多久。

    平仓后**不能等满一个周期**。需求是「一单做完，1 分钟内必须重新开仓」，
    而平仓后还要先过 `cooldown_sec`（默认 30 秒）才允许开：
    照旧等 60 秒的话，重新开仓直接落在 60 秒开外，条件当场违反。
    所以平仓后按「冷却 + 1 秒」唤醒——30 秒冷却满足，重新开仓约 31 秒，
    离 1 分钟还有一半余量，留够模型响应和下单的抖动空间。
    """
    if row.get("kind") == "close":
        return max(1.0, min(float(cfg.cooldown_sec + 1), float(cfg.interval_sec)))
    return float(cfg.interval_sec)


def run(cfg: Cfg, login=None) -> None:
    state = load_state(login)
    state["running"] = True
    state["started_at"] = state.get("started_at") or int(time.time())
    state["stop_reason"] = None
    state["owner"] = "cli"
    state["pid"] = os.getpid()
    state["cfg"] = cfg_to_dict(cfg)
    save_state(state, login)

    mode = "纸面（不发单）" if cfg.dry_run else "真实下单"
    print(f"AI 交易员启动 | {cfg.symbol} 超短线 {cfg.lots} 手 | 每 {cfg.interval_sec} 秒决策 | "
          f"平仓后 {cfg.cooldown_sec} 秒冷却、1 分钟内重开 | 不挂 SL/TP | {mode}")
    print("Ctrl-C 停止。状态与流水在账户目录下 autotrade_state.json / autotrade_log.jsonl")

    try:
        while True:
            started = time.time()
            row = tick(cfg, login=login)
            print(_fmt(row), flush=True)
            if row.get("stop"):
                break
            time.sleep(max(1.0, started + next_wait_sec(row, cfg) - time.time()))
    except KeyboardInterrupt:
        print("\n已手动中断。")
    finally:
        state = load_state(login)
        state["running"] = False
        save_state(state, login)


# ------------------------------------------------------------------ 一键清仓
#
# 面板上的「关闭并清仓」按钮背后是这个函数。它的语义很硬：**平掉账户上的
# 所有持仓**，不只是 AI 自己开的那些。理由很简单——按钮是用户自己按的，
# 他按这个按钮的时候想的是"全停下"，不是"停一半"。
# 但这也意味着它会动你手动开的单，所以面板那边必须带 confirm 才允许调用。


def _all_positions() -> list[dict] | None:
    """账户上所有持仓。读失败返回 None（含义同 `_positions`）。"""
    try:
        from live.journal import open_positions

        return open_positions()
    except Exception:  # noqa: BLE001
        return None


def close_all(cfg: Cfg, login=None, symbol_only: bool = False,
              why: str = "面板停止并清仓") -> dict:
    """平掉所有持仓并停掉 AI。返回每一笔的结果，失败的单独列出来。

    两种模式的语义不一样，别混：
      - 真实模式：向券商逐笔发平仓单，平的是**账户上所有持仓**（含手动单）。
      - 纸面模式：券商那边本来就没有我们的仓位，硬发单只会得到「持仓不存在」，
        然后仓位永远卡在 state 里。所以只在本地按当前价结算那一笔纸面仓，
        真实持仓一概不碰（也没资格碰）。
    """
    now = int(time.time())
    done: list[dict] = []
    failed: list[dict] = []
    untouched = 0

    if cfg.dry_run:
        untouched = len(_all_positions() or [])
    else:
        positions = _positions(cfg.symbol) if symbol_only else _all_positions()
        if positions is None:
            # 读不到持仓时必须明说「AI 停了但没平仓」，不能让用户以为清仓成功了
            state = load_state(login)
            state["running"] = False
            state["stop_reason"] = why
            save_state(state, login)
            append_log({"ts": now, "kind": "flatten",
                        "reason": f"{why}（失败：读不到持仓）"}, login)
            return {"ok": False, "closed": [], "failed": [], "count": 0,
                    "error": "读不到持仓（MT5 未连接？）。AI 已停止，但**没有平掉任何仓位**，"
                             "请回终端手动确认。"}

        for p in positions:
            ticket = int(p.get("ticket") or 0)
            res = send_close(cfg, ticket)
            item = {"ticket": ticket, "symbol": p.get("symbol"),
                    "direction": p.get("direction"), "ok": bool(res.get("ok"))}
            if res.get("ok"):
                done.append(item)
            else:
                item["error"] = res.get("reason")
                failed.append(item)

    state = load_state(login)
    pos = state.get("position")
    if pos:
        closed_tickets = {d["ticket"] for d in done}
        is_paper = bool(pos.get("dry_run"))
        if is_paper or int(pos.get("ticket") or 0) in closed_tickets:
            bid, ask = _quote(cfg.symbol) or (0.0, 0.0)
            # 拿不到报价时按 0 记，不能编一个价出来
            pnl = _realized_pnl(cfg, pos, bid, ask) if (bid and ask) else 0.0
            state["closed"] = int(state.get("closed") or 0) + 1
            if pnl > 0:
                state["wins"] = int(state.get("wins") or 0) + 1
            state["pnl"] = round(float(state.get("pnl") or 0.0) + pnl, 2)
            state["last_close_ts"] = now
            state["position"] = None
            append_log({
                "ts": now, "kind": "close", "ticket": pos.get("ticket"),
                "direction": pos.get("direction"), "pnl": round(pnl, 2),
                "p_up": pos.get("p_up"), "hit": pnl > 0,
                "held_sec": now - int(pos.get("opened_at") or now),
                "entry_reason": pos.get("entry_reason"),
                "reason": why, "source": "panel",
                "dry_run": is_paper,
            }, login)
            # 面板清仓这一笔也要复盘：AI 停掉后不会跑，队列留在 state 里，
            # 下次启动时 if 到期就跑（K 线还在，窗口按 closed_at 切片，不受影响）。
            pos["exit_reason"] = why
            queue_review(state, cfg, pos, _exit_price(pos, bid, ask), pnl, now)
            done.append({"ticket": pos.get("ticket"), "paper": is_paper,
                         "pnl": round(pnl, 2)})

    state["running"] = False
    state["stop_reason"] = why
    save_state(state, login)

    append_log({"ts": now, "kind": "flatten", "reason": why,
                "closed": len(done), "failed": len(failed),
                "untouched": untouched}, login)
    out = {"ok": not failed, "closed": done, "failed": failed,
           "count": len(done), "dry_run": cfg.dry_run}
    if cfg.dry_run and untouched:
        out["note"] = (f"纸面模式没有动券商上的 {untouched} 笔真实持仓；"
                       "要平真单请取消勾选「真实下单」后重新启动，或者直接去终端手动平。")
    return out


# ------------------------------------------------------------------ 面板控制线程
#
# 为什么放在 dashboard 进程内，而不是起一个独立后台进程：
# 跨进程管一个常驻任务要处理 pid 文件、僵尸进程、端口占用、日志重定向，
# 对一个单人用的复盘工具是过度设计。代价是**关掉面板 AI 就停**——
# 这恰好是想要的行为：看不见的东西不该在后台悄悄下单。
# 也因为这个，state 里必须记 owner/pid，否则面板重启后会把一条
# 早就死掉的 running=True 当成"还在跑"。


class Worker:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.cfg: Cfg | None = None
        self.login = None

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, cfg: Cfg, login=None) -> dict:
        if self.alive():
            return {"ok": False, "error": "已经在跑了，先点「关闭并清仓」再启动"}
        issues = validate_cfg(cfg)
        if issues:
            return {"ok": False, "error": "；".join(issues), "errors": issues}

        self.cfg = cfg
        self.login = login
        self._stop.clear()

        state = load_state(login)
        state["running"] = True
        state["started_at"] = int(time.time())
        state["stop_reason"] = None
        state["owner"] = "panel"
        state["pid"] = os.getpid()
        state["cfg"] = cfg_to_dict(cfg)
        save_state(state, login)

        self._thread = threading.Thread(target=self._loop, name="autotrade",
                                        daemon=True)
        self._thread.start()
        return {"ok": True, "dry_run": cfg.dry_run, "symbol": cfg.symbol,
                "lots": cfg.lots, "interval_sec": cfg.interval_sec}

    def _loop(self) -> None:
        cfg, login = self.cfg, self.login
        while not self._stop.is_set():
            started = time.time()
            try:
                row = tick(cfg, login=login)
            except Exception as exc:  # noqa: BLE001
                # 单次 tick 炸掉不能带走整个线程，否则面板显示"运行中"但实际全停
                append_log({"ts": int(time.time()), "kind": "error",
                            "reason": f"{type(exc).__name__}: {exc}"}, login)
                row = {}
            if row.get("stop"):
                break
            self._stop.wait(max(1.0, started + next_wait_sec(row, cfg) - time.time()))

        state = load_state(login)
        state["running"] = False
        save_state(state, login)

    def stop(self, login=None, cfg: Cfg | None = None,
             flatten: bool = False) -> dict:
        """停线程。flatten=True 时顺带清仓（面板的「关闭并清仓」）。"""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=15)
        self._thread = None

        use_cfg = cfg or self.cfg or cfg_from_state(login or self.login) or Cfg()
        if flatten:
            return close_all(use_cfg, login=login or self.login)

        state = load_state(login or self.login)
        state["running"] = False
        state["stop_reason"] = "面板手动停止"
        save_state(state, login or self.login)
        return {"ok": True, "closed": [], "failed": [], "count": 0}


WORKER = Worker()


# ------------------------------------------------------------------ 面板数据


def reset_history(login: int | str | None = None) -> dict:
    """清空「教训 + 战绩」：把决策流水和 state 归档，然后从零开始。

    为什么要清：教训和战绩都会被拼进提示词。之前的亏损样本会让它越来越保守
    （实测空单 1 胜 8 负 → 复盘攒出两条铁律 → 干脆不做空），换参数或换思路
    之前必须有一个干净起点，否则新策略永远背着旧样本的包袱。

    为什么是「归档」不是「删除」：流水是唯一能回答「当时为什么这么判断」的
    东西，删掉就再也说不清。改名成 `autotrade_log.<时间戳>.jsonl` 留在同目录，
    面板只读不带时间戳的那两个文件名，所以等于从零开始，人随时还能翻旧账。
    """
    state = load_state(login)
    if state.get("position"):
        return {"ok": False,
                "error": "还有一笔持仓没平。先点「关闭并清仓」，再来重置。"}

    # 先把在跑的线程停掉：不然重置完它下一轮又会把流水写回来
    if WORKER.alive():
        WORKER.stop(login=login, flatten=False)

    rows = read_log(login, limit=100000)
    cleared = {
        "rows": len(rows),
        "opens": sum(1 for r in rows if r.get("kind") == "open"),
        "closes": sum(1 for r in rows if r.get("kind") == "close"),
        "reviews": sum(1 for r in rows if r.get("kind") == "review" and r.get("lesson")),
    }

    tag = time.strftime("%Y%m%d-%H%M%S")
    archived: dict[str, str] = {}
    for p, key in ((log_path(login), "log"), (state_path(login), "state")):
        if p.exists():
            dst = p.with_name(f"{p.stem}.{tag}{p.suffix}")
            try:
                p.replace(dst)
                archived[key] = dst.name
            except OSError as exc:
                return {"ok": False,
                        "error": f"归档失败（{key}）：{exc}。没有删除任何东西。"}

    save_state(_empty_state(), login)
    # 新流水第一行记一笔重置，之后翻日志能看出「这里换了个起点」
    append_log({"ts": int(time.time()), "kind": "reset", "cleared": cleared,
                "archived": archived}, login)
    return {"ok": True, "archived": archived, "cleared": cleared}


def cache_stats(login: int | str | None = None) -> dict:
    """从 api_calls.jsonl 现算前缀缓存命中率。

    为什么单独算：DeepSeek 的上下文缓存是**自动**的（按前缀匹配），
    没有 prompt_cache_key 这种参数可传。想让命中率变好，唯一办法是让
    「每轮都不变的部分」尽量长、并放在最前面；而要判断有没有生效，
    只能看服务端返回的 cache_hit_prompt_tokens。
    """
    p = paths.account_file(paths.resolve_login(login), "api_calls.jsonl")
    hit = miss = calls = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = row.get("usage") or {}
            h, m = u.get("cache_hit_tokens"), u.get("cache_miss_tokens")
            if h is None and m is None:
                continue
            calls += 1
            hit += int(h or 0)
            miss += int(m or 0)
    total = hit + miss
    return {
        "calls": calls,
        "hit_tokens": hit,
        "miss_tokens": miss,
        "hit_rate": round(hit / total, 4) if total else None,
    }


def summary(login: int | str | None = None, recent: int = 30) -> dict:
    """给面板用。全部从 state + 日志现算，不做任何缓存，避免和实际不一致。"""
    state = load_state(login)
    rows = read_log(login, limit=5000)
    closes = [r for r in rows if r.get("kind") == "close"]
    opens = [r for r in rows if r.get("kind") == "open"]
    llm_decisions = [r for r in rows if r.get("source") == "llm"]

    closed = len(closes)
    wins = sum(1 for r in closes if r.get("pnl", 0) > 0)
    llm_closes = [r for r in closes if r.get("source") == "llm"]
    llm_wins = sum(1 for r in llm_closes if r.get("pnl", 0) > 0)
    reviews = [r for r in rows if r.get("kind") == "review" and r.get("lesson")]
    # 最近 10 笔的多空分布。实测过一次只做多（最近 4 笔全多）——这种偏置
    # 光看盈亏看不出来，必须单独统计，否则会以为"策略就是做多"。
    last_n = closes[-10:]
    longs = sum(1 for r in last_n if int(r.get("direction") or 0) > 0)
    shorts = sum(1 for r in last_n if int(r.get("direction") or 0) < 0)
    skew = len(last_n) >= 8 and max(longs, shorts) / len(last_n) >= 0.8
    # 模型主动说"不做"的次数。和"被闸门挡掉"分开算：
    # 前者是它的判断，后者是我们替它挡的。只看一个数会误判它胆量。
    declines = sum(1 for r in rows if r.get("kind") == "skip" and r.get("act") is False)
    # 调用失败必须单独计数：混进 declines 会把"API 挂了"读成"模型变保守了"
    failures = sum(1 for r in rows if r.get("source") == "error")

    # 面板线程已经不在了（面板重启过），state 里那条 running=True 就是废话。
    # 不纠正的话，界面会一直显示「运行中」，但实际上没有任何东西在做单。
    running = bool(state.get("running"))
    stale = running and state.get("owner") == "panel" and not WORKER.alive()
    saved_cfg = state.get("cfg") or {}
    dry_run = bool(saved_cfg.get("dry_run")) if "dry_run" in saved_cfg else True
    pending = state.get("pending_reviews") or []
    # 旧 state 里没有新加的字段，缺的时候要回退到当前默认值
    _defaults = Cfg()

    # 面板上要能看见"现在用的是哪个模型"：换供应商（DeepSeek → 智谱免费档）
    # 之后如果面板不显示，出了 401/404 根本分不清是 key 没换还是模型名写错。
    try:
        from agent.llm import _load_dotenv, DEFAULT_MODEL

        _load_dotenv()
        model_name = os.getenv("LLM_MODEL") or DEFAULT_MODEL
    except Exception:  # noqa: BLE001
        model_name = "未知"

    return {
        "ok": True,
        "running": running and not stale,
        "stale": stale,
        "worker_alive": WORKER.alive(),
        "dry_run": dry_run,
        "cfg": saved_cfg,
        "stop_reason": state.get("stop_reason"),
        "started_at": state.get("started_at"),
        "last_tick_ts": state.get("last_tick_ts"),
        "position": state.get("position"),
        "last_decision": state.get("last_decision"),
        "trades": len(opens),
        "closed": closed,
        "wins": wins,
        "hit_rate": round(wins / closed, 4) if closed else None,
        "pnl": state.get("pnl", 0.0),
        "llm_decisions": len(llm_decisions),
        "llm_closes": len(llm_closes),
        "llm_hit_rate": round(llm_wins / len(llm_closes), 4) if llm_closes else None,
        # 程序硬止损平的笔数。单独的指标：止损多说明入场方向差，
        # 而不是"模型平得不好"，两者的处理方式完全不同。
        "sl_closes": sum(1 for r in rows
                         if r.get("kind") == "close" and r.get("source") == "sl"),
        # 新出场规则的阈值，面板上要能看见，否则用户不知道规则已经变了。
        # 旧 state 里的 cfg 没有这两个字段（那会儿还不存在），
        # 所以缺了就回退到 Cfg 的当前默认值，而不是显示 None。
        "sl_usd": saved_cfg.get("sl_usd", _defaults.sl_usd),
        "tp_min_usd": saved_cfg.get("tp_min_usd", _defaults.tp_min_usd),
        "max_hold_sec": saved_cfg.get("max_hold_sec", _defaults.max_hold_sec),
        "reviews": len(reviews),
        "last_lesson": reviews[-1]["lesson"] if reviews else None,
        # 已平仓但还没到复盘时机的单子：面板据此显示「还要等几根」，
        # 否则用户会以为复盘坏了。
        "pending_reviews": len(pending),
        "next_review_ts": min((review_due_ts(p) for p in pending), default=None),
        # 最近 10 笔多空：偏斜时面板要报警，这是"只会做多/只会做空"的唯一信号
        "dir_recent": {"n": len(last_n), "long": longs, "short": shorts,
                       "skew": skew},
        "model_declines": declines,
        "model_failures": failures,
        "cache": cache_stats(login),
        "model": model_name,
        "recent": rows[-recent:],
    }


def trade_detail(ticket: int, login: int | str | None = None,
                 live: bool = False) -> dict:
    """一笔交易的完整档案：原料 → 结构 → 程序判断 → 模型判断 → 执行 → 结果。

    面板的交易溯源接口背后是这个。设计约束：
    - **只读日志，不调模型，不连终端**（除非 live=True 且要补 tick）。
      查历史不能产生 API 费用，也不能因为 MT5 没开就查不了。
    - K 线是**现拉的**（decision 时刻的原始行情没存盘——存了也就 14 行，
      但每笔都存会让日志膨胀 40 倍，不值得）。live=True 时从终端补拉
      开仓时刻附近的数据重算结构；拿不到就如实说"行情已过，无法重建"，
      绝不用现在的行情冒充当时的。
    - 复盘窗口可以事后重建（K 线历史还在），所以教训部分总是完整的。
    """
    rows = read_log(login, limit=20000)
    open_row = next((r for r in rows
                     if r.get("kind") == "open" and int(r.get("ticket") or 0) == int(ticket)), None)
    if open_row is None:
        return {"ok": False, "error": f"日志里找不到 ticket={ticket} 的开仓记录"}

    close_row = next((r for r in rows
                      if r.get("kind") == "close" and int(r.get("ticket") or 0) == int(ticket)), None)
    review_row = next((r for r in rows
                       if r.get("kind") == "review" and int(r.get("ticket") or 0) == int(ticket)), None)

    opened_at = int(open_row.get("ts") or 0)
    cfg = cfg_from_state(login) or Cfg()

    # --- 原料与结构：能重建就重建，不能就明说 --------------------------------
    bars = structure = ticks_block = None
    structure_note = None
    try:
        import MetaTrader5 as _mt5  # noqa: F401  探测终端是否可用
        hist = _bars(cfg.symbol, cfg.tf, cfg.bars + 120)
        if hist:
            # 用「不晚于开仓时刻的最后一根」当开仓时的当前 K 线，
            # 截出与决策时一致的 14 根窗口。
            eligible = [b for b in hist if int(b.get("time") or 0) <= opened_at]
            if len(eligible) >= VISIBLE_BARS:
                window = eligible[-VISIBLE_BARS:]
                bars = _fmt_bar_lines(window)
                structure = _structure_text(window, cfg.zone_edge)
                prog_dir = _prog_direction(window, cfg.zone_edge)
                try:
                    ticks_block = _ticks_text(cfg, window)
                except Exception:  # noqa: BLE001
                    ticks_block = None
            else:
                structure_note = (f"终端历史只覆盖到 {len(eligible)} 根，"
                                  "开仓时刻的完整 14 根窗口已不可得")
        else:
            structure_note = "终端历史不可用（未连接或品种无数据）"
    except Exception:
        structure_note = "终端未连接，历史行情无法重建"

    # --- 程序 vs 模型：分歧记账的两列 ----------------------------------------
    prog_dir = open_row.get("prog_dir")
    model_dir = open_row.get("direction")
    agree_field = open_row.get("agree")
    if prog_dir is None and structure:
        # 旧日志没有 prog_dir 字段（2026-10-25 之前），从重建的结构补算
        try:
            prog_dir = _prog_direction(
                _bars(cfg.symbol, cfg.tf, cfg.bars + 120)[-VISIBLE_BARS:],
                cfg.zone_edge)
        except Exception:  # noqa: BLE001
            pass
    if prog_dir is not None and model_dir is not None:
        verdict = ("一致" if int(prog_dir) == int(model_dir)
                   else "分歧" if int(prog_dir) != 0 else "程序无建议，模型自选")
    else:
        verdict = None

    return {
        "ok": True,
        "ticket": int(ticket),
        "open": open_row,
        "close": close_row,
        "review": review_row,
        "result": {
            "pnl": (close_row or {}).get("pnl"),
            "hit": (close_row or {}).get("hit"),
            "held_sec": (close_row or {}).get("held_sec"),
            "exit_source": (close_row or {}).get("source"),
        },
        "recon": {
            "bars": bars,
            "structure": structure,
            "prog_dir": prog_dir,
            "ticks": ticks_block,
            "note": structure_note,
        },
        "judgement": {
            "prog_dir": prog_dir,
            "model_dir": model_dir,
            "agree_field": agree_field,
            "p_up": open_row.get("p_up"),
            "model_reason": open_row.get("reason"),
            "verdict": verdict,
        },
    }


# ------------------------------------------------------------------ CLI


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AI 实时做单（默认纸面，不发真单）")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--lots", type=float, default=0.01)
    parser.add_argument("--interval", type=int, default=Cfg.interval_sec,
                        help="决策间隔秒数（最小 3，模型单次约 2.6 秒）")
    parser.add_argument("--cooldown", type=int, default=30, help="平仓后冷却秒数")
    parser.add_argument("--max-trades", type=int, default=0, help="开仓笔数上限，0=不限")
    parser.add_argument("--max-loss", type=float, default=0.0, help="累计亏损上限 USD，0=不限")
    parser.add_argument("--max-minutes", type=int, default=0, help="运行时长上限分钟，0=不限")
    parser.add_argument("--live", action="store_true", help="关掉纸面模式，真发单（危险）")
    parser.add_argument("--status", action="store_true", help="只看状态，不跑")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    args = parser.parse_args()

    cfg = Cfg(
        symbol=args.symbol, lots=args.lots, interval_sec=args.interval,
        cooldown_sec=args.cooldown, dry_run=not args.live,
        max_trades=args.max_trades, max_loss_usd=args.max_loss,
        max_minutes=args.max_minutes,
    )

    if args.status:
        s = summary()
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
        return

    if args.once:
        print(_fmt(tick(cfg)))
        return

    run(cfg)


if __name__ == "__main__":
    main()
