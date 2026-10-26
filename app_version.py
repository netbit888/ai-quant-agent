"""版本号的**唯一来源**。

改版本只动这里的 ``APP_VERSION``，其他地方（Inno 安装脚本、release.bat、
面板 Server 头）全部从它派生：

    build_installer.bat / release.bat
        用 .venv 的 python ``import app_version`` 打印出来取用；
    installer/aqa.iss
        不再自带版本号，由 build_installer.bat 通过 ``/DMyAppVersion=`` 传入；
    tools/dashboard.py
        顶层 ``from app_version import APP_VERSION``。

为什么是 .py 而不是 VERSION 文本文件：冻结包（PyInstaller）能原样带走模块，
批处理脚本解析模块输出也不需要额外约定——两边都不用发明新的读取协议。
"""

from __future__ import annotations

import re

APP_VERSION = "1.2.1"

_NUM = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(s: str) -> tuple[int, ...]:
    """``'v1.2.3-beta2'`` -> ``(1, 2, 3)``。

    只取前导数字段做比较——预发布后缀（-beta2）不参与大小判断，发布流程
    里本来就不该出现；脏 tag / 空串返回 ``()``，调用方（is_newer）把解析
    失败当「不更新」处理，宁可不提示也不误报。
    """
    if not s:
        return ()
    m = _NUM.match(s.strip().lstrip("vV"))
    if not m:
        return ()
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return ()
