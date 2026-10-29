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

from live import appdirs, mt5lock
from live.mt5lock import MT5Busy

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
        """建立连接。走闸门，不是裸锁。

        连接是唯一可能"几十秒不返回"的操作（`initialize` 内部超时 30 秒，
        常驻进程还可能先拉起终端）。如果靠锁排队，面板轮询和 2 秒一格的
        止损快查会跟着停——那是最不能停的路径。闸门在持锁后关闭，期间
        新调用立刻 `MT5Busy` 快速失败，业务层按"这一轮没行情"跳过。
        """
        if self.connected:
            return True
        kwargs = {}
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path

        try:
            with mt5lock.connecting():
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
                    if attempt < retries:
                        time.sleep(2)
                self._record("connect_failed", {"error": str(mt5.last_error())})
                return False
        except MT5Busy as exc:
            # 另一条线程正在连接：不排队、不重复 initialize，直接报失败
            self._record("connect_busy", {"error": str(exc)})
            return False

    def shutdown(self) -> None:
        if not (self.connected and not self.cfg.keep_alive):
            return
        try:
            mt5lock.call(mt5.shutdown)
        except MT5Busy:
            return
        self.connected = False

    # ---------------------------------------------------------- 只读

    def account(self) -> dict | None:
        info = mt5lock.call(mt5.account_info)
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
        """报价。**拿不到锁就当这一轮没有报价**，不排队。

        面板 1.5 秒轮询一次、止损快查 2~5 秒一格，都是"这一格没有就算了"
        的调用。等锁等于把下单的耗时转嫁给止损判定——宁可返回 None 让上层
        跳过这一轮。
        """
        tick = mt5lock.call_quick(mt5.symbol_info_tick, self.cfg.symbol,
                                  timeout=0.3)
        if tick is None:
            return None
        return tick.bid, tick.ask

    def positions(self) -> list[dict]:
        """当前持仓。读失败抛异常——`None` 被当成"没有持仓"是开仓事故的源头。"""
        items = mt5lock.call(mt5.positions_get, symbol=self.cfg.symbol)
        if items is None:
            raise RuntimeError(f"读不到持仓: {mt5.last_error()}")
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
        # 两个快照必须成对读：中间插进一次别的调用，可能读到"终端允许、
        # 账户已禁"的错位组合，判出来的结论不可信。
        with mt5lock.lock():
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

        查可见性和勾选必须原子：中间被别人插一次（切品种），勾完的就不是
        原来那个了。
        """
        try:
            with mt5lock.lock():
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
            info = mt5lock.call(mt5.symbol_info, self.cfg.symbol)
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

        **每次 order_send 单独拿锁，整段不持锁**：券商无响应时单次
        `order_send` 自己就会卡，不能再把锁一起带走——否则一次卡单会连带
        停掉报价和止损快查。
        """
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        tried: list[dict] = []
        result = None
        for mode in self._filling_modes():
            req = dict(request)
            req["type_filling"] = mode
            result = mt5lock.call(mt5.order_send, req)
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
        """市价开仓。dry_run=True 时只返回构造好的请求。

        **整段不持锁**，只有真正的接口调用各拿一次。参数自检、组请求、
        写审计日志都在锁外——它们不碰 MT5，占着锁只会拖累报价和止损快查。
        """
        if not self.connected and not self.connect():
            raise RuntimeError("MT5 连接失败")
        self._ensure_symbol()

        tick = mt5lock.call(mt5.symbol_info_tick, self.cfg.symbol)
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
        """平掉指定持仓。

        持仓快照和报价必须成对读：中间插一次别的调用，"按 A 的价格去平 B"
        这种错位在平仓路径上是直接损失。
        """
        self._ensure_symbol()
        with mt5lock.lock():
            positions = mt5.positions_get(ticket=ticket) or []
            if not positions:
                return {"ok": False, "reason": "持仓不存在"}
            pos = positions[0]
            tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return {"ok": False, "reason": f"无法获取 {pos.symbol} 报价"}
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

    def modify_sl(self, ticket: int, sl: float) -> dict:
        """把持仓的硬止损改到指定价，**不动止盈**。

        为什么要走券商端而不是让程序轮询着市价平：
          · 止损挂在券商那边，进程崩了、面板关了、网络断一下它仍然有效；
          · 成交就在设的那个价上——实测 5 秒轮询把 −3.00 打成 −3.39
            （超调 13%），改单没有这个损耗。

        保本止损（SL 抬到开仓价）是它的第一个用户。

        持仓快照和请求必须成对读：中间插一次别的调用，就可能把止损改到
        另一笔仓上（ticket 已经换了），那是直接的钱。

        **拿不到锁就直接返回 busy，不排队**：调用方（5 秒一格的止损快查）
        不能为了改单停在那里。MT5 的 `initialize` 最长 30 秒且全程持锁，
        排队等于把止损判定停半分钟——那是最不能停的路径。
        """
        self._ensure_symbol()
        # call_quick + 显式区分 None（读不到）与 []（没有这笔仓）：
        # 把"终端忙"翻成"持仓不存在"会让调用方以为仓位已经没了。
        positions = mt5lock.call_quick(mt5.positions_get, ticket=ticket,
                                       timeout=0.5, default=None)
        if positions is None:
            return {"ok": False, "busy": True, "reason": "终端忙，这一格改单作废"}
        if not positions:
            return {"ok": False, "reason": "持仓不存在"}
        pos = positions[0]
        info = mt5lock.call_quick(mt5.symbol_info, pos.symbol, timeout=0.5)
        tick = mt5lock.call_quick(mt5.symbol_info_tick, pos.symbol, timeout=0.5)
        if tick is None:
            return {"ok": False, "busy": True, "reason": "拿不到报价，这一格改单作废"}

        digits = int(getattr(info, "digits", 3) or 3) if info is not None else 3
        price = round(float(sl), digits)
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        # 方向自检：多单 SL 必须在现价（bid）下方、空单在上方（ask），
        # 否则券商返回 10016「无效止损」。与其发一个必被拒的请求，
        # 不如在本地说清楚——调用方据此判断"价格已经回头了，这条线现在挂不上去"。
        if is_buy and price >= tick.bid:
            return {"ok": False, "reason": "止损价不低于现价（价格已回到开仓价下方）"}
        if not is_buy and price <= tick.ask:
            return {"ok": False, "reason": "止损价不高于现价（价格已回到开仓价上方）"}

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": price,
            # 必须原样带回去：SLTP 请求里 tp=0 的含义是「没有止盈」，
            # 漏传会把持仓上已有的止盈清掉。
            "tp": float(getattr(pos, "tp", 0.0) or 0.0),
            "magic": self.cfg.magic,
        }
        if self.cfg.dry_run:
            self._record("dry_run_modify_sl", request)
            return {"ok": True, "dry_run": True, "request": request, "price": price}

        # 不走 _send：它会给请求塞 type_filling 并轮换成交方式，而 SLTP
        # 请求没有"成交方式"这回事，多余字段在部分券商上会被整单拒掉。
        # 也**不用 call()**：连接期它会在锁上排到 `initialize` 结束（最长 30 秒）。
        # 拿不到锁就当这一格没做，下一格（2~5 秒后）再来。
        # 注意参数必须按位置传：带 kwargs 调 order_send 会得到 None + error -2。
        result = mt5lock.call_quick(mt5.order_send, request, timeout=1.0)
        if result is None:
            return {"ok": False, "busy": True, "reason": "终端忙，这一格改单作废"}
        rc = getattr(result, "retcode", None)
        ok = rc == mt5.TRADE_RETCODE_DONE
        self._record("sl_modify_sent" if ok else "sl_modify_failed",
                     {"ticket": ticket, "sl": price, "retcode": rc,
                      "comment": getattr(result, "comment", None)})
        return {"ok": ok, "retcode": rc,
                "comment": getattr(result, "comment", None), "price": price}

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
