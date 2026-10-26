"""交易画像与盈亏平衡线。

为什么需要这个
--------------
评分里最关键的维度是「你离不亏钱还有多远」。这一条**不需要任何外部基准**——
它由你自己的盈亏比和这个品种的点差结构决定，是数学，不是观点。

市面上能找到的「M1 剥头皮 PF 1.5~2.0」都是卖 EA 的广告，没有可验证的成交记录。
拿它当基准等于给自己找一个编出来的数字。真正不可争辩的参照只有盈亏平衡线。

两种口径，别混用
----------------
1. **事后口径**（用已实现的盈亏比，点差已经含在盈亏里）：
       需要的胜率 = 1 / (1 + 盈亏比)
   用来评判「已经打完的这批单子，是正期望还是负期望」。

2. **事前口径**（规划止损止盈，点差是额外成本）：
       需要的胜率 = (止损 + 点差) / (止盈 + 止损)
   用来回答「我这样设止损止盈，毛胜率要多高才能不亏」。

把两个混在一起会得出互相矛盾的数字。评分用事后口径，规划用事前口径。
"""

from __future__ import annotations

import json
from pathlib import Path

from live import appdirs


def profile_file() -> Path:
    """画像落盘位置。走可写数据根，冻结后不能落进 _MEIPASS。"""
    return appdirs.data_dir() / "live" / "profile.json"

# 当前交易画像（2026-10 实测）。可用 `python -m tools.profile` 从数据重算。
DEFAULT_PROFILE = {
    "symbol": "XAUUSDm",
    "timeframe": "M1",
    "style": "scalping",
    "spread_points": 240,
    "range_points": 1743,
    "atr_points": 1866,
    "point": 0.001,
    "measured_from": "data/XAUUSDm_M1.csv",
    "measured_at": None,
    "note": "M1 超短线；点差占 M1 振幅约 13.8%。波动越低占比越高，低波动期可能无法盈利。",
}


def load_profile() -> dict:
    p = profile_file()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {**DEFAULT_PROFILE, **data}
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_PROFILE)


def breakeven_win_rate(rr: float | None) -> float | None:
    """事后口径：盈亏比 rr 下不亏钱所需的最低胜率（%）。

    rr = 平均盈利 / 平均亏损，两者都是**已实现**金额（点差已含）。
    期望 = p×rr − (1−p) = 0  →  p = 1/(1+rr)。
    """
    if rr is None or rr <= 0:
        return None
    return 100.0 / (1.0 + rr)


def exante_win_rate(sl_points: float, tp_points: float, spread_points: float) -> float | None:
    """事前口径：规划止损/止盈/点差（都是以 point 计）时所需的毛胜率（%）。

    赢：+TP − 点差；输：−SL − 点差。
    期望 = p(TP−s) − (1−p)(SL+s) = 0  →  p = (SL + s)/(TP + SL)。
    """
    if sl_points <= 0 or tp_points <= 0:
        return None
    return (sl_points + spread_points) / (tp_points + sl_points) * 100.0


def cost_hurdle(profile: dict | None = None) -> dict:
    """点差相对波动的占比。占比越高，策略能盈利的空间越窄。"""
    p = profile or load_profile()
    spread = float(p.get("spread_points") or 0)
    rng = float(p.get("range_points") or 0)
    atr = float(p.get("atr_points") or 0)
    return {
        "spread_points": spread,
        "range_points": rng,
        "atr_points": atr,
        "spread_over_range_pct": round(spread / rng * 100, 1) if rng else None,
        "spread_over_atr_pct": round(spread / atr * 100, 1) if atr else None,
        "spread_usd_per_lot": round(spread * float(p.get("point") or 0.001)
                                    * 100, 2),  # 黄金 1 手 = 100 oz
    }


def exante_table(profile: dict | None = None, rr_list: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
                 sl_points: float | None = None) -> list[dict]:
    """用「止损 = 1 倍 ATR」为基准，列出不同盈亏比所需的毛胜率。"""
    p = profile or load_profile()
    spread = float(p.get("spread_points") or 0)
    sl = sl_points or float(p.get("atr_points") or 0)
    rows = []
    for rr in rr_list:
        need = exante_win_rate(sl, sl * rr, spread)
        rows.append({"rr": rr, "sl_points": round(sl), "tp_points": round(sl * rr),
                     "required_win_rate": round(need, 1) if need else None})
    return rows


if __name__ == "__main__":
    prof = load_profile()
    print(f"画像：{prof['symbol']} {prof['timeframe']}（{prof['style']}）")
    h = cost_hurdle(prof)
    print(f"  点差 {h['spread_points']:.0f} pt = ${h['spread_usd_per_lot']:.2f}/手")
    print(f"  M1 振幅中位 {h['range_points']:.0f} pt → 点差占 {h['spread_over_range_pct']}%")
    print(f"  ATR 中位 {h['atr_points']:.0f} pt → 点差占 {h['spread_over_atr_pct']}%")
    print()
    print("事后口径：盈亏比 → 需要的胜率")
    for rr in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        print(f"  RR {rr:<4} → {breakeven_win_rate(rr):.1f}%")
    print()
    print(f"事前口径（止损 1×ATR = {h['atr_points']:.0f} pt，含点差）：")
    for r in exante_table(prof):
        print(f"  RR {r['rr']:<4} (TP {r['tp_points']} pt) → 需要毛胜率 {r['required_win_rate']}%")
