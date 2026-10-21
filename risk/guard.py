"""风控层：仓位管理 + 多层熔断

设计原则：风控只在「开新仓」这一个点上介入，不干扰策略逻辑。
四条规则，任何一条触发即拒绝开仓：

  1. 单笔风险 = 账户净值的固定百分比（默认 1%），据此反算手数
  2. 日亏损熔断：当日累计亏损达到 X% 停到次日
  3. 连续亏损停手：连亏 N 笔后冷却 M 根 bar
  4. 总回撤熔断：从净值峰值回撤达到 X% 直接停系统

手数计算：
  risk_amount = balance * risk_pct / 100
  lots = risk_amount / (|entry - sl| * contract_size)
  向下取整到 0.01 步长，夹在 [min_lots, max_lots] 之间
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0  # 单笔风险占净值 %
    daily_loss_limit_pct: float = 3.0  # 日亏损熔断 %
    max_consecutive_losses: int = 6  # 连亏多少笔停手
    cooldown_bars: int = 24  # 连亏后冷却多少根 bar
    max_drawdown_pct: float = 25.0  # 总回撤熔断 %
    min_lots: float = 0.01
    max_lots: float = 1.0
    lot_step: float = 0.01


@dataclass
class RiskStats:
    blocked_by_daily: int = 0
    blocked_by_streak: int = 0
    blocked_by_drawdown: int = 0
    blocked_by_size: int = 0
    halted: bool = False
    max_drawdown_seen: float = 0.0


class RiskGuard:
    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self.cfg = cfg or RiskConfig()
        self.peak_equity = 0.0
        self.day_key: int | None = None
        self.day_start_equity = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = 0
        self.stats = RiskStats()

    # ---------------------------------------------------------- 状态更新

    def on_bar(self, ts: int, balance: float) -> None:
        """每根 bar 调用：更新净值峰值、日切。"""
        self.peak_equity = max(self.peak_equity, balance)

        # 日切（按 UTC 天）
        day = ts // 86400
        if day != self.day_key:
            self.day_key = day
            self.day_start_equity = balance

        if self.peak_equity > 0:
            dd = (self.peak_equity - balance) / self.peak_equity * 100
            self.stats.max_drawdown_seen = max(self.stats.max_drawdown_seen, dd)
            if dd >= self.cfg.max_drawdown_pct:
                self.stats.halted = True

    def on_trade_closed(self, profit: float, ts: int) -> None:
        if profit <= 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = ts + self.cfg.cooldown_bars * 3600
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    # ---------------------------------------------------------- 开仓许可

    def allow(self, ts: int, balance: float) -> tuple[bool, str]:
        # 先判回撤（更具体的拒绝原因），再判是否已被熔断
        if self.peak_equity > 0 and balance > 0:
            dd = (self.peak_equity - balance) / self.peak_equity * 100
            if dd >= self.cfg.max_drawdown_pct:
                self.stats.halted = True
                self.stats.blocked_by_drawdown += 1
                return False, "drawdown"

        if self.stats.halted:
            return False, "halted"

        if self.day_start_equity > 0:
            daily_pnl_pct = (balance - self.day_start_equity) / self.day_start_equity * 100
            if daily_pnl_pct <= -self.cfg.daily_loss_limit_pct:
                self.stats.blocked_by_daily += 1
                return False, "daily"

        if ts < self.cooldown_until:
            self.stats.blocked_by_streak += 1
            return False, "streak"

        return True, "ok"

    # ---------------------------------------------------------- 手数

    def lots(self, balance: float, stop_distance: float, contract_size: float) -> float:
        """按固定风险百分比反算手数；止损距离非法时返回 0（拒绝交易）。"""
        if stop_distance <= 0 or contract_size <= 0 or balance <= 0:
            self.stats.blocked_by_size += 1
            return 0.0

        risk_amount = balance * self.cfg.risk_per_trade_pct / 100
        raw = risk_amount / (stop_distance * contract_size)

        steps = int(raw / self.cfg.lot_step)
        lots = round(steps * self.cfg.lot_step, 2)
        lots = max(self.cfg.min_lots, min(self.cfg.max_lots, lots))

        # 最小手数下的实际风险是否超过上限（超了就拒绝，宁可不做）
        actual_risk = lots * stop_distance * contract_size
        if actual_risk > risk_amount * 1.5:
            self.stats.blocked_by_size += 1
            return 0.0

        return lots

    # ---------------------------------------------------------- 报告

    def summary(self) -> dict:
        return {
            "blocked_by_daily": self.stats.blocked_by_daily,
            "blocked_by_streak": self.stats.blocked_by_streak,
            "blocked_by_drawdown": self.stats.blocked_by_drawdown,
            "blocked_by_size": self.stats.blocked_by_size,
            "halted": self.stats.halted,
            "max_drawdown_seen": round(self.stats.max_drawdown_seen, 2),
        }
