"""策略代码动态加载与约束校验

安全说明：这里是本地个人项目，exec 执行 LLM 生成的代码，不做严格沙箱。
代码来源是可信的 LLM 服务，且运行在本机。若要对外提供服务，必须换成容器隔离。
"""

from __future__ import annotations

import re
from typing import Sequence

from backtest.engine import EngineConfig
from backtest.models import Bar


def extract_code(text: str) -> str:
    """从 LLM 回复里提取 ```python ... ``` 代码块。"""
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if match:
        return match.group(1).strip()
    # 没有代码块就认为整段是代码（去掉可能的前后说明行）
    return text.strip()


def extract_json(text: str) -> dict:
    """从回复里提取 JSON 对象。"""
    import json

    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    raw = match.group(1).strip() if match else text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 退一步：找第一个 { 到最后一个 }
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def load_strategy(code: str):
    """执行代码并实例化 Strategy 类。"""
    namespace: dict = {}
    exec(compile(code, "<llm_strategy>", "exec"), namespace)  # noqa: S102
    cls = namespace.get("Strategy")
    if cls is None:
        raise ValueError("代码中没有找到名为 Strategy 的类")
    return cls()


def validate_constraints(strategy, cfg: EngineConfig, min_tp_points: int = 360) -> dict:
    """硬校验：止盈/止损距离是否满足成本约束。

    用固定入场价分别测多空两个方向的 sl_tp 输出。
    """
    entry = 2000.0
    issues: list[str] = []
    for direction in (1, -1):
        sl, tp = strategy.sl_tp(1, entry, direction)
        if tp is None:
            issues.append(f"方向 {direction} 没有止盈")
            continue
        if sl is None:
            issues.append(f"方向 {direction} 没有止损")
            continue
        tp_points = abs(tp - entry) / cfg.point
        sl_points = abs(sl - entry) / cfg.point
        if tp_points < min_tp_points:
            issues.append(f"方向 {direction} 止盈 {tp_points:.0f} points < {min_tp_points}")
        if sl_points < 100:
            issues.append(f"方向 {direction} 止损 {sl_points:.0f} points 太近")
    return {"ok": not issues, "issues": issues}
