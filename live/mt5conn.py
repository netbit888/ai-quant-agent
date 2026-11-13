"""MT5 连接的唯一持有者：终端发现 / 拉起 / initialize / 退避。

为什么从 `live/journal.py` 搬出来
--------------------------------
账本模块的自我定位是「只读账本」——只调 `history_deals_get` / `positions_get`
/ `account_info`。但它一直兼着**连接生命周期**：单例状态、终端进程探测、
自动拉起、initialize 退避。结果 `mt5/bridge.py`（回测取数）要连接时只能
`from live.journal import _mt5`——回测侧为了拿一条连接，反向依赖了实盘账本，
包级依赖图上就是 `mt5 → live` 的环，而且一个私有函数成了跨包公共 API。

这和 `live/mt5lock.py` 当初被拆出来的理由完全一样：底座能力不该长在业务模块上。

搬完之后：`journal` 只是这个模块的一个调用方（顶部用 `as` 重新导出，
`from live.journal import _mt5` 的老调用点全部继续可用）。

三条不能破坏的性质
------------------
1. **单例**。`_connected` 只有一份，谁都不能复制一个副本自己管。
2. **闸门不换锁**。走 `mt5lock.connecting()` 而不是裸锁：终端拉起可能
   几十秒、initialize 内部超时 30 秒，持裸锁会让 2 秒一格的止损快查排队到
   天荒地老；闸门让并发调用直接 `MT5Busy` 快速失败。
3. **测试可断链**。`tests/_stubs.py` 把 `ensure` 换成拒绝版是本进程不碰真实
   终端的唯一保险，所以连接必须走**模块属性** `ensure(...)`，不能
   `from . import ensure` 之后直接调用（那样打桩不生效）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from live.mt5lock import connecting as mt5_connecting

# ---------------------------------------------------------------- 连接状态
_connected = False

# MT5 不在时 mt5.initialize() 会**阻塞整个进程 60 秒**（MT5 内部最小超时就是 60s，
# timeout 参数调小没用）。面板是 HTTP 服务，被冻住 60 秒等于整个页面假死。
# 所以失败后要记住时间，这段时间内直接短路，不再反复去撞那 60 秒。
_conn_failed_at: float = 0.0
CONN_BACKOFF = 60.0

_launch_failed_at: float = 0.0
_last_launch_ok: bool | None = None
LAUNCH_BACKOFF = 600.0

# ---------------------------------------------------------------- 终端

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


def ensure(allow_launch: bool = False) -> None:
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
    ensure(allow_launch)
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
