"""JSONL 读写与 JSON 文本拆包——两个模块共用的最小工具层。

为什么单独一层
--------------
`read_jsonl` 原本叫 `autotrade._read_jsonl`，被教训库、日志、统计好几处用；
`unwrap_lesson` 原本叫 `autotrade._unwrap_lesson`，教训库和 `lesson_packs`
都要用。第 3 步把教训库搬出 `autotrade` 时，这两个函数如果留在原地，
新模块就得反过来 import `autotrade`，刚拆开的边界立刻又被缝上；如果各复制
一份，两边会悄悄漂移（历史遗留的 `{"answer":...}` 包裹会有一边拆不干净）。

所以下沉到这里：**只依赖标准库**，谁都可以 import，谁都不会被它拖成环。
老名字 `autotrade._read_jsonl` / `autotrade._unwrap_lesson` 由 autotrade
顶部用 `as` 别名继续导出，历史调用点（含 17,000 行测试）不用改。
"""

from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def unwrap_lesson(text: str) -> str:
    """拆掉没解包的 JSON。

    历史 bug：早期复盘调用 JSON mode 时，`lesson` 直接存成了整段
    `{"answer":"..."}`（实测 224 条里有 10 条）。注入提示词时它既难读、
    又让关键词归类和去重全部失效，所以在入口处统一拆一次。
    """
    raw = " ".join((text or "").split())
    if not raw.startswith("{"):
        return raw
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(obj, dict):
        for k in ("lesson", "answer", "复盘", "reason", "text"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())
    return raw
