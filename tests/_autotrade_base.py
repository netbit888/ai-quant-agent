"""test_autotrade 系列共用的小工具与常量。

从 test_autotrade.py 拆出来的（2026-10-29）：原来那个文件 5203 行、72 个类，
改一处功能的 diff 全埋在里面。**类体逐字未动**，只是搬家。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests._stubs import install_missing_stubs

install_missing_stubs()   # 断链真实 MT5 + 隔离账本目录，必须早于 live 的 import

from live import paths


NOW = 1_790_000_000


def bar(o, h, l, c, t=NOW):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def bars_up(n=30):
    return [bar(4000 + i, 4001 + i, 3999 + i, 4000.5 + i, NOW + i * 60) for i in range(n)]


def bars_down(n=30):
    return [bar(4000 - i, 4001 - i, 3999 - i, 3999.5 - i, NOW + i * 60) for i in range(n)]


class TempLedger(unittest.TestCase):
    """每个用例独立的账本目录，绝不碰真实账户数据。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()


def ticks_for(bars, per_bar=3, step=0.1, spread=0.24):
    out = []
    for b in bars:
        t0 = int(b["time"])
        for k in range(per_bar):
            out.append({"time": t0 + k, "time_msc": 0,
                        "bid": 4000 + k * step, "ask": 4000 + k * step + spread,
                        "last": 0.0, "volume": 1})
    return out


def bars_from_closes(closes):
    return [{"time": NOW + i * 60, "open": c, "high": c + 1, "low": c - 1, "close": c}
            for i, c in enumerate(closes)]


def bars_range(n=30):
    """震荡 K 线（ER≈0.08）：锚点机制的活水场景。单边 bars_up/bars_down
    从 2026-10-28 起是趋势市，锚点整体停用，不能拿来测锚点接线。"""
    return bars_from_closes([4000 + (2 if i % 2 else 0) for i in range(n)])


class _FakeClock:
    """假时钟。等待逻辑必须能"不真等 90 秒"地测，否则没人会跑这些用例。"""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.t += float(sec)


def _sig(clock_start: float | None = None, wait: int = 90, kind: str = "entry",
         price: float = 4000.24, bid: float = 4000.0, ask: float = 4000.24,
         dry_run: bool = True) -> dict:
    # 默认用真实时钟做基准：answer_signal 会用真实 time.time() 判过期，
    # 用假基准会让"还没过期"的用例直接被拒。
    base = float(time.time() if clock_start is None else clock_start)
    return {"id": "sig-" + os.urandom(3).hex(), "kind": kind,
            "ts": int(base), "expires_at": int(base + wait),
            "direction": 1, "p_up": 0.62, "prog_dir": 0, "agree": None,
            "act": True, "advice": None, "advice_text": None,
            "reason": "结构支持", "bid": bid, "ask": ask, "price": price,
            "feat": {}, "ticket": None, "held_sec": None, "fp": None,
            "dry_run": dry_run, "wait_sec": wait}
