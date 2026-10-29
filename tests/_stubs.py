"""测试环境桩：机器上没装 MetaTrader5 / openai 时也不让整套测试变红。

为什么需要
----------
这两个包只在真正连终端或真正调模型时才会用到，但它们在**模块顶层**被 import。
缺包时，一批根本没碰网络和终端的用例（比如「未确认时不许调 LLM」）会在
import 阶段直接抛 ModuleNotFoundError，测试套件永远红着——永远红的测试
等于没有测试，后面改任何东西都看不出是不是自己弄坏的。

做法：只在真实包缺失时注入最小桩；真实包存在就什么都不做。
桩故意做得很薄：OpenAI() 一被真正构造就抛错，确保「不小心真调用」会立刻暴露，
而不是悄悄跑在一个假客户端上。
"""

from __future__ import annotations

import os
import sys
import types

# 进程级临时数据根。None = 还没隔离过。
_TEST_ROOT: str | None = None


class _StubOpenAI:
    """占位客户端。真实客户端应由测试 mock 掉；走到这里说明测试漏了 mock。"""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("测试桩：不应真实创建 OpenAI 客户端，请 mock agent.llm.chat_ex")


def isolate_test_data_root() -> str | None:
    """把实盘数据根**默认**指到进程级临时目录。

    为什么
    ------
    `_degrade`（`_point` / `_digits` 算不出来时的兜底留痕）会走 `append_log`，
    直接写真实账本。2026-10-29 在 `autotrade_log.jsonl` 里查出 23 条
    `symbol="WHATEVER"` 的 degrade 行，时间跨度 23:20→02:55，正是几次全量
    测试跑出来的——**它们不难看，但会污染复盘检索**（按品种筛的时候混进来）。

    靠每个 TestCase 自己在 setUp 里 `set_root` 是不现实的：漏一个就脏一次，
    而且脏得毫无提示。所以反过来——**默认全隔离**，需要真实数据的用例显式
    `paths.set_root(None)` 拿回默认路径。

    `AQA_DATA_DIR` 已设（CI、手动排查、想复现真实账本）时一律尊重，不覆盖。

    **必须走环境变量、不能只调 `paths.set_root(tmp)`**：几十个 TestCase 的
    tearDown 都是 `paths.set_root(None)`，而 None 会重算 `appdirs.data_dir()`
    ——只 set_root 的话，每跑完一个用例就被拉回真实账本，下一个用例在 setUp
    之前触发的兜底留痕照样写脏。设了 AQA_DATA_DIR，`set_root(None)` 才落在
    隔离目录里。

    返回临时目录路径；已有隔离或用户显式指定时返回 None。
    """
    global _TEST_ROOT
    if _TEST_ROOT is not None:
        return None
    if os.environ.get("AQA_DATA_DIR"):
        return None

    import atexit
    import shutil
    import tempfile

    _TEST_ROOT = tempfile.mkdtemp(prefix="aqa-test-data-")
    atexit.register(shutil.rmtree, _TEST_ROOT, True)

    os.environ["AQA_DATA_DIR"] = _TEST_ROOT
    from live import paths

    paths.set_root(None)
    return _TEST_ROOT


def disable_live_mt5() -> None:
    """测试进程**默认不连真实终端**。

    为什么
    ------
    2026-10-29 实测：跑完一个 tick 级用例后 ``journal._connected`` 从 False
    变成 True——测试真的对本机 MT5 终端调了 ``initialize``。后果有三个：

    1. **慢**：456 个 test_autotrade_* 用例，连着终端要 8.34s，断链后 4.27s。
       单用例 profile 显示时间全花在 ``_symbol_contract``（symbol_info）和
       ``fetch_ticks``（copy_ticks_range）这两条真实 IPC 上。
    2. **不确定**：终端没开、开着但没登录、被别的东西占着，跑出来的结果
       不一样；有 4 个用例甚至会因为读不到持仓而换一条分支直接失败。
    3. **有副作用**：连的是真实 demo 账户的连接，和正在跑的面板抢同一把锁。

    做法：把 ``live.journal._ensure`` 换成一个立刻抛错的函数。所有 MT5 调用
    都经 ``_mt5() -> _ensure()``，所以这一处就够。抛错不会让用例变红——生产
    代码本来就会捕获它并走"这一轮没行情"的兜底分支，那正是没装终端时的真实
    行为，反而比"假装连上了"更贴近要验的东西（各测试自己 mock 需要的数据）。

    少数**专门测连接逻辑**的用例（``test_live`` 里那几个）要真实实现，改用
    ``journal._ensure_real``——它们自己 mock 了 ``_terminal_alive`` 和
    ``MetaTrader5.initialize``，不会真的碰网络。

    ``AQA_ALLOW_MT5=1`` 可以关掉断链（排查"是不是断链导致的"时用）。
    """
    if os.environ.get("AQA_ALLOW_MT5"):
        return
    try:
        from live import journal
    except Exception:  # noqa: BLE001
        return
    if getattr(journal, "_ensure_real", None) is not None:
        return

    journal._ensure_real = journal._ensure

    def _refuse(allow_launch: bool = False):  # noqa: ARG001
        raise RuntimeError(
            "测试环境已禁用真实 MT5 连接（tests/_stubs.disable_live_mt5）；"
            "需要真实实现的用例请调 journal._ensure_real"
        )

    journal._ensure = _refuse
    journal._connected = False


def install_missing_stubs() -> None:
    if "MetaTrader5" not in sys.modules:
        try:
            import MetaTrader5  # noqa: F401
        except ModuleNotFoundError:
            mt5 = types.ModuleType("MetaTrader5")
            mt5.initialize = lambda *a, **k: True
            mt5.shutdown = lambda *a, **k: None
            mt5.last_error = lambda: (0, "stub")
            mt5.login = lambda *a, **k: True
            mt5.terminal_info = lambda *a, **k: None
            mt5.account_info = lambda *a, **k: None
            mt5.history_deals_get = lambda *a, **k: ()
            mt5.history_deals_total = lambda *a, **k: 0
            mt5.positions_get = lambda *a, **k: ()
            mt5.orders_get = lambda *a, **k: ()
            mt5.symbol_info = lambda *a, **k: None
            mt5.symbol_info_tick = lambda *a, **k: None
            mt5.symbol_select = lambda *a, **k: True
            mt5.copy_rates_from_pos = lambda *a, **k: None
            mt5.copy_ticks_range = lambda *a, **k: None
            # mt5.bridge 在模块顶层就引用这些常量，缺一个整个模块 import 失败
            mt5.TIMEFRAME_M1 = 1
            mt5.TIMEFRAME_M5 = 5
            mt5.TIMEFRAME_M15 = 15
            mt5.TIMEFRAME_M30 = 30
            mt5.TIMEFRAME_H1 = 16385
            mt5.TIMEFRAME_H4 = 16388
            mt5.TIMEFRAME_D1 = 16408
            mt5.COPY_TICKS_ALL = -1
            sys.modules["MetaTrader5"] = mt5

    if "openai" not in sys.modules:
        try:
            import openai  # noqa: F401
        except ModuleNotFoundError:
            openai = types.ModuleType("openai")
            openai.OpenAI = _StubOpenAI
            sys.modules["openai"] = openai

    # 顺手做数据隔离：所有测试模块 import 时都会走到这里，比逐文件加更可靠。
    try:
        isolate_test_data_root()
    except Exception as exc:  # noqa: BLE001
        # 隔离失败不能让整个套件在 import 阶段就崩，但必须喊一声——
        # 静默失败的结果就是又往真实账本里写了一批脏记录。
        print(f"WARN: 测试数据目录隔离失败，可能写入真实账本：{exc}",
              file=sys.stderr)

    # 断链放在最后：它依赖 live.journal 能 import（那个模块顶层并不连终端）。
    disable_live_mt5()
