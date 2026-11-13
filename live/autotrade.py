"""AI 实时做单：按分钟节奏让模型判方向、自己决定何时平仓。

规则（写死在代码里，不是配置项）
--------------------------------
1. 默认每 60 秒一次决策（`interval_sec` 是面板项，可改）。**不交易是允许的**：
   空仓时给方向或 `act=false`，持仓时给「继续持有 / 立刻平仓」。
2. 同一品种上同时只允许 1 笔持仓，**任何来源的持仓都算**（包括你手动开的）。
3. 平仓后冷却按方向分两档：反向（及方向未定）`cooldown_sec` 默认 60 秒，
   同向 `cooldown_same_sec` 默认 180 秒。
4. **开仓请求本身不带 SL/TP**（`open_market(sl=None, tp=None)`），成交之后
   再挂券商端保护线（2026-11-09 起挂止损，2026-11-11 起非趋势单还挂止盈，
   见 `_attach_entry_sl` / `_attach_entry_tp`）。不能用开仓请求里的价算：
   滑点会让那条线偏离真实成交价，偏到近的一侧就是白送一次被扫，偏太多
   还可能让券商以 10016 把整笔开仓拒掉。
   入场之后有几类退出，它们在流水 `source` 里必须分得开——这是统计口径的前提：
     · `sl` / `be_local` —— 程序按浮亏自己发市价平（硬止损 / 保本兜底）；
     · `be` —— 券商端那条保本止损被打：趋势单峰值到过 `be_arm_usd` 后
       `modify_sl` 把 SL 抬到开仓价，成交由券商端触发；
     · `tp` —— 券商端硬止盈被打：**只有非趋势单有这条线**（震荡单开仓时
       挂 `tp_min_usd` 价距），触价即成交，模型没有否决权；
     · `llm` —— 模型判断平仓；
     · `external` / `panel` —— 人工在终端平、面板「停止并清仓」。
   `sl` / 超时 / 停滞唤醒 / 止盈门槛的先后顺序见 `_exit_gate`（顺序是致命的）。
5. 开仓 = 让模型估「按它判的打法，接下来这段行情往哪边走」的 p_up：
   p_up > 0.5 做多、< 0.5 做空；|p_up-0.5| 不到 `min_edge` 不做（信号不够强）。

一次 tick 只花一次模型调用
---------------------------
空仓就调「进场判断」，持仓就调「平仓判断」，两者二选一。
不会为了记账多打一次 API，也不会在持仓期间白烧调用。

风控在出场端，不在开仓端
-------------------------
`sl_usd` / `max_hold_sec` / 保本止损一起兜住单笔亏损；另外三个刹车
`max_trades` / `max_loss_usd` / `max_minutes` 触发即停，写入
`autotrade_state.json` 的 `stop_reason`，重启不会自动恢复。

数据落在账户目录下：
    autotrade_state.json   运行状态（是否在仓、上次开平仓时间、累计笔数）
    autotrade_log.jsonl    逐 tick 决策流水（open / skip / hold / close / stop）
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path

from live import paths
from live.lessons import (  # noqa: F401  （重新导出：老路径 autotrade.recent_lessons 继续可用）
    HARD_RULE_MIN,
    LESSON_SCOPES,
    LESSON_THEMES,
    LESSON_THEME_ENUM,
    REVIEW_AFTER_BARS,
    REVIEW_BEFORE_BARS,
    THEME_DOMAIN,
    VERDICT_EARLY,
    VERDICT_LATE,
    VERDICT_OK,
    _DOMAIN_TEXT,
    _ENTRY_WORDS,
    _EXIT_WORDS,
    _FORCED_EXIT_SOURCES,
    _NO_INJECT_THEMES,
    _THEME_TAIL,
    _VERDICT_EARLY_WORDS,
    _VERDICT_LATE_WORDS,
    _VERDICT_OK_WORDS,
    _can_be_hard,
    _fmt_review_bars,
    _lesson_tail,
    _norm_lesson,
    _refine_exit_theme,
    _scope_ok,
    append_lesson,
    effective_theme,
    exit_counterfact,
    exit_verdict,
    infer_scope,
    lesson_clusters,
    lesson_rows,
    lesson_theme,
    lessons_path,
    migrate_lessons,
    parse_review,
    recent_lessons,
    reset_lessons,
    review_window,
    row_scope,
    row_theme,
    row_verdict,
    verdict_for,
)
from live.jsonl import unwrap_lesson as _unwrap_lesson  # noqa: F401
from live.jsonl import read_jsonl as _read_jsonl  # noqa: F401

from live.cfg import (  # noqa: F401  （重新导出：老路径 autotrade.Cfg 继续可用）
    BASE_LOTS,
    CODE_ONLY,
    Cfg,
    PANEL_FIELDS,
    TRADABLE_SYMBOLS,
    _SCALE_EPS,
    _TYPE_LABEL,
    _as_bool,
    _coerce,
    _field_kind,
    cfg_from_start_body,
    cfg_to_dict,
    lot_scale,
    money_at_lots,
    money_label,
)
from live.ta import (  # noqa: F401  （重新导出：老路径 autotrade._atr 继续可用）
    ER_RANGE_MAX,
    ER_TREND_MIN,
    MA_PERIOD,
    P_UP_CEIL,
    P_UP_FLOOR,
    SYMBOL_LABELS,
    VISIBLE_BARS,
    _JSON_RE,
    _PUP_RE,
    _atr,
    _fmt_bar_lines,
    _last_bar_shape,
    _ma_calc,
    _norm_bool,
    _norm_prob,
    _prog_direction,
    _regime_word,
    _returns,
    _spread_points,
    _structure,
    _structure_text,
    _symbol_label,
    _zone_fact,
    _zone_text,
    clamp_p_up,
    feature_snapshot,
    ma_line,
    parse_p_up,
    regime_of,
    visible_bars,
)

from live.units import (  # noqa: F401  （重新导出：老路径 autotrade._degrade / _digits 继续可用）
    _DEGRADE_INTERVAL,
    _DEGRADE_SEEN,
    degrade as _degrade,
    digits as _digits,
    point as _point,
    px as _px,
    spread_cost as _spread_cost,
)

from live.bootstrap import (  # noqa: F401  （重新导出：老路径 autotrade.validate_cfg 继续可用）
    cfg_from_state,
    validate_cfg,
)

from live.storage import (  # noqa: F401  （重新导出：老路径 autotrade.read_log / load_state 继续可用）
    _empty_state,
    _log_signal,
    _tail_lines,
    append_log,
    cache_stats,
    load_state,
    log_path,
    read_log,
    save_state,
    state_path,
)
from live.market import (  # noqa: F401  （重新导出：老路径 autotrade._quote / price_dist_of 继续可用）
    _ACCT_CUR,
    _ACCT_F,
    _ACCT_TTL,
    _CONTRACT_CACHE,
    _acct_currency,
    _acct_factor,
    _bars,
    _positions,
    _quote,
    _symbol_contract,
    price_dist_of,
)
from live.report import (  # noqa: F401  （重新导出：老路径 autotrade.symbol_options 继续可用）
    symbol_options,
    model_vs_actual,
)


def _llm():
    """延迟导入：没装 openai 的机器上，纯函数单测也要能跑。"""
    from agent import llm

    return llm


# ------------------------------------------------------------------ 配置


# ------------------------------------------------------------------ 存储


def _finish(state: dict, row: dict, login: int | str | None = None,
            ts: int | None = None) -> dict:
    """一轮决策的**唯一出口**：落流水 + 落状态，然后把行返回给调用方。

    为什么要收敛
    ------------
    tick() 有二十多个出口，以前每处手写「append_log + save_state + return」。
    漏一处的后果不是抛异常，而是**流水写了、state 没写**：面板显示空仓而
    实际持仓还在，或者下一条信号一直挂在 pending 上等人点。这类问题只在
    事后对账时才发现，而账就是钱。

    收敛之后新加分支只要 `return _finish(state, row, login, ts=now)`，
    结构上不可能漏。`ts` 传了就顺手更新 last_tick_ts（面板用它判断心跳）。

    顺序固定：**先写流水再写状态**。反过来的话，状态已经说"平了"、
    流水里却还没有那一行，两边对不上时说不清是哪一步断的。
    """
    if ts is not None:
        state["last_tick_ts"] = ts
    append_log(row, login)
    save_state(state, login)
    return row


# 分桶用的来源名。`be` = 券商端那条线被打（仓位在终端消失时才能确认），
# `be_local` = 挂了保本线但本地兜底平的（改单没成，浮盈回 0 时程序代平）。
_SL_SOURCES = ("sl",)
_BE_SOURCES = ("be", "be_local")


def _book_close(state: dict, pos: dict, pnl: float, now: int, *,
                source: str | None, reason: str | None,
                held_sec: int | None = None,
                dry_run: bool | None = None) -> dict:
    """一笔平仓的**唯一记账出口**：更新计数器、清仓位、返回 close 行。

    为什么收敛
    ----------
    平仓有三条路：决策轮自己平的、发现仓位在终端消失（人工 / 保本线被触发）、
    面板「停止并清仓」。以前三处各写一遍「加计数 + 拼 close 行」，于是**字段
    已经分叉**：`peak_pnl`/`peak_ts`/`plan`/`atr` 只有决策轮那条写，另外两条
    不写。后果不是报错，而是统计静默变形——「这笔 tp 是几倍 ATR」会把人工平仓
    和清仓的单子直接排除，而那正是用来定 tp 参数的口径。`sl_closes` /
    `be_closes` 同理，只有决策轮计数，于是"止损占比"看着比实际低。

    这正是 `_finish` 收敛过的那个坑（二十多个出口各写一遍，漏一处的后果是
    流水写了 state 没写）。出口收敛了，记账漏了。

    本函数**不管**的（各路径语义不同，留在调用点）：
      - 成交价与 pnl 怎么算（要 bid/ask，读不到报价时清仓路径按 0 记）
      - `be_sl`、`via`、`signal_id`/`exec_by`/`slip`、`error` 这些路径专属字段
      - 复盘排队（`queue_review`），以及 `pos["exit_reason"]`

    `source` 决定计数分桶：硬止损 = 入场方向错了；保本止损 = 方向对过但没吃到。
    混在一起就再也分不开「入场不行」和「出场没拿够」。
    """
    # 平仓发起方落到 pos 上，`queue_review` 靠它决定这条教训归出场端还是两端
    # （见 `_FORCED_EXIT_SOURCES`）。2026-11-10 那笔券商端保本止损：仓位在终端
    # 消失那条路算出了 source="be" 却没写回 pos，复盘拿到的 exit_source 是空，
    # scope 停在 exit、src 为空——「这个位置当初就不该开」永远进不了入场提示词。
    # 放在这里而不是各调用点：路一共有三条，漏一条就静默归错域。
    pos["exit_source"] = source or ""
    held = (int(held_sec) if held_sec is not None
            else int(now) - int(pos.get("opened_at") or now))
    state["closed"] = int(state.get("closed") or 0) + 1
    if pnl > 0:
        state["wins"] = int(state.get("wins") or 0) + 1
    state["pnl"] = round(float(state.get("pnl") or 0.0) + pnl, 2)
    state["last_close_ts"] = now
    state["last_close_pnl"] = round(float(pnl), 2)
    # 同向重进的冷却靠它判（见 cooldown_left）。平仓当刻这笔的方向还拿得到，
    # 之后 position 就被清了——所以必须在清仓之前记。
    state["last_close_dir"] = pos.get("direction")
    state["position"] = None
    if source in _SL_SOURCES:
        state["sl_closes"] = int(state.get("sl_closes") or 0) + 1
    elif source in _BE_SOURCES:
        state["be_closes"] = int(state.get("be_closes") or 0) + 1
    return {
        "ts": now, "kind": "close", "ticket": pos.get("ticket"),
        "direction": pos.get("direction"), "pnl": round(pnl, 2),
        "p_up": pos.get("p_up"), "hit": pnl > 0,
        "held_sec": held,
        "peak_pnl": pos.get("peak_pnl"),
        "peak_ts": pos.get("peak_ts"),
        # plan / atr 在 close 行冗余一份（open 行已有）：做「这笔 tp 是几倍
        # ATR」的统计时不用再按 ticket 反查 open 行，少一次 join 就少一处漏。
        "plan": pos.get("plan"),
        "atr": pos.get("atr"),
        # 脉冲留痕（2026-11-13）：atr_peak = 峰值折成几倍 ATR；t3/t5 = 首次
        # 达到 3 / 5 个 ATR 用了几分钟（None = 没到过，或旧持仓没锁 atr_pnl）；
        # burst = 3 分钟内走满 5 ATR，即 tools/measure_burst.py 判出的那类事件。
        # 只落盘不消费：要回答的是"这类单在我真实入场质量上是不是也吐回去"。
        "atr_peak": _peak_atr(pos),
        "t3": pos.get("t3"),
        "t5": pos.get("t5"),
        "burst": _is_burst(pos),
        # 两个理由分别存：开仓理由来自当初的进场判断，平仓理由来自这次离场
        # 判断。合成一个字段就再也分不清「为什么进」和「为什么出」，复盘直接废掉。
        "entry_reason": pos.get("entry_reason"),
        "reason": reason, "source": source,
        "dry_run": bool(dry_run) if dry_run is not None else bool(pos.get("dry_run")),
    }


def backfill_sl_src(login: int | str | None = None) -> dict:
    """给历史上「被硬止损砍掉」的单留下的教训补 `src="sl"`（幂等）。

    为什么要回填：分域注入在 2026-10-28 之前不分 sl，那一批教训的 scope
    全是 exit，被 `_scope_ok` 永久挡在入场端外面——21:46 写下「破 4047.4
    双底立即止损」，22:07 就在 4047.44 又接了一笔多，因为入场提示词里
    根本读不到它。改代码只对新数据生效，库里那批老教训要靠回填才能救回来。

    判据不猜文本，直接查流水：close 行 `source == "sl"` 的 ticket 就是硬止损平的。
    幂等：`src` 已经有值的不动；重复跑不会重复计数。
    """
    p = lessons_path(login)
    if not p.exists():
        return {"ok": False, "error": "教训库文件不存在"}
    acc = paths.account_dir(paths.resolve_login(login))
    sl_tickets: set = set()
    for f in sorted(acc.glob("autotrade_log*.jsonl")):
        for r in _read_jsonl(f):
            if r.get("kind") == "close" and r.get("source") == "sl":
                t = r.get("ticket")
                if t:
                    sl_tickets.add(t)
    if not sl_tickets:
        return {"ok": True, "checked": 0, "changed": 0, "sl_tickets": 0}

    rows = _read_jsonl(p)
    changed = 0
    for r in rows:
        if r.get("src"):            # 已标过的不动，保证幂等
            continue
        if r.get("ticket") in sl_tickets:
            r["src"] = "sl"
            # 出场端锁死的话，标记了也进不了入场提示词（domain 端要 both）。
            if r.get("scope") == "exit":
                r["scope"] = "both"
            changed += 1
    if changed:
        with p.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return {"ok": True, "checked": len(rows), "changed": changed,
            "sl_tickets": len(sl_tickets)}


# ------------------------------------------------------------------ 解析模型输出
#
# 模型不保证严格 JSON，所以解析必须是「宽进严出」：能从各种烂格式里抠出数字，
# 但抠不出来就返回 None，由调用方退回规则兜底。绝不能猜一个数字糊过去——
# 猜出来的概率和模型给的没区别，事后就没法判断到底是模型的锅还是解析的锅。

_CLOSE_RE = re.compile(r'"?close"?\s*[:=]\s*(true|false|1|0|yes|no)', re.I)


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
#   1. 按分钟聚合成表：tick 数 / 买价区间 / 点差（只在偏离基准时标）；
#   2. 再附最后十几笔原始 tick，让它看见"刚刚这一下"。
# 时间边界与 K 线**完全一致**：只用这 14 根覆盖的那段时间，不额外偷看未来。
#
# 2026-10-26 瘦身：原来每行还印"卖价区间"和"点差 均/最大"。实测这两段
# 是纯重复——卖价恒等于买价 + 点差，点差在 Exness 上全窗口固定 240/240。
# 占 tick 块 1/3、进场提示词 8.5%。现在卖价不再逐行印（用买价 + 末行的点差
# 就能算出来），点差只在某一分钟跑偏时才标出来。

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

    sp_sorted = sorted(spreads) if spreads else [0.0]
    return {
        "ok": True,
        "rows": rows,
        "total": len(ticks),
        "spread_avg": sum(spreads) / len(spreads) if spreads else 0.0,
        "spread_max": max(spreads) if spreads else 0.0,
        "spread_min": min(spreads) if spreads else 0.0,
        # 基准点差 = 中位数。渲染时只有偏离它的分钟才写点差——
        # 固定点差券商（Exness 240/240）下等于每行都省掉一段。
        "spread_base": sp_sorted[len(sp_sorted) // 2],
        "max_jump": jump,
        "recent": ticks[-recent:],
        "point": point,
    }


def _fmt_tick_block(summary: dict, digits: int = 3) -> str:
    """tick 块的文本形态。

    **去掉了卖价区间和逐行点差**。原因不是"版面好看"，是实测重复：
    卖价恒等于买价 + 点差、点差在固定点差券商上是全员同一个数
    （Exness 全窗口 240/240）。实测这一段占了 tick 块的 1/3、
    进场提示词的 8.5%，删掉不损失任何信息——点差仍然在汇总行里给
    平均/最宽，某一分钟真跑偏了那一行会单独标出来。

    "信息不丢"是这条改动的底线：省的是**重复**，不是**数据**。
    """
    if not summary or not summary.get("ok"):
        return "（tick 数据暂不可用，只能依据上面的 K 线判断）"

    d = max(0, int(digits))
    base = int(round(summary.get("spread_base") or summary.get("spread_avg") or 0))

    def _sp_note(avg: float, mx: float) -> str:
        """只在偏离基准点差时写出来。"""
        a, m = int(round(avg)), int(round(mx))
        if base and a == base and m == base:
            return ""
        return f"   点差 {a}/{m}"

    lines = ["分钟   tick数  买价区间（卖价 = 买价 + 点差，见末行统计）"]
    for r in summary["rows"]:
        if not r.get("n"):
            lines.append(f"{r['tag']:<6} {0:>6}  —")
            continue
        lines.append(
            f"{r['tag']:<6} {r['n']:>6}  {r['bid_lo']:.{d}f}-{r['bid_hi']:.{d}f}"
            + _sp_note(r.get("spread_avg") or 0.0, r.get("spread_max") or 0.0)
        )

    recent = summary.get("recent") or []
    if recent:
        lines.append("")
        lines.append(f"最近 {len(recent)} 笔 tick（时间 UTC，买价）")
        for t in recent:
            ts = int(t.get("time") or 0)
            clock = time.strftime("%H:%M:%S", time.gmtime(ts)) if ts else "--"
            sp = _spread_points(t, summary.get("point") or 0.001)
            note = "" if (base and int(round(sp)) == base) else f"   点差 {int(round(sp))}"
            lines.append(f"  {clock}  {float(t.get('bid') or 0):.{d}f}{note}")

    s_min = int(round(summary.get("spread_min") or 0))
    s_max = int(round(summary.get("spread_max") or 0))
    s_avg = int(round(summary.get("spread_avg") or 0))
    # 全程一个值就说"恒"——比"平均 240、最宽 240"少几个字，而且更准确
    sp_text = (f"点差 恒 {s_avg} 点" if s_min == s_max
               else f"点差 平均 {s_avg} 点、{s_min}~{s_max} 点")
    lines.append("")
    lines.append(
        f"tick 合计 {summary['total']} 笔；{sp_text}；"
        f"相邻 tick 最大单步跳动 {summary['max_jump']:.0f} 点。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ 提示词
#
# 信息边界（用户指定的硬约束）
# ---------------------------


# ---------------------------------------------------------------- 多周期背景
#
# 边界变更（2026-10-26，用户明确要求）：原来只有 M1 14 根，等于让模型在
# 14 分钟的窗口里判断方向，完全不知道这 14 分钟处在更大的趋势的哪个位置。
# 现在额外给一路 M5 背景，但**只给结构摘要、不给原始 K 线**：
#   1) token：再来一份 14 行 OHLC，进场 prompt 直接翻倍；
#   2) 一致性：给了原始表模型会自己重算 ER，和程序算的必然有出入，
#      两个 ER 打架时它按哪个走都说不清。程序算好只给结论，
#      和 M1 的处理方式保持一致。
# 这不是"放宽 14 根"——M1 的可见根数仍然是 14，物理截断没有松动。
# 新增的是一个**不同周期**的、已经压缩成一行结论的上下文。
MTF_TF = "M5"
MTF_BARS = 14          # 14 根 M5 = 70 分钟背景
MTF_MIN_BARS = 5       # 少于这个数就不给背景（样本太少，ER 没意义）


ENTRY_SYSTEM = """你是 {SYMBOL_LABEL} 的日内交易员，看 M1 图定时机。
每一单先判行情（震荡/趋势/不明），再选对应打法——两套打法在下面的
「先判行情，再选打法」里，判错了行情，打法一定跟着错。
震荡单赚的是区间内的来回，趋势单赚的是顺势的一段；两种单都可能持有
几十分钟，单笔赚多少由行情结构决定，不由你预先框死。不要指望"大行情"
来救一笔烂入场，也不要因为持仓超过几分钟就慌——持有本身就是计划的一部分。

【信息边界】
你只能看到三样东西：
  1) 当前 1 根 M1 和之前 13 根，共 14 根（约 14 分钟），以及这段时间的逐笔 tick；
  2) 一行 M5 背景摘要：14 根 M5（约 70 分钟）的结构结论，程序算好的，没有原始 K 线；
  3) 一个 MA28 数字：最近 28 根 M1 的收盘均价，以及当前收盘相对它的位置。
     这同样是程序算好的一个数，**不是 28 根原始 K 线**，你看不到那 28 根长什么样。
除此之外没有别的数据，也不要假装有：不要引用均线系统、其它技术指标、新闻、
成交量、市场情绪——除了 MA28，别的均线你也看不到，编出来就是幻觉。
（消息里另有你自己的战绩、复盘教训和「刚平仓提醒」——那是历史，不是行情，
不构成对盘面的新证据。）

M5 背景怎么用：它只回答"大方向偏哪一边"，**不参与这一根 K 线的进出场判定**。
M1 决定时机，M5 决定你更愿意站在哪一侧。两者打架时（M1 位置提示做多、
M5 却是下跌趋势），不是禁止交易，但你需要更强的理由，p_up 也不该给到激进的一端。

MA28 怎么用：它是 28 分钟的价格中枢，回答"中期偏向哪边"，和 M5 同级，
**同样不参与这一根 K 线的时机判定**。
- 收盘在均线**上方** → 中期偏多：做多是顺风，做空要更强理由。
- 收盘在均线**下方** → 中期偏空：做空是顺风，做多要更强理由。
- 标了「贴着均线」→ 中性，别拿它当方向依据，回到 M1 结构和位置判定。
- MA28 与 M1 短期方向打架（均线下方但刚连阳）＝ 逆势反弹：
  要么不做，要么理由必须比平时硬，不能因为"看着要涨"就去接逆势多单。
它不是入场信号。贴线反复穿越的时候均线最容易骗人，位置与确认仍然看 M1。

【主次不能反】
以上面 1 分钟蜡烛图为主：结构、连阳连阴、实体与影线、收盘在区间里的位置，
这些才是你做判断的依据。
tick 级数据是辅助，只用来确认两件事：这一下是真突破还是插针，点差有没有异常放大。
tick 不得单独构成开仓理由；K 线与 tick 冲突时，以 K 线为准。

【先判行情，再选打法（两套打法，先判后用，别用反）】
用「结构 ER（净移动 ÷ 总行程，0~1）」判断这 14 根的性质，再选对应打法：

▍震荡市（ER < 0.30）：低买高卖
  贴近区间下沿做多、贴近上沿做空（以数据里的「位置判定」为准），中间区域不动手；
  不要追着单根大阳线/大阴线跑，那通常是震荡里的假突破。
  **只有震荡市可以「贴边就动手」**：区间没被突破，上沿就是卖压所在。
  贴着上沿做空不叫追空，那是震荡市最标准的动作，别拿趋势市的打法来管它。

▍趋势市（ER > 0.50）：顺势直接做，**不等回踩、不等反抽**
  方向就是 drift 的方向（ER 回答"走得多直"，drift 回答"往哪边走"）。
  以前让你"等回踩到 X 再进"，实测那套做法接到的回踩大多是反转的开头：
  等回踩的单 47 笔均值 -1.21，胜率 36%——是接刀，不是低吸。所以从这一版起：
  · 方向明确就做，位置决定的是这笔质量，不是做不做；
  · "等回踩到 XX 再进""等反抽再空"这类话在趋势市**不再合法**——
    要么现在按现价做，要么这轮不做；
  · 唯一否决顺势入场的依据是**当前根形状**（结构行里有：收盘在自身区间
    的百分比、上/下影线占比）：趋势做多要求当前根强势收（收盘在自身区间上半、
    上影线不长）；冲高回落（长上影＋收盘掉到下半）= 插针顶，这轮不做。
    这一条管的是"这一根是不是见顶信号"，不是"位置高不高"——位置高本身不是理由。
  · 逆势抄底摸顶在任何行情下都不做。趋势市里唯一的逆势单是：
    结构 ER 已跌回 0.50 以下（趋势结束），那是新行情，按新行情重新判。

▍方向不明（0.30 ~ 0.50）：只做位置明显的单（收盘位置贴近区间边缘），
  中间位置给 0.50，不开仓。
  数据里的「位置判定」已经替你算好了是否够边缘（默认阈值 35%/65%），直接按它走。

【贴边怎么处理：先看行情，四种情况别混成一锅】
「贴下沿」「贴上沿」本身不是买卖信号，要看这 14 根是什么性质：
- **震荡（ER < 0.30）**：贴下沿做多、贴上沿做空。**这是唯一"位置决定做不做"的情形。**
  区间没被突破，上沿就是卖压、下沿就是支撑。判据是 ER 破没破，不是"价格看起来太高"。
- **下跌趋势（ER > 0.50 且 drift < 0）**：顺势做空。贴下沿是弱势延续，**不是超卖，
  不接多**；贴下沿做空不算追空，但当前根必须弱势收（收盘在自身区间下半、下影不长）——
  长下影＝下方有人接，这种不做。
- **上涨趋势（ER > 0.50 且 drift > 0）**：顺势做多。贴上沿是强势延续，**不是超买，
  不因为"位置高"就放弃**；但当前根必须强势收（收盘在自身区间上半、上影不长）——
  冲高回落的长上影＝插针顶，这种不做。
- **方向不明（0.30 ~ 0.50）**：只做收盘位置够边缘的单，中间不动手。

**一句话记法：震荡市位置决定做不做（贴边做、中间不做）；趋势市方向决定做不做
（顺势做、逆势不做），当前根形状决定这一根做不做（插针不做、推进做）；
任何行情都不抄底摸顶。**

- 多空都要有确认：做多要看到止跌（收阳，或收盘回到区间上半），
  做空要看到滞涨（收阴，或收盘回落到区间下半）。没有确认就等。
- **两个方向的机会是对等的。** 不要因为"最近做空老是亏"就只做多，
  也不要因为"最近做多连赢"就默认下一步还该做多——那都是样本偏差，不是规则。
- **看跌就要考虑做空，不要把"看跌"翻译成"不做"。** 这两件事之间只差一个
  位置确认：位置合适就开空，位置不合适才等。下跌行情里"什么都不做"往往是
  漏掉了整段顺势利润，而不是规避了风险。
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
  3) 【主】结构指标：ER、振幅与 ATR 倍数、收盘位置、连阳连阴、当前根形状、
     位置判定、行情判定
  4) 【背景】M5 结构摘要（一行；程序拿不到时这一行不出现）
  5) 【背景】MA28 与当前收盘的相对位置（一行；算不出来时不出现）
  6) 【辅】tick 数据：每根的笔数与买价区间，末行给全窗口点差与最大单步跳动
  7) 短周期变化与振幅
  8) 你的历史战绩与最近复盘
  9) 当前报价与点差（只给点数，**不给成本金额**——成本不由你负责）
 10) 当前时间（最后一行）

关于「× ATR」这个数：结构行里的振幅会同时给一个「= N 倍单根均幅」的比值。
单根均幅 = 这 14 根每根真实波幅（高-低，并与前收比较取大）的平均值。
这个比值是**无量纲**的，所以能跨品种、跨时段比较：
  上限约 13（每根都创新高/新低才能到）；
  越接近 13，说明这波越接近单边走到极限，追进去的余地越小；
  比值小说明区间被压缩，突破前的蓄势，假突破概率高。
用它判断"这波走完了没有"，比单看振幅绝对数字可靠。

【输出】
只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"act": true, "p_up": 0.58, "agree": true, "reason": ""}
act —— 这笔到底做不做。false 就是"不做"，这是允许的、也是常见的选择。
p_up —— 接下来这段行情里（几分钟到几十分钟，取决于你判的打法）价格上涨的概率，
        0 到 1 之间的小数。
        **它是方向概率，不是信心分。** 写 0.32 的意思是"我判断有 68% 的概率下跌"，
        这本身就是做空的理由——不要把它翻译成"看不准所以不做"。
        看跌和"要开空单"之间只差一个位置确认，不差勇气。
        即使 act=false 也要给，用来记录你当时的方向倾向。
        **只在 0.20 ~ 0.80 之间取值**：0/1 是伪精确（没人能百分百确定 1 分钟后
        的方向），写 0.20 就代表你最强的看空，0.80 代表最强的看多。
        **上面示例里的 0.58 只是格式示范，不是建议值。** 每一次都要按当根
        K 线重新估、写你自己的数。照着示例填等于没有判断——0.58 离 0.5 只有
        0.08，刚好越过系统闸门，于是每一轮都够格开多；示例写成 0.00 的那版，
        历史上 46% 的单子因为照抄全变成了空单。
agree —— 你的方向判断是否与数据里「位置判定」的程序倾向一致。
        一致 true，不一致 false（此时 reason 必须给出你反对的依据）。
reason —— 不超过 40 字的中文依据，必须引用输入里的具体数字。

注意 act 和 reason 必须一致：写了"不追""等确认"，act 就是 false。
不要出现理由说不做、act 却给 true 的情况。

【执行方式——写 reason 之前先读这一条】
你只有两个选项：**现在按报价成交**，或者**这一轮不做**。没有中间档。
- 系统只有市价单，没有挂单、没有限价、没有条件单。你写"回踩 3998 挂多"，
  真实发生的是"以当前价立刻买入"——那个价位不会被执行，只会变成日志里
  一句和成交价对不上的空话（实测有单子写"回踩 4004 挂多"，成交在 4010.9）。
- 所以"想等回踩/等确认/等企稳"的唯一正确写法是 `"act": false`，
  并在 reason 里写清等的是什么价位。{INTERVAL_SEC} 秒后你会拿到新的 K 线，
  那时候真到位了再开。
- 程序**不会替你拦单**：开不开由你决定。但程序会给出建议，并且记下你否决了
  哪一条。所以下面两种情况，如果照做，reason 里必须正面回应，不能装作没看见：
  ① reason 里出现"等确认""等企稳""等收阳""回踩再""挂多""挂空""不追"这类
     字眼，却给了 act=true —— 那就等于说"我说要等，但我现在就买"。系统只有
     市价单，成交价会是你写下等待价的那一刻的现价，两者必然对不上。
     例外：**趋势市里这些词根本不该出现**——趋势打法的标准动作就是顺势直接做，
     你在趋势市写"等回踩"等于自己放弃，而此时等待锚点也已停用，写了也没人记住。
  ② 震荡市里做多且收盘位置 ≥ {ZONE_UP}%（贴上沿）。震荡市上沿是做空位，贴它
     做多就是买在区间顶，这个组合不成立。趋势市里贴上沿做多**合法**，但当前根
     必须强势收（收盘在自身区间上半、上影不长）——理由里引用的应该是形状证据，
     不是"我觉得还会涨"。
- **等待锚点只在震荡市和方向不明时生效，而且是锁定的。** 震荡市里你说"等回踩到 X"，
  系统就记住 X；下一轮会把 X、离现价还差多少、已经等了多久一起给你。往后
  把 X 改成更靠近现价的数字**不算数**（那只是把"追"改写成"等"），system
  不会采纳。要改只有两种合法走向：往离现价更远的方向调（更保守），或者
  直接说"这一波不做"。锚点 15 分钟没成交会自动作废，到时候重新按结构判断，
  别顺着旧价位往下说。**趋势市里锚点整体停用**：你说"等回踩到 Y"不会留下
  任何记录，下一轮也看不到——趋势市的标准动作是顺势直接做，"等一个价位"
  在这里不是合法选项。

【判断原则】
- **不做交易是允许的，而且经常是对的。** 给出 `"act": false` 即可，
  这是正常决策，不是失误，不需要找补。
- 反过来，结构确实给出方向时就敢做。**p_up 0.40 和 0.60 是同样的强度**：
  一个是做空、一个是做多，没有哪个方向"更安全"、也不需要更高门槛。
  只要与 0.5 的距离够 {MIN_EDGE}（系统闸门）且位置确认过，就该动手，
  别拿"还不够有把握"当借口把机会放过去。
- **act=false 的理由必须分行情，不许只写"再看看"：**
  · 震荡市：方向不清，或位置不在边缘（这是老规矩）；
  · 趋势市：方向不清，或当前根是插针顶/接刀形状——**唯独不能是"位置高/低"**。
    趋势市里 p_up ≤ 0.40（明确顺势方向）却给 act=false，reason 里必须写清
    是当前根的哪个具体形状让你不做（比如"上影 62%、收盘掉到区间 38%，插针"），
    不许写"等回踩""等企稳"这种没有落点的话——趋势市没有"等"这个选项。
- 即使 act=true，p_up 与 0.50 的差距不到 {MIN_EDGE} 时系统仍会跳过——
  那是因为成本不值，不是你的判断有问题。
- 上面的教训是你自己的历史，不是摆设：当前情形如果和某条教训说的是同一回事，
  就按它做——包括"这种情况不要做"。
- **成本不由你负责。** 点差和手续费由系统承担，你已经知道这一点，不需要计算它。
  不要以"点差太高""成本吃掉利润""利润空间不够覆盖成本"为由拒绝开仓，
  也不要因此偏移方向判断。你只回答一件事：按你判的打法，接下来这段行情往哪边走。"""

ENTRY_PROMPT = """品种 {symbol} · 周期 {tf} · 窗口 14 根（当前 + 前 13 根）
【主】K线（序号 / 时间 UTC / 开 / 高 / 低 / 收）
{bars}
【主】{structure}
{mtf}{ma}
【辅】Tick 级数据（逐笔 bid/ask，时间段与上面 14 根完全相同；只用于确认突破真假与点差）
{ticks}
短周期变化 r1={r1:+.3%}  r5={r5:+.3%}  r13={r13:+.3%}  振幅={span}
战绩 样本={n_closed} 笔  命中率={hit}
{recent_close}{wait_note}{skip_note}{lessons}
报价 bid={bid}  ask={ask}  点差={spread_pts} 点
时间 {clock} UTC"""

EXIT_SYSTEM = """你是 {SYMBOL_LABEL} 超短线交易员的持仓管理模块，只决定「继续持有」还是「立刻平仓」。
这套打法的钱来自「方向对的时候拿够一段」，不是靠秒级周转——实测 60 秒就跑的
时候平均只抓到很小一段（黄金实盘口径：约 1 美元），而同样一次信号按结构拿下去
能走到几美元。做得越多亏得越快。

【信息边界】
你只能看到两样东西：
  1) 当前 1 根 M1 和之前 13 根，共 14 根（约 14 分钟），以及这段时间的逐笔 tick；
  2) 一行 M5 背景摘要（14 根 M5 约 70 分钟的结构结论，程序算好的，没有原始 K 线）。
除此之外没有更多数据。M5 只用来判断"这波大方向还有没有余量"，
不改变 M1 的出场时机判断。
（消息里另有这笔单自己的事实——方向、开仓价、打法、浮盈峰值、当初的开仓依据，
那是持仓状态，不是行情。）

【输入格式（顺序固定）】
  1) 品种与周期
  2) 【主】14 根 M1 K 线：序号 / 时间 / 开 / 高 / 低 / 收
  3) 【主】结构指标：ER、振幅与 ATR 倍数、收盘位置、连阳连阴、行情判定
  4) 【背景】M5 结构摘要（一行；拿不到时不出现）
  5) 【辅】tick 数据（只用于确认突破真假与点差异常）
  6) 短周期变化
  7) 当前持仓：方向 / 手数 / 开仓价 / 已持仓时间 / 浮动盈亏 / 当初开仓依据
  8) 本笔打法（趋势单或震荡单，开仓时锁定）与浮盈峰值
  9) 你从自己交易里总结的教训（按重复次数排序）
 10) 当前报价
 11) 当前时间（最后一行）

出场同样要看教训：像「止跌没延续就该立刻走」这种话，只有出现在出场判断里才有用。

【输出】
只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"close": false, "reason": ""}
close —— true 表示立刻市价平仓，false 表示继续持有。
reason —— 不超过 40 字的中文依据，必须引用具体数字。

【系统的自动规则（不用你判断，也不要跟它较劲）】
- 浮亏达到 {SL_USD} {MONEY_UNIT}：系统直接市价平，不会问你；
- 持仓满 {MAX_HOLD_MIN} 分钟：系统无条件平掉，不管盈亏；
- 浮盈不到 {TP_MIN_USD} {MONEY_UNIT}：系统也不问你要不要平，直接继续持有。
{BE_RULE}这些数字会随配置变，以本段显示的为准，不要引用你记忆里的旧值。
所以**你被问到，就说明这笔单已经浮盈 ≥ {TP_MIN_USD} {MONEY_UNIT}**，而且还没到 {MAX_HOLD_MIN} 分钟。
（金额单位是账户的结算货币：美元户是 USD，美分户是 USC。同一个数字在两种
账户上的实际价值差 100 倍，所以以这里写明的单位为准，不要自己换算。）{LOT_RULE}

【判断原则】
- 主看 K 线结构，tick 只做辅证；两者冲突时以 K 线为准。
- 止损和超时已经由系统兜住了，你不用考虑「万一反手怎么办」。
- 你唯一的任务是判断**这波行情还有没有余量**：
  · 结构没走完（仍贴趋势方向、ER 在走高、连续阴/阳没被打断）→ 继续持有；
  · 结构已经走完（贴到反向沿、连续形态被打断、ER 掉回震荡区）→ 落袋。
- 成本不由你负责：点差由系统承担，不要用"点差吃掉利润"当平仓理由。
- 必须二选一，不存在"再等等看情况"。
- 别因为"已经浮盈够出场门槛了"就急着落袋，也别死拿等它翻倍——看结构，不看金额。"""

EXIT_PROMPT = """品种 {symbol} · 周期 {tf} · 窗口 14 根（当前 + 前 13 根）
【主】K线（序号 / 时间 UTC / 开 / 高 / 低 / 收）
{bars}
【主】{structure}
{mtf}
【辅】Tick 级数据（逐笔 bid/ask，时间段与上面 14 根完全相同）
{ticks}
短周期变化 r1={r1:+.3%}  r5={r5:+.3%}  r13={r13:+.3%}
持仓 {direction_text} {lots} 手  开仓 {entry}  已持仓 {held_text}  浮动 {pnl_text}
本笔打法 {plan_text}
{peak_text}
开仓依据 {entry_reason}
你从自己交易里总结的教训
{lessons}
报价 bid={bid}  ask={ask}
时间 {clock} UTC"""

REVIEW_SYSTEM = """你是一名 {SYMBOL_LABEL} 超短线交易员的复盘教练。仓位已经平掉了，这一笔的钱已经落袋，
所以你现在可以看比平时更多的 K 线：**平仓前 7 根 + 平仓这根 + 平仓后 7 根，共 15 根**。
平仓后那 7 根是你判断"这笔平得对不对"的唯一依据：
  - 平完行情继续顺着原方向走 → 平早了，该多拿一会儿；
  - 平完行情掉头 → 平得对，这个离场信号要记住；
  - 平完来回磨 → 无关紧要，重点看进场那一端。
开仓和平仓是两个独立决策，哪个错就骂哪个，别混成一句"方向看错了"。

【最重要的判据：错在哪一端】
这条教训会被注入到**下一次进场**或**下一次出场**的判断里，注错地方就是噪音，
所以你必须先回答"错的是哪一端"，再从固定清单里选一个主题：
  scope = "entry" → 进场这一端错了（方向看反、位置不对、该等没等、追了/接了刀）
  scope = "exit"  → 出场这一端错了（平太早、平太晚、该走没走、拿着不放）
  scope = "both"  → 两端都错，或者这笔纯粹是行情不给、说不出错在哪

  theme 只能从这 8 个里选一个，照抄原文，不许自己造词：
  方向与依据矛盾 / 逆势抢反弹/接刀 / 追破位/假突破 / 进场时点不对 /
  止盈太早 / 止损太晚 / 出场时点不对 / 其他
  "其他"只在确实归不进上面 7 类时用；拿不准时宁可归到最接近的那一类。

只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"scope": "exit", "theme": "止盈太早", "lesson": ""}
lesson —— 一句话，不超过 60 字，说清「主要因为什么赚/亏，下次同样情形该怎么做」。
不要客套，不要复述数字以外的事实。"""

REVIEW_PROMPT = """这一笔刚平掉。
方向：{direction_text}　手数：{lots}　开仓 {entry} → 平仓 {exit_price}
最终盈亏：{pnl:+.2f} {MONEY_UNIT}　持仓 {held_text}
开仓理由：{why_in}
平仓理由：{why_out}

你被允许回看的区间：平仓前 {before} 根 → 平仓这根 → 平仓后 {after} 根（共 {n} 根 {tf}）
序号  时间(UTC)      开      高      低      收
{bars}

只输出那个 JSON 对象（scope / theme / lesson 三个字段），复盘内容全写在 lesson 里。"""


def exit_params(cfg: Cfg, plan: str | None) -> dict:
    """这一笔持仓按哪套出场参数走。**由开仓时的行情决定，持仓期间不换**。

    为什么不跟着当前行情实时切：一笔单在震荡里开、十分钟后变成趋势，
    如果出场参数跟着跳，同一笔仓会先后被两套互相矛盾的规则管——
    窄止盈的场子里它该走了，宽止盈的场子里它该留，面板和复盘都说不圆。
    开仓那一刻锁定，和等待锚点锁定是同一个原则。

    返回 dict 而不是 tuple：调用方只取自己关心的键，以后加参数不用改签名。
    `plan` 为空（旧数据、state 里没存）一律按震荡参数，保持历史行为不变。

    金额单位统一是**账户结算货币**：美元户读到 3.00 就是 3 美元，
    美分户读到 3.00 就是 3 分。同一个 3.00 在美分户上正好对应小 100 倍的
    仓位（0.01 手 XAUUSDc = 0.01 盎司），价距仍然是 3.00 美元——两者等价。

    返回的金额已按 `money_at_lots(cfg, …)` 等比缩放到当前手数：**这里是一切
    出场金额的唯一出口**。手数放大 N 倍，金额也放大 N 倍，价距保持不变；
    否则 0.02 手就会把 3.00 USD 止损压成 1.50 价距。调用方不要再自己读
    `cfg.sl_usd` 直接比浮盈，那会绕过缩放。
    """
    if plan == "trend":
        return {
            "plan": "trend",
            "sl_usd": money_at_lots(cfg, cfg.sl_usd),
            "tp_min_usd": money_at_lots(
                cfg, cfg.tp_min_trend if cfg.tp_min_trend > 0 else cfg.tp_min_usd),
            "max_hold_sec": int(cfg.max_hold_trend or cfg.max_hold_sec),
        }
    return {
        "plan": plan or "range",
        "sl_usd": money_at_lots(cfg, cfg.sl_usd),
        "tp_min_usd": money_at_lots(cfg, cfg.tp_min_usd),
        "max_hold_sec": int(cfg.max_hold_sec),
    }


# 模型在 reason 里"说我这一轮还不进场"的措辞。
#
# 只收「待办/未来时」的写法，**刻意不收裸的"企稳""止跌""回踩"**：
# 实测赢单的 reason 里全是"收阳企稳""连阳止跌""回踩到位符合贴边做多"，
# 把裸词收进来会把赚钱的单一起挡掉（10-27 那轮 +4.00 / +4.04 / +5.12 三笔
# 的 reason 都含"企稳/止跌"，正好会被误伤）。
# 跨周期冲突的判据阈值：M5 窗口净变动（drift）达到这个幅度，才算"中期有方向"。
# M1 十四根窗口的振幅中位约 1.5 USD，M5 的 drift 到 5 USD 已经是单边推进。
# **样本支撑很弱**（历史带 M5 字段的只有 26 笔），所以这条只做建议不做闸门。
MTF_CONFLICT_DRIFT = 5.0

_HESITATE_WORDS = (
    "等确认", "等企稳", "等止跌", "等收阳", "等收阴", "等回踩", "等反抽",
    "等回抽", "等放量", "等突破", "等信号",
    "再进场", "再入场", "再进",
    "挂多", "挂空", "挂单", "不追",
)


def _hesitation_word(reason: str) -> str | None:
    """reason 里有没有"我现在先不进"的措辞。有就返回命中的那个词。

    为什么按词匹配而不是让模型自己保证：提示词里「注意 act 和 reason
    必须一致」那段已经写了
    「写了'不追''等确认'，act 就是 false」，10-27 那轮 10 笔里违反 3 次，
    这 3 笔合计 -9.75，占当天亏损 58%。规则写在提示词里只能"劝"，
    写在代码里才能"拦"。
    """
    text = str(reason or "")
    for w in _HESITATE_WORDS:
        if w in text:
            return w
    return None


def _entry_advice(cfg: Cfg, direction: int, feat: dict,
                  reason: str, act) -> tuple[str, str] | None:
    """**程序对这笔开仓的建议**。返回 (代号, 说明)，None = 没意见。

    2026-10-28 定位调整（用户定的）：程序只提供建议和特征，不替模型做决定。
    所以这个函数只负责"算出程序怎么看"，拦不拦由 `_gate_blocks()` 按开关决定，
    两个开关默认都是关的——默认行为是：模型想开就开，程序把意见写进日志，
    面板上标一句"模型否决了建议"，事后可以按 `would_veto` 分桶算期望。

    刻意做成纯函数：tick() 里那堆东西没法单测，而这两条判断恰恰是
    "以后还会被人想改"的地方，必须能一个一个用例锁死。

    两条都只在 act 不为 False 时才有意义——模型自己说不做的时候，
    走到这里之前就已经 return 了。
    """
    if act is False:
        return None
    # 建议 1：理由与动作自相矛盾。
    w = _hesitation_word(reason)
    if w:
        return ("act_guard",
                f"理由自相矛盾：写了「{w}」（这是「先不进」的意思），"
                f"却给了 act=true。若照做，成交价会和你说的等待价对不上")
    # 建议 2：贴上沿做多。
    # `_zone_text` 在任何一个 regime 分支下都不会给出"贴上沿做多"
    # （震荡→做空、上涨→等回踩低吸、下跌→做空），所以这条不是新规则，
    # 是让代码和位置判定对齐。10-27 那轮两笔追多（分位 0.97 / 0.96）
    # 合计 -6.26，全在这一条上。
    if direction > 0:
        pos = feat.get("pos")
        line = 1.0 - float(cfg.zone_edge)
        if pos is not None and float(pos) >= line:
            return ("top_long_ban",
                    f"贴上沿做多：入场收盘位置 {float(pos):.2f} ≥ {line:.2f}，"
                    f"买在窗口最高处。上涨趋势里贴上沿是强势延续不是超买，"
                    f"此处追多＝接最后一棒；等回踩到下沿再低吸")
    # 建议 3：中期方向与这笔相反（跨周期冲突）。
    #
    # 实测 2026-10-28 22:07 那笔：M1 ER=0.2359 判震荡 → 打法允许「贴下沿做多」，
    # 而同一时刻 M5 净变动 -7.11、价格在 MA28 下方 -1.98——中期是单边下行，
    # 所谓"下沿"其实是下跌途中的一个台阶。这笔 -3.15 硬止损，和 21:34 那笔
    # （M5 -7.72 / ma28_gap -3.47，也是 -3.15）一模一样。
    # 打法规则只看 M1 的 ER，中期方向在这套规则里没有发言权，这是漏洞不是噪音。
    #
    # 阈值定 5.0 的依据（**样本很小，只够当提示不够当规律**）：
    # 历史带 M5 特征的 26 笔里，M5 ≤ -5 时做多 5 笔均值 -1.88、做空 4 笔 -1.03；
    # 而赢的那笔（21:52 上沿做空）M5 只有 -2.45。
    #
    # 2026-11-06 扩到趋势单（用户要求），但**只记录、永不拦截**：
    #   · 逻辑上没理由放过：趋势单能拿 3 小时、震荡单 1 小时，逆中期方向的
    #     暴露时间是 3 倍，越该管的地方反而没管，这是漏的；
    #   · 但现有 5 笔趋势单里，冲突 3 笔均值 **+0.20**、非冲突 2 笔 **-1.70**，
    #     且被砍掉的会包括唯一一笔 +3.71（peak 7.39）。样本 5 笔不足以下结论；
    #   · 冲突率高达 3/5：趋势单的「冲突」多半是"14 分钟脉冲 vs 70 分钟方向"，
    #     直接否决等于把趋势打法关掉一半。
    # 所以代码用**独立代号** mtf_conflict_trend，`_gate_blocks` 对它永远返回
    # False（没有开关）。先攒数据，等趋势单样本到 20 笔以上再谈阈值和拦不拦。
    regime = feat.get("regime")
    if regime in ("range", "trend"):
        mtf = feat.get("mtf") or {}
        drift = mtf.get("drift")
        if drift is not None:
            try:
                drift = float(drift)
            except (TypeError, ValueError):
                drift = None
        if drift is not None and abs(drift) >= MTF_CONFLICT_DRIFT and direction * drift < 0:
            side = "下行" if drift < 0 else "上行"
            act_text = "做多" if direction > 0 else "做空"
            if regime == "trend":
                return ("mtf_conflict_trend",
                        f"中期方向冲突：M5 净变动 {drift:+.2f}（明显{side}），"
                        f"与这笔{act_text}相反。M1 的 ER 只说最近 14 分钟走得直，"
                        f"M5 是 70 分钟的中期方向——这是逆着中期追短期脉冲（仅记录）")
            return ("mtf_conflict",
                    f"中期方向冲突：M5 净变动 {drift:+.2f}（明显{side}），"
                    f"与这笔{act_text}相反。M1 判震荡只是短期没方向，"
                    f"中期在{side}，此时贴边{act_text}＝逆着中期接刀；"
                    f"要么顺着中期做，要么等中期走平")
    return None


def _gate_blocks(cfg: Cfg, code: str) -> bool:
    """这条建议要不要升级成硬拦。两个开关默认关（2026-10-28 起）。

    留着开关而不是删代码：拦与不拦各自的期望要能用数据说话，
    删掉就再也测不回来了。想恢复 10-27 的严格模式，把这两个字段置 True。
    """
    if code == "act_guard":
        return bool(cfg.act_guard)
    if code == "top_long_ban":
        return bool(cfg.top_long_ban)
    if code == "mtf_conflict":
        return bool(cfg.mtf_conflict)
    if code == "mtf_conflict_trend":
        # **故意没有开关**（2026-11-06）。趋势单的跨周期冲突目前只记录：
        # 5 笔样本里冲突组均值 +0.20、非冲突 -1.70，且冲突率 3/5——现在拦
        # 就是把趋势打法关掉一半，还可能砍掉唯一那笔 +3.71。
        # 要升级成闸门，先拿 20 笔以上的趋势单把阈值（现在是绝对美元 5.0）
        # 标定清楚，并改成按 ATR 归一化，别直接照搬震荡单的数。
        return False
    return False


# ------------------------------------------------------------ 等待锚点（锁定）
#
# 模型说"等回踩到 4044 再进"，下一分钟又说"等回踩到 4049.9"——它**没有上一轮
# 的记忆**（prompt 里不含历史决策），所以目标价跟着价格一起上移：名义上在等，
# 实际上在追。2026-10-27 22:29–22:40 实测十二分钟：4037 → 4038 → 4041 →
# 4043 → 4045 → 4046 → 4049.9，价格涨了 12 美元，一次都没等到。
# 危害不只是错过：真回调来时它锚在新高位上，一接就接在半山腰。
#
# 治法不是让它"记性变好"，而是给它一个外部记忆——锚点一次设定就锁住，
# 只允许往**离现价更远**的方向调（那是更保守的等待）；每轮 prompt 把
# 锚点、差价、等了多久写回去。它想挪，系统不采纳并记一笔 drift。
_WAIT_RE = re.compile(
    r"(?:等回踩|等反抽|等回到|等价格回到|等跌到|等回抽|回踩到|回踩至|反抽到)"
    r".{0,14}?(\d{4,5}(?:\.\d{1,2})?)")
WAIT_TTL_SEC = 900        # 锚点最长存活 15 分钟：再久这条等待已经不是同一个结构
WAIT_DRIFT_ATR = 0.25     # 朝现价方向挪动超过 0.25 倍均幅 = 漂移


def parse_wait_level(reason: str, price: float | None = None) -> float | None:
    """从 reason 里抠出"等回踩到 X"的那个 X。抠不到返回 None。

    只认紧跟在等待措辞后面的价位数，不做全句扫数字：reason 里还有
    "MA28 上方 +10.1""ER 0.55""5.5 倍均幅""前高 4050.9"这些数字，
    全句扫必然抓错。离现价 0.5% 以上的也丢掉——M1 十四根窗口的振幅
    通常只有几美元，差半个百分点开外的价位等的根本不是这一波结构。
    """
    m = _WAIT_RE.search(str(reason or ""))
    if not m:
        return None
    try:
        level = float(m.group(1))
    except (TypeError, ValueError):
        return None
    if price and abs(level - float(price)) > float(price) * 0.005:
        return None
    return level


def _wait_touched(wait: dict, price: float, side: int) -> bool:
    """价格有没有已经踩到锚点（多：跌到价下方；空：涨到价上方）。"""
    level = float(wait.get("level") or 0.0)
    if not level:
        return False
    return price <= level if side > 0 else price >= level


def _wait_note(wait: dict | None, price: float, atr: float, symbol: str,
               now: int, ttl: int = WAIT_TTL_SEC) -> str:
    """把上一轮的锚点写回 prompt。没有锚点就返回空串（模板里不留空行）。"""
    if not isinstance(wait, dict) or not wait.get("level"):
        return ""
    level = float(wait["level"])
    side = int(wait.get("side") or 1)
    age = max(0, int(now) - int(wait.get("ts") or 0))
    px = lambda v: _px(symbol, v)          # noqa: E731
    if age >= ttl:
        return (f"【等待锚点·已作废】上一轮定的 {px(level)} 等了 {age // 60} 分钟仍没成交，"
                f"这条等待作废——按现在的结构重新判断，不要接着旧价位往下说。\n")
    if _wait_touched(wait, price, side):
        return (f"【等待锚点·已到位】你等的 {px(level)} 已经触及（现价 {px(price)}）。"
                f"这一轮必须二选一：act=true 就在这个位置做，或者明确放弃这条等待。\n")
    gap = (price - level) * (1 if side > 0 else -1)
    mult = gap / atr if atr else 0.0
    return (f"【等待锚点·锁定】{age} 秒前你定了等 {px(level)} 再进"
            f"{'多' if side > 0 else '空'}，现价 {px(price)}，还差 {gap:.3f}"
            f"（约 {mult:.1f} 倍单根均幅）。锚点由系统锁定：把价位改成更靠近现价的"
            f"数字**不会生效**——那只是把「追」改写成「等」。要么继续等这个价，"
            f"要么这轮直接说「这一波不做」。\n")


# ------------------------------------------------------- 上一轮判断（回灌）
#
# 模型没有跨轮记忆：每轮 prompt 都是从零拼的（见 agent/llm.py chat_ex），
# 上一轮说了什么、之后价格往哪走了，下一轮它一概不知道。等待锚点治的是
# 「等回踩」被改写成「追」，但趋势市里锚点整体停用——于是趋势市里它连续
# 说「不做」时，每一轮都是全新的旁观者，每轮理由单看都成立，合起来是
# 整段行情没参与。
#
# 治法同锚点：给它一份外部记忆。只回灌**结论 + 之后发生了什么**。
# 只回灌结论是灾难：模型会为了自洽继续坚持上一轮（p_up=0.00 那次 46% 的
# 样本就是照抄 system 示例，模型自我重复的倾向有硬证据）。带上「你没进场，
# 价格走了 X 倍均幅」才是反馈，不带就是自我强化。
SKIP_NOTE_MAX_SEC = 600    # 10 分钟前的判断不再提：结构早换了，提它只是噪音
SKIP_STREAK_MIN = 3        # 连续几轮才提「整段错过」（1~2 轮不做很正常）
SKIP_STREAK_ATR = 1.5      # 且这段位移要够大，否则 3 轮横盘也算「错过」


def _skip_note(prev: dict | None, price: float, atr: float, symbol: str,
               now: int, max_sec: int = SKIP_NOTE_MAX_SEC) -> str:
    """把上一轮「不做」的判断和之后的价格走向写回 prompt。无记录返回空串。"""
    if not isinstance(prev, dict):
        return ""
    try:
        ts = int(prev.get("ts") or 0)
        asked = float(prev.get("bid") or 0.0)
    except (TypeError, ValueError):
        return ""
    age = max(0, int(now) - ts)
    if not ts or age > max_sec:
        return ""
    p_up = prev.get("p_up")
    try:
        p_up_txt = f"p_up={float(p_up):.2f}"
    except (TypeError, ValueError):
        p_up_txt = "p_up 未给"
    reason = str(prev.get("reason") or "").strip().replace("\n", " ")[:40]
    head = f"【上一轮你的判断】{age} 秒前 · 不做（{p_up_txt}）"
    if reason:
        head += f" · “{reason}”"
    if not asked:
        return head + "\n"
    dg = _digits(symbol)
    move = float(price) - asked
    mult = abs(move) / atr if atr else 0.0
    return (f"{head}\n"
            f"之后价格 {_px(symbol, asked)} → {_px(symbol, price)}"
            f"（{move:+.{dg}f}，≈{mult:.1f} 倍单根均幅），你没有进场。\n")


def _skip_streak_note(streak: dict | None, price: float, atr: float,
                      symbol: str, now: int, regime: str = "",
                      min_n: int = SKIP_STREAK_MIN,
                      min_atr: float = SKIP_STREAK_ATR) -> str:
    """连续 N 轮不做、且这期间价格已经走了一段 —— 明说「你在整段错过」。

    不给这一句，模型每轮都只看眼前 14 根 K 线，"这几根没机会"和"这半小时
    没机会"在它眼里长得一样。
    """
    if not isinstance(streak, dict):
        return ""
    try:
        n = int(streak.get("n") or 0)
        start_px = float(streak.get("price") or 0.0)
        start_ts = int(streak.get("ts") or 0)
    except (TypeError, ValueError):
        return ""
    if n < min_n or not start_px or not start_ts:
        return ""
    move = float(price) - start_px
    mult = abs(move) / atr if atr else 0.0
    if mult < min_atr:
        return ""
    dg = _digits(symbol)
    mins = max(1, (int(now) - start_ts) // 60)
    if regime == "trend":
        # 措辞要和 system 的【判断原则】对齐：趋势市不许拿"位置高/低"当理由。
        ask = ("趋势市没有「等回踩」这个选项——要么按现价顺势做，要么明确"
               "承认这一段放弃，并把理由写成当前根的形状证据，"
               "不要再用「位置太高/太低」当不做的理由。")
    else:
        ask = ("如果你在等一个回踩价，它一直没来。按当前结构重新判断："
               "要么现在做，要么承认这段放弃，不要再把等待价往现价挪。")
    return (f"【连续未进场】你已经连续 {n} 轮判「不做」，{mins} 分钟里价格 "
            f"{_px(symbol, start_px)} → {_px(symbol, price)}"
            f"（{move:+.{dg}f}，≈{mult:.1f} 倍单根均幅）。{ask}\n")


def _skip_step(streak: dict | None, act, bid: float, now: int) -> dict | None:
    """推进「连续未进场」计数：act=false 才累积，做了就清零。"""
    if act is not False:
        return None
    if not isinstance(streak, dict) or not streak.get("ts"):
        # 首轮：记下起点价，后面所有位移都相对它算（跟锚点同一个道理，
        # 起点跟着价格挪，那个数就永远算不出"整段"）。
        return {"n": 1, "ts": int(now), "price": float(bid)}
    out = dict(streak)
    out["n"] = int(streak.get("n") or 0) + 1
    out["last"] = int(now)
    return out


def _remember_skip(state: dict, act, bid: float, now: int, p_up,
                   reason, ok: bool = True) -> None:
    """记下这一轮的「不做」，供下一轮回灌。真做了就清空（它已经知道结果，
    平仓那边有自己的 recent_close，不需要这里再说一遍）。

    `ok=False`（调用失败 / 输出解析不了）什么都不动：那不是它的判断，
    覆盖上一轮真实结论等于把「我本来在等 4044」改成「我上轮连话都没说」，
    而且技术故障也不该被算进「连续 N 轮不做」里。
    """
    if not ok:
        return
    if act is False:
        state["prev_entry"] = {"ts": int(now), "bid": float(bid),
                               "p_up": p_up, "reason": str(reason or "")[:60]}
        state["skip_streak"] = _skip_step(state.get("skip_streak"), act, bid, now)
    else:
        state["prev_entry"] = None
        state["skip_streak"] = None


def _wait_step(wait: dict | None, reason: str, act, price: float, atr: float,
               now: int, side: int, ttl: int = WAIT_TTL_SEC,
               tol_atr: float = WAIT_DRIFT_ATR,
               regime: str = "") -> tuple[dict | None, str]:
    """推进锚点状态机，返回 (新状态, 本轮标记)。

    标记：
      set       —— 第一次定锚
      keep      —— 这轮没提价位，继续等
      move      —— 往更保守的方向改（允许，ts 不变：等的还是同一次机会）
      drift     —— 朝现价方向挪动 = 跟着价格跑，**不采纳**，锚点不变
      expired   —— 超过 ttl，作废
      consumed  —— 已经踩到锚点却仍不进，锚点作废（不敢做就别占着）
      cleared_open —— 这轮进场了，等待结束
      disabled  —— 趋势市里锚点整体停用（2026-10-28），连"set"都不接受

    disabled 的理由：锚点是「等回踩到 X 低吸」的配套，实测这批等回踩的单
    47 笔均值 -1.21（接刀）。趋势打法改成直接顺势后，模型在趋势市说
    "等回踩到 Y" 不应再产生任何状态——下一轮它自己也不会看到锚点，
    从机制上断了"名义上等、实际上追"的老路。
    """
    if regime == "trend":
        return None, "disabled"
    old = wait if isinstance(wait, dict) and wait.get("level") else None
    if act is True:
        return None, "cleared_open"
    flag = ""
    if old and now - int(old.get("ts") or 0) >= ttl:
        old, flag = None, "expired"
    level = parse_wait_level(reason, price)
    if level is None:
        if old and _wait_touched(old, price, side):
            return None, "consumed"
        return old, flag or "keep"
    if old is None:
        return {"level": level, "ts": now, "side": side, "drift": 0}, flag or "set"
    tol = max(float(atr or 0.0) * tol_atr, 0.05)
    drifted = (level > old["level"] + tol) if side > 0 else (level < old["level"] - tol)
    if drifted:
        return {**old, "side": side, "drift": int(old.get("drift") or 0) + 1}, "drift"
    return {"level": level, "ts": old.get("ts") or now, "side": side,
            "drift": int(old.get("drift") or 0)}, "move"


def _mtf_bundle(symbol: str, zone_edge: float = 0.35,
                digits: int = 3) -> dict:
    """更高周期背景：M5 14 根的结构摘要。返回 {"text": ..., "info": ...}。

    返回两个东西是刻意的：`text` 拼进 prompt，`info` 落进特征快照。
    只取一次 M5 行情就同时满足两处，不在 tick 里查两遍。

    为什么只给结论不给原始 K 线：见 MTF_TF 处的注释（token + 一致性）。

    设计上刻意**不让它推翻 M1**：system 里写明 M5 只决定"更愿意站哪一侧"，
    不参与 1~3 分钟的进出场判定。否则模型会拿 M5 的趋势去否决 M1 的位置，
    而 M1 的超短线逻辑本来就是"在震荡里做边缘"，两者会互相拆台。
    """
    empty = {"text": "", "info": None}
    try:
        rows = _bars(symbol, MTF_TF, MTF_BARS)
    except Exception:  # noqa: BLE001
        return empty
    w = [r for r in (rows or []) if r][-MTF_BARS:]
    if len(w) < MTF_MIN_BARS:
        return empty
    s = _structure(w)
    if not s.get("ok"):
        return empty
    a = _atr(w)
    d = max(0, int(digits))
    drift = s["drift"]
    dir_text = "上行" if drift > 0 else ("下行" if drift < 0 else "走平")
    has_atr = bool(a.get("ok") and a.get("atr", 0) > 0)
    atr_part = (f"｜振幅 {s['span']:.{d}f}（{a['span_atr']:.1f} 倍单根均幅）"
                if has_atr else f"｜振幅 {s['span']:.{d}f}")
    regime = _regime_word(s["er"], drift)
    text = (f"【背景】M5 {len(w)} 根（约 {len(w) * 5} 分钟）："
            f"ER={s['er']:.2f}（净移动 {drift:+.{d}f} {dir_text}）"
            f"｜收盘位置 {s['pos']:.0%}{atr_part}｜判定：{regime}")
    info = {
        "tf": MTF_TF,
        "n": len(w),
        "er": round(s["er"], 4),
        "pos": round(s["pos"], 4),
        "drift": round(drift, 5),
        "regime": regime,
        "span_atr": round(a.get("span_atr") or 0.0, 3),
    }
    return {"text": text, "info": info}


def _mtf_text(symbol: str, zone_edge: float = 0.35, digits: int = 3) -> str:
    """只要 prompt 那一行的简版包装（测试和 trade_detail 用）。"""
    return _mtf_bundle(symbol, zone_edge, digits)["text"]


def _fmt_bars(rows: list[dict], digits: int = 3) -> str:
    """喂给决策提示词的 K 线。**永远只有 14 根**，不接受"多给一点"。"""
    return _fmt_bar_lines(visible_bars(rows), digits)


def _ticks_text(cfg: Cfg, bars: list[dict]) -> str:
    """把 tick 聚合表准备好。拿不到就返回一句说明，绝不让 prompt 缺字段。"""
    if not cfg.ticks:
        return "（tick 数据已关闭，只能依据 K 线判断）"
    ticks = fetch_ticks(cfg, bars)
    return _fmt_tick_block(summarize_ticks(ticks, bars, _point(cfg.symbol)),
                           _digits(cfg.symbol))


def build_entry_prompt(rows: list[dict], bid: float, ask: float,
                       symbol: str = "XAUUSDm", tf: str = "M1",
                       point: float = 0.001, stats: dict | None = None,
                       lots: float = 0.01, lessons: str = "",
                       ticks_text: str = "", zone_edge: float = 0.35,
                       mtf_text: str = "",
                       recent_close: dict | None = None,
                       wait_note: str = "", skip_note: str = "",
                       trend_follow: bool = True) -> str:
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
    dg = _digits(symbol)
    # MA28 用**未截断的 rows**（至少 28 根）。拿不到就整块不出现，
    # 不能让模板里留一行空的【背景】让模型猜。
    ma_text = ma_line(rows, dg)
    # 刚平仓上下文：只在前 5 分钟（报复单定义窗口）注入，之后不提——
    # 每一轮都提「你平仓很久了」是噪音。300 秒 = 复盘体系里报复单的判定窗口。
    rc_text = ""
    rc = recent_close or {}
    seconds_ago = int(rc.get("seconds_ago") or 0)
    if 0 < seconds_ago < 300:
        pnl = rc.get("pnl")
        pnl_text = (f"（盈亏 {float(pnl):+.2f} {money_label(_acct_currency())}）"
                    if pnl is not None else "")
        rc_text = (
            f"【刚平仓提醒】{seconds_ago} 秒前你刚平掉一笔{pnl_text}。\n"
            "刚平仓后的几分钟是「报复单」高发窗口——你自己的教训库标着"
            "「亏损后快速再入场」是坏行为。除非眼前的结构确实满足上面的打法，"
            "否则宁可 act=false 等下一轮。\n"
        )
    return ENTRY_PROMPT.format(
        symbol=symbol, clock=clock, bid=_px(symbol, bid), ask=_px(symbol, ask),
        spread_pts=spread_pts,
        n=len(w), tf=tf, bars=_fmt_bar_lines(w, dg),
        structure=_structure_text(w, zone_edge, dg, trend_follow),
        r1=r1, r5=r5, r13=r13, span=_px(symbol, span),
        n_closed=n_closed, hit=hit,
        lessons=lessons or "暂无复盘笔记。",
        recent_close=rc_text,
        wait_note=wait_note or "",
        skip_note=skip_note or "",
        mtf=mtf_text or "",
        ma=(ma_text + "\n") if ma_text else "",
        ticks=ticks_text or "（tick 数据暂不可用，只能依据上面的 K 线判断）",
    )


def build_exit_prompt(rows: list[dict], position: dict, bid: float, ask: float,
                      tf: str = "M1", contract: float = 100.0,
                      ticks_text: str = "", symbol: str = "XAUUSDm",
                      lessons: str = "", zone_edge: float = 0.35,
                      mtf_text: str = "", trend_follow: bool = True,
                      tp_min: float = 0.0) -> str:
    w = visible_bars(rows)
    r1, r5, r13, _ = _returns(w)
    direction = int(position.get("direction") or 1)
    entry = float(position.get("price_open") or 0.0)
    lots = float(position.get("lots") or 0.0)
    cur = bid if direction > 0 else ask  # 平仓价：多头看 bid，空头看 ask
    # × _acct_factor()：价差 × 合约 × 手数 算出来是美元，模型和阈值都用
    # 账户货币（美分户是分），两边必须同口径，否则「浮盈 0.01」和
    # 「止盈门槛 3.25」在美分户上永远对不上。
    pnl = (cur - entry) * direction * contract * lots * _acct_factor()

    held = int(time.time()) - int(position.get("opened_at") or time.time())
    held_text = f"{held // 60} 分 {held % 60} 秒" if held >= 60 else f"{max(held, 0)} 秒"
    dg = _digits(symbol)
    pnl_text = f"{pnl:+.2f} {money_label(_acct_currency())}（{cur - entry:+.{dg}f}）"
    # 打法在开仓那一刻就定死了（见 exit_params）。这里只是把它讲给模型听：
    # 不知道自己是趋势单还是震荡单，模型会拿震荡的"贴反向沿就走"去管趋势单。
    plan = position.get("plan") or "range"
    plan_text = ("趋势单（开仓时判为趋势市）：拿的是一段行情，不是一次抖动。"
                 "别因为回到区间中位、或刚浮盈就想跑——只有结构走完才平"
                 if plan == "trend" else
                 "震荡单（开仓时判为震荡或方向不明）：赚的是区间内的来回，"
                 "贴到反向沿、形态被打断就走，别指望它变成趋势")

    # 峰值浮盈（2026-10-29）。这一行是「到过门槛后每分钟复核」的**前提**：
    # 光把模型叫来、不给它"已经摸到过 7.39 又吐回 6.08"这个事实，它会当成
    # 一笔普通的 +6.08 趋势单照旧回答"继续拿"——每分钟问一次、每次都说拿住，
    # 等于把 token 烧在信息缺失的判断上。**模型没有跨轮记忆**，峰值必须
    # 每一轮都写在 user 里，不能指望它记得上一轮。
    tp_min = float(tp_min or 0.0)
    try:
        peak = float(position["peak_pnl"]) if position.get("peak_pnl") is not None else None
    except (TypeError, ValueError):
        peak = None
    if tp_min <= 0:
        peak_text = ""
    elif peak is None:
        peak_text = f"浮盈峰值 未记录（止盈门槛 {tp_min:.2f}）"
    elif peak >= tp_min:
        back = peak - pnl
        # peak_pnl 由快查循环按 2~5 秒采样，可能比此刻的浮盈略低
        # （写这句话时的报价和上一次采样不同）。回落为负就别说"回落 −0.03"，
        # 那读起来像程序算错了。
        back_txt = (f"随后回落 {back:.2f} 到当前 {pnl:+.2f}" if back > 0.01
                    else f"当前 {pnl:+.2f}，比记录到的峰值还高")
        peak_text = (f"浮盈峰值 {peak:+.2f}（**已触及**止盈门槛 {tp_min:.2f}，"
                     f"{back_txt}）"
                     "—— 这笔单曾经够格落袋，现在必须明确回答「结构还在、继续拿」"
                     "还是「回落已经破坏结构、现在就平」，不要含糊带过")
    else:
        peak_text = (f"浮盈峰值 {peak:+.2f}（止盈门槛 {tp_min:.2f}，尚未到线），"
                     f"当前 {pnl:+.2f}")

    return EXIT_PROMPT.format(
        symbol=symbol, clock=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        direction_text="多" if direction > 0 else "空",
        lots=lots, entry=_px(symbol, entry), held_text=held_text,
        bid=_px(symbol, bid), ask=_px(symbol, ask), pnl_text=pnl_text,
        entry_reason=(position.get("entry_reason") or "未记录")[:120],
        plan_text=plan_text,
        peak_text=peak_text,
        n=len(w), tf=tf, bars=_fmt_bar_lines(w, dg),
        structure=_structure_text(w, zone_edge, dg, trend_follow, for_exit=True),
        r1=r1, r5=r5, r13=r13,
        lessons=lessons or "暂无复盘笔记。",
        mtf=mtf_text or "",
        ticks=ticks_text or "（tick 数据暂不可用，只能依据上面的 K 线判断）",
    )


# ------------------------------------------------------------------ 平仓复盘
#
# 允许 AI 在**平仓之后**回看「平仓前 13 根 → 开仓之前」。
# 为什么这时候可以多看：仓位已经没了，回看不可能改变这一笔的结果，
# 只影响下一笔。信息边界是防止它偷看未来做决策，不是禁止它学习。


def _sl_price_for(cfg, pos: dict) -> float:
    """这笔单子的**硬止损价**。0 表示没有止损线。

    `cfg.sl_usd` 的金额单位是**账户结算货币**（美元户是 USD，美分户是分），
    必须换成价格距离才能当价格用。换算两头都要换算成同一口径：
        单位数 × (1 USD 折多少账户货币) = 每 1.00 价距亏多少账户货币
    美分户 0.01 手 XAUUSDc：0.01 盎司 × 100 = 1，所以 3.00 分 → 价距 3.00。
    """
    if not cfg.sl_usd or float(cfg.sl_usd) <= 0:
        return 0.0
    try:
        lots = float(pos.get("lots") or 0.0)
        entry = float(pos.get("price_open") or 0.0)
        if lots <= 0 or entry <= 0:
            return 0.0
        # 价距与手数**无关**：金额阈值随手数等比放大，除回去正好相消。
        # 所以这里既不能读 pos["lots"]（复盘回放里那笔单可能是 0.03 手开的，
        # 而 cfg 是当前配置，两者未必同数），也不能直接读缩放后的金额——
        # 唯一自洽的算法是用基准手数把金额换回价距。
        dist = price_dist_of(cfg, cfg.sl_usd)
        if dist <= 0:
            return 0.0
        d = 1 if int(pos.get("direction") or 1) > 0 else -1
        return entry - d * dist
    except (TypeError, ValueError):
        return 0.0


def build_review_prompt(rows: list[dict], position: dict, exit_price: float,
                        pnl: float, held_sec: int, tf: str = "M1",
                        close_ts: int | None = None,
                        after: int = REVIEW_AFTER_BARS,
                        symbol: str = "XAUUSDm") -> str:
    direction = int(position.get("direction") or 1)
    held_text = f"{held_sec // 60} 分 {held_sec % 60} 秒" if held_sec >= 60 else f"{max(held_sec, 0)} 秒"
    close_ts = int(close_ts if close_ts is not None else (rows[-1].get("time") if rows else 0))
    dg = _digits(symbol)
    return REVIEW_PROMPT.format(
        direction_text="多" if direction > 0 else "空",
        lots=float(position.get("lots") or 0.0),
        entry=_px(symbol, position.get("price_open")),
        exit_price=_px(symbol, exit_price), pnl=pnl, held_text=held_text,
        MONEY_UNIT=money_label(_acct_currency()),
        why_in=(position.get("entry_reason") or "未记录")[:120],
        why_out=(position.get("exit_reason") or "外部平仓")[:120],
        before=REVIEW_BEFORE_BARS, after=after, n=len(rows), tf=tf,
        bars=_fmt_review_bars(rows, close_ts, dg),
    )


def ask_review(cfg: Cfg, position: dict, exit_price: float, pnl: float,
               opened_at: int, closed_at: int, login=None,
               exit_source: str = "") -> dict:
    """平仓满 7 根之后要一句复盘。失败就返回空，绝不影响主流程。

    `exit_source` = 谁发起的这次平仓（"sl" / "be" / "be_local" / "tp" /
    "llm" / "timeout" / "panel"）。
    **"sl"（程序硬止损）是特殊情况**：出场那一刻不是模型选的，是代码按
    sl_usd 砍的，模型从头到尾没有出场决策权。这时候复盘却把它归成
    「止损太晚 · scope=exit」——出场端的错误模型根本无从改起，
    而真正该学的「这个位置/方向当初就不该开」被锁在出场端，
    永远进不了下一次入场的提示词（实测 2026-10-28 22:07：上一笔 21:46
    刚写下「破 4047.4 双底立即止损」，下一笔又在 4047.44 接多，
    因为那条教训 scope=exit，入场端压根看不到）。

    所以 sl 平仓强制升级成 scope="both"，并在返回里标 `src="sl"`，
    注入端的 `_scope_ok` 凭这个标记放行到入场端。2026-10-29 起保本止损
    （be / be_local）走同一条路：那也是"程序/券商替你出的场"，见
    _FORCED_EXIT_SOURCES。
    """
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
                                 after=REVIEW_AFTER_BARS, symbol=cfg.symbol)
    try:
        text, usage = _chat(prompt, system_for(REVIEW_SYSTEM, cfg))
    except Exception as exc:  # noqa: BLE001
        _log_call(False, None, f"{type(exc).__name__}: {exc}", login,
                  purpose="autotrade_review")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _log_call(True, usage, login=login, purpose="autotrade_review")
    parsed = parse_review(text)
    if parsed is None:
        return {"ok": False, "error": "模型复盘返回空"}
    # 给这条教训打上「当时是什么行情」的标签：震荡里攒的教训，
    # 下次震荡时优先拿出来。**只用平仓前那一段算 ER**——平仓后的 7 根
    # 是给复盘判断用的，不属于这笔单子进场时面对的行情，混进来会串味。
    pre_close = [r for r in window if int(r.get("time") or 0) <= int(closed_at)]
    post_close = [r for r in window if int(r.get("time") or 0) > int(closed_at)]

    # 出场类的复盘要过一道反事实：模型说"平太早"，但平后的 7 根里行情
    # 要么先跌回止损线、要么先回撤掉半个止盈门槛——两种情况继续持有都比落袋差，
    # 这笔其实平得对。两条判据见 exit_counterfact。
    theme = parsed["theme"]
    verdict = verdict_for(theme, parsed["lesson"])
    cf = ""
    if theme in ("止盈太早", "止损太晚", "出场时点不对") or verdict:
        # 回撤线取「半个止盈门槛」：多拿的代价到这个量级，
        # 收益和风险就是同一枚硬币，"平太早"不能算教训。
        # dd_limit 要和 post_bars 的 high/low 比，是**价格**不是金额：
        # 手数放大后 tp_min_usd 的金额也放大，直接除 2 会变成一条永远碰不到的线
        # （等于反事实校验静默失效）。
        cf = exit_counterfact(post_close, int(position.get("direction") or 1),
                              _sl_price_for(cfg, position), exit_price,
                              dd_limit=price_dist_of(cfg, cfg.tp_min_usd or 0.0) / 2.0)
    if verdict == VERDICT_EARLY and cf:
        verdict = VERDICT_OK
        theme = "离场正确"
        reason = "平仓后行情先回撤到止损线" if cf == "sl_hit" else "平仓后回撤已超过半个止盈门槛"
        lesson_extra = f"（反事实校正：{reason}，继续持有会更差）"
    else:
        lesson_extra = ""

    scope = parsed["scope"]
    src = ""
    forced = str(exit_source or "").lower()
    if forced in _FORCED_EXIT_SOURCES and scope != "both":
        # 出场不是模型决定的 → 这条教训不能只归出场端，否则入场永远学不到。
        scope = "both"
        src = forced
    elif forced in _FORCED_EXIT_SOURCES:
        src = forced

    # 出场类教训的 regime 跟 plan 走，不用 regime_of(pre_close)。
    # regime_of 回答的是「平仓那一刻面对什么行情」，而这条教训真正绑定的是
    # 「这笔按哪套出场参数管」（trend: tp 7.0 / 3h；range: tp 3.25 / 1h）——
    # 两套参数下同一个动作的结论可以相反，混进同一域会互相抵消。
    # 实例（2026-11-10 22:38 那笔趋势空单）：开仓时 pos=0.03 追空、回抽走到
    # 3.8 ATR，保本止损把 +50 打成 0；平仓前 15 根在 4033~4046 来回，
    # regime_of 判 range，"be 别贴开仓价"于是进了震荡域——而下一次震荡单
    # 读到它只会更糟（震荡单本来就用 3.25 的窄门槛，贴着走是对的）。
    # 入场类（scope=entry）不改：开仓那一刻还没有 plan，行情本身就是 regime。
    plan = str(position.get("plan") or "")
    if scope in ("exit", "both") and plan in ("trend", "range", "mid"):
        reg = plan
    else:
        reg = regime_of(pre_close)

    return {"ok": True, "lesson": parsed["lesson"], "theme": theme,
            "scope": scope, "bars": len(window),
            "verdict": verdict, "counterfact": cf, "lesson_extra": lesson_extra,
            "regime": reg, "src": src}


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
                         int(pos.get("opened_at") or closed_at), closed_at, login,
                         exit_source=pos.get("exit_source") or "")
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    row = {"ts": int(time.time()), "kind": "review", "closed_at": closed_at,
           "ticket": pos.get("ticket"),
           "direction": pos.get("direction"), "pnl": round(pnl, 2)}
    if res.get("ok"):
        row["lesson"] = res.get("lesson")
        row["bars"] = res.get("bars")
        row["regime"] = res.get("regime") or ""
        # 主题与端：ask_review 正常会给；测试桩 / 老式返回只给 lesson 时在这里兜底，
        # 保证落盘的行永远带这两个字段，注入端不用再猜。
        row["theme"] = res.get("theme") or row_theme(row)
        sc = res.get("scope") or ""
        row["scope"] = sc if sc in LESSON_SCOPES else row_scope(row)
        # 出场方向 + 反事实结论：聚类时按 verdict 重分派，counterfact 留作审计。
        # 被反事实推翻的那条（模型说平太早、但平后先回撤到止损线）在正文后
        # 追加一句校正，免得以后翻文本时又把它当成"该多拿"的证据。
        row["verdict"] = res.get("verdict") or ""
        row["counterfact"] = res.get("counterfact") or ""
        # 平仓发起方（sl = 程序硬止损）。硬止损的教训两端都要看，见 _scope_ok。
        row["src"] = res.get("src") or ""
        extra = res.get("lesson_extra") or ""
        if extra and row.get("lesson"):
            row["lesson"] = f"{row['lesson']}{extra}"
    else:
        row["error"] = res.get("error")
    append_log(row, login)
    # 教训同时进独立教训库：它不随流水归档，重置战绩不会把经验一起清掉。
    # 失败行（error）不进库——那不是经验，是"这次没复盘成功"。
    if row.get("lesson"):
        append_lesson({
            "ts": row["ts"], "ticket": row.get("ticket"), "lesson": row["lesson"],
            "theme": row["theme"], "scope": row["scope"],
            "verdict": row.get("verdict") or "",
            "counterfact": row.get("counterfact") or "",
            "regime": row.get("regime") or "", "pnl": row.get("pnl"),
            "direction": row.get("direction"), "source": "live",
            "src": row.get("src") or "", "closed_at": closed_at,
        }, login)
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
        # 平仓发起方。复盘要拿它决定这条教训归出场端还是两端都看。
        "exit_source": pos.get("exit_source") or "",
        # 开仓时锁定的打法。`ask_review` 靠它决定出場教训归哪个 regime 域
        # （trend 与 range 是两套出场参数，同一动作结论可以相反）。
        # 2026-11-10 补：原来这里不带 plan，于是只有"决策轮当场复盘"能拿到它，
        # 走补跑队列的那些笔永远落到 `regime_of(pre_close)`——平仓后行情怎么走
        # 就归到哪个域，和这笔当初按哪套参数管无关。
        "plan": pos.get("plan") or "",
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
               "exit_source": item.get("exit_source") or "",
               # 必须把 plan 带进去：`ask_review` 用它给出场教训定域。
               # 少了这一个字段，补跑出来的复盘会被 `regime_of(pre_close)` 接管，
               # 和决策轮当场复盘的同一种单归到不同域——两条路必须同口径。
               "plan": item.get("plan") or "",
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


# JSON mode 只该因为「这个模型/供应商根本不认这个参数」而永久关闭。
# 以前把任何异常都当成"不支持"：一次 429 或超时就把 JSON mode 关掉不再打开，
# 之后整条链路（进场/出场/复盘）都在用纯文本——实测 10-23 有 70 次 429/超时，
# 等于那一天基本没吃到 JSON mode。临时错误现在只抛给调用方，不动开关。
_JSON_OFF_HINTS = ("response_format", "json_object", "not support", "unsupported",
                   "unknown parameter", "unrecognized", "unexpected keyword")


def _json_mode_unsupported(exc: BaseException) -> bool:
    if isinstance(exc, TypeError):
        return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(h in msg for h in _JSON_OFF_HINTS)


def _chat(prompt: str, system: str):
    """调模型。优先要求 JSON mode（省输出 token、免解析失败），不支持就退回。"""
    global _JSON_MODE_OK
    if _JSON_MODE_OK:
        try:
            return _llm().chat_ex(prompt, system=system, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            if not _json_mode_unsupported(exc):
                # 429 / 超时 / 网络 / 模型名写错：和 JSON mode 无关。
                # 不关开关、也不在这里重试——重试只会再烧一次配额，
                # 调用方本来就有"这一轮不做/继续持有"的失败处理。
                raise
            _JSON_MODE_OK = False       # 真不支持，记住，别每轮白试
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
              ticks_text: str = "", mtf_text: str = "",
              recent_close: dict | None = None,
              wait_note: str = "", skip_note: str = "") -> dict:
    """让模型给上涨概率。**模型没答上来就不做**，不拿规则替它下单。"""
    prompt = build_entry_prompt(rows, bid, ask, cfg.symbol, cfg.tf,
                                _point(cfg.symbol), stats,
                                lots=cfg.lots,
                                lessons=recent_lessons(
                                    login, ignore_cost=cfg.ignore_spread,
                                    regime=regime_of(rows), scope="entry"),
                                zone_edge=cfg.zone_edge,
                                ticks_text=ticks_text,
                                mtf_text=mtf_text,
                                recent_close=recent_close,
                                wait_note=wait_note,
                                skip_note=skip_note,
                                trend_follow=cfg.trend_follow)
    try:
        text, usage = _chat(prompt, system_for(ENTRY_SYSTEM, cfg))
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
             login: int | str | None = None, ticks_text: str = "",
             mtf_text: str = "") -> dict:
    prompt = build_exit_prompt(rows, position, bid, ask, cfg.tf, contract,
                               ticks_text=ticks_text, symbol=cfg.symbol,
                               lessons=recent_lessons(
                                   login, ignore_cost=cfg.ignore_spread,
                                   regime=regime_of(rows), scope="exit"),
                               zone_edge=cfg.zone_edge,
                               mtf_text=mtf_text,
                               trend_follow=cfg.trend_follow,
                               tp_min=float(exit_params(
                                   cfg, position.get("plan"))["tp_min_usd"] or 0.0))
    try:
        text, usage = _chat(prompt, system_for(
            EXIT_SYSTEM, cfg, plan=position.get("plan")))
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
# 真正的开仓间隔由 cooldown_sec（平仓冷却）和 interval_sec（决策周期）管，
# 这里只防同一个 tick 里连环开仓（状态写入和仓位读取之间的竞态窗口）。
MIN_OPEN_GAP_SEC = 3


def system_for(template: str, cfg: Cfg, plan: str | None = None) -> str:
    """把 system 模板里的占位符换成**当前配置的实际值**。

    除了品种名，出场阈值也必须换：`sl_usd` / `tp_min_usd` / `max_hold_sec`
    都是可配的，而 system 里原来硬编码着 3.0 / 3.25 / 60 分钟。cfg 一改，
    system 就在骗模型——它会以 3.0 为前提判断"系统什么时候会砍我"，
    实际却是 5.0。这类"界面说一套、代码做一套"的错位最难查，因为不报错。

    用 str.replace 而不是 str.format：system 里含 JSON 示例的花括号，
    format 会把它当占位符直接炸。
    """
    # 出场参数按**这笔单的打法**取（趋势单和震荡单不是一套数字）。
    # plan 为空 = 震荡参数，老调用方不用改。
    ep = exit_params(cfg, plan)
    out = template.replace("{SYMBOL_LABEL}", _symbol_label(cfg.symbol))
    out = out.replace("{SL_USD}", f"{ep['sl_usd']:g}")
    out = out.replace("{TP_MIN_USD}", f"{ep['tp_min_usd']:g}")
    # 金额单位跟着账户走（USD / USC）。模型看到「3」和「3 分」是两件事，
    # 而它给出的浮盈数字已经是账户货币——不给单位会让它按美元理解。
    out = out.replace("{MONEY_UNIT}", money_label(_acct_currency()))
    out = out.replace("{MAX_HOLD_MIN}", f"{int(ep['max_hold_sec']) // 60}")
    # 保本止损是**模型看不见的**第二条退出线：趋势单峰值到过 be_arm_usd 后，
    # 券商端 SL 被抬到开仓价，之后回落到开仓价就成交了——不问模型。
    # 不告诉它，它会把这个平仓读成"结构走完了"，而实际上是"回到起点"。
    # 只给趋势单（规则本身见 _be_applies）；震荡单不适用，关掉也整行不出现。
    be_arm = money_at_lots(cfg, getattr(cfg, "be_arm_usd", 0.0) or 0.0)
    if plan == "trend" and bool(getattr(cfg, "be_at_entry", False)) and be_arm > 0:
        be_rule = (f"- 趋势单浮盈到过 {be_arm:g} {money_label(_acct_currency())}："
                   "系统会把券商端止损抬到开仓价，之后价格回落到开仓价会直接平掉"
                   "（约保本），同样不问你要不要平。\n")
    else:
        be_rule = ""
    out = out.replace("{BE_RULE}", be_rule)
    # 手数一变，上面的止损/止盈金额也等比变，但模型看的是价格 K 线。
    # 只给金额不说价距，它会把「浮亏 9.00 才止损」读成一段更远的行情——
    # 实际上价距仍是 3.00。0.01 手时不注入任何文字，保持原提示词逐字不变。
    scale = lot_scale(cfg)
    if abs(scale - 1.0) > _SCALE_EPS:
        # 价距要用 price_dist_of 换（合约 × 基准手数 × 账户系数），不能拿金额
        # 除以倍数：那只在 0.01 手 XAUUSDm（1 盎司）这种 1:1 的口径下成立，
        # 换到美分户就是把「3.00 美元」当成「3.00 美分」报给模型。
        sl_dist = price_dist_of(cfg, cfg.sl_usd)
        # ep 里的金额已经是缩放后的，价距不变，除回去就是这一档的价距
        tp_dist = price_dist_of(cfg, ep["tp_min_usd"]) / scale
        lot_rule = (
            f"\n- 本场开仓 {float(cfg.lots):g} 手：浮盈亏金额随手数等比放大，"
            f"上面的止损 / 止盈金额对应的价格距离仍分别是 "
            f"{sl_dist:g} / {tp_dist:g}；判断结构看价格，不要拿金额换算。")
    else:
        lot_rule = ""
    out = out.replace("{LOT_RULE}", lot_rule)
    # 贴上沿做多的分位门槛（= 1 - zone_edge）。写成 100-... 而不是硬编码 65：
    # 面板上一改 zone_edge，system 里那句话就必须跟着变，否则又是"说一套做一套"。
    out = out.replace("{ZONE_UP}",
                      f"{max(1, min(99, round((1 - float(cfg.zone_edge)) * 100)))}")
    # 决策间隔也是面板项：提示词里原来写死「60 秒后你会拿到新的 K 线」，
    # 用户把 interval_sec 调成 30 就变成了假话——模型会按 60 秒估下一次机会。
    out = out.replace("{INTERVAL_SEC}", str(int(cfg.interval_sec)))
    # 闸门阈值同理：写死 0.06 的话，min_edge 一改提示词就在骗模型。
    out = out.replace("{MIN_EDGE}", f"{float(cfg.min_edge):g}")
    return out


def cooldown_left(state: dict, now: int, direction: int | None, cfg: Cfg) -> int:
    """平仓冷却还剩几秒。同向用 cooldown_same_sec，反向/方向未定用 cooldown_sec。

    方向为 None 时按**短**的算：这道判定在问模型之前也要跑一次，那会儿还
    不知道模型要做哪个方向，它只负责省掉冷却期内的无效 API 调用。同向那
    180 秒由开仓前、拿到 direction 之后的第二次判定兜住（见 tick 里
    send_open 之前那一段）。
    """
    last = state.get("last_close_ts")
    if not last:
        return 0
    cd = int(cfg.cooldown_sec)
    same = int(cfg.cooldown_same_sec or 0)
    if direction and same > cd and int(state.get("last_close_dir") or 0) == int(direction):
        cd = same
    return max(0, cd - (now - int(last)))


def _cool_cfg(state: dict) -> Cfg:
    """从 state 里的 cfg 快照还原一份 Cfg，只为算冷却剩余秒数。

    为什么不直接调 `cfg_from_state()`：那个会再 `load_state()` 一次，而
    `summary()` / `pending_signal_view()` 是面板每秒轮询的接口，state 已经
    在手上，没必要为了两个整数再读一遍盘。
    """
    raw = state.get("cfg")
    if not isinstance(raw, dict):
        return Cfg()
    try:
        return Cfg(**{k: v for k, v in raw.items() if k in Cfg.__dataclass_fields__})
    except (TypeError, ValueError):
        return Cfg()


def signal_cool_left(state: dict, sig: dict | None, now: int | None = None) -> int:
    """这条**入场**信号还受同向平仓冷却约束几秒（0 = 不受约束）。

    为什么需要它
    ------------
    辅助模式的弹卡发生在「人同意之后那道复核」**之前**：`_new_signal()` →
    落盘 → 通知面板，同向 `cooldown_same_sec` 的判定在 `send_open()` 前的
    `can_open(direction=...)`。于是冷却期内的同向信号照样会弹，人点了同意
    才收到 `void_state`「同向平仓后冷却中，还需 N 秒」——**让人对一张点了
    也没用的卡做决策**。

    2026-11-09 实盘：20:17 那笔趋势多单 21:11:33 平仓，21:12:11 就弹出
    「震荡贴下沿做多」，同向冷却还剩 106 秒；人否决后以为错过一波 9 美元
    的上涨，实际那张卡本来也执行不了。

    判定只有 `cooldown_left()` 一处，这里不复制阈值。出场信号不受冷却约束
    （平仓永远允许），所以只认 `kind == "entry"`。
    """
    if not sig or str(sig.get("kind") or "") != "entry":
        return 0
    direction = int(sig.get("direction") or 0)
    if not direction:
        return 0
    ts = int(time.time()) if now is None else int(now)
    return cooldown_left(state, ts, direction, _cool_cfg(state))


def can_open(state: dict, now: int, has_position: bool, cfg: Cfg,
             direction: int | None = None) -> tuple[bool, str]:
    if state.get("stop_reason"):
        return False, f"已停止：{state['stop_reason']}"
    if has_position:
        return False, "品种上已有持仓，不叠加"

    if cfg.max_trades and int(state.get("trades") or 0) >= cfg.max_trades:
        return False, f"已达开仓上限 {cfg.max_trades} 笔"

    if cfg.max_loss_usd and float(state.get("pnl") or 0.0) <= -abs(cfg.max_loss_usd):
        return False, (f"累计亏损已达 {cfg.max_loss_usd:.2f} "
                       f"{money_label(_acct_currency())} 上限")

    left = cooldown_left(state, now, direction, cfg)
    if left > 0:
        same_dir = bool(direction) and (int(state.get("last_close_dir") or 0)
                                        == int(direction))
        return False, f"{'同向' if same_dir else ''}平仓后冷却中，还需 {left} 秒"

    last_open = state.get("last_open_ts")
    if last_open and now - int(last_open) < MIN_OPEN_GAP_SEC:
        left = MIN_OPEN_GAP_SEC - (now - int(last_open))
        return False, f"距上次开仓不足 {MIN_OPEN_GAP_SEC} 秒，还需 {left} 秒"

    return True, "可开仓"


# ------------------------------------------------------------------ 终端读写


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


def _symbol_probe(symbol: str) -> tuple[bool, object | None, list[str]]:
    """品种在这个终端里存不存在；不存在时给出名字相近的候选。

    返回 `(查过没有, info, 近名候选)`。**第一个值是三态的钥匙**：
    没连上终端时无从判断，必须返回 False 让调用方跳过检查——否则
    「读不到」会被当成「不存在」，无终端环境下每条配置都过不了体检。

    为什么必须单独查（2026-11-02）：换账户后品种名会变（标准户 XAUUSDm →
    美分户 XAUUSDc），而名字不存在时 `symbol_info_tick` 只是返回 None，
    不报错——流水里留一行「拿不到报价」，看着像行情断了，其实是配错了名字。
    启动时当场点出来，比让他盯着一小时的空跑日志强。
    """
    try:
        from live.journal import _mt5, mt5_call

        mt5 = _mt5()
        info = mt5_call(mt5.symbol_info, symbol)
        if info is not None:
            return True, info, []
        near: list[str] = []
        try:
            prefix = symbol[:3]
            for s in (mt5_call(mt5.symbols_get, prefix + "*") or []):
                near.append(str(getattr(s, "name", "")))
        except Exception:  # noqa: BLE001
            near = []
        return True, None, [n for n in near if n][:8]
    except Exception:  # noqa: BLE001
        return False, None, []


def precheck(cfg: Cfg) -> dict:
    """发真单之前的体检。纸面模式只查品种（没报价连纸面也跑不动）。"""
    issues: list[str] = []
    # 品种存在性排最前：它决定后面所有检查有没有意义。
    # 换账户后最常见的坑就是名字变了，而错误表现只有「拿不到报价」。
    checked, info, near = _symbol_probe(cfg.symbol)
    if checked and info is None:
        hint = f"；终端里相近的品种：{'/'.join(near)}" if near else ""
        issues.append(
            f"终端里没有品种 {cfg.symbol}（换账户后品种名会变，"
            f"美分账户的黄金是 XAUUSDc）{hint}"
        )

    if cfg.dry_run:
        return {"ok": not issues, "errors": issues}

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


def _try_protect(ex, ticket: int, sl: float, tp: float | None) -> dict:
    """发一轮保护线改单（含 3 次重试）。`tp=None` 时只动止损。

    抽出来是因为**同一份重试逻辑要跑两次**：先把 SL+TP 一条发完，被券商
    整单拒掉（10016 无效止盈之类）时再退回只挂 SL。重试的分支条件
    （busy / 持仓不存在 = 时序问题，值得再等一格）必须两次完全一致，
    复制一遍迟早分叉——2019 年那种「两处各写一份，改了一处」的坑。
    """
    last: dict = {}
    for i in range(3):
        try:
            r = (ex.modify_sl(ticket, sl) if tp is None
                 else ex.modify_protect(ticket, sl, tp))
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            r["tries"] = i + 1
            return r
        last = r
        # busy / 持仓还没出现 = 时序问题，退一步再试；其余（方向非法等）重试无益。
        retryable = bool(r.get("busy")) or "不存在" in str(r.get("reason") or "")
        if not retryable:
            break
        if i < 2:
            time.sleep(0.3)
    return {**last, "ok": False, "tries": i + 1,
            "reason": str(last.get("reason") or "改单失败")}


def _attach_entry_sl(ex, cfg: Cfg, res: dict, direction: int,
                     tp: float | None = None) -> dict:
    """开仓成交后，把这笔的**保护线**挂到券商端。失败不抛异常、不影响开仓。

    为什么在成交之后挂，而不是把 sl/tp 塞进开仓请求：
      · 塞进请求时只能用报价算，滑点会让它偏离真实成交价。偏到「离成交价
        更近」那一侧就是白送一次被扫；偏太多还可能让券商以 10016
        「无效止损」**把整笔开仓拒掉**——挂保护线失败绝不该让开仓也失败；
      · 成交后挂用的是真实成交价，和程序内存里 `_sl_price_for()` 用的是同
        一个 `price_dist_of()`，两道线重合，不会出现「券商那边比程序近」。

    **SL 和 TP 必须一条请求发完，不能分两步**（2026-11-12 实盘打回）：
    Exness 这个账户每秒只放两个交易请求。开仓一个、挂 SL 一个，再单独发
    一个 SLTP 挂 TP 就是第三个 → `10024 Too many trade requests`，
    实测 TP 一次都没挂上。合成一条 `TRADE_ACTION_SLTP` 后请求数回到
    「开仓 + 改单」两条，和 2026-11-09 以来一直稳定的挂止损模式一致。

    `tp` 只有一个来源：`_entry_tp_price()` 算好的价（只给非趋势单）。
    没有 tp 时行为与 2026-11-09 完全一致。

    被整单拒（比如止盈价被判非法）时**退回只挂止损**：止盈是锦上添花，
    止损是保命，绝不能因为多要一条线把保命的那条也丢掉。

    重试 3 次（0.3 秒间隔）：市价单成交后 position 未必立刻出现在
    `positions_get` 里（会返回「持仓不存在」），终端刚下完单也可能正忙
    （busy）。方向非法这类参数错重试没用，直接放弃，交给轮询守卫。
    """
    if cfg.dry_run:
        # 纸面单在券商端没有仓位，改单无从谈起；开仓请求本身仍会落盘留证。
        return {"ok": False, "skipped": "dry_run"}
    if not cfg.sl_usd or float(cfg.sl_usd) <= 0:
        # sl_usd=0 是「这笔不要硬止损」的显式设定，不是漏配。
        return {"ok": False, "skipped": "sl_disabled"}
    ticket = int(res.get("order") or 0)
    if ticket <= 0:
        return {"ok": False, "skipped": "no_ticket"}
    dist = price_dist_of(cfg, cfg.sl_usd)
    entry = float(res.get("price") or 0.0)
    if dist <= 0 or entry <= 0:
        return {"ok": False, "skipped": "no_price_or_dist"}
    # 价距必须显著大于点差，否则这条线挂在点差里面：多单的 SL 落在 bid 之上，
    # 一开仓就是"已触发"，等于把仓位白送出去。
    #
    # 这不是理论担忧：`price_dist_of()` = 金额 /（合约 × 基准手数 × 账户系数），
    # 而 `_symbol_contract()` 在 `mt5.symbol_info()` 拿不到该品种时会**静默退回
    # 100**。同一笔 sl_usd=3.0，`XAUUSDc`（contract=1）算出来是 3.0 价距，
    # 写错成 `XAUUSDm`（contract 退到 100）就是 0.03 —— 正好落在这 0.24 的
    # 点差里，挂上去必被瞬间扫掉。品种代码是面板传进来的，配错一次就够了。
    # 所以用点差当尺子把这种情形挡在挂单之前：正常 3.0 对 0.24 绰绰有余，
    # 量级错一档则连 1.5 倍点差都不到。
    q = _quote(cfg.symbol)
    if q is None:
        return {"ok": False, "skipped": "no_quote"}
    spread = max(0.0, float(q[1]) - float(q[0]))
    if dist < 1.5 * spread:
        return {"ok": False, "skipped": "dist_within_spread",
                "dist": dist, "spread": spread, "entry": entry}
    sl = entry - (1 if direction > 0 else -1) * dist
    want_tp = float(tp) if (tp and float(tp) > 0) else None
    r = _try_protect(ex, ticket, sl, want_tp)
    tp_err = ""
    if not r.get("ok") and want_tp:
        # 整单被拒（10016 无效止盈 / 价格已越过止盈线）：退回只挂止损。
        tp_err = str(r.get("reason") or "改单失败")
        r = _try_protect(ex, ticket, sl, None)
    base = {"entry": entry, "dist": dist, "tries": int(r.get("tries") or 1)}
    if r.get("ok"):
        out = {"ok": True, "sl": float(r.get("price") or sl), **base}
        if want_tp and not tp_err:
            out["tp"] = float(r.get("tp") or want_tp)
        if tp_err:
            out["tp_err"] = tp_err
        return out
    out = {"ok": False, "sl": sl, **base,
           "reason": str(r.get("reason") or "改单失败")}
    if tp_err:
        out["tp_err"] = tp_err
    return out


def _entry_tp_price(cfg: Cfg, res: dict, direction: int,
                    plan: str | None) -> dict:
    """震荡单该把止盈挂在哪个价。**纯计算，不发任何券商请求。**

    `wanted` = 这个 plan 本来该不该有止盈线。调用方据此决定 open 行写不写
    `tp_broker`/`tp_err`——趋势单不该每笔都留一条"没挂"的噪音。

    为什么只有震荡单挂（304 笔闭环单反事实，2026-11-11）
    --------------------------------------------------
      · 震荡单 280 笔：实际合计 -95.9；硬 tp@3.25 价距 -> -71.35（**+24.55**，
        只触发了 19 笔）。反向看：175 笔亏损单里只有 2 笔是"赚过又亏回去"，
        所以这条线磨的是尾部擦伤，不是收益来源；
      · 趋势单 24 笔：实际 +44.83（胜率 25%）；硬 tp@3.25 -> 31.77，**-13.06**。
        趋势单靠右尾吃饭，卡死一条 3.25 的线等于把利润来源砍掉。

    所以门槛数值一个都不动（227 笔回放已证明扫参数就是拟合噪声），改的只是
    **谁来执行这条线**。原来的结构是：亏损端券商端机械执行，盈利端要
    "越过门槛 → 问模型 → 模型点头"才平。2026-11-11 那笔震荡多（4034.192 开）
    浮盈摸到 +19.31、TP 线 4037.442 早就穿过，模型回"6 连阳未断、上探空间
    未耗尽"继续持有，44 秒后 -15 被硬止损打掉。盈利端有否决权、亏损端没有，
    这个不对称没有任何道理——挂上券商端 TP 就是把这个否决权收回来。

    `plan` 为空（旧调用、测试）一律不挂：挂错会把趋势单截断，宁可不挂。
    `tp_min_usd<=0` 是「这笔不要止盈门槛」的显式设定，同时也不挂 TP 单。
    """
    if plan is None:
        return {"wanted": False, "tp": 0.0, "skipped": "no_plan"}
    if plan == "trend":
        return {"wanted": False, "tp": 0.0, "skipped": "trend_plan"}
    if cfg.dry_run:
        return {"wanted": True, "tp": 0.0, "skipped": "dry_run"}
    if not cfg.tp_min_usd or float(cfg.tp_min_usd) <= 0:
        return {"wanted": False, "tp": 0.0, "skipped": "tp_disabled"}
    if int(res.get("order") or 0) <= 0:
        return {"wanted": False, "tp": 0.0, "skipped": "no_ticket"}
    # 价距而不是金额：TP 是挂在价格轴上的一条线，和手数无关
    # （`price_dist_of` 的注释里有推导）。3.25 在 XAUUSDc 上正好是 3.25 价距。
    dist = price_dist_of(cfg, cfg.tp_min_usd)
    entry = float(res.get("price") or 0.0)
    if dist <= 0 or entry <= 0:
        return {"wanted": False, "tp": 0.0, "skipped": "no_price_or_dist"}
    # 和止损同一个尺子：价距落在点差里说明 `_symbol_contract()` 退回默认值了
    # （品种代码配错一次就够了），这种情况挂上去等于开仓即离场。
    q = _quote(cfg.symbol)
    if q is None:
        return {"wanted": True, "tp": 0.0, "skipped": "no_quote"}
    spread = max(0.0, float(q[1]) - float(q[0]))
    if dist < 1.5 * spread:
        return {"wanted": True, "tp": 0.0, "skipped": "dist_within_spread",
                "dist": dist, "spread": spread, "entry": entry}
    return {"wanted": True, "skipped": "", "dist": dist, "entry": entry,
            "tp": entry + (1 if direction > 0 else -1) * dist}


def send_open(cfg: Cfg, direction: int, plan: str | None = None) -> dict:
    """市价开仓，成交后挂券商端保护线。失败时 ok=False。

    **一条 SLTP 请求同时挂 SL 和 TP**（`_attach_entry_sl`），结果拆成两份报：
      · `sl_attach` —— 止损的挂载结果，所有单都有；
      · `tp_attach` —— 止盈的挂载结果，**只有非趋势单才有这条线**
        （价由 `_entry_tp_price` 先算好）。
    为什么不是两个请求：Exness 每秒只放两个交易请求，开仓+改单已经用完，
    再发第三个必被 10024 拒（2026-11-12 实盘踩过）。

    `ok` 只反映开仓本身：挂不上保护线时仓位已经真实存在，报成"开仓失败"
    会让调用方以为没开、下一轮再开一笔。挂不上时 open 行留
    `sl_broker=0`/`sl_err`、`tp_broker=0`/`tp_err`，事后面板/统计能看出这笔
    的哪条线不在券商端。

    `plan` 是开仓这一刻锁定的打法（"range"/"trend"/"mid"），由 `tick` 在
    `send_open` 之前就算好。**默认 None = 不挂止盈**：只在明确知道这是震荡
    单时才截断利润，不知道就当趋势单处理。
    """
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
        # 成交了才谈挂线。止盈价先算（纯计算），SL+TP 一条请求发完——
        # 分两步会被券商的「每秒两个交易请求」限流拒掉（见 `_attach_entry_sl`）。
        tp_plan = _entry_tp_price(cfg, res, direction, plan)
        sl_att = _attach_entry_sl(ex, cfg, res, direction,
                                  tp=tp_plan.get("tp") or None)
        res["sl_attach"] = sl_att
        tp_px = float(sl_att.get("tp") or 0.0)
        res["tp_attach"] = {
            "ok": tp_px > 0.0,
            "wanted": bool(tp_plan.get("wanted")),
            "tp": tp_px,
            "skipped": (tp_plan.get("skipped") or sl_att.get("skipped")
                        or ("" if tp_px > 0 else "sltp_failed")),
            "reason": sl_att.get("tp_err") or sl_att.get("reason"),
        }
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            ex.shutdown()
        except Exception:  # noqa: BLE001
            pass


def send_modify_sl(cfg: Cfg, ticket: int, sl: float) -> dict:
    """把持仓的券商端止损改到 `sl`。失败返回 ok=False，不抛异常。

    单独一个函数（而不是复用 send_close 的路径）是因为它是**改单**，不是成交：
    它不改持仓，只改券商端那条线。日志、错误码、重试语义都和开平仓不同。
    """
    ex = _executor(cfg)
    try:
        if not ex.connect():
            return {"ok": False, "reason": "MT5 连接失败"}
        res = ex.modify_sl(int(ticket), float(sl))
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
    row = {"ts": now, "kind": "stop", "reason": reason}
    # 顺序与 _finish 一致：先流水后状态。反过来的话面板会先看到"已停"，
    # 而流水里还没有那一行，说不清是哪一步断的。
    append_log(row, login)
    save_state(state, login)
    return {"kind": "stop", "stop": True, "reason": reason}


def tick(cfg: Cfg, login=None, now: int | None = None,
         quote: tuple[float, float] | None = None,
         rows: list[dict] | None = None,
         positions: list[dict] | None = None) -> dict:
    """跑一轮决策。所有外部依赖都可注入，方便在没有终端的机器上测。

    **异常在这一层兜住并落盘**，不再往上抛。以前全靠 `Worker._loop` 兜：
    tick 中途炸掉时流水可能已经写了一行、state 却没写，面板显示的还是上一轮
    的状态——人以为程序在盯盘，实际这一轮什么都没做。现在异常也走 `_finish`，
    至少留下「这一轮出过错」的痕迹，下一轮照常跑。
    """
    now = int(now or time.time())
    # 内存里那份 state 的引用。异常时必须用它，**不能重新 load_state**：
    # 重读会把这一轮已经做进内存的改动（pending_reviews、model_errors、
    # 刚记下的 pos）全部丢掉，最坏的情况是 `send_open` 已经发出去了、
    # 结算记录还在内存里没落盘——于是"券商有单、面板空仓"，下一轮还会
    # 以为空仓再开一单。
    live_state: list[dict] = []
    try:
        return _tick_body(cfg, login, now, quote, rows, positions, live_state)
    except Exception as exc:  # noqa: BLE001
        row = {"ts": now, "kind": "error",
               "reason": f"{type(exc).__name__}: {exc}"}
        try:
            state = live_state[0] if live_state else load_state(login)
            _finish(state, row, login, ts=now)
        except Exception:  # noqa: BLE001
            # 连 state 都读不出来（磁盘满 / 权限），至少把流水留下
            append_log(row, login)
        return row


def _tick_body(cfg: Cfg, login, now: int,
               quote: tuple[float, float] | None,
               rows: list[dict] | None,
               positions: list[dict] | None,
               live_state: list[dict] | None = None) -> dict:
    """一轮决策的实际实现（now 已由 tick 归一，出口一律走 _finish）。

    `live_state` 是给外层异常兜底用的：这里 load 出来的 state 引用塞进去，
    外层异常时就能拿到"这一轮改到一半"的那份，而不是重新读盘。
    """
    now = int(now or time.time())

    # 到期的复盘先跑掉（平仓后 7 根 K 线才到期）。必须在 load_state 之前：
    # 它自己读写 state，放在后面会被这一轮末尾的 save_state 覆盖回旧队列，
    # 同一条复盘就会反复重跑。
    process_due_reviews(cfg, now, login)

    state = load_state(login)
    if live_state is not None:
        # 交给外层异常兜底：异常时用它落盘，别重新读盘把改动丢了
        live_state.append(state)
    # 孤儿信号：state 里挂着一条待确认信号，但这个进程里没有人在等它
    # （面板重启过、或者上一轮异常退出）。不清理的话面板会一直弹一个
    # 点了也没用的窗，而且 answer_signal 那边会一直拒绝。
    orphan = state.get("pending_signal")
    if orphan and not DESK.waiting((orphan or {}).get("id")):
        _log_signal(orphan, {"answer": "orphan", "waited_sec": 0,
                             "bid": orphan.get("bid"), "ask": orphan.get("ask")},
                    cfg, login)
        state["pending_signal"] = None
        save_state(state, login)

    if state.get("stop_reason"):
        return {"kind": "stop", "stop": True, "reason": state["stop_reason"]}

    # --- 刹车：跑飞了自动停 -------------------------------------------------
    if cfg.max_minutes and state.get("started_at"):
        if now - int(state["started_at"]) >= cfg.max_minutes * 60:
            return _stop(state, f"到达运行上限 {cfg.max_minutes} 分钟", now, login)
    if cfg.max_loss_usd and float(state.get("pnl") or 0.0) <= -abs(cfg.max_loss_usd):
        return _stop(state, f"累计亏损 {state['pnl']:.2f} "
                                f"{money_label(_acct_currency())} 触发上限", now, login)

    # --- 行情 ------------------------------------------------------------
    q = quote if quote is not None else _quote(cfg.symbol)
    if not q:
        row = {"ts": now, "kind": "skip", "reason": "拿不到报价"}
        return _finish(state, row, login, ts=now)
    bid, ask = q

    # 取数要超过可见窗口：MA28 需要 28 根，模型仍然只看到其中最近 14 根
    # （visible_bars 在 build_entry_prompt / _structure 里各截一次）。
    bars = rows if rows is not None else _bars(
        cfg.symbol, cfg.tf, max(int(cfg.bars or 0), MA_PERIOD + 6))

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
            return _finish(state, row, login, ts=now)

    # --- 持仓已经没了（AI 平的 / 你手动平的 / 券商砍的）---------------------
    if pos and not paper and not any(
        int(p.get("ticket") or 0) == int(pos.get("ticket") or -1) for p in live_positions
    ):
        # 盈亏与出场价**以券商成交为准**，不拿此刻的报价估。这一条路的延迟
        # 是几十秒级的，按报价估会把保本平掉的单子记成亏损（2026-11-06）。
        pnl, exit_price, fills = _settle_pnl(cfg, pos, bid, ask)
        # 平仓来源：券商端硬止盈 / 保本止损 / 入场止损被打，还是人工平的。
        # 判定逻辑和文案都在 `_gone_source`——**面板清仓那条路也要用同一份**：
        # 用户在终端手动平掉之后又点「停止并清仓」，那时账户上已经没这笔仓，
        # `close_all` 走的是同一段判定，文案分叉就会让复盘模型把同一件事读成
        # 两种（"人工平仓" vs "终端故障"），写出方向相反的教训（2026-10-29）。
        source, why = _gone_source(pos, None if fills is None else fills["reason"])
        # 计数 / 清仓位 / close 行统一走 _book_close：这条路上的单子以前不带
        # peak_pnl/peak_ts/plan/atr，于是"人工平仓的单子在 ATR 统计里不存在"。
        row = _book_close(state, pos, pnl, now, source=source, reason=why)
        row["exit_price"] = round(float(exit_price), 5)
        # 这一笔的盈亏是券商成交还是报价估的，落盘留着。以后怀疑"记账不对"时
        # 先看这个字段，不用再去反推。
        row["pnl_src"] = "deal" if fills is not None else "quote"
        if pos.get("be_ok"):
            row["be_sl"] = float(pos.get("be_sl") or 0.0)

        # 复盘排进队列：等 7 根 K 线走完再复盘，这期间照常判断下一笔。
        # 平仓当刻就复盘看不出「平完之后怎么走」，等于白问。
        pos["exit_reason"] = why
        queue_review(state, cfg, pos, exit_price, pnl, now)
        return {**_finish(state, row, login), "detected": True}

    # --- 有仓：先过程序闸门，再问模型该不该平 --------------------------------
    if pos:
        contract = _symbol_contract(cfg.symbol)
        held_sec = now - int(pos.get("opened_at") or now)
        float_pnl = _realized_pnl(cfg, pos, bid, ask)

        # 峰值浮盈：这一笔从开仓到现在，浮盈最高摸到过多少（USD）。
        #
        # 存在的唯一目的是**用实测数据回答「3.25 到底几次能到」**。决策轮的
        # 粒度是 60 秒，漏掉的冲高回落由快查循环（_wait_next）按 2~5 秒补上；
        # 两处共用 _track_peak，峰值逻辑同源。
        _track_peak(pos, float_pnl, now)

        # 保本止损（2026-10-29）：趋势单峰值到 be_arm_usd 就把券商端 SL 抬到
        # 开仓价。**在闸门之前做**——同一轮里如果价格正好又跌回开仓价，
        # 闸门要能用上刚记下的 be_arm_ts 做本地兜底。
        # 快查循环也调这一个函数，两处同源（否则 60 秒这一格才知道要挂，
        # 而峰值往往在 2 秒格里就被抓到了），挂上过一次之后自动空转。
        be_row = _be_step(cfg, pos, float_pnl, now)
        if be_row:
            append_log(be_row, login)

        # 脉冲锁利（2026-11-13）：趋势单首次走满 5 ATR 之后，把券商端 SL 从
        # 开仓价抬到「峰值 − 1.5 ATR」，并随峰值继续抬。放在 be 之后 = 覆盖
        # be 抬过的位置（门槛天然更高）。**只改券商端那条线，不新增平仓路径**：
        # 平不平仍由券商 SL / _exit_gate / 模型三方照旧决定。
        lock_row = _lock_step(cfg, pos, now)
        if lock_row:
            append_log(lock_row, login)

        # plan 在开仓时锁定（见 state["position"]），出场参数跟着这笔单打法走，
        # 中途 regime 跳变不换规则——见 exit_params 的注释。
        #
        # stale 先判：它只决定"这一轮要不要问模型"，不决定平不平。
        # 判定和 _wait_next 的快查共用 _stale_hit，两处同源。
        stale = _stale_hit(cfg, pos, float_pnl, now)
        # 到过门槛 → 每分钟复核（2026-10-29）。判定和写 tp_reached_ts 都在这个
        # 函数里，闸门只接两个布尔/整数：tp_seen（到过门槛）、tp_left（距复核秒数）。
        tp_seen, tp_left = _tp_reached_step(cfg, pos, float_pnl, now)
        gate = _exit_gate(cfg, held_sec, float_pnl, pos.get("plan"),
                          stale_ready=stale,
                          be_local=_be_local_hit(cfg, pos, float_pnl),
                          lock_local=_lock_local_hit(cfg, pos, float_pnl),
                          tp_seen=tp_seen, tp_left=tp_left)

        # 辅助模式的痕迹位。默认 auto：走到这里说明是程序或模型自己决定的。
        sig_id = None
        exec_by = "auto"
        sig_slip = None

        # 闸门说不平：直接记 hold，不调模型（省 token，也断了"每轮问一次要不要跑"）
        if gate is not None and not gate.get("close"):
            row = {"ts": now, "kind": "hold", "reason": gate["reason"],
                   "source": gate["source"], "held_sec": held_sec,
                   "ticket": pos.get("ticket"),
                   "float_pnl": round(float_pnl, 2), "peak_pnl": pos["peak_pnl"],
                   "peak_ts": pos.get("peak_ts")}
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": False,
                "reason": gate["reason"], "source": gate["source"],
            }
            return _finish(state, row, login, ts=now)

        if gate is not None:
            # 止损：程序直接平，不问模型，也不占用 model_errors 计数
            decision = gate
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": True,
                "reason": gate["reason"], "source": gate["source"],
            }
        else:
            mtf = _mtf_bundle(cfg.symbol, cfg.zone_edge, _digits(cfg.symbol))
            decision = ask_exit(bars, pos, bid, ask, cfg, contract, login,
                                ticks_text=_ticks_text(cfg, bars),
                                mtf_text=mtf["text"])
            # 峰值停滞唤醒的防抖标记：**只有真问到模型才记**。
            # 写在调用之前的话，一次模型调用失败就会白白吃掉 300 秒唤醒窗口，
            # 而这正是最需要人/模型看一眼的时候。
            if stale and decision.get("source") != "error":
                pos["stale_ts"] = now
                # 留痕：这笔是"系统放行、模型判断"而不是"浮盈到门槛了"。
                # 面板和复盘靠这个把两类决策分开算命中率。
                decision["via"] = "stale"
            # 到过门槛的复核节拍：**只有真问到模型才记**，理由同 stale_ts——
            # 模型调用失败不该吃掉 60 秒的复核窗口。
            if tp_seen and decision.get("source") != "error":
                pos["tp_ask_ts"] = now
                # 留痕：这次是"到过门槛后又回落，按节拍复核"，不是首次穿越。
                # 复盘按 via 分桶时这两类的期望完全不同——一个是"刚够线要不要走"，
                # 一个是"已经吐掉一截要不要止损离场"。setdefault 不会盖掉 stale。
                tp_now = float(exit_params(cfg, pos.get("plan"))["tp_min_usd"] or 0.0)
                if tp_now > 0 and float_pnl < tp_now:
                    decision.setdefault("via", "tp_recheck")
            # 模型失败要计数：进场失败只是少做一笔，出场失败意味着仓位没人管，
            # 连错 3 次必须停下来让人接手，不能让它一直挂在那儿。
            if decision.get("source") == "error":
                state["model_errors"] = int(state.get("model_errors") or 0) + 1
            else:
                state["model_errors"] = 0
            state["last_decision"] = {
                "ts": now, "kind": "exit", "close": decision.get("close"),
                "reason": decision.get("reason"), "source": decision.get("source"),
                "via": decision.get("via"),
            }
            if not decision.get("close"):
                # 埋点和闸门那条 hold 行**同口径**：复盘时要能对齐"这一轮浮盈
                # 多少、峰值摸到过多少、是哪一笔"。2026-10-29 那笔就是因为这里
                # 只有 reason 文本，得手抠"浮盈+4.86仍有空间"才知道当时过门槛了。
                row = {"ts": now, "kind": "hold", "reason": decision.get("reason"),
                       "source": decision.get("source"),
                       "ticket": pos.get("ticket"), "held_sec": held_sec,
                       "float_pnl": round(float_pnl, 2),
                       "peak_pnl": pos["peak_pnl"], "peak_ts": pos.get("peak_ts")}
                if decision.get("via"):
                    row["via"] = decision["via"]
                if decision.get("error"):
                    row["error"] = decision["error"]
                _finish(state, row, login, ts=now)
                if int(state.get("model_errors") or 0) >= 3:
                    return _stop(state,
                                 f"模型连续 {state['model_errors']} 次调用失败，"
                                 "持仓无人管理，已自动停止（请回终端手动处理）",
                                 now, login)
                return row

        # 辅助模式：模型说平仓，先问人。
        #
        # 只拦"模型主动要平"这一条路；止损（gate.source == "sl"）和持仓上限
        # 根本走不到这里——它们在 gate 阶段就定了结论。这是刻意的：
        # **人手确认不等于人肉止损**，一次走神就是 20 美元而不是 3 美元。
        if cfg.assist and gate is None and decision.get("close"):
            until = _assist_deny_until(state, "exit")
            now_real = int(time.time())
            if until and now_real < until:
                row = {"ts": now, "kind": "hold", "source": "human",
                       "reason": (f"人工刚否决平仓，{until - now_real} 秒内不再弹窗"
                                  f"（模型仍在评估，止损照旧）"),
                       "held_sec": held_sec, "float_pnl": round(float_pnl, 2),
                       "peak_pnl": pos.get("peak_pnl")}
                state["last_decision"] = {
                    "ts": now, "kind": "exit", "close": False,
                    "reason": row["reason"], "source": "human",
                }
                return _finish(state, row, login, ts=now)

            sig = _new_signal(
                "exit", cfg, direction=int(pos.get("direction") or 1),
                p_up=pos.get("p_up"), prog_dir=pos.get("prog_dir"),
                agree=pos.get("agree"), act=False,
                reason=decision.get("reason"), bid=bid, ask=ask,
                price=(bid if int(pos.get("direction") or 1) > 0 else ask),
                ticket=pos.get("ticket"), held_sec=held_sec,
                fp=round(float_pnl, 2), wait_sec=cfg.assist_exit_wait_sec)
            state["pending_signal"] = sig
            state["last_tick_ts"] = now
            save_state(state, login)
            if cfg.assist_raise_window:
                _notify_signal(sig)

            ans = _await_human(cfg, sig, login=login, pos=pos)
            sig_id = sig["id"]
            fp_new = ans.get("fp")
            if fp_new is not None:
                sig_slip = round(float(fp_new) - float(float_pnl), 2)
            _log_signal(sig, ans, cfg, login,
                        extra=({"slip": sig_slip} if sig_slip is not None else None))
            state["pending_signal"] = None
            state["last_signal_ts"] = int(time.time())

            if ans["answer"] in ("sl", "timeout"):
                # 事实优先：等待期间触发硬止损 / 持仓上限，程序直接平。
                # 走原路径（source 仍是 sl/timeout），sl_closes 计数、复盘口径不变。
                decision = {
                    "close": True, "source": ans["answer"],
                    "reason": (f"等待人工确认期间触发"
                               f"{'硬止损' if ans['answer'] == 'sl' else '持仓上限'}，"
                               f"程序直接平仓（信号 {sig_id} 被打断）"),
                }
            elif ans["answer"] == "gone":
                row = {"ts": now, "kind": "hold", "source": "external_pending",
                       "signal_id": sig_id,
                       "reason": "等待确认期间仓位已在终端消失，下一轮按外部平仓记账",
                       "held_sec": held_sec}
                return _finish(state, row, login, ts=now)
            elif ans["answer"] in ("deny", "cancelled"):
                if ans["answer"] != "cancelled":
                    _set_assist_deny(state, "exit", cfg.assist_deny_cooldown)
                why_txt = ("人工否决平仓，继续持有" if ans["answer"] == "deny"
                           else "做单线程已停止，本轮不平")
                row = {"ts": now, "kind": "hold", "source": "human",
                       "signal_id": sig_id, "answer": ans["answer"],
                       "reason": why_txt,
                       "held_sec": held_sec, "float_pnl": round(float_pnl, 2),
                       "peak_pnl": pos.get("peak_pnl")}
                state["last_decision"] = {
                    "ts": now, "kind": "exit", "close": False,
                    "reason": why_txt, "source": "human",
                }
                return _finish(state, row, login, ts=now)
            else:                       # approve / expired
                exec_by = "human" if ans["answer"] == "approve" else "assist_expired"
                # 用答复瞬间的价格重算：等了 120 秒，浮盈早就不是原来那个数，
                # 拿旧价写平仓行等于记错账。
                bid, ask = float(ans["bid"] or bid), float(ans["ask"] or ask)
                if fp_new is not None:
                    float_pnl = float(fp_new)
                else:
                    float_pnl = _realized_pnl(cfg, pos, bid, ask)
                _track_peak(pos, float_pnl, now)
                tail = ("人工确认平仓" if exec_by == "human"
                        else f"{cfg.assist_exit_wait_sec} 秒无人应答，按模型决定执行")
                decision = {**decision,
                            "reason": f"{decision.get('reason') or ''}（{tail}）"}

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
            if sig_id:      # 人工点过同意的平仓：让面板能把这一笔对上号
                row["signal_id"] = sig_id
            _finish(state, row, login, ts=now)
            # 平不掉还继续每分钟调模型问一次，只会白花钱；
            # 仓位还在手上，必须停下来让人去终端处理。
            return _stop(state, f"平仓失败（{res.get('reason')}），已自动停止", now, login)

        # 盈亏按**回执上的成交价**算，不用发单前的报价。dry_run 没有回执，
        # 按报价估（纸面本来就没有真实成交）。两者都拿不到才算 0，
        # 这与 _book_close 的约定一致：这一格出错会污染所有战绩统计。
        fill_px = float(res.get("price") or 0.0)
        exit_price = fill_px if fill_px > 0 else _exit_price(pos, bid, ask)
        pnl = (_pnl_at_price(cfg, pos, fill_px) if fill_px > 0
               else _realized_pnl(cfg, pos, bid, ask))
        # 计数 / 清仓位 / close 行统一走 _book_close（含 sl_closes / be_closes
        # 分桶）。这里只补这条路径特有的字段。
        row = _book_close(state, pos, pnl, now,
                          source=decision.get("source"),
                          reason=decision.get("reason"),
                          held_sec=held_sec, dry_run=cfg.dry_run)
        row["exit_price"] = round(float(exit_price), 5)
        row["pnl_src"] = "fill" if fill_px > 0 else "quote"
        # 这笔是不是"峰值停滞唤醒"之后才平的。统计「靠唤醒救回来的单」时
        # 只能靠这个字段，否则它和"浮盈到门槛后正常平"在流水里长得一模一样。
        if decision.get("via"):
            row["via"] = decision["via"]
        if decision.get("error"):
            row["error"] = decision["error"]
        if sig_id:
            # `source` 不动（模型仍是发起方），谁按的按钮记在 exec_by。
            # 面板的 llm_hit_rate 分母挂在 source 上，改它会让统计静默变形。
            row["signal_id"] = sig_id
            row["exec_by"] = exec_by
            if sig_slip is not None:
                row["slip"] = sig_slip
        # 平仓之后才允许回看更长的一段，但**要等 7 根 K 线**才复盘：
        # 排进队列，这期间照常分析行情、照常开下一笔。
        pos["exit_reason"] = decision.get("reason") or ""
        # 谁发起的平仓（sl / llm / timeout / panel）。复盘要靠它判断
        # 「出场这一端到底有没有模型的决策」——被硬止损砍掉的单，
        # 出场时机不是模型选的，复盘不能再把账算到出场头上（见 ask_review）。
        pos["exit_source"] = decision.get("source") or ""
        queue_review(state, cfg, pos, exit_price, pnl, now)
        return _finish(state, row, login, ts=now)

    # --- 空仓：先看闸门，过不了就不花 API 的钱 -------------------------------
    ok, why = can_open(state, now, bool(live_positions), cfg)
    if not ok:
        row = {"ts": now, "kind": "skip", "reason": why}
        return _finish(state, row, login, ts=now)

    # 辅助模式的静默期：刚被否决 / 刚超时，这一段时间既不弹窗也不调模型。
    # 放在 ask_entry **之前**是刻意的——省的是 token，不是版面。
    # 用真实时钟比：`now` 是可注入的测试值，拿它和真实时间戳比会算错。
    if cfg.assist:
        until = _assist_deny_until(state, "entry")
        now_real = int(time.time())
        if until and now_real < until:
            # 措辞必须对上**上一轮的真实结局**。原来这里一律写「未获批准」，
            # 而 void_slip（点了同意、价已跑出上限）同样走这条分支——
            # 用户明明批了，屏幕却说他没批，看着就像「点了没反应」。
            # 2026-11-06 实盘：最后几秒点的同意，价已跑 3.38，于是被问住。
            ans = _last_answer(state)
            row = {"ts": now, "kind": "skip", "assist": True,
                   "reason": (f"{_DENY_HEAD.get(ans, '上一条信号未获批准')}，"
                              f"{until - now_real} 秒内不再弹窗（也不重复问模型）")}
            if ans:      # 留着让面板的标签和这句文案同源，旧 state 就不写空字段
                row["answer"] = ans
            return _finish(state, row, login, ts=now)

    # 程序建议方向：与喂给模型的位置判定同源（同 _structure + zone_edge），
    # 落盘后才能事后对账「程序对 vs 模型对」。trend_follow 决定趋势市给不给方向。
    prog_dir = _prog_direction(bars, cfg.zone_edge, cfg.trend_follow)
    # 行情判定：这一步就是「先判行情，再选打法」的分流点。
    # 同一个 regime 会同时喂给程序建议、提示词、锚点开关、开仓时的 plan。
    regime = regime_of(bars)
    # M5 背景只查一次：text 进 prompt，info 进特征快照。
    mtf = _mtf_bundle(cfg.symbol, cfg.zone_edge, _digits(cfg.symbol))
    # 刚平仓上下文：只在前 5 分钟（报复单窗口）传。让模型知道自己在
    # 「亏损后快速再入场」的高发期，教训库的报复单教训才能对症起效。
    recent_close = None
    last_close = state.get("last_close_ts")
    if last_close:
        seconds_ago = int(now) - int(last_close)
        if 0 < seconds_ago < 300:
            recent_close = {
                "seconds_ago": seconds_ago,
                "pnl": state.get("last_close_pnl"),
            }
    # 等待锚点：把上一轮它自己定的回踩价写回 prompt（它没有跨轮记忆，
    # 不喂回去就必然跟着价格漂——见 _wait_step 上面的注释）。
    #
    # 2026-10-28：趋势市停用锚点。锚点是"等回踩到 X 低吸"的配套机制，而趋势打法
    # 已经改成"顺势直接做、不等回踩"——实测按锚点挂限价等回踩 47 笔均值 -1.21，
    # 是接刀不是低吸。趋势市里继续注入旧锚点只会引诱模型继续"等"，所以连注带
    # 锁一起清：wait 置 None，状态机收到 None 就是"没有锚点"。
    wait_prev = None if regime == "trend" else state.get("wait")
    atr_now = float(_atr(bars).get("atr") or 0.0)
    wait_note = (_wait_note(wait_prev, bid, atr_now, cfg.symbol, now)
                 if wait_prev else "")
    # 上一轮判断回灌（2026-11-13）。趋势市里锚点是停用的，这段是趋势市
    # 唯一能告诉它「你刚才没做、之后价格走了多远」的东西——没有它，连续
    # act=false 在模型眼里每轮都是独立的第一次观察。顺序：先整段、再上一轮。
    skip_note = (_skip_streak_note(state.get("skip_streak"), bid, atr_now,
                                   cfg.symbol, now, regime)
                 + _skip_note(state.get("prev_entry"), bid, atr_now,
                              cfg.symbol, now))
    decision = ask_entry(bars, bid, ask, cfg, state, login,
                         ticks_text=_ticks_text(cfg, bars),
                         mtf_text=mtf["text"],
                         recent_close=recent_close,
                         wait_note=wait_note,
                         skip_note=skip_note)
    # 特征快照：决策当下就走一次，后面所有分支（skip / open / failed）都带上。
    # 不存它的话，事后想问「ER 高的单更容易赢吗」只能靠重建，而重建必然失败。
    feat = feature_snapshot(bars, cfg, mtf=mtf["info"])
    p_up = float(decision.get("p_up") if decision.get("p_up") is not None else 0.5)
    act = decision.get("act")
    direction = 1 if p_up > 0.5 else -1
    agree = decision.get("agree")
    # 推动锚点状态机。放在 last_decision 之前：act=false 的三个分支和
    # 后面的开仓分支都要能读到最新的 wait，否则面板上看到的还是上一轮的。
    # 趋势市里 wait_prev 已经是 None，状态机直接走"清除"分支——
    # 旧锚点（震荡市留下的）不会在趋势市继续挂着误导。
    state["wait"], wait_flag = _wait_step(
        wait_prev, decision.get("reason"), act, bid, atr_now, now, direction,
        regime=regime)
    if wait_flag == "drift":
        state["wait_drift"] = int(state.get("wait_drift") or 0) + 1
    state["last_decision"] = {
        "ts": now, "kind": "entry", "p_up": p_up, "direction": direction,
        "act": act, "prog_dir": prog_dir, "agree": agree,
        "reason": decision.get("reason"), "source": decision.get("source"),
        "wait": state.get("wait"), "wait_flag": wait_flag,
        "regime": regime,
    }
    # 记下这一轮的结论，下一轮 prompt 要用。放在这里（而不是各个 return
    # 分支）是因为下面每一条出口都经过这一行，漏一处的效果是"回灌停在两轮前"，
    # 比不写更难查。
    _remember_skip(state, act, bid, now, p_up, decision.get("reason"),
                   ok=decision.get("source") == "llm")

    # 模型明确说不做，就别做。以前只能靠 p_up≈0.5 暗示，结果常常
    # "理由写不追、数值 0.38 照开"——现在听它自己的。
    if act is False:
        failed = decision.get("source") == "error"
        row = {"ts": now, "kind": "skip", "p_up": p_up, "act": False,
               "prog_dir": prog_dir, "feat": feat,
               "wait": state.get("wait"), "wait_flag": wait_flag,
               "reason": (decision.get("reason") if failed else
                          f"模型判定不做：{decision.get('reason') or '未给理由'}")}
        if failed:
            # 调用失败不等于"它主动放弃"，两者混在一起看会把故障当成保守
            row["source"] = "error"
            row["error"] = decision.get("error")
        return _finish(state, row, login, ts=now)

    # 信号强度闸门。p_up 贴着 0.50 时模型自己也没把握，而开一单要先付 0.24 点差。
    # 5 秒周期下每分钟能出现十几次这种"微弱优势"，不挡住就是稳定亏损。
    edge = abs(p_up - 0.5)
    if cfg.min_edge > 0 and edge < cfg.min_edge:
        row = {"ts": now, "kind": "skip", "p_up": p_up, "direction": direction,
               "prog_dir": prog_dir, "agree": agree, "feat": feat,
               "reason": (f"信号不够强：p_up={p_up:.2f}，偏离 0.50 仅 {edge:.2f}，"
                          f"低于门槛 {cfg.min_edge:.2f}（开仓要先付点差）")}
        return _finish(state, row, login, ts=now)

    # 程序对这笔开仓的建议（2026-10-28 起默认只建议、不拦）。
    # 放在 min_edge 之后、send_open 之前：前面那道判的是"信号够不够强"，
    # 这两条判的是"这个位置/这句话合不合适"——但最终开不开由模型说了算。
    advice = _entry_advice(cfg, direction, feat, decision.get("reason"), act)
    advice_code = advice[0] if advice else None
    if advice and _gate_blocks(cfg, advice_code):
        code, why = advice
        # 记在 last_decision 上：面板要能区分「模型自己不做」和「被规则否决」，
        # 这两件事该找的人不一样（前者调提示词，后者调开关）。
        state["last_decision"]["veto"] = code
        row = {"ts": now, "kind": "skip", "p_up": p_up, "direction": direction,
               "prog_dir": prog_dir, "agree": agree, "feat": feat,
               "veto": code, "act": act,
               "reason": f"{why}（入场闸门 {code} 拦下，模型原话：{decision.get('reason') or '无'}）"}
        return _finish(state, row, login, ts=now)
    if advice_code:
        # 建议归建议，单照开。把意见挂到决策上，面板会显示「模型否决了建议」，
        # 开仓行也带 would_veto——事后能按这一格算「听了 vs 没听」的期望。
        state["last_decision"]["advice"] = advice_code
        state["last_decision"]["advice_text"] = advice[1]

    # 辅助模式：**开仓前停住，等人点一下**。
    #
    # 插在这里是有讲究的：前面所有闸门（act=false / min_edge / 建议闸门）都
    # 已经过完，所以弹窗只弹"模型确实要开、且程序没否掉"的单子——不会拿
    # 一堆半成品信号去轰炸人。后面 send_open 之前的复核是另一件事：
    # 等 90 秒之后世界变了，价格、持仓、刹车都可能不再允许。
    sig_id = None
    exec_by = "auto"
    sig_slip = None
    sig_ts = None
    sig_wait = None
    if cfg.assist:
        sig = _new_signal("entry", cfg, direction=direction, p_up=p_up,
                          prog_dir=prog_dir, agree=agree, act=act,
                          advice=advice_code,
                          advice_text=(advice[1] if advice_code else None),
                          reason=decision.get("reason"), bid=bid, ask=ask,
                          price=(ask if direction > 0 else bid),
                          feat=feat, wait_sec=cfg.assist_entry_wait_sec)
        state["pending_signal"] = sig
        state["last_tick_ts"] = now
        save_state(state, login)          # 面板 1 秒轮询，落盘才看得见
        if cfg.assist_raise_window:
            _notify_signal(sig)

        ans = _await_human(cfg, sig, login=login)
        sig_id, sig_ts = sig["id"], sig["ts"]
        sig_wait = round(float(ans["waited_sec"]), 1)
        if ans["answer"] == "approve":
            # 漂移只判**不利方向**：买得更便宜 / 空得更高，拦掉它没道理
            cur = float(ans["ask"] if direction > 0 else ans["bid"])
            sig_slip = round((cur - float(sig["price"])) * direction, 2)
        _log_signal(sig, ans, cfg, login,
                    extra=({"slip": sig_slip} if sig_slip is not None else None))
        state["pending_signal"] = None
        state["last_signal_ts"] = int(time.time())

        if ans["answer"] != "approve":
            # 取消不写冷却：那是"关程序"，不是"这单不该做"
            if ans["answer"] != "cancelled":
                _set_assist_deny(state, "entry", cfg.assist_deny_cooldown)
            # 让状态带跟着改口：否则它还挂在"模型开仓"上，跟刚弹出来的
            # "已否决"正好相反（见 _mark_answered 的注释）。
            _mark_answered(state, ans["answer"])
            why_txt = {
                "deny": "人工否决，不下单",
                "expired": f"{cfg.assist_entry_wait_sec} 秒无人应答，按不做处理",
                "cancelled": "做单线程已停止，这条信号作废（不会补单）",
                "blocked": "等待期间品种上已出现持仓（你手动开的），信号作废",
            }.get(ans["answer"], str(ans["answer"]))
            row = {"ts": now, "kind": "skip", "p_up": p_up,
                   "direction": direction, "prog_dir": prog_dir,
                   "agree": agree, "feat": feat, "assist": True,
                   "signal_id": sig_id, "answer": ans["answer"],
                   "reason": why_txt}
            return _finish(state, row, login, ts=now)

        if sig_slip is not None and sig_slip > float(cfg.assist_max_slip_usd):
            _set_assist_deny(state, "entry", cfg.assist_deny_cooldown)
            _mark_answered(state, "void_slip")
            row = {"ts": now, "kind": "skip", "p_up": p_up,
                   "direction": direction, "prog_dir": prog_dir,
                   "agree": agree, "feat": feat, "assist": True,
                   "signal_id": sig_id, "answer": "void_slip",
                   "slip": sig_slip,
                   "reason": (f"已同意，但价已朝不利方向跑了 {sig_slip:.2f} > "
                              f"{float(cfg.assist_max_slip_usd):.2f}，"
                              f"信号作废不下单")}
            return _finish(state, row, login, ts=now)

        # 复核：这 90 秒里你完全可能自己开了一笔，或者刹车已经踩了
        has_pos = False
        if not cfg.dry_run:
            ps = _positions(cfg.symbol)
            if ps is None:
                _mark_answered(state, "void_unknown")
                row = {"ts": now, "kind": "skip", "p_up": p_up,
                       "direction": direction, "prog_dir": prog_dir,
                       "agree": agree, "feat": feat, "assist": True,
                       "signal_id": sig_id, "answer": "void_unknown",
                       "reason": "已同意，但读不到持仓状态，无法确认没有叠仓，信号作废"}
                return _finish(state, row, login, ts=now)
            has_pos = bool(ps)
        ok2, why2 = can_open(state, int(time.time()), has_pos, cfg, direction=direction)
        if not ok2:
            _mark_answered(state, "void_state")
            row = {"ts": now, "kind": "skip", "p_up": p_up,
                   "direction": direction, "prog_dir": prog_dir,
                   "agree": agree, "feat": feat, "assist": True,
                   "signal_id": sig_id, "answer": "void_state",
                   "reason": f"已同意，但此刻已不可开仓：{why2}"}
            return _finish(state, row, login, ts=now)
        exec_by = "human"
        bid, ask = float(ans["bid"] or bid), float(ans["ask"] or ask)

    # 同向重进：方向只有拿到 decision 之后才确定，所以按方向分档的冷却必须
    # 在这里再判一次——上面 3943 那次在问模型之前（不知道方向），4143 那次只在
    # 辅助模式里跑。放在 send_open 之前 = 扣钱之前。assist 模式下人手点了最多
    # 90 秒，`now` 是这一轮开始的时刻，已经旧了，所以用真实时钟。
    left_dir = cooldown_left(state, int(time.time()) if cfg.assist else int(now),
                             direction, cfg)
    if left_dir > 0:
        same_dir = int(state.get("last_close_dir") or 0) == int(direction)
        row = {"ts": now, "kind": "skip", "p_up": p_up, "direction": direction,
               "prog_dir": prog_dir, "agree": agree, "feat": feat,
               "veto": "same_dir_cooldown" if same_dir else "cooldown",
               "reason": f"{'同向' if same_dir else ''}平仓后冷却中，还需 "
                         f"{left_dir} 秒，这一笔不做"}
        # 辅助模式下这一笔是**人点过同意**之后才被冷却挡下的。挂上 signal_id，
        # 面板才能把"点了同意"和"最后没开"这两件事对上（见 sigVerdictCheck）。
        if sig_id:
            row["signal_id"] = sig_id
        return _finish(state, row, login, ts=now)

    # plan 显式传进去：震荡单要挂券商端硬止盈（见 `_attach_entry_tp`），
    # 趋势单不挂。这个值必须在发单之前就定下来，出场参数也锁同一个值。
    res = send_open(cfg, direction, regime)
    if not res.get("ok"):
        row = {"ts": now, "kind": "open_failed", "p_up": p_up, "direction": direction,
               "reason": decision.get("reason"), "error": res.get("reason")}
        # 同上：同意之后被券商拒掉，是最需要能对上号的一种结局。
        if sig_id:
            row["signal_id"] = sig_id
        _finish(state, row, login, ts=now)
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
    sl_att = res.get("sl_attach") or {}
    tp_att = res.get("tp_attach") or {}
    tp_broker = (float(tp_att.get("tp") or 0.0)
                 if tp_att.get("ok") else 0.0)
    state["position"] = {
        "ticket": ticket, "direction": direction, "lots": cfg.lots,
        "price_open": price, "opened_at": now, "p_up": p_up,
        "entry_reason": decision.get("reason"), "dry_run": cfg.dry_run,
        # 开仓时是不是辅助模式——只用来决定脉冲弹不弹（全自动模式下没人盯屏，
        # 弹窗是干扰）。锁利本身两种模式都做，不看这个字段。记进 pos 而不是
        # 每轮读 cfg：面板中途切模式不该影响一笔已经开出去的仓。
        "assist": bool(cfg.assist),
        # 分歧记账：程序建议方向 + 模型是否自称同意。
        # 事后按 (prog_dir, direction) 分格算期望，谁该拥有判断权让数据判。
        "prog_dir": prog_dir, "agree": agree,
        # 打法在开仓这一刻锁定（"range"/"trend"/"mid"），出场参数跟着 plan 走：
        # 趋势单止盈 7.0、上限 3 小时；震荡单维持老参数。中途行情怎么变都不换。
        "plan": regime,
        # 开仓时刻的 ATR（价格，不是美元）也一起锁进持仓，理由和 plan 一样：
        # 出场阈值一旦按 ATR 归一化，中途重算会让同一笔仓前后被两套门槛管。
        # 现在（P0）只落盘不消费，唯一用途是把「这笔的 tp 相当于几倍 ATR」
        # 变成可统计的量——以前要靠 ticket 反查 open 行的 feat，绕且容易漏。
        # 取 0.0 表示当时算不出（bars 不足），消费方必须把它当"无数据"降级。
        "atr": float((feat or {}).get("atr") or 0.0),
        # 1 个 ATR 折成这笔单的浮盈（账户货币）。与 atr 同理开仓锁死：
        # 脉冲判据（_mark_burst）要用它，中途重算会让同一笔仓前后两套门槛。
        "atr_pnl": _atr_pnl_unit(float((feat or {}).get("atr") or 0.0),
                                 cfg.lots, cfg.symbol),
        # 辅助模式：这笔是人工确认进来的。平仓行要用它标 exec_by。
        "signal_id": sig_id, "exec_by": exec_by,
        # 券商端有没有这条硬止损线（2026-11-09）。0 = 没挂上，这笔只有程序
        # 轮询守卫在管；非 0 = 券商端实际挂在哪个价。
        # 它的第二个用途在「仓在终端消失」的判定里：reason=4 且这个字段非 0
        # 才算止损被打，否则一律记人工平仓（见 detected 分支）。
        "sl_broker": float(sl_att.get("sl") or 0.0) if sl_att.get("ok") else 0.0,
        # 券商端止盈线（2026-11-11）：**只有非趋势单有**。0 = 没有这条线
        # （趋势单、纸面单、挂载失败），此时盈利端仍走"越过门槛问模型"。
        # 它在「仓在终端消失」的判定里与 sl_broker 同权：只有 reason=5 且
        # 这个字段非 0 才记止盈成交，否则一律按人工平仓（见 detected 分支）。
        "tp_broker": tp_broker,
    }
    state["trades"] = int(state.get("trades") or 0) + 1
    state["last_open_ts"] = now
    state["last_tick_ts"] = now

    row = {
        "ts": now, "kind": "open", "ticket": ticket, "direction": direction,
        "lots": cfg.lots, "price": price, "p_up": p_up,
        "confidence": round(max(p_up, 1 - p_up), 4),
        "prog_dir": prog_dir, "agree": agree,
        # plan 冗余在 feat.regime 之外单列一行：统计工具按 kind=open 取 plan 时
        # 不用钻 feat 字典，和 close 行取法对称。
        "plan": regime,
        "feat": feat,
        "reason": decision.get("reason"), "source": decision.get("source"),
        "dry_run": cfg.dry_run,
        # 券商端止损挂载结果。挂上了写价格，没挂上写 0 + 原因——
        # 这两种单子在复盘时是两种东西：一种是「保护在券商那边」，
        # 一种是「只有程序在盯」。不写清楚，亏损只能看到结果看不到保护状态。
        "sl_broker": float(sl_att.get("sl") or 0.0) if sl_att.get("ok") else 0.0,
    }
    if not sl_att.get("ok"):
        row["sl_err"] = str(sl_att.get("reason") or sl_att.get("skipped") or "未知")
    # 止盈线只在「这个 plan 本来该有」时才写进流水：趋势单每笔都带一条
    # tp_err="trend_plan" 只会把真正该看的挂载失败淹掉。
    if tp_att.get("wanted"):
        row["tp_broker"] = tp_broker
        if not tp_att.get("ok"):
            row["tp_err"] = str(tp_att.get("reason") or tp_att.get("skipped") or "未知")
    if advice_code:
        # 程序不建议这笔、但模型坚持开了。留痕是为了事后分桶：
        # 「程序不建议却开了」的期望 vs「两边都同意」的期望，谁更该有判断权。
        row["would_veto"] = advice_code
    if decision.get("error"):
        row["error"] = decision["error"]
    if sig_id:
        # 人工确认的痕迹。`source` 保持原值（模型仍然是发起方），
        # 谁执行记在 exec_by —— 面板的命中率口径全挂在 source 上，不能动。
        row["signal_id"] = sig_id
        row["exec_by"] = exec_by
        row["sig_ts"] = sig_ts
        row["sig_wait_sec"] = sig_wait
        if sig_slip is not None:
            row["slip"] = sig_slip
    return _finish(state, row, login, ts=now)


def _exit_gate(cfg: Cfg, held_sec: int, float_pnl: float,
               plan: str | None = None, stale_ready: bool = False,
               be_local: bool = False, tp_seen: bool = False,
               tp_left: int = 0, lock_local: bool = False) -> dict | None:
    """持仓期间的**程序闸门**。返回 dict = 本轮结论已定；返回 None = 交给模型判断。

    顺序是致命的：**止损必须排在止盈门槛之前**。
    反过来写的话，一笔浮亏 -5 USD 的单子会因为「浮盈没到 2.50」被判成继续持有，
    止损永远不触发——参数越保守亏得越多。所以单独抽出来 + 单测钉住顺序。

    现在的顺序是 **sl → lock_local → be_local → timeout → stale → tp_wait**，
    六条各自的位置都有理由：
      · sl 最前：兜底，任何时候都不许被别的规则挡住；
      · lock_local（2026-11-13）第二：脉冲锁利线的本地兜底，只在券商端没挂上
        时成立。位置比保本线高（峰值 −1.5 ATR > 0），价格往下走一定先破它，
        所以排在 be_local 之前——反过来会被记成 be_local，分桶就废了；
      · be_local（2026-10-29）第三：保本线的本地兜底，只在券商端没挂上时成立。
        必须排在 timeout 和 tp_wait 之前——它的触发条件是"浮盈回到 0"，
        那时候 fp 远低于门槛，落在 tp_wait 后面就永远走不到；
        也必须排在 sl 之后：浮亏 -3.5 的单子两条线同时为真，记成 sl 才是实情；
      · timeout 第四：卡在 +2.0（没到门槛）的单子不能被无限持有，否则超短线拖成隔夜；
      · stale 第五：**只放行、不改阈值**。峰值停滞时返回 None 把决定
        交给模型，但它必须排在 tp_wait 之前——否则 fp < tp 永远走不到这里，
        "唤醒"就成了一句空话（10-28 那笔盲等 455 秒就是这么来的）；
      · tp_wait 最后：fp 没到门槛就继续持有、不调模型，省 token。
        **tp_wait 只对"从没到过门槛"的单子生效**（2026-10-29）：tp_seen=True
        时改走 tp_recheck，最多静默 tp_reask_sec 秒。否则一笔摸到 7.39 又掉回
        6.08 的趋势单会被 tp_wait 永久闭嘴，而它比任何时候都更需要判断。

    `stale_ready` / `be_local` / `lock_local` 都由调用方判好再传进来，本函数
    **不碰时间、也不读 pos**——否则那些钉顺序的单测全得跟着传 timestamp 和
    整笔仓位。

    `plan` 决定这一笔按哪套参数走（见 exit_params）：趋势单的止盈门槛是
    tp_min_trend（默认 7.0）、持仓上限 max_hold_trend（默认 3 小时）；震荡单
    维持 tp_min_usd / max_hold_sec 不变。plan 为空一律按震荡参数——
    旧 state 里没存 plan 的持仓也能继续被原规则管着，行为不变。

    三条老规则的来源（2026-10-23 第 3 场 191 笔的账，别丢）：
      · 60 秒中位持仓 = min_hold 从下限变成了目标，150 笔 ≤90 秒平仓吃掉 90% 的亏损；
      · avgW 1.011 / avgL 1.026 → 盈亏比 0.99，扣掉每笔 0.24 点差和 35.6% 胜率，必亏；
      · 最大单笔 -6.08 是平均亏损的 5.9 倍，且它的 p_up=0.0（示例占位值被照抄，
        又被 confidence 记成 1.0 的"最自信"，实际是没做判断）。
    """
    ep = exit_params(cfg, plan)
    if ep["sl_usd"] > 0 and float_pnl <= -abs(ep["sl_usd"]):
        return {"close": True, "source": "sl",
                "reason": (f"硬止损：浮亏 {float_pnl:+.2f} 触及 -{abs(ep['sl_usd']):.2f}")
                }

    # 锁利线兜底（2026-11-13）：脉冲后该挂的锁利线没挂上（多半是价格已经
    # 回落到目标线下方，券商直接拒 10016），现在浮盈跌破锁定值——程序代平。
    # **必须排在 be_local 之前**：锁利线的位置更高（峰值 −1.5 ATR），价格往下
    # 走一定先破它；排在后面的话，fp 一路掉到 0 才会触发，锁利等于没做。
    if lock_local:
        return {"close": True, "source": "lock_local",
                "reason": (f"脉冲锁利线：浮盈 {float_pnl:+.2f} 跌破锁定值，"
                           "券商端没挂上，程序代平")
                }

    # 保本线兜底（2026-10-29）：峰值到过 be_arm_usd、但券商端那条 SL 没挂上，
    # 现在浮盈回到 0 —— 程序自己平，别让这条规则只活在设计文档里。
    if be_local:
        return {"close": True, "source": "be_local",
                "reason": (f"保本线：浮盈 {float_pnl:+.2f} 回到 0，"
                           "券商端止损没挂上，程序代平")
                }

    # 持仓上限排在止盈门槛之前：否则一笔卡在 +2.0（没到门槛 3.25）的单子
    # 可以一直挂到天荒地老，把「超短线」拖成隔夜单。
    if ep["max_hold_sec"] > 0 and held_sec >= ep["max_hold_sec"]:
        return {"close": True, "source": "timeout",
                "reason": (f"持仓超上限：已 {held_sec}s / 上限 {ep['max_hold_sec']}s，"
                           f"浮盈亏 {float_pnl:+.2f}，无条件市价平")
                }

    # 峰值停滞：阈值一个字没改，只是把这一轮的决定权交回模型。
    # 位置必须在 tp_wait 之前——见 docstring 第三条。返回 None 而不是
    # {"close": True}：平不平仍由模型（辅助模式下再由人）判断。
    if stale_ready:
        return None

    if ep["tp_min_usd"] > 0:
        if float_pnl < ep["tp_min_usd"]:
            # 到过门槛（2026-10-29）：门槛不是"单程票"，是"这笔单进入高频复核"。
            # 一旦浮盈摸到过门槛，之后哪怕掉回门槛以内也照样每隔 tp_reask_sec
            # 把决定交回模型——回落到 6.08 那一格恰恰最该被判断，旧逻辑却在
            # 那时改口"本轮不调模型"，于是 4.2~7.0 成了无人看守的静默区。
            # tp_left>0 只是节流（同一分钟内不重复问），不是永久静默。
            if tp_seen:
                if tp_left <= 0:
                    return None
                return {"close": False, "source": "tp_recheck",
                        "reason": (f"浮盈 {float_pnl:+.2f} 已回落到门槛 "
                                   f"{ep['tp_min_usd']:.2f} 以内，但本笔到过门槛；"
                                   f"{tp_left}s 后复核，暂不调模型")}
            return {"close": False, "source": "tp_wait",
                    "reason": (f"浮盈 {float_pnl:+.2f} 未达止盈门槛 "
                               f"{ep['tp_min_usd']:.2f}，继续持有（本轮不调模型）")
                    }
        return None   # 到门槛了，让模型决定平还是继续跑

    # tp_min_usd=0：回退到旧的最短持仓保护
    # 豁免线同样是账户结算货币，与浮盈亏同单位，直接比。
    guard = abs(money_at_lots(cfg, cfg.min_hold_exempt_usd))
    if cfg.min_hold_sec > 0 and held_sec < cfg.min_hold_sec and float_pnl > -guard:
        return {"close": False, "source": "min_hold",
                "reason": (f"最短持仓中：已 {held_sec}s / 需 {cfg.min_hold_sec}s，"
                           f"浮盈亏 {float_pnl:+.2f} {money_label(_acct_currency())}"
                           f"（未触及 -{guard:.2f} 豁免线），"
                           f"本轮不调模型")
                }
    return None


# 「仓位在终端消失」时，人工平仓那条的文案。抽成常量是为了让两条路
# （`tick` 发现仓位没了 / `close_all` 要清的仓位早就不在了）说同一句话。
#
# 文案为什么是这句（2026-10-29）：最初写的是"仓位已在终端消失"，复盘模型把它
# 读成"程序/终端出了故障"，于是那一笔 +7.39→+3.71 的人工平仓被写成
# 「别被终端消失吓出」——一条教模型"下次该走也别走"的教训，还进了提示词。
# 人工平仓是**主动决策**，文本必须让模型分得清这两件事。
_GONE_WHY_EXTERNAL = ("外部平仓（人工或终端操作，非程序决策）："
                      "模型没发平仓单，仓位已不在券商端")


def _gone_source(pos: dict, deal_reason: int | None) -> tuple[str, str]:
    """仓位在券商端消失时，这笔平仓该记谁的账。返回 `(source, 给复盘的理由)`。

    `deal_reason` 来自 `_close_fills` 的成交回执（`DEAL_REASON_*`），读不到时
    为 None。**判据只能是回执，不能靠猜**：

      · 5 = 止盈 —— 震荡单开仓就挂了硬止盈（`_attach_entry_tp`），触价后仓位
        直接消失、程序没有平仓单。不认出来的话这笔 +16 的正常止盈会被记成
        「人工平仓」，再按 external 进教训库（"别被终端消失吓出"那种反向教训
        就是这么来的）。只在 `tp_broker` 已记且 reason 明确等于 5 时认；
      · 4 = 止损 —— 分会入场止损（`sl_broker`）和保本止损（`be_ok`）。保本那条
        读不到回执（None）也认：抬到开仓价的线被打掉，profit 恰好 0.00，
        回执常在这几十秒里还没落。入场止损则只在 reason==4 时认——「挂着止损
        但人手动平了」确实存在，不能默认成止损；
      · 其余（含回执 None，且没有保本线）→ external。

    两种"读不到"要分开对待，是因为误判的代价方向相反：把人工平仓记成止损，
    会教模型"这个位置该止损"；把止损记成人工平仓，会教模型"该走也别走"。
    """
    if deal_reason == 5 and pos.get("tp_broker"):
        return "tp", (f"券商端硬止盈 {float(pos.get('tp_broker') or 0):.3f} 成交"
                      f"（震荡单开仓时挂的止盈，程序未发平仓单）")
    # lock 判在 be 之前：锁利线一定在 be 之后挂、位置更高，两者都置了 sl_broker。
    # 先判 be 的话，锁利单会被记成保本单——那正是"按 source 分桶算期望"要避免的
    # 混淆（be 的期望是 0 附近，lock 的期望必须单独看才知道这条规则值不值）。
    if pos.get("lock_ok") and (deal_reason is None or deal_reason == 4):
        return "lock", (f"券商端脉冲锁利 {float(pos.get('lock_sl') or 0):.3f} 被触发"
                        f"（峰值后把 SL 抬到峰值 −{_LOCK_L:g} ATR）")
    if pos.get("be_ok") and (deal_reason is None or deal_reason == 4):
        return "be", (f"券商端保本止损 {float(pos.get('be_sl') or 0):.3f} 被触发"
                      f"（峰值浮盈后把 SL 抬到开仓价）")
    if pos.get("sl_broker") and deal_reason == 4:
        return "sl", (f"券商端入场止损 {float(pos.get('sl_broker') or 0):.3f} 被触发"
                      f"（开仓时挂的硬止损，程序未发平仓单）")
    return "external", _GONE_WHY_EXTERNAL


def _settle_pnl(cfg: Cfg, pos: dict, bid: float, ask: float
                ) -> tuple[float, float, dict | None]:
    """这笔单的最终盈亏与出场价。**成交回执优先，报价只做兜底。**

    返回 `(pnl, exit_price, fills)`；`fills is None` 表示这次是按报价估的，
    调用方据此在 close 行写 `pnl_src="quote"`——以后怀疑"记账不对"时先看这个
    字段，不用反推。

    为什么不能只按报价算：2026-11-06 那笔券商端保本 SL 在 4110.781 成交、
    profit 0.00，程序 48 秒后才发现仓位没了，用当时的 bid 一算记成 -0.96——
    保本平掉的单子变成亏损，还进了胜率统计。成交只有一个可信来源：成交回执。
    """
    fills = _close_fills(pos)
    if fills is not None:
        return fills["pnl"], (fills["price"] or _exit_price(pos, bid, ask)), fills
    if bid and ask:
        return _realized_pnl(cfg, pos, bid, ask), _exit_price(pos, bid, ask), None
    # 拿不到报价时按 0 记，不能编一个价出来
    return 0.0, 0.0, None


def _close_fills(pos: dict) -> dict | None:
    """这笔持仓在券商端的**平仓成交**汇总。还没成交 / 读不到 → None。

    返回 {"pnl": 净盈亏, "price": 成交均价, "reason": DEAL_REASON_*, "time": ts}。

    为什么单独抽出来
    ----------------
    「仓在终端消失」这条路要做三件事：算盈亏、判来源（券商 SL 还是人工）、
    给复盘一个真实的出场价。三件事读的是同一批 deal，各查一次等于三次
    MT5 往返——而 `_realized_pnl` 在 2 秒一格的止损快查里也会被调用。

    为什么 `pnl == 0.00` 必须照原样返回
    -----------------------------------
    保本止损打出来的**就是** 0.00，那是合法结果，不是"没读到"。
    2026-11-06 那笔 4919144776 正是踩在这里：外层用 `if total:` 判断有没有
    读到券商成交，0.00 被当成空值，于是退回用 48 秒后的报价估算——券商账本
    上 profit=0.00 的保本平仓，被这段记成 **-0.96**，还进了胜率和
    「保本止损到底有没有用」的统计。判据只能是"有没有平仓 deal"。
    """
    try:
        ticket = int(pos.get("ticket") or 0)
        if ticket <= 0:
            return None
        from live.journal import mt5_call, _mt5 as _conn

        deals = mt5_call(_conn().history_deals_get, position=ticket) or []
    except Exception:  # noqa: BLE001
        return None
    closing = [d for d in deals if int(getattr(d, "entry", 1)) == 1]
    if not closing:
        return None
    pnl = sum(
        float(getattr(d, "profit", 0.0))
        + float(getattr(d, "commission", 0.0))
        + float(getattr(d, "swap", 0.0))
        + float(getattr(d, "fee", 0.0))
        for d in closing
    )
    # 部分平仓会有多条 deal，成交价按成交量加权；只取第一条会把均价记偏。
    vol = sum(float(getattr(d, "volume", 0.0) or 0.0) for d in closing)
    if vol > 0:
        px = sum(float(getattr(d, "price", 0.0) or 0.0)
                 * float(getattr(d, "volume", 0.0) or 0.0)
                 for d in closing) / vol
    else:
        px = float(getattr(closing[-1], "price", 0.0) or 0.0)
    return {"pnl": round(pnl, 2), "price": px,
            "reason": int(getattr(closing[-1], "reason", -1)),
            "time": int(getattr(closing[-1], "time", 0) or 0)}


def _pnl_at_price(cfg: Cfg, pos: dict, price: float) -> float:
    """按给定出场价算这一笔的净盈亏（账户结算货币）。"""
    direction = int(pos.get("direction") or 1)
    entry = float(pos.get("price_open") or 0.0)
    lots = float(pos.get("lots") or 0.0)
    if not entry or not lots:
        return 0.0
    return round((float(price) - entry) * direction
                 * _symbol_contract(cfg.symbol) * lots * _acct_factor(), 2)


def _realized_pnl(cfg: Cfg, pos: dict, bid: float, ask: float) -> float:
    """这一笔的净盈亏，单位是**账户结算货币**（与 cfg 里的阈值同单位）。

    优先读券商成交记录，读不到就用价差估。估算不含点差和佣金，
    只能看方向对不对；真实数字以 MT5 账本为准。

    这里**不做币种换算**（2026-11-02 定）：`deal.profit` 本来就是账户货币，
    美分户返回的是「分」。配置里的 sl_usd / max_loss_usd 也按账户货币写，
    两边同单位才能直接比。若在这换算成美元，反而要把每个阈值都改成 1/100。

    判据是"有没有平仓 deal"，**不是"算出来是不是 0"**——保本止损的成交
    就是 0.00，见 `_close_fills` 里那段账。
    """
    fills = _close_fills(pos)
    if fills is not None:
        return fills["pnl"]
    direction = int(pos.get("direction") or 1)
    entry = float(pos.get("price_open") or 0.0)
    cur = bid if direction > 0 else ask  # 平仓价：多头看 bid，空头看 ask
    if not entry or not float(pos.get("lots") or 0.0):
        return 0.0
    return _pnl_at_price(cfg, pos, cur)


# ------------------------------------------------------------------ 主循环


def _fmt(row: dict) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(int(row.get("ts") or 0)))
    kind = row.get("kind")
    if kind == "open":
        arrow = "多" if int(row.get("direction") or 0) > 0 else "空"
        return (f"{clock} 开仓 {arrow} {row.get('lots')} @ {row.get('price')} "
                f"p_up={row.get('p_up')} [{row.get('source')}] {row.get('reason') or ''}")
    if kind == "close":
        return (f"{clock} 平仓 #{row.get('ticket')} 盈亏 {row.get('pnl'):+.2f} "
                f"{money_label(_acct_currency())} "
                f"[{row.get('source')}] {row.get('reason') or ''}")
    if kind == "hold":
        return f"{clock} 持有 {row.get('reason') or ''}"
    if kind == "be":
        return (f"{clock} 保本止损 #{row.get('ticket')} "
                f"SL→{float(row.get('sl') or 0):.3f} [{row.get('mode')}] "
                f"{row.get('reason') or ''}")
    if kind == "lock":
        return (f"{clock} 脉冲锁利 #{row.get('ticket')} "
                f"SL→{float(row.get('sl') or 0):.3f} [{row.get('mode')}] "
                f"{row.get('reason') or ''}")
    if kind == "be_fail":
        return (f"{clock} 保本止损没挂上 #{row.get('ticket')}："
                f"{row.get('error') or row.get('reason') or ''}")
    if kind == "skip":
        return f"{clock} 跳过：{row.get('reason')}"
    if kind == "stop":
        return f"{clock} 停止：{row.get('reason')}"
    if kind == "signal":
        ANS = {"approve": "同意", "deny": "否决", "expired": "超时未答",
               "cancelled": "已取消", "blocked": "等待期已有持仓",
               "gone": "仓已不在", "sl": "被硬止损抢占",
               "timeout": "被持仓上限抢占", "void_slip": "价格漂移作废",
               "orphan": "孤儿信号（已清理）"}
        what = "入场" if row.get("sig_kind") == "entry" else "出场"
        ans = ANS.get(str(row.get("answer")), str(row.get("answer")))
        wait = row.get("waited_sec")
        wait_txt = f"，等 {float(wait):.0f}s" if wait is not None else ""
        slip = row.get("slip")
        slip_txt = f"，漂移 {float(slip):.2f}" if slip is not None else ""
        return f"{clock} 人工确认 [{what}] {ans}{wait_txt}{slip_txt}"
    return f"{clock} {kind} {row.get('reason') or row.get('error') or ''}"


def _wait_deadline(started: float, wait: float, cfg: Cfg) -> float:
    """这一轮跑完，等到**哪个时刻**再决策（不是"等多久"）。

    只在 `align_settle_sec > 0` 时对齐：返回「≥ started+wait 的最近 K 线网格点」，
    网格点 = `k × interval_sec + align_settle_sec`。

    为什么换成"时刻"表述（2026-11-10）：间隔是相对量，基准一旦被搬走（尾段保底
    那 1 秒、快查提前唤醒、辅助模式从 tick 结束才计时），相对量就永远搬不回来。
    实测段内相位标准差中位 15.5~15.9 秒。换成绝对时刻之后，慢一轮只是**吃掉**
    这一轮的空闲，不会把下一次的采样点往后推——漂移无处累积。

    为什么不向下取整：唤醒点必须 ≥ started+wait，那是冷却/周期写下的下界，
    早醒等于绕过它。代价是首轮最多多等一个周期（相位不对时），此后稳定在网格上。
    """
    iv = float(cfg.interval_sec or 0)
    settle = float(getattr(cfg, "align_settle_sec", 0.0) or 0.0)
    if iv <= 0 or settle <= 0:
        return started + wait
    # 周期配得比 2×settle 还小（interval_sec=3 那种）时按半个周期算：
    # settle 接近甚至超过一个周期，网格就没有意义了，但也不该让配置直接失效。
    settle = min(settle, iv / 2.0)
    k = math.ceil((started + wait - settle) / iv - 1e-9)
    return k * iv + settle


def next_wait_sec(row: dict, cfg: Cfg) -> float:
    """这一轮跑完，下一次决策该等多久。

    平仓后**在冷却结束点提前唤醒一次**（cooldown+1 秒）：
    给模型一次决策机会——刚平仓时的市场状态往往紧接着就有结构变化，
    早看一眼不亏。但**开不开是模型的事**：教训库的「报复单」教训和
    提示词里的【刚平仓提醒】会约束它，act=false 是正常输出。

    2026-10-29 起 cooldown_sec 默认 60 秒 = 决策周期，`cooldown+1` 被夹到
    interval_sec，这一档实际等于按周期唤醒；同向那 180 秒由 can_open 拦，
    不靠唤醒节拍——31 秒唤醒顺手把人推回同一方向的副作用就此消失。

    以前这里的理由是「1 分钟内必须重新开仓」——那条硬需求已删
    （和复盘体系的报复单标记直接矛盾）。唤醒保留，语义从「必须快开」
    变成「给一次机会」。
    """
    if row.get("kind") == "close":
        return max(1.0, min(float(cfg.cooldown_sec + 1), float(cfg.interval_sec)))
    return float(cfg.interval_sec)


# 脉冲判据（"浮盈速度"特征）。与 tools/measure_burst.py 同源、同参数：
#   3 根 M1 内走满 5 个 ATR 的事件，之后 10 分钟中位回吐 1.27 ATR，
#   15 分钟内最大反向 2.33 ATR，吐回超一半浮盈的比例 50%（n=30，50 天 M1）。
# 对照：同样 5 ATR 位移但慢速走到（15 根）的 214 个样本只有 −0.20 / 26%。
#
# **这里只留痕，不改行为。** n=30、正比例 37%、p≈0.10，还没到能改出场逻辑的
# 显著度；而且快速组 15 分钟均值是 +0.39（被少数暴涨拉正）——走掉就是砍右尾。
# 先攒够本账户的实盘样本，再决定要不要动。判据里两个条件必须同时成立：
# 单独看速度没意义（2.5 ATR 档快慢几乎没差），单独看位移也没意义。
_BURST_ATR = 5.0
_BURST_WIN_MIN = 3.0
# 观察档位：记「首次到 3 倍 / 5 倍 ATR 分别用了多久」。只记 5 倍的话，峰值
# 只有 1~3 ATR 的单子（占多数）在统计里完全没有速度可算；两个点一起记，
# 任何一笔都能算"从 3 ATR 走到 5 ATR 用了几分钟"，也就是真正的速度。
_ATR_STEPS = (3.0, _BURST_ATR)

# ---- 脉冲锁利（2026-11-13） ----------------------------------------------
# 脉冲成立（3 分钟内走满 5 ATR）之后，把**券商端 SL** 从开仓价抬到
# 「峰值 − L 个 ATR」。峰值继续抬高这条线就跟着抬，只有回撤超过 L 才走掉。
#
# 为什么是抬 SL 而不是平仓（`python -m tools.burst_lock`，50 天 M1，5 ATR 档）：
#   L=0.5 → 93% 会被打掉，其中 16% 打掉之后价格又摸回峰值（纯亏）；
#   L=1.5 → 77% 被打掉，砍错只有 6%，锁住的利润还够看；
#   L=3.0 → 61% 被打掉，砍错 0%，但锁住的明显少一截，等于半途而废。
# 1.5 是"松到不被 M1 噪音打掉、紧到真能接住回撤"的那一档。打掉比例高不是
# 坏事——趋势单本来就只有少数能一路走，锁利要的是让那 77% 变成正收益。
#
# 为什么不平仓：同一批事件 15 分钟均值 +0.39 ATR（被少数暴涨拉正）、中位
# −1.27。全平就是砍右尾，而趋势单的钱全在右尾。
# 为什么不让模型决定：脉冲的价值在"当下"，决策轮 60 秒一次，等它看到已经
# 吐完了；何况这个特征没进提示词，模型根本不知道自己在脉冲里。
#
# **只对 trend 单、且只在全自动模式**：震荡单有券商端硬 tp 3.25，到线就走，
# 再加一条线会跟它打架；辅助模式下这条规则整个不生效，只弹窗提醒人看——
# 人盯屏的时候替人抬线，等于替人做决定（见 `_lock_applies`）。
# 与 be（抬到开仓价）的关系：两条都改同一条券商 SL，取更高的那个。
# lock 门槛（5 ATR）永远高于 be 门槛（3.25 USD），所以 lock 一定后发生、
# 并且覆盖 be 抬过的位置——顺序天然安全，不需要额外仲裁。
_LOCK_L = 1.5          # 锁利线 = 峰值 − 1.5 个 ATR
_LOCK_STEP_ATR = 0.5   # 目标价要比当前挂着的线高出这么多才值得改一次单


def _atr_pnl_unit(atr: float, lots: float, symbol: str) -> float:
    """1 个 ATR 折成这笔单的浮盈（账户结算货币）。算不出来返回 0。

    与 `_pnl_at_price` 同一个换算式（价差 × 合约 × 手数 × 账户系数），
    只是把"价差"固定成 1 个 ATR。开仓时算一次锁进 `pos["atr_pnl"]`，
    和 `atr` 一样中途不重算——否则同一笔仓前后会被两套门槛管。
    取 0 表示当时算不出（旧 state、bars 不足），消费方必须当"无数据"降级。
    """
    atr = float(atr or 0.0)
    lots = float(lots or 0.0)
    if atr <= 0 or lots <= 0:
        return 0.0
    try:
        return round(atr * _symbol_contract(symbol) * lots * _acct_factor(), 4)
    except Exception:  # noqa: BLE001
        return 0.0


def _mark_burst(pos: dict, peak: float, ts: int) -> bool:
    """记下浮盈**首次**达到各档 ATR 倍数时距开仓多少分钟（`pos["t3"]/["t5"]`）。

    返回 True = 这一轮刚跨过 5 倍 ATR（调用方必须写盘，否则弹窗标记会丢）。

    每档只写一次，之后峰值再创新高也不覆盖——判据关心的是"多久走满这一段"，
    不是"峰值出现在第几分钟"。后者 `peak_ts` 已经在记，但两者不等价：先在
    2 分钟到 5 ATR、10 分钟才创新高的单子，按 peak_ts 会被判成慢速，按 t5
    才是它本来的样子。

    **弹窗只给趋势单**（`burst_alert`）：震荡单有券商端硬 tp 3.25，到线就走，
    不需要人来插手；趋势单不挂 tp，冲高回撤全靠人/模型判断，那才是要看一眼的。
    但 t3/t5 对**所有**单都记——震荡单的脉冲是天然对照组，而且零成本。

    平仓行写 `t3`/`t5` + `burst`（t5 <= 3 分钟）。没到过的档位留 None；
    `atr_pnl` 缺失（旧持仓）时全部留 None，用 `atr_peak` 是否为空区分。
    """
    unit = float(pos.get("atr_pnl") or 0.0)
    opened = int(pos.get("opened_at") or 0)
    if unit <= 0 or not opened:
        return False
    fired = False
    for k in _ATR_STEPS:
        key = "t%g" % k
        if pos.get(key) is not None:
            continue
        if float(peak) < k * unit:
            continue
        pos[key] = round((int(ts) - opened) / 60.0, 2)
        if k != _BURST_ATR:
            continue
        fired = True
        # 弹窗与抬线**按模式分工**（2026-11-13）：
        #   辅助模式 → 只弹窗，SL 一动不动，抬不抬由人决定；
        #   全自动   → 不弹窗（没人盯屏，纯干扰），由 `_lock_step` 自己抬线。
        # 记录（t3/t5/burst）与模式无关，照记——那是统计的原料，两边都留。
        if (pos.get("plan") == "trend" and pos.get("assist")
                and not pos.get("burst_alert")):
            # 只放面板要显示的那几个数。**不在这里做任何交易动作**——
            # 弹窗是提醒人看，不是替人平仓；程序平不平仓仍由 _exit_gate 决定。
            pos["burst_alert"] = {
                "ts": int(ts), "ticket": pos.get("ticket"),
                "t5": pos.get("t5"), "atr_peak": round(float(peak) / unit, 2),
                "fp": round(float(peak), 2), "atr": pos.get("atr"),
                "lots": pos.get("lots"), "direction": pos.get("direction"),
                "price_open": pos.get("price_open"),
                "dry_run": bool(pos.get("dry_run")),
            }
    return fired


def _peak_atr(pos: dict) -> float | None:
    """峰值浮盈折成几倍 ATR。None = 没有 atr_pnl（旧持仓 / 开仓时算不出 ATR）。

    这个值和 `t5` 一起看才有意义：`atr_peak` 大而 `t5` 小 = 脉冲型到顶，
    `atr_peak` 大而 `t5` 大 = 阶梯型磨上去的。前者是这次要验证的对象。
    """
    unit = float(pos.get("atr_pnl") or 0.0)
    peak = pos.get("peak_pnl")
    if unit <= 0 or peak is None:
        return None
    return round(float(peak) / unit, 2)


def _is_burst(pos: dict) -> bool:
    """3 分钟（≈3 根 M1）内走满 5 个 ATR。"""
    t5 = pos.get("t5")
    return t5 is not None and float(t5) <= _BURST_WIN_MIN


def _lock_applies(pos: dict | None) -> bool:
    """脉冲锁利管不管这一笔：趋势单、脉冲已成立（`t5` 有值）、**且不是辅助模式**。

    门槛用 `t5` 而不是 `_is_burst`：3 分钟内没走完、但 4 分钟走完 5 ATR 的单子
    同样有回撤风险，而"3 分钟"这个窗口本身是 n=30 的样本里挑出来的，不该
    让它同时决定"要不要保护"。窗口只作统计标签，保护只看"到过 5 ATR"。

    **辅助模式不抬线**（2026-11-13 用户定）：辅助模式的全部意义是"人看一眼
    再决定"。程序在人收到弹窗的同一刻把 SL 抬掉，等于替人做了半个决定——
    而且弹窗上写的"峰值/浮盈"和券商端那条已经动过的线不是一回事，人看着
    旧数字做判断，实际成交在另一条线上。所以辅助模式只弹窗（见 `_mark_burst`
    里的 `burst_alert`），抬不抬、抬到哪，由人自己决定。

    缺 `assist` 字段（升级瞬间的旧持仓）按全自动处理：那笔单本来就在全自动
    逻辑下跑，不能因为一次升级把保护抽走。与 `burst_alert` 的缺省方向一致。
    """
    if not pos:
        return False
    if str(pos.get("plan") or "") != "trend":
        return False
    if pos.get("assist"):
        return False
    return pos.get("t5") is not None


def _lock_target(pos: dict) -> float | None:
    """锁利线该挂在哪。None = 这一笔现在没有该抬的线。

    峰值折成价格：`peak_pnl / atr_pnl` 就是峰值那一刻价格相对开仓价走了多少
    个 ATR（`atr_pnl` = 1 个 ATR 的浮盈，见 `_atr_pnl_unit`），乘回 ATR 得到
    价格位移，再退 L 个 ATR = 要锁住的位置。

    全程用**开仓时锁定的** atr / atr_pnl，中途不重算——和 be 同理，否则同一
    笔仓前后会被两套门槛管。缺失就返回 None：算不出就不挂，不猜。
    """
    try:
        unit = float(pos.get("atr_pnl") or 0.0)
        atr = float(pos.get("atr") or 0.0)
        entry = float(pos.get("price_open") or 0.0)
        d = int(pos.get("direction") or 0)
        peak = float(pos.get("peak_pnl") or 0.0)
    except (TypeError, ValueError):
        return None
    if unit <= 0 or atr <= 0 or entry <= 0 or not d:
        return None
    move = (peak / unit - _LOCK_L) * atr      # 价格位移，单位=报价货币
    if move <= 0:
        return None                          # 峰值还不够高，锁不出利润
    return entry + d * move


def _lock_step(cfg: Cfg, pos: dict, now: int) -> dict | None:
    """把券商端 SL 抬到锁利线。返回要落盘的一行或 None。

    和 `_be_step` 完全同构（幂等用 `lock_ok`、失败节流用 `lock_try_ts`、
    busy 不算失败、纸面单只记账），差别只有目标价：be 是开仓价，这条是
    「峰值 − 1.5 ATR」，且会随峰值继续抬高。

    挂失败时的兜底在 `_lock_local_hit` + `_exit_gate` 的 `lock_local` 分支：
    券商会拒掉"已经在现价错误一侧"的 SL（10016），而那正是锁利该生效的时刻，
    不能只记一个 `lock_err` 就当作没这条规则。

    辅助模式下整条规则不生效（`_lock_applies` 拦在第二行）：那种模式只弹窗，
    线由人自己决定抬不抬。
    """
    if not bool(getattr(cfg, "lock_profit", False)):
        return None
    if not _lock_applies(pos):
        return None
    target = _lock_target(pos)
    if target is None:
        return None

    atr = float(pos.get("atr") or 0.0)
    prev = pos.get("lock_sl")
    if prev is not None and atr > 0:
        try:
            # 没比已经挂着的线高出半个 ATR：不值得再改一次单。券商端改单要
            # 抢 MT5 锁，峰值一路上抬时连改几十次，会把 2 秒一格的止损快查
            # 挤成 5 秒——那条路径上还有硬止损在判，不能为一点小钱冒险。
            if float(target) - float(prev) < _LOCK_STEP_ATR * atr:
                return None
        except (TypeError, ValueError):
            pass

    now = int(now)
    retry = int(getattr(cfg, "be_retry_sec", 20) or 0)
    last = pos.get("lock_try_ts")
    if last is not None and retry > 0:
        try:
            if now - int(last) < retry:
                return None
        except (TypeError, ValueError):
            pass

    ticket = int(pos.get("ticket") or 0)
    peak = float(pos.get("peak_pnl") or 0.0)
    if pos.get("dry_run") or ticket <= 0:
        pos["lock_try_ts"] = now
        pos["lock_mode"] = "paper"
        pos["lock_sl"] = round(target, 3)
        pos["lock_ts"] = now
        return {"ts": now, "kind": "lock", "ticket": ticket,
                "direction": pos.get("direction"), "mode": "paper",
                "sl": round(target, 3), "peak_pnl": round(peak, 2),
                "reason": (f"峰值 {peak:+.2f}（{_peak_atr(pos)} ATR）后，"
                           f"纸面锁利线设到 {target:.3f}（峰值 −{_LOCK_L:g} ATR）")}

    res = send_modify_sl(cfg, ticket, target)
    if res.get("busy"):
        # 终端这一格没排上队：不算失败，也不消耗重试窗口（同 _be_step）。
        return None
    pos["lock_try_ts"] = now
    if res.get("ok"):
        pos["lock_ok"] = 1
        pos["lock_sl"] = float(res.get("price") or target)
        pos["lock_ts"] = now
        # 券商端那条线现在就是锁利价；同步给 sl_broker，让面板只读一处。
        pos["sl_broker"] = float(pos["lock_sl"])
        pos.pop("lock_err", None)
        return {"ts": now, "kind": "lock", "ticket": ticket,
                "direction": pos.get("direction"), "mode": "broker",
                "sl": round(float(pos["lock_sl"]), 3),
                "peak_pnl": round(peak, 2),
                "reason": (f"峰值 {peak:+.2f}（{_peak_atr(pos)} ATR）后，"
                           f"券商端 SL 抬到 {float(pos['lock_sl']):.3f}"
                           f"（峰值 −{_LOCK_L:g} ATR，锁住这一段）")}

    # 改单失败（多半是价格已经回落到目标线下方 → 10016）。只记一笔原因，
    # **不进流水**：这种失败会每 20 秒重复一次，写进流水会把真正的成交淹掉。
    # 面板/复盘看 `lock_err` 就知道这条线当时为什么没挂上。
    pos["lock_err"] = str(res.get("reason") or "改单失败")
    return None


def _track_peak(pos: dict, float_pnl: float, ts: int) -> bool:
    """更新峰值浮盈。返回 True = **需要写盘**（调用方据此决定是否落盘）。

    绝大多数情况下 True 就是"峰值被抬高了"，但有一个例外：旧持仓升级那一轮
    峰值没涨、却补记了脉冲（`_mark_burst` 写进 `burst_alert`），这时也要写盘，
    否则面板永远看不到那条提醒。

    peak_pnl 的唯一用途：**用实测数据回答「3.25 到底几次能到」**。除了值还记
    `peak_ts`（峰值出现的时刻）——「MFE 通常在持仓第几秒」对 max_hold_sec 的
    分析和 peak_pnl 本身一样值钱，而且顺手就能记，零成本。

    快查循环（_wait_next）和决策轮（tick）都调这一个函数，**峰值逻辑同源**，
    不存在两份实现各记各的。

    注意别写成 `pos.get("peak_pnl") or float_pnl`：peak_pnl 恰好是 0.0 时
    `or` 会退回 float_pnl，把已经摸到的正峰值抹掉——所以必须判 None。
    """
    prev = pos.get("peak_pnl")
    # 用**峰值**判定脉冲（当前浮盈可能已从高点回落）。放在早返回之前：升级
    # 瞬间手里正拿着一笔没有 t5 的旧持仓，靠这一句补记，否则它在这份统计里
    # 会永远被算成"没到过 5 ATR"。补记的 t5 会偏大（记的是观察时刻而非穿越
    # 时刻），所以旧持仓那一两笔在分析时要单独排掉——`atr_peak` 有值而 t5 偏大
    # 就是它的特征。
    peak = float(float_pnl) if prev is None else max(float(float_pnl), float(prev))
    fired = _mark_burst(pos, peak, ts)
    if prev is not None and float_pnl <= float(prev):
        # 峰值没涨，但补记了脉冲（旧持仓升级那一轮）——也得让调用方写盘，
        # 否则刚写的 burst_alert 只活在内存里，重启就没了。
        return fired
    pos["peak_pnl"] = round(float_pnl, 2)
    pos["peak_ts"] = int(ts)
    return True


def _stale_hit(cfg: Cfg, pos: dict | None, float_pnl: float, now: int) -> bool:
    """峰值停滞判定。True = 这一轮把决定权交给模型（**不是**直接平仓）。

    为什么单独一个函数、而不塞进 _exit_gate：判定需要 `now` 和整个 pos，
    而 `_exit_gate` 是**纯函数**（只吃 cfg/held/fp/plan，单测里直接调）。
    把时间依赖塞进去，那 20 条钉顺序的单测就全得传 timestamp。
    所以分工是：这里算"该不该说话"，_exit_gate 只接一个 stale_ready 布尔。
    tick() 和 _wait_next() 都调这一个函数——**两处不能各写一份判定**，
    否则快查的唤醒点和闸门的放行点迟早对不上（tp_wake 当年就差点这么漂）。

    六条同时成立才返回 True（顺序按"最容易为假"排，省得白算）：
      1. stale_wake 打开；
      2. 这笔单的 peak_pnl / peak_ts 都在（旧 state、测试里手写的 pos 没有，
         直接判否——这是兼容旧持仓的关键，不能抛异常）；
      3. peak_ts > 0 且 peak >= stale_min_peak（0.5）。峰值是负数或只有几毛钱
         的单子不是"摸到过利润又吐回去"，那是止损和持仓上限的活；
      4. 距峰值出现已 >= stale_sec（180 秒）；
      5. 距上次因停滞唤醒已 >= stale_reask_sec（300 秒）——防抖。没有它，
         peak_ts 不再更新会让本函数每轮都为真，正好把 tp_wait 省 token 的
         初衷全废掉，退化成"每 60 秒问一次模型"；
      6. 浮盈 < tp × stale_frac（0.6）。已经接近门槛的单子不用救，它马上就会
         被 tp_wake 问到，多问一次是纯浪费。

    副作用：无。只读，不写 pos["stale_ts"]——那个标记必须由**真正要调模型**
    的调用方写，否则模型调用失败会白白吃掉一次防抖窗口。
    """
    if not pos or not bool(getattr(cfg, "stale_wake", False)):
        return False

    peak = pos.get("peak_pnl")
    peak_ts = pos.get("peak_ts")
    if peak is None or peak_ts is None:
        return False
    try:
        peak = float(peak)
        peak_ts = int(peak_ts)
        now = int(now)
    except (TypeError, ValueError):
        return False

    min_peak = money_at_lots(cfg, getattr(cfg, "stale_min_peak", 0.0) or 0.0)
    if peak_ts <= 0 or peak <= 0 or peak < min_peak:
        return False

    stale_sec = int(getattr(cfg, "stale_sec", 0) or 0)
    if stale_sec <= 0 or now - peak_ts < stale_sec:
        return False

    reask = int(getattr(cfg, "stale_reask_sec", 0) or 0)
    if reask > 0:
        last = pos.get("stale_ts")
        if last is not None:
            try:
                if now - int(last) < reask:
                    return False
            except (TypeError, ValueError):
                pass

    frac = float(getattr(cfg, "stale_frac", 0.0) or 0.0)
    if frac <= 0:
        return False
    tp = float(exit_params(cfg, pos.get("plan"))["tp_min_usd"] or 0.0)
    if tp <= 0:
        return False
    return float(float_pnl) < tp * frac


def _tp_reached_step(cfg: Cfg, pos: dict | None, float_pnl: float,
                     now: int) -> tuple[bool, int]:
    """这一笔到过止盈门槛吗？到过之后距下一次复核还有几秒。

    返回 `(tp_seen, tp_left)`：
      · `tp_seen=True` = 浮盈曾经 ≥ 门槛，闸门因此**不再用 tp_wait 静默**；
      · `tp_left>0` = 还在 tp_reask_sec 的节流窗口内，这一轮不必再问。

    和 `_stale_hit` 一样是**只读判定 + 一个写标记**的分工：`_exit_gate` 是纯函数
    （单测直接调，不传时间戳），所以时间相关的判定全部留在外面，闸门只接
    `tp_seen` / `tp_left` 两个语义参数。

    两个标记都落在 pos 里，跟着 state 一起持久化：
      · `tp_reached_ts`（首次到门槛的时刻）——跨轮、跨重启都必须保留，
        否则程序一重启，这笔单就"从没到过门槛"，重新回到静默；
      · `tp_ask_ts`（上次真问了模型的时刻）——**只由真正调用模型的调用方写**
        （见 tick），本函数不写。模型调用失败还占掉一个节流窗口的话，
        最该被问的那一分钟恰好没人问。

    旧 state 兼容：升级瞬间手里正拿着一笔已经到过门槛的单子（有 peak_pnl、
    没有 tp_reached_ts），在这里用 peak 补记一次——否则这个洞只在"新开的单"
    上被堵住，正在亏的那笔依然静默。

    `tp_reask_sec <= 0` 直接判否，等于整条规则关掉、回到旧行为。
    """
    if not pos:
        return False, 0
    reask = int(getattr(cfg, "tp_reask_sec", 0) or 0)
    if reask <= 0:
        return False, 0

    tp = float(exit_params(cfg, pos.get("plan"))["tp_min_usd"] or 0.0)
    if tp <= 0:
        return False, 0

    try:
        now = int(now)
    except (TypeError, ValueError):
        return False, 0

    seen = pos.get("tp_reached_ts") is not None
    if not seen:
        peak = pos.get("peak_pnl")
        try:
            peak = float(peak) if peak is not None else float("-inf")
        except (TypeError, ValueError):
            peak = float("-inf")
        if float(float_pnl) >= tp or peak >= tp:
            pos["tp_reached_ts"] = now
            seen = True
    if not seen:
        return False, 0

    try:
        last = int(pos.get("tp_ask_ts") or 0)
    except (TypeError, ValueError):
        last = 0
    if last <= 0:
        return True, 0          # 到过门槛但还没问过 → 这一轮就问
    return True, max(0, reask - (now - last))


def _be_applies(pos: dict | None) -> bool:
    """保本止损管不管这一笔。**只判 plan**：规则只给趋势单。

    震荡单的止盈门槛本来就是 3.25，摸到就该让模型说话，不需要第二道线；
    趋势单的门槛是 7.0，摸到 3.25 之后既不平也没人问，那才是要补的洞。
    """
    if not pos:
        return False
    return str(pos.get("plan") or "") == "trend"


def _lock_local_hit(cfg: Cfg, pos: dict | None, float_pnl: float) -> bool:
    """锁利线的**本地兜底**：券商端那条线没挂上时，浮盈跌破锁定值由程序平。

    只在 `lock_ok` 为假时成立。挂上了就绝不重复判——两道同时生效会让成交价
    变成"谁先到谁赢"，而这条规则的全部价值就在于事后能按 source 算期望。

    为什么必须有：`modify_sl` 在价格已经回落到目标线下方时会被券商直接拒
    （10016），而这恰恰就是"锁利想防的事已经发生了"的那一刻。没有兜底的话，
    这笔单会一路退回开仓价（再由 be 兜底），锁利线形同虚设。

    为什么要求 `lock_try_ts`（**试过挂单**）而不只是"脉冲成立"：升级瞬间手里
    可能正拿着一笔峰值 6 ATR、已回落到 4.5 ATR 的旧持仓，`_mark_burst` 会当场
    把 t5 补记上。只按"脉冲成立 + 浮盈跌破锁定值"判，这一格就会**平掉一笔本来
    没打算平的盈利单**。加上"试过改单"这个前提，兜底只接管真正挂失败的那些。

    锁定值用 `peak − L×unit`：和 `_lock_target` 同一个式子，只是单位是钱而不是
    价格。峰值抬高时这条线跟着抬，所以它永远比"当前峰值 − L"更松不了。
    """
    if not pos or not bool(getattr(cfg, "lock_profit", False)):
        return False
    if not _lock_applies(pos):
        return False
    if pos.get("lock_ok") or pos.get("lock_mode") == "paper":
        return False
    if not pos.get("lock_try_ts"):
        return False        # 还没试过挂单（升级瞬间的旧持仓），不接管
    try:
        unit = float(pos.get("atr_pnl") or 0.0)
        peak = float(pos.get("peak_pnl") or 0.0)
        fp = float(float_pnl)
    except (TypeError, ValueError):
        return False
    if unit <= 0:
        return False
    locked = peak - _LOCK_L * unit
    if locked <= 0:
        return False
    return fp <= locked


def _be_local_hit(cfg: Cfg, pos: dict | None, float_pnl: float) -> bool:
    """保本线的**本地兜底**：券商端没挂上时，浮盈回到 0 由程序平。

    只在 `be_ok` 为假时成立。券商端挂上了就绝不重复判——两道同时生效的话，
    成交价变成"谁先到谁赢"，统计上再也分不清是券商 SL 打的还是程序平的，
    而这条规则的全部价值就在于事后能按 source 分桶算期望。

    为什么需要兜底：改单会失败（终端算法交易关了、锁被占、价格已经回到
    开仓价以下挂不上去）。没有兜底的话「保本」只是一句建议，最需要它的
    那笔单恰好就拿不到保护。
    """
    if not pos or not bool(getattr(cfg, "be_at_entry", False)):
        return False
    if not _be_applies(pos):
        return False
    if not pos.get("be_arm_ts") or pos.get("be_ok"):
        return False
    return float(float_pnl) <= 0


def _be_step(cfg: Cfg, pos: dict, float_pnl: float, now: int) -> dict | None:
    """保本止损：峰值到线后把券商端 SL 抬到开仓价。返回要落盘的一行或 None。

    **这一步只改券商端那条线**，不改任何平仓判定：挂上之后平不平仍然由
    券商 SL / _exit_gate / 模型三方照旧决定。

    为什么在 5 秒快查里做，而不是只在 60 秒的 tick 里做：3.63 那样的峰值是
    2 秒一格抓到的。等决策轮再看，价格早就回来了，止损也就没机会抬——
    而保护窗口恰好就是"刚摸到高点"那几分钟。

    返回 None 的情况：规则关 / 不是趋势单 / 已经处理过 / 还没到线 /
    失败节流期 / 终端这一格忙（正在连接）。返回 dict 时**调用方负责
    append_log + save_state**——本函数不落盘，理由和 _stale_hit 一样：
    写盘时机必须由唯一的那处决定。
    """
    if not pos or not bool(getattr(cfg, "be_at_entry", False)):
        return None
    if not _be_applies(pos):
        return None
    arm = money_at_lots(cfg, getattr(cfg, "be_arm_usd", 0.0) or 0.0)
    if arm <= 0:
        return None
    # 已经挂上（be_ok）或纸面已处理（be_mode=paper）就不再进
    if pos.get("be_ok") or pos.get("be_mode") == "paper":
        return None

    peak = pos.get("peak_pnl")
    try:
        peak = float(peak) if peak is not None else float(float_pnl)
        fp = float(float_pnl)
    except (TypeError, ValueError):
        return None
    if max(peak, fp) < arm:
        return None

    entry = float(pos.get("price_open") or 0.0)
    ticket = int(pos.get("ticket") or 0)
    if entry <= 0:
        return None
    now = int(now)

    # 第一次到线：先把「该有保本了」记下来。券商端挂不上时，_guard_hit
    # 靠这个标记做本地兜底——否则这条规则在最需要用它的场景里正好是空的。
    if not pos.get("be_arm_ts"):
        pos["be_arm_ts"] = now

    # 价格已经回到（或穿到）开仓价以下：多单的 SL 设在现价上方会被券商
    # 直接拒（10016）。这不是故障，是"这条线现在挂不上去了"，交给本地兜底，
    # 不再重试——重试一万次也不会成功。
    if fp <= 0:
        return None

    retry = int(getattr(cfg, "be_retry_sec", 20) or 0)
    last = pos.get("be_try_ts")
    if last is not None and retry > 0:
        try:
            if now - int(last) < retry:
                return None
        except (TypeError, ValueError):
            pass

    # 纸面单：券商那边没有这一笔，改单无从谈起。记录成 be_mode=paper，
    # 由本地兜底在浮盈回到 0 时平——纸面演练也要按同一套规则跑，
    # 否则"纸面表现"和"真实表现"差的就是这条保护。
    if pos.get("dry_run") or ticket <= 0:
        pos["be_try_ts"] = now
        pos["be_mode"] = "paper"
        pos["be_sl"] = float(entry)
        pos["be_ts"] = now
        return {"ts": now, "kind": "be", "ticket": ticket,
                "direction": pos.get("direction"),
                "mode": "paper", "sl": round(entry, 3),
                "peak_pnl": round(peak, 2), "fp": round(fp, 2),
                "reason": (f"纸面单：峰值 {peak:+.2f} ≥ {arm:.2f}，"
                           f"保本线设到开仓价 {entry:.3f}（由程序在浮盈回到 0 时平）")}

    res = send_modify_sl(cfg, ticket, entry)
    if res.get("busy"):
        # 终端这一格没排上队（正在连接 / 锁被占）：**不算失败**，也不消耗
        # 重试节流。算成失败的话，一次 30 秒的连接尝试会顺手吃掉一次重试窗口，
        # 而这段时间恰好是价格最可能回头的时候。
        return None
    pos["be_try_ts"] = now
    if res.get("ok"):
        pos["be_ok"] = 1
        pos["be_sl"] = float(res.get("price") or entry)
        pos["be_ts"] = now
        # 券商端那条线现在就是保本价，入场止损已经被它顶掉了。
        # 同步过来是为了让 `sl_broker` 始终等于「券商端当前实际的止损价」，
        # 面板只需读这一个字段；source 判定仍按 be_ok 优先，不受影响。
        pos["sl_broker"] = float(pos["be_sl"])
        pos.pop("be_err", None)
        return {"ts": now, "kind": "be", "ticket": ticket,
                "direction": pos.get("direction"), "mode": "broker",
                "sl": round(float(pos["be_sl"]), 3),
                "peak_pnl": round(peak, 2), "fp": round(fp, 2),
                "reason": (f"峰值浮盈 {peak:+.2f} ≥ {arm:.2f}，"
                           f"券商端硬止损已抬到开仓价 {float(pos['be_sl']):.3f}")}

    pos["be_err"] = str(res.get("reason") or "改单失败")
    tries = int(pos.get("be_tries") or 0) + 1
    pos["be_tries"] = tries
    # 只记第一次失败：节流期每 20 秒记一行的话，一小时能刷出 180 行噪音，
    # 面板上真正要看的"这笔发生了什么"会被淹掉。后续失败只更新 be_err，
    # 面板的「保本未挂上」标记会一直亮着。
    if tries == 1:
        return {"ts": now, "kind": "be_fail", "ticket": ticket,
                "direction": pos.get("direction"),
                "sl": round(entry, 3), "peak_pnl": round(peak, 2),
                "error": pos["be_err"],
                "reason": (f"峰值 {peak:+.2f} 到线，但保本止损没挂上"
                           f"（{pos['be_err']}）；浮盈回到 0 时由程序代平")}
    return None


def _close_deal_reason(pos: dict) -> int | None:
    """平仓那一笔 deal 的 reason（MT5 DEAL_REASON_*）。读不到返回 None。

    0/1/2 = 人工在终端/手机/网页点的，3 = EA/API，4 = 止损，5 = 止盈。

    有了它，「仓在终端消失」才分得清是**券商端保本止损打的**，还是人手动
    平的——两者在账本里都是"没有平仓单"，但一个是规则生效、一个是人为干预，
    混在一起算期望会把规则的效果冲淡。
    """
    fills = _close_fills(pos)
    return None if fills is None else fills["reason"]


def _guard_hit(cfg: Cfg, pos: dict, bid: float, ask: float,
               fp: float | None = None) -> str:
    """持仓期间的硬止损快查。返回 '' = 没触发。

    **只看止损这一类线**，故意不看止盈门槛、也不看持仓上限：
      · 止盈门槛一旦到线就该让模型说话，那是 tick() 里 _exit_gate 的活；
      · 持仓上限是分钟级的粗规则，拿 5 秒的节拍去看它是浪费。

    2026-10-29 加的第二条线：**保本止损的本地兜底**（见 _be_local_hit）。
    券商端挂上了（be_ok）就完全不判；只有改单一直没成功时，浮盈回到 0
    才由这里提前结束等待、交给下一轮 _exit_gate 去平。

    这里是纯读，不下单、不写流水、不调模型。开销就是一次 symbol_info_tick。
    `fp` 允许调用方传入已算好的浮盈（快查循环里峰值更新和止损判定共用一次
    _realized_pnl，不重复算）；不传就自己算。
    """
    if fp is None:
        fp = _realized_pnl(cfg, pos, bid, ask)
    # 止损线同样走 exit_params：快查是 5 秒一次的真闸门，不能和 60 秒决策轮
    # 用两套不同的数（一处缩放、一处不缩放 = 手数一大就提前止损）。
    sl = float(exit_params(cfg, pos.get("plan"))["sl_usd"] or 0.0)
    if sl > 0 and fp <= -abs(sl):
        return f"浮亏 {fp:+.2f} 触及 -{abs(sl):.2f}"
    # 锁利线位置比保本线高（峰值 −1.5 ATR > 0），价格往下走一定先破它，
    # 所以锁利这条排在前面判——反过来的话，跳空那一下会被记成 be_local。
    if _lock_local_hit(cfg, pos, fp):
        return f"浮盈 {fp:+.2f} 跌破锁利线（券商端未挂上，程序代平）"
    if _be_local_hit(cfg, pos, fp):
        return f"浮盈 {fp:+.2f} 回到 0（保本线，券商端未挂上，程序代平）"
    return ""


# ---------------------------------------------------------- 人工确认通道
#
# AI 辅助模式的全部机制就是这一块：worker 在开/平仓前停住，等人给一个答复。
# 四个约束，每一个都是踩过的坑：
#
#  1. **答复不能只靠内存 Event**。面板的 HTTP 线程与 worker 同进程时内存够用，
#     但 CLI 跑辅助模式、或面板重启后状态还在，Event 就不在了。所以权威通道
#     是账户目录下的 autotrade_signal.json（读一次就删），内存只做加速。
#  2. **不能写进 autotrade_state.json**。state 是整文件覆盖写，HTTP 线程
#     读改写会把 worker 期间写进去的 position/peak_pnl 覆盖回旧值。
#  3. **等待必须可被抢占**。硬止损、持仓上限、仓在终端消失，这三件事优先于
#     人的点击——人不在电脑前，不能让一个弹窗把止损卡住。
#  4. **取消优先于一切**。「关闭并清仓」时先取消等待再停线程，否则 join 超时
#     后 close_all 已经平完仓，worker 醒来拿到「同意」会再开一笔。


def signal_answer_path(login: int | str | None = None) -> Path:
    """人工答复的落地文件。独立小文件，不与 state 抢写。"""
    return paths.account_file(paths.resolve_login(login), "autotrade_signal.json")


class _SignalDesk:
    """进程内的单槽收发。

    一个进程只有一个 worker 线程，所以单槽就够——多槽只会引入"答复该给谁"
    的歧义。`id` 匹配是唯一的安全边界：过期的答复（上一轮弹窗）绝不作用于
    这一轮信号。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._evt = threading.Event()
        self._id: str | None = None
        self._answer: dict | None = None
        self._cancelled = False

    # ---- 等待方（worker 线程）----
    def open(self, sig_id: str) -> None:
        with self._lock:
            self._id, self._answer, self._cancelled = str(sig_id), None, False
            self._evt.clear()

    def close(self) -> None:
        with self._lock:
            self._id, self._answer = None, None
            self._evt.clear()

    def waiting(self, sig_id: str) -> bool:
        with self._lock:
            return self._id == str(sig_id)

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def take_answer(self) -> dict | None:
        """取一次即消费。同一条信号不会被结算两次。"""
        with self._lock:
            ans, self._answer = self._answer, None
            return ans

    def wait(self, sec: float) -> bool:
        """返回 True = 有事发生（答复或取消）。"""
        return bool(self._evt.wait(max(0.0, float(sec))))

    # ---- 答复方（HTTP 线程 / CLI）----
    def post(self, sig_id: str, approve: bool, source: str = "human") -> bool:
        with self._lock:
            if str(sig_id) != (self._id or ""):
                return False           # 不是当前在等的那条，丢弃
            self._answer = {"approve": bool(approve), "source": source,
                            "ts": int(time.time())}
            self._evt.set()
            return True

    def cancel(self) -> None:
        """Worker.stop() 在 `_stop.set()` **之前**调用。

        先取消再停，是为了让等待者立刻醒来、看到"已取消"然后什么都不做；
        反过来的话 join(15) 会超时，close_all 已经把仓平了，worker 醒来
        才拿到答复，于是「关闭并清仓」之后又开一笔。
        """
        with self._lock:
            self._cancelled = True
            self._evt.set()


DESK = _SignalDesk()

# 窗口提醒回调。面板启动时注册（把窗口还原并置顶几秒）；CLI 下没人注册。
#
# 为什么提醒不能只靠前端轮询：面板被最小化或在别的标签页时，浏览器会把
# setInterval 节流到每分钟一次，而入场信号只活 90 秒——可能一次都轮不到。
# 窗口层面的提醒必须由发信号的那一侧直接触发。
_SIGNAL_NOTIFIER = None


def register_signal_notifier(fn) -> None:
    """面板注册提醒回调。回调在 worker 线程里被调，必须自己异步化。"""
    global _SIGNAL_NOTIFIER
    _SIGNAL_NOTIFIER = fn


def _notify_signal(sig: dict) -> None:
    fn = _SIGNAL_NOTIFIER
    if fn is None:
        return
    try:
        fn(sig)
    except Exception:  # noqa: BLE001
        pass          # 喊不动人也不能打断下单流程


def pending_signal_view(login=None) -> dict:
    """轻量接口：只读 state，不读日志、不碰 MT5、不算浮盈。

    面板每秒无条件轮询它（不判页面可见性），所以开销必须小到"窗口最小化
    也敢每秒问一次"——这正是它在隐藏状态下仍然生效的前提。

    **没有 worker 在等的时候不返回信号。** 2026-10-29 实机：卡死 → 用户强关
    → 重启，state 里那条 pending_signal 还在（90 秒还没过期），页面立刻弹出
    一个"同意做空"的窗。可这个进程里没有任何人在读答复：点了 approve 只会
    写一个没人消费的文件，弹窗倒计时走完自动消失。人以为自己在授权下单，
    实际什么都发生过不了——比不弹更糟。

    判据是 `running`（外加进程内 worker 是否真活着）：`_tick_body` 开始就会
    把 running 写进 state，`_stop` 会清掉；正常等待期间 running 恒为 True。
    跨进程（CLI 跑 worker、面板只看）时 state 里同样是 True，不会误杀。
    """
    try:
        state = load_state(login)
    except Exception:  # noqa: BLE001
        state = {}
    now = int(time.time())
    running = bool(state.get("running"))
    # 面板重启过、worker 已经不在了，但 state 还写着 running=True：这时候
    # 信号同样是孤儿，不能因为字段是 True 就发给页面。
    if running and state.get("owner") == "panel" and not WORKER.alive():
        running = False
    sig = state.get("pending_signal") if running else None
    return {
        "ok": True,
        "server_ts": now,
        "assist": bool((state.get("cfg") or {}).get("assist")),
        "running": bool(state.get("running")),
        # 有信号却发不出去时把原因带上，省得以后又靠猜"为什么没弹"
        "signal_suppressed": (
            None if (sig or not state.get("pending_signal"))
            else ("not_running" if not state.get("running") else "worker_dead")),
        "signal": ({**sig, "remain_sec": max(0, int(sig.get("expires_at") or 0) - now),
                    "cool_left": signal_cool_left(state, sig, now)}
                   if sig else None),
    }


def _read_answer(path: Path, sig_id: str) -> dict | None:
    """读一次性答复文件。id 不匹配 = 上一轮遗留，当作没有。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or str(raw.get("id") or "") != str(sig_id):
        return None
    return {"approve": bool(raw.get("approve")), "source": raw.get("source") or "human"}


def _clear_answer(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def answer_signal(signal_id: str, approve: bool, login=None,
                  source: str = "human") -> dict:
    """面板按钮的唯一入口。**幂等**：同一条信号重复提交不会造成第二笔。

    先校验再写：`pending_signal` 不存在、id 不匹配、已过期，一律拒绝并说明
    原因——面板要拿这句话显示"这条信号已经失效"，而不是静默失败。
    """
    sid = str(signal_id or "").strip()
    if not sid:
        return {"ok": False, "error": "缺少信号编号"}
    state = load_state(login)
    sig = state.get("pending_signal") or {}
    if str(sig.get("id") or "") != sid:
        return {"ok": False, "expired": True,
                "error": "这条信号已失效或已处理"}
    if int(time.time()) > int(sig.get("expires_at") or 0):
        return {"ok": False, "expired": True, "error": "已过答复时限"}
    row = {"id": sid, "approve": bool(approve), "ts": int(time.time()),
           "source": source}
    # 权威通道先落盘再从内存唤醒：内存通道在跨进程/重启时不存在，
    # 文件在两种情况下都可靠。
    paths.write_json(paths.resolve_login(login), "autotrade_signal.json", row)
    DESK.post(sid, bool(approve), source)
    return {"ok": True, "id": sid, "approve": bool(approve)}


def _new_signal(kind: str, cfg: Cfg, *, direction: int, p_up: float | None,
                prog_dir: int | None, reason: str | None, bid: float,
                ask: float, price: float, wait_sec: int,
                agree=None, advice: str | None = None,
                advice_text: str | None = None, act=None,
                feat: dict | None = None, ticket=None,
                held_sec: int | None = None, fp: float | None = None) -> dict:
    """造一条待确认信号。

    **时间戳用真实时钟**，不用 tick 的 `now`：`now` 是可注入的测试值
    （单测传 2026-09 的固定秒），拿它算 expires_at，真实时钟会当场判超时，
    面板倒计时也会算成负数。
    """
    now = int(time.time())
    return {
        "id": os.urandom(6).hex(), "kind": kind,
        "ts": now, "expires_at": now + max(5, int(wait_sec)),
        "direction": int(direction), "p_up": p_up, "prog_dir": prog_dir,
        "agree": agree, "act": act,
        "advice": advice, "advice_text": advice_text,
        "reason": reason, "bid": float(bid), "ask": float(ask),
        "price": round(float(price), 3),
        "feat": feat or {}, "ticket": ticket,
        "held_sec": held_sec, "fp": fp,
        "dry_run": bool(cfg.dry_run), "wait_sec": max(5, int(wait_sec)),
    }


# 静默期那句话的开头（见 tick 里的冷却分支）。只描述上一轮**真的发生了什么**：
# 「未获批准」不能盖住「批准了但没能执行」——后者是价格跑掉了，不是人没点。
_DENY_HEAD: dict[str | None, str] = {
    "deny": "上一条信号你否决了",
    "expired": "上一条信号无人应答、按不做处理",
    "cancelled": "上一条信号因做单线程停止而作废",
    "blocked": "上一条信号因已有持仓而作废",
    "void_slip": "上一条信号已同意，但价格已朝不利方向跑出上限、作废",
    "void_unknown": "上一条信号已同意，但读不到持仓、作废",
    "void_state": "上一条信号已同意，但当时已不可开仓、作废",
}


def _last_answer(state: dict) -> str | None:
    """上一轮人工应答的结局（deny / expired / void_slip …）。旧 state 没有就 None。"""
    ld = state.get("last_decision")
    if not isinstance(ld, dict):
        return None
    ans = ld.get("answer")
    return str(ans) if ans else None


def _assist_deny_until(state: dict, kind: str) -> int:
    """取某个信号类型的静默期截止时间。旧 state 没有这个字段时返回 0。"""
    raw = state.get("assist_deny_until")
    if not isinstance(raw, dict):
        return 0
    try:
        return int(raw.get(kind) or 0)
    except (TypeError, ValueError):
        return 0


def _set_assist_deny(state: dict, kind: str, sec: int) -> None:
    raw = state.get("assist_deny_until")
    if not isinstance(raw, dict):
        raw = {}
    raw[kind] = int(time.time()) + max(0, int(sec))
    state["assist_deny_until"] = raw


def _mark_answered(state: dict, answer: str) -> None:
    """把「这一轮最终没进场」写到 last_decision 上。

    为什么必须写：`last_decision` 在轮首就落好了（那时只有模型的原话），
    人工应答发生在它之后。不主动改的话，状态带会一直挂着「模型开仓」
    + 做多的理由，而弹窗那边写着「已否决」——同一块屏幕上两句相反的话。
    10-29 用户当场就问「我否决开仓信号后，上面的分析怎么还不变」。

    只加两格，且都只是描述事实：
      · answer     —— deny / expired / cancelled / blocked / void_slip / void_unknown
      · deny_until —— 静默到期时刻（绝对时间戳）。前端拿它和 server_ts 比，
                      还没过期才显示「静默到 HH:MM」；过期就只留标签，
                      免得几小时后又翻出来一句"静默到 21:57"。

    **reason 一个字都不动**：复盘要看的仍然是「它当时怎么说的」，只是现在
    能同时知道这句话已经被否掉了。
    """
    ld = state.get("last_decision")
    if not isinstance(ld, dict):
        return
    ld["answer"] = answer
    until = _assist_deny_until(state, "entry")
    try:
        until = int(until)
    except (TypeError, ValueError):
        until = 0
    # 只有还没到期的才挂上去。cancelled 不写冷却，这时取到的可能是更早
    # 一次否决留下的旧值——已经过期就清成 None，否则标签会指着一个早就
    # 过去的时间。
    ld["deny_until"] = until if until > int(time.time()) else None


def _await_human(cfg: Cfg, sig: dict, login=None, pos: dict | None = None,
                 *, clock=None, quote_fn=None, positions_fn=None,
                 sleeper=None) -> dict:
    """阻塞等人点，返回 {"answer", "waited_sec", "bid", "ask", "fp", "src", "late"}。

    answer 取值与含义：

        approve   人点了同意
        deny      人点了否决
        expired   到期无人应答（入场=不做；出场=按模型决定执行）
        cancelled Worker.stop()（**只落日志，绝不下单**）
        blocked   入场等待期间品种上出现了持仓（这 90 秒里你自己开了一笔）
        gone      出场等待期间仓在终端消失（交给下一轮的"外部平仓"分支）
        sl        出场等待期间浮亏触及硬止损（程序抢跑）
        timeout   出场等待期间越过持仓上限（程序抢跑）

    **判定顺序每片都一样：取消 → 超时 → 行情抢占 → 人的答复。**
    事实优先于意愿：同一片里人的点击和止损同时到，按止损处理，把那次点击
    记成 `late=True` 留痕。反过来的话，人点得稍慢一点就能让浮亏突破 3 美元。
    """
    clock = clock or time.time
    q_fn = quote_fn or _quote
    p_fn = positions_fn or _positions
    sid = str(sig.get("id"))
    deadline = float(sig.get("expires_at") or 0)
    started = float(clock())
    is_exit = sig.get("kind") == "exit"
    bid = float(sig.get("bid") or 0.0)
    ask = float(sig.get("ask") or 0.0)
    fp = sig.get("fp")
    ans_path = signal_answer_path(login)
    # 行情检查节流。0.2 秒查一次 = 120 秒里打 600 次终端，而**答复延迟仍只有
    # 0.2 秒**——答复走 Event，行情走定时器，两件事的密度没必须一样。
    # 入场侧只在"这期间是否多出一笔持仓"上关心行情，10 秒足够。
    guard = max(2.0, min(float(getattr(cfg, "guard_sec", 0.0) or 5.0), 5.0)) \
        if is_exit else 10.0
    next_mkt = started
    head = {"bid": bid, "ask": ask, "fp": fp, "src": None, "late": False}

    def ret(answer: str, t: float, **kw) -> dict:
        out = dict(head)
        out.update(kw)
        out["answer"] = answer
        out["waited_sec"] = max(0.0, float(t) - started)
        return out

    DESK.open(sid)
    try:
        while True:
            t = float(clock())

            # 1) 取消：最高优先。此刻 close_all 可能已经在平仓，任何下单都是灾难
            if DESK.cancelled():
                return ret("cancelled", t)

            # 2) 超时：到点即结论。迟到的答复由 answer_signal 那边拦住
            if deadline and t >= deadline:
                return ret("expired", t)

            # 3) 行情抢占：客观事实优先于人的意愿
            if t >= next_mkt:
                next_mkt = t + guard
                q = q_fn(cfg.symbol)
                if q:
                    bid, ask = float(q[0]), float(q[1])
                    head["bid"], head["ask"] = bid, ask
                if is_exit and pos:
                    ps = p_fn(cfg.symbol)
                    # 读不到持仓（None）是"不知道"，不是"没有"——照 _positions
                    # 的约定，不能据此判断已经平了。
                    if ps is not None and not any(
                            int(p.get("ticket") or 0) == int(pos.get("ticket") or -1)
                            for p in ps):
                        return ret("gone", t)
                    if q:
                        fp = _realized_pnl(cfg, pos, bid, ask)
                        head["fp"] = fp
                        if _guard_hit(cfg, pos, bid, ask, fp=fp):
                            return ret("sl", t, late=bool(DESK.take_answer()))
                    opened = int(pos.get("opened_at") or t)
                    if cfg.max_hold_sec and (t - opened) >= cfg.max_hold_sec:
                        return ret("timeout", t, late=bool(DESK.take_answer()))
                elif not is_exit and not cfg.dry_run:
                    ps = p_fn(cfg.symbol)
                    if ps:                     # 这 90 秒里你自己开了一笔
                        return ret("blocked", t)

            # 4) 人的答复：先内存（同进程零延迟），再文件（跨进程 / 面板重启）
            ans = DESK.take_answer() or _read_answer(ans_path, sid)
            if ans is not None:
                return ret("approve" if ans["approve"] else "deny", t,
                           src=ans.get("source"))

            # 5) 睡一小片。有答复/取消都会 set Event，立刻醒来
            if sleeper is not None:
                sleeper(0.2)
            else:
                DESK.wait(0.2)
    finally:
        DESK.close()
        _clear_answer(ans_path)       # 一次性消费，防止下一轮误读上一条


def _nap(sec: float, stop_wait=None, sleeper=None) -> bool:
    """睡一段。传了 stop_wait（面板线程的 Event.wait）时返回是否收到停止信号。"""
    sec = max(0.0, sec)
    if stop_wait is not None:
        return bool(stop_wait(sec))
    (sleeper or time.sleep)(sec)
    return False


def _wait_next(started: float, row: dict, cfg: Cfg, login=None,
               stop_wait=None, sleeper=None, quote_fn=None) -> bool:
    """等下一轮决策。有仓时把长睡切成快查一格，过线就提前返回。

    快查每格做五件事（一次报价、一次浮盈，全部复用）：
      1. **峰值更新**（_track_peak）：只在峰值被抬高时写一次 state——峰值只增
         不减，一笔持仓真正抬高的次数有限，无条件每格写盘不可接受。
      2. **保本止损上抬**（_be_step，2026-10-29）：趋势单峰值首次到
         be_arm_usd 时把券商端 SL 改到开仓价。只在这一格做是不够的（5 秒的
         节拍会漏掉一瞬间的峰值），但**只靠 60 秒的决策轮一定漏**——这正是
         10-29 那笔 +3.63 → −3.11 的形状。挂上之后每格空转。
      3. **止损判定**（_guard_hit）：浮亏过线提前返回；券商端没挂上保本线时，
         浮盈回到 0 也在这里提前返回（本地兜底）。
      4. **tp 穿越唤醒**（cfg.tp_wake）：浮盈首次 ≥ 这一笔的门槛（趋势 7.0 /
         震荡 3.25，按 plan 取）也提前返回。**只叫醒第一次**——到过门槛之后
         由决策轮按 tp_reask_sec 复核，这里不再插队（见第 6 条）。
      5. **峰值停滞唤醒**（_stale_hit，2026-10-29）：浮盈摸到高点后停滞
         stale_sec 秒、且已经掉到 tp×stale_frac 以下，也提前返回一次。
         同样只是"把最坏 60 秒的无人看守缩到一格"，阈值一个字没改。
      6. **到过门槛后的复核节拍**（tp_reask_sec，2026-10-29）：**不在这一层做**。
         已到过门槛的单子在这一层刻意不提前返回，按决策轮的 60 秒自然节拍
         走到 tick()，由 _exit_gate 的 tp_recheck 分支放行或再等一格。
         理由：如果这里也按 fp≥tp 叫醒，浮盈稳定在门槛上方就变成每 2~5 秒
         问一次模型——那是把 60 秒的判断变成 2 秒的骚扰，不是本规则要的东西。

    步长是**自适应**的：fp 离止盈门槛远时按 guard_sec 粗看，进入
    [tp_min - tp_wake_band, ∞) 后切 guard_near_sec 细看。采样密度花在决策
    边界附近——fp 在 0 附近时细看是浪费，逼近 3.25 时粗看会错过穿越。

    返回 True = 收到停止信号（只有面板线程会传 stop_wait，CLI 永远是 False）。

    为什么是「提前返回」而不是「在这里平仓」：平仓要走 tick() 里的
    _exit_gate → send_close → append_log → queue_review 整条链。在这里复制一份
    就等于给同一件事留两个实现，迟早对不上账。这里只负责**把最坏 60 秒的
    无人看守缩到一格**，剩下的原样交给下一轮。
    """
    deadline = _wait_deadline(started, next_wait_sec(row, cfg), cfg)
    # 辅助模式下 tick 可能被人等拖长（入场 90 秒 / 出场 120 秒），deadline
    # 在 tick 结束时早就过期了，下面会退化成"睡 1 秒再问一次模型"——刚问完人
    # 立刻又问，既费 token 又会在冷却生效前反复落 skip 行。这种情况下从
    # **现在**重新计时。非辅助模式不受影响（tick 本身就 < 1 秒）。
    if getattr(cfg, "assist", False) and time.time() - started > 1.0:
        deadline = _wait_deadline(time.time(), next_wait_sec(row, cfg), cfg)
    # 夹一道：配得比决策间隔大就等于没开，不如直接退回旧行为，别让短周期
    # 配置（interval_sec=3）里一个默认的 5 秒快查把节奏搅乱。
    guard = min(float(getattr(cfg, "guard_sec", 0.0) or 0.0), float(cfg.interval_sec))
    near = min(float(getattr(cfg, "guard_near_sec", 0.0) or 0.0), guard)
    band = money_at_lots(cfg, getattr(cfg, "tp_wake_band", 0.0) or 0.0)

    # 无仓 / 平仓轮 / 刚停：不需要快查，睡满一整轮（也省掉每格的报价读取）
    pos = load_state(login).get("position") if guard > 0 else None
    # 门槛按**这一笔自己的 plan** 取（趋势 7.0 / 震荡 3.25）。以前这里固定读
    # cfg.tp_min_usd，趋势单在 3.25 就被叫醒，醒来又撞上 tp_wait → tick 空转，
    # 每 2 秒白算一次 K 线。和 _exit_gate 用同一个 exit_params，口径不许分家。
    tp = float(exit_params(cfg, pos.get("plan"))["tp_min_usd"] or 0.0) if pos else 0.0
    wake = bool(getattr(cfg, "tp_wake", False)) and tp > 0
    if not pos or row.get("kind") in ("close", "stop"):
        return _nap(max(1.0, deadline - time.time()), stop_wait, sleeper)

    quote_fn = quote_fn or _quote
    fp_last: float | None = None
    while deadline - time.time() > 1.0:
        # 近档判定用上一格的浮盈：这格还没读到报价，只能拿已知信息选步长。
        # 第一次进循环 fp_last 是 None，必然远档——合理，刚开仓时离门槛最远。
        tight = wake and band > 0 and fp_last is not None \
            and fp_last >= tp - band
        step = min(near if tight else guard, deadline - time.time())
        if _nap(step, stop_wait, sleeper):
            return True
        q = quote_fn(cfg.symbol)
        if not q:
            continue
        fp = _realized_pnl(cfg, pos, q[0], q[1])
        peak_up = _track_peak(pos, fp, int(time.time()))
        # 保本止损（2026-10-29）：峰值刚摸到 3.25 的那一格就把券商端 SL 抬到
        # 开仓价——等 60 秒那一格再看，价格往往已经回落，止损就没机会抬了。
        # 和 tick 里同一个 _be_step，挂上过一次之后自动空转（be_ok）。
        be_row = _be_step(cfg, pos, fp, int(time.time()))
        if be_row:
            append_log(be_row, login)
        # 脉冲锁利：和 tick 里同一个 _lock_step。之所以必须在快查里做，
        # 理由和 be 一样——脉冲的峰值是 2 秒一格抓到的，等 60 秒那一轮再看，
        # 价格早就从峰值退下来了，锁利线也就抬不到该在的位置。
        lock_row = _lock_step(cfg, pos, int(time.time()))
        if lock_row:
            append_log(lock_row, login)
        if peak_up or be_row or lock_row:
            # 重读再写：state 里可能还有面板写的其他字段，别拿旧快照整体覆盖。
            # 外部平仓竞态无害：pos 还是旧引用，峰值照记，下一轮 tick()
            # 检测外部平仓时会整体覆盖处理。
            fresh = load_state(login)
            fresh["position"] = pos
            save_state(fresh, login)
        if _guard_hit(cfg, pos, q[0], q[1], fp=fp):
            return False       # 剩下的时间不要了，立刻进下一轮去平仓
        if wake and fp >= tp and not pos.get("tp_reached_ts"):
            # tp 穿越唤醒：**只叫醒第一次**（tp_reached_ts 由 tick 的
            # _tp_reached_step 写入）。到过门槛之后如果还按 fp>=tp 提前返回，
            # 浮盈一稳定在门槛上方就会每 2~5 秒叫一次模型——那是骚扰不是判断。
            return False
        if _stale_hit(cfg, pos, fp, int(time.time())):
            # 峰值停滞唤醒（2026-10-29）：和止损快查一样只提前返回，不平仓。
            # 判定用 tick 里同一个 _stale_hit，保证"快查认为该醒"和"闸门放行"
            # 永远同步——两处各写一份条件，迟早会出现快查醒了但闸门还在 tp_wait
            # 的空转（当年 tp_wake 的唤醒点和 tp_min 就是差点这么漂开的）。
            return False
        fp_last = fp
    # 尾段不足 1 秒（或已经超时）：保底睡 1 秒，别空转
    return _nap(max(1.0, deadline - time.time()), stop_wait, sleeper)


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
    if cfg.assist:
        mode = f"AI 辅助（{mode}）"
    align = (f"（对齐 K 线收盘，+{cfg.align_settle_sec:g} 秒）"
             if cfg.align_settle_sec > 0 else "（自由跑表，不对齐收盘）")
    # 「不挂 SL/TP」是 _attach_entry_sl 之前的说法。现在开仓成交后会往券商端挂
    # 硬止损（只不挂 TP），启动横幅不该还在骗人——sl_usd=0 才是真的不挂。
    sl_txt = (f"券商端 SL {cfg.sl_usd:g} USD / 不挂 TP"
              if cfg.sl_usd and float(cfg.sl_usd) > 0 else "不挂 SL（sl_usd=0）/ 不挂 TP")
    print(f"AI 交易员启动 | {cfg.symbol} 超短线 {cfg.lots} 手 | 每 {cfg.interval_sec} 秒决策{align} | "
          f"平仓后冷却 {cfg.cooldown_sec} 秒（同向 {cfg.cooldown_same_sec} 秒）| {sl_txt} | {mode}")
    if cfg.assist:
        print("⚠ 辅助模式在命令行里没有面板可点：所有信号都会超时 → "
              "入场一律不做、出场按模型决定平。要人点请用面板。")
    print("Ctrl-C 停止。状态与流水在账户目录下 autotrade_state.json / autotrade_log.jsonl")

    try:
        while True:
            started = time.time()
            row = tick(cfg, login=login)
            print(_fmt(row), flush=True)
            if row.get("stop"):
                break
            _wait_next(started, row, cfg, login)
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
    # 全账户持仓读一次：清仓要用它挑品种，记账还要用它判断「state 里那笔是不是
    # 早就不在了」。`_positions(symbol)` 本身就是 `open_positions()` 再按品种过
    # 一遍，所以这里不用再多读一次。
    account = _all_positions()

    if cfg.dry_run:
        untouched = len(account or [])
    else:
        if account is None:
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

        positions = (account if not symbol_only
                     else [p for p in account if str(p.get("symbol")) == cfg.symbol])
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
        ticket = int(pos.get("ticket") or 0)
        # 这笔是不是早就不在了（2026-11-12 实盘打回）。
        #
        # 用户先在终端手动平了，再点面板「停止并清仓」：那时 account 上已经没有
        # 这笔仓，`done` 是空的，下面的 `ticket in closed_tickets` 不成立——旧代码
        # 什么都不做，于是 state 里这笔**永远卡着**：pnl 不计、closed 不加、下一轮
        # 启动第一眼看到的是"还有仓"（其实是幽灵），复盘也排不进去。
        # 判据只认「全账户持仓里找不到它」，读不到持仓（account is None）时
        # 一律不动——把"不知道"当成"已平"会记出一笔不存在的盈亏。
        gone = (not is_paper and account is not None
                and not any(int(p.get("ticket") or 0) == ticket for p in account))
        if is_paper or ticket in closed_tickets or gone:
            bid, ask = _quote(cfg.symbol) or (0.0, 0.0)
            # 优先照券商成交记账（清仓也是市价单，回执和 deal 都拿得到）；
            # 纸面没有真实成交，退回按报价估。
            pnl, exit_price, fills = _settle_pnl(cfg, pos, bid, ask)
            if gone and ticket not in closed_tickets:
                # 仓位不是这次清掉的：要么用户在终端手动平的，要么券商端
                # SL/TP 已经打掉。来源判定与 `tick` 那条路共用 `_gone_source`，
                # 否则同一件事会被记成两种来源、喂出方向相反的教训。
                source, why_txt = _gone_source(
                    pos, None if fills is None else fills["reason"])
            else:
                source, why_txt = "panel", why
            # 清仓也走同一个记账出口。以前这里少 peak_pnl/peak_ts/plan/atr，
            # 于是"面板清仓的单子"在按打法 / ATR 倍数的统计里整批消失。
            row = _book_close(state, pos, pnl, now, source=source, reason=why_txt,
                              dry_run=is_paper)
            row["exit_price"] = round(float(exit_price), 5)
            row["pnl_src"] = "deal" if fills is not None else "quote"
            append_log(row, login)

            # 面板清仓这一笔也要复盘：AI 停掉后不会跑，队列留在 state 里，
            # 下次启动时 if 到期就跑（K 线还在，窗口按 closed_at 切片，不受影响）。
            pos["exit_reason"] = why_txt
            queue_review(state, cfg, pos, exit_price, pnl, now)
            item = {"ticket": pos.get("ticket"), "paper": is_paper,
                    "pnl": round(pnl, 2)}
            if gone and not done:
                item["external"] = True
            done.append(item)

    state["running"] = False
    state["stop_reason"] = why
    save_state(state, login)

    append_log({"ts": now, "kind": "flatten", "reason": why,
                "closed": len(done), "failed": len(failed),
                "untouched": untouched}, login)
    out = {"ok": not failed, "closed": done, "failed": failed,
           "count": len(done), "dry_run": cfg.dry_run}
    notes: list[str] = []
    # 补记的那几笔**不是这次清掉的**，面板上的「平掉 N 笔」会让人以为是自己
    # 平成功的。用 note 说清楚：仓位早就不在了（人工平的 / 券商端线被打），
    # 这笔只是把账补上。
    ext = [d for d in done if d.get("external")]
    if ext:
        amt = sum(float(d.get("pnl") or 0.0) for d in ext)
        notes.append(f"其中 {len(ext)} 笔早已不在账户上（人工平仓或券商端线被触发），"
                     f"已按成交回执补记 {amt:+.2f} 并清空状态")
    if cfg.dry_run and untouched:
        notes.append(f"纸面模式没有动券商上的 {untouched} 笔真实持仓；"
                     "要平真单请取消勾选「真实下单」后重新启动，或者直接去终端手动平。")
    if notes:
        out["note"] = "；".join(notes)
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
        # 上一轮异常退出可能留下残状态：清掉，免得新线程把旧信号当成自己在等
        DESK.close()

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
                "lots": cfg.lots, "interval_sec": cfg.interval_sec,
                "assist": cfg.assist}

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
            _wait_next(started, row, cfg, login, stop_wait=self._stop.wait)

        state = load_state(login)
        state["running"] = False
        save_state(state, login)

    def stop(self, login=None, cfg: Cfg | None = None,
             flatten: bool = False) -> dict:
        """停线程。flatten=True 时顺带清仓（面板的「关闭并清仓」）。"""
        # **先取消再停**，顺序不能反。辅助模式下 worker 可能正卡在
        # _await_human 里（最长 120 秒），只 set _stop 的话 join(15) 必然超时，
        # 而 close_all 已经把仓平完了——worker 随后醒来拿到「同意」，会在
        # 「已关闭并清仓」之后重新开一笔。取消让等待者立刻返回 cancelled，
        # 那条路径只落日志、绝不下单。
        DESK.cancel()
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


def reset_history(login: int | str | None = None,
                  clear_lessons: bool = False) -> dict:
    """清空「战绩」：把决策流水和 state 归档，然后从零开始。

    为什么要清：战绩会被拼进提示词（样本数、命中率），旧样本会让它越来越保守。
    换参数或换思路之前必须有一个干净起点，否则新策略永远背着旧样本的包袱。

    为什么是「归档」不是「删除」：流水是唯一能回答「当时为什么这么判断」的
    东西，删掉就再也说不清。改名成 `autotrade_log.<时间戳>.jsonl` 留在同目录，
    面板只读不带时间戳的那两个文件名，所以等于从零开始，人随时还能翻旧账。

    **教训默认保留**（`clear_lessons=False`）：教训在 `lessons.jsonl` 里，
    和流水解耦。2026-10-26 之前它挂在流水上，一次重置就等于把经验清零——
    实测 13:55 那次重置让 188 条教训全部不可见，注入变成「暂无复盘笔记」。
    经验要跨场次才有意义，所以清经验必须是单独的动作（见 `reset_lessons`）。
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
    targets = [(log_path(login), "log"), (state_path(login), "state")]
    if clear_lessons:
        targets.append((lessons_path(login), "lessons"))
    for p, key in targets:
        if p.exists():
            dst = p.with_name(f"{p.stem}.{tag}{p.suffix}")
            try:
                p.replace(dst)
                archived[key] = dst.name
            except OSError as exc:
                return {"ok": False,
                        "error": f"归档失败（{key}）：{exc}。没有删除任何东西。"}
            if key == "lessons":
                # 留一个空文件 = 已清空的状态标记。没有它，lesson_rows() 会
                # 退回去读归档流水，清完立刻又"长"出来。
                p.write_text("", encoding="utf-8")

    save_state(_empty_state(), login)
    # 新流水第一行记一笔重置，之后翻日志能看出「这里换了个起点」
    append_log({"ts": int(time.time()), "kind": "reset", "cleared": cleared,
                "archived": archived}, login)
    return {"ok": True, "archived": archived, "cleared": cleared,
            "lessons_kept": not clear_lessons, "lessons": len(lesson_rows(login))}


def _prefs_snapshot(saved_cfg: dict | None = None) -> dict:
    """面板控件的上次选择。坏了就当没记过，绝不让它拖垮整个 summary。

    面板 1 秒轮询一次，这里会跟着读一次小文件（<1KB）。值得：
    缓存在进程里反而会和「另一个面板进程刚改过」打架——同一账户本来就
    只该跑一个面板，但这个文件是全局的，多开调试时读盘才是真相。

    **prefs 文件还没生成时，用 state 里上一场的 cfg 兜底**：这是给老用户
    的一次性迁移。他升级前已经跑过若干场，state 里躺着上次真用的品种/模式/
    间隔，但从来没有地方记过「界面默认值」。不兜底的话，他打开新版面板看到
    的仍是出厂默认（真实做单 / 60 秒），还得再手动选一遍——那正是这个功能
    要消灭的动作。只在文件缺失时生效，绝不覆盖用户的显式选择。
    """
    try:
        from live import prefs

        if saved_cfg and not prefs.prefs_file().exists():
            try:
                cfg = Cfg(**{k: v for k, v in saved_cfg.items()
                             if k in Cfg.__dataclass_fields__})
                return prefs.prefs_from_cfg(cfg)
            except (TypeError, ValueError):
                pass
        return prefs.load_prefs(TRADABLE_SYMBOLS)
    except Exception:  # noqa: BLE001
        return {}


def playbook_of(state: dict, rows: list[dict],
                now: float | None = None) -> dict:
    """当前 regime 下生效的打法：与持仓无关，空仓也必须有具体文案。

    面板原来把「打法」显示成 `position.plan`，空仓时必然是「–」——而人最
    需要知道打法的时刻恰恰是空仓、准备手动下单的时候。这里改成「先判行情、
    再选打法」的分流结果，与 tick 里真正决定提示词 / 程序建议 / 锚点开关 /
    开仓 plan 的那个 regime 同源（见 tick 里的 `regime_of(bars)`）。

    出场参数走 `exit_params()`，和真正下单用的是同一套口径；文案在后端算好，
    前端不拼字符串——参数改一次，前后端各写一份必然对不上。
    """
    raw = state.get("cfg") or {}
    cfg = Cfg(**{k: v for k, v in raw.items()
                 if k in Cfg.__dataclass_fields__})
    ld = state.get("last_decision") or {}
    regime = str(ld.get("regime") or "")
    ts = int(ld.get("ts") or 0)
    iv = int(cfg.interval_sec or 60)
    age = max(0, int((now or time.time()) - ts)) if ts else None
    # 大字承载的是「现在」的状态：超过两个决策间隔就已经不是现在了。
    # 放大一个 14 分钟前的判定，比把它写成小字更危险。
    stale = bool(ts and age is not None and age > 2 * iv)

    # plan 在开仓时锁定；这里用当前 regime 预览「若此刻开仓会按哪套参数走」。
    # mid / 空值按震荡处理，与 exit_params 的历史行为一致。
    ep = exit_params(cfg, regime if regime in ("trend", "range") else "range")
    tp = float(ep["tp_min_usd"])
    hold = int(ep["max_hold_sec"])
    hold_txt = (f"{hold / 3600:g}h" if hold >= 3600 and hold % 3600 == 0
                else f"{hold // 60}min")

    # 趋势市的上/下行取最近一次特征快照的 drift，与 _structure_text 同源。
    drift = 0.0
    for r in reversed(rows):
        f = r.get("feat") or {}
        if "drift" in f:
            try:
                drift = float(f["drift"])
            except (TypeError, ValueError):
                pass
            break

    if not regime:
        name, action = "待判定", "等待首轮判定"
        detail = f"间隔 {iv}s · 还没跑过"
    elif regime == "range":
        name, action = "震荡市", "低买高卖"
        detail = f"tp {tp:g} / {hold_txt} · 等待锚点生效"
    elif regime == "mid":
        name, action = "方向不明", "只做位置明显的单"
        detail = f"tp {tp:g} / {hold_txt} · 只在上下沿动手"
    elif not cfg.trend_follow:
        name, action = "趋势市", "不交易"
        detail = "trend_follow 已关闭 · 本程序不建议开仓"
    else:
        name = "趋势市"
        action = "顺势做多" if drift >= 0 else "顺势做空"
        detail = f"tp {tp:g} / {hold_txt} · 锚点停用，不等回踩"

    return {
        "regime": regime, "name": name, "action": action, "detail": detail,
        "tp": tp, "max_hold_sec": hold, "interval_sec": iv,
        "ts": ts, "age_sec": age, "stale": stale,
        "trend_follow": bool(cfg.trend_follow), "anchor": regime != "trend",
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
    # 旧 state 没有这个字段时回退到**当前**默认（默认真实），
    # 写死 True 会让面板显示"真实"而实际按纸面跑，是最坏的那种谎。
    dry_run = bool(saved_cfg.get("dry_run")) if "dry_run" in saved_cfg else Cfg.dry_run
    # 辅助模式同理：旧 state 里没有这个字段时回退到当前默认（False）。
    # 写死 True 会让面板显示"辅助"而实际按真实单跑——那是最坏的一种谎。
    assist = bool(saved_cfg.get("assist")) if "assist" in saved_cfg else Cfg.assist
    pending = state.get("pending_reviews") or []

    # 人工确认统计：从日志现算，不做缓存——和这个函数里其它指标一个口径。
    # 关心的不是"弹了几次"，而是 aprove_rate：人点同意的比例长期偏低，
    # 说明模型的信号质量差；偏高但期望差，说明人只是跟着模型走。
    #
    # **有信号 ≠ 该弹窗。** 2026-10-29 实机：面板卡死 → 强关 → 重启，state 里
    # 那条 90 秒还没过期的 pending_signal 被原样送到页面，立刻弹出「同意做空」。
    # 可这个进程里没有任何 worker 在读答复——点 approve 只是写一个没人消费的
    # 文件，倒计时走完消失。人以为自己授权了一笔单，实际什么都没发生。
    #
    # 判据用 running（外加 stale：面板重启后线程早没了，state 却还写着 True）。
    # 正常等待期间 running 恒为 True；跨进程（CLI 跑 worker、面板只看）时 state
    # 里也是 True，不会误杀。
    signal_now = (state.get("pending_signal")
                  if (running and not stale) else None)
    sig_rows = [r for r in rows if r.get("kind") == "signal"]
    sig_ans: dict[str, int] = {}
    for r in sig_rows:
        key = str(r.get("answer") or "?")
        sig_ans[key] = sig_ans.get(key, 0) + 1
    _waits = [float(r.get("waited_sec") or 0) for r in sig_rows
              if r.get("waited_sec") is not None]
    _decided = (sig_ans.get("approve", 0) + sig_ans.get("deny", 0)
                + sig_ans.get("expired", 0))
    signal_stats = {
        "n": len(sig_rows),
        "approve": sig_ans.get("approve", 0),
        "deny": sig_ans.get("deny", 0),
        "expired": sig_ans.get("expired", 0),
        "void_slip": sig_ans.get("void_slip", 0),
        "cancelled": sig_ans.get("cancelled", 0),
        "avg_wait_sec": round(sum(_waits) / len(_waits), 1) if _waits else None,
        "approve_rate": (round(sig_ans.get("approve", 0) / _decided, 4)
                         if _decided else None),
    }
    # 旧 state 里没有新加的字段，缺的时候要回退到当前默认值
    _defaults = Cfg()
    # 面板上摆的必须是**实际生效值**：手数放大时金额阈值等比放大，直接把
    # 存档里的原始值（3.00）摆出来，用户会拿它去对 0.03 手那场的账。
    # 这里只借一个 Cfg 壳算缩放，不改存档、不写回 state。
    _view_cfg = Cfg(lots=saved_cfg.get("lots", _defaults.lots))

    # 今日口径：按平仓行的本地日期算。**不能拿 state["pnl"] 冒充「今日」**——
    # 那个数是本进程累计，点一次「重置战绩」就归零，跨午夜也不会归零。
    # 卡片上写「今日累计」就必须真的是今天。
    today_str = time.strftime("%Y-%m-%d")
    closes_today = [r for r in closes
                    if time.strftime("%Y-%m-%d",
                                     time.localtime(int(r.get("ts") or 0))) == today_str]
    wins_today = sum(1 for r in closes_today if float(r.get("pnl") or 0) > 0)
    pnl_today = round(sum(float(r.get("pnl") or 0) for r in closes_today), 2)

    # 持仓浮动盈亏：面板顶部的常驻状态带要显示「这笔单现在赚还是亏」。
    # 不在这里算的话，用户得滚回页面最上面看，或者去看券商的浮动（那是整个
    # 账户的，含手动单，和这一笔对不上）。
    # 代价是每次 summary 多读一次报价——面板 1 秒轮询一次，一次
    # symbol_info_tick，和机器人自己每 5 秒读一次是同一个量级。
    # 拿不到报价就给 None，让前端显示「—」，绝不猜一个数出来。
    float_pnl = None
    pos_now = state.get("position")
    if pos_now:
        try:
            cfg_now = Cfg(**{k: v for k, v in saved_cfg.items()
                             if k in Cfg.__dataclass_fields__})
        except (TypeError, ValueError):
            cfg_now = _defaults
        q = _quote(cfg_now.symbol)
        if q:
            float_pnl = _realized_pnl(cfg_now, pos_now, q[0], q[1])

    # 面板上要能看见"现在用的是哪个模型"：换供应商（DeepSeek → 智谱免费档）
    # 之后如果面板不显示，出了 401/404 根本分不清是 key 没换还是模型名写错。
    try:
        from agent.llm import _load_dotenv, DEFAULT_MODEL

        _load_dotenv()
        model_name = os.getenv("LLM_MODEL") or DEFAULT_MODEL
    except Exception:  # noqa: BLE001
        model_name = "未知"

    # 做单台态势带：当前打法 + 人/模型对照。账本读失败不能拖垮整个 summary
    # ——面板每秒轮询，/api/live 挂掉比少一块信息严重得多。
    try:
        from live import journal
        ledger = journal.load_ledger(login)
    except Exception:  # noqa: BLE001
        ledger = {}
    playbook = playbook_of(state, rows)
    vs = model_vs_actual(
        rows, ledger,
        interval_sec=int(saved_cfg.get("interval_sec") or 60),
        symbol=str(saved_cfg.get("symbol") or ""))

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
        # 持仓浮动盈亏（USD）。None = 没仓或拿不到报价。状态带用它显示实时盈亏，
        # 和峰值 peak_pnl 一起看就能当场判断「是不是摸到过门槛又吐回去」。
        "float_pnl": float_pnl,
        "last_decision": state.get("last_decision"),
        "trades": len(opens),
        "closed": closed,
        "wins": wins,
        "hit_rate": round(wins / closed, 4) if closed else None,
        "pnl": state.get("pnl", 0.0),
        # 今日口径（按平仓行本地日期）。面板和幕布上写「今日」的地方都用这组，
        # 别再用 state["pnl"]——那个会随「重置战绩」归零，不是自然日。
        "closed_today": len(closes_today),
        "wins_today": wins_today,
        "losses_today": len(closes_today) - wins_today,
        "pnl_today": pnl_today,
        "llm_decisions": len(llm_decisions),
        "llm_closes": len(llm_closes),
        "llm_hit_rate": round(llm_wins / len(llm_closes), 4) if llm_closes else None,
        # 程序硬止损平的笔数。单独的指标：止损多说明入场方向差，
        # 而不是"模型平得不好"，两者的处理方式完全不同。
        "sl_closes": sum(1 for r in rows
                         if r.get("kind") == "close" and r.get("source") == "sl"),
        # 保本止损平的笔数（券商端 / 程序代平都算）。和 sl_closes 分开：
        # 硬止损说明入场方向错了，保本止损说明方向对过但没吃到，两者的
        # 改法完全相反。攒够样本才谈得上判这条规则值不值。
        "be_closes": sum(1 for r in rows if r.get("kind") == "close"
                         and r.get("source") in ("be", "be_local")),
        # 新出场规则的阈值，面板上要能看见，否则用户不知道规则已经变了。
        # 旧 state 里的 cfg 没有这两个字段（那会儿还不存在），
        # 所以缺了就回退到 Cfg 的当前默认值，而不是显示 None。
        "sl_usd": money_at_lots(_view_cfg, saved_cfg.get("sl_usd", _defaults.sl_usd)),
        "tp_min_usd": money_at_lots(_view_cfg,
                                    saved_cfg.get("tp_min_usd", _defaults.tp_min_usd)),
        # 金额单位（USD / USC）。上面这些阈值是**账户结算货币**的数字，
        # 面板必须把它贴在数字旁边：美分户的 3.00 是 3 分，不标出来
        # 会和美元户的 3.00 长得一模一样，差 100 倍也看不出来。
        "money_unit": money_label(_acct_currency()),
        "account_currency": _acct_currency(),
        # 1 USD 折多少账户货币（美元户 1，美分户 100）。面板上的成本估算
        # 都是按美元算的，要乘这个系数才能和上面的阈值同口径。
        "acct_factor": _acct_factor(),
        "max_hold_sec": saved_cfg.get("max_hold_sec", _defaults.max_hold_sec),
        # 趋势打法（2026-10-28 分 regime）。这三个字段旧 state 里没有，
        # 回退到当前默认值；面板持仓行按 position.plan 选用哪套显示。
        "tp_min_trend": money_at_lots(_view_cfg,
                                      saved_cfg.get("tp_min_trend", _defaults.tp_min_trend)),
        "max_hold_trend": saved_cfg.get("max_hold_trend", _defaults.max_hold_trend),
        "trend_follow": bool(saved_cfg.get("trend_follow", _defaults.trend_follow)),
        # 止损快查间隔。0 = 关（每 interval_sec 才看一次止损）。面板要显示，
        # 否则用户没法判断「止损会不会超调」这件事现在到底有没有生效。
        "guard_sec": saved_cfg.get("guard_sec", _defaults.guard_sec),
        # 自适应近档 + tp 穿越唤醒。同理：面板不显示，用户就不知道
        # 「浮盈到 3.25 会被 2 秒内交模型」这件事开着还是关着。
        "guard_near_sec": saved_cfg.get("guard_near_sec", _defaults.guard_near_sec),
        "tp_wake_band": money_at_lots(_view_cfg,
                                      saved_cfg.get("tp_wake_band", _defaults.tp_wake_band)),
        "tp_wake": saved_cfg.get("tp_wake", _defaults.tp_wake),
        # 到过门槛后的复核节拍（2026-10-29）。必须上面板：这笔单到底是
        # 「到过门槛、每分钟问一次」还是「没到门槛、不问」，用户得能对上账，
        # 否则面板显示"本轮不调模型"会被理解成程序坏了。
        "tp_reask_sec": saved_cfg.get("tp_reask_sec", _defaults.tp_reask_sec),
        # 峰值停滞唤醒（2026-10-29）。同理必须上去：用户得能看见
        # 「浮盈摸到高点后停滞 3 分钟会被问一次」这件事到底开着还是关着，
        # 否则面板显示的和实际跑的又是两套。
        "stale_wake": bool(saved_cfg.get("stale_wake", _defaults.stale_wake)),
        "stale_sec": saved_cfg.get("stale_sec", _defaults.stale_sec),
        "stale_frac": saved_cfg.get("stale_frac", _defaults.stale_frac),
        "stale_reask_sec": saved_cfg.get("stale_reask_sec", _defaults.stale_reask_sec),
        "stale_min_peak": money_at_lots(_view_cfg,
                                        saved_cfg.get("stale_min_peak", _defaults.stale_min_peak)),
        # 保本止损（2026-10-29）。面板必须能看见"这条线开着还是关着、触发线
        # 是多少"——挂的是券商端的单，页面上不写出来，用户没法判断自己看到的
        # 持仓到底有没有保护。持仓行另外会读 position.be_ok / be_sl 显示实际状态。
        "be_at_entry": bool(saved_cfg.get("be_at_entry", _defaults.be_at_entry)),
        "be_arm_usd": money_at_lots(_view_cfg,
                                    saved_cfg.get("be_arm_usd", _defaults.be_arm_usd)),
        "reviews": len(reviews),
        "last_lesson": reviews[-1]["lesson"] if reviews else None,
        # 教训库（独立文件、不随流水归档）的条数。面板上要能看见：
        # 「重置」之后这里不清零，才说明经验真的留住了。
        "lessons": len(lesson_rows(login)),
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
        # 品种选择器：面板下拉框直接读这个，别在前端另存一份白名单——
        # 前端写死一份、后端校验另一份，加品种时必然漂移。
        "symbols": symbol_options(),
        "symbol": saved_cfg.get("symbol", _defaults.symbol),
        # 面板「交易参数」的默认值：上次选过的品种 / 模式 / 间隔（见 live/prefs.py）。
        # 和上面的 cfg 是两件事：cfg 是「正在跑（或上一场跑过）的参数」，
        # prefs 是「下次打开时帮用户填好」。没开跑过的人只有后者。
        "prefs": _prefs_snapshot(saved_cfg),
        # ---------------- AI 辅助做单 ----------------
        # pending_signal 的存在本身就是「现在该弹窗」。面板 1 秒轮询这一格，
        # 不去另开接口——多一个请求就多一处会不同步的地方。
        "assist": assist,
        "pending_signal": (
            {**signal_now, "remain_sec": max(0, int(signal_now.get("expires_at") or 0)
                                             - int(time.time())),
             "cool_left": signal_cool_left(state, signal_now)}
            if signal_now else None),
        # 倒计时基准用服务端时钟：客户端时钟偏几分钟的话，弹窗会显示负秒数。
        "server_ts": int(time.time()),
        "signal_stats": signal_stats,
        "assist_cfg": {
            "entry_wait_sec": saved_cfg.get("assist_entry_wait_sec",
                                            _defaults.assist_entry_wait_sec),
            "exit_wait_sec": saved_cfg.get("assist_exit_wait_sec",
                                           _defaults.assist_exit_wait_sec),
            "max_slip_usd": saved_cfg.get("assist_max_slip_usd",
                                          _defaults.assist_max_slip_usd),
            "deny_cooldown": saved_cfg.get("assist_deny_cooldown",
                                           _defaults.assist_deny_cooldown),
        },
        "assist_deny_until": state.get("assist_deny_until") or {},
        # 态势带（2026-11-05）：打法与「我 vs 模型」都在后端算好，前端只显示。
        "playbook": playbook,
        "vs_actual": vs,
        "recent": rows[-recent:],
    }


def trade_detail(ticket: int, login: int | str | None = None,
                 live: bool = False) -> dict:
    """一笔交易的完整档案：原料 → 结构 → 程序判断 → 模型判断 → 执行 → 结果。

    面板的交易溯源接口背后是这个。设计约束：
    - **只读日志，不调模型，不连终端**（除非 live=True 且要补 tick）。
      查历史不能产生 API 费用，也不能因为 MT5 没开就查不了。
    - 2026-10-26 起，open 行自带 `feat` 特征快照（ER/位置/振幅×ATR/regime/
      prog_dir/M5 背景），所以**结构数不再依赖终端**——终端没开也能看到
      当时的特征。下面这段"现拉 K 线"只是补 K 线原文，补不到就明说。
    - K 线原文没有落盘（每笔存 14 行会让日志膨胀 3~4 倍）。live=True 时
      从终端补拉开仓时刻附近的数据重建；拿不到就如实说"行情已过，无法重建"，
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
                structure = _structure_text(window, cfg.zone_edge,
                                            trend_follow=cfg.trend_follow)
                prog_dir = _prog_direction(window, cfg.zone_edge,
                                           cfg.trend_follow)
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
    feat = open_row.get("feat") or None
    prog_dir = open_row.get("prog_dir")
    if prog_dir is None and isinstance(feat, dict):
        # 2026-10-26 起 open 行带特征快照，里面的 prog_dir 比重建算出来的可靠得多：
        # 它是决策当下程序真实给出的建议，不是事后拿同一批 K 线重算的近似值。
        prog_dir = feat.get("prog_dir")
    model_dir = open_row.get("direction")
    agree_field = open_row.get("agree")
    if prog_dir is None and structure:
        # 旧日志没有 prog_dir 字段（2026-10-25 之前），从重建的结构补算
        try:
            prog_dir = _prog_direction(
                _bars(cfg.symbol, cfg.tf, cfg.bars + 120)[-VISIBLE_BARS:],
                cfg.zone_edge, cfg.trend_follow)
        except Exception:  # noqa: BLE001
            pass
    if prog_dir is not None and model_dir is not None:
        verdict = ("一致" if int(prog_dir) == int(model_dir)
                   else "分歧" if int(prog_dir) != 0 else "程序无建议，模型自选")
    else:
        verdict = None

    # 终端重建失败时，把「特征其实还在」说清楚——否则看面板的人会以为
    # 这笔的 ER/位置全丢了。丢的只是 K 线原文，结构数在 feat 里。
    if structure_note and isinstance(open_row.get("feat"), dict):
        structure_note += "；但决策时刻的特征快照完整（见下），结构数没有丢"

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
            # 决策时刻的特征快照。2026-10-26 之前的老单没有这个字段，
            # 那时候只存了 p_up 和理由，结构数算完就扔了。
            "feat": open_row.get("feat"),
            "mtf": (open_row.get("feat") or {}).get("mtf")
                   if isinstance(open_row.get("feat"), dict) else None,
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

    parser = argparse.ArgumentParser(description="AI 实时做单（默认真实下单；加 --paper 只记账）")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--lots", type=float, default=0.01,
                        help="开仓手数（只接受 0.01/0.02/0.03/0.05；出场金额阈值会等比缩放）")
    parser.add_argument("--interval", type=int, default=Cfg.interval_sec,
                        help="决策间隔秒数（最小 3，模型单次约 2.6 秒）")
    parser.add_argument("--cooldown", type=int, default=Cfg.cooldown_sec,
                        help="平仓后冷却秒数（反向；也是方向未定时的兜底）")
    parser.add_argument("--cooldown-same", type=int, default=Cfg.cooldown_same_sec,
                        help="平仓后同向重进的冷却秒数（应大于等于 --cooldown）")
    parser.add_argument("--max-trades", type=int, default=0, help="开仓笔数上限，0=不限")
    parser.add_argument("--max-loss", type=float, default=0.0, help="累计亏损上限 USD，0=不限")
    parser.add_argument("--max-minutes", type=int, default=0, help="运行时长上限分钟，0=不限")
    # 用户 10-26 要求默认真实下单，所以这个开关反过来了：
    # 以前要 --live 才发真单，现在要 --paper 才不发单。
    parser.add_argument("--paper", action="store_true", help="只记账不发单（纸面模式）")
    # AI 辅助模式：CLI 也能跑，但没有面板就没有人点，所有信号都会超时。
    # 保留这个开关是为了无界面调试（配 --once 可以走一遍完整链路）。
    parser.add_argument("--assist", action="store_true",
                        help="AI 辅助：开/平仓前等人确认（无面板时会全部超时）")
    parser.add_argument("--assist-wait", type=int, default=Cfg.assist_entry_wait_sec,
                        help=f"入场确认时限秒数（默认 {Cfg.assist_entry_wait_sec}）")
    parser.add_argument("--assist-slip", type=float, default=Cfg.assist_max_slip_usd,
                        help=f"价格漂移作废阈值 USD（默认 {Cfg.assist_max_slip_usd}）")
    parser.add_argument("--status", action="store_true", help="只看状态，不跑")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    args = parser.parse_args()

    cfg = Cfg(
        symbol=args.symbol, lots=args.lots, interval_sec=args.interval,
        cooldown_sec=args.cooldown, cooldown_same_sec=args.cooldown_same,
        dry_run=args.paper,
        max_trades=args.max_trades, max_loss_usd=args.max_loss,
        max_minutes=args.max_minutes,
        assist=args.assist, assist_entry_wait_sec=args.assist_wait,
        assist_max_slip_usd=args.assist_slip,
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
