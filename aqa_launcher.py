"""安装版入口。

顺序很讲究：**先修 stdio，再 import 任何业务模块**。

打包成无控制台的 exe（`console=False`）后，`sys.stdout` / `sys.stderr`
都是 `None`。而这套代码里 print 极多，任何一句都会
`AttributeError: 'NoneType' object has no attribute 'write'`——
而且是在 import 阶段就炸，用户看到的现象只是「双击没反应」。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _base_dir() -> Path:
    """可写根。冻结后是 exe 同级；源码态是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _install_stdio_shim() -> None:
    """把 stdout/stderr 接到 data/logs/app.log。

    `console=False` 的前置条件，不是可选项。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = _base_dir() / "data" / "logs" / "app.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        # 超过 5MB 轮一份，别让日志无限长
        if log.is_file() and log.stat().st_size > 5 * 1024 * 1024:
            bak = log.with_name("app.log.1")
            try:
                if bak.is_file():
                    bak.unlink()
                log.replace(bak)
            except OSError:
                pass
        fh = open(log, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:  # noqa: BLE001
        return
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _single_instance_mutex():
    """给安装包的 AppMutex 用：程序在跑时，安装/卸载能提示先关掉。

    返回值必须留住（不能让句柄被回收），所以由调用方持有。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        return ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\AIQuantDashboard")
    except Exception:  # noqa: BLE001
        return None


def _fatal_log_path() -> Path:
    return _base_dir() / "data" / "logs" / "fatal.log"


def _log_fatal() -> Path | None:
    try:
        p = _fatal_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        return p
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    _install_stdio_shim()

    # 控制台编码兜底。Windows 控制台默认 GBK，遇到不在 GBK 里的字符
    # （⚠ 之类的符号）print 会抛 UnicodeEncodeError，直接把整个进程带走。
    # 打包后实测踩过：源码态因为不走那句 print，完全看不出来。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    _MUTEX = _single_instance_mutex()   # noqa: F841 —— 故意持有引用，别被回收

    from live import appdirs

    # 放到 Program Files 这类只读目录时，这里就当场报错，
    # 而不是等用户点「开始做单」才 500（那时候根本看不出是权限问题）。
    appdirs.ensure_writable_dirs()

    from tools.dashboard import main as dashboard_main

    return dashboard_main() or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        path = _log_fatal()
        msg = f"启动失败，详见 {path}"
        try:
            print(f"[FATAL] {msg}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        # 无控制台时用户看不到任何东西，必须弹出来
        if getattr(sys, "frozen", False):
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"{msg}\n\n{_short_tb()}",
                    "AI Quant Agent",
                    0x10 | 0x4000,      # MB_ICONERROR | MB_TOPMOST
                )
            except Exception:  # noqa: BLE001
                pass
        if os.environ.get("AQA_PAUSE_ON_FATAL") == "1" and sys.stdin is not None:
            input("按回车退出…")
        raise


def _short_tb() -> str:
    """给弹窗用的短 traceback：最后两行通常就够定位了。"""
    lines = traceback.format_exc().strip().splitlines()
    return "\n".join(lines[-2:])
