"""MT5 接口的进程内串行化。

为什么需要
----------
MT5 的 Python 接口背后是**一条**终端会话，不是线程安全的。面板进程里同时
有 4 条线程在调它（做单 worker、HTTP handler、live-recorder、止损快查）。
争用时它不报错，而是 `symbol_info_tick()` 直接返回 None——在业务层看起来
和「行情断了」一模一样，于是被记成"拿不到报价"一路吞掉，只能靠结构保证
它不发生。

三条纪律（每条都对应一次事故）
------------------------------
1. **锁只包单次 API 调用。** 业务方法不要整段加锁：`open_market` 里的
   `_record` 写盘、filling 重试循环都在锁外。整段加锁会把「毫秒级串行」
   放大成「秒级停摆」——下单期间面板轮询全部排队。
2. **连接期走 `connecting()` 闸门。** `mt5.initialize()` 最长 30 秒
   （MT5 内部最小超时），`ensure_terminal()` 拉起终端可能几十秒。这段时间
   若靠锁排队，止损快查会被一次连接尝试拖住几十秒——那是最不能停的路径。
   闸门期间新调用**立刻失败**，让业务层当作「这一轮没有行情」跳过。
3. **报价这类高频、可丢的调用走 `call_quick()`。** 拿不到锁就当这一轮
   没报价，宁可漏一格，不可阻塞。止损判定的时间精度来自 `guard_sec`
   （5 秒 / 近门槛 2 秒），不是来自无条件等待。

`call()` 与 `call_quick()` 的分工
---------------------------------
· `call()`      —— 下单、账户、持仓、历史查询。等得起（等的是单次调用的
                   毫秒级），拿不到就抛 `MT5Busy` 让上层明确失败。
· `call_quick()` —— 报价轮询、止损快查。超时返回 `default`，并记一次
                   跳过的计数（面板可以显示"锁争用"次数）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# 所有 MT5 调用共用的一把锁。
#
# 用 RLock 而不是 Lock：`mt5_call(_mt5().xxx, ...)` 里 `_mt5()` 会先拿锁再
# 返回，外层还会再拿一次；不可重入就会自己把自己锁死。
_lock = threading.RLock()

# 连接期闸门。set 表示"正在 initialize / 正在拉起终端"，此刻任何新调用
# 都应该快速失败而不是排队等锁。
_connecting = threading.Event()

_busy_m = threading.Lock()
_busy_skips = 0
_busy_last_ts = 0.0


class MT5Busy(RuntimeError):
    """MT5 正在建立连接（或正忙），此刻不接受新的接口调用。

    故意抛异常而不是返回 None：`positions_get()` 拿 None 在业务层会被
    `or []` 翻成"没有持仓"，那和"读不到持仓"是两件完全不同的事，后者
    会让人以为空仓而实际还有单。
    """


def lock() -> threading.RLock:
    """MT5 调用锁。连续多次调用需要原子时用 `with mt5_lock():` 包一段。

    **不要**在持锁期间做与 MT5 无关的慢活（写盘、等模型、sleep）——
    这期间别的线程读不到报价。
    """
    return _lock


def is_connecting() -> bool:
    """是否正在建立连接（面板可据此显示"正在连接 MT5"而不是"无行情"）。"""
    return _connecting.is_set()


def call(fn, *args, **kwargs):
    """单次 MT5 调用，等待锁。

        tick = mt5_call(_mt5().symbol_info_tick, symbol)

    比 `with mt5_lock():` 好在调用点短、不会漏掉 return 那一行。
    """
    if _connecting.is_set():
        raise MT5Busy("MT5 正在连接，稍后重试")
    with _lock:
        return fn(*args, **kwargs)


def call_quick(fn, *args, timeout: float = 0.4, default=None, **kwargs):
    """单次 MT5 调用，最多等 `timeout` 秒，拿不到锁就返回 `default`。

    专给「这一轮拿不到也没关系」的路径：止损快查、面板状态轮询。
    跳过的次数记在 `busy_stats()` 里——它接近 0 说明锁没成为瓶颈；
    持续增长说明有调用点忘了把慢活挪出锁。
    """
    if _connecting.is_set():
        _note_busy()
        return default
    if not _lock.acquire(timeout=timeout):
        _note_busy()
        return default
    try:
        return fn(*args, **kwargs)
    finally:
        _lock.release()


@contextmanager
def connecting():
    """连接期闸门。

    先拿锁、再关闸：拿锁之后才 set，保证已经进来排队的调用不会因为闸门
    半开而漏过去；关闸之后新来的调用直接 `MT5Busy`，不排队。

    用法：
        with mt5_connecting():
            mt5.initialize(...)      # 这里是裸调用，锁已在本上下文内
    """
    if not _lock.acquire(timeout=10.0):
        raise MT5Busy("MT5 正忙，无法开始连接")
    try:
        _connecting.set()
        yield
    finally:
        _connecting.clear()
        _lock.release()


def busy_stats() -> dict:
    """锁争用统计。面板可以把 `skips` 显示出来，用来判断锁是否过粗。"""
    with _busy_m:
        return {"skips": _busy_skips, "last_ts": _busy_last_ts}


def reset_busy_stats() -> None:
    global _busy_skips, _busy_last_ts
    with _busy_m:
        _busy_skips = 0
        _busy_last_ts = 0.0


def _note_busy() -> None:
    global _busy_skips, _busy_last_ts
    with _busy_m:
        _busy_skips += 1
        _busy_last_ts = time.time()
