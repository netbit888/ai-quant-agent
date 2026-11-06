"""ruff 守门：应用代码里不许出现 pyflakes 级错误。

为什么值得一条测试
------------------
2026-11-06 ruff 第一次跑就抓到两个真 bug，都在不常走的路径上：
  · aqa.py `time` 未定义 —— `aqa.py live --watch` 一进循环就 NameError；
  · aqa_launcher.py 的 `_short_tb` 定义在 `if __name__` 之后 —— 启动失败时
    崩溃弹窗自己先抛 NameError，又被外层 `except: pass` 吞掉，用户看到的是
    "点了没反应"。
单测覆盖不到这两处（一个要手工挂循环，一个是冻结包的错误路径），但静态检查
一次就看见。这条测试的作用是**让它们不会以新的形式再回来**。

没装 ruff 就跳过——它是开发依赖，不是运行依赖。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ruff() -> str | None:
    """优先用当前解释器同目录下的 ruff（.venv/Scripts/ruff.exe）。"""
    name = "ruff.exe" if sys.platform == "win32" else "ruff"
    local = Path(sys.executable).with_name(name)
    if local.exists():
        return str(local)
    return shutil.which("ruff")


class TestRuffClean(unittest.TestCase):
    def test_no_pyflakes_errors(self):
        exe = _ruff()
        if not exe:
            self.skipTest("没装 ruff：pip install -r requirements-dev.txt")
        proc = subprocess.run(
            [exe, "check", ".", "--output-format", "concise"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            proc.returncode, 0,
            "ruff 报错（未定义名 / 未使用导入变量这类真问题，不是风格）：\n" + output)


if __name__ == "__main__":
    unittest.main()
