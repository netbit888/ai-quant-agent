"""纯技术指标层：把 K 线折成数字，不碰 MT5、不读 state、不写盘。

从 `live/autotrade.py` 搬出来的第一部分（2026-11-13）。**纯函数，无副作用**：
输入 dict 序列，输出 dict / float / str；除了 `time.strftime` 格式化时间戳，
没有一处 I/O，也没有一处依赖 `Cfg`。

为什么单独成模块
----------------
这一层是全项目复用面最广、也被复制最多的一份：`tools/replay_exit.py` 手抄过
`_atr` 的口径（注释写"必须一模一样"，实际靠人肉同步）。搬到这里之后，回放
工具可以直接 import，不必再抄第二份。

搬迁约定
--------
· 原样搬运，逻辑一个字没改。行内注释保持原样——它们是账本，不是装饰。
· `autotrade` 顶部 `from live.ta import ...` 把这些名字重新导出，所以
  `autotrade._atr`、`autotrade.parse_p_up` 这些老路径继续可用，17,000 行
  测试不用改一行。
· 刻意**没有**搬进来的：`_mtf_bundle`（读 MT5 取 M5 行情）、`price_dist_of`
  （依赖账户系数与合约规格）、`_digits`/`_px`（降级要写流水）。它们看着像
  同一层，实际都带 I/O，搬进来会把这个模块重新弄脏。
"""

from __future__ import annotations

import json
import re
import time

P_UP_FLOOR, P_UP_CEIL = 0.20, 0.80

# 从模型回包里抠数字用的两个正则。跟着 `parse_p_up` 一起搬走——它们只服务
# 这一个解析函数，留在 autotrade 就会变成"两处各定义一份"。
_JSON_RE = re.compile(r"\{[^{}]*\}", re.S)
_PUP_RE = re.compile(r'"?p_?up"?\s*[:=]\s*([0-9]+\.?[0-9]*)', re.I)


# AI 交易员**只能看到当前这根 K 线 + 之前 13 根**，共 14 根，四价全给。
# 这不是在提示词里客气地写一句"请你只看这些"，而是在构造 prompt 时**物理截断**：
# 就算上游传进来 120 根，`visible_bars()` 也只会输出最后 14 根。
# 模型守不守规矩不该靠它自觉——它能看到的和它被告知能看到的一致，才叫约束。
#
# 唯一的例外是**平仓之后**：允许回看「平仓前 13 根 → 开仓之前」那一段，
# 用来复盘。那时仓位已经没了，回看不会让它改主意，所以不违反约束。

VISIBLE_BARS = 14      # 当前 1 根 + 之前 13 根


# 行情状态分桶阈值。_structure_text 的「行情判定」和教训的 regime 标签
# 必须用同一套数，各写一份必然漂移。
ER_RANGE_MAX = 0.30   # ER 低于此 = 震荡 → 低买高卖
ER_TREND_MIN = 0.50   # ER 高于此 = 趋势 → 等回踩顺势


# MA 的周期。2026-10-26 用户要求：用 28 根 M1 收盘均线做多空偏向
# （"当前 K 线在均线下方偏空，反之偏多"），他自己做单也看这条。
#
# 关键设计：28 根是可见窗口（14 根）的两倍，所以**必须额外取数**。
# 但模型仍然只看 14 根原始 K 线——多出来的部分只落成一个均线数字，
# 和 M5 摘要同一个原则：程序算好结论，不给原始数据。
# 这不是"放宽信息边界"，是"补一个模型自己算不出来的参照系"。
MA_PERIOD = 28


# 给面板用的品种展示名（下拉框文案）。键必须都在 TRADABLE_SYMBOLS 里。
SYMBOL_LABELS = {
    "XAUUSDm": "黄金 XAUUSD",
    "XAUUSDc": "黄金 XAUUSD（美分）",
    "BTCUSDm": "比特币 BTCUSD",
}


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


def _spread_points(tick: dict, point: float) -> float:
    if not point:
        return 0.0
    return (float(tick.get("ask") or 0.0) - float(tick.get("bid") or 0.0)) / point


def visible_bars(rows: list[dict]) -> list[dict]:
    """裁到 AI 允许看到的 14 根。所有喂给模型的 prompt 都必须过这一道。"""
    return rows[-VISIBLE_BARS:] if rows else []


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


def _last_bar_shape(rows: list[dict]) -> dict:
    """当前这根 K 线自己的形状：收盘在自身区间的位置、上下影线占比。

    趋势打法需要它来区分「强势推进」和「插针见顶」——两者在 14 根窗口里
    的 ER、drift、pos 可以完全一样，区别只在最后一根长什么样。
    只看当前根，不引入新指标，也不碰可见窗口之外的数据。

    clv（close location value）= (收-低)/(高-低)，1 = 收在最高，0 = 收在最低。
    上影线占比大 + clv 低 = 冲高被打回来，趋势市追这种就是接最后一棒。
    """
    w = visible_bars(rows)
    if not w:
        return {"ok": False}
    b = w[-1]
    try:
        h, l = float(b.get("high") or 0.0), float(b.get("low") or 0.0)
        c, o = float(b.get("close") or 0.0), float(b.get("open") or 0.0)
    except (TypeError, ValueError):
        return {"ok": False}
    rng = h - l
    if rng <= 0 or min(h, l, c, o) <= 0:
        return {"ok": False}
    return {
        "ok": True,
        "clv": round((c - l) / rng, 4),
        "upper_wick": round((h - max(c, o)) / rng, 4),
        "lower_wick": round((min(c, o) - l) / rng, 4),
        "body": round(abs(c - o) / rng, 4),
        "bull": c > o,
    }


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


def _zone_text(pos: float, er: float, drift: float, edge: float,
               trend_follow: bool = True) -> str:
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
    if (uptrend or downtrend) and not trend_follow:
        return ("趋势市，但趋势打法已关闭（trend_follow=False）→ 本程序不建议开仓。"
                "震荡贴边那一套不适用于这里，别拿 ER 高当成抄底摸顶的理由")
    if pos <= lo:
        if downtrend:
            return ("贴下沿＋下跌趋势（程序倾向：顺势做空。贴下沿在这里是弱势延续，"
                    "不是超卖，**不要反手接多**；但当前根必须弱势收——收盘在自身区间下半、"
                    "下影线不长，长下影＝有人接盘，不做）")
        if uptrend:
            return ("贴下沿＋上涨趋势（程序倾向：回踩已经发生，直接做多，"
                    "条件是当前根止跌——收阳或收盘回到自身区间上半。**不要再等更低的价位**）")
        return f"贴下沿（程序倾向：可考虑做多，阈值 {edge:.0%}）"
    if pos >= hi:
        if uptrend:
            # 2026-10-28 改：原文是「此处不是买点，等回踩到前一根实体中位」。
            # 实测那句话让趋势市 94% 的时间空仓（70 次决策只开 4 仓），而
            # 按它等来的回踩单 47 笔均值 -1.21、胜率 36% —— 等回踩在这批样本里
            # 是负期望。现在允许顺势直接做，但用**当前根的形状**代替"等一个价位"
            # 来挡插针顶：这条不拦"位置高"，只拦"冲高被打回来"。
            return ("贴上沿＋上涨趋势（程序倾向：顺势做多。高位本身不是否决理由，"
                    "但当前根必须是强势收——收盘在自身区间上半、上影线不超过区间一半；"
                    "若冲高回落（长上影＋收盘掉到下半）就是插针顶，不做）")
        if downtrend:
            return ("贴上沿＋下跌趋势（程序倾向：反抽已经发生，直接做空，"
                    "条件是当前根滞涨——收阴或收盘回落到自身区间下半。不要再等更高的价位）")
        return f"贴上沿（程序倾向：可考虑做空，阈值 {1 - edge:.0%}）"
    if uptrend or downtrend:
        # 趋势市的中间地带不再是"不动手"：方向已经由 ER+drift 给出来了，
        # 位置只影响好坏、不决定有无。震荡市中间不动手的规矩不搬到这里。
        side = "做多" if uptrend else "做空"
        return (f"中间地带（{edge:.0%}~{1 - edge:.0%}）＋{'上涨' if uptrend else '下跌'}趋势"
                f"（程序倾向：可顺势{side}，位置一般，所以 p_up 别给激进值；"
                f"当前根形状必须同向）")
    return f"中间地带（{edge:.0%}~{1 - edge:.0%}，程序倾向不动手）"


def _zone_fact(pos: float, edge: float) -> str:
    """位置判定（**纯事实版**）：只说价格站在区间哪里，不说该买还是该卖。

    出场 prompt 用这个，不用 `_zone_text`。理由见 `_structure_text` 的
    `for_exit` 分支——出场端已经锁定打法（plan），再给它一句"程序倾向：
    顺势做多"，只会让模型在两个方向之间挑一个它爱听的。
    """
    edge = max(0.0, min(0.49, float(edge)))
    if pos <= edge:
        return f"贴着区间下沿（≤{edge:.0%}）"
    if pos >= 1 - edge:
        return f"贴着区间上沿（≥{1 - edge:.0%}）"
    return f"区间中部（{edge:.0%}~{1 - edge:.0%}）"


def _prog_direction(rows: list[dict], zone_edge: float = 0.35,
                    trend_follow: bool = True) -> int:
    """程序对方向的**建议**：+1 多 / -1 空 / 0 不动手。

    与 _zone_text 用同一套数（_structure + zone_edge），保证「模型看到的建议」
    和「落盘的 prog_dir」永远一致——各算各的必然漂移。
    0 的语义是"程序这一格没有方向建议"，模型选哪个方向都记为
    "程序无建议，模型自选"（见 trade_detail 的裁决）。

    分 regime（2026-10-28）：
      · 震荡：贴下沿 +1、贴上沿 -1、中间 0（老逻辑，一个字没改）；
      · 趋势：一律给顺势方向，位置不再决定是否给建议；
      · 趋势但 trend_follow=False：返回 0，趋势市不交易（不是退回"等回踩"，
        那条路实测 47 笔均值 -1.21，没有保留价值）。
    """
    s = _structure(rows)
    if not s.get("ok"):
        return 0
    edge = max(0.0, min(0.49, float(zone_edge)))
    pos = s["pos"]
    downtrend = s["er"] > ER_TREND_MIN and s["drift"] < 0
    uptrend = s["er"] > ER_TREND_MIN and s["drift"] > 0
    # 趋势市：方向由 ER + drift 决定，**位置不再决定是否给建议**。
    # 2026-10-28 之前这里在「涨势贴上沿」返回 0（不背书追高），中间地带也返回 0。
    # 后果是趋势市里程序长期没有方向，模型每次顺势开仓都被面板记成
    # 「程序无建议，模型自选」，事后根本没法按 regime 算谁的判断更准。
    # 现在趋势市一律给顺势方向，追高与否交给当前根形状和提示词去管。
    if uptrend or downtrend:
        if not trend_follow:
            return 0
        return 1 if uptrend else -1
    if pos > edge and pos < 1 - edge:
        return 0
    if pos <= edge:
        return 1               # 震荡贴下沿：倾向做多
    return -1                  # 震荡贴上沿：倾向做空


def feature_snapshot(rows: list[dict], cfg=None,
                     zone_edge: float | None = None,
                     mtf: dict | None = None) -> dict:
    """决策时刻的特征快照，落盘到日志行。

    参数 `cfg` 是 `live.cfg.Cfg`（鸭子类型：只读 symbol / tf / zone_edge
    / trend_follow 四个字段）。这里**故意不写类型注解**：写了就得
    `from live.cfg import Cfg`，这层「只依赖标准库」的纯度立刻破防
    （见 tests/test_ta_module.py 的守门用例），而收益只是编辑器提示。

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
        "prog_dir": _prog_direction(
            rows, edge,
            cfg.trend_follow if cfg is not None else True),
        # 当前根形状：趋势打法的入场确认全靠它（强势收 vs 插针顶），必须落盘
        # 才能事后回答「追高的单里，形状占多大权重」。
        "shape": _last_bar_shape(rows),
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
                    digits: int = 3, trend_follow: bool = True,
                    for_exit: bool = False) -> str:
    s = _structure(rows)
    if not s.get("ok"):
        return "结构 数据不足"
    if for_exit:
        # 出场端只报「是什么行情」，不报「该怎么做」。
        #
        # 2026-10-29 那笔的账：plan=range（止盈 3.25）锁定了打法，20 分钟后
        # 一根插针把 ER 从 0.04 顶到 0.74，这里原样塞进 prompt 的
        # 「趋势市·上行 → 顺势做多，不等回踩」和 plan_text 的「震荡单别指望
        # 变成趋势」正面打架。模型 13 秒里被问三次、三次都挑后者，从 +4.86
        # 拿到 -3.90（真实高点 8.30，浮盈 6.9 倍 ATR）。
        #
        # 入场端靠这句话选打法，必须留着；出场端打法已经定了，再看一遍
        # 行情判定只会给它一个反悔的理由。纯结构（ER/位置/形态）仍在。
        zone = _zone_fact(s["pos"], zone_edge)
        regime = _regime_word(s["er"], s["drift"])
    elif s["er"] < ER_RANGE_MAX:
        regime = "震荡市 → 低买高卖（贴下沿做多、贴上沿做空，中间不动手）"
    elif s["er"] > ER_TREND_MIN:
        if not trend_follow:
            regime = "趋势市，趋势打法已关闭 → 不交易"
        else:
            regime = ("趋势市·上行 → 顺势做多，不等回踩"
                      if s["drift"] > 0 else
                      "趋势市·下行 → 顺势做空，不等反抽")
    else:
        regime = "方向不明确 → 只做位置明显的单"

    # 位置判定直接算好给模型，不让它自己套阈值：
    # 阈值改一次就得让它跟着变，各算各的必然对不上。
    pos = s["pos"]
    edge = max(0.0, min(0.49, float(zone_edge)))
    if not for_exit:
        zone = _zone_text(pos, s["er"], s["drift"], edge, trend_follow)

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
    # 当前根形状：趋势打法就靠它分辨「强势推进」和「插针见顶」。
    # 只描述事实、不下结论——结论在上面 zone 里，两处必须说同一件事。
    sh = _last_bar_shape(rows)
    if sh.get("ok"):
        shape = (f"｜当前根：收盘在自身区间 {sh['clv']:.0%}"
                 f"（{'收阳' if sh['bull'] else '收阴'}，实体占 {sh['body']:.0%}，"
                 f"上影 {sh['upper_wick']:.0%}，下影 {sh['lower_wick']:.0%}）")
    else:
        shape = ""
    return (f"结构 ER={s['er']:.2f}（净移动 {drift:+.{d}f}（{dir_text}）/ 总行程 {s['path']:.{d}f}）"
            f"｜振幅 {s['span']:.{d}f}{atr_text}｜当前收盘位置 {pos:.0%}（0%=区间最低 100%=最高）"
            f"｜{streak_text}{shape}｜位置判定：{zone}｜行情判定：{regime}")


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


def _symbol_label(symbol: str) -> str:
    """system prompt 里自我介绍用的品种名。

    为什么必须动态（2026-10-26 修）：三个 system prompt 原来全部硬编码
    「XAUUSD」。上一轮加 BTC 时改了精度、白名单、成本提示，唯独漏了这里——
    真选 BTC 跑起来，模型会被告知"你是黄金交易员"同时收到 80000 的价格。
    这比没验证更糟：它会把黄金的波动直觉套到 BTC 上。
    """
    name = SYMBOL_LABELS.get(symbol)
    return name or symbol
