"""回测引擎

模型对齐 MT5 的「仅开盘价」（Open prices only）：
  - 信号基于上一根 bar 收盘后的指标，在当前 bar 的 open 成交
  - 持仓的 SL/TP 用当前 bar 的 high/low 判断是否触发
  - 同一根 bar 内 SL 与 TP 都可能触及时，取距离开仓价更近的那个（保守假设）

成本模型：
  - 每笔往返扣一次点差（spread_points × point × lots × contract_size）
  - 暂不建模手续费/隔夜利息
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .models import Bar, BacktestResult, Position, Trade


@dataclass
class EngineConfig:
    initial_balance: float = 10_000.0
    lots: float = 0.1
    point: float = 0.001
    contract_size: float = 100.0
    spread_points: int | None = None  # None = 用每根 bar 自带的 spread
    reverse_on_signal: bool = True  # 反向信号直接反手

    # ---- 币种换算 ----
    # 引擎的盈亏公式 (price-entry)*lots*contract 得到的是**报价货币**金额。
    # 账户是 USD，所以 JPY 报价的品种（USDJPY/JP225）必须换算，否则手数会
    # 被算大两个数量级、被风控整批拒绝。
    #   quote_rate            : 1 USD 等于多少报价货币（HKD 7.8 / GBP 0.79 ...）
    #   quote_rate_from_price : 报价货币就是「USD/XXX」里的 XXX 时（如 USDJPY），
    #                           直接用当根收盘价作为汇率，精确且随时间变化
    quote_rate: float = 1.0
    quote_rate_from_price: bool = False

    # ---- 持仓管理（默认全关，关闭时行为与旧版完全一致）----
    # R = |开仓价 - 初始止损|，是这笔交易的风险单位。
    be_at_r: float = 0.0  # 浮盈达 N×R 后把止损移到开仓价（保本），0=关闭
    trail_start_r: float = 0.0  # 浮盈达 N×R 后启动追踪止损，0=关闭
    trail_gap_r: float = 1.0  # 追踪间距 = N×R（仅在 trail_start_r>0 时生效）


class Strategy(Protocol):
    def prepare(self, bars: Sequence[Bar]) -> None: ...
    def signal(self, i: int) -> int: ...  # +1 多 / -1 空 / 0 无
    def sl_tp(self, i: int, entry: float, direction: int) -> tuple[float | None, float | None]: ...


def _spread_price(bar: Bar, cfg: EngineConfig) -> float:
    points = cfg.spread_points if cfg.spread_points is not None else bar.spread
    return points * cfg.point


def _quote_rate(bar: Bar, cfg: EngineConfig) -> float:
    """1 USD = 多少报价货币。用于把盈亏换算回账户货币。"""
    if cfg.quote_rate_from_price:
        return bar.close if bar.close > 0 else 1.0
    return cfg.quote_rate if cfg.quote_rate > 0 else 1.0


def _usd_contract(cfg: EngineConfig, bar: Bar | None = None) -> float:
    """合约价值换算成 USD 后的等效 contract_size（用于手数计算）。"""
    rate = (_quote_rate(bar, cfg) if bar is not None else (cfg.quote_rate or 1.0))
    return cfg.contract_size / rate


def _check_exit(bar: Bar, pos: Position) -> tuple[float, str] | None:
    """返回 (成交价, 原因)；未触发返回 None。

    按 MT5「仅开盘价」的价格路径顺序判断：
      阳线（close >= open）：open → low → high → close
      阴线（close <  open）：open → high → low → close
    谁先被触到谁生效，而不是简单地取"离入场价更近的那个"。
    这一条决定了胜率量级——用错顺序会让每笔都判成止损。
    """
    if bar.close >= bar.open:
        path = (bar.open, bar.low, bar.high, bar.close)
    else:
        path = (bar.open, bar.high, bar.low, bar.close)

    for price in path:
        if pos.direction > 0:
            if pos.sl is not None and price <= pos.sl:
                return pos.sl, "sl"
            if pos.tp is not None and price >= pos.tp:
                return pos.tp, "tp"
        else:
            if pos.sl is not None and price >= pos.sl:
                return pos.sl, "sl"
            if pos.tp is not None and price <= pos.tp:
                return pos.tp, "tp"
    return None


def _manage_position(bar: Bar, pos: Position, risk_dist: float, best: float, cfg: EngineConfig) -> float:
    """按当前 bar 的极值上移/下移止损，返回更新后的最优价。

    保守原则：本函数在「当前 bar 的 SL/TP 已经判定完毕」之后调用，因此
    调整后的止损从**下一根** bar 才生效，不会出现同根 bar 内先看最高价、
    再用该最高价设止损、然后假设止损被触发的未来函数。

    best: 持仓期间见过的最有利价格（多单是最高价，空单是最低价）。
    """
    if pos.direction > 0:
        best = max(best, bar.high)
    else:
        best = min(best, bar.low)

    if risk_dist <= 0:
        return best

    if pos.direction > 0:
        mfe_r = (best - pos.entry_price) / risk_dist
    else:
        mfe_r = (pos.entry_price - best) / risk_dist

    new_sl = pos.sl
    if cfg.be_at_r > 0 and mfe_r >= cfg.be_at_r:
        cand = pos.entry_price
        new_sl = cand if new_sl is None else max(new_sl, cand)

    if cfg.trail_start_r > 0 and mfe_r >= cfg.trail_start_r:
        gap = cfg.trail_gap_r * risk_dist
        cand = best - gap if pos.direction > 0 else best + gap
        if pos.direction > 0:
            new_sl = cand if new_sl is None else max(new_sl, cand)
        else:
            new_sl = cand if new_sl is None else min(new_sl, cand)

    # 止损只能朝有利方向移动，绝不回退
    if pos.sl is None:
        pos.sl = new_sl
    elif new_sl is not None:
        pos.sl = max(pos.sl, new_sl) if pos.direction > 0 else min(pos.sl, new_sl)
    return best


def run_backtest(
    bars: Sequence[Bar], strategy: Strategy, cfg: EngineConfig, guard=None
) -> BacktestResult:
    """guard: 可选的 RiskGuard。传入后由它决定「是否允许开仓」和「手数大小」。"""
    if len(bars) < 3:
        raise ValueError("bar 数量太少")

    strategy.prepare(bars)

    balance = cfg.initial_balance
    pos: Position | None = None
    trades: list[Trade] = []
    equity: list[tuple[int, float]] = [(bars[0].time, balance)]

    def close_pos(bar: Bar, price: float, reason: str) -> None:
        nonlocal balance, pos
        if pos is None:
            return
        gross = (price - pos.entry_price) * pos.direction * pos.lots * cfg.contract_size
        cost = _spread_price(bar, cfg) * pos.lots * cfg.contract_size
        rate = _quote_rate(bar, cfg) if (cfg.quote_rate_from_price or cfg.quote_rate != 1.0) else 1.0
        profit = (gross - cost) / rate
        # 真实账户余额不会被扣成负数：亏光本金即强平。回测同样处理。
        if balance + profit < 0:
            profit = -balance
        balance += profit
        trades.append(
            Trade(
                direction=pos.direction,
                entry_time=pos.entry_time,
                entry_price=pos.entry_price,
                exit_time=bar.time,
                exit_price=price,
                lots=pos.lots,
                profit=profit,
                exit_reason=reason,
                sl=pos.sl,
                tp=pos.tp,
            )
        )
        if guard is not None:
            guard.on_trade_closed(profit, bar.time)
        pos = None

    bankrupt_at: int | None = None
    risk_dist = 0.0  # 当前持仓的 1R 距离
    best_price = 0.0  # 当前持仓期间的最有利价

    for i in range(1, len(bars)):
        bar = bars[i]

        # 爆仓保护：真实账户余额归零会被强制平仓，回测必须同样处理，
        # 否则会出现"亏掉的钱超过本金"这种不可能的曲线。
        if balance <= 0:
            bankrupt_at = bar.time
            break

        if guard is not None:
            guard.on_bar(bar.time, balance)

        # 1) 用上一根 bar 的信号，在当前 bar 开盘价执行
        sig = strategy.signal(i - 1)
        if sig and pos is not None and sig == -pos.direction and cfg.reverse_on_signal:
            close_pos(bar, bar.open, "reverse")

        if sig and pos is None:
            sl, tp = strategy.sl_tp(i - 1, bar.open, sig)

            lots = cfg.lots
            if guard is not None:
                ok, _reason = guard.allow(bar.time, balance)
                if not ok:
                    sig = 0
                else:
                    stop_dist = abs(bar.open - sl) if sl is not None else 0.0
                    lots = guard.lots(balance, stop_dist, _usd_contract(cfg, bar))
                    if lots <= 0:
                        sig = 0

            if sig:
                pos = Position(
                    direction=sig,
                    entry_time=bar.time,
                    entry_price=bar.open,
                    lots=lots,
                    sl=sl,
                    tp=tp,
                )
                # 记录这笔交易的风险单位 R 与最有利价，供保本/追踪止损使用
                risk_dist = abs(bar.open - sl) if sl is not None else 0.0
                best_price = bar.open

        # 2) 持仓的 SL/TP（用当前 bar 的极值）
        if pos is not None:
            hit = _check_exit(bar, pos)
            if hit:
                price, reason = hit
                close_pos(bar, price, reason)
            elif cfg.be_at_r > 0 or cfg.trail_start_r > 0:
                # 未触发离场才管理止损，调整从下一根 bar 生效
                best_price = _manage_position(bar, pos, risk_dist, best_price, cfg)

        equity.append((bar.time, balance))

    # 收尾：最后一根强制平仓
    if pos is not None:
        close_pos(bars[-1], bars[-1].close, "end")
        equity.append((bars[-1].time, balance))

    return BacktestResult(
        trades=trades,
        equity_curve=equity,
        initial_balance=cfg.initial_balance,
        final_balance=balance,
        config={
            "lots": cfg.lots,
            "point": cfg.point,
            "contract_size": cfg.contract_size,
            "spread_points": cfg.spread_points,
            "bars": len(bars),
            "risk": guard.summary() if guard is not None else None,
            "bankrupt_at": bankrupt_at,
        },
    )
