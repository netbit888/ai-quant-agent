"""原生窗口宿主。

这一层只做「开一个窗口，把页面装进去」，不关心业务：
策略、路由、退出语义全部留在 `tools/dashboard.py`。这样 `--web` 模式和
窗口模式共享同一套逻辑，不会出现两份实现慢慢分叉。

后端是 Edge WebView2（系统自带，Win10/11 一般都有）。它不是浏览器：
没有地址栏、没有标签页、没有后退键，任务栏上是自己的图标。
但渲染内核与 Edge 同源，所以现有页面（3956 行 HTML）一行都不用改。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# WebView2 运行时的 EdgeUpdate client id，微软固定值，不要改
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
# 官方 Evergreen Bootstrapper 跳转链接
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# Win32 MessageBox 常量
MB_OK = 0x00
MB_YESNO = 0x04
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_TOPMOST = 0x4000
IDYES = 6


def message_box(text: str, title: str = "AI Quant Agent", style: int = MB_OK) -> bool:
    """Win32 消息框，返回 True 表示点了「是」。

    `console=False` 之后所有 print 都进虚空，这是**唯一**能让用户看见错误
    的通道。所以它必须永不抛异常——最差也要退化到 stderr。
    """
    try:
        import ctypes

        rc = ctypes.windll.user32.MessageBoxW(None, text, title, style | MB_TOPMOST)
        return rc == IDYES
    except Exception:  # noqa: BLE001
        try:
            print(f"[{title}] {text}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return False


def _reg_pv(root, path: str) -> str:
    import winreg

    try:
        with winreg.OpenKey(root, path) as k:
            v, _ = winreg.QueryValueEx(k, "pv")
            return str(v or "")
    except OSError:
        return ""


def webview2_version() -> str | None:
    """返回 WebView2 运行时版本；没装返回 None。

    注册表是微软推荐做法，但它有两类漏判，所以补了文件系统兜底：
      · 有些离线包只写 HKLM\\WOW6432Node；
      · 极少数精简系统连 EdgeUpdate 键都没有，但程序目录在。
    `pv == "0.0.0.0"` 表示「装了但运行时丢了」，按未安装处理。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    keys = (rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}",
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}")
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for k in keys:
            v = _reg_pv(root, k)
            if v and v != "0.0.0.0":
                return v

    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        try:
            for exe in Path(base).glob("Microsoft/EdgeWebView/Application/*/msedgewebview2.exe"):
                return exe.parent.name
        except OSError:
            continue
    return None


def prompt_missing_webview2(fallback) -> int:
    """没有 WebView2 时的处理。返回 fallback() 的返回值，或 1（用户去装）。

    「否」走浏览器模式是必须留的路：缺个组件就完全不能用，比少个窗口难接受得多。
    """
    yes = message_box(
        "缺少 Microsoft Edge WebView2 运行时，无法打开程序窗口。\n\n"
        "它是微软的免费组件，Win10 / Win11 绝大多数机器已自带，\n"
        "单独安装约 1~2 分钟。\n\n"
        "「是」= 打开官方下载页，装完再启动本程序\n"
        "「否」= 先用浏览器打开面板（功能完全一样，只是多一个浏览器窗口）",
        style=MB_YESNO | MB_ICONWARNING,
    )
    if yes:
        try:
            import webbrowser

            webbrowser.open(WEBVIEW2_URL)
        except Exception:  # noqa: BLE001
            pass
        return 1
    return fallback()


def run_window(*, url: str, title: str, js_api, on_window, on_closing,
               storage_path: Path | None, attach: bool = False) -> None:
    """创建窗口并进入消息循环。**必须从主线程调用，会阻塞到窗口关闭。**

    `attach=True` 是「已经有一份在跑」时的只读看板窗口：临时 profile、
    没有 js_api、关掉不影响别人。
    """
    import webview

    if storage_path is not None:
        storage_path.mkdir(parents=True, exist_ok=True)

    win = webview.create_window(
        title,
        url,
        width=1480,
        height=920,
        # 1120 是 .at-bar 折行的临界值（flex-wrap），再小程序顶部会变两行
        min_size=(1120, 720),
        js_api=js_api,
        text_select=True,
        confirm_close=False,     # 关窗确认由我们自己的 closing 事件做
    )
    on_window(win)
    win.events.closing += on_closing

    webview.start(
        gui="edgechromium",      # 显式指定，杜绝悄悄退回 IE(mshtml)
        debug=False,             # False = 没有 Inspect / 右键开发者工具
        # private_mode=True 会用临时目录并在退出时清空，localStorage 里的
        # 备注草稿会静默丢失（前端 catch 是空的，查都查不出来）。
        private_mode=bool(attach),
        storage_path=None if attach else (str(storage_path) if storage_path else None),
    )
