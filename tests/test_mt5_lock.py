"""MT5 全局锁：多线程同时调接口必须串行。

为什么要有这组用例
------------------
面板进程里有 4 条线程同时调 MT5（做单 worker、HTTP handler、live-recorder、
止损快查）。MT5 的 Python 接口背后是**一条**终端会话，并发进入时它不会报错，
而是返回 None——在业务层看起来和「行情断了」一模一样，于是被记成"拿不到报价"
一路吞掉。这类故障没法从日志里复盘，只能靠结构保证它不发生。

这里用一个「并发进入就返回 None」的假连接把真实故障固化下来：
锁掉了这条用例就绿，锁被人删掉它就红。
"""

from __future__ import annotations

import threading
import time
import unittest

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, journal


class _Tick:
    def __init__(self, bid: float, ask: float) -> None:
        self.bid = bid
        self.ask = ask


class _ConcurrencySensitiveConn:
    """模拟真实终端：两个线程同时进来时，后到的那个拿到 None。"""

    def __init__(self, delay: float = 0.002) -> None:
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self._m = threading.Lock()

    def symbol_info_tick(self, symbol):  # noqa: ARG002
        with self._m:
            self.in_flight += 1
            self.calls += 1
            peak = self.in_flight
            if peak > self.max_in_flight:
                self.max_in_flight = peak
        try:
            time.sleep(self.delay)      # 放大争用窗口
            if peak > 1:
                return None             # 真实终端的表现：不报错，返回空
            return _Tick(4000.0, 4000.3)
        finally:
            with self._m:
                self.in_flight -= 1


class _LockTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_mt5 = journal._mt5
        self.fake = _ConcurrencySensitiveConn()
        journal._mt5 = lambda allow_launch=False: self.fake  # type: ignore[assignment]

    def tearDown(self) -> None:
        journal._mt5 = self._orig_mt5  # type: ignore[assignment]


class TestQuoteIsSerialized(_LockTestBase):
    def test_concurrent_quote_never_returns_none(self):
        """20 条线程 × 5 次报价，一次都不许拿到 None。"""
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(5):
                try:
                    got = autotrade._quote("XAUUSDm")
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
                    return
                with lock:
                    results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 100)
        missing = [r for r in results if r is None]
        self.assertEqual(missing, [],
                         f"{len(missing)}/100 次拿到 None —— 锁没生效或在锁外调用")
        self.assertEqual(self.fake.max_in_flight, 1,
                         "出现了并发进入 MT5 的窗口")
        self.assertEqual(self.fake.calls, 100)

    def test_quote_still_returns_price(self):
        """加了锁不许把正常返回值弄丢。"""
        self.assertEqual(autotrade._quote("XAUUSDm"), (4000.0, 4000.3))


class TestLockIsReentrant(unittest.TestCase):
    """`mt5_call(_mt5().xxx, ...)` 里外层又会拿一次锁，必须可重入。

    不可重入的话这段代码会自己把自己锁死——表现是面板整体卡住，
    而不是报错，比崩掉更难查。
    """

    def test_nested_acquisition(self):
        with journal.mt5_lock():
            with journal.mt5_lock():
                value = journal.mt5_call(lambda a, b=0: a + b, 1, b=2)
        self.assertEqual(value, 3)

    def test_call_propagates_exception(self):
        def boom():
            raise ValueError("终端炸了")

        with self.assertRaises(ValueError):
            journal.mt5_call(boom)
        # 抛异常后锁必须已经释放，否则后面所有线程永久阻塞
        self.assertTrue(journal.mt5_lock().acquire(timeout=1))
        journal.mt5_lock().release()


class TestBridgeUsesLock(_LockTestBase):
    """桥接层的取数也要走同一个入口，不能自己裸调。

    `get_rates_live` 是 `from live.journal import _mt5` 拿连接的，所以在
    `bridge` 上打补丁没用，必须补 `journal._mt5`（顺带保证这条用例
    永远不碰真终端）。
    """

    def test_bridge_rates_goes_through_lock(self):
        from mt5 import bridge

        seen: dict = {}

        class _Conn:
            def copy_rates_from_pos(self, symbol, tf, start, count):  # noqa: ARG002
                # 只有当前线程持锁时 _is_owned() 才为 True
                seen["locked"] = journal.mt5_lock()._is_owned()  # type: ignore[attr-defined]
                return [_row()]

            def last_error(self):
                return (0, "fake")

        journal._mt5 = lambda allow_launch=False: _Conn()  # type: ignore[assignment]
        orig_ensure = bridge.ensure_symbol
        self.addCleanup(setattr, bridge, "ensure_symbol", orig_ensure)
        bridge.ensure_symbol = lambda symbol: None        # 这条用例只看 copy_rates 入口

        rows = bridge.get_rates_live("XAUUSDm", "M1", 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("locked", seen, "copy_rates_from_pos 根本没被调到")
        self.assertTrue(seen["locked"], "get_rates_live 没在 mt5_call 里调用")


def _row() -> dict:
    return {"time": 1790000000, "open": 1.0, "high": 2.0, "low": 0.5,
            "close": 1.5, "tick_volume": 10, "spread": 12}


class _NonePositionsConn:
    """`positions_get()` 返回 None 的假连接（= 终端查询失败）。

    MT5 在空仓时返回空 tuple，返回 None 只代表查询失败。这两件事如果被
    混成同一个结果，`_positions()` 的"读不到 ≠ 没持仓"就形同虚设。
    """

    def positions_get(self, **kwargs):  # noqa: ARG002
        return None

    def last_error(self):
        return (-10004, "no ipc")


class TestPositionsReadFailureIsNotFlat(unittest.TestCase):
    """读持仓失败必须抛，不能返回空列表。

    返回 `[]` 会让 `_positions()` 得到"确实没持仓"，于是模块以为刚才那笔
    已经平了、反手再开一仓——终端抖一下就是连续开仓。
    """

    def setUp(self) -> None:
        self._orig = journal._mt5
        journal._mt5 = lambda allow_launch=False: _NonePositionsConn()  # type: ignore[assignment]

    def tearDown(self) -> None:
        journal._mt5 = self._orig  # type: ignore[assignment]

    def test_open_positions_raises(self):
        with self.assertRaises(RuntimeError):
            journal.open_positions()

    def test_positions_helper_returns_none(self):
        """异常在上层被翻成 None，而不是 []。"""
        self.assertIsNone(autotrade._positions("XAUUSDm"))


class TestConnectGateFailsFast(unittest.TestCase):
    """连接期闸门：新调用立刻失败，**不排队**。

    `initialize` 内部超时 30 秒、拉起终端可能更久。如果只靠锁排队，
    2 秒一格的止损快查会被一次连接尝试整个吃掉——那是最不能停的路径。
    """

    def setUp(self) -> None:
        from live import mt5lock

        self.mt5lock = mt5lock

    def test_call_raises_instead_of_waiting(self):
        with self.mt5lock.connecting():
            started = time.time()
            with self.assertRaises(self.mt5lock.MT5Busy):
                self.mt5lock.call(lambda: "never")
            self.assertLess(time.time() - started, 0.5,
                            "闸门期 call 在等锁，应该直接失败")

    def test_quick_returns_default_instead_of_waiting(self):
        with self.mt5lock.connecting():
            started = time.time()
            got = self.mt5lock.call_quick(lambda: "never", timeout=5.0,
                                          default="skipped")
            self.assertEqual(got, "skipped")
            self.assertLess(time.time() - started, 0.5)

    def test_other_thread_is_not_blocked(self):
        """连接期间从另一条线程要报价：必须在毫秒级返回。"""
        out: dict = {}

        def worker() -> None:
            t0 = time.time()
            out["value"] = self.mt5lock.call_quick(
                lambda: "never", timeout=5.0, default="no-quote")
            out["elapsed"] = time.time() - t0

        with self.mt5lock.connecting():
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5)
            time.sleep(0.25)      # 主线程继续持锁，证明 worker 没等在这上面

        self.assertEqual(out.get("value"), "no-quote")
        self.assertLess(out.get("elapsed", 99), 0.5,
                        f"worker 等了 {out.get('elapsed')} 秒，闸门没生效")

    def test_busy_skips_are_counted(self):
        """跳过的次数要能看见——它是判断"锁是否过粗"的唯一信号。"""
        self.mt5lock.reset_busy_stats()
        with self.mt5lock.connecting():
            self.mt5lock.call_quick(lambda: None, timeout=0.1, default=None)
        self.assertGreaterEqual(self.mt5lock.busy_stats()["skips"], 1)


class TestNormalCallStillWaits(unittest.TestCase):
    """普通 `call()` 该等就等——它等的是单次调用的毫秒级。

    如果连它也改成不等待，下单时读到的报价会莫名其妙变成 None，
    变成"券商报错说没价格"，比排队更难查。
    """

    def test_call_waits_for_lock_then_succeeds(self):
        from live import mt5lock

        out: dict = {}

        def worker() -> None:
            out["value"] = mt5lock.call(lambda: "done")

        with mt5lock.lock():
            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.15)
            self.assertTrue(t.is_alive(), "call 没等锁，直接跑了")
        t.join(timeout=5)
        self.assertEqual(out.get("value"), "done")


class TestExecutorQuoteNeverQueues(unittest.TestCase):
    """面板 1.5 秒轮询的报价不许排在下单后面。"""

    def test_quote_returns_none_when_lock_is_held(self):
        from execution import live as exmod
        from live import mt5lock

        ex = exmod.Executor(exmod.ExecConfig())
        out: dict = {}

        def worker() -> None:
            t0 = time.time()
            out["value"] = ex.quote()
            out["elapsed"] = time.time() - t0

        with mt5lock.lock():
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5)
            time.sleep(0.35)

        self.assertIsNone(out.get("value"))
        self.assertLess(out.get("elapsed", 99), 0.5,
                        "Executor.quote 在 gate 上排队了")


class TestOpenMarketKeepsDiskOutOfLock(unittest.TestCase):
    """`open_market` 整段不持锁：写审计日志时手上不能有 MT5 锁。

    否则每次下单的写盘时间会直接变成"报价和止损快查的盲区"。
    """

    def test_record_runs_without_lock(self):
        import tempfile
        import types
        from pathlib import Path

        from execution import live as exmod
        from live import mt5lock

        class _Tick:
            bid = 4000.0
            ask = 4000.3

        fake = types.SimpleNamespace(
            symbol_info=lambda symbol: types.SimpleNamespace(visible=True),
            symbol_info_tick=lambda symbol: _Tick(),
            symbol_select=lambda symbol, flag: True,
            last_error=lambda: (0, "ok"),
            ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1,
            ORDER_TIME_GTC=0, ORDER_FILLING_IOC=1,
        )
        orig_mt5, orig_dir = exmod.mt5, exmod.LOG_DIR
        exmod.mt5 = fake                       # type: ignore[assignment]
        exmod.LOG_DIR = Path(tempfile.mkdtemp())
        self.addCleanup(setattr, exmod, "mt5", orig_mt5)
        self.addCleanup(setattr, exmod, "LOG_DIR", orig_dir)

        ex = exmod.Executor(exmod.ExecConfig(dry_run=True))
        ex.connected = True
        seen: dict = {}
        orig_record = ex._record

        def spy(kind, payload):
            seen["owned"] = mt5lock.lock()._is_owned()  # type: ignore[attr-defined]
            orig_record(kind, payload)

        ex._record = spy
        res = ex.open_market(direction=1, lots=0.01)
        self.assertTrue(res.get("ok"))
        self.assertIn("owned", seen, "_record 没被调到")
        self.assertFalse(seen["owned"], "写盘时还持有 MT5 锁")


class TestEmptyKwargsMustNotBeExpanded(unittest.TestCase):
    """回归（2026-10-28 实盘事故）：空 kwargs 不能展开成 `**{}`。

    MT5 是 C 扩展。`f(*a, **{})` 走的是带关键字字典的调用形式，交易类函数
    （`order_send` / `order_check`）会直接返回 None，`last_error` 变成
    -2「Unnamed arguments not allowed」——不抛异常，只是拿不到结果。

    更麻烦的是**只读函数不敏感**：`symbol_info_tick` 照常返回报价，所以面板
    看起来一切正常，只有下单静默失败（流水里是 `retcode=None`）。实盘上就
    表现为"每笔开仓都失败、每笔平仓都失败"。

    这里锁住实现形态：kwargs 为空必须退化成纯位置调用。用源码断言而不是
    行为断言，是因为 Python 层区分不出 `f(x)` 和 `f(x, **{})`，只有 C 扩展
    能感知——而 CI 里没有终端。
    """

    def test_call_degrades_to_positional(self):
        import inspect

        from live import mt5lock

        self.assertIn("if kwargs else fn(*args)", inspect.getsource(mt5lock.call))

    def test_call_quick_degrades_to_positional(self):
        import inspect

        from live import mt5lock

        self.assertIn("if kwargs else fn(*args)", inspect.getsource(mt5lock.call_quick))

    def test_arguments_still_reach_fn(self):
        """退化成一位置调用不许把参数丢了。"""
        from live import mt5lock

        got: dict = {}

        def fn(a, b=0):
            got["a"], got["b"] = a, b
            return a + b

        self.assertEqual(mt5lock.call(fn, 1, b=2), 3)
        self.assertEqual(got, {"a": 1, "b": 2})
        self.assertEqual(mt5lock.call(fn, 5), 5)
        self.assertEqual(mt5lock.call_quick(fn, 7, timeout=1.0), 7)


if __name__ == "__main__":
    unittest.main()
