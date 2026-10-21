"""EA 编译 + 回测触发 + 报告定位

MT5 的硬限制：同一数据目录只允许一个终端实例。
终端已经开着时，`terminal64.exe /config:xxx.ini` 会被静默忽略（进程直接退出），
所以本模块默认检测到终端运行就报错，要求先关终端；传 force_close=True 才会自动结束进程。

用法：
    编译：
        python -m mt5.backtest --compile ../path/to/MyEA.mq5
    编译 + 回测：
        python -m mt5.backtest --compile ../path/to/MyEA.mq5 --run --symbol XAUUSDm --period H1 \
            --from 2026.08.01 --to 2026.09.30 --force-close
    只回测已有 EA（Expert 路径相对 MQL5\\Experts，不带扩展名）：
        python -m mt5.backtest --run --expert Advisors\\ExpertMACD --force-close
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TERMINAL_EXE = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")
EDITOR_EXE = Path(r"C:\Program Files\MetaTrader 5\MetaEditor64.exe")


# ---------------------------------------------------------------- 路径定位

def find_data_path() -> Path:
    """定位 MT5 终端数据目录（hash 名会变，用 MQL5\\Experts 存在性判断）。"""
    root = Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal"
    candidates = [p for p in root.iterdir() if (p / "MQL5" / "Experts").exists()]
    if not candidates:
        raise RuntimeError(f"找不到 MT5 数据目录: {root}")
    return max(candidates, key=lambda p: (p / "MQL5").stat().st_mtime)


# ---------------------------------------------------------------- 编译

def compile_ea(src: Path, dest_subdir: str = "agent_gen") -> dict:
    """编译 .mq5：补 BOM → 复制到 MQL5\\Experts → MetaEditor 命令行编译 → 解析日志。

    注意：MetaEditor 的 returncode 恒为 1，唯一判据是日志里的 Result 行。
    """
    src = Path(src).resolve()
    if not src.exists():
        raise FileNotFoundError(src)

    data_path = find_data_path()
    dst_dir = data_path / "MQL5" / "Experts" / dest_subdir
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name

    raw = src.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raw = b"\xef\xbb\xbf" + raw
    dst.write_bytes(raw)

    log = dst.with_suffix(".log")
    if log.exists():
        log.unlink()

    subprocess.run(
        [str(EDITOR_EXE), f"/compile:{dst}", f"/log:{log}"],
        capture_output=True,
        timeout=120,
    )

    for _ in range(40):  # 最多等 20 秒
        if log.exists() and log.stat().st_size > 0:
            break
        time.sleep(0.5)

    text = log.read_bytes().decode("utf-16-le", errors="replace") if log.exists() else ""
    match = re.search(r"(\d+)\s+errors?,\s+(\d+)\s+warnings?", text)
    errors = int(match.group(1)) if match else -1
    warnings = int(match.group(2)) if match else -1

    ex5 = dst.with_suffix(".ex5")
    interesting = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"error|warning|Result:", line, re.I)
    ][:20]

    return {
        "ok": ex5.exists() and errors == 0,
        "errors": errors,
        "warnings": warnings,
        "ex5": str(ex5) if ex5.exists() else None,
        "expert_rel": f"{dest_subdir}\\{src.stem}",
        "log_lines": interesting,
    }


# ---------------------------------------------------------------- 回测

def terminal_running() -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )
    return "terminal64.exe" in (out.stdout or "").lower()


def tester_log_hint(lines: int = 8) -> str:
    """从最新 Tester 日志提取关键行，失败时用来定位（数据范围 / OnInit / 品种缺失）。"""
    log_dir = find_data_path() / "Tester" / "logs"
    files = sorted(log_dir.glob("*.log"))
    if not files:
        return "（无 Tester 日志）"
    try:
        text = files[-1].read_bytes().decode("utf-16-le", errors="replace")
    except OSError:
        return "（日志读取失败）"
    keys = re.compile(
        r"history cache|history begins|testing of|OnInit|stopped because|Unknown symbol|math calculations",
        re.I,
    )
    picked = [line.strip() for line in text.splitlines() if keys.search(line)]
    return "\n".join(picked[-lines:]) if picked else "（日志中无关键行）"


def run_backtest(
    expert_rel: str,
    symbol: str = "XAUUSDm",
    period: str = "H1",
    from_date: str = "2025.01.01",
    to_date: str = "2026.07.31",
    deposit: float = 10_000,
    currency: str = "USD",
    leverage: str = "1:200",
    model: int = 2,  # 0=每tick 1=1分钟OHLC 2=仅开盘价 3=数学计算 4=真实tick
    timeout_s: int = 300,
    force_close: bool = False,
    report_name: str | None = None,
) -> Path:
    """启动无头回测，返回报告文件路径。

    Model 选择（实测结论）：
      2（仅开盘价）——默认。有完整 symbol info，不依赖 M1 数据，速度快
      1（1分钟OHLC）——更接近真实，要求 M1 历史完整
      3（数学计算）——**不可用**：该模式不提供 history 和 symbol info，
                        任何用 CSymbolInfo 的 EA 都会在 OnInit 里失败退出
    """
    if terminal_running():
        if not force_close:
            raise RuntimeError(
                "MT5 终端正在运行，/config 会被忽略。\n"
                "请先手动关闭 MT5，或传 force_close=True 自动结束进程。"
            )
        subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], capture_output=True)
        time.sleep(3)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = report_name or f"{expert_rel.split(chr(92))[-1]}_{symbol}_{period}_{stamp}"
    report_base = DATA_DIR / "reports" / name
    report_base.parent.mkdir(parents=True, exist_ok=True)

    ini_dir = DATA_DIR / "tester"
    ini_dir.mkdir(parents=True, exist_ok=True)
    ini = ini_dir / f"{name}.ini"
    ini.write_text(
        "\n".join(
            [
                "[Tester]",
                f"Expert={expert_rel}",
                f"Symbol={symbol}",
                f"Period={period}",
                "Optimization=0",
                f"Model={model}",
                f"FromDate={from_date}",
                f"ToDate={to_date}",
                "ForwardMode=0",
                f"Deposit={deposit}",
                f"Currency={currency}",
                f"Leverage={leverage}",
                "ExecutionMode=0",
                "Visual=0",
                "ShutdownTerminal=1",
                "ReplaceReport=1",
                f"Report={report_base}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [str(TERMINAL_EXE), f"/config:{ini}"],
        cwd=str(TERMINAL_EXE.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _find_report() -> Path | None:
        for suffix in (".htm", ".html", ".xml"):
            candidate = Path(str(report_base) + suffix)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found = _find_report()
        if found:
            time.sleep(1)  # 等文件写完
            return found
        if proc.poll() is not None:
            # 终端已退出：给它 3 秒落盘，再判失败
            time.sleep(3)
            found = _find_report()
            if found:
                return found
            raise RuntimeError(
                "终端已退出但报告未生成。\n"
                f"最近的测试器日志：\n{tester_log_hint()}\n"
                "常见原因：\n"
                "  1) 区间内没有历史数据 —— 按日志里 'history cache ... from X to Y' 调整 --from/--to\n"
                "  2) Model=3 不提供 symbol info —— 改用 Model=2\n"
                "  3) 品种名不对（注意后缀 m）"
            )
        time.sleep(2)

    raise TimeoutError(f"回测超时（{timeout_s}s），检查 {DATA_DIR / 'tester'} 与终端日志")


# ---------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="EA 编译 + 无头回测")
    parser.add_argument("--compile", metavar="MQ5", help="编译指定 .mq5 文件")
    parser.add_argument("--run", action="store_true", help="执行回测")
    parser.add_argument("--expert", help=r"相对 MQL5\Experts 的路径，不带扩展名（如 Advisors\ExpertMACD）")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--period", default="H1")
    parser.add_argument("--from", dest="from_date", default="2025.01.01")
    parser.add_argument("--to", dest="to_date", default="2026.07.31")
    parser.add_argument("--deposit", type=float, default=10_000)
    parser.add_argument("--model", type=int, default=2, help="0=每tick 1=1分钟OHLC 2=开盘价(默认) 3=数学计算(不可用,无symbol info) 4=真实tick")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--force-close", action="store_true", help="自动结束正在运行的终端")
    args = parser.parse_args()

    expert_rel = args.expert
    if args.compile:
        result = compile_ea(Path(args.compile))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            sys.exit(1)
        expert_rel = expert_rel or result["expert_rel"]

    if args.run:
        if not expert_rel:
            parser.error("回测需要 --expert 或先 --compile")
        report = run_backtest(
            expert_rel=expert_rel,
            symbol=args.symbol,
            period=args.period,
            from_date=args.from_date,
            to_date=args.to_date,
            deposit=args.deposit,
            model=args.model,
            timeout_s=args.timeout,
            force_close=args.force_close,
        )
        print(f"回测完成，报告: {report}")


if __name__ == "__main__":
    main()
