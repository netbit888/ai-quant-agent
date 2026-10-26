"""冻结 / 源码两种形态下的路径约定。

一个原则：**能写的东西一律不放进 bundle。**

    bundle_root()   只读资源：dashboard_template.html、strategies/*.py
    app_root()      可写数据：data/、config/

冻结后（PyInstaller onedir）：
    sys.executable  = <绿色版目录>/AIQuant.exe   →  app_root()
    sys._MEIPASS    = <绿色版目录>/_internal     →  bundle_root()

两者必须分开。如果数据的根也走 _MEIPASS，绿色版的账本会写进 _internal/，
覆盖升级或重装就一起没了——而 data/live 下的账本是**唯一副本**
（MT5 历史有保留期、换服务器就丢），丢了没有任何地方能恢复。

源码直跑时两个根都等于仓库根，行为和打包前逐字节一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 可选覆盖：把 data/config 放到 U 盘或同步盘。既是测试逃生口，也是运维需要。
DATA_ENV = "AQA_DATA_DIR"
STRATEGY_ENV = "AQA_STRATEGY_DIR"


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """只读资源根。**不要往这里写任何东西。**"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """可写数据根。绿化版 = exe 所在目录；源码 = 仓库根。"""
    env = os.environ.get(DATA_ENV)
    if env:
        return Path(env).expanduser()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return app_root() / "data"


def config_dir() -> Path:
    return app_root() / "config"


def env_file() -> Path:
    """`.env` 的唯一出口。

    顺序有讲究：先看 `config/.env`，再回落 exe 同级的 `.env`（就地升级的老用户），
    都不存在时返回 `config/.env`（新装第一份就写这里）。
    这样源码直跑继续用仓库根的 `.env`——现有习惯和测试都不受影响；
    绿化版首次配置则落在 `config/` 里，和只读资源彻底分开。
    """
    cfg = config_dir() / ".env"
    if cfg.is_file():
        return cfg
    legacy = app_root() / ".env"
    return legacy if legacy.is_file() else cfg


def strategies_dir() -> Path:
    """策略源码目录。`/api/strategies` 要 glob 真文件 + exec_module，不能是压缩包。"""
    env = os.environ.get(STRATEGY_ENV)
    return Path(env).expanduser() if env else bundle_root() / "strategies"


def template_file() -> Path:
    return bundle_root() / "tools" / "dashboard_template.html"


def setup_marker() -> Path:
    """首启向导的「不再提示」标记。跟着数据走，删 config/ 就重新提示。"""
    return config_dir() / ".setup_done"


def logs_dir() -> Path:
    return data_dir() / "logs"


def ensure_writable_dirs() -> None:
    """启动时调一次。

    如果程序被放到 Program Files 这类只读目录，这里就会当场抛错并给出
    明确原因——而不是等用户第一次点「开始做单」才 500，那时候更难查。
    """
    for p in (data_dir(), config_dir(), logs_dir()):
        p.mkdir(parents=True, exist_ok=True)
    probe = config_dir() / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)
