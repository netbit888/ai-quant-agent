# -*- mode: python ; coding: utf-8 -*-
"""AI Quant Agent 绿色版（onedir）打包配置。

构建：
    .venv\\Scripts\\pyinstaller.exe --noconfirm aqa.spec

产物：
    dist/AIQuant/AIQuant.exe        ← 双击启动
    dist/AIQuant/_internal/         ← 只读资源与依赖，别动
    dist/AIQuant/data/              ← 运行时生成：账本、日志（可写）

一条原则：能写的东西一律在 exe 同级，不能写进 _internal。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)


def _subs(*names):
    """collect_submodules 对不存在的包会抛异常；这里降级成空列表，
    免得某个可选依赖没了整个打包就中断。"""
    out = []
    for n in names:
        try:
            out += collect_submodules(n)
        except Exception:  # noqa: BLE001
            pass
    return out


# ---- 只读资源：必须是真磁盘文件 ------------------------------------------
datas = [
    # 面板模板。每次请求都从磁盘重读，缺失就直接 500 "template missing"。
    (str(ROOT / "tools" / "dashboard_template.html"), "tools"),
    # 策略源码：/api/strategies 靠 glob + exec_module 动态加载，压缩进 PYZ 读不到。
    (str(ROOT / "strategies"), "strategies"),
]

# ---- 静态分析看不见的模块 ------------------------------------------------
hiddenimports = [
    # 原生扩展（.pyd），静态分析经常漏
    "MetaTrader5", "MetaTrader5._core",
    # 面板的函数内 import（PyInstaller 通常能扫到，列出来更保险）
    "live.paths", "live.journal", "live.advisor", "live.stats", "live.rules",
    "live.sessions", "live.autotrade", "live.positions",
    "live.scoring", "live.quality", "live.cost", "live.profile",
    "agent.llm",
    "tools.lesson_stats", "tools.switch_llm",
    "execution.live",
    # 策略源码在运行时被 exec_module 加载，它内部的 import 静态扫不到
    "backtest.structure", "backtest.models", "backtest.indicators", "backtest.symbols",
    *_subs("backtest"),
    # openai 3.x 用的是新包名 httpx2 / httpcore2，PyInstaller 自带 hook 只认旧名
    *_subs("httpx2", "httpcore2"),
]

excludes = [
    # ---- v1 明确不打包 ----
    # agent.core 是 LLM 策略迭代入口，会写 docs/ 并依赖一堆回测链路。
    "agent.core",
    "aqa",
    "tools.evaluate", "tools.sweep", "tools.run_pa", "tools.run_portfolio",
    "tools.replay_exit", "tools.migrate_accounts", "tools.profile",
    "tools.check_structure",
    "mt5.backtest", "mt5.report",
    # ---- 体积 ----
    "tkinter", "_tkinter", "turtle", "turtledemo",
    "unittest", "pydoc", "doctest", "test",
    "pytest", "setuptools", "pip", "wheel",
]

a = Analysis(
    ["aqa_launcher.py"],
    pathex=[str(ROOT)],           # tools / strategies / live 都在源码树里，不在 site-packages
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AIQuant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX 会引发 Defender 误报，对 .pyd 也没有收益
    console=True,                 # v1 保留黑窗：崩溃可见、能 Ctrl+C 看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AIQuant",
)
