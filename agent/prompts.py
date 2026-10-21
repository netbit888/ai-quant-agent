"""LLM 提示词模板：策略生成 + 归因分析

设计原则：
  1. 接口约定写死，LLM 只填策略逻辑，降低"生成跑不起来"的概率
  2. 硬约束写进 prompt，同时代码里再做一次硬校验（不依赖 LLM 自觉）
  3. 分析阶段要求输出 JSON，便于程序解析
"""

from __future__ import annotations

SYSTEM = """你是一个量化策略研究员，为一个自动化研究系统生成和迭代交易策略。

你只做黄金（XAUUSDm）H1 周期。数据是标准 OHLC bar。

环境事实（不可违反）：
- 黄金 point = 0.001，合约 100 盎司，每 point 价值 0.1 USD/手
- 真实点差约 240 points（0.24 USD），这是硬成本
- 回测为「仅开盘价」模型：信号基于上一根 bar 收盘，在当前 bar 开盘价成交
- 杠杆 1:200，初始资金 10000 USD

硬约束（违反即判无效）：
1. 止盈距离必须 > 360 points（点差的 1.5 倍），否则成本吃掉全部利润
2. 必须有止损，止损距离 > 100 points
3. 不要在策略里做资金管理/复利，手数由回测引擎固定
4. 不要读文件、不要发网络请求、不要用 subprocess
"""

STRATEGY_INTERFACE = """策略类必须严格符合以下接口：

```python
from typing import Sequence
from backtest.models import Bar
from backtest.indicators import sma, ema, macd, atr   # 只能用这些

class Strategy:
    def __init__(self, **params):
        # 你的参数，给默认值
        ...

    def prepare(self, bars: Sequence[Bar]) -> None:
        # 一次性预计算指标，存到 self 上
        # bars[i] 有 .time .open .high .low .close .tick_volume .spread
        ...

    def signal(self, i: int) -> int:
        # 返回 1 做多 / -1 做空 / 0 不动
        # i 是当前已收盘的 bar 索引；只能用 <= i 的数据（不许看未来）
        ...

    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]:
        # 返回 (止损价, 止盈价)，direction 为 +1/-1
        ...
```

只输出一个类，类名必须是 `Strategy`。禁止修改接口签名。"""

GENERATE = """{system}

{interface}

参考实现（已校准，可作为写法示例）：

```python
{example}
```

当前任务：生成一个新策略。

策略方向：{hypothesis}

{history_block}

请直接输出完整的 Python 代码（一个 ```python 代码块），不要解释。
代码要能独立运行，不依赖除 backtest.* 之外的本地模块。"""

ANALYZE = """{system}

以下是一个策略代码和它的回测结果，请做归因分析。

策略代码：
```python
{code}
```

回测指标（2025 年 XAUUSDm H1）：
{metrics}

约束校验：{constraints}

历史轮次摘要：
{history_block}

请分析：
1. 这个策略的问题在哪（结合指标，不要泛泛而谈）
2. 下一版应该改什么（具体到参数或逻辑）
3. 如果已经不错，接下来往哪个方向探索

只输出 JSON，格式：
```json
{{
  "diagnosis": "问题诊断（2-3 句）",
  "next_hypothesis": "下一版策略的具体改进方向（1-2 句，要可执行）",
  "changes": ["改动点1", "改动点2"]
}}
```"""
