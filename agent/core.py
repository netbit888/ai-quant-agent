"""Agent 主循环：生成策略 → 回测 → 归因 → 迭代

用法：
    python -m agent.core --hypothesis "基于双均线交叉的趋势跟踪，加波动率过滤" --rounds 3

产物：
    strategies/generated/round_NN.py   每轮生成的策略代码
    docs/iteration-log.md              追加到迭代日志
    data/runs/<时间戳>/                 每轮的指标 JSON
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.llm import chat
from agent.prompts import ANALYZE, GENERATE, STRATEGY_INTERFACE, SYSTEM
from agent.sandbox import extract_code, extract_json, load_strategy, validate_constraints
from backtest.engine import EngineConfig, run_backtest
from backtest.metrics import compute_metrics
from backtest.models import Bar

ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / "strategies" / "generated"
LOG_FILE = ROOT / "docs" / "iteration-log.md"
EXAMPLE_FILE = ROOT / "strategies" / "macd_cross.py"

DEFAULT_CONFIG = EngineConfig(
    initial_balance=10_000,
    lots=0.1,
    point=0.001,
    contract_size=100,
    spread_points=240,  # 用真实点差，不用 MT5 测试器的"当前点差"
)


def load_bars(csv_path: Path, year: int | None = 2025) -> list[Bar]:
    bars: list[Bar] = []
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    time=int(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread=int(row["spread"]),
                )
            )
    if year is not None:
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        bars = [b for b in bars if start <= b.time < end]
    return bars


def format_metrics(metrics: dict) -> str:
    keys = [
        "net_profit",
        "profit_factor",
        "total_trades",
        "win_rate",
        "avg_win",
        "avg_loss",
        "max_drawdown",
        "max_drawdown_pct",
        "sharpe",
        "expected_payoff",
    ]
    return "\n".join(f"- {k}: {metrics.get(k)}" for k in keys)


def format_history(history: list[dict]) -> str:
    if not history:
        return "（这是第一轮，没有历史）"
    lines = []
    for item in history[-3:]:
        m = item["metrics"]
        lines.append(
            f"第 {item['round']} 轮：净盈利 {m.get('net_profit')}，"
            f"交易 {m.get('total_trades')} 笔，胜率 {m.get('win_rate')}%，"
            f"盈利因子 {m.get('profit_factor')}，最大回撤 {m.get('max_drawdown_pct')}%"
        )
        if item.get("analysis", {}).get("diagnosis"):
            lines.append(f"  诊断：{item['analysis']['diagnosis']}")
    return "\n".join(lines)


def generate_strategy(hypothesis: str, history: list[dict]) -> tuple[str, object]:
    """让 LLM 生成策略代码并加载；失败时把错误回喂一次。"""
    example = EXAMPLE_FILE.read_text(encoding="utf-8") if EXAMPLE_FILE.exists() else ""
    prompt = GENERATE.format(
        system=SYSTEM,
        interface=STRATEGY_INTERFACE,
        example=example,
        hypothesis=hypothesis,
        history_block=format_history(history),
    )
    code = extract_code(chat(prompt))

    try:
        return code, load_strategy(code)
    except Exception as first_err:  # noqa: BLE001
        fix_prompt = (
            f"{prompt}\n\n你上一版代码报错了：\n{type(first_err).__name__}: {first_err}\n\n"
            "请修正后重新输出完整代码。"
        )
        code = extract_code(chat(fix_prompt))
        return code, load_strategy(code)


def append_log(round_no: int, hypothesis: str, code: str, metrics: dict, analysis: dict, constraints: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    block = f"""
## Round {round_no}（{time.strftime('%Y-%m-%d %H:%M')}）

- 策略假设：{hypothesis}
- 代码：`strategies/generated/round_{round_no:02d}.py`
- 约束校验：{'通过' if constraints['ok'] else '; '.join(constraints['issues'])}
- 回测指标：
{format_metrics(metrics)}
- LLM 诊断：{analysis.get('diagnosis', '')}
- 下一轮假设：{analysis.get('next_hypothesis', '')}
"""
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(block)


STATE_FILE = ROOT / "data" / "current_state.json"


def _write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_loop(hypothesis: str, rounds: int = 3, csv_path: Path | None = None, year: int = 2025) -> list[dict]:
    csv_path = csv_path or (ROOT / "data" / "XAUUSDm_H1.csv")
    bars = load_bars(csv_path, year)
    if len(bars) < 500:
        raise RuntimeError(f"{year} 年只有 {len(bars)} 根 bar，数据不足")

    run_dir = ROOT / "data" / "runs" / time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    history: list[dict] = []
    current = hypothesis
    t_start = time.time()

    state: dict = {
        "status": "running",
        "phase": "starting",
        "current_round": 0,
        "total_rounds": rounds,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": 0,
        "hypothesis": hypothesis,
        "year": year,
        "bars": len(bars),
        "spread_points": DEFAULT_CONFIG.spread_points,
        "run_dir": str(run_dir),
        "rounds": [],
    }

    def push(**kwargs) -> None:
        state.update(kwargs)
        state["elapsed_s"] = round(time.time() - t_start, 1)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_state(state)

    push()

    print(f"数据：{len(bars)} 根 bar（{year} 年）")
    print(f"点差：{DEFAULT_CONFIG.spread_points} points，手数：{DEFAULT_CONFIG.lots}")
    print("=" * 60)

    try:
        for round_no in range(1, rounds + 1):
            print(f"\n[Round {round_no}] 生成策略中…… 假设：{current}")
            push(phase="generating", current_round=round_no, hypothesis=current)
            t0 = time.time()

            code, strategy = generate_strategy(current, history)
            code_path = GENERATED_DIR / f"round_{round_no:02d}.py"
            code_path.write_text(code, encoding="utf-8")

            push(phase="backtesting")
            # 校验前必须先 prepare：sl_tp() 依赖已算好的指标，
            # 否则会返回 None 并被误判为"没有止盈/止损"，连带把 LLM 的诊断带偏。
            strategy.prepare(bars)
            constraints = validate_constraints(strategy, DEFAULT_CONFIG)
            result = run_backtest(bars, strategy, DEFAULT_CONFIG)
            metrics = compute_metrics(result)

            print(
                f"  回测：净盈利 {metrics['net_profit']} | 交易 {metrics['total_trades']} 笔 | "
                f"胜率 {metrics['win_rate']}% | 回撤 {metrics['max_drawdown_pct']}%"
            )
            if not constraints["ok"]:
                print(f"  ⚠ 约束问题：{'; '.join(constraints['issues'])}")

            push(phase="analyzing")
            analysis = extract_json(
                chat(
                    ANALYZE.format(
                        system=SYSTEM,
                        code=code,
                        metrics=format_metrics(metrics),
                        constraints="通过" if constraints["ok"] else "; ".join(constraints["issues"]),
                        history_block=format_history(history),
                    )
                )
            )

            print(f"  诊断：{analysis.get('diagnosis', '')[:100]}")
            print(f"  下一轮：{analysis.get('next_hypothesis', '')[:100]}")

            history.append(
                {
                    "round": round_no,
                    "hypothesis": current,
                    "code_file": str(code_path.relative_to(ROOT)).replace("\\", "/"),
                    "metrics": metrics,
                    "constraints": constraints,
                    "analysis": analysis,
                }
            )
            append_log(round_no, current, code, metrics, analysis, constraints)
            (run_dir / f"round_{round_no:02d}.json").write_text(
                json.dumps(history[-1], ensure_ascii=False, indent=2), encoding="utf-8"
            )

            state["rounds"].append(
                {
                    "round": round_no,
                    "hypothesis": current,
                    "code_file": history[-1]["code_file"],
                    "net_profit": metrics.get("net_profit"),
                    "total_trades": metrics.get("total_trades"),
                    "win_rate": metrics.get("win_rate"),
                    "profit_factor": metrics.get("profit_factor"),
                    "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                    "diagnosis": analysis.get("diagnosis", ""),
                    "next_hypothesis": analysis.get("next_hypothesis", ""),
                    "constraints_ok": constraints["ok"],
                }
            )

            current = analysis.get("next_hypothesis") or current
            push(phase="round done")
            print(f"  用时 {time.time() - t0:.1f}s")

        push(status="done", phase="finished")
    except Exception as exc:  # noqa: BLE001
        push(status="error", phase=f"error: {type(exc).__name__}: {exc}")
        raise

    (run_dir / "summary.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"完成 {rounds} 轮，结果目录：{run_dir}")
    print(f"状态文件：{STATE_FILE}（可用于面板）")
    return history


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AI 量化策略迭代")
    parser.add_argument("--hypothesis", required=True, help="初始策略假设")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--csv", default=None, help="K 线 CSV 路径")
    parser.add_argument("--year", type=int, default=2025)
    args = parser.parse_args()

    run_loop(
        hypothesis=args.hypothesis,
        rounds=args.rounds,
        csv_path=Path(args.csv) if args.csv else None,
        year=args.year,
    )


if __name__ == "__main__":
    main()
