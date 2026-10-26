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
    # 决策节奏。用户 10-26 明确要求默认 60 秒：3 秒那个档位一轮决策约 2.6 秒，
    # 等于不间断连打模型，一天上万次调用，而且平仓后 30 秒冷却根本插不进去。
    interval_sec: int = 60
    cooldown_sec: int = 30
    magic: int = 20261022
    # 用户 10-26 明确要求默认真实做单。**面板启动前仍要过一次确认弹窗**，
    # 且纸面/真实二选一始终留在界面上——默认值只是省一次点击，不是免确认。
    dry_run: bool = False
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
    # 为什么不信模型：前四大亏（-6.08/-3.51/-2.70/-2.49）全是 p_up=0.0 的单子。
    # 但注意 p_up=0.0 当时被 confidence=max(p_up,1-p_up)=1.0 记成"最自信"，
    # 实际那是输出示例的占位值被照抄，不是自信（见 clamp_p_up）。所以这条教训
    # 的真实含义是"模型给的数会骗人，不能让它单独管出场"，不是"它自信时必亏"。
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
    # 持仓期间的硬止损快查间隔（秒）。0 = 关，回到「每 interval_sec 才看一次止损」。
    #
    # 为什么必须有：决策间隔是 60 秒，而 XAUUSD M1 的单根 K 线振幅中位就有 1.51
    # USD、P90 2.99。止损 3.00 只在每 60 秒那一格被检查一次，意味着价格在两次
    # 检查之间可以跑完整根 K 线——**止损永远不是按 3.00 成交的**。
    # 实测证据（2026-10-26 13:13 那笔）：13:42:42 浮亏 -2.06，13:43:42 以 -3.77
    # 平掉，超调 0.77 = 把 3.00 的止损放大 26%。227 笔反事实回放里所有规则都按
    # -3.00 精确成交算，等于给了自己一个乐观假设。
    #
    # 这个快查**只读一个 tick、只判 sl_usd 一条、不调模型**：过线就提前结束等待，
    # 让下一轮 tick() 照常走 _exit_gate→send_close 的老路平仓。
    # 刻意不在这里直接下单——复制一份平仓逻辑就等于有两个账本口径。
    # 5 秒的依据：一次 tick 读取的成本可以忽略；模型单次调用实测 2.6 秒，但这个
    # 循环不碰模型，所以不受它限制。
    guard_sec: float = 5.0
    # 近门槛快查步长（秒）。快查循环的步长不是均匀的：浮盈离止盈门槛远时按
    # guard_sec 粗看，一旦 fp >= tp_min_usd - tp_wake_band（默认 2.25 起）就切到
    # 这一档细看。理由（第一性原理）：采样密度该花在决策边界附近——fp 在 0 附近
    # 时用 2 秒盯是浪费，fp 逼近 3.25 时 5 秒反而可能错过穿越。2 秒下限的依据与
    # guard_sec 相同：再快 MT5 tick 读取的抖动比价格变化还大。
    guard_near_sec: float = 2.0
    # 近门槛带宽（USD）：浮盈进入 [tp_min - band, ∞) 就切近档。0 = 关闭近档，
    # 整轮都按 guard_sec 粗看（纯记录模式）。
    tp_wake_band: float = 1.0
    # tp 穿越是否提前唤醒。True 时快查里发现 fp >= tp_min_usd 就提前结束等待，
    # 下一轮 tick() 的 _exit_gate 会因为到线返回 None 而把决定交给模型——
    # **参数没变，变的只是延迟**：模型从「最坏 60 秒后才被问到」变成「≤2 秒」。
    # False = 快查只记录峰值和止损，出场节奏完全回到老行为。
    tp_wake: bool = True
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


def lessons_path(login: int | str | None = None) -> Path:
    """教训库的当前文件路径。**唯一出口**。

    教训库现在是「可切换的包」：一组命名好的 .jsonl，面板上选一个生效。
    包管理在 `live/lesson_packs.py`，这里只是转发——`active_file()`
    返回当前激活的包。

    `login` 参数保留但**不再使用**：以前教训按账户隔离，现在全局共享。
    保留签名是为了不破坏 `append_lesson(row, login)` 等老调用方。

    以前教训只能从流水里读，而「重置」会把流水改名归档 → 教训跟着一起消失：
    实测 2026-10-26 13:55 一次重置，188 条教训全变成不可见。经验要跨场次
    才有意义，就不能挂在会被清空的文件上。改成独立包之后，重置只清战绩、
    清经验是另一个动作（`/api/autotrade/reset_lessons`），两者彻底解耦。
    """
    from live import lesson_packs
    return lesson_packs.active_file()


def _empty_state() -> dict:
    return {
        "running": False,
        "started_at": None,
        "last_tick_ts": None,
        "last_open_ts": None,
        "last_close_ts": None,
        "last_close_pnl": None,
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


def append_lesson(row: dict, login: int | str | None = None) -> None:
    p = lessons_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(p: Path) -> list[dict]:
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


def lesson_rows(login: int | str | None = None) -> list[dict]:
    """教训库的全部行。**不受 400 行窗口限制**（以前踩过）。

    `lesson_clusters` 原来走 `read_log(limit=400)`：日志写到 808 行时，
    188 条复盘只有 86 条进库，而且随着日志变长不断往后滚——次数永远涨不到
    HARD_RULE_MIN=5，铁律机制自动失效。教训库现在是独立文件、全量读。

    包改成全局后：当前没有激活的包时返回 `[]`——不再退回首账户流水
    （那是老 fallback，全局包语义下「没有包」就该是空的，不要把某个
    账户的流水当教训来用）。
    """
    p = lessons_path(login)
    if p.exists():
        return _read_jsonl(p)
    return []


def migrate_lessons(login: int | str | None = None) -> dict:
    """一次性把历史流水里的复盘搬进 `lessons.jsonl`（含归档文件）。

    只在升级时跑一次。dedupe 用 (ts, ticket, lesson)：重复跑不会翻倍。
    老数据没有 theme/scope，用词典 + 关键词推断，**推断结果如实标注**
    (source="migrated")，不假装是模型自己标的。
    """
    p = lessons_path(login)
    if p.exists() and _read_jsonl(p):
        return {"ok": False, "error": "lessons.jsonl 已有内容，不覆盖"}
    acc = paths.account_dir(paths.resolve_login(login))
    seen: set[tuple] = set()
    rows: list[dict] = []
    for f in sorted(acc.glob("autotrade_log*.jsonl")):
        for r in _read_jsonl(f):
            if r.get("kind") != "review" or not r.get("lesson"):
                continue
            key = (r.get("ts"), r.get("ticket"), r["lesson"])
            if key in seen:
                continue
            seen.add(key)
            lesson = _unwrap_lesson(r["lesson"])
            theme = r.get("theme") if r.get("theme") in LESSON_THEME_ENUM else ""
            if not theme or theme == "其他":
                theme = lesson_theme(lesson)
            scope = r.get("scope") if r.get("scope") in LESSON_SCOPES else ""
            if not scope:
                scope = infer_scope(lesson)
            rows.append({
                "ts": int(r.get("ts") or 0), "ticket": r.get("ticket"),
                "lesson": lesson, "theme": theme, "scope": scope,
                "regime": r.get("regime") or "", "pnl": r.get("pnl"),
                "direction": r.get("direction"), "source": "migrated",
            })
    rows.sort(key=lambda r: r["ts"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return {"ok": True, "migrated": len(rows), "files": len(list(acc.glob("autotrade_log*.jsonl")))}


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

    实测 46% 的 p_up 精确等于 0.0，|p_up-0.5|≥min_edge 的闸门对它们完全
    无效（偏离 0.5 达 0.5，比任何正常估计都"强"）。根因已查明：ENTRY_SYSTEM
    的输出示例原来写着 `"p_up": 0.00`，模型照抄这个占位值，程序按
    `p_up > 0.5 else -1` 读成"最强看空"→ 89 笔清一色空单。示例已改。

    注意 clamp **替代不了修示例**：0.0 被压成 0.20 之后仍然是"最强看空"
    （|0.20-0.5|=0.30 照样过闸门），还是开空单，只是日志里再也看不到 0.0
    ——等于把 bug 藏起来。所以判断 p_up 是否真的散开，要看 0.20 / 0.80
    这两个边界值的出现频率，它们等价于旧的 0.0 / 1.0。
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


def _unwrap_lesson(text: str) -> str:
    """拆掉没解包的 JSON。

    历史 bug：早期复盘调用 JSON mode 时，`lesson` 直接存成了整段
    `{"answer":"..."}`（实测 224 条里有 10 条）。注入提示词时它既难读、
    又让关键词归类和去重全部失效，所以在入口处统一拆一次。
    """
    raw = " ".join((text or "").split())
    if not raw.startswith("{"):
        return raw
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(obj, dict):
        for k in ("lesson", "answer", "复盘", "reason", "text"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())
    return raw


def parse_review(text: str) -> dict | None:
    """从复盘回复里抠出 {scope, theme, lesson}。空回复返回 None。

    **宽进**：模型可能给 JSON、可能给纯文本、也可能 JSON 里缺字段。
      - JSON 齐全 → 直接用；theme 不在枚举里（或给了"其他"）→ 用关键词再试一次；
      - scope 不合法 / 缺 → 关键词推断（infer_scope）；
      - 根本没有 JSON → 整段当 lesson，主题和端都靠关键词推断。
    宁可归错一个桶，也不能因为格式不对就把这条教训丢掉——以前
    `_chat` 的 json_mode 会被一次 429 永久关掉，复盘现场本来就是纯文本返回。
    """
    raw = " ".join((text or "").split())
    if not raw:
        return None

    data: dict | None = None
    m = _JSON_RE.search(raw)
    if m:
        try:
            loaded = json.loads(m.group(0))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = None

    lesson = raw
    theme = ""
    scope = ""
    if data is not None:
        got = str(data.get("lesson") or data.get("复盘") or data.get("answer") or "").strip()
        lesson = " ".join(got.split()) or raw
        theme = str(data.get("theme") or data.get("主题") or "").strip()
        scope = str(data.get("scope") or data.get("端") or "").strip().lower()
    lesson = _unwrap_lesson(lesson)

    if theme not in LESSON_THEME_ENUM or theme == "其他":
        # 模型没给 / 给了枚举外的词 / 偷懒写"其他" → 关键词再试一次；
        # 还是归不进去就明确标"其他"，落盘能看出"这条没归类"。
        theme = lesson_theme(lesson) or "其他"
    if scope not in LESSON_SCOPES:
        scope = infer_scope(lesson)
    return {"theme": theme, "scope": scope, "lesson": lesson[:120]}


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


def visible_bars(rows: list[dict]) -> list[dict]:
    """裁到 AI 允许看到的 14 根。所有喂给模型的 prompt 都必须过这一道。"""
    return rows[-VISIBLE_BARS:] if rows else []


ENTRY_SYSTEM = """你是 {SYMBOL_LABEL} 的超短线交易员，看 M1 图。
你赚的是 1~3 分钟内的那一小段价差——不是趋势、不是波段、更不是隔夜。
目标是尽可能多地把市场里真实存在的利润拿下来：单笔目标小、出手频次高，
所以每一笔都必须干净利落，不要指望"大行情"来救你。

【信息边界】
你只能看到三样东西：
  1) 当前 1 根 M1 和之前 13 根，共 14 根（约 14 分钟），以及这段时间的逐笔 tick；
  2) 一行 M5 背景摘要：14 根 M5（约 70 分钟）的结构结论，程序算好的，没有原始 K 线；
  3) 一个 MA28 数字：最近 28 根 M1 的收盘均价，以及当前收盘相对它的位置。
     这同样是程序算好的一个数，**不是 28 根原始 K 线**，你看不到那 28 根长什么样。
除此之外没有别的数据，也不要假装有：不要引用均线系统、其它技术指标、新闻、
成交量、市场情绪——除了 MA28，别的均线你也看不到，编出来就是幻觉。

M5 背景怎么用：它只回答"大方向偏哪一边"，**不参与 1~3 分钟的进出场判定**。
M1 决定时机，M5 决定你更愿意站在哪一侧。两者打架时（M1 位置提示做多、
M5 却是下跌趋势），不是禁止交易，但你需要更强的理由，p_up 也不该给到激进的一端。

MA28 怎么用：它是 28 分钟的价格中枢，回答"中期偏向哪边"，和 M5 同级，
**同样不参与 1~3 分钟的时机判定**。
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

【先判行情，再选打法（两套打法，别用反）】
用「结构 ER（净移动 ÷ 总行程，0~1）」判断这 14 根的性质，再选对应打法：
- ER < 0.30 → 窄幅震荡（小波动）：低买高卖。
  贴近区间下沿做多、贴近上沿做空（以数据里的「位置判定」为准），中间区域不动手；
  不要追着单根大阳线/大阴线跑，那通常是震荡里的假突破。
  **这三条里只有震荡市可以「贴边就动手」**：区间没被突破，上沿就是卖压所在。
  贴着上沿做空不叫追空，那是震荡市最标准的动作，别拿趋势市的"不追"来管它。
- ER > 0.50 → 单边趋势（大波动）：不要逆势抄底摸顶。
  等回踩到均势位置再顺势进——回踩位参考前一根实体的中位、
  或最近 3~4 根的低点（上升趋势）/高点（下降趋势）。没回踩就等。
- 0.30 ~ 0.50 → 方向不明确：只做位置明显的单（收盘位置贴近区间边缘），
  中间位置给 0.50，不开仓。
  数据里的「位置判定」已经替你算好了是否够边缘（默认阈值 35%/65%），直接按它走。

【贴边怎么处理：先看行情，四种情况别混成一锅】
「贴下沿」「贴上沿」本身不是买卖信号，要看这 14 根是什么性质：
- **震荡（ER < 0.30）**：贴下沿做多、贴上沿做空。**这是唯一可以贴着边动手的情形。**
  区间没被突破，上沿就是卖压、下沿就是支撑。判据是 ER 破没破，不是"价格看起来太高"。
- **下跌趋势（ER > 0.50 且 drift < 0）**：贴下沿是弱势延续不是超卖，**不接多**；
  做空要等反抽到均势位置（前一根实体中位、或最近 3~4 根高点），没反抽就等。
  **但"等反抽"不等于永远不做——反抽到位就是做空点。**
- **上涨趋势（ER > 0.50 且 drift > 0）**：贴上沿是强势延续不是超买，**不追多**；
  做多要等回踩，没回踩就等。同理，等到了该做就做。
- **方向不明（0.30 ~ 0.50）**：只做收盘位置够边缘的单，中间不动手。

**一句话记法：震荡市贴边就动手（下沿买、上沿卖）；趋势市等回撤/反抽到位再顺势做；
两种情形都不许逆势抄底摸顶。**

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
  3) 【主】结构指标：ER、振幅与 ATR 倍数、收盘位置、连阳连阴、行情判定
  4) 【背景】M5 结构摘要（一行；程序拿不到时这一行不出现）
  5) 【背景】MA28 与当前收盘的相对位置（一行；算不出来时不出现）
  6) 【辅】tick 数据：每根的笔数与买价区间，末行给全窗口点差与最大单步跳动
  7) 短周期变化与振幅
  8) 你的历史战绩与最近复盘
  9) 当前报价与来回成本
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
p_up —— 未来 1~3 分钟内价格上涨的概率，0 到 1 之间的小数。
        **它是方向概率，不是信心分。** 写 0.32 的意思是"我判断有 68% 的概率下跌"，
        这本身就是做空的理由——不要把它翻译成"看不准所以不做"。
        看跌和"要开空单"之间只差一个位置确认，不差勇气。
        即使 act=false 也要给，用来记录你当时的方向倾向。
        **只在 0.20 ~ 0.80 之间取值**：0/1 是伪精确（没人能百分百确定 1 分钟后
        的方向），写 0.20 就代表你最强的看空，0.80 代表最强的看多。
        **上面示例里的 0.58 只是格式示范，不是建议值。** 每一次都要按当根
        K 线重新估、写你自己的数；照着示例填等于没有判断，而且系统会把它
        当成"极端确信"直接下单（历史上 46% 的单子就是这样变成了空单）。
agree —— 你的方向判断是否与数据里「位置判定」的程序倾向一致。
        一致 true，不一致 false（此时 reason 必须给出你反对的依据）。
reason —— 不超过 40 字的中文依据，必须引用输入里的具体数字。

注意 act 和 reason 必须一致：写了"不追""等确认"，act 就是 false。
不要出现理由说不做、act 却给 true 的情况。

【判断原则】
- **不做交易是允许的，而且经常是对的。** 给出 `"act": false` 即可，
  这是正常决策，不是失误，不需要找补。
- 反过来，结构确实给出方向时就敢做。**p_up 0.40 和 0.60 是同样的强度**：
  一个是做空、一个是做多，没有哪个方向"更安全"、也不需要更高门槛。
  只要与 0.5 的距离够 0.06（系统闸门）且位置确认过，就该动手，
  别拿"还不够有把握"当借口把机会放过去。
- **act=false 只用于两件事：方向不清，或位置不到。** 不用于"我有方向但不敢"。
  如果你算出来 p_up ≤ 0.40（明确看跌）却给 act=false，reason 里必须写清
  是哪个具体数字让这个位置不能做空（比如"贴下沿做空等于追空"），
  不许只写"等企稳""再观察"这种没有落点的话。
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
{mtf}{ma}
【辅】Tick 级数据（逐笔 bid/ask，时间段与上面 14 根完全相同；只用于确认突破真假与点差）
{ticks}
短周期变化 r1={r1:+.3%}  r5={r5:+.3%}  r13={r13:+.3%}  振幅={span}
战绩 样本={n_closed} 笔  命中率={hit}
{recent_close}{lessons}
报价 bid={bid}  ask={ask}  点差={spread_pts} 点
时间 {clock} UTC"""

EXIT_SYSTEM = """你是 {SYMBOL_LABEL} 超短线交易员的持仓管理模块，只决定「继续持有」还是「立刻平仓」。
这套打法的钱来自「方向对的时候拿够一段」，不是靠秒级周转——实测 60 秒就跑的
时候平均只抓到很小一段（黄金实盘口径：约 1 美元），光点差就吃掉四分之一，
做的越多亏得越快。

【信息边界】
你只能看到两样东西：
  1) 当前 1 根 M1 和之前 13 根，共 14 根（约 14 分钟），以及这段时间的逐笔 tick；
  2) 一行 M5 背景摘要（14 根 M5 约 70 分钟的结构结论，程序算好的，没有原始 K 线）。
除此之外没有更多数据。M5 只用来判断"这波大方向还有没有余量"，
不改变 M1 的出场时机判断。

【输入格式（顺序固定）】
  1) 品种与周期
  2) 【主】14 根 M1 K 线：序号 / 时间 / 开 / 高 / 低 / 收
  3) 【主】结构指标：ER、振幅与 ATR 倍数、收盘位置、连阳连阴、行情判定
  4) 【背景】M5 结构摘要（一行；拿不到时不出现）
  5) 【辅】tick 数据（只用于确认突破真假与点差异常）
  6) 短周期变化
  7) 当前持仓：方向 / 手数 / 开仓价 / 已持仓时间 / 浮动盈亏 / 当初开仓依据
  8) 你从自己交易里总结的教训（按重复次数排序）
  9) 当前报价
 10) 当前时间（最后一行）

出场同样要看教训：像「止跌没延续就该立刻走」这种话，只有出现在出场判断里才有用。

【输出】
只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀：
{"close": false, "reason": ""}
close —— true 表示立刻市价平仓，false 表示继续持有。
reason —— 不超过 40 字的中文依据，必须引用具体数字。

【系统的自动规则（不用你判断，也不要跟它较劲）】
- 浮亏达到 {SL_USD} USD：系统直接市价平，不会问你；
- 持仓满 {MAX_HOLD_MIN} 分钟：系统无条件平掉，不管盈亏；
- 浮盈不到 {TP_MIN_USD} USD：系统也不问你要不要平，直接继续持有。
这些数字会随配置变，以本段显示的为准，不要引用你记忆里的旧值。
所以**你被问到，就说明这笔单已经浮盈 ≥ {TP_MIN_USD} USD**，而且还没到 {MAX_HOLD_MIN} 分钟。

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
最终盈亏：{pnl:+.2f} USD　持仓 {held_text}
开仓理由：{why_in}
平仓理由：{why_out}

你被允许回看的区间：平仓前 {before} 根 → 平仓这根 → 平仓后 {after} 根（共 {n} 根 {tf}）
序号  时间(UTC)      开      高      低      收
{bars}

只输出那个 JSON 对象（scope / theme / lesson 三个字段），复盘内容全写在 lesson 里。"""


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


# MA 的周期。2026-10-26 用户要求：用 28 根 M1 收盘均线做多空偏向
# （"当前 K 线在均线下方偏空，反之偏多"），他自己做单也看这条。
#
# 关键设计：28 根是可见窗口（14 根）的两倍，所以**必须额外取数**。
# 但模型仍然只看 14 根原始 K 线——多出来的部分只落成一个均线数字，
# 和 M5 摘要同一个原则：程序算好结论，不给原始数据。
# 这不是"放宽信息边界"，是"补一个模型自己算不出来的参照系"。
MA_PERIOD = 28


def _ma_calc(rows: list[dict], period: int = MA_PERIOD) -> dict | None:
    """MA 的数值部分。`ma_line` 和 `feature_snapshot` 共用同一套数，别各算各的。

    算不出来（数据不够 / 有脏价格）返回 None——调用方据此决定"不写这一行"，
    绝不编一个数出来。
    """
    if not rows or len(rows) < period:
        return None
    closes: list[float] = []
    for r in rows[-period:]:
        try:
            c = float(r.get("close") or 0.0)
        except (TypeError, ValueError):
            return None
        if c <= 0:
            return None
        closes.append(c)
    ma = sum(closes) / len(closes)
    last = closes[-1]
    gap = last - ma
    unit = float(_atr(rows).get("atr") or 0.0)
    ratio = (abs(gap) / unit) if unit > 0 else 0.0
    # side: +1 上方（偏多）/ -1 下方（偏空）/ 0 贴线或无法判断
    if unit > 0 and ratio < 0.5:
        side = 0
    elif gap > 0:
        side = 1
    elif gap < 0:
        side = -1
    else:
        side = 0
    return {"ma": ma, "last": last, "gap": gap, "ratio": ratio,
            "side": side, "unit": unit, "period": int(period)}


def ma_line(rows: list[dict], digits: int = 3, period: int = MA_PERIOD) -> str:
    """MA28 背景偏向，一行文字。数据不够返回空串，**不编数**。

    只用收盘价算术均值——用户自己看的就是这条，不加 EMA、不加带宽之类的花活。

    为什么必须同时给「距均线多远」：贴着均线时"在上方"和"在下方"只差几分钱，
    模型会随着价格穿越反复改方向。用单根均幅（近 14 根 ATR）归一化之后，
    "是不是真的离得远"才看得见。距离不足 0.5 倍单根均幅直接标中性。
    """
    m = _ma_calc(rows, period)
    if m is None:
        return ""
    d = max(0, int(digits))
    if m["side"] == 0:
        side = ("贴着均线 → 中性，别拿它当方向依据" if m["unit"] > 0
                else "正好压在均线上 → 中性")
    elif m["side"] > 0:
        side = "在均线上方 → 中期偏多"
    else:
        side = "在均线下方 → 中期偏空"
    rel = (f" ｜ 距均线 {m['gap']:+.{d}f}（= {m['ratio']:.1f} 倍单根均幅）"
           if m["unit"] > 0 else f" ｜ 距均线 {m['gap']:+.{d}f}")
    return (f"【背景】MA{period}（{period} 根 M1 收盘均值）= {m['ma']:.{d}f}"
            f" ｜ 当前收盘 {m['last']:.{d}f} {side}{rel}")


def _atr(rows: list[dict]) -> dict:
    """窗口内的真实波幅均值（ATR），以及归一化后的振幅。只用可见 14 根。

    为什么必须有这个（2026-10-26 加）
    ---------------------------------
    模型看到的「振幅 5.100」是绝对价格，它不知道这算大还是小。同样 5.1 USD，
    在亚洲盘午后和纽约开盘（非农）是两个完全不同的信号，但数字一模一样。
    ATR 就是这个数字的参照系——**振幅 ÷ ATR 才是无量纲、可跨时段比较的量**。

    为什么不是常见的 ATR(14)
    -------------------------
    TR 需要"前一根收盘"，所以 14 根 K 线只能产出 13 个 TR。命名上不装成
    ATR14，就叫 `atr`，并在 `n_tr` 里如实给出用了几个 TR。

    TR = max(高-低, |高-前收|, |低-前收|)
    必须带上后两项：跳空时（周开盘、数据窗）光看高低差会严重低估波动。
    """
    w = visible_bars(rows)
    if len(w) < 2:
        return {"ok": False, "atr": 0.0, "n_tr": 0, "span": 0.0, "span_atr": 0.0}
    trs = []
    for i in range(1, len(w)):
        high = float(w[i].get("high") or 0.0)
        low = float(w[i].get("low") or 0.0)
        prev_close = float(w[i - 1].get("close") or 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(trs) / len(trs) if trs else 0.0
    highs = [float(r.get("high") or 0.0) for r in w]
    lows = [float(r.get("low") or 0.0) for r in w]
    span = (max(highs) - min(lows)) if highs else 0.0
    # 比值上限约等于根数（每根都创新高/新低才能到）。分母为 0 时给 0，不编数。
    ratio = (span / atr) if atr > 0 else 0.0
    return {"ok": True, "atr": atr, "n_tr": len(trs), "span": span,
            "span_atr": ratio}


def _zone_text(pos: float, er: float, drift: float, edge: float) -> str:
    """位置判定：同一个「贴下沿」，在震荡里是买点，在下跌趋势里是弱势延续。

    为什么必须分方向（实测教训）：以前这里只输出「贴下沿（可考虑做多）」，
    完全不看趋势方向。价格从 4030 一路跌到 4021 的十几分钟里，14 根窗口
    永远处于区间下部 → 永远显示"可考虑做多"；同一时刻想做空又被
    「追破位」铁律按住。结果 18 笔里最近 4 笔全做多，而且全是在跌势里买跌。

    2026-10-25 话术降级：从「不许/唯一合法方向」改成「程序建议」。
    旧话术把模型逼成二选一：顺从时它直接复述程序结论（reason 里出现最多的
    就是"贴下沿，可考虑做多，阈值35%"这种），反抗时给极端值。实测 46% 的
    p_up 精确等于 0.0——那是输出示例的占位值被照抄，不是判断（见 clamp_p_up）。
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


def feature_snapshot(rows: list[dict], cfg: Cfg | None = None,
                     zone_edge: float | None = None,
                     mtf: dict | None = None) -> dict:
    """决策时刻的特征快照，落盘到日志行。

    为什么要存（2026-10-26 加）
    --------------------------
    这些数算出来就被扔掉了，事后想问最基础的问题——「ER 高的单是不是更容易赢」
    「振幅大的时候胜率有没有下降」——一个都答不了。原来只能靠从终端历史重建，
    而 M1 历史有限，越老的订单越必然重建失败（trade_detail 里就是这个困境：
    终端没开或者历史不够，结构就永远显示不出来）。

    为什么只存数、不存原始 14 根 K 线
    ---------------------------------
    每笔都存 14 行 OHLC 会让 open 行膨胀到 3~4 倍，而「特征值 + 结果」已经
    够做全部单变量/双变量分析。真要看 K 线原文时，终端历史还在就重建，
    不在就认了——但特征本身从此不会再丢。

    为什么读数都是 round 过的
    ------------------------
    日志是给人看也给人算的，5~6 位小数足够区分任何两个决策点，
    再多只是把文件撑大。
    """
    w = visible_bars(rows)
    s = _structure(rows)
    if not s.get("ok"):
        return {"ok": False, "n": len(w)}
    r1, r5, r13, _ = _returns(rows)
    a = _atr(rows)
    edge = float(zone_edge if zone_edge is not None
                 else (cfg.zone_edge if cfg is not None else 0.35))
    out = {
        "ok": True,
        "symbol": cfg.symbol if cfg is not None else None,
        "tf": cfg.tf if cfg is not None else "M1",
        "n": len(w),
        # 窗口的真实边界：事后核对"这笔决策时用的是哪 14 分钟"，比什么都可靠
        "win": ([int(w[0].get("time") or 0), int(w[-1].get("time") or 0)]
                if w else []),
        "er": round(s["er"], 4),
        "drift": round(s["drift"], 5),
        "pos": round(s["pos"], 4),
        "span": round(s["span"], 5),
        "streak": int(s["streak"]),
        "path": round(s["path"], 5),
        "r1": round(r1, 6),
        "r5": round(r5, 6),
        "r13": round(r13, 6),
        "atr": round(a.get("atr") or 0.0, 5),
        "span_atr": round(a.get("span_atr") or 0.0, 3),
        "regime": regime_of(rows),
        "prog_dir": _prog_direction(rows, edge),
    }
    if mtf:
        out["mtf"] = mtf
    # MA28 同时落盘：以后才能回答「均线上方的单是不是真的更容易赢」。
    # 只存数和方向标记，不存那 28 根原始 K 线（同上：特征够用，原文太胖）。
    m = _ma_calc(rows)
    if m is not None:
        out["ma28"] = round(m["ma"], 4)
        out["ma28_gap"] = round(m["gap"], 4)
        out["ma28_atr"] = round(m["ratio"], 2)
        out["ma28_side"] = m["side"]        # +1 上方 / -1 下方 / 0 贴线
    return out


def _structure_text(rows: list[dict], zone_edge: float = 0.35,
                    digits: int = 3) -> str:
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
    d = max(0, int(digits))
    # 振幅后面补一个无量纲比值：绝对价格模型没法判断大小，比值可以。
    # 拿不到 ATR 就不写这一截，不编一个数出来。
    a = _atr(rows)
    atr_text = (f"（= {a['span_atr']:.1f} 倍单根均幅 {a['atr']:.{d}f}）"
                if a.get("ok") and a.get("atr", 0) > 0 else "")
    return (f"结构 ER={s['er']:.2f}（净移动 {drift:+.{d}f}（{dir_text}）/ 总行程 {s['path']:.{d}f}）"
            f"｜振幅 {s['span']:.{d}f}{atr_text}｜当前收盘位置 {pos:.0%}（0%=区间最低 100%=最高）"
            f"｜{streak_text}｜位置判定：{zone}｜行情判定：{regime}")


def _regime_word(er: float, drift: float) -> str:
    """三态归类的**短词**版本，给多周期背景用。

    没复用 `_structure_text` 里的长文案：那一段带「→ 低买高卖」这类打法指引，
    是给 M1 主窗口的。M5 背景只该回答"大方向偏哪边"，不该再给一套打法，
    否则模型会在两套打法之间反复横跳。
    """
    if er < ER_RANGE_MAX:
        return "窄幅震荡"
    if er > ER_TREND_MIN:
        return "单边上行" if drift > 0 else "单边下行"
    return "方向不明"


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


def _fmt_bar_lines(rows: list[dict], digits: int = 3) -> str:
    """K 线表格。序号列用 T-13…T-1 / 当前，让模型一眼知道"只有这么点数据"。

    digits 必须跟着品种走：黄金 3 位、BTC 2 位。写死 3 位在 BTC 上只是多一个
    没意义的 0，但在 5 位报价的外汇上会把价格截断——那是真的误导。
    """
    lines = []
    n = len(rows)
    d = max(0, int(digits))
    for i, r in enumerate(rows):
        ts = int(r.get("time") or 0)
        clock = time.strftime("%m-%d %H:%M", time.gmtime(ts)) if ts else "--"
        tag = "当前" if i == n - 1 else f"T-{n - 1 - i:<2}"
        lines.append(
            f"{tag}  {clock}  {float(r.get('open') or 0):.{d}f}  "
            f"{float(r.get('high') or 0):.{d}f}  "
            f"{float(r.get('low') or 0):.{d}f}  "
            f"{float(r.get('close') or 0):.{d}f}"
        )
    return "\n".join(lines)


def _fmt_bars(rows: list[dict], digits: int = 3) -> str:
    """喂给决策提示词的 K 线。**永远只有 14 根**，不接受"多给一点"。"""
    return _fmt_bar_lines(visible_bars(rows), digits)


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


def _digits(symbol: str) -> int:
    """报价小数位。黄金 3 位、BTC 2 位——写死 3 位会让 80185.81 显示成
    80185.810，看着像精度过剩；更有害的是 EURUSD 5 位会被砍成 3 位。"""
    try:
        from backtest.symbols import SYMBOLS

        return int((SYMBOLS.get(symbol) or {}).get("digits") or 3)
    except Exception:  # noqa: BLE001
        return 3


def _px(symbol: str, value) -> str:
    """按品种精度格式化价格。"""
    try:
        return f"{float(value):.{_digits(symbol)}f}"
    except (TypeError, ValueError):
        return "—"


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
    return _fmt_tick_block(summarize_ticks(ticks, bars, _point(cfg.symbol)),
                           _digits(cfg.symbol))


def build_entry_prompt(rows: list[dict], bid: float, ask: float,
                       symbol: str = "XAUUSDm", tf: str = "M1",
                       point: float = 0.001, stats: dict | None = None,
                       lots: float = 0.01, lessons: str = "",
                       ticks_text: str = "", zone_edge: float = 0.35,
                       mtf_text: str = "",
                       recent_close: dict | None = None) -> str:
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
        pnl_text = f"（盈亏 {float(pnl):+.2f} USD）" if pnl is not None else ""
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
        structure=_structure_text(w, zone_edge, dg),
        r1=r1, r5=r5, r13=r13, span=_px(symbol, span),
        n_closed=n_closed, hit=hit,
        lessons=lessons or "暂无复盘笔记。",
        recent_close=rc_text,
        mtf=mtf_text or "",
        ma=(ma_text + "\n") if ma_text else "",
        ticks=ticks_text or "（tick 数据暂不可用，只能依据上面的 K 线判断）",
    )


def build_exit_prompt(rows: list[dict], position: dict, bid: float, ask: float,
                      tf: str = "M1", contract: float = 100.0,
                      ticks_text: str = "", symbol: str = "XAUUSDm",
                      lessons: str = "", zone_edge: float = 0.35,
                      mtf_text: str = "") -> str:
    w = visible_bars(rows)
    r1, r5, r13, _ = _returns(w)
    direction = int(position.get("direction") or 1)
    entry = float(position.get("price_open") or 0.0)
    lots = float(position.get("lots") or 0.0)
    cur = bid if direction > 0 else ask  # 平仓价：多头看 bid，空头看 ask
    pnl = (cur - entry) * direction * contract * lots

    held = int(time.time()) - int(position.get("opened_at") or time.time())
    held_text = f"{held // 60} 分 {held % 60} 秒" if held >= 60 else f"{max(held, 0)} 秒"
    dg = _digits(symbol)
    pnl_text = f"{pnl:+.2f} USD（{cur - entry:+.{dg}f}）"

    return EXIT_PROMPT.format(
        symbol=symbol, clock=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        direction_text="多" if direction > 0 else "空",
        lots=lots, entry=_px(symbol, entry), held_text=held_text,
        bid=_px(symbol, bid), ask=_px(symbol, ask), pnl_text=pnl_text,
        entry_reason=(position.get("entry_reason") or "未记录")[:120],
        n=len(w), tf=tf, bars=_fmt_bar_lines(w, dg),
        structure=_structure_text(w, zone_edge, dg),
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


def _sl_price_for(cfg, pos: dict) -> float:
    """这笔单子的**硬止损价**。0 表示没有止损线。

    `cfg.sl_usd` 是美元金额，必须按 `contract × lots` 换成价格距离，
    不能直接当价格用：0.01 手黄金时 1 美元价格 = 1 USD，0.05 手就是 5 USD。
    """
    if not cfg.sl_usd or float(cfg.sl_usd) <= 0:
        return 0.0
    try:
        lots = float(pos.get("lots") or 0.0)
        entry = float(pos.get("price_open") or 0.0)
        if lots <= 0 or entry <= 0:
            return 0.0
        unit = float(_symbol_contract(cfg.symbol)) * lots
        if unit <= 0:
            return 0.0
        dist = abs(float(cfg.sl_usd)) / unit
        d = 1 if int(pos.get("direction") or 1) > 0 else -1
        return entry - d * dist
    except (TypeError, ValueError):
        return 0.0


def exit_counterfact(post_bars: list[dict], direction: int, sl_price: float,
                     exit_price: float = 0.0, dd_limit: float = 0.0) -> str:
    """平仓后那几根里，「多拿一会儿」到底划不划算。返回否决理由或空串。

    两条否决线，任一命中就不该记成「止盈太早」：
      · "sl_hit" —— 平后跌回硬止损线。继续持有 = 亏 -sl_usd 出局，比落袋差。
      · "dd_risk" —— 平后回撤超过 `dd_limit`。多拿的代价已经和一次止盈同量级，
        这时候"平太早"是把运气当判断。

    为什么必须有这个对照：现在的复盘只看到"平仓后涨没涨"，涨了一根就判
    「止盈太早」。但复盘窗口是 7 根，上涨行情里任何平仓点事后看都太早。
    实测今天 4 笔盈利单平后最低价都没破止损线（于是全部"平太早"成立），
    可它们的平后回撤是 2.90 / 2.30 / 0.97 / 2.46 —— 三笔的多拿代价已经
    超过半个止盈门槛。**只看平后最高价的复盘，天生偏袒"再拿一会儿"。**

    而 227 笔真实入场点的反事实回放早就证明：所有放松出场的版本都更差
    （tp2.50 -0.141/笔、峰2.0回撤1.0 -0.114/笔）。

    `sl_price` 必须是**已换算好的价格**（见 `_sl_price_for`），不是美元。
    """
    if not post_bars:
        return ""
    try:
        d = int(direction or 1)
        lows = [float(r.get("low")) for r in post_bars if r.get("low") is not None]
        highs = [float(r.get("high")) for r in post_bars if r.get("high") is not None]
        if not lows or not highs:
            return ""
        if sl_price and float(sl_price) > 0:
            sl = float(sl_price)
            if d > 0 and min(lows) <= sl:
                return "sl_hit"
            if d < 0 and max(highs) >= sl:
                return "sl_hit"
        if exit_price and float(exit_price) > 0 and dd_limit and float(dd_limit) > 0:
            ex = float(exit_price)
            dd = (ex - min(lows)) if d > 0 else (max(highs) - ex)
            if dd >= float(dd_limit):
                return "dd_risk"
        return ""
    except (TypeError, ValueError):
        return ""


def _fmt_review_bars(window: list[dict], close_ts: int, digits: int = 3) -> str:
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
    lines = _fmt_bar_lines(window, digits).splitlines()
    lines[idx] = lines[idx] + "  ← 平仓"
    return "\n".join(lines) + ("\n（「← 平仓」之后的是平仓后的走势）"
                               if idx < len(lines) - 1 else "")


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
        why_in=(position.get("entry_reason") or "未记录")[:120],
        why_out=(position.get("exit_reason") or "外部平仓")[:120],
        before=REVIEW_BEFORE_BARS, after=after, n=len(rows), tf=tf,
        bars=_fmt_review_bars(rows, close_ts, dg),
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
        cf = exit_counterfact(post_close, int(position.get("direction") or 1),
                              _sl_price_for(cfg, position), exit_price,
                              dd_limit=float(cfg.tp_min_usd or 0.0) / 2.0)
    if verdict == VERDICT_EARLY and cf:
        verdict = VERDICT_OK
        theme = "离场正确"
        reason = "平仓后行情先回撤到止损线" if cf == "sl_hit" else "平仓后回撤已超过半个止盈门槛"
        lesson_extra = f"（反事实校正：{reason}，继续持有会更差）"
    else:
        lesson_extra = ""

    return {"ok": True, "lesson": parsed["lesson"], "theme": theme,
            "scope": parsed["scope"], "bars": len(window),
            "verdict": verdict, "counterfact": cf, "lesson_extra": lesson_extra,
            "regime": regime_of(pre_close)}


def _norm_lesson(text: str) -> str:
    """去掉数字和标点，只留汉字与字母。

    "点差 240 点" 和 "点差 250 点" 是同一类教训，不归一化就会被当成两条，
    3 个名额立刻被同义句占满。
    """
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z]", "", text or "")


# 主题枚举。新数据由复盘模型**直接给标签**（parse_review），下面那套词典只做两件事：
#   1) 老数据（没有 theme 字段）兜底归类；
#   2) 模型给的标签不在枚举里 / 给了"其他"时，再试一次关键词。
# 为什么改成模型给标签：实测 188 条老教训里 167 条（89%）落进"其他"单例桶，
# 因为模型的措辞是浮动的（"耐心等待反抽""等确认再进""方向不明确"），
# 5 个固定关键词根本追不上，次数永远涨不到 5，铁律机制整场只触发过 1 次。
LESSON_THEME_ENUM: tuple[str, ...] = (
    "方向与依据矛盾", "逆势抢反弹/接刀", "追破位/假突破", "进场时点不对",
    "止盈太早", "止损太晚", "出场时点不对", "点差/成本过高",
    "离场正确", "其他",
)

# 这条教训该注入到哪一端的提示词。注错端 = 纯噪音。
# 实测：188 条老教训里 143 条（76%）讲的是平仓时机，但它们原来被同时注入
# 进场提示词——进场决策读到"平仓别太早"，等于每轮都喂一遍无关信息。
LESSON_SCOPES = ("entry", "exit", "both")

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
    ("止盈太早", ("止盈太早", "平得太早", "平早了", "止盈过早", "卖早了", "离场太早")),
    ("止损太晚", ("止损太晚", "扛单", "死扛", "没止损", "不止损", "舍不得砍")),
    # 「出场」排在「进场时点」前面是有意的：像"等确认再平仓"这种句子，
    # 同时含进场的"确认再"和出场的"平仓"，按关键词顺序会先命中进场——
    # 实测就把一条平仓教训归成了进场教训，然后注入到进场提示词里。
    # 出场词更具体，先匹配它。
    ("出场时点不对", ("出场", "离场", "平仓", "过早", "拿太久", "拿不住", "持有太久")),
    ("进场时点不对", ("太晚", "太早", "确认再", "等回踩", "等反抽", "等确认")),
    ("点差/成本过高", ("点差", "成本", "吃掉", "吃光", "吞掉")),
]

# 推断 scope 用的词表。只用于**没有 scope 字段**的行（老数据 / 模型没答），
# 属于启发式，不是精确分类：两个词表都命中就归 "both"。
_EXIT_WORDS = ("平仓", "止盈", "止损", "离场", "持仓", "拿着", "拿住", "落袋",
               "平掉", "平得", "太早", "过早", "太晚", "持有", "走")
_ENTRY_WORDS = ("开仓", "进场", "入场", "接刀", "追多", "追空", "追破", "位置",
                "区间", "回踩", "反抽", "突破", "下沿", "上沿")


def infer_scope(text: str) -> str:
    """猜一条教训讲的是进场还是出场。两条都没提或都提到 → "both"。"""
    t = text or ""
    ex = any(k in t for k in _EXIT_WORDS)
    en = any(k in t for k in _ENTRY_WORDS)
    if ex and not en:
        return "exit"
    if en and not ex:
        return "entry"
    return "both"


def lesson_theme(text: str) -> str:
    """归一个主题；都不匹配返回空串（归到"其他"）。"""
    for name, kws in LESSON_THEMES:
        if any(k in text for k in kws):
            return name
    return ""


def row_theme(r: dict) -> str:
    """一条复盘行的主题：模型标的优先，标不了再退回关键词词典。"""
    t = r.get("theme") or ""
    if t in LESSON_THEME_ENUM and t != "其他":
        return t
    return lesson_theme(r.get("lesson") or "")


# 「出场时点不对」这个主题名是个垃圾桶：它把方向完全相反的两类教训装在一起。
# 实测 102 条铁律桶里，59 条在说「平太早」、10 条在说「平太晚」、4 条在说
# 「平得对」，剩下 64 条啥也没说清。同一个铁律标签同时告诉模型「你急着平」
# 和「你不及时平」，等于什么都没说，只剩焦虑——这是铁律机制测不出效果的主因。
#
# 所以聚类时要按**方向**把这个桶拆开，再分派回已有的真主题：
#   early → 「止盈太早」   late → 「止损太晚」   ok → 「离场正确」（不算教训）
# 复用这两个已存在的主题名而不是新造一个，是为了让老的「止盈太早」桶
# 和从垃圾桶里拆出来的同向教训**自动合并**，不再各记各的次数。
VERDICT_OK = "ok"          # 平得对：这不是教训，不该教模型任何东西
VERDICT_EARLY = "early"    # 平太早
VERDICT_LATE = "late"      # 平太晚 / 没及时

# 顺序有意义：先判「平得对」，再判早/晚。
# 「平得对：离场后先破位下探」这种句子同时含"下探"和"对"，先判 ok 才不会误判；
# 而「平得对，但下次应该多拿一根」这种句子两个信号都有，按"明确表态优先"归 ok——
# 模型自己都说这笔平得对了，再拿它去教「别急着平」是反着用。
_VERDICT_OK_WORDS = (
    "平得对", "平仓对", "离场对", "砍得对", "砍对", "平得没错", "平仓没错",
    "离场没错", "止损没错", "止盈没错", "平掉没错", "该走", "平得正确",
)
_VERDICT_EARLY_WORDS = (
    "太早", "过早", "早了", "多拿", "再拿", "续持", "拿住", "持有更久",
    "拿多一", "别急着落袋", "别急平", "不该平",
)
_VERDICT_LATE_WORDS = (
    "太晚", "未及时", "没及时", "扛单", "死扛", "舍不得砍", "拖到",
    "拖延", "该更早平", "砍得太慢", "平得太慢", "止损太晚",
)


def exit_verdict(text: str) -> str:
    """一条出场教训的方向：ok / early / late，判不出来返回空串。

    只用于出场那几类主题（「出场时点不对」「止盈太早」「止损太晚」）。
    判不出来不要硬猜——猜错方向比不分类更坏，等于把"平得对"喂成"平太早"。
    """
    t = text or ""
    if any(k in t for k in _VERDICT_OK_WORDS):
        return VERDICT_OK
    if any(k in t for k in _VERDICT_LATE_WORDS):
        return VERDICT_LATE
    if any(k in t for k in _VERDICT_EARLY_WORDS):
        return VERDICT_EARLY
    return ""


def verdict_for(theme: str, lesson: str) -> str:
    """综合「模型给的主题标签」和「教训正文」判出场方向。

    **必须先看标签再看正文**：实测 4 条「止盈太早」里有 2 条正文写的是
    "贴上沿就慌着离场""遇 ER 回落就急平"，一个"太早"都没有——只靠关键词
    会漏掉一半。模型已经把主题标成「止盈太早」了，那就是它的方向表态。
    正文优先只在它给出更强的反向信号时才生效（比如标签「出场时点不对」、
    正文写"平得对"）。
    """
    v = exit_verdict(lesson)
    if v:
        return v
    if theme == "止盈太早":
        return VERDICT_EARLY
    if theme == "止损太晚":
        return VERDICT_LATE
    return ""


def row_verdict(r: dict) -> str:
    """一条复盘行的出场方向。

    落盘时算过就信落盘值（含反事实推翻的结果），没有才现推。
    老数据没有这个字段，靠主题标签 + 正文推断。
    """
    v = r.get("verdict") or ""
    if v in (VERDICT_OK, VERDICT_EARLY, VERDICT_LATE):
        return v
    return verdict_for(r.get("theme") or "", r.get("lesson") or "")


def row_scope(r: dict) -> str:
    """一条复盘行该注入哪一端。字段缺失（老数据）→ 关键词推断，不丢数据。"""
    s = r.get("scope") or ""
    if s in LESSON_SCOPES:
        return s
    return infer_scope(r.get("lesson") or "")


def _refine_exit_theme(name: str, verdict: str) -> str:
    """把出场类主题按方向分派到真主题，见上面 VERDICT_* 那段说明。

    只在出场三兄弟之间搬运，别的主题原样放回。
    """
    if name not in ("出场时点不对", "止盈太早", "止损太晚"):
        return name
    if verdict == VERDICT_OK:
        return "离场正确"
    if verdict == VERDICT_LATE:
        return "止损太晚"
    if verdict == VERDICT_EARLY:
        return "止盈太早"
    return name                      # 判不出方向就不搬，宁可留在原桶


def effective_theme(r: dict) -> str:
    """一条复盘行**最终生效**的主题。

    外部（面板统计、`tools/lesson_stats.py`、注入）都必须走这一格，
    否则报表按「出场时点不对」算、注入按拆开后的「止盈太早」算，
    两边看到一个库却报出两套数字。
    """
    return _refine_exit_theme(row_theme(r), row_verdict(r))


def lesson_clusters(login=None) -> list[dict]:
    """把复盘教训按（主题 × 端）归档并计次，按出现次数排序。

    数据源是 `lesson_rows()`（独立教训库、全量），**不再受 400 行窗口限制**。

    **为什么桶要带上"端"**：只按主题分桶时，一个桶里既有进场教训也有出场教训，
    注入哪一端都会把另一端的代表句带过去——实测第一次分域后，"平仓太早"
    仍然出现在进场提示词里，因为同主题里混着 scope=both 的行。
    按 (主题, 端) 分桶后，进场的桶只装进场的行，代表句不会串味；
    scope=both 的桶按定义两端都该看，它两头都进。

    每个桶额外记 `regimes`：这条教训分别是在哪种行情状态下犯的（""=旧数据没标签）。
    """
    rows = lesson_rows(login)

    buckets: dict[str, dict] = {}
    for r in rows:
        text = r.get("lesson") or ""
        if not text:
            continue
        ts = int(r.get("ts") or 0)
        name = effective_theme(r)     # 已含出场三兄弟的方向重分派
        sc = row_scope(r)
        key = (name or ("其他:" + _norm_lesson(text)[:24])) + "|" + sc
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"name": name, "domain": sc, "count": 0,
                                "lesson": text, "ts": ts, "regimes": {},
                                "verified": 0}
        b["count"] += 1
        # verified = 这条桶里有几条**过了反事实检验**。
        # 只认落盘的 verdict 字段，不认 row_verdict 的文本推断——
        # 推断出来的方向没有对照行情，不能当"已验证"。
        if r.get("verdict"):
            b["verified"] += 1
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


# 桶的 domain 在提示词里的叫法。不写这一格，模型看不出这条是进场教训还是出场教训。
_DOMAIN_TEXT = {"entry": "进场端", "exit": "出场端", "both": "两端"}

# 主题自带的端属性。有些主题本身就是"某一端"的错误（追破位只可能是进场错、
# 止盈太早只可能是出场错），这时标签里的 scope 说成"两端"也不能信——
# 实测出现过「出场时点不对 · 两端 · 已犯 35 次」被塞进进场提示词。
# 两道闸取交集：主题端 + 标签端都要过。
THEME_DOMAIN = {
    "追破位/假突破": "entry",
    "逆势抢反弹/接刀": "entry",
    "进场时点不对": "entry",
    "止盈太早": "exit",
    "止损太晚": "exit",
    "出场时点不对": "exit",
    "离场正确": "exit",          # 不注入，但归类/统计时按出场算
}


def _scope_ok(c: dict, scope: str) -> bool:
    """这个桶能不能注入到 `scope` 这一端。

    `scope=""` = 不分域（老调用方、汇总渲染）。
    """
    if not scope:
        return True
    td = THEME_DOMAIN.get(c.get("name") or "", "")
    if td and td != scope:        # 主题本身就是另一端的错，直接拦掉
        return False
    return c.get("domain") in ("both", scope)


# 出现次数达到这个值的主题升级为「铁律」：从"参考教训"变成"先自查再动手"的禁令。
# 定 5 的依据：实测 16 笔里「追破位」犯了 7 次还在犯——到 5 次说明提示词里
# 普通一行字的剂量已经压不住这个错误，必须换更重的说法。
HARD_RULE_MIN = 5


def _can_be_hard(c: dict) -> bool:
    """这个桶配不配升级成「铁律」。

    规则：出场类的时机判断（止盈太早 / 止损太晚 / 出场时点不对）必须有
    **至少一条过了反事实检验**才能当铁律。

    理由：判断"该不该早平"必须知道**不平仓的结局**。现在的复盘只看到
    平仓后涨没涨，这是单边证据——只要平后有一根更高就叫"太早"，永远成立。
    结果是库攒出 67 条「止盈太早」、48 条「出场时点不对」，而 227 笔真实
    入场点的反事实回放早就证明：所有放松出场的版本都比现在更差
    （tp2.50 -0.141/笔、峰2.0回撤1.0 -0.114/笔）。

    历史数据一条反事实记录都没有，所以这些桶只能降级为普通教训
    （还留在库里、还能被看见，但不再以"已犯 N 次"的铁律口吻下命令）。
    新数据带上 verdict 之后才重新有资格。
    """
    if c["count"] < HARD_RULE_MIN:
        return False
    if THEME_DOMAIN.get(c.get("name") or "") == "exit" and not c.get("verified"):
        return False
    return True


# 只记录、绝不注入的主题。放在模块级是因为 lesson_stats / 面板也要能读到。
_NO_INJECT_THEMES = ("离场正确", "出场时点不对")


# 教训文本是模型自己写的。它写"别接刀"时心里想的是"别逆势做多"，
# 但下一轮读回去会泛化成"别在下跌里动手"，把顺势做空一起禁掉。
# 实测 10-26：p_up<0.5 的 38 次机会一次空单都没开，reason 反复出现
# "若在下跌途中接多，就是重复此前 8 次的错误"——它把禁令扩大解释了。
# 所以按主题补一句边界，把"禁什么"钉死在字面上。
_THEME_TAIL = {
    "逆势抢反弹/接刀":
        "→ 这条只禁**逆势做多**（下跌途中接多）。下跌趋势里**顺势做空不算接刀**；"
        "震荡区间内贴下沿做多也不算。",
    "追破位/假突破":
        "→ 这条禁的是**顺着破位方向追进去**。震荡市贴着上沿/下沿的正常进出不算追破位。",
}


def _lesson_tail(name: str) -> str:
    return _THEME_TAIL.get(name or "", "")


def recent_lessons(login=None, n: int = 3, ignore_cost: bool = True,
                   regime: str = "", scope: str = "") -> str:
    """取最反复出现的几条教训，塞进提示词。

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

    `scope` 是这一端（"entry"/"exit"），**硬过滤**，和 regime 的软匹配相反：
    实测 76% 的教训讲的是平仓时机，把它们注进进场提示词是纯噪音，
    宁可这一端暂时没经验可用，也不要错位。`scope=""` = 不分域（老调用方）。

    count >= HARD_RULE_MIN 的主题升级为「铁律」，单独成块放最前面，
    不占 n 个普通名额。
    """
    clusters = lesson_clusters(login)
    # 两个主题只记录、不注入：
    #   「离场正确」——记的是"这笔平得对"，教不了任何东西；
    #   「出场时点不对」——按定义就是**判不出方向**的那一批（说得清是早是晚的
    #     都会被 _refine_exit_theme 分派走）。一条说不清"早了还是晚了"的教训
    #     注入进去只剩焦虑，实测这桶的代表句是"未达做多阈值且浮亏平仓"，
    #     连进场味都混进来了。留在库里是为了让方向分布看得见。
    clusters = [c for c in clusters
                if (c.get("name") or "") not in _NO_INJECT_THEMES]
    if ignore_cost:
        clusters = [c for c in clusters
                    if not any(k in (c.get("name") or "") for k in ("点差", "成本"))]
    if scope:
        clusters = [c for c in clusters if _scope_ok(c, scope)]
    if not clusters:
        return "暂无复盘笔记。"

    if regime:
        # 同状态优先的两趟稳定排序：先按总次数，再按当前状态次数。
        # 没标签（""）的旧教训 regimes 里没有这个键，按 0 计、自然靠后，
        # 但仍会因为总次数高而补位进来——这就是软匹配。
        clusters = sorted(clusters, key=lambda c: c["count"], reverse=True)
        clusters.sort(key=lambda c: c["regimes"].get(regime, 0), reverse=True)

    hard = [c for c in clusters if _can_be_hard(c)]
    # 普通教训保持 clusters 的原有顺序（同状态优先 → 总次数），
    # 不要在这里再按 verified 重排：那会把 regime 软匹配压掉，
    # 实测会让"本状态犯过 1 次"输给"别的状态犯过 3 次"。
    soft = [c for c in clusters if not _can_be_hard(c)][:n]

    blocks = []
    if hard:
        lines = []
        for c in hard:
            name = c["name"] or "反复犯的错误"
            dom = _DOMAIN_TEXT.get(c.get("domain") or "", "")
            tail = f" · {dom}" if dom else ""
            line = f"- 【铁律 · 已犯 {c['count']} 次{tail}】{name}：{c['lesson']}"
            lt = _lesson_tail(c.get("name"))
            if lt:
                line += f"\n  {lt}"
            lines.append(line)
        blocks.append("铁律（以下错误你已各犯多次，先自查这一笔是不是又要犯，"
                      "是就直接不做/立刻平，再考虑别的）：\n"
                      # 教训文本是模型自己写的，天然偏空（空单亏得多），
                      # 不加这句它会把"别追破位/别接刀"读成"只能做多"。
                      "（这些禁令对多空同等生效。不要把「不追空」读成「只能做多」，"
                      "也不要把「不接多」读成「只能做空」。）\n"
                      # 实测：只写上一条不够。模型会直接引用"不追破位铁律"否掉
                      # 震荡市贴上沿的做空，理由是"贴着上沿做空位置已过"。
                      # 铁律是历史情绪的沉淀，不能让它推翻当前的打法判定。
                      "（**铁律不许推翻上面的打法规则**：震荡市贴边进出（下沿买、上沿卖）"
                      "既不是追破位也不是接刀；下跌趋势里顺势做空同样不是接刀。"
                      "两者冲突时，以【先判行情，再选打法】和【贴边怎么处理】为准。）\n"
                      + "\n".join(lines))

    if soft:
        lines = []
        for c in soft:
            same = c["regimes"].get(regime, 0) if regime else 0
            tag = f"，本状态 {same} 次" if same else ""
            dom = _DOMAIN_TEXT.get(c.get("domain") or "", "")
            dtag = f" · {dom}" if dom else ""
            # 出场类没有反事实记录的，明说一句：这条只有单边证据，
            # 不能当"以后都别急着平"的硬结论用。
            if THEME_DOMAIN.get(c.get("name") or "") == "exit" and not c.get("verified"):
                dtag += " · 未过反事实检验，别当硬结论"
            if c["count"] >= 2:
                head = (f"[{c['name']} · 已出现 {c['count']} 次{dtag}{tag}]" if c["name"]
                        else f"[已出现 {c['count']} 次{dtag}{tag}]")
            else:
                head = (f"[{c['name']}{dtag} · 只出现 1 次，证据弱]" if c["name"]
                        else f"[只出现 1 次，证据弱{dtag}]")
            line = f"- {head} {c['lesson']}"
            lt = _lesson_tail(c.get("name"))
            if lt:
                line += f"\n  {lt}"
            lines.append(line)
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
            "closed_at": closed_at,
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
              recent_close: dict | None = None) -> dict:
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
                                recent_close=recent_close)
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
                               mtf_text=mtf_text)
    held = int(time.time()) - int(position.get("opened_at") or time.time())
    try:
        text, usage = _chat(prompt, system_for(EXIT_SYSTEM, cfg))
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

# 允许上真单的品种。不直接用 SYMBOLS.keys()：那张表是回测用的全量清单，
# 里面的外汇/指数没在实盘验证过——实盘白名单必须显式列出来，加一个要想一次。
# 每个品种的 point/contract/spread/atr 都在 backtest/symbols.py 里带实测来源。
TRADABLE_SYMBOLS = ("XAUUSDm", "BTCUSDm")

# 给面板用的品种展示名（下拉框文案）。键必须都在 TRADABLE_SYMBOLS 里。
SYMBOL_LABELS = {
    "XAUUSDm": "黄金 XAUUSD",
    "BTCUSDm": "比特币 BTCUSD",
}


def _symbol_label(symbol: str) -> str:
    """system prompt 里自我介绍用的品种名。

    为什么必须动态（2026-10-26 修）：三个 system prompt 原来全部硬编码
    「XAUUSD」。上一轮加 BTC 时改了精度、白名单、成本提示，唯独漏了这里——
    真选 BTC 跑起来，模型会被告知"你是黄金交易员"同时收到 80000 的价格。
    这比没验证更糟：它会把黄金的波动直觉套到 BTC 上。
    """
    name = SYMBOL_LABELS.get(symbol)
    return name or symbol


def system_for(template: str, cfg: Cfg) -> str:
    """把 system 模板里的占位符换成**当前配置的实际值**。

    除了品种名，出场阈值也必须换：`sl_usd` / `tp_min_usd` / `max_hold_sec`
    都是可配的，而 system 里原来硬编码着 3.0 / 3.25 / 60 分钟。cfg 一改，
    system 就在骗模型——它会以 3.0 为前提判断"系统什么时候会砍我"，
    实际却是 5.0。这类"界面说一套、代码做一套"的错位最难查，因为不报错。

    用 str.replace 而不是 str.format：system 里含 JSON 示例的花括号，
    format 会把它当占位符直接炸。
    """
    out = template.replace("{SYMBOL_LABEL}", _symbol_label(cfg.symbol))
    out = out.replace("{SL_USD}", f"{float(cfg.sl_usd):g}")
    out = out.replace("{TP_MIN_USD}", f"{float(cfg.tp_min_usd):g}")
    out = out.replace("{MAX_HOLD_MIN}", f"{int(cfg.max_hold_sec) // 60}")
    return out


def symbol_options() -> list[dict]:
    """面板下拉框的数据源：只给白名单，附 point/contract 供前端显示成本。"""
    from backtest.symbols import SYMBOLS

    out = []
    for s in TRADABLE_SYMBOLS:
        spec = SYMBOLS.get(s) or {}
        out.append({
            "symbol": s,
            "label": SYMBOL_LABELS.get(s, s),
            "point": spec.get("point"),
            "contract": spec.get("contract"),
            "spread": spec.get("spread"),
            "digits": spec.get("digits"),
        })
    return out


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
        state["last_close_pnl"] = round(float(pnl), 2)
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

        # 峰值浮盈：这一笔从开仓到现在，浮盈最高摸到过多少（USD）。
        #
        # 存在的唯一目的是**用实测数据回答「3.25 到底几次能到」**。决策轮的
        # 粒度是 60 秒，漏掉的冲高回落由快查循环（_wait_next）按 2~5 秒补上；
        # 两处共用 _track_peak，峰值逻辑同源。
        _track_peak(pos, float_pnl, now)

        gate = _exit_gate(cfg, held_sec, float_pnl)

        # 闸门说不平：直接记 hold，不调模型（省 token，也断了"每轮问一次要不要跑"）
        if gate is not None and not gate.get("close"):
            row = {"ts": now, "kind": "hold", "reason": gate["reason"],
                   "source": gate["source"], "held_sec": held_sec,
                   "float_pnl": round(float_pnl, 2), "peak_pnl": pos["peak_pnl"],
                   "peak_ts": pos.get("peak_ts")}
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
            mtf = _mtf_bundle(cfg.symbol, cfg.zone_edge, _digits(cfg.symbol))
            decision = ask_exit(bars, pos, bid, ask, cfg, contract, login,
                                ticks_text=_ticks_text(cfg, bars),
                                mtf_text=mtf["text"])
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
        state["last_close_pnl"] = round(float(pnl), 2)
        state["position"] = None
        row = {
            "ts": now, "kind": "close", "ticket": pos.get("ticket"),
            "direction": pos.get("direction"), "pnl": round(pnl, 2),
            "p_up": pos.get("p_up"), "hit": pnl > 0,
            "held_sec": held_sec,
            "peak_pnl": pos.get("peak_pnl"),
            "peak_ts": pos.get("peak_ts"),
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
    decision = ask_entry(bars, bid, ask, cfg, state, login,
                         ticks_text=_ticks_text(cfg, bars),
                         mtf_text=mtf["text"],
                         recent_close=recent_close)
    # 特征快照：决策当下就走一次，后面所有分支（skip / open / failed）都带上。
    # 不存它的话，事后想问「ER 高的单更容易赢吗」只能靠重建，而重建必然失败。
    feat = feature_snapshot(bars, cfg, mtf=mtf["info"])
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
               "prog_dir": prog_dir, "feat": feat,
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
        row = {"ts": now, "kind": "skip", "p_up": p_up, "direction": direction,
               "prog_dir": prog_dir, "agree": agree, "feat": feat,
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
        "feat": feat,
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
      · 最大单笔 -6.08 是平均亏损的 5.9 倍，且它的 p_up=0.0（示例占位值被照抄，
        又被 confidence 记成 1.0 的"最自信"，实际是没做判断）。
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
    # 品种必须在白名单里：SYMBOLS 里没测过 point/contract/spread 的品种，
    # 点差成本会退回 0（把「没测过」显示成「零成本」），风险直接失控。
    if cfg.symbol not in TRADABLE_SYMBOLS:
        issues.append(
            f"品种 {cfg.symbol} 不在可交易白名单（{'/'.join(TRADABLE_SYMBOLS)}）："
            "没有实测 point/contract/spread 的品种不能上真单"
        )
    # 3 秒是物理下限：一次模型调用实测 ~2.6 秒，再短线程就在堆请求，
    # 上一轮没回来下一轮已经开始，决策全挤在一起。
    if cfg.interval_sec < 3:
        issues.append(
            f"决策间隔不能小于 3 秒（模型单次调用约 2.6 秒，再快只会堆积请求）"
        )
    if cfg.lots <= 0:
        issues.append("手数必须大于 0")
    # 冷却不再有「必须 <60 秒」的上限——「平仓后 1 分钟内必须重新开仓」的硬需求
    # 已删除（它和复盘体系的报复单标记直接矛盾，且是输出量要求不是质量要求）。
    # 冷却只是防连环单的下限，用户想配 2 分钟冷却应该允许。
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
    # 止损快查的间隔只有下限有意义：< 2 秒时 MT5 tick 读取的抖动开始比价格
    # 变化本身大，白白空转。**上界不在这里拦**——配得比决策间隔大也不报错，
    # _wait_next 会把它夹到 interval_sec（等于没开）。理由：这是个纯粹的
    # 安全网参数，配歪了最坏结果是"回到旧行为"，不该让人启动不了。
    # 0 是合法的「关掉」。
    if getattr(cfg, "guard_sec", 0.0) and cfg.guard_sec > 0 and cfg.guard_sec < 2:
        issues.append(
            f"止损快查间隔 {cfg.guard_sec:.1f} 秒太短（tick 读取抖动比价差还大），最小 2 秒"
        )
    # 近门槛档位同样只有下限有意义，且不可能比远档还粗——粗的那档就该叫远档。
    if getattr(cfg, "guard_near_sec", 0.0) and cfg.guard_near_sec > 0 and cfg.guard_near_sec < 2:
        issues.append(
            f"近门槛快查间隔 {cfg.guard_near_sec:.1f} 秒太短（同 guard_sec 口径），最小 2 秒"
        )
    if getattr(cfg, "guard_sec", 0.0) and getattr(cfg, "guard_near_sec", 0.0) \
            and cfg.guard_near_sec > cfg.guard_sec:
        issues.append(
            f"近门槛快查间隔 {cfg.guard_near_sec:.1f} 秒不能比远档 {cfg.guard_sec:.1f} 秒还粗"
        )
    return issues


def next_wait_sec(row: dict, cfg: Cfg) -> float:
    """这一轮跑完，下一次决策该等多久。

    平仓后**在冷却结束点提前唤醒一次**（cooldown+1 秒，默认约 31 秒）：
    给模型一次决策机会——刚平仓时的市场状态往往紧接着就有结构变化，
    早看一眼不亏。但**开不开是模型的事**：教训库的「报复单」教训和
    提示词里的【刚平仓提醒】会约束它，act=false 是正常输出。

    以前这里的理由是「1 分钟内必须重新开仓」——那条硬需求已删
    （和复盘体系的报复单标记直接矛盾）。唤醒保留，语义从「必须快开」
    变成「给一次机会」。
    """
    if row.get("kind") == "close":
        return max(1.0, min(float(cfg.cooldown_sec + 1), float(cfg.interval_sec)))
    return float(cfg.interval_sec)


def _track_peak(pos: dict, float_pnl: float, ts: int) -> bool:
    """更新峰值浮盈。返回 True = 峰值被抬高（调用方据此决定是否写盘）。

    peak_pnl 的唯一用途：**用实测数据回答「3.25 到底几次能到」**。除了值还记
    `peak_ts`（峰值出现的时刻）——「MFE 通常在持仓第几秒」对 max_hold_sec 的
    分析和 peak_pnl 本身一样值钱，而且顺手就能记，零成本。

    快查循环（_wait_next）和决策轮（tick）都调这一个函数，**峰值逻辑同源**，
    不存在两份实现各记各的。

    注意别写成 `pos.get("peak_pnl") or float_pnl`：peak_pnl 恰好是 0.0 时
    `or` 会退回 float_pnl，把已经摸到的正峰值抹掉——所以必须判 None。
    """
    prev = pos.get("peak_pnl")
    if prev is not None and float_pnl <= float(prev):
        return False
    pos["peak_pnl"] = round(float_pnl, 2)
    pos["peak_ts"] = int(ts)
    return True


def _guard_hit(cfg: Cfg, pos: dict, bid: float, ask: float,
               fp: float | None = None) -> str:
    """持仓期间的硬止损快查。返回 '' = 没触发。

    **只看 sl_usd 一条**，故意不看止盈门槛、也不看持仓上限：
      · 止盈门槛一旦到线就该让模型说话，那是 tick() 里 _exit_gate 的活；
      · 持仓上限是分钟级的粗规则，拿 5 秒的节拍去看它是浪费。

    这里是纯读，不下单、不写流水、不调模型。开销就是一次 symbol_info_tick。
    `fp` 允许调用方传入已算好的浮盈（快查循环里峰值更新和止损判定共用一次
    _realized_pnl，不重复算）；不传就自己算。
    """
    if cfg.sl_usd <= 0:
        return ""
    if fp is None:
        fp = _realized_pnl(cfg, pos, bid, ask)
    if fp <= -abs(cfg.sl_usd):
        return f"浮亏 {fp:+.2f} 触及 -{abs(cfg.sl_usd):.2f}"
    return ""


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

    快查每格做三件事（一次报价、一次浮盈，全部复用）：
      1. **峰值更新**（_track_peak）：只在峰值被抬高时写一次 state——峰值只增
         不减，一笔持仓真正抬高的次数有限，无条件每格写盘不可接受。
      2. **止损判定**（_guard_hit）：浮亏过线提前返回。
      3. **tp 穿越唤醒**（cfg.tp_wake）：浮盈首次 ≥ tp_min_usd 也提前返回。
         下一轮 tick() 的 _exit_gate 会因为到线返回 None 而把决定交给模型——
         参数没变，变的只是模型被问到的延迟（最坏 60 秒 → ≤2 秒）。

    步长是**自适应**的：fp 离止盈门槛远时按 guard_sec 粗看，进入
    [tp_min - tp_wake_band, ∞) 后切 guard_near_sec 细看。采样密度花在决策
    边界附近——fp 在 0 附近时细看是浪费，逼近 3.25 时粗看会错过穿越。

    返回 True = 收到停止信号（只有面板线程会传 stop_wait，CLI 永远是 False）。

    为什么是「提前返回」而不是「在这里平仓」：平仓要走 tick() 里的
    _exit_gate → send_close → append_log → queue_review 整条链。在这里复制一份
    就等于给同一件事留两个实现，迟早对不上账。这里只负责**把最坏 60 秒的
    无人看守缩到一格**，剩下的原样交给下一轮。
    """
    deadline = started + next_wait_sec(row, cfg)
    # 夹一道：配得比决策间隔大就等于没开，不如直接退回旧行为，别让短周期
    # 配置（interval_sec=3）里一个默认的 5 秒快查把节奏搅乱。
    guard = min(float(getattr(cfg, "guard_sec", 0.0) or 0.0), float(cfg.interval_sec))
    near = min(float(getattr(cfg, "guard_near_sec", 0.0) or 0.0), guard)
    band = float(getattr(cfg, "tp_wake_band", 0.0) or 0.0)
    tp = float(getattr(cfg, "tp_min_usd", 0.0) or 0.0)
    wake = bool(getattr(cfg, "tp_wake", False)) and tp > 0

    # 无仓 / 平仓轮 / 刚停：不需要快查，睡满一整轮（也省掉每格的报价读取）
    pos = load_state(login).get("position") if guard > 0 else None
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
        if _track_peak(pos, fp, int(time.time())):
            # 重读再写：state 里可能还有面板写的其他字段，别拿旧快照整体覆盖。
            # 外部平仓竞态无害：pos 还是旧引用，峰值照记，下一轮 tick()
            # 检测外部平仓时会整体覆盖处理。
            fresh = load_state(login)
            fresh["position"] = pos
            save_state(fresh, login)
        if _guard_hit(cfg, pos, q[0], q[1], fp=fp):
            return False       # 剩下的时间不要了，立刻进下一轮去平仓
        if wake and fp >= tp:
            return False       # tp 穿越唤醒：交模型决定，不等满一轮
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
    print(f"AI 交易员启动 | {cfg.symbol} 超短线 {cfg.lots} 手 | 每 {cfg.interval_sec} 秒决策 | "
          f"平仓后 {cfg.cooldown_sec} 秒冷却 | 不挂 SL/TP | {mode}")
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
            state["last_close_pnl"] = round(float(pnl), 2)
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
            _wait_next(started, row, cfg, login, stop_wait=self._stop.wait)

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


def reset_lessons(login: int | str | None = None) -> dict:
    """只清经验：教训库里的一条不留，战绩（流水/state）原样保留。

    什么时候用：旧教训本身就是错的（比如某条铁律是拿坏数据攒的），
    或者换了打法、旧经验成了包袱。和「重置」分开，是因为这两件事的代价
    完全不对称——清战绩只损失统计样本，清经验丢掉的是模型唯一的历史。
    """
    p = lessons_path(login)
    if not p.exists():
        return {"ok": True, "cleared": 0, "archived": None,
                "note": "教训库本来就不存在"}
    rows = _read_jsonl(p)
    if not rows:
        return {"ok": True, "cleared": 0, "archived": None, "note": "教训库已经是空的"}
    dst = p.with_name(f"{p.stem}.{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}")
    try:
        p.replace(dst)
    except OSError as exc:
        return {"ok": False, "error": f"归档失败：{exc}。没有删除任何东西。"}
    p.write_text("", encoding="utf-8")     # 空文件 = 已清空，禁止回退读归档
    return {"ok": True, "cleared": len(rows), "archived": dst.name}


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
    # 旧 state 没有这个字段时回退到**当前**默认（默认真实），
    # 写死 True 会让面板显示"真实"而实际按纸面跑，是最坏的那种谎。
    dry_run = bool(saved_cfg.get("dry_run")) if "dry_run" in saved_cfg else Cfg.dry_run
    pending = state.get("pending_reviews") or []
    # 旧 state 里没有新加的字段，缺的时候要回退到当前默认值
    _defaults = Cfg()

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
        # 新出场规则的阈值，面板上要能看见，否则用户不知道规则已经变了。
        # 旧 state 里的 cfg 没有这两个字段（那会儿还不存在），
        # 所以缺了就回退到 Cfg 的当前默认值，而不是显示 None。
        "sl_usd": saved_cfg.get("sl_usd", _defaults.sl_usd),
        "tp_min_usd": saved_cfg.get("tp_min_usd", _defaults.tp_min_usd),
        "max_hold_sec": saved_cfg.get("max_hold_sec", _defaults.max_hold_sec),
        # 止损快查间隔。0 = 关（每 interval_sec 才看一次止损）。面板要显示，
        # 否则用户没法判断「止损会不会超调」这件事现在到底有没有生效。
        "guard_sec": saved_cfg.get("guard_sec", _defaults.guard_sec),
        # 自适应近档 + tp 穿越唤醒。同理：面板不显示，用户就不知道
        # 「浮盈到 3.25 会被 2 秒内交模型」这件事开着还是关着。
        "guard_near_sec": saved_cfg.get("guard_near_sec", _defaults.guard_near_sec),
        "tp_wake_band": saved_cfg.get("tp_wake_band", _defaults.tp_wake_band),
        "tp_wake": saved_cfg.get("tp_wake", _defaults.tp_wake),
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
                cfg.zone_edge)
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
    parser.add_argument("--lots", type=float, default=0.01)
    parser.add_argument("--interval", type=int, default=Cfg.interval_sec,
                        help="决策间隔秒数（最小 3，模型单次约 2.6 秒）")
    parser.add_argument("--cooldown", type=int, default=30, help="平仓后冷却秒数")
    parser.add_argument("--max-trades", type=int, default=0, help="开仓笔数上限，0=不限")
    parser.add_argument("--max-loss", type=float, default=0.0, help="累计亏损上限 USD，0=不限")
    parser.add_argument("--max-minutes", type=int, default=0, help="运行时长上限分钟，0=不限")
    # 用户 10-26 要求默认真实下单，所以这个开关反过来了：
    # 以前要 --live 才发真单，现在要 --paper 才不发单。
    parser.add_argument("--paper", action="store_true", help="只记账不发单（纸面模式）")
    parser.add_argument("--status", action="store_true", help="只看状态，不跑")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    args = parser.parse_args()

    cfg = Cfg(
        symbol=args.symbol, lots=args.lots, interval_sec=args.interval,
        cooldown_sec=args.cooldown, dry_run=args.paper,
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
