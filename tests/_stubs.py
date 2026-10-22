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

import sys
import types


class _StubOpenAI:
    """占位客户端。真实客户端应由测试 mock 掉；走到这里说明测试漏了 mock。"""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("测试桩：不应真实创建 OpenAI 客户端，请 mock agent.llm.chat_ex")


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
            sys.modules["MetaTrader5"] = mt5

    if "openai" not in sys.modules:
        try:
            import openai  # noqa: F401
        except ModuleNotFoundError:
            openai = types.ModuleType("openai")
            openai.OpenAI = _StubOpenAI
            sys.modules["openai"] = openai
