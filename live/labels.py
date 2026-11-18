"""每笔单的人工标签：这笔**为什么进**、走的哪套打法。

为什么需要一层人工标签
----------------------
程序能记下发生过的每一件事（开平价、峰值、SL 挪了几次、平仓时点），
唯独记不下开仓那一刻人脑子里在想什么。而这一句**决定了复盘怎么归因**：

  - 标「顺势突破」的单走早了  → 执行问题（怕回撤），要治手；
  - 标「抢反弹就跑」的单走早了 → 判断本来是这么定的，根本不该算损失。

两件事在流水里长得一模一样。没有标签，MFE 捕获率就是两者的混合，
拿它去优化任意一边都是在拟合噪声。

为什么单独一个文件、不塞进 state
--------------------------------
1. `state["position"]` 是出场闸门的输入。标签是纯记录，写进去等于给
   交易路径加了一个不参与决策的字段，下一次改出场逻辑时它会变成噪音。
2. 标签要在**平仓之后**还存在（复盘要用），而 state["position"] 平仓即清。
3. 手工单压根不在 state 里（见 `autotrade._manual_position`），只有一条
   影子。标签表按 ticket 存，天然覆盖手工单和程序单两条路。

为什么不叫 lessons
------------------
教训库是全量注入提示词的模型经验（scope/theme），这里是人给自己看的分类，
两者生命周期、读者、写入方都不同，不要混。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from live import paths

_LABEL_FILE = "trade_labels.json"

# 两套标签，对应两种完全不同的打法。键用 `trend` / `range`，和程序的
# `pos["plan"]` 同一口径——不要另造「趋势市」「震荡市」两套词，
# 否则前端要维护一张对照表，早晚漂移。
#
# 标签本身只描述**入场意图**，不评价对错。评价留给复盘。
TAGS = {
    "trend": [
        "顺势突破",   # 顺着已成立的单边，突破前高/前低进
        "回踩续势",   # 等回踩到均线/结构位再顺原方向进
        "加速中继",   # 已经跑了一段，赌它还没结束
        "逆势反抽",   # 趋势中反向抢一段，快进快出
    ],
    "range": [
        "下沿多",     # 区间底部做多
        "上沿空",     # 区间顶部做空
        "中轨试单",   # 区间中部试方向，赌它去一边
        "假突破",     # 破位后赌它回去
    ],
}

REGIME_TXT = {"trend": "趋势单", "range": "震荡单"}


def tag_options() -> dict:
    """给面板直接用的结构。前端**不许**自己写死一份标签表。"""
    return {"tags": {k: list(v) for k, v in TAGS.items()},
            "regime_txt": dict(REGIME_TXT)}


def labels_path(login=None) -> Path:
    return paths.account_dir(paths.resolve_login(login)) / _LABEL_FILE


def load_labels(login=None) -> dict:
    """读整张表。坏了就当空表——这一层只做记录，读不到不能拖垮面板。"""
    try:
        raw = labels_path(login).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_labels(data: dict, login=None) -> None:
    """原子写。面板每秒轮询，写一半被读到就是整块「—」。"""
    try:
        p = labels_path(login)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def get_label(ticket, login=None) -> dict | None:
    """取一笔的标签。没有就返回 None（前端据此显示「待标签」）。"""
    try:
        key = str(int(ticket))
    except (TypeError, ValueError):
        return None
    item = load_labels(login).get(key)
    return item if isinstance(item, dict) and item.get("tag") else None


def set_label(ticket, regime: str, tag: str, login=None,
              src: str = "user", note: str = "") -> dict:
    """写入/覆盖一笔的标签。**校验在这里，不在调用方。**

    regime 与 tag 必须成套：`range` 里没有「顺势突破」这种打法，
    允许自由文本等于把这一层的统计价值直接毁掉（半年后没人能对上
    两种写法是不是同一件事）。

    返回写进去的那条记录；非法输入抛 ValueError（路由层转 400）。
    """
    try:
        key = str(int(ticket))
    except (TypeError, ValueError):
        raise ValueError(f"ticket 不合法：{ticket!r}") from None
    regime = str(regime or "").strip().lower()
    tag = str(tag or "").strip()
    if regime not in TAGS:
        raise ValueError(f"regime 只能是 trend / range，收到 {regime!r}")
    if tag not in TAGS[regime]:
        raise ValueError(f"{REGIME_TXT[regime]}没有这个标签：{tag!r}")
    item = {
        "ticket": int(key),
        "regime": regime,
        "tag": tag,
        "ts": int(time.time()),
        "src": str(src or "user")[:16],
    }
    note = str(note or "").strip()[:80]
    if note:
        item["note"] = note
    # 写前重读：面板线程和 worker 线程都可能写（同一份文件），
    # 拿几秒前的快照整份覆盖会把另一个 ticket 的标签抹掉。
    data = load_labels(login)
    data[key] = item
    save_labels(data, login)
    return item


def clear_label(ticket, login=None) -> bool:
    """删掉一笔的标签。返回是否真的删了。"""
    try:
        key = str(int(ticket))
    except (TypeError, ValueError):
        return False
    data = load_labels(login)
    if key not in data:
        return False
    data.pop(key, None)
    save_labels(data, login)
    return True


def label_of(ticket, login=None) -> str:
    """平仓流水里那一格：`趋势单 · 顺势突破`。没标签就是空串。

    流水写的是**人话快照**而不是 regime/tag 两个键：复盘/教训库读的是
    老流水，将来标签集合改了，历史行仍要能读懂（和 `plan` 存
    trend/range 一个道理，但这里连改写都不需要）。
    """
    item = get_label(ticket, login)
    if not item:
        return ""
    txt = REGIME_TXT.get(str(item.get("regime")), "")
    tag = str(item.get("tag") or "")
    return f"{txt} · {tag}" if txt and tag else (txt or tag)
