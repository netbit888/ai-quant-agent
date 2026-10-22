"""AI Quant Agent — 统一 CLI

用法：
    python aqa.py data pull --symbol XAUUSDm --tf H4
    python aqa.py data stats
    python aqa.py strategy list
    python aqa.py backtest --strategy pa_breakout_pullback --symbol XAUUSDm --tf H4
    python aqa.py evaluate --strategy pa_breakout_pullback --symbol XAUUSDm --tf H4
    python aqa.py iterate --hypothesis "结构突破回踩" --rounds 3
    python aqa.py portfolio --symbols XAUUSDm,GBPUSDm --risk 4
    python aqa.py serve
"""

from __future__ import annotations

import csv
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
STRATEGY_DIR = ROOT / "strategies"

# 品种规格抽到 backtest/symbols.py：回测与实盘成本核算必须用同一套点差，
# 两处各存一份必然漂移。见该模块顶部的口径说明。
from backtest.symbols import STRUCT_ATR, STOP_ATR, SYMBOLS, TOL_ATR



# ---------------------------------------------------------------- 工具

def read_bars(path: Path, strict: bool = True):
    """读 CSV 成 Bar 列表。strict=True 时自动切掉与文件名周期不符的头尾段
    （实测券商 XAUUSDm_H4 的 2014-2016 是日线级数据，混进来会污染逐年统计）。"""
    from backtest.dataquality import trim_to_timeframe
    from backtest.models import Bar

    bars = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            bars.append(
                Bar(
                    time=int(r["time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    tick_volume=int(r["tick_volume"]),
                    spread=int(r["spread"]),
                )
            )
    if not strict:
        return bars

    tf = path.stem.rsplit("_", 1)[-1].upper()
    bars, report = trim_to_timeframe(bars, tf)
    if not report.uniform:
        import warnings

        warnings.warn(f"{path.name} 存在粒度漂移：{report.summary()}", stacklevel=2)
    return bars


def data_path(symbol: str, tf: str) -> Path:
    return DATA_DIR / f"{symbol}_{tf.upper()}.csv"


def load_strategy_class(name: str):
    """支持 'pa_breakout_pullback'、'strategies.pa_breakout_pullback'、'模块:类' 三种写法。"""
    if ":" in name:
        module_name, _, cls_name = name.partition(":")
    else:
        module_name = name if name.startswith("strategies.") else f"strategies.{name}"
        cls_name = "Strategy"
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


def strategy_params(symbol: str, extra: dict | None = None) -> dict:
    spec = SYMBOLS.get(symbol)
    if not spec:
        return dict(extra or {})
    atr_pts = spec.get("atr_pts", 0)
    base = {"point": spec["point"]}
    if atr_pts:
        # 结构阈值按 ATR 归一化，保证同一套逻辑在各品种上尺度一致
        base["min_structure_points"] = round(STRUCT_ATR * atr_pts)
        base["min_stop_points"] = round(STOP_ATR * atr_pts)
        base["pullback_tol_points"] = round(TOL_ATR * atr_pts)
    base.update(extra or {})
    return base


def engine_cfg(symbol: str, balance: float = 3000):
    from backtest.engine import EngineConfig

    spec = SYMBOLS.get(symbol, {"point": 0.001, "contract": 100.0, "spread": 180})
    return EngineConfig(
        initial_balance=balance,
        lots=0.1,
        point=spec["point"],
        contract_size=spec["contract"],
        spread_points=spec["spread"],
        # 盈亏以报价货币计价，账户是 USD，非 USD 报价的品种必须换算
        quote_rate=spec.get("quote_rate", 1.0),
        quote_rate_from_price=spec.get("quote_from_price", False),
    )


# ---------------------------------------------------------------- 命令实现

def cmd_data(args) -> None:
    if args.action == "pull":
        import MetaTrader5 as mt5

        from mt5.bridge import get_rates, mt5_session, save_csv

        try:
            with mt5_session():
                rows = get_rates(args.symbol, args.tf, args.count)
                out = save_csv(rows, data_path(args.symbol, args.tf))
                print(f"{args.symbol} {args.tf}: {len(rows)} 根 → {out}")
        except Exception as exc:  # noqa: BLE001
            print(f"失败：{exc}\n提示：需要先打开 MT5 终端并登录")
        return

    # stats
    files = sorted(DATA_DIR.glob("*.csv"))
    if not files:
        print("没有数据文件，先跑 `aqa.py data pull`")
        return
    from datetime import datetime, timezone

    from backtest.dataquality import scan_segments, trim_to_timeframe

    print(f"{'文件':<26}{'原始':>7}{'可用':>7}{'实际粒度':>9}{'起始':>12}{'结束':>12}{'点差':>7}")
    for f in files:
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        tf = f.stem.rsplit("_", 1)[-1].upper()
        bars = read_bars(f, strict=False)
        clean, rep = trim_to_timeframe(bars, tf)
        if not clean:
            print(f"{f.name:<26}{len(bars):>7}{'0':>7}{'不符':>9}  ← 全部与该周期不匹配")
            continue
        segs = scan_segments(clean)
        gaps = sorted({s["median_gap_h"] for s in segs})

        def _gap(g: float) -> str:
            return f"{g * 60:g}m" if g < 1 else f"{g:g}h"

        gap_txt = "/".join(_gap(g) for g in gaps)
        if len(gaps) > 1 or not rep.uniform:
            gap_txt += " ⚠"
        spreads = sorted(b.spread for b in clean)
        med = spreads[len(spreads) // 2]
        f0 = datetime.fromtimestamp(clean[0].time, timezone.utc).strftime("%Y-%m-%d")
        f1 = datetime.fromtimestamp(clean[-1].time, timezone.utc).strftime("%Y-%m-%d")
        flag = ""
        if rep.dropped_head or rep.dropped_tail:
            flag = f"  （已剔除 头{rep.dropped_head}/尾{rep.dropped_tail} 根粒度不符数据）"
        print(f"{f.name:<26}{len(bars):>7}{len(clean):>7}{gap_txt:>9}{f0:>12}{f1:>12}{med:>7}{flag}")


def cmd_strategy(args) -> None:
    if args.action != "list":
        return

    print(f"{'策略':<28}{'说明':<46}参数")
    print("-" * 110)
    for path in sorted(STRATEGY_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            module = importlib.import_module(f"strategies.{name}")
            cls = getattr(module, "Strategy")
        except Exception:  # noqa: BLE001
            continue
        # 说明优先取模块 docstring（策略文件通常在顶部写用途），类 docstring 次之
        raw_doc = (module.__doc__ or cls.__doc__ or "").strip()
        doc = raw_doc.split("\n")[0].strip()[:44]
        sig = inspect.signature(cls.__init__)
        params = ", ".join(
            f"{p.name}={p.default}" for p in sig.parameters.values() if p.name != "self" and p.default is not inspect.Parameter.empty
        )
        print(f"{name:<28}{doc:<46}{params[:60]}")


def cmd_backtest(args) -> None:
    from backtest.engine import run_backtest
    from backtest.metrics import compute_metrics

    path = data_path(args.symbol, args.tf)
    if not path.exists():
        print(f"缺少数据：{path.name}，先跑 `aqa.py data pull --symbol {args.symbol} --tf {args.tf}`")
        return

    from risk import RiskConfig, RiskGuard

    bars = read_bars(path)
    cls = load_strategy_class(args.strategy)
    strategy = cls(**strategy_params(args.symbol, args.param))
    guard = RiskGuard(RiskConfig(risk_per_trade_pct=args.risk, max_drawdown_pct=30)) if args.risk > 0 else None
    result = run_backtest(bars, strategy, engine_cfg(args.symbol, args.balance), guard=guard)
    m = compute_metrics(result)

    risk_txt = f"风控 {args.risk}%" if guard else "固定 0.1 手（无风控）"
    print(f"策略 {args.strategy} | {args.symbol} {args.tf} | {len(bars)} 根 | {risk_txt}")
    for k in ("net_profit", "profit_factor", "total_trades", "win_rate", "avg_win", "avg_loss",
              "max_drawdown", "max_drawdown_pct", "sharpe", "max_win_streak", "max_loss_streak"):
        print(f"  {k:<20}{m[k]}")


def cmd_evaluate(args) -> None:
    from datetime import datetime, timezone

    from backtest.metrics import compute_metrics
    from tools.evaluate import eval_stress, eval_years

    path = data_path(args.symbol, args.tf)
    if not path.exists():
        print(f"缺少数据：{path.name}")
        return

    bars = read_bars(path)
    cls = load_strategy_class(args.strategy)
    params = strategy_params(args.symbol, args.param)
    make = lambda: cls(**params)  # noqa: E731

    print(f"策略 {args.strategy} | {args.symbol} {args.tf} | {len(bars)} 根\n")

    print("=== 逐年 ===")
    print(f"{'年份':<7}{'净盈利':>10}{'笔数':>7}{'胜率':>8}{'PF':>7}{'回撤%':>8}")
    yearly = eval_years(bars, make, args.spread or SYMBOLS.get(args.symbol, {}).get("spread", 180), args.risk, args.balance)
    profitable = 0
    for r in yearly:
        if r["net_profit"] > 0:
            profitable += 1
        print(f"{r['year']:<7}{r['net_profit']:>10.0f}{r['total_trades']:>7}{r['win_rate']:>8.1f}{r['profit_factor'] or 0:>7.2f}{r['max_drawdown_pct']:>8.1f}")

    print("\n=== 成本压力 ===")
    print(f"{'点差':<7}{'净盈利':>10}{'PF':>7}{'回撤%':>8}")
    stress = eval_stress(bars, make, (120, 240, 400, 600), args.risk, args.balance)
    for r in stress:
        print(f"{r['spread']:<7}{r['net_profit']:>10.0f}{r['profit_factor'] or 0:>7.2f}{r['max_drawdown_pct']:>8.1f}")

    avg_pf = sum((r["profit_factor"] or 0) for r in yearly) / len(yearly) if yearly else 0
    worst_dd = max((r["max_drawdown_pct"] for r in yearly), default=100)
    s600 = next((r for r in stress if r["spread"] == 600), None)
    checks = {
        "盈利年份占比 >= 70%": bool(yearly) and profitable / len(yearly) >= 0.7,
        "逐年平均 PF >= 1.2": avg_pf >= 1.2,
        "最差年度回撤 <= 30%": worst_dd <= 30,
        "点差 600 仍盈利": bool(s600 and s600["net_profit"] > 0),
    }

    print("\n=== Gate 2 ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"\n结论：{'通过' if all(checks.values()) else '未通过，需要改策略逻辑'}")

    if args.save:
        out_dir = DATA_DIR / "evaluations"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{args.strategy}_{args.symbol}_{args.tf}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        (out_dir / name).write_text(
            json.dumps(
                {"strategy": args.strategy, "symbol": args.symbol, "tf": args.tf,
                 "yearly": yearly, "stress": stress, "checks": checks, "verdict": all(checks.values())},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已保存：data/evaluations/{name}")


def cmd_iterate(args) -> None:
    from agent.core import run_loop

    print(f"LLM 迭代：{args.rounds} 轮，每轮 2 次 API 调用（生成 + 归因），"
          f"预计 {args.rounds * 2} 次请求。")
    if not args.yes and not _confirm_call("DeepSeek API（发送策略代码与回测指标，每轮 2 次请求）"):
        print("已取消。没有调用 API，不产生费用。")
        return
    run_loop(hypothesis=args.hypothesis, rounds=args.rounds, year=args.year)


def cmd_portfolio(args) -> None:
    from backtest.engine import EngineConfig
    from portfolio import PortfolioItem, run_portfolio
    from risk import RiskConfig
    from strategies.pa_breakout_pullback import Strategy

    items = []
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        path = data_path(sym, args.tf)
        if not path.exists():
            print(f"跳过 {sym}：缺 {path.name}")
            continue
        spec = SYMBOLS[sym]
        params = {**strategy_params(sym, {"rr_target": args.rr})}
        items.append(
            PortfolioItem(
                symbol=sym,
                bars=read_bars(path),
                strategy_factory=lambda p=params: Strategy(**p),
                cfg=EngineConfig(initial_balance=0, lots=0.1, point=spec["point"],
                                 contract_size=spec["contract"], spread_points=spec["spread"]),
                risk=RiskConfig(risk_per_trade_pct=args.risk, max_drawdown_pct=30),
            )
        )

    if not items:
        print("没有可用品种")
        return

    res = run_portfolio(items, total_balance=args.balance, total_risk_pct=args.risk)
    print(f"组合：{', '.join(i.symbol for i in items)} | 资金 {args.balance:.0f} | 总风险 {args.risk}%\n")
    print(f"{'品种':<12}{'收益%':>9}{'PF':>7}{'回撤%':>8}{'笔数':>6}")
    for sym, m in res["per_symbol"].items():
        print(f"{sym:<12}{m['return_pct']:>9.1f}{m['profit_factor'] or 0:>7.2f}{m['max_drawdown_pct']:>8.1f}{m['total_trades']:>6}")

    print(f"\n组合：{res['years']} 年 | 总收益 {res['return_pct']:.1f}% | 年化 {res['cagr']:.2f}% | "
          f"最大回撤 {res['max_drawdown_pct']:.1f}%")
    if res["corr"]:
        print("相关性：" + "，".join(f"{k}={v:.3f}" for k, v in res["corr"].items()))


def cmd_validate(args) -> None:
    """样本外验证：训练段调参，测试段验证。"""
    from datetime import datetime, timezone

    from backtest.engine import run_backtest
    from backtest.metrics import compute_metrics
    from risk import RiskConfig, RiskGuard

    path = data_path(args.symbol, args.tf)
    if not path.exists():
        print(f"缺少数据：{path.name}")
        return

    bars = read_bars(path)
    cls = load_strategy_class(args.strategy)
    params = strategy_params(args.symbol, args.param)
    cfg = engine_cfg(args.symbol, args.balance)

    def year_of(ts: int) -> int:
        return datetime.fromtimestamp(ts, timezone.utc).year

    def parse_range(text: str) -> tuple[int, int]:
        a, _, b = text.partition("-")
        return int(a), int(b or a)

    tr0, tr1 = parse_range(args.train)
    te0, te1 = parse_range(args.test)

    train_bars = [b for b in bars if tr0 <= year_of(b.time) <= tr1]
    test_bars = [b for b in bars if te0 <= year_of(b.time) <= te1]

    if len(train_bars) < 200 or len(test_bars) < 100:
        print(f"数据不足：训练 {len(train_bars)} 根 / 测试 {len(test_bars)} 根")
        return

    print(f"策略 {args.strategy} | {args.symbol} {args.tf}")
    print(f"训练段 {tr0}-{tr1}：{len(train_bars)} 根｜测试段 {te0}-{te1}：{len(test_bars)} 根")
    print(f"风控 {args.risk}%\n")

    results = {}
    print(f"{'段':<8}{'净盈利':>10}{'收益%':>9}{'笔数':>7}{'胜率':>8}{'PF':>7}{'回撤%':>8}")
    for label, subset in (("训练", train_bars), ("测试", test_bars)):
        guard = RiskGuard(RiskConfig(risk_per_trade_pct=args.risk, max_drawdown_pct=30)) if args.risk > 0 else None
        m = compute_metrics(run_backtest(subset, cls(**params), cfg, guard=guard))
        ret = (m["final_balance"] - args.balance) / args.balance * 100
        results[label] = m
        print(f"{label:<8}{m['net_profit']:>10.0f}{ret:>9.1f}{m['total_trades']:>7}"
              f"{m['win_rate']:>8.1f}{m['profit_factor'] or 0:>7.2f}{m['max_drawdown_pct']:>8.1f}")

    tr, te = results["训练"], results["测试"]
    print("\n=== 判定 ===")
    if tr["net_profit"] > 0 and te["net_profit"] > 0:
        pf_drop = (tr["profit_factor"] or 0) - (te["profit_factor"] or 0)
        if pf_drop > 0.5:
            print(f"⚠ 两段都盈利，但测试段 PF 下降 {pf_drop:.2f} —— 有衰减迹象，需更多样本确认")
        else:
            print("✓ 两段都盈利且衰减可接受 —— 策略有真实 edge，可进入 demo 验证")
    elif tr["net_profit"] > 0 and te["net_profit"] <= 0:
        print("✗ 训练段盈利、测试段亏损 —— 典型过拟合，策略不具备样本外能力")
    elif tr["net_profit"] <= 0 and te["net_profit"] > 0:
        print("? 训练段亏损、测试段盈利 —— 可能只是运气，不可信")
    else:
        print("✗ 两段都亏损 —— 策略本身无效")


def cmd_sweep(args) -> None:
    """参数稳健性扫描——回答「参数是不是踩在尖峰上」。"""
    import subprocess
    import sys

    cmd = [
        sys.executable, "-m", "tools.sweep",
        "--strategy", args.strategy, "--symbol", args.symbol, "--tf", args.tf,
        "--balance", str(args.balance), "--risk", str(args.risk),
        "--train", args.train, "--test", args.test,
    ]
    for g in args.grid:
        cmd += ["--grid", g]
    subprocess.run(cmd, check=False)


def _confirm_call(what: str = "DeepSeek API") -> bool:
    """调用付费 API 前的终端确认。非交互环境（EOF/无 stdin）一律当作取消。"""
    print(f"  ⚠ 即将调用 {what}，会产生费用。")
    try:
        answer = input("  确认继续？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes", "是")


def cmd_live(args) -> None:
    """实盘复盘：账户 + 逐笔统计 + 风险标记 + 可选 AI 建议。"""
    import time as _time

    from live.advisor import advise
    from live.journal import collect, collect_offline
    from live.stats import compute, risk_flags

    def once() -> None:
        # --account 看的是磁盘上归档的历史账户，不连 MT5（那个账户已经登不上）
        if getattr(args, "account", None):
            info = collect_offline(args.account)
        else:
            info = collect(force=True)
        if not info.get("ok"):
            print(f"读取失败：{info.get('error')}")
            return
        a = info["account"]
        s = compute(info["trades"], a, info["deposits"], info.get("flows"))
        flags = risk_flags(s)

        if args.json:
            print(json.dumps({"account": a, "stats": s, "flags": flags}, ensure_ascii=False, indent=2, default=str))
            return

        mode = {"real": "实盘", "demo": "模拟"}.get(a.get("trade_mode"), a.get("trade_mode"))
        tag = "  [归档 · 离线]" if info.get("offline") else ""
        print(f"账户 {a['login']} @ {a['server']}（{mode}）{tag}")
        print(f"  余额 {a['balance']} / 净值 {a['equity']} / 可用 {a['margin_free']} {a['currency']}")
        print(f"  持仓 {len(info['positions'])} 个，浮动盈亏 {a['profit']:+.2f}")
        if s.get("empty"):
            print("  账本里还没有已平仓交易")
            return

        print(f"\n=== 交易统计（{s['count']} 笔已平仓 / 跨 {s['span_days']} 天 / 实际 {s['active_days']} 天）===")
        print(f"  净盈亏 {s['net']:+.2f}   胜率 {s['win_rate']}%   盈利因子 {s['pf']}")
        print(f"  平均盈利 {s['avg_win']:+.2f} / 平均亏损 {s['avg_loss']:+.2f} → 盈亏比 {s['risk_reward']}")
        print(
            f"  单笔期望 {s['expectancy']:+.2f}（{s['expectancy_r']}R）"
            f"   最大回撤 {s['max_dd']:.2f}"
            f"（{s['dd_peak']:.2f} → {s['trough_balance']:.2f}，占峰值 {s['max_dd_pct']}%）"
        )
        if s.get("wipeouts"):
            print(f"  ⚠ 账户被清零 {s['wipeouts']} 次（历史峰值余额 {s['peak_balance']:.2f}）")
        print(f"  持仓中位数 {s['hold_median_min']} 分钟   最长连亏 {s['max_loss_streak']} 笔")
        print(f"  净入金 {s['deposits']:.2f}   收益率 {s['return_pct']}%")
        t = s["today"]
        print(f"  今日：{t['trades']} 笔，{t['profit']:+.2f}，胜率 {t.get('win_rate', 0)}%")

        print("\n=== 分方向 ===")
        for r in s["by_direction"]:
            print(f"  {r['key']}  {r['trades']:>3} 笔  胜率 {r['win_rate']:>5}%  净 {r['net']:>+9.2f}  PF {r['pf']}")
        print("=== 分平仓原因 ===")
        for r in s["by_reason"]:
            print(f"  {r['key']:<8} {r['trades']:>3} 笔  净 {r['net']:>+9.2f}")

        q = info.get("quality") or {}
        if q.get("windows"):
            vmap = {
                "better": "变好", "worse": "变差", "flat": "持平",
                "low_sample": "样本不足", "no_baseline": "无前序", "no_data": "无交易",
            }
            print(f"\n=== 近期交易质量（1R = {q['risk_unit']:.2f}）===")
            print(f"{'窗口':<9}{'笔数':>5}{'期望R':>8}{'PF':>7}{'胜率':>8}{'95%CI':>13}{'回撤R':>8}{'判定':>9}")
            for w in q["windows"]:
                c = w["current"]
                ci = f"[{c['win_ci'][0]:.0f},{c['win_ci'][1]:.0f}]"
                print(
                    f"{w['label']:<9}{c['n']:>5}{c['expectancy_r']:>8.3f}"
                    f"{(c['pf'] or 0):>7.2f}{c['win_rate']:>8.1f}{ci:>13}{c['dd_r']:>8.2f}"
                    f"{vmap.get(w['verdict'], w['verdict']):>9}"
                )
            low = [w["label"] for w in q["windows"] if w["current"]["low_sample"]]
            if low:
                print(f"  ⚠ 样本不足 {q['min_sample']} 笔：{'、'.join(low)}（不可据此决策）")

        sc = info.get("scoring") or {}
        if sc.get("dimensions"):
            from live.scoring import grade_text

            print(f"\n=== 交易评分 ===")
            print(f"  综合等级 {sc['grade']}（{grade_text(sc['grade'])}）  参考分 {sc.get('score')}  "
                  f"单笔均分 {sc.get('avg_trade_score')}")
            for d in sc["dimensions"]:
                print(f"    {d['grade']}  {d['label']:<8}{d['display']:<16} 目标 {d['target']}")
            dev = sc.get("breakeven") or {}
            if dev.get("margin_pp") is not None:
                print(
                    f"  盈亏平衡：盈亏比 {dev['rr']} → 需胜率 {dev['required_win_rate']}%  "
                    f"实际 {dev['actual_win_rate']}%  边际 {dev['margin_pp']:+.1f} pp"
                )
            plan = sc.get("plan") or {}
            if plan.get("avg_planned_rr"):
                print(f"  计划盈亏比均值 {plan['avg_planned_rr']} → 实际实现 {plan.get('realized_rr')}")
            elif plan.get("note"):
                print(f"  {plan['note']}")
            for f in sc.get("red_flags") or []:
                print(f"  ⚠ {f}")

        print("\n=== 风险标记 ===")
        for f in flags:
            print(f"  [{f['level']:<4}] {f['title']} — {f['detail']}")

        if args.ai:
            print("\n=== AI 复盘 ===")
            if not args.yes and not _confirm_call("DeepSeek API（发送当前账户统计以生成复盘）"):
                print("  已取消。没有调用 API，不产生费用。")
                return
            res = advise(s, flags, info["trades"], force=args.force, allow_call=True,
                         scoring=info.get("scoring"), login=info.get("login"))
            if not res.get("ok"):
                print(f"  生成失败：{res.get('error')}")
            else:
                adv = res["advice"]
                print(f"  结论：{adv.get('headline')}（{adv.get('verdict')}）")
                for p in adv.get("problems", []):
                    print(f"  [{p.get('severity')}] {p.get('title')}")
                    print(f"      证据：{p.get('evidence')}")
                for x in sorted(adv.get("actions", []), key=lambda y: y.get("priority", 99)):
                    print(f"  {x.get('priority')}. {x.get('action')}")
                    print(f"      验证：{x.get('how_to_verify')}")
                if adv.get("note"):
                    print(f"  样本说明：{adv['note']}")

    if not args.watch:
        once()
        return

    print(f"实盘监控：每 {args.interval}s 同步一次，Ctrl+C 停止\n")
    while True:
        try:
            once()
            print(f"\n--- {time.strftime('%H:%M:%S')} 已同步，{args.interval}s 后继续 ---\n")
            _time.sleep(args.interval)
        except KeyboardInterrupt:
            print("已停止")
            return


def cmd_accounts(args) -> None:
    """多账户管理：列出已归档账户 / 补登记元信息。

    切换账户的做法是在 **MT5 终端里切**，这里自动识别并归档到对应目录。
    `add` 只用于补登记一个「已经不在终端里、但本地有归档数据」的老账户——
    正常情况不需要手动跑。
    """
    import time as _t

    from live import paths

    if args.action == "list":
        rows = paths.list_accounts()
        if not rows:
            print("还没有任何账户归档。先在 MT5 终端登录，然后跑 `aqa live` 同步一次。")
            return
        try:
            from live.journal import probe_login

            cur = probe_login()
        except Exception:  # noqa: BLE001
            cur = None
        if cur is None:
            print("（MT5 未运行或未登录，「当前」一列不显示）")
        cur = str(cur) if cur else None
        print(f"{'账户':<12}{'服务器':<22}{'类型':<8}{'余额':>10}{'成交':>7}  最后活跃")
        for r in rows:
            acc = paths.read_json(r["login"], "account.json", {}) or {}
            ledger = paths.account_dir(r["login"]) / "deals.jsonl"
            n = 0
            if ledger.exists():
                n = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip())
            mark = " ← 当前" if str(r["login"]) == str(cur) else ""
            print(
                f"{str(r['login']):<12}{str(r.get('server') or '?'):<22}"
                f"{str(r.get('trade_mode') or '?'):<8}{r.get('last_balance') or 0:>10}"
                f"{n:>7}  {_t.strftime('%m-%d %H:%M', _t.localtime(r.get('last_seen') or 0))}{mark}"
            )
        return

    if args.action == "add":
        if not args.login:
            print("需要账户号：aqa accounts add <login> --server <服务器>")
            return
        prev = paths.load_index().get(str(args.login)) or {}
        paths.touch_account({
            "login": int(args.login) if str(args.login).isdigit() else args.login,
            "server": args.server or prev.get("server") or "unknown",
            "name": args.name or prev.get("name"),
            "currency": args.currency or prev.get("currency") or "USD",
            "trade_mode": args.mode or prev.get("trade_mode") or "real",
            "balance": prev.get("last_balance"),
            "equity": prev.get("last_equity"),
        })
        print(f"已登记 {args.login} @ {args.server or prev.get('server') or 'unknown'}")
        print(f"  数据目录 {paths.account_dir(args.login)}")
        return

    if args.action == "show":
        login = args.login or paths.current_login()
        if not login:
            print("没有账户。")
            return
        acc = paths.read_json(login, "account.json", {}) or {}
        print(json.dumps({"index": paths.load_index().get(str(login)) or {},
                          "account": acc,
                          "dir": str(paths.account_dir(login))},
                         ensure_ascii=False, indent=2, default=str))
        return


def cmd_doctor(args) -> None:
    """环境自检：MT5 通不通、账本在哪、面板在不在。

    专门为了回答「同步失败到底卡在哪一步」。MT5 的 GUI 没法从 agent 进程拉起来
    （terminal64 / MetaEditor64 / metatester64 全部 0xC0000139），所以这里只能
    把状态摆清楚，让人一眼看出是该手动开终端，还是终端开着但连不上。
    """
    import socket
    import time as _t

    from live import journal, paths

    mt5_exe = journal.terminal_path()
    print("=== MT5 终端 ===")
    print(f"  安装路径  {mt5_exe or '【没找到 terminal64.exe】'}")
    alive = journal._terminal_alive()
    print(f"  进程状态  {'运行中' if alive else '未运行'}")
    if not alive:
        print("  正在尝试启动…", end="", flush=True)
        launched = journal.ensure_terminal()
        print("成功" if launched else "失败")
        alive = journal._terminal_alive()

    print("\n=== 连接 ===")
    info = journal.collect(force=True)
    if info.get("ok"):
        a = info["account"]
        print(
            f"  [OK] {a['login']} @ {a['server']}  余额 {a['balance']} {a['currency']}"
            f"  持仓 {len(info['positions'])} 个  账本 {len(info['trades'])} 笔"
        )
    else:
        print(f"  [失败] {info.get('error')}")
        if not alive:
            print("  -> 手动双击 MetaTrader 5 启动，再跑一次本命令。")
        else:
            print("  -> 终端在跑但管道没响应：确认终端已登录账户，别停在登录框。")

    print("\n=== 账本 ===")
    print(f"  数据目录  {paths.LIVE_DIR}")
    rows = paths.list_accounts()
    if not rows:
        print("  （还没有任何账户归档）")
    for r in rows:
        acc = paths.read_json(r["login"], "account.json", {}) or {}
        last = _t.strftime("%m-%d %H:%M", _t.localtime(r.get("last_seen") or 0))
        print(
            f"  {r['login']}  {str(r.get('server') or '?'):<20}"
            f"余额 {acc.get('balance')}  最后同步 {last}"
        )

    print("\n=== 面板 ===")
    host, port = "127.0.0.1", 8765
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"  [OK] http://{host}:{port} 正在监听")
    except OSError:
        print(f"  [未运行] 启动：start-dashboard.bat 或 python -m tools.dashboard")

    if not alive:
        print(
            "\n结论：MT5 关着。它不是本程序能启动的——GUI 从 agent 进程拉起会 0xC0000139。\n"
            "      手动打开终端后，面板 20 秒内自动连上，不用重启任何东西。"
        )


def cmd_serve(args) -> None:
    from tools.dashboard import main as dash_main

    sys.argv = ["dashboard", "--port", str(args.port)] + (["--no-open"] if args.no_open else [])
    dash_main()


# ---------------------------------------------------------------- CLI

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="aqa", description="AI Quant Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # data
    p_data = sub.add_parser("data", help="数据管理")
    p_data.add_argument("action", choices=["pull", "stats"])
    p_data.add_argument("--symbol", default="XAUUSDm")
    p_data.add_argument("--tf", default="H4", choices=["M1", "M5", "M15", "M30", "H1", "H4", "D1"])
    p_data.add_argument("--count", type=int, default=50000)
    p_data.set_defaults(func=cmd_data)

    # strategy
    p_strat = sub.add_parser("strategy", help="策略库")
    p_strat.add_argument("action", choices=["list"])
    p_strat.set_defaults(func=cmd_strategy)

    # backtest
    p_bt = sub.add_parser("backtest", help="单策略回测")
    p_bt.add_argument("--strategy", default="pa_breakout_pullback")
    p_bt.add_argument("--symbol", default="XAUUSDm")
    p_bt.add_argument("--tf", default="H4")
    p_bt.add_argument("--balance", type=float, default=3000)
    p_bt.add_argument("--param", action="append", default=[], help="k=v，可多次")
    p_bt.add_argument("--risk", type=float, default=2.0, help="单笔风险 %%，0=关闭风控")
    p_bt.set_defaults(func=cmd_backtest)

    # evaluate
    p_ev = sub.add_parser("evaluate", help="逐年 + 成本压力 + Gate 2")
    p_ev.add_argument("--strategy", default="pa_breakout_pullback")
    p_ev.add_argument("--symbol", default="XAUUSDm")
    p_ev.add_argument("--tf", default="H4")
    p_ev.add_argument("--spread", type=int, default=None)
    p_ev.add_argument("--param", action="append", default=[])
    p_ev.add_argument("--save", action="store_true")
    p_ev.add_argument("--balance", type=float, default=3000)
    p_ev.add_argument("--risk", type=float, default=2.0)
    p_ev.set_defaults(func=cmd_evaluate)

    # iterate
    p_it = sub.add_parser("iterate", help="LLM 自动迭代")
    p_it.add_argument("--hypothesis", required=True)
    p_it.add_argument("--rounds", type=int, default=3)
    p_it.add_argument("--year", type=int, default=2025)
    p_it.add_argument("--yes", action="store_true", help="跳过付费 API 的确认提示（脚本用）")
    p_it.set_defaults(func=cmd_iterate)

    # portfolio
    p_pf = sub.add_parser("portfolio", help="多品种组合")
    p_pf.add_argument("--symbols", default="XAUUSDm,GBPUSDm")
    p_pf.add_argument("--tf", default="H4")
    p_pf.add_argument("--balance", type=float, default=3000)
    p_pf.add_argument("--risk", type=float, default=4)
    p_pf.add_argument("--rr", type=float, default=3)
    p_pf.set_defaults(func=cmd_portfolio)

    # validate
    p_va = sub.add_parser("validate", help="样本外验证（训练段 / 测试段）")
    p_va.add_argument("--strategy", default="pa_breakout_pullback")
    p_va.add_argument("--symbol", default="XAUUSDm")
    p_va.add_argument("--tf", default="H4")
    p_va.add_argument("--train", default="2017-2023")
    p_va.add_argument("--test", default="2024-2026")
    p_va.add_argument("--balance", type=float, default=3000)
    p_va.add_argument("--risk", type=float, default=2.0)
    p_va.add_argument("--param", action="append", default=[])
    p_va.set_defaults(func=cmd_validate)

    # sweep
    p_sw = sub.add_parser("sweep", help="参数稳健性扫描（训练段选参，测试段验证）")
    p_sw.add_argument("--strategy", default="pa_breakout_pullback")
    p_sw.add_argument("--symbol", default="XAUUSDm")
    p_sw.add_argument("--tf", default="H4")
    p_sw.add_argument("--train", default="2017-2023")
    p_sw.add_argument("--test", default="2024-2026")
    p_sw.add_argument("--grid", action="append", default=[], help="k=v1,v2，可多次")
    p_sw.add_argument("--balance", type=float, default=3000)
    p_sw.add_argument("--risk", type=float, default=2.0)
    p_sw.set_defaults(func=cmd_sweep)

    # live
    p_lv = sub.add_parser("live", help="实盘复盘（读 MT5 账户成交，统计 + AI 建议）")
    p_lv.add_argument("--ai", action="store_true", help="调用 LLM 生成复盘建议（会先要求确认）")
    p_lv.add_argument("--yes", action="store_true", help="跳过付费 API 的确认提示（脚本用）")
    p_lv.add_argument("--force", action="store_true", help="忽略缓存重新生成 AI 建议")
    p_lv.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    p_lv.add_argument("--watch", action="store_true", help="常驻循环同步")
    p_lv.add_argument("--interval", type=int, default=300)
    p_lv.add_argument("--account", default=None,
                      help="查看归档的某个账户（离线读磁盘，不连 MT5）。默认=终端当前登录的")
    p_lv.set_defaults(func=cmd_live)

    # accounts
    p_ac = sub.add_parser("accounts", help="多账户管理（在 MT5 终端里切账户，这里自动归档）")
    p_ac.add_argument("action", choices=["list", "add", "show"], help="list=列出 / add=补登记 / show=详情")
    p_ac.add_argument("login", nargs="?", default=None)
    p_ac.add_argument("--server", default=None)
    p_ac.add_argument("--name", default=None)
    p_ac.add_argument("--currency", default=None)
    p_ac.add_argument("--mode", default=None, choices=["demo", "contest", "real"])
    p_ac.set_defaults(func=cmd_accounts)

    # doctor
    p_doc = sub.add_parser("doctor", help="环境自检（MT5 进程与连接、账本、面板）")
    p_doc.set_defaults(func=cmd_doctor)

    # serve
    p_sv = sub.add_parser("serve", help="启动监控面板")
    p_sv.add_argument("--port", type=int, default=8765)
    p_sv.add_argument("--no-open", action="store_true")
    p_sv.set_defaults(func=cmd_serve)

    args = parser.parse_args()

    # --param k=v 解析
    if hasattr(args, "param"):
        parsed = {}
        for item in args.param:
            k, _, v = item.partition("=")
            low = v.lower()
            if low in ("true", "false"):
                parsed[k.strip()] = low == "true"
            else:
                try:
                    parsed[k.strip()] = int(v)
                except ValueError:
                    try:
                        parsed[k.strip()] = float(v)
                    except ValueError:
                        parsed[k.strip()] = v
        args.param = parsed

    args.func(args)


if __name__ == "__main__":
    main()
