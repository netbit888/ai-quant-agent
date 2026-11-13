"""品种精度、点差成本与降级留痕。

为什么单独一层
--------------
`degrade()` 是所有「看起来像安静算错」的地方的唯一出口：品种不在符号表里、
点值猜错、币种认不出、格式化失败，都从这里留一行 `kind="degrade"` 的流水。
它只依赖 `storage.append_log`，所以任何模块都能用——包括 `live/journal.py`。

这正是它必须独立的原因：`journal` 曾经为了写这一行去
`from live.autotrade import _degrade`，于是 journal 与 autotrade 成了循环
依赖，只能靠「把 import 挪进函数体」绕过，加载顺序变成隐式约束。
搬到这里之后那条边变成 journal -> units -> storage，单向。

`digits` / `point` / `spread_cost` / `px` 一起下沉，是因为它们都靠
`degrade()` 报告兜底——留一个在 autotrade 就得再造一条反向依赖。
"""

import time

from live.storage import append_log


# 降级留痕的节流表：{(函数名, 品种): 上次记录的时间}。
# 必须节流——MT5 掉线时每 tick 会调 point/digits 好几遍，不节流就是
# 一天几千行 degrade，把真正的流水淹掉。
_DEGRADE_SEEN: dict[tuple[str, str], float] = {}


_DEGRADE_INTERVAL = 600.0


def degrade(what: str, symbol: str, info: str,
             login: int | str | None = None) -> None:
    """某个价格常量没算准，退回兜底值——**留个痕**。

    这些兜底本身是必要的（一个陌生品种名不该让 prompt 构建崩掉），
    但它们"安静地算错"：点值猜成 0.001 时，黄金还行、BTC 的点差会被
    读成 640000 点塞给模型。没有痕迹的话，事后只能怀疑"模型怎么这么蠢"。
    """
    key = (what, symbol)
    now = time.time()
    last = _DEGRADE_SEEN.get(key)
    if last is not None and now - last < _DEGRADE_INTERVAL:
        return
    _DEGRADE_SEEN[key] = now
    try:
        append_log({"ts": int(now), "kind": "degrade", "what": what,
                    "symbol": symbol, "info": info}, login)
    except Exception:  # noqa: BLE001
        # 已经在降级路径上了，写不进流水也没别的办法，别把调用方拖崩
        pass


def point(symbol: str) -> float:
    try:
        from backtest.symbols import SYMBOLS

        known = (SYMBOLS.get(symbol) or {}).get("point")
        if not known:
            degrade("point", symbol, "品种不在符号表里，按 0.001 兜底")
        return float(known or 0.001)
    except Exception as exc:  # noqa: BLE001
        degrade("point", symbol, f"{type(exc).__name__}: {exc}")
        return 0.001


def digits(symbol: str) -> int:
    """报价小数位。黄金 3 位、BTC 2 位——写死 3 位会让 80185.81 显示成
    80185.810，看着像精度过剩；更有害的是 EURUSD 5 位会被砍成 3 位。"""
    try:
        from backtest.symbols import SYMBOLS

        known = (SYMBOLS.get(symbol) or {}).get("digits")
        if not known:
            degrade("digits", symbol, "品种不在符号表里，按 3 位兜底")
        return int(known or 3)
    except Exception as exc:  # noqa: BLE001
        degrade("digits", symbol, f"{type(exc).__name__}: {exc}")
        return 3


def px(symbol: str, value) -> str:
    """按品种精度格式化价格。"""
    try:
        return f"{float(value):.{digits(symbol)}f}"
    except (TypeError, ValueError):
        return "—"


def spread_cost(symbol: str, lots: float) -> float | None:
    """这一手的点差成本（USD）。**算不出来返回 None，不返回 0.0**。

    0.0 是个危险的谎：调用方拿它当"成本为零"，闸门就形同不存在，
    所有单子一律放行。宁可让它显式拿到 None 自己决定怎么办。
    （当前没有生产调用点，只有测试在用——正因为没人接，才更要一开始
    就把语义定对，免得将来第一次接入就踩坑。）
    """
    try:
        from backtest.symbols import spread_cost_usd

        got = spread_cost_usd(symbol, lots)
    except Exception as exc:  # noqa: BLE001
        degrade("spread_cost", symbol, f"{type(exc).__name__}: {exc}")
        return None
    if got is None:
        degrade("spread_cost", symbol, "spread_cost_usd 返回空值")
        return None
    return float(got)
