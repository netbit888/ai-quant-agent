"""A/B：同一批历史决策点，思考模式「开」与「关」谁更接近后续真实走势。

为什么需要这个工具
------------------
2026-10-29 把模型从 deepseek-chat 换到 deepseek-flash，发现后者**默认开思考**
（响应带 reasoning_content，思维链按输出 token 计费）。同一条提示词实测：
默认 out=124（思考 102）/ 1.63s，关掉 out=21 / 0.71s。

「思考更强」是模型厂商的说法，对**这个**任务成立不成立要自己验。所以这里把
真实决策点重放两遍，只看两件能证伪的事：

  1. `p_up` 的校准度（Brier 分数）——模型说 0.7 的时候，真的有 70% 的次数上涨吗？
     Brier 越低越好，0.25 = 永远猜 0.5 的水平。
  2. `act=true` 的方向命中率——说做的时候，方向对不对。

重放的方法与已知偏差
--------------------
· K 线按日志里的 `feat.win` 从 MT5 历史取回（**不存原始 K 线是当初的取舍**，
  见 feature_snapshot 的注释），用 `feature_snapshot` 重算一遍特征，与日志里
  记的 `er / pos / span_atr` 逐点比对——对不上就说明重建失真，结论不可用。
· `build_entry_prompt` 里的 clock 用的是真实当前时间，这里替换成**该决策点的
  历史时刻**，否则模型会看到「现在 02:12」而 K 线停在 17:40。
· M5 背景按同一时刻从历史 M5 现算，格式与 `_mtf_bundle` 一致。
· **没有的东西**：当时的 tick 文本、教训库、战绩统计（前两个没落盘）。两边
  一样缺，所以对 A/B 公平，但绝对值不等于生产环境的表现。

用法
----
    .venv/Scripts/python.exe -m tools.ab_thinking            # 默认 20 开仓 + 20 空仓
    .venv/Scripts/python.exe -m tools.ab_thinking --opens 0 --skips 60
    .venv/Scripts/python.exe -m tools.ab_thinking --ahead 15 --no-verify
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import autotrade as at          # noqa: E402
from agent.llm import _client, current_config   # noqa: E402

SPREAD = 0.24        # XAUUSDm 实测点差（价格单位），与 replay_exit 同一口径
BARS_BEFORE = 120    # 重建 prompt 时往前取多少根（MA28 需要 ≥28 根）

# flash 空闲价（USD / 1M tokens），只用来给个量级，不当作账本
PRICE_HIT, PRICE_MISS, PRICE_OUT = 0.003, 0.15, 0.6


class _FrozenClock:
    """把 build_entry_prompt 里的「现在几点」冻在该决策点的历史时刻。"""

    def __init__(self, ts: int) -> None:
        self._ts = int(ts)

    def gmtime(self, t=None):
        return time.gmtime(self._ts if t is None else t)

    def strftime(self, fmt: str, t=None) -> str:
        return time.strftime(fmt, self.gmtime() if t is None else t)


def load_decisions() -> tuple[list[dict], list[dict]]:
    """返回 (开仓点, 空仓点)，都只要带 feat 快照的行。"""
    rows: list[dict] = []
    for p in glob.glob(str(ROOT / "data/live/accounts/*/autotrade_log*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: r.get("ts") or 0)
    opens = [r for r in rows if r.get("kind") == "open" and (r.get("feat") or {}).get("ok")]
    skips = [r for r in rows if r.get("kind") == "skip"
             and (r.get("feat") or {}).get("ok") and r["feat"].get("win")]
    opens = [r for r in opens if (r.get("feat") or {}).get("win")]
    return opens, skips


def _rates(symbol: str, tf: int, t0: int, t1: int) -> list[dict]:
    import MetaTrader5 as mt5
    from datetime import datetime, timezone

    arr = mt5.copy_rates_range(
        symbol, tf,
        datetime.fromtimestamp(t0, timezone.utc),
        datetime.fromtimestamp(t1, timezone.utc),
    )
    if arr is None:
        return []
    return [
        {"time": int(b["time"]), "open": float(b["open"]), "high": float(b["high"]),
         "low": float(b["low"]), "close": float(b["close"]),
         "tick_volume": int(b["tick_volume"])}
        for b in arr
    ]


def mtf_text_at(m5: list[dict], ts: int) -> str:
    """复刻 `_mtf_bundle` 的文案（同一时刻的历史 M5）。"""
    idx = bisect.bisect_right([b["time"] for b in m5], ts) - 1
    if idx < 0:
        return ""
    w = m5[max(0, idx + 1 - at.MTF_BARS): idx + 1]
    if len(w) < at.MTF_MIN_BARS:
        return ""
    s = at._structure(w)
    if not s.get("ok"):
        return ""
    a = at._atr(w)
    drift = s["drift"]
    dir_text = "上行" if drift > 0 else ("下行" if drift < 0 else "走平")
    has_atr = bool(a.get("ok") and a.get("atr", 0) > 0)
    atr_part = (f"｜振幅 {s['span']:.3f}（{a['span_atr']:.1f} 倍单根均幅）"
                if has_atr else f"｜振幅 {s['span']:.3f}")
    return (f"【背景】M5 {len(w)} 根（约 {len(w) * 5} 分钟）："
            f"ER={s['er']:.2f}（净移动 {drift:+.3f} {dir_text}）"
            f"｜收盘位置 {s['pos']:.0%}{atr_part}"
            f"｜判定：{at._regime_word(s['er'], drift)}")


def partial_bar(symbol: str, bar: dict, dec_ts: int, mt5) -> dict | None:
    """把决策时刻那根**还没走完**的 K 线重建出来。

    为什么必须做：tick 每 60 秒一轮，落在每根 M1 的第 28~39 秒，所以生产里
    `rows[-1]` 是一根走到一半的 K 线（含当时的 high/low/close），而历史接口
    给的是**走完的整根**。拿整根重放，等于让模型偷看未来——而思考臂恰恰更会
    利用「这根收得强/弱」这种形态信息，会系统性地把它评得更准。

    重建口径与 `_bars`（MT5 rates）一致：用 bid 的 tick 序列算 O/H/L/C。
    口径是标定过的：10 个点 × bid/ask/mid × 回看 0/1/2/3/5 秒，**bid + 回看到
    日志时刻**误差最小（均 |Δer| 0.008 / |Δpos| 0.010），ask 与 mid 都更差。
    """
    from datetime import datetime, timezone

    arr = mt5.copy_ticks_range(
        symbol,
        datetime.fromtimestamp(int(bar["time"]), timezone.utc),
        datetime.fromtimestamp(int(dec_ts) + 1, timezone.utc),
        mt5.COPY_TICKS_ALL,
    )
    if arr is None or not len(arr):
        return None
    bids = [float(t["bid"]) for t in arr if float(t["bid"]) > 0]
    if not bids:
        return None
    return {"time": int(bar["time"]), "open": bids[0], "high": max(bids),
            "low": min(bids), "close": bids[-1], "tick_volume": len(bids)}


def build_prompt(dec: dict, m1: list[dict], m5: list[dict], cfg,
                 last_override: dict | None = None) -> str | None:
    ts = int(dec["feat"]["win"][1])
    idx = bisect.bisect_right([b["time"] for b in m1], ts) - 1
    if idx < 28:
        return None
    rows = m1[max(0, idx + 1 - BARS_BEFORE): idx + 1]
    if last_override:
        rows = rows[:-1] + [last_override]
    close = rows[-1]["close"]
    orig = at.time
    at.time = _FrozenClock(ts)
    try:
        return at.build_entry_prompt(
            rows, close - SPREAD / 2, close + SPREAD / 2,
            symbol=cfg.symbol, tf=cfg.tf, point=0.001,
            stats={"closed": 200, "wins": 71},     # 固定的战绩背景，两臂一致
            lots=cfg.lots, lessons="",
            zone_edge=cfg.zone_edge,
            mtf_text=mtf_text_at(m5, ts),
            trend_follow=cfg.trend_follow,
        )
    finally:
        at.time = orig


def ask(client, system: str, prompt: str, thinking: bool, model: str) -> dict:
    """一次调用。thinking=False 走 extra_body 关思考（顶层传会被 SDK 拒）。"""
    extra = {} if thinking else {"extra_body": {"thinking": {"type": "disabled"}}}
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            **extra,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "latency": time.time() - t0}
    text = resp.choices[0].message.content or ""
    parsed = at.parse_p_up(text) or {}
    u = resp.usage
    return {
        "ok": True,
        "p_up": parsed.get("p_up"),
        "act": parsed.get("act"),
        "reason": parsed.get("reason") or "",
        "latency": time.time() - t0,
        "out": getattr(u, "completion_tokens", 0) or 0,
        "hit": getattr(u, "prompt_cache_hit_tokens", 0) or 0,
        "miss": getattr(u, "prompt_cache_miss_tokens", 0) or 0,
        "raw": text,
    }


def forward(m1: list[dict], ts: int, ahead: int) -> dict | None:
    """该决策点之后 ahead 根的真实走势（不含决策那根）。"""
    idx = bisect.bisect_right([b["time"] for b in m1], ts) - 1
    if idx < 0 or idx + ahead >= len(m1):
        return None
    base = m1[idx]["close"]
    fut = m1[idx + 1: idx + 1 + ahead]
    if len(fut) < ahead:
        return None
    return {
        "ret": fut[-1]["close"] - base,
        "hi": max(b["high"] for b in fut) - base,
        "lo": min(b["low"] for b in fut) - base,
    }


def brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _q(v: list[float], p: float) -> float:
    if not v:
        return 0.0
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser(description="思考模式开/关的 A/B（历史决策点重放）")
    ap.add_argument("--opens", type=int, default=20, help="取样多少笔真实开仓点")
    ap.add_argument("--skips", type=int, default=20, help="取样多少笔空仓决策点")
    ap.add_argument("--ahead", type=int, default=10, help="往后看几根判涨跌")
    ap.add_argument("--no-verify", action="store_true", help="跳过特征重建校验")
    ap.add_argument("--dump", help="把逐点明细写成 JSON 文件")
    args = ap.parse_args()

    cfg = at.Cfg()
    opens, skips = load_decisions()
    print(f"库里：开仓点 {len(opens)} / 空仓点 {len(skips)}")

    def _even(seq: list[dict], k: int) -> list[dict]:
        if k <= 0 or not seq:
            return []
        if k >= len(seq):
            return list(seq)
        step = len(seq) / k
        return [seq[int(i * step)] for i in range(k)]

    picks = [("open", r) for r in _even(opens, args.opens)]
    picks += [("skip", r) for r in _even(skips, args.skips)]
    picks.sort(key=lambda x: x[1]["ts"])
    if not picks:
        raise SystemExit("取不到决策点")

    t0 = int(picks[0][1]["ts"]) - 6 * 3600
    t1 = int(picks[-1][1]["ts"]) + (args.ahead + 2) * 60
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize 失败：{mt5.last_error()}")
    try:
        m1 = _rates(cfg.symbol, mt5.TIMEFRAME_M1, t0, t1)
        m5 = _rates(cfg.symbol, mt5.TIMEFRAME_M5, t0, t1)
        print(f"K 线：M1 {len(m1)} 根 / M5 {len(m5)} 根")

        system = at.system_for(at.ENTRY_SYSTEM, cfg)
        client = _client()
        model = current_config()["model"]
        print(f"模型：{model}\n")

        def _partial_of(dec: dict) -> dict | None:
            """决策时刻那根未走完的 K 线（见 partial_bar 的注释）。"""
            ts = int(dec["feat"]["win"][1])
            idx = bisect.bisect_right([b["time"] for b in m1], ts) - 1
            if idx < 0:
                return None
            return partial_bar(cfg.symbol, m1[idx], int(dec["ts"]), mt5)

        # --- 重建校验：重算的特征要和日志里记的一致，否则重放不可信
        #
        # 口径已标定（10 个点 × bid/ask/mid × 回看 0/1/2/3/5 秒的网格）：
        # bid + 回看到日志时刻误差最小（均 |Δer| 0.008 / |Δpos| 0.010，6/10 逐位
        # 命中），ask 和 mid 都更差，回看得越多越差。
        #
        # 剩下那点误差是**结构性的**：日志的 ts 是模型返回之后才写的，而特征
        # 快照在调用之前，真实时刻不知道（差一次模型调用的时延）。所以这里看
        # 均值而不是卡单点阈值。
        drift_er, drift_pos, bad = [], [], 0
        if not args.no_verify:
            for _kind, dec in picks:
                ts = int(dec["feat"]["win"][1])
                idx = bisect.bisect_right([b["time"] for b in m1], ts) - 1
                if idx < 28:
                    continue
                rows = m1[max(0, idx + 1 - BARS_BEFORE): idx + 1]
                pb = _partial_of(dec)
                if pb:
                    rows = rows[:-1] + [pb]
                snap = at.feature_snapshot(rows, cfg)
                logged = dec["feat"]
                d_er = abs(float(snap.get("er") or 0) - float(logged.get("er") or 0))
                d_pos = abs(float(snap.get("pos") or 0) - float(logged.get("pos") or 0))
                drift_er.append(d_er)
                drift_pos.append(d_pos)
                if d_er > 0.03 or d_pos > 0.05:
                    bad += 1
            if drift_er:
                print(f"重建校验：{len(drift_er)} 点  "
                      f"均|Δer|={st.mean(drift_er):.4f}  均|Δpos|={st.mean(drift_pos):.4f}  "
                      f"偏差较大 {bad} 点")
                if bad > len(drift_er) * 0.3:
                    print("⚠️ 偏差点过多，下面的对比只能当参考——先怀疑历史 K 线与当时不同")
            print()

        # --- 逐点跑两臂
        records = []
        for i, (kind, dec) in enumerate(picks, 1):
            ts = int(dec["feat"]["win"][1])
            fw = forward(m1, ts, args.ahead)
            prompt = build_prompt(dec, m1, m5, cfg, _partial_of(dec))
            if prompt is None or fw is None:
                continue
            a = ask(client, system, prompt, True, model)
            b = ask(client, system, prompt, False, model)
            was = "做" if kind == "open" else ("不做" if dec.get("act") is False else "?")
            records.append({"kind": kind, "ts": ts, "atr": float(dec["feat"].get("atr") or 0),
                            "logged_act": kind == "open" or dec.get("act") is True,
                            "logged_p_up": dec.get("p_up"),
                            "fwd": fw, "think": a, "fast": b})
            print(f"  [{i:2d}/{len(picks)}] {time.strftime('%m-%d %H:%M', time.localtime(ts))}"
                  f" {kind:4} 当时{was}"
              f" | 思考 p={a.get('p_up')} act={a.get('act')} {a.get('latency', 0):.1f}s"
              f" | 直给 p={b.get('p_up')} act={b.get('act')} {b.get('latency', 0):.1f}s")
    finally:
        mt5.shutdown()      # 独立进程用完就关：本工具不常驻，不会拖累面板

    if not records:
        raise SystemExit("没有可评分的点（历史不够？）")

    # --- 汇总
    print("\n" + "=" * 78)
    for arm, label in (("think", "思考 开"), ("fast", "思考 关")):
        ok = [r for r in records if r[arm].get("ok")]
        pairs = [(float(r[arm]["p_up"]), 1 if r["fwd"]["ret"] > 0 else 0)
                 for r in ok if r[arm].get("p_up") is not None]
        acted = [r for r in ok if r[arm].get("act") is True]
        right = [r for r in acted
                 if (r[arm]["p_up"] - 0.5) * r["fwd"]["ret"] > 0]
        out = [r[arm]["out"] for r in ok]
        lat = [r[arm]["latency"] for r in ok]
        ps = [float(r[arm]["p_up"]) for r in ok if r[arm].get("p_up") is not None]
        print(f"{label}：n={len(ok)}  解析失败={len(records) - len(ok)}"
              f"  act=true {len(acted)}"
              f"  方向命中 {len(right)}/{len(acted)}"
              f"（{100 * len(right) / max(1, len(acted)):.0f}%）")
        print(f"   Brier={brier(pairs):.4f}（0.25=永远猜 0.5）"
              f"  p_up 中位 {_q(ps, .5):.2f}  取 0.5 的 {sum(1 for x in ps if abs(x - 0.5) < 1e-9)}/{len(ps)}"
              f"  取 0.58 的 {sum(1 for x in ps if abs(x - 0.58) < 0.005)}/{len(ps)}")
        print(f"   输出 {st.mean(out):.0f} tok/次  延迟 {st.mean(lat):.2f}s/次"
              f"  成本 ≈ {sum(r[arm]['hit'] / 1e6 * PRICE_HIT + r[arm]['miss'] / 1e6 * PRICE_MISS + r[arm]['out'] / 1e6 * PRICE_OUT for r in ok):.4f} USD")
        print()

    # 两臂是否给出不同结论
    both = [r for r in records if r["think"].get("ok") and r["fast"].get("ok")]
    diff_act = [r for r in both if r["think"].get("act") != r["fast"].get("act")]
    diff_p = [r for r in both if r["think"].get("p_up") is not None
              and r["fast"].get("p_up") is not None
              and abs(float(r["think"]["p_up"]) - float(r["fast"]["p_up"])) >= 0.05]
    print(f"两臂对比（n={len(both)}）：act 不同 {len(diff_act)} 点"
          f"  p_up 差 ≥0.05 的 {len(diff_p)} 点")
    if diff_act:
        for r in diff_act[:10]:
            print(f"   {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}"
                  f" {r['kind']:4} 思考 act={r['think'].get('act')} p={r['think'].get('p_up')}"
                  f" | 直给 act={r['fast'].get('act')} p={r['fast'].get('p_up')}"
                  f" | 后续 {r['fwd']['ret']:+.2f}")

    # 分箱校准：模型说多少，实际涨了多少
    print("\n校准（p_up 分箱 → 实际上涨比例）：")
    for arm in ("think", "fast"):
        ok = [r for r in records if r[arm].get("ok") and r[arm].get("p_up") is not None]
        bins: dict[str, list[int]] = {}
        for r in ok:
            p = float(r[arm]["p_up"])
            key = ("<0.35" if p < 0.35 else "0.35~0.45" if p < 0.45
                   else "0.45~0.55" if p < 0.55 else "0.55~0.65" if p < 0.65 else "≥0.65")
            bins.setdefault(key, []).append(1 if r["fwd"]["ret"] > 0 else 0)
        parts = [f"{k}:{sum(v)}/{len(v)}" for k, v in bins.items()]
        print(f"  {'思考开' if arm == 'think' else '思考关'}  " + "  ".join(parts))

    if args.dump:
        Path(args.dump).write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n明细已写 {args.dump}")


if __name__ == "__main__":
    main()
