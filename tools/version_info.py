"""生成 PyInstaller 的 VERSIONINFO 资源文件。

**为什么需要它**：没有 VERSIONINFO 的 exe，任务管理器只能拿文件名当显示名；
有了 ``FileDescription``，任务管理器 / 任务栏跳转列表 / 通知 / 文件属性
显示的才是「SeatOne 实盘账本」，而不是 ``AIQuant`` 或 ``Python``。

版本号只有一个来源（``app_version.APP_VERSION``），这里不重复维护。

用法（aqa.spec 里自动调用）::

    from tools.version_info import write_version_file
    _VER = write_version_file(ROOT / "build" / "version_info.txt")

生成物是 PyInstaller 的 ``VSVersionInfo(...)`` 文本序列化，由它自己 eval。
"""

from __future__ import annotations

from pathlib import Path

from app_version import APP_VERSION, parse_version

# 进程/窗口的应用身份标识。**永不改**：改了任务栏固定项、通知归组、
# 单实例关联全部断开（表现为任务栏出现两个图标）。
APP_ID = "SeatOne.LiveDesk.1"

# 显示名。改这里要同时改 installer/aqa.iss 的快捷方式名与 launcher 的窗口标题。
APP_NAME = "SeatOne 实盘账本"
COMPANY = "SeatOne"
EXE_NAME = "AIQuant.exe"

# zh-CN(0x0804) + Unicode(1200)，与 StringTable 的键 '080404B0' 一一对应
_LANG, _CODEPAGE = 0x0804, 1200


def _four(ver: str) -> tuple[int, int, int, int]:
    """``'1.5.30'`` -> ``(1, 5, 30, 0)``；VERSIONINFO 固定四段。"""
    parts = parse_version(ver)
    if not parts:
        return (0, 0, 0, 0)
    nums = list(parts[:4]) + [0] * (4 - len(parts[:4]))
    return tuple(min(n, 65535) for n in nums)  # type: ignore[return-value]


def render(version: str = APP_VERSION) -> str:
    """生成版本资源文件内容（纯函数，方便测试）。"""
    fv = _four(version)
    # 文件里写死 UTF-8 cookie：PyInstaller 读文件时会认它
    return f"""# -*- coding: utf-8 -*-
# 自动生成，勿手改。源：app_version.APP_VERSION
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={fv},
    prodvers={fv},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '{_LANG:04x}{_CODEPAGE:04X}',
        [
          StringStruct('CompanyName', '{COMPANY}'),
          StringStruct('FileDescription', '{APP_NAME}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'AIQuant'),
          StringStruct('LegalCopyright', '{COMPANY}'),
          StringStruct('OriginalFilename', '{EXE_NAME}'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{version}'),
        ],
      ),
    ]),
    VarFileInfo([VarStruct('Translation', [{_LANG}, {_CODEPAGE}])]),
  ],
)
"""


def write_version_file(path: str | Path, version: str = APP_VERSION) -> Path:
    """写出版本资源文件，返回路径（aqa.spec 的 ``version=`` 参数要它）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render(version)
    # 内容没变就不动文件：避免每次打包都刷新 mtime，增量构建能少一次失效
    if not p.is_file() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8", newline="\n")
    return p
