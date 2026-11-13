"""教训库：存储、归类、分域、注入。

这个模块只管一件事——**模型从自己交易里总结出的经验**，从落盘到回灌提示词
的全部逻辑。它不判断行情、不下单、不读账户状态。

分界（这是第 3 步拆分的核心判断）
--------------------------------
搬进来的：教训库自己的文件读写（`lessons.jsonl` 是它的数据）、主题词典、
`scope`/`verdict` 推断、反事实闸门（`exit_counterfact`）、聚类与注入文本。

留在 `autotrade` 的：
  · `build_review_prompt` / `ask_review`——要读 M5 行情（`_bars`）、调模型
    （`_chat`）、写调用流水（`_log_call`），是 I/O 层不是知识层；
  · `_do_review` / `queue_review` / `process_due_reviews`——编排层，写运行时
    流水和 `autotrade_state.json`。

依赖方向：`autotrade` → `lessons` → {`jsonl`, `ta`, `paths`}，单向无环。
`lesson_packs` 现在只依赖 `live.jsonl`，不再借 `autotrade._unwrap_lesson`。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from live import paths
from live.jsonl import read_jsonl, unwrap_lesson
from live.ta import _JSON_RE, _fmt_bar_lines


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


def append_lesson(row: dict, login: int | str | None = None) -> None:
    p = lessons_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


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
        return read_jsonl(p)
    return []


def migrate_lessons(login: int | str | None = None) -> dict:
    """一次性把历史流水里的复盘搬进 `lessons.jsonl`（含归档文件）。

    只在升级时跑一次。dedupe 用 (ts, ticket, lesson)：重复跑不会翻倍。
    老数据没有 theme/scope，用词典 + 关键词推断，**推断结果如实标注**
    (source="migrated")，不假装是模型自己标的。
    """
    p = lessons_path(login)
    if p.exists() and read_jsonl(p):
        return {"ok": False, "error": "lessons.jsonl 已有内容，不覆盖"}
    acc = paths.account_dir(paths.resolve_login(login))
    seen: set[tuple] = set()
    rows: list[dict] = []
    for f in sorted(acc.glob("autotrade_log*.jsonl")):
        for r in read_jsonl(f):
            if r.get("kind") != "review" or not r.get("lesson"):
                continue
            key = (r.get("ts"), r.get("ticket"), r["lesson"])
            if key in seen:
                continue
            seen.add(key)
            lesson = unwrap_lesson(r["lesson"])
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
    rows = read_jsonl(p)
    if not rows:
        return {"ok": True, "cleared": 0, "archived": None, "note": "教训库已经是空的"}
    dst = p.with_name(f"{p.stem}.{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}")
    try:
        p.replace(dst)
    except OSError as exc:
        return {"ok": False, "error": f"归档失败：{exc}。没有删除任何东西。"}
    p.write_text("", encoding="utf-8")     # 空文件 = 已清空，禁止回退读归档
    return {"ok": True, "cleared": len(rows), "archived": dst.name}


# 复盘窗口：**平仓前 7 根 + 平仓这根 + 平仓后 7 根 = 15 根**（用户指定）。
# 为什么要等平仓后 7 根走完才复盘：刚平掉那一刻还没有"平仓后走势"，
# 只能得出"赚了/亏了"；等 7 根之后才看得出「平太早」还是「跑得及时」。
REVIEW_BEFORE_BARS = 7
REVIEW_AFTER_BARS = 7


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


# 出场**不是模型决定**的那几种 source：程序硬止损（sl）、券商端保本止损（be）、
# 程序代平保本（be_local）、券商端硬止盈（tp，2026-11-11 起只给非趋势单）。
# 它们的共同点是"平仓那一刻模型没有决策权"，所以复盘出来的教训不能只锁在
# 出场端——模型真正能改的是"这一笔该不该开"。
_FORCED_EXIT_SOURCES = ("sl", "be", "be_local", "tp", "lock", "lock_local")


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
    lesson = unwrap_lesson(lesson)

    if theme not in LESSON_THEME_ENUM or theme == "其他":
        # 模型没给 / 给了枚举外的词 / 偷懒写"其他" → 关键词再试一次；
        # 还是归不进去就明确标"其他"，落盘能看出"这条没归类"。
        theme = lesson_theme(lesson) or "其他"
    if scope not in LESSON_SCOPES:
        scope = infer_scope(lesson)
    return {"theme": theme, "scope": scope, "lesson": lesson[:120]}


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
                                "verified": 0, "sl": 0}
        b["count"] += 1
        # sl = 这个桶里有几条是**程序/券商强制平仓**的单留下的（硬止损、
        # 保本止损）。出场那一刻不是模型决定的，这类教训两端都要看
        # （见 _scope_ok 与 _FORCED_EXIT_SOURCES）。
        if str(r.get("src") or "") in _FORCED_EXIT_SOURCES:
            b["sl"] += 1
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
        # 例外：硬止损留下的教训。出场时刻是程序按 sl_usd 砍的，模型没有
        # 出场决策权，把这条锁在出场端等于它永远影响不到下一次入场——
        # 实测 2026-10-28：21:46 写下「破 4047.4 双底立即止损」（scope=exit），
        # 22:07 在 4047.44 又接了一笔多，因为入场提示词里根本没有这句话。
        if not (scope == "entry" and c.get("sl")):
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
            if c.get("sl"):
                # 这条来自被硬止损砍掉的单：出场那一刻是程序决定的，
                # 不写清楚，模型会把它读成"下次早点平"，而真正该问的是该不该开。
                line += ("\n  （这笔是硬止损砍的，出场时机不是你选的——"
                         "重点看当初这个位置/方向该不该进）")
            lines.append(line)
        blocks.append("你从自己的交易里总结出的教训，按重复次数排序（次数越多越可能是"
                      "真规律，只出现一次的别当铁律）：\n" + "\n".join(lines))

    return "\n\n".join(blocks) if blocks else "暂无复盘笔记。"
