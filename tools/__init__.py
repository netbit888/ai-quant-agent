"""工具脚本包。

这个文件是**为打包而加**的，不是重构。

`tools/` 原来是 PEP 420 命名空间包（没有 `__init__.py`），`python -m tools.dashboard`
靠隐式命名空间包才能跑。PyInstaller 对隐式命名空间包的支持不稳，尤其是
`tools.dashboard` 既要进 PYZ、又要作为 `-m` 的入口模块时，容易报
`ModuleNotFoundError: tools`。加一个空 `__init__.py` 让它变成普通包，
所有现有用法（`from tools import lesson_stats`、`-m tools.xxx`）都不受影响。
"""
