"""解析 MT5 策略测试器导出的 HTML 报告

用法：
    python -m mt5.report data/reports/ReportTester-<login>.html
    python -m mt5.report data/reports/xxx.html --json out.json

MT5 报告特点：
  - 编码是 UTF-16LE（带 FF FE BOM），中文界面的标签是中文
  - 前几十行是「设置 + 结果」表，后面几千行是逐笔成交明细（本模块只取汇总）
  - 组合值格式如 "39.43 (0.39%)"、"521 (45.86%)"，需要拆开
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 中文标签 → 规范字段名（英文界面报告用英文标签，另有一套映射）
LABELS_CN = {
    "专家": "expert",
    "交易品种": "symbol",
    "期间": "period",
    "公司": "company",
    "货币": "currency",
    "初始入金": "deposit",
    "杠杆": "leverage",
    "质量历史": "quality",
    "柱": "bars",
    "报价": "ticks",
    "总净盈利": "net_profit",
    "毛利": "gross_profit",
    "毛损": "gross_loss",
    "盈利因子": "profit_factor",
    "预期收益": "expected_payoff",
    "采收率": "recovery_factor",
    "夏普比率": "sharpe",
    "交易总计": "total_trades",
    "总成交": "total_deals",
}

LABELS_EN = {
    "Expert": "expert",
    "Symbol": "symbol",
    "Period": "period",
    "Company": "company",
    "Currency": "currency",
    "Initial Deposit": "deposit",
    "Leverage": "leverage",
    "History Quality": "quality",
    "Bars": "bars",
    "Ticks": "ticks",
    "Total Net Profit": "net_profit",
    "Gross Profit": "gross_profit",
    "Gross Loss": "gross_loss",
    "Profit Factor": "profit_factor",
    "Expected Payoff": "expected_payoff",
    "Recovery Factor": "recovery_factor",
    "Sharpe Ratio": "sharpe",
    "Total Trades": "total_trades",
    "Total Deals": "total_deals",
}

# 组合值：count/amount + 括号内的百分比
COMBO_CN = {
    "盈利交易 (% 全部)": ("winning_trades", "win_rate"),
    "亏损交易 (% 全部)": ("losing_trades", "loss_rate"),
    "买入交易 (赢得 %)": ("long_trades", "long_win_rate"),
    "卖出交易 (赢得 %)": ("short_trades", "short_win_rate"),
    "最大净值亏损": ("max_drawdown", "max_drawdown_pct"),
    "最大结余亏损": ("max_balance_drawdown", "max_balance_drawdown_pct"),
}

COMBO_EN = {
    "Profit Trades (% of total)": ("winning_trades", "win_rate"),
    "Loss Trades (% of total)": ("losing_trades", "loss_rate"),
    "Long Trades (won %)": ("long_trades", "long_win_rate"),
    "Short Trades (won %)": ("short_trades", "short_win_rate"),
    "Balance Drawdown Maximal": ("max_balance_drawdown", "max_balance_drawdown_pct"),
    "Equity Drawdown Maximal": ("max_drawdown", "max_drawdown_pct"),
}


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean(cell: str) -> str:
    text = re.sub(r"<[^>]+>", "", cell)
    return re.sub(r"\s+", " ", text).strip()


def _num(text: str) -> float | None:
    match = re.search(r"-?\d[\d,\s]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _pairs(html: str) -> dict[str, str]:
    """把表格行拆成 label → value。

    不按固定位置配对：MT5 报告里存在空单元格和四列混排，位置会错位。
    改用形态判断——标签统一以 ':' 结尾（中英文都是），取其后第一个非空且非标签的单元格。
    """
    out: dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [_clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        for idx, cell in enumerate(cells):
            if not cell.endswith(":"):
                continue
            label = cell[:-1].strip()
            if not label:
                continue
            j = idx + 1
            while j < len(cells) and (not cells[j] or cells[j].endswith(":")):
                j += 1
            if j < len(cells):
                out.setdefault(label, cells[j])
    return out


def parse_report(path: str | Path) -> dict:
    """解析报告，返回 {settings, metrics, raw}。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    html = _decode(path)
    pairs = _pairs(html)

    labels = {**LABELS_CN, **LABELS_EN}
    combos = {**COMBO_CN, **COMBO_EN}

    settings: dict = {}
    metrics: dict = {}

    for label, field in labels.items():
        if label not in pairs:
            continue
        value = pairs[label]
        if field in {"expert", "symbol", "period", "company", "currency", "leverage"}:
            settings[field] = value
        elif field == "deposit":
            settings[field] = _num(value)
        elif field == "quality":
            metrics[field] = _num(value)
        else:
            metrics[field] = _num(value)

    for label, (abs_field, pct_field) in combos.items():
        if label not in pairs:
            continue
        value = pairs[label]
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", value)]
        if nums:
            metrics[abs_field] = nums[0]
        if len(nums) > 1:
            metrics[pct_field] = nums[1]

    return {"file": str(path), "settings": settings, "metrics": metrics, "raw_count": len(pairs)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="解析 MT5 回测 HTML 报告")
    parser.add_argument("report", help="报告 html 路径")
    parser.add_argument("--json", help="另存为 JSON 的路径")
    args = parser.parse_args()

    result = parse_report(args.report)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)

    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
        print(f"\n已保存: {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
