"""决策唤醒对齐 K 线收盘（2026-11-10）。

起因：唤醒点算的是 `started + interval_sec`，而 started 是上一轮**实际**返回的
时刻。尾段"不足 1 秒也保底睡 1 秒"、快查的提前唤醒（止损/保本/tp/停滞）、辅助
模式从 tick 结束才计时——三样都会把基准搬到任意一秒，之后没有任何东西拉回来。
实测按会话切开，段内相位标准差中位 15.5~15.9 秒：同一套判据，有时量的是刚开了
2 秒的半根 K 线，有时量的是完整的。

要钉死的四件事：
  1. **落在网格上**：`k × interval + settle`，一分不差；
  2. **绝不下穿下界**：唤醒点 ≥ started + wait（那是冷却/周期写下的地板，
     早醒等于绕过它）；
  3. **稳态不翻倍**：从网格点出发再来一轮，正好间隔一个周期——按"下界向上取整"
     的实现很容易在这里退化成 2×周期，这条就是防它的；
  4. **关掉就回到老行为**：settle=0 时必须是 started + wait，不能偷偷对齐。
"""

from __future__ import annotations

import unittest
from unittest import mock

from live import autotrade
from tests._autotrade_base import TempLedger

IV = 60
SETTLE = 3.0


def _cfg(**kw) -> autotrade.Cfg:
    base = {"interval_sec": IV, "align_settle_sec": SETTLE}
    base.update(kw)
    return autotrade.Cfg(**base)


def _on_grid(t: float, iv: float = IV, settle: float = SETTLE) -> bool:
    """t 是不是 `k×iv + settle`。"""
    r = (t - settle) % iv
    return r < 1e-6 or abs(r - iv) < 1e-6


class TestWaitDeadline(unittest.TestCase):
    """纯函数：给"起点 + 想等多久"，返回"等到哪个时刻"。"""

    def test_lands_on_grid(self):
        cfg = _cfg()
        # 相位 0~59 全扫一遍：既要落在网格上，又不能早于下界，还不能多等一个周期
        for phase in range(0, 60):
            started = 1_000_000.0 + phase
            d = autotrade._wait_deadline(started, 60.0, cfg)
            self.assertTrue(_on_grid(d), f"phase={phase} -> {d} 不在网格上")
            self.assertGreaterEqual(d - started, 60 - 1e-6,
                                    f"phase={phase} 早于下界：{d - started:.3f}s")
            self.assertLess(d - started, 60 + IV, f"phase={phase} 多等了一个周期")

    def test_steady_state_is_exactly_one_period(self):
        """这条防的是最隐蔽的退化：从网格点出发却等到下下个网格点（周期翻倍）。"""
        cfg = _cfg()
        t = autotrade._wait_deadline(1_000_000.0, 60.0, cfg)
        for _ in range(5):
            nxt = autotrade._wait_deadline(t, 60.0, cfg)
            self.assertAlmostEqual(nxt - t, 60.0, places=6)
            t = nxt

    def test_floor_is_respected_when_cooldown_is_shorter(self):
        """冷却 30 秒 → wait=31，唤醒点必须 ≥ 31 秒后（落在下一根收盘也无所谓）。"""
        cfg = _cfg(cooldown_sec=30)
        started = 1_000_040.0
        d = autotrade._wait_deadline(started, 31.0, cfg)
        self.assertGreaterEqual(d - started, 31 - 1e-6)
        self.assertTrue(_on_grid(d))

    def test_align_off_is_free_running(self):
        cfg = _cfg(align_settle_sec=0)
        for phase in (0, 17, 43):
            started = 1_000_000.0 + phase
            self.assertEqual(autotrade._wait_deadline(started, 60.0, cfg),
                             started + 60.0)

    def test_interval_is_the_grid(self):
        cfg = _cfg(interval_sec=180)
        d = autotrade._wait_deadline(1_000_000.0, 180.0, cfg)
        self.assertTrue(_on_grid(d, iv=180))
        self.assertGreaterEqual(d - 1_000_000.0, 180 - 1e-6)

    def test_tiny_interval_does_not_degenerate(self):
        """interval_sec=3（面板最小档）+ settle 3 → settle 夹到半个周期，不能失效。"""
        cfg = _cfg(interval_sec=3)
        d = autotrade._wait_deadline(1_000_000.0, 3.0, cfg)
        self.assertTrue(_on_grid(d, iv=3, settle=1.5))
        self.assertGreaterEqual(d - 1_000_000.0, 3 - 1e-6)

    def test_negative_settle_rejected(self):
        issues = autotrade.validate_cfg(autotrade.Cfg(align_settle_sec=-1))
        self.assertTrue(any("对齐偏移" in x for x in issues))

    def test_default_is_on(self):
        """默认必须开着：关掉它等于把"在半根 K 线上判方向"当默认行为。"""
        self.assertEqual(autotrade.Cfg().align_settle_sec, SETTLE)
        self.assertIn("align_settle_sec", autotrade.CODE_ONLY)


class TestWaitNextAligned(TempLedger):
    """接线：_wait_next 真的用上了它，且没把快查循环搞坏。"""

    class _Clock:
        def __init__(self, t: float = 1_000_000.0):
            self.t = t
            self.sleeps: list[float] = []

        def time(self) -> float:
            return self.t

        def sleep(self, sec: float) -> None:
            self.sleeps.append(round(sec, 2))
            self.t += sec

    def _position(self) -> dict:
        state = autotrade._empty_state()
        pos = {"ticket": -1, "direction": 1, "lots": 0.01, "price_open": 4000.0,
               "opened_at": 1_000_000, "p_up": 0.6, "entry_reason": "突破",
               "dry_run": True}
        state["position"] = pos
        autotrade.save_state(state)
        return pos

    def test_flat_round_wakes_on_the_bar_close(self):
        clock = self._Clock()
        cfg = _cfg(guard_sec=5)
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(clock.t, {"kind": "skip"}, cfg,
                                 sleeper=clock.sleep)
        self.assertEqual(len(clock.sleeps), 1)          # 空仓：一次睡到点
        self.assertTrue(_on_grid(clock.t))
        self.assertGreaterEqual(clock.t - 1_000_000.0, IV)

    def test_two_cycles_are_one_period_apart(self):
        """第二轮必须正好 60 秒，不能被 tick 耗时顶到下一根（周期翻倍）。"""
        cfg = _cfg(guard_sec=0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(clock.t, {"kind": "skip"}, cfg,
                                 sleeper=clock.sleep)
            first = clock.t
            clock.sleeps.clear()
            autotrade._wait_next(first, {"kind": "skip"}, cfg,
                                 sleeper=clock.sleep)
        self.assertEqual(clock.sleeps, [60.0])

    def test_guard_loop_still_runs_inside_aligned_wait(self):
        """对齐只改"睡到几点"，不改"每格几秒看一眼止损"。"""
        self._position()
        cfg = _cfg(guard_sec=5, sl_usd=3.0, tp_min_usd=3.25)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (4000.2, 4000.44))
        # 快查循环的尾段是"保底睡 1 秒"，允许越过网格点 1 秒以内
        r = (clock.t - SETTLE) % IV
        self.assertLess(min(r, IV - r), 1.5, f"结束在 {clock.t}，不在网格附近")
        self.assertGreaterEqual(clock.t - 1_000_000.0, IV)
        # 全程按 5 秒一格走，不是一口气睡到收盘
        self.assertGreater(len(clock.sleeps), 10)
        self.assertTrue(all(s <= 5.0 for s in clock.sleeps))
        self.assertGreaterEqual(sum(1 for s in clock.sleeps if s == 5.0), 15)

    def test_stop_loss_still_wakes_before_the_aligned_point(self):
        """对齐不能拖慢止损：过线就在第一格返回，不等收盘。"""
        self._position()
        cfg = _cfg(guard_sec=5, sl_usd=3.0)
        clock = self._Clock()
        with mock.patch.object(autotrade.time, "time", clock.time):
            woke = autotrade._wait_next(
                clock.t, {"kind": "hold"}, cfg, sleeper=clock.sleep,
                quote_fn=lambda s: (3996.5, 3996.74))
        self.assertFalse(woke)
        self.assertLessEqual(clock.t - 1_000_000.0, 5)


if __name__ == "__main__":
    unittest.main()
