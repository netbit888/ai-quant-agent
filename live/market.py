"""行情与账户口径：读报价、读 K 线、读持仓、算合约规格与币种系数。

为什么单独一层
--------------
这些函数是**唯一**允许直接碰 MT5 读接口的地方（下单在 `execution/live.py`，
刻意分开）。它们以前散在 `autotrade` 里，导致回放工具想复用「金额怎么换成
价格距离」必须 import 整个交易模块。

三个缓存是本模块的私有状态，别在别处读：
  `_CONTRACT_CACHE` 合约规格（进程内不变）
  `_ACCT_CUR`       账户结算币种（60 秒 TTL）
  `_ACCT_F`         USD → 账户货币系数（60 秒 TTL）
缓存是必须的：`_acct_currency()` 背后是一次 `account_info`，要拿 MT5 锁，
一段 30 行的流水若每行都查一次，止损快查会被自己的日志拖死。

依赖方向：本模块 → `mt5conn` / `mt5lock` / `mt5.bridge` / `journal.usd_rate`。
反向没有——**任何底层模块都不许 import 本模块**。
"""

import time

from live.cfg import BASE_LOTS, Cfg


_CONTRACT_CACHE: dict[str, float] = {}


_ACCT_CUR = {"code": "", "ts": 0.0}


_ACCT_TTL = 60.0


_ACCT_F = {"factor": 0.0, "ts": 0.0}


def _positions(symbol: str) -> list[dict] | None:
    """该品种的持仓。**读失败返回 None，不是空列表**。

    这个区分很关键：返回 [] 表示"确实没持仓"，返回 None 表示"不知道"。
    如果读失败也返回 []，模块会误判「刚才那笔已经平了」，然后马上再开一笔——
    终端抖一下就能变成连续开仓。
    """
    try:
        from live.journal import open_positions

        rows = open_positions()
    except Exception:  # noqa: BLE001
        return None
    return [p for p in rows if str(p.get("symbol")) == symbol]


def _symbol_contract(symbol: str) -> float:
    hit = _CONTRACT_CACHE.get(symbol)
    if hit:
        return hit
    value = 0.0
    try:
        from live.mt5conn import _mt5
        from live.mt5lock import call as mt5_call

        info = mt5_call(_mt5().symbol_info, symbol)
        if info is not None and getattr(info, "trade_contract_size", 0):
            value = float(info.trade_contract_size)
    except Exception:  # noqa: BLE001
        value = 0.0
    if not value:
        try:
            from backtest.symbols import SYMBOLS

            value = float((SYMBOLS.get(symbol) or {}).get("contract") or 100.0)
        except Exception:  # noqa: BLE001
            value = 100.0
    # 同一进程内合约规格不会变，缓存住：止损价换算、金额核算每轮都要用，
    # 每次都去碰 MT5 等于把一次 tick 读取变成两次，还得排队拿锁。
    _CONTRACT_CACHE[symbol] = value
    return value


def _acct_currency() -> str:
    """当前账户的结算币种（USD / USC …）。读不到按 USD。

    **必须缓存**：这个函数被每一行流水文案、每一条闸门 reason 调用，
    而背后是一次 `account_info`——要拿 MT5 的锁。不缓存的话，一段
    30 行的流水会变成 30 次账户查询，止损快查被自己的日志拖死。
    账户币种在进程生命周期内不会变（换账户本来就该重启面板）。
    """
    now = time.time()
    hit = _ACCT_CUR["code"]
    if hit and now - float(_ACCT_CUR["ts"]) < _ACCT_TTL:
        return hit
    code = "USD"
    try:
        from live.mt5conn import _mt5
        from live.mt5lock import call as mt5_call

        info = mt5_call(_mt5().account_info)
        if info is not None and getattr(info, "currency", None):
            code = str(info.currency).upper()
    except Exception:  # noqa: BLE001
        code = "USD"
    _ACCT_CUR.update(code=code, ts=now)
    return code


def _acct_factor() -> float:
    """1 USD 等于多少**账户结算货币**：美元户 1，美分户 100。

    代码内部按「价差 × 合约 × 手数」算出来的钱都是美元，而 MT5 的 profit、
    账户余额、cfg 里的阈值全是账户货币。两者只差这一个系数：
        账户金额 = 美元金额 × _acct_factor()
    只在需要和账户货币对齐的地方用它，不要往别处扩散。
    """
    now = time.time()
    hit = _ACCT_F["factor"]
    if hit and now - float(_ACCT_F["ts"]) < _ACCT_TTL:
        return hit
    factor = 1.0
    try:
        from live.journal import usd_rate

        rate = usd_rate(_acct_currency())
        if rate > 0:
            factor = 1.0 / rate
    except Exception:  # noqa: BLE001
        factor = 1.0
    _ACCT_F.update(factor=factor, ts=now)
    return factor


def price_dist_of(cfg: Cfg, value) -> float:
    """把「0.01 手口径」的金额阈值换成**价格距离**（同一账户货币下的价差）。

    浮盈 = 价距 × 合约 × 手数 × 账户系数；而金额阈值随手数等比放大，
    两个手数一乘一除正好相消。所以价距**与手数无关**，用基准手数算一次就够：
    227 笔反事实回放标定的 `sl_usd=3.00` 本质是 3.00 的价距，只是当时
    0.01 手 XAUUSDm 恰好 1 盎司，金额和价距是同一个数。
    """
    try:
        unit = float(_symbol_contract(cfg.symbol)) * BASE_LOTS * _acct_factor()
        if unit <= 0:
            return 0.0
        return abs(float(value)) / unit
    except (TypeError, ValueError):
        return 0.0


def _quote(symbol: str) -> tuple[float, float] | None:
    # 走 journal 的连接保活，不要直接 mt5.symbol_info_tick：
    # 没 initialize 过的话它只会返回 None，看起来像行情断了。
    #
    # 用 call_quick 而不是 call：这条路径同时被 tick 和 2 秒一格的止损快查
    # 调用，而 `order_send` 可能要等券商几百毫秒。拿不到锁就当「这一轮没有
    # 报价」——上层已经有完整处理（skip 掉这一轮），下一格再来。
    try:
        from live.mt5conn import _mt5
        from live.mt5lock import call_quick

        tick = call_quick(_mt5().symbol_info_tick, symbol, timeout=0.3)
        if tick is None:
            return None
        return float(tick.bid), float(tick.ask)
    except Exception:  # noqa: BLE001
        return None


def _bars(symbol: str, tf: str, count: int) -> list[dict]:
    # 用 get_rates_live：不 initialize、不 shutdown。
    # 老写法 mt5_session() 会在退出时 shutdown，把整个面板进程的连接一起关掉，
    # 下一轮就只剩「拿不到报价」。
    try:
        from mt5.bridge import get_rates_live

        return get_rates_live(symbol, tf, count)
    except Exception:  # noqa: BLE001
        return []
