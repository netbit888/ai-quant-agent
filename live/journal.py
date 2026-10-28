"""实盘账本：从 MT5 账户读成交，重建为逐笔交易，并持久化以便累积历史。

设计要点
--------
1. **只读**。本模块只调用 `history_deals_get` / `positions_get` / `account_info`，
   不具备任何下单能力。下单在 `execution/live.py`，两者刻意分开。
2. **可累积**。MT5 终端的成交历史有保留期，重装终端或换服务器都会丢。
   本模块把读到的每一条 deal 按 ticket 去重后写入
   `data/live/accounts/<login>/deals.jsonl`，之后所有统计都基于本地账本，
   不再依赖终端历史。
3. **按账户隔离**。每条 deal 都带上 `login` 字段，写进自己账户的目录。
   切换账户时不会把两个账户的成交混进同一个账本——混了之后所有统计
   （净额、回撤、入金）都是错的，而且事后无法拆分。
4. **逐笔重建**。MT5 的 deal 是「一次成交」，一个仓位可能拆成多笔
   （分批平仓、部分成交）。按 `position_id` 聚合，手数加权算均价。

关于「为什么不提供程序切换账户」
------------------------------
MT5 的 Python 接口（`mt5.login`）需要账号**密码**才能切换登录。把密码存明文
是严重安全风险，不该由这个工具承担。所以设计成：**你在 MT5 终端里切账户，
这里自动识别（`account_snapshot()["login"]`）并归档到对应目录。**
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from live import paths

ROOT = Path(__file__).resolve().parent.parent

# MT5 常量（避免在无终端环境下 import 失败）
ENTRY_IN = 0
ENTRY_OUT = 1
ENTRY_INOUT = 2
ENTRY_OUT_BY = 3
TYPE_BUY = 0
TYPE_SELL = 1
TYPE_BALANCE = 2

_lock = threading.Lock()
_connected = False

# MT5 接口的串行化搬到 `live/mt5lock.py` 了。
#
# 为什么不留在这里：`execution/live.py`（下单）和 `mt5/bridge.py`（回测取数）
# 都要用同一把锁，而它们本来不该依赖**账本**模块——放 journal 里等于让
# "锁"这个底座能力长在业务模块上，回测侧为了拿锁得反向 import 实盘账本。
#
# 这里保留同名别名，是为了不动既有调用点（含 tests/test_mt5_lock.py 的
# `journal.mt5_lock()`）。新代码请直接 `from live import mt5lock`。
from live.mt5lock import (  # noqa: E402,F401  （放在 _lock 之后，避免循环导入时半初始化）
    MT5Busy,
    busy_stats as mt5_busy_stats,
    call as mt5_call,
    call_quick as mt5_call_quick,
    connecting as mt5_connecting,
    is_connecting as mt5_is_connecting,
    lock as mt5_lock,
)

# MT5 不在时 mt5.initialize() 会**阻塞整个进程 60 秒**（MT5 内部最小超时就是 60s，
# timeout 参数调小没用）。面板是 HTTP 服务，被冻住 60 秒等于整个页面假死。
# 所以失败后要记住时间，这段时间内直接短路，不再反复去撞那 60 秒。
_conn_failed_at: float = 0.0
CONN_BACKOFF = 60.0

set_login = paths.set_login          # 由 collect() 从 MT5 读出后写入
current_login = paths.current_login
_login_of = paths.resolve_login


def _p(login: int | str | None, name: str) -> Path:
    return paths.account_file(_login_of(login), name)


def deals_path(login: int | str | None = None) -> Path:
    return _p(login, "deals.jsonl")


def daily_path(login: int | str | None = None) -> Path:
    return _p(login, "daily.json")


def snapshot_path(login: int | str | None = None) -> Path:
    return _p(login, "account.json")


def account_daily_path(login: int | str | None = None) -> Path:
    return _p(login, "account_daily.json")


def quality_path(login: int | str | None = None) -> Path:
    return _p(login, "quality_daily.json")


# ------------------------------------------------------------------ 连接


def _terminal_alive() -> bool:
    """MT5 终端进程在不在。

    不在时**绝不能**去调 `mt5.initialize()` —— 那会阻塞整个进程 60 秒，
    HTTP 面板会跟着假死。查一下进程列表只要几十毫秒。
    判断不出来（比如 tasklist 不可用）就返回 True，交给 initialize 自己决定，别误杀。
    """
    try:
        import subprocess

        # 按**字节**匹配，不做解码：中文 Windows 上 tasklist 输出是 GBK，
        # 用 text=True 会在 reader 线程里抛 UnicodeDecodeError，把 stdout 吞掉，
        # 结果就是「终端明明开着一律被判成没开」。
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/NH"],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return b"terminal64.exe" in (out.stdout or b"").lower()
    except Exception:  # noqa: BLE001
        return True


def terminal_path() -> Path | None:
    """找 MetaTrader 5 的 terminal64.exe。找不到返回 None。"""
    candidates = [
        Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
        Path(r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MetaTrader 5" / "terminal64.exe",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _start_terminal() -> bool:
    """拉起终端进程（不等待）。返回 Popen 是否成功。

    注意：这个调用的成败**取决于本进程从哪来**。从资源管理器/双击 bat 启动的
    进程树可以正常拉起 MT5；从 agent 的进程树里拉会 0xC0000139（加载器拒绝）。
    所以自动启动只在面板自己跑起来的情况下才有效——这正是它该待的地方。
    """
    exe = terminal_path()
    if exe is None:
        return False
    try:
        import subprocess

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_terminal(timeout: float = 30.0) -> bool:
    """确保终端在跑：不在就拉起来，然后等进程出现。返回最终是否在跑。

    只该由**后台线程**调用 —— 它会阻塞最多 `timeout` 秒等进程起来。
    HTTP 请求路径不允许调它，否则页面会卡住。
    """
    if _terminal_alive():
        return True
    if _last_launch_ok is False and time.time() - _launch_failed_at < LAUNCH_BACKOFF:
        return False
    if not _start_terminal():
        _mark_launch_failed()
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _terminal_alive():
            time.sleep(2.0)      # 进程刚起时管道还没就绪，等一下再 initialize
            return True
    _mark_launch_failed()
    return False


_launch_failed_at: float = 0.0
_last_launch_ok: bool | None = None
LAUNCH_BACKOFF = 600.0


def _mark_launch_failed() -> None:
    global _launch_failed_at, _last_launch_ok
    _launch_failed_at = time.time()
    _last_launch_ok = False


def _mark_launch_ok() -> None:
    global _launch_failed_at, _last_launch_ok, _conn_failed_at
    _launch_failed_at = 0.0
    _last_launch_ok = True
    # 终端刚被拉起来，之前那次 initialize 失败不该再挡住重连
    _conn_failed_at = 0.0


def _ensure(allow_launch: bool = False) -> None:
    """常驻连接。走 `connecting()` 闸门，不是裸锁。

    以前这里是裸的双检（`if _connected: return`）：两条线程同时看到 False
    就会各调一次 `mt5.initialize()`，引用计数涨到 2、连接状态却只有一份，
    后面查不清是"连上了"还是"连了两次"。闸门内重做双检就没有这个窗口。

    **为什么用闸门而不是普通锁**：这段里有 `ensure_terminal()`（拉起终端，
    可能几十秒）和 `mt5.initialize()`（内部超时 30 秒）。如果只是持锁，
    别的线程会排队等到天荒地老——包括 2 秒一格的止损快查。闸门在持锁后
    set，期间新调用直接 `MT5Busy` 快速失败，业务层按"这一轮没行情"跳过。
    """
    if _connected:
        return
    with mt5_connecting():
        _ensure_locked(allow_launch)


def _ensure_locked(allow_launch: bool = False) -> None:
    """常驻连接的实际实现（**调用方必须已持有 MT5 锁 / 在闸门内**）。

    三道闸：
    1. 终端进程不在 → 默认**立刻抛错**，不等那 60 秒（让 HTTP 请求快速失败）。
    2. `allow_launch=True`（只由后台线程传）→ 先尝试拉起终端再连。
    3. initialize 真失败 → 退避 `CONN_BACKOFF` 秒，避免反复去等 60 秒。
    """
    global _connected, _conn_failed_at
    if _connected:
        return

    if not _terminal_alive():
        if not allow_launch:
            raise RuntimeError(
                "MT5 终端未运行。正在等待它启动 —— 打开 MetaTrader 5 后 20 秒内会自动连上，"
                "面板不用重启。"
            )
        # 后台线程才会走到这里：尝试自己拉起终端
        if not ensure_terminal():
            raise RuntimeError(
                "MT5 终端未运行，且本进程无法启动它。"
                "请手动双击 MetaTrader 5；或者用 start-dashboard.bat 启动面板"
                "（从资源管理器启动的进程树才能拉起 MT5）。"
            )
        _mark_launch_ok()

    now = time.time()
    if _conn_failed_at and now - _conn_failed_at < CONN_BACKOFF:
        raise RuntimeError("MT5 未连接（刚失败过，稍后自动重试）")

    import MetaTrader5 as mt5

    if not mt5.initialize(timeout=30_000):
        _conn_failed_at = time.time()
        raise RuntimeError(f"MT5 连接失败: {mt5.last_error()}")
    _connected = True
    _conn_failed_at = 0.0


def _mt5(allow_launch: bool = False):
    _ensure(allow_launch)
    import MetaTrader5 as mt5

    return mt5


def disconnect() -> None:
    """断开常驻连接。

    走闸门：`shutdown()` 本身很快，但它必须独占——正在 initialize 的时候
    插一脚会把引用计数搞乱。
    """
    global _connected, _conn_failed_at
    with mt5_connecting():
        if _connected:
            import MetaTrader5 as mt5

            mt5.shutdown()
            _connected = False
        _conn_failed_at = 0.0


# ------------------------------------------------------------------ 账户


def is_demo_mode(trade_mode) -> bool:
    """MT5 `trade_mode` → 这个账户是不是虚拟资金。

    0 = DEMO，1 = CONTEST（比赛盘，也是虚拟），2 = REAL。
    只有 REAL 才是真钱。

    **未知取值一律返回 False（=按真实处理）**：把真钱当假钱会让人放松警惕，
    比反过来危险得多。
    """
    try:
        return int(trade_mode) in (0, 1)
    except (TypeError, ValueError):
        return False


def account_snapshot(allow_launch: bool = False) -> dict:
    """当前账户状态快照。"""
    mt5 = _mt5(allow_launch)
    with mt5_lock():
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError(f"读不到账户信息: {mt5.last_error()}")
        ti = mt5.terminal_info()
    return {
        "login": ai.login,
        "server": ai.server,
        "name": ai.name,
        "currency": ai.currency,
        "balance": round(ai.balance, 2),
        "equity": round(ai.equity, 2),
        "margin": round(ai.margin, 2),
        "margin_free": round(ai.margin_free, 2),
        "margin_level": round(ai.margin_level, 2) if ai.margin_level else 0.0,
        "profit": round(ai.profit, 2),
        "leverage": ai.leverage,
        # 原来写 `ai.trade_mode != 0`，方向是反的：DEMO(0) 被算成 False→"不是模拟"，
        # 真实账户(2) 反而被算成 True→"是模拟"。实测本账户 trade_mode=0、
        # server=Exness-MT5Trial5（模拟盘），却被记成 is_demo=false。
        "is_demo": is_demo_mode(ai.trade_mode),
        "trade_mode": {0: "demo", 1: "contest", 2: "real"}.get(ai.trade_mode, str(ai.trade_mode)),
        "terminal_connected": bool(ti and ti.connected),
        "ts": int(time.time()),
    }


def probe_login() -> int | None:
    """只问 MT5 现在登录的是哪个账户，不读别的东西。连不上返回 None。"""
    try:
        return int(account_snapshot()["login"])
    except Exception:  # noqa: BLE001
        return None


def open_positions(allow_launch: bool = False) -> list[dict]:
    """账户当前持仓。

    **读失败抛异常，不返回空列表。** `positions_get()` 返回 None 表示查询
    失败，以前这里 `or []` 把它翻成"没有持仓"——而上层 `_positions()` 特意
    用 None 表示"不知道"、就是为了防止把读失败当成"刚才那笔已经平了"再反手
    开一仓。`or []` 让那层保护整个失效：终端抖一下就能变成连续开仓。
    空仓时 MT5 返回的是空 tuple，走不到这个分支。
    """
    mt5 = _mt5(allow_launch)
    rows = mt5_call(mt5.positions_get)
    if rows is None:
        raise RuntimeError(f"读不到持仓: {mt5.last_error()}")
    out = []
    for p in rows:
        out.append(
            {
                "ticket": p.ticket,
                "position_id": p.identifier,
                "symbol": p.symbol,
                "direction": 1 if p.type == TYPE_BUY else -1,
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit, 2),
                "swap": round(p.swap, 2),
                "open_time": p.time,
                "comment": p.comment,
            }
        )
    return sorted(out, key=lambda r: r["open_time"], reverse=True)


# ------------------------------------------------------------------ 账本


@dataclass
class Trade:
    position_id: int
    symbol: str
    direction: int  # 1 多 / -1 空
    volume: float
    entry_time: int
    entry_price: float
    exit_time: int | None = None
    exit_price: float | None = None
    profit: float = 0.0  # 含手续费与库存费的净额
    gross: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    comment: str = ""
    close_reason: str = "open"
    # 已平掉的手数。部分平仓时 volume 仍是开仓总量、profit 只含已平部分，
    # 用这个字段区分「这一笔实际结算了多少」，成本也按它算，否则会重复计点差。
    volume_closed: float = 0.0
    partial: bool = False  # True = 还有剩余手数没平（这条记录只是已实现部分）

    @property
    def is_closed(self) -> bool:
        return self.exit_time is not None

    @property
    def holding_min(self) -> float | None:
        if self.exit_time is None:
            return None
        return (self.exit_time - self.entry_time) / 60.0


@dataclass
class SyncResult:
    deals_total: int = 0
    deals_new: int = 0
    trades: int = 0
    deposits: float = 0.0  # 净入金 = 入金 - 出金
    gross_in: float = 0.0
    gross_out: float = 0.0
    window_days: int = 0
    login: str | None = None
    error: str | None = None


def _deal_to_dict(d, login: int | str | None = None) -> dict:
    return {
        "ticket": int(d.ticket),
        "order": int(d.order),
        "position_id": int(d.position_id),
        "symbol": d.symbol,
        "type": int(d.type),
        "entry": int(d.entry),
        "volume": float(d.volume),
        "price": float(d.price),
        "profit": float(d.profit),
        "commission": float(getattr(d, "commission", 0.0) or 0.0),
        "swap": float(getattr(d, "swap", 0.0) or 0.0),
        "fee": float(getattr(d, "fee", 0.0) or 0.0),
        "time": int(d.time),
        "comment": d.comment or "",
        "magic": int(getattr(d, "magic", 0) or 0),
        # 关键：记录归属账户。没有它，切账户时两条账本会混在一起且无法拆分。
        "login": int(login) if str(login).isdigit() else login,
    }


def load_ledger(login: int | str | None = None) -> dict[int, dict]:
    """ticket -> deal。只返回属于该账户的记录。"""
    path = deals_path(login)
    want = _login_of(login)
    if not path.exists():
        return {}
    out: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        owner = d.get("login")
        # 老记录没有 login 字段：既然它躺在这个账户的目录里，就认它是这个账户的
        if owner is not None and str(owner) != str(want):
            continue
        out[int(d["ticket"])] = d
    return out


def _append_ledger(new_deals: list[dict], login: int | str | None = None) -> None:
    if not new_deals:
        return
    path = deals_path(login)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for d in new_deals:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")


def sync(days: int = 365, login: int | str | None = None, allow_launch: bool = False) -> SyncResult:
    """拉最近 days 天的成交，增量并入**当前账户**的本地账本。

    调用前必须先拿到 login（通常由 collect() 从 MT5 读出后传入）。
    """
    from datetime import timedelta

    mt5 = _mt5(allow_launch)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    raw = mt5_call(mt5.history_deals_get, start, now)
    if raw is None:
        return SyncResult(login=_login_of(login), error=f"history_deals_get 失败: {mt5.last_error()}")

    fetched = [_deal_to_dict(d, login=_login_of(login)) for d in raw]
    ledger = load_ledger(login)
    fresh = [d for d in fetched if d["ticket"] not in ledger]
    _append_ledger(fresh, login)

    merged = {**ledger, **{d["ticket"]: d for d in fresh}}
    trades = build_trades(list(merged.values()))

    # 资金流水：type=2 是入金/出金/利息调整。按符号拆分，
    # 净入金才是计算收益率的正确分母（出金不算亏损）。
    gross_in = sum(d["profit"] for d in merged.values() if d["type"] == TYPE_BALANCE and d["profit"] > 0)
    gross_out = -sum(d["profit"] for d in merged.values() if d["type"] == TYPE_BALANCE and d["profit"] < 0)
    net_capital = gross_in - gross_out

    _write_daily(trades, net_capital, login)
    return SyncResult(
        deals_total=len(merged),
        deals_new=len(fresh),
        trades=len(trades),
        deposits=round(net_capital, 2),
        gross_in=round(gross_in, 2),
        gross_out=round(gross_out, 2),
        window_days=days,
        login=_login_of(login),
    )


def build_trades(deals: list[dict]) -> list[Trade]:
    """按 position_id 聚合 deal，重建逐笔交易。"""
    groups: dict[int, list[dict]] = {}
    for d in deals:
        if d["type"] == TYPE_BALANCE or d["position_id"] == 0:
            continue
        groups.setdefault(d["position_id"], []).append(d)

    trades: list[Trade] = []
    for pid, items in groups.items():
        items.sort(key=lambda x: x["time"])
        ins = [x for x in items if x["entry"] in (ENTRY_IN, ENTRY_INOUT)]
        if not ins:
            continue
        first = ins[0]
        vol = sum(x["volume"] for x in ins) or 1e-9
        entry_price = sum(x["price"] * x["volume"] for x in ins) / vol
        direction = 1 if first["type"] == TYPE_BUY else -1

        outs = [x for x in items if x["entry"] in (ENTRY_OUT, ENTRY_OUT_BY)]
        t = Trade(
            position_id=pid,
            symbol=first["symbol"],
            direction=direction,
            volume=round(vol, 4),
            entry_time=first["time"],
            entry_price=round(entry_price, 6),
            gross=round(sum(x["profit"] for x in outs), 2),
            commission=round(sum(x["commission"] for x in items), 2),
            swap=round(sum(x["swap"] for x in items), 2),
        )
        t.profit = round(t.gross + t.commission + t.swap, 2)

        if outs:
            out_vol = sum(x["volume"] for x in outs) or 1e-9
            t.volume_closed = round(out_vol, 4)
            # 只平了一部分：这条记录代表「已实现的那部分」，
            # 剩余手数还在持仓里，不能当成整笔已平仓来重复计成本。
            t.partial = out_vol + 1e-6 < vol
            t.exit_time = max(x["time"] for x in outs)
            t.exit_price = round(sum(x["price"] * x["volume"] for x in outs) / out_vol, 6)
            t.comment = outs[-1]["comment"]
            t.close_reason = _reason(t.comment)
        elif any(x["entry"] == ENTRY_OUT_BY for x in items):
            t.close_reason = "close_by"
        trades.append(t)

    trades.sort(key=lambda x: x.entry_time)
    return trades


def _reason(comment: str) -> str:
    c = (comment or "").strip().lower()
    if c.startswith("[tp") or "tp " in c[:6]:
        return "tp"
    if c.startswith("[sl") or "sl " in c[:6]:
        return "sl"
    # MT5 的强平注释是 "so: <价格>"（带冒号），只匹配 "so " 会漏掉，
    # 穿仓就被记成手动平仓 —— 纪律维度和穿仓红旗全靠这个字段。
    if c.startswith("[so") or c.startswith("so:") or "so " in c[:6] or "stop out" in c:
        return "stop_out"
    if "close by" in c:
        return "close_by"
    if not c:
        return "manual"
    return "manual"


def _write_daily(trades: list[Trade], deposits: float, login: int | str | None = None) -> None:
    """按日聚合，写一份可累积的日快照。"""
    by_day: dict[str, dict] = {}
    for t in trades:
        if not t.is_closed:
            continue
        day = datetime.fromtimestamp(t.exit_time, timezone.utc).strftime("%Y-%m-%d")
        row = by_day.setdefault(day, {"date": day, "trades": 0, "wins": 0, "profit": 0.0, "volume": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if t.profit > 0 else 0
        row["profit"] = round(row["profit"] + t.profit, 2)
        row["volume"] = round(row["volume"] + (t.volume_closed or t.volume), 4)

    payload = {
        "updated_at": int(time.time()),
        "login": _login_of(login),
        "deposits": deposits,
        "days": [by_day[k] for k in sorted(by_day)],
    }
    paths.write_json(_login_of(login), "daily.json", payload)


def record_quality(quality: dict, login: int | str | None = None) -> list[dict]:
    """每天存一行滚动窗口指标。

    滚动窗口的历史值不可重现（「上周的近 10 笔」早被新交易顶掉了），
    不存快照就只能看到「此刻回看」，永远画不出趋势。
    """
    from live.quality import merge_snapshot, snapshot_row

    key = _login_of(login)
    existing = paths.read_json(key, "quality_daily.json", [])
    if not isinstance(existing, list):
        existing = []
    rows = merge_snapshot(existing, snapshot_row(quality))
    paths.write_json(key, "quality_daily.json", rows)
    return rows


def snapshot_account(acct: dict | None = None, login: int | str | None = None) -> None:
    acct = acct or account_snapshot()
    paths.write_json(_login_of(login or acct.get("login")), "account.json", acct)


def record_account_daily(acct: dict | None = None, login: int | str | None = None) -> list[dict]:
    """每个自然日留一条账户快照，用于画余额曲线。

    同一天多次调用只覆盖当天那条，不会把文件撑大。
    """
    acct = acct or account_snapshot()
    key = _login_of(login or acct.get("login"))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows: dict[str, dict] = {}
    for r in (paths.read_json(key, "account_daily.json", []) or []):
        try:
            rows[r["date"]] = r
        except (KeyError, TypeError):
            continue
    rows[day] = {
        "date": day,
        "balance": acct.get("balance"),
        "equity": acct.get("equity"),
        "margin_free": acct.get("margin_free"),
        "ts": acct.get("ts") or int(time.time()),
    }
    ordered = [rows[k] for k in sorted(rows)]
    paths.write_json(key, "account_daily.json", ordered)
    return ordered


# ------------------------------------------------------------------ 离线读取


def _balance_from_ledger(ledger: dict[int, dict]) -> float:
    """从成交记录反推账户余额。

    MT5 里 余额 = 累计入金 + 累计（已实现盈亏 + 手续费 + 库存费）。
    平掉的历史账户拿不到 account.json 时，这是唯一能复原的口径。
    """
    total = 0.0
    for d in ledger.values():
        total += float(d.get("profit") or 0.0)
        total += float(d.get("commission") or 0.0)
        total += float(d.get("swap") or 0.0)
    return round(max(total, 0.0), 2)


def collect_offline(login: int | str) -> dict:
    """纯读磁盘，不同步 MT5。

    用于回看**非当前登录**的历史账户——那些账户连不上了，只能看攒下来的数据。
    """
    key = str(login)
    acct = dict(paths.read_json(key, "account.json", {}) or {})
    ledger = load_ledger(key)

    # 老账户的 account.json 可能早已被后来的账户覆盖（本工具早期就是这么丢的）。
    # 这时用索引里的元信息 + 从账本反推的余额兜底，至少让统计能跑起来。
    meta = paths.load_index().get(key) or {}
    acct.setdefault("login", meta.get("login") or (int(key) if key.isdigit() else key))
    for k in ("server", "name", "currency", "trade_mode", "leverage"):
        acct.setdefault(k, meta.get(k))
    acct.setdefault("server", "unknown")
    acct.setdefault("currency", "USD")
    acct.setdefault("trade_mode", "unknown")
    acct.setdefault("balance", _balance_from_ledger(ledger))
    acct.setdefault("equity", acct["balance"])
    acct.setdefault("margin_free", acct["balance"])
    acct.setdefault("profit", 0.0)
    acct.setdefault("leverage", meta.get("leverage") or 0)
    acct.setdefault("ts", int(time.time()))
    trades = [asdict(t) for t in build_trades(list(ledger.values()))]
    flows = sorted(
        (
            {"time": d["time"], "amount": d["profit"], "comment": d["comment"]}
            for d in ledger.values()
            if d["type"] == TYPE_BALANCE
        ),
        key=lambda x: x["time"],
    )
    gross_in = sum(d["profit"] for d in ledger.values() if d["type"] == TYPE_BALANCE and d["profit"] > 0)
    gross_out = -sum(d["profit"] for d in ledger.values() if d["type"] == TYPE_BALANCE and d["profit"] < 0)

    # 历史账户的「现在」应该是它最后一笔交易的时间，否则近 7 天窗口永远是空的
    exits = [t["exit_time"] for t in trades if t.get("exit_time")]
    as_of = max(exits) if exits else int(time.time())

    try:
        from live.quality import compute as quality_compute

        quality = quality_compute(trades, now=as_of)
    except Exception as exc:  # noqa: BLE001
        quality = {"empty": True, "error": f"{type(exc).__name__}: {exc}", "windows": [], "rolling": []}

    try:
        from live.positions import initial_plans
        from live.scoring import compute as scoring_compute
        from live.stats import compute as stats_compute

        plans = initial_plans(key)
        _stats = stats_compute(trades, acct, round(gross_in - gross_out, 2), flows)
        scoring = scoring_compute(_stats, quality, trades, plans=plans)
    except Exception as exc:  # noqa: BLE001
        plans = {}
        scoring = {"grade": "insufficient", "error": f"{type(exc).__name__}: {exc}",
                   "dimensions": [], "red_flags": [], "trades": []}

    daily = paths.read_json(key, "daily.json", {}) or {}
    try:
        from live.sessions import build as build_sessions

        sessions = build_sessions(trades, flows=flows, deposits=round(gross_in - gross_out, 2),
                                  plans=plans)
    except Exception:  # noqa: BLE001
        sessions = []
    return {
        "ok": bool(ledger),
        "offline": True,
        "error": None if ledger else "该账户没有已归档的成交记录",
        "account": {**acct, "login": acct.get("login") or (int(key) if key.isdigit() else key)},
        "positions": [],
        "trades": trades,
        "flows": flows,
        "quality": quality,
        "scoring": scoring,
        "plans": plans,
        "sessions": sessions,
        "sync": asdict(SyncResult(
            deals_total=len(ledger), deals_new=0, trades=len(trades),
            deposits=round(gross_in - gross_out, 2),
            gross_in=round(gross_in, 2), gross_out=round(gross_out, 2),
            login=key,
        )),
        "deposits": round(gross_in - gross_out, 2),
        "account_daily": paths.read_json(key, "account_daily.json", []) or [],
        "daily": daily.get("days") or [],
        "updated_at": int(time.time()),
    }


# ------------------------------------------------------------------ 对外入口

_cache: dict = {"ts": 0.0, "data": None, "login": None}
CACHE_TTL = 3.0
# 面板要"实时"，这一层就不能压 15 秒：前端 3 秒刷一次，缓存要是 15 秒，
# 界面上看到的还是旧数字。实测一次 force 查询约 33ms，3 秒一次完全撑得住。


def collect(days: int = 365, force: bool = False, allow_launch: bool = False) -> dict:
    """一次完整的实盘数据读取：账户 + 持仓 + 逐笔交易。带 15 秒缓存。"""
    with _lock:
        now = time.time()
        if not force and _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"]

        result: dict = {"ok": False, "error": None}
        try:
            acct = account_snapshot(allow_launch)
            login = str(acct["login"])
            set_login(login)
            paths.touch_account(acct)
            pos = open_positions(allow_launch)
            history = record_account_daily(acct, login)
            try:
                from live.positions import record_positions

                record_positions(pos, now=int(now), login=login)
            except Exception:  # noqa: BLE001
                pass  # 持仓快照失败不影响主流程
            res = sync(days, login, allow_launch)
            ledger = load_ledger(login)
            trades = build_trades(list(ledger.values()))
            flows = sorted(
                (
                    {"time": d["time"], "amount": d["profit"], "comment": d["comment"]}
                    for d in ledger.values()
                    if d["type"] == TYPE_BALANCE
                ),
                key=lambda x: x["time"],
            )

            trade_dicts = [asdict(t) for t in trades]
            try:
                from live.quality import compute as quality_compute

                quality = quality_compute(trade_dicts, now=int(now))
                record_quality(quality, login)
            except Exception as exc:  # noqa: BLE001
                quality = {"empty": True, "error": f"{type(exc).__name__}: {exc}", "windows": [], "rolling": []}

            # 评分依赖 stats + quality，放在两者之后
            try:
                from live.positions import initial_plans
                from live.scoring import compute as scoring_compute
                from live.stats import compute as stats_compute

                plans = initial_plans(login)
                _stats = stats_compute(trade_dicts, acct, res.deposits, flows)
                scoring = scoring_compute(_stats, quality, trade_dicts, plans=plans)
            except Exception as exc:  # noqa: BLE001
                plans = {}
                scoring = {"grade": "insufficient", "error": f"{type(exc).__name__}: {exc}",
                           "dimensions": [], "red_flags": [], "trades": []}

            # 场次（盘后复盘用）：按「上一笔平仓 → 下一笔开仓」间隔 > 30 分钟切场
            try:
                from live.sessions import build as build_sessions

                sessions = build_sessions(trade_dicts, flows=flows, deposits=res.deposits,
                                          plans=plans)
            except Exception as exc:  # noqa: BLE001
                sessions = []
                result_note = f"场次切分失败: {type(exc).__name__}: {exc}"
            else:
                result_note = None

            result = {
                "ok": True,
                "error": res.error,
                "login": login,
                "account": acct,
                "positions": pos,
                "trades": trade_dicts,
                "flows": flows,
                "quality": quality,
                "scoring": scoring,
                "plans": plans,
                "sync": asdict(res),
                "deposits": res.deposits,
                "account_daily": history,
                "sessions": sessions,
                "sessions_error": result_note,
                "updated_at": now,
            }
            snapshot_account(acct, login)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "updated_at": now}

        _cache.update({"ts": now, "data": result, "login": result.get("login")})
        return result


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    info = collect(force=True)
    if not info["ok"]:
        print("失败:", info["error"])
        raise SystemExit(1)
    a = info["account"]
    print(f"账户 {a['login']} @ {a['server']}（{a['trade_mode']}）")
    print(f"  余额 {a['balance']} / 净值 {a['equity']} / 可用 {a['margin_free']} {a['currency']}")
    print(f"  持仓 {len(info['positions'])} 个")
    print(f"  成交 deal {info['sync']['deals_total']} 条（新增 {info['sync']['deals_new']}），"
          f"重建出 {len(info['trades'])} 笔交易，入金合计 {info['deposits']}")
    print(f"  账本目录 {paths.account_dir(a['login'])}")
