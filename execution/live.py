"""MT5 实盘执行接口

⚠️ 安全约定：
  - **默认 dry_run=True**：只构造请求并打印，不发送
  - 真下单必须显式 `dry_run=False`，且建议先在 DEMO 账户验证至少 3 个月
  - 所有请求/响应都会记录，便于事后核对滑点与成交

用法：
    python -m execution.live --check           # 检查账户与交易权限（只读）
    python -m execution.live --quote           # 查询当前报价（只读）
    python -m execution.live --positions       # 查看持仓（只读）
    python -m execution.live --test-order      # dry-run 构造一笔订单（不发送）

不作为投资建议，下单前请自行确认参数。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import MetaTrader5 as mt5

from live import appdirs

ROOT = appdirs.app_root()
# 下单审计日志。走可写数据根——冻结后 _MEIPASS 是临时目录，写进去等于没写。
LOG_DIR = appdirs.data_dir() / "execution"


@dataclass
class ExecConfig:
    symbol: str = "XAUUSDm"
    magic: int = 20261019
    deviation_points: int = 30  # 允许滑点
    dry_run: bool = True
    terminal_path: str | None = None
    # 常驻进程（面板里的做单线程）必须置 True：
    # MT5 的 initialize/shutdown 是进程级引用计数，每发一单就 shutdown 一次，
    # 会把整个进程的连接关掉，下一轮连报价都拿不到。
    keep_alive: bool = False


class Executor:
    def __init__(self, cfg: ExecConfig | None = None) -> None:
        self.cfg = cfg or ExecConfig()
        self.connected = False
        self._log: list[dict] = []

    # ---------------------------------------------------------- 连接

    def connect(self, retries: int = 3) -> bool:
        kwargs = {}
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path

        if self.cfg.keep_alive:
            # 已经连上就直接复用，不要再 initialize 一次（引用计数会一直涨）
            if mt5.terminal_info() is not None:
                self.connected = True
                return True
            if mt5.initialize(**kwargs):
                self.connected = True
                return True
            self._record("connect_failed", {"error": str(mt5.last_error())})
            return False

        for attempt in range(1, retries + 1):
            if mt5.initialize(**kwargs):
                self.connected = True
                return True
            time.sleep(2)
        self._record("connect_failed", {"error": str(mt5.last_error())})
        return False

    def shutdown(self) -> None:
        if self.connected and not self.cfg.keep_alive:
            mt5.shutdown()
            self.connected = False

    # ---------------------------------------------------------- 只读

    def account(self) -> dict | None:
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "margin_free": info.margin_free,
            "currency": info.currency,
            "leverage": info.leverage,
            "trade_allowed": info.trade_allowed,
            "trade_expert": info.trade_expert,
        }

    def quote(self) -> tuple[float, float] | None:
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            return None
        return tick.bid, tick.ask

    def positions(self) -> list[dict]:
        items = mt5.positions_get(symbol=self.cfg.symbol) or []
        return [
            {
                "ticket": p.ticket,
                "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "magic": p.magic,
            }
            for p in items
        ]

    def check_trading_allowed(self) -> tuple[bool, str]:
        info = mt5.terminal_info()
        acct = mt5.account_info()
        if info is None or acct is None:
            return False, "终端或账户信息不可用"
        if not info.trade_allowed:
            return False, "终端未开启算法交易（AutoTrading 按钮）"
        if not acct.trade_allowed:
            return False, "账户不允许交易"
        if not info.connected:
            return False, "未连接到交易服务器"
        return True, "可交易"

    # ---------------------------------------------------------- 下单底层

    def _ensure_symbol(self) -> None:
        """确保品种在 Market Watch 里。

        不在的话 `symbol_info_tick` 返回 None、`order_send` 报「未知品种」，
        表面上像行情断了，实际只是没勾选这个品种。
        """
        try:
            info = mt5.symbol_info(self.cfg.symbol)
            if info is None:
                return
            if not info.visible:
                mt5.symbol_select(self.cfg.symbol, True)
        except Exception:  # noqa: BLE001
            pass

    def _filling_modes(self) -> list[int]:
        """券商支持的成交方式，按优先级排。

        很多券商（Exness 等）对市价单只认其中一种，传错直接返回 10030
        「不支持的成交方式」，而这条错误信息里根本不提 filling——
        硬编码 ORDER_FILLING_IOC 的结果就是真单一笔都发不出去。
        """
        fok = getattr(mt5, "ORDER_FILLING_FOK", 0)
        ioc = getattr(mt5, "ORDER_FILLING_IOC", 1)
        ret = getattr(mt5, "ORDER_FILLING_RETURN", 2)
        modes: list[int] = []
        try:
            info = mt5.symbol_info(self.cfg.symbol)
            fm = int(getattr(info, "filling_mode", 0) or 0)
            if fm & 1:  # SYMBOL_FILLING_FOK
                modes.append(fok)
            if fm & 2:  # SYMBOL_FILLING_IOC
                modes.append(ioc)
        except Exception:  # noqa: BLE001
            pass
        for m in (ioc, fok, ret):
            if m not in modes:
                modes.append(m)
        return modes

    def _send(self, request: dict):
        """发单；遇到「不支持的成交方式」就换下一种重试。

        返回 (mt5 结果, 说明)。说明里记下试过哪些 filling，
        这样失败时能看出是券商拒绝成交方式，不是价格或手数的问题。
        """
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        tried: list[dict] = []
        result = None
        for mode in self._filling_modes():
            req = dict(request)
            req["type_filling"] = mode
            result = mt5.order_send(req)
            rc = getattr(result, "retcode", None)
            tried.append({"filling": mode, "retcode": rc})
            if rc == done or rc != invalid_fill:
                return result, {"tried": tried}
        return result, {"tried": tried,
                        "error": "所有成交方式都被券商拒绝（10030）"}

    # ---------------------------------------------------------- 下单

    def open_market(
        self,
        direction: int,  # +1 多 / -1 空
        lots: float,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "aqa",
    ) -> dict:
        """市价开仓。dry_run=True 时只返回构造好的请求。"""
        if not self.connected and not self.connect():
            raise RuntimeError("MT5 连接失败")
        self._ensure_symbol()

        tick = mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            raise RuntimeError(f"无法获取 {self.cfg.symbol} 报价")

        price = tick.ask if direction > 0 else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.cfg.symbol,
            "volume": round(lots, 2),
            "type": order_type,
            "price": price,
            "sl": sl or 0.0,
            "tp": tp or 0.0,
            "deviation": self.cfg.deviation_points,
            "magic": self.cfg.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # 参数自检：止损方向、距离
        issues: list[str] = []
        if sl:
            if direction > 0 and sl >= price:
                issues.append("多单止损必须低于入场价")
            if direction < 0 and sl <= price:
                issues.append("空单止损必须高于入场价")
        if tp:
            if direction > 0 and tp <= price:
                issues.append("多单止盈必须高于入场价")
            if direction < 0 and tp >= price:
                issues.append("空单止盈必须低于入场价")
        if issues:
            self._record("order_rejected", {"issues": issues, "request": request})
            return {"ok": False, "reason": "参数非法", "issues": issues}

        if self.cfg.dry_run:
            self._record("dry_run_order", request)
            return {"ok": True, "dry_run": True, "request": request}

        result, meta = self._send(request)
        payload = {
            "retcode": result.retcode if result else None,
            "comment": result.comment if result else None,
            "order": result.order if result else None,
            "price": result.price if result else None,
            "request_price": price,
            "filling": meta,
        }
        ok = bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)
        if ok:
            payload["slippage_points"] = round(abs(result.price - price) / 0.001, 0)
        self._record("order_sent" if ok else "order_failed", payload)
        return {"ok": ok, **payload}

    def close_position(self, ticket: int) -> dict:
        self._ensure_symbol()
        positions = mt5.positions_get(ticket=ticket) or []
        if not positions:
            return {"ok": False, "reason": "持仓不存在"}
        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        direction = -1 if pos.type == mt5.POSITION_TYPE_BUY else 1
        price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": self.cfg.deviation_points,
            "magic": self.cfg.magic,
            "comment": "aqa close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if self.cfg.dry_run:
            self._record("dry_run_close", request)
            return {"ok": True, "dry_run": True, "request": request}

        result, meta = self._send(request)
        ok = bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)
        self._record("close_sent" if ok else "close_failed",
                     {"retcode": getattr(result, "retcode", None), **meta})
        return {"ok": ok, "retcode": getattr(result, "retcode", None),
                "comment": getattr(result, "comment", None), "filling": meta}

    # ---------------------------------------------------------- 记录

    def _record(self, kind: str, payload: dict) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "payload": payload}
        self._log.append(entry)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"{time.strftime('%Y%m%d')}.jsonl"
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MT5 执行接口（默认 dry-run）")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--quote", action="store_true")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--test-order", action="store_true")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--live", action="store_true", help="关闭 dry-run（危险）")
    args = parser.parse_args()

    ex = Executor(ExecConfig(symbol=args.symbol, dry_run=not args.live))
    if not ex.connect():
        print(f"连接失败：{mt5.last_error()}")
        print("提示：需要先手动打开 MT5 终端并登录")
        return

    try:
        print("账户：", json.dumps(ex.account(), ensure_ascii=False))
        print("可交易：", ex.check_trading_allowed())

        if args.quote or args.check:
            print("报价：", ex.quote())
        if args.positions or args.check:
            print("持仓：", ex.positions())

        if args.test_order:
            bid, ask = ex.quote()
            result = ex.open_market(direction=1, lots=0.01, sl=bid - 5.0, tp=bid + 10.0)
            print("测试订单：", json.dumps(result, ensure_ascii=False, default=str))
    finally:
        ex.shutdown()


if __name__ == "__main__":
    main()
