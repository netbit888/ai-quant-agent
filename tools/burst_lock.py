"""脉冲之后锁利线该设在哪？纯 K 线统计，不调模型。

问题
----
脉冲（3 根内走满 K 个 ATR）之后 15 分钟最大反向中位 −2.33 ATR，也就是说
"吐回去"是真的。但直接平仓会砍掉右尾（15 分钟均值还是正的 +0.39）。

折中形态只有一种：不平仓，把 SL 抬到「峰值 − L 个 ATR」。峰值继续抬高时
SL 跟着抬，回撤超过 L 就走。这个脚本回答两件事：

  1. 从峰值回撤 L 个 ATR 的**发生概率**（= 锁利被打掉的比例）
  2. 被打掉之后，价格**又超过峰值**的比例（= 砍错的代价）
  3. 被打掉时**锁住了多少利润** = 峰值倍数 − L

L 太小 → 几乎每笔都被打掉，且大量砍错；L 太大 → 等于没做。
要看的是「打掉比例」和「砍错比例」的曲线，不是找最优值。

方法上的两个近似（都会让结果偏乐观，必须记住）
------------------------------------------------
  A. 峰值取事后最大值。实盘 SL 是随峰值一路抬上去的，等效，但"确认峰值"
     也要花时间，实际触发点会比这里晚。
  B. 峰值只用 high/low（真实可触及），回撤用 close（避免影线噪音打掉，
     实盘是 bid/ask 逐笔，比 close 更细）。

参数事先定死，不扫。
"""

from __future__ import annotations

import argparse
import statistics as st
import sys

import numpy as np

SYMBOL = "XAUUSDc"
N_FAST = 3
K_ATR = 2.5
COOLDOWN = 15
ATR_WIN = 14
PEAK_WIN = 60        # 峰值在事件后 60 根内找（1 小时，够趋势单走完）
AFTER_WIN = 60       # 被打掉之后再看 60 根，判断"是不是砍错"
LS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)


def load_rates():
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize 失败: {mt5.last_error()}")
    try:
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 50_000)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise SystemExit("拿不到 K 线")
    return rates


def true_range(high, low, close):
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def rolling_atr(tr, win=ATR_WIN):
    out = np.full(len(tr), np.nan)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    for i in range(win, len(tr)):
        out[i] = (csum[i] - csum[i - win]) / win
    return out


def collect(close, atr, n_back, k_atr):
    out, last = [], -10**9
    hi = len(close) - PEAK_WIN - AFTER_WIN - 1
    for i in range(max(n_back, ATR_WIN), hi):
        a = atr[i]
        if not np.isfinite(a) or a <= 0 or i - last < COOLDOWN:
            continue
        move = close[i] - close[i - n_back]
        if abs(move) < k_atr * a:
            continue
        out.append((i, 1.0 if move > 0 else -1.0))
        last = i
    return out


def analyze(close, high, low, atr, events, label, k_atr):
    """对每个 L 算：被打掉的比例、砍错的比例、锁住的利润。"""
    if not events:
        print(f"{label}: 无样本")
        return
    rows = []
    for i, d in events:
        a = atr[i]
        # 同向价格轨迹：多头=high、空头=-low（取负统一成"越高越好"）
        fut_h = high[i + 1:i + 1 + PEAK_WIN] * d
        fut_l = low[i + 1:i + 1 + PEAK_WIN] * d
        fut_c = close[i + 1:i + 1 + PEAK_WIN] * d
        if len(fut_h) < 20:
            continue
        base = close[i] * d
        peak_off = int(np.argmax(fut_h))
        peak = (fut_h[peak_off] - base) / a
        after = fut_l[peak_off + 1:peak_off + 1 + AFTER_WIN]
        after_c = fut_c[peak_off + 1:peak_off + 1 + AFTER_WIN]
        rows.append((peak, peak_off, after, after_c, base, a, d))
    if not rows:
        print(f"{label}: 无样本")
        return
    peaks = [r[0] for r in rows]
    print(f"{label}: n={len(rows)}  峰值中位 {st.median(peaks):.2f} ATR  "
          f"峰值在事件后 {st.median([r[1] for r in rows]):.0f} 分")

    for L in LS:
        hit, wrong, lock, keep = 0, 0, [], []
        for peak, peak_off, after, after_c, base, a, d in rows:
            if peak <= L:
                continue                    # 峰值还不够高，锁不出利润
            sl = peak - L                   # SL 距开仓价 L 个 ATR
            worst = ((after - base) / a).min() if len(after) else peak - L
            if worst > sl:
                keep.append(1)
                continue
            hit += 1
            lock.append(sl)
            # 砍错 = 打掉之后价格又摸回峰值（本该继续持有）
            if len(after_c):
                tail = ((after_c - base) / a).max()
                if tail >= peak:
                    wrong += 1
        n = hit + len(keep)
        if not n:
            continue
        print(f"  L={L:>3.1f} ATR  SL 打掉 {hit}/{n} ({hit / n:>4.0%})  "
              f"其中砍错 {wrong}/{hit if hit else 1} ({wrong / (hit or 1):>4.0%})  "
              f"锁住利润中位 {st.median(lock) if lock else 0:+.2f} ATR")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", default="2.5,4,5")
    args = ap.parse_args()
    rates = load_rates()
    close = rates["close"].astype(float)
    high = rates["high"].astype(float)
    low = rates["low"].astype(float)
    atr = rolling_atr(true_range(high, low, close))
    span = (int(rates["time"][-1]) - int(rates["time"][0])) / 86400
    print(f"{SYMBOL} M1 {len(close):,} 根 {span:.0f} 天  "
          f"ATR 中位 {np.nanmedian(atr):.3f}\n")
    for k in [float(x) for x in args.ks.split(",") if x.strip()]:
        ev = collect(close, atr, N_FAST, k)
        analyze(close, high, low, atr, ev,
                f"===== {N_FAST} 根内 >= {k} ATR", k)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
