"""脉冲事件检验：快速冲高之后，价格更容易往回吐吗？

**不调用任何模型**。纯 K 线统计：从 MT5 拉历史 M1，按固定规则识别"短时间
内走了一大段"的事件，再看事件之后 3/5/10/15 分钟价格往哪走。

为什么是这个设计
----------------
待检验的命题不是"浮盈到 70 就走"，而是"**达到同样浮盈，走得越快越危险**"。
所以必须**控制位移、只变速度**：

  快速组：3 根内累计走 >= K 个 ATR
  慢速组：15 根内累计走 >= K 个 ATR，且最近 3 根内不足 1 个 ATR

两组的起始浮盈相同（都是 K 个 ATR），唯一差别是用了多久。如果速度真的携带
信息，快速组之后的收益应该更差、回吐应该更深；如果没有差别，那"速度"只是
"位移"的另一种写法（peak = 速度 x 时间），不构成独立信号。

指标（都按 ATR 归一，跨时段可比）
  ret_h   事件后 h 分钟的收益（同向）：(close[i+h]-close[i])*dir/atr
  mae     事件后 15 分钟内最大反向偏移（负数，绝对值越大吐得越多）
  mfe     事件后 15 分钟内最大同向偏移
  hold_h  事件后 h 分钟内是否创出 >= 0.5 ATR 的新高

参数是**事先定死的**（见下方常量）。看过结果再调 N/K 就是拟合噪声，那样
跑出来的任何结论都不作数——227 笔那次就是这么废掉的。

用法
    python -m tools.measure_burst                # 默认 90 天
    python -m tools.measure_burst --days 180
"""

from __future__ import annotations

import argparse
import statistics as st
import sys

import numpy as np

# ---- 固定参数（不要扫） --------------------------------------------------
SYMBOL = "XAUUSDc"
TF_MIN = 1
N_FAST = 3          # 快速组：3 根
N_SLOW = 15         # 慢速组：15 根
K_ATR = 2.5         # 两组共同的位移门槛（ATR 倍数）
FAST_SLOW_MAX = 1.0  # 慢速组要求最近 3 根内不足 1 个 ATR（排除先冲后停）
HORIZONS = (3, 5, 10, 15)
COOLDOWN = 15       # 两次事件至少隔 15 根，避免同一波被重复计数
ATR_WIN = 14        # ATR 窗口，与 live/autotrade.py::_atr 同口径


def load_rates(days: int):
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize 失败: {mt5.last_error()}")
    try:
        # 这个终端的 M1 缓存上限就在 5 万根（约 51 天），要更多会报
        # -2 Invalid params。用 from_pos 拿满，比 range 稳。
        want = min(days * 1440, 50_000)
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, want)
    finally:
        # 只读脚本，用完必须放连接：面板那边还在跑，不能挂着
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise SystemExit("拿不到 K 线")
    return rates


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR = max(高-低, |高-前收|, |低-前收|)。含跳空修正，与 _atr 同口径。"""
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def rolling_atr(tr: np.ndarray, win: int = ATR_WIN) -> np.ndarray:
    """每个位置 i 用它**之前**的 win 根 TR 算 ATR（不含 i，避免用到未来）。"""
    out = np.full(len(tr), np.nan)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    for i in range(win, len(tr)):
        out[i] = (csum[i] - csum[i - win]) / win
    return out


def collect(close, atr, n_back: int, k_atr: float, extra=None):
    """扫描事件，返回 [(i, dir)]。dir 是位移方向，后面所有收益都按它算。"""
    out = []
    last = -10**9
    start = max(N_SLOW, ATR_WIN)
    for i in range(start, len(close) - max(HORIZONS) - 1):
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        if i - last < COOLDOWN:
            continue
        move = close[i] - close[i - n_back]
        if abs(move) < k_atr * a:
            continue
        if extra and not extra(i, close, atr):
            continue
        out.append((i, 1.0 if move > 0 else -1.0))
        last = i
    return out


def stats_for(close, atr, events, label: str, k_atr: float = K_ATR) -> list[str]:
    if not events:
        return [f"{label}: 无样本"]
    lines = [f"{label}: n={len(events)}"]
    for h in HORIZONS:
        rets, mfes, maes = [], [], []
        for i, d in events:
            if i + h >= len(close):
                continue
            a = atr[i]
            rets.append((close[i + h] - close[i]) * d / a)
            seg = close[i + 1:i + HORIZONS[-1] + 1]
            if len(seg):
                rel = (seg - close[i]) * d / a      # 同向为正
                mfes.append(float(rel.max()))
                maes.append(float(rel.min()))
        if not rets:
            continue
        lines.append(
            f"  +{h:>2}分  中位 {st.median(rets):+.2f} ATR  "
            f"均值 {st.mean(rets):+.2f}  "
            f"正比例 {sum(1 for r in rets if r > 0) / len(rets):.0%}")
    if mfes:
        # 事件后 15 分钟内的最大顺向/反向偏移：回答"还能走多远、会吐多深"
        lines.append(
            f"  15分内 最大顺向 {st.median(mfes):+.2f} ATR  "
            f"最大反向 {st.median(maes):+.2f} ATR  "
            f"吐回超一半的比例 {sum(1 for m in maes if m < -k_atr / 2) / len(maes):.0%}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--ks", default="2.5,4,5",
                    help="位移门槛（ATR 倍数），逗号分隔。三档都报，不挑最优。")
    args = ap.parse_args()

    rates = load_rates(args.days)
    close = rates["close"].astype(float)
    high = rates["high"].astype(float)
    low = rates["low"].astype(float)
    tr = true_range(high, low, close)
    atr = rolling_atr(tr)
    span_days = (int(rates["time"][-1]) - int(rates["time"][0])) / 86400
    print(f"{SYMBOL} M1  {len(close):,} 根  {span_days:.0f} 天  "
          f"ATR 中位 {np.nanmedian(atr):.3f}")

    for k in [float(x) for x in args.ks.split(",") if x.strip()]:
        fast = collect(close, atr, N_FAST, k)
        slow = collect(close, atr, N_SLOW, k,
                       extra=lambda i, c, a, kk=k: abs(c[i] - c[i - N_FAST])
                       < FAST_SLOW_MAX * a[i])
        print(f"\n===== 位移门槛 {k} ATR =====")
        print(f"快速（{N_FAST} 根）{len(fast)} 个 | "
              f"慢速（{N_SLOW} 根且近 3 根 <{FAST_SLOW_MAX} ATR）{len(slow)} 个")
        for line in stats_for(close, atr, fast, "快速", k):
            print(line)
        for line in stats_for(close, atr, slow, "慢速", k):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
