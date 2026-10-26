"""绿色版入口。

只做三件事：建可写目录 → 起面板 → 崩溃时留下日志。

为什么不直接把 `tools/dashboard.py` 当 PyInstaller 的入口脚本：
  1. 冻结后入口模块叫 `__main__`，而 `tools.dashboard` 会以另一个模块名被再导入
     一次，两边状态不一致；
  2. 万一是 `console=False` 或者用户根本没看黑窗口，崩了也需要一个能翻的痕迹。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _fatal_log_path() -> Path:
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
    return base / "data" / "logs" / "fatal.log"


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
    # 控制台编码兜底。Windows 控制台默认 GBK，遇到不在 GBK 里的字符
    # （⚠ 之类的符号）print 会抛 UnicodeEncodeError，直接把整个进程带走。
    # 打包后实测踩过：源码态不打印那句，所以完全看不出来。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    from live import appdirs

    # 放到 Program Files 这类只读目录时，这里就当场报错，
    # 而不是等用户点「开始做单」才 500（那时候根本看不出是权限问题）。
    appdirs.ensure_writable_dirs()

    from tools.dashboard import main as dashboard_main

    dashboard_main()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        path = _log_fatal()
        print(f"[FATAL] 启动失败，详见 {path}", file=sys.stderr)
        if os.environ.get("AQA_PAUSE_ON_FATAL") == "1":
            input("按回车退出…")
        raise
