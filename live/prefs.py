"""面板「交易参数」的记忆：品种 / 模式 / 决策间隔。

为什么单独一个文件，而不是靠 state 里的 cfg
------------------------------------------
`autotrade_state.json` 的 cfg 是**上一场跑过的参数**，只有在真的启动过之后
才存在。用户在界面上选好了但还没开跑、或者只是想换个档位看两眼成本提示，
state 里都没有痕迹——于是每次打开面板都要从头再选一遍。这个文件就是为这条
线存在的：它记的是「上次的选择」，不是「正在跑的参数」。

为什么不放 localStorage
-----------------------
`tools/dashboard.py --attach`（浏览器调试）用的是 `private_mode=True`，
localStorage 退出即清；清一次缓存也会回到默认值。用户会以为「又没记住」。
落在数据目录里则跨浏览器、跨面板升级都留得住（安装包卸载时保留 data\\）。

**这是默认值，不是运行参数。** 真正决定一笔单怎么跑的是启动时构造的 Cfg
（写进 state）；prefs 只负责把上次的选择回填到控件上。启动前的确认弹窗
一个字都没少，所以「记住真实做单」不等于「下次偷偷开真单」。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from live import paths

# 与模板（tools/ui/html/view-setup.html）里那四个控件的出厂状态保持一致。
DEFAULT_PREFS = {
    "symbol": "XAUUSDm",
    "interval_sec": 60,
    "mode": "live",          # paper / live / assist
    "assist_paper": False,   # 仅 mode=assist 时有意义：勾了「纸面演练」
}

MODES = ("paper", "live", "assist")

# 间隔的合法档位，必须和模板里 <select id="atInterval"> 的 option 一一对应。
# 记一个界面上不存在的值回来，`select.value = "7"` 赋不进去会变成空串——
# 界面显示空白比记不住更糟：用户会以为参数丢了，实际是记了个没人认识的数。
INTERVALS = (3, 5, 10, 30, 60)


def prefs_file() -> Path:
    """落盘位置。

    走 `paths.LIVE_DIR` 而不是 `appdirs.data_dir()` 直取：单测用
    `paths.set_root(tmp)` 隔离账本，直取会写进真实数据目录。也不能
    `from live.paths import LIVE_DIR`——那样拿到的是 import 时的旧值。
    """
    return paths.LIVE_DIR / "autotrade_prefs.json"


def _clean(raw, symbols=None) -> dict:
    """把文件内容收敛成一份可用的 prefs。

    坏值一律回退默认：这个文件只影响「默认值」，为它报错会把整个面板拖下水。
    """
    out = dict(DEFAULT_PREFS)
    if not isinstance(raw, dict):
        return out

    sym = str(raw.get("symbol") or "").strip()
    # symbols 是白名单（不给就不校验）。文件可能被手改成没测过 point/contract
    # 的品种，那会把界面指向一个启动必然被 validate_cfg 拒掉的选项。
    if sym and (symbols is None or sym in symbols):
        out["symbol"] = sym

    try:
        iv = int(raw.get("interval_sec"))
    except (TypeError, ValueError):
        iv = None
    if iv in INTERVALS:
        out["interval_sec"] = iv

    mode = str(raw.get("mode") or "").strip().lower()
    if mode in MODES:
        out["mode"] = mode

    out["assist_paper"] = bool(raw.get("assist_paper", False))

    try:
        ts = int(raw.get("updated_at"))
    except (TypeError, ValueError):
        ts = 0
    if ts > 0:
        out["updated_at"] = ts
    return out


def load_prefs(symbols=None) -> dict:
    p = prefs_file()
    if not p.exists():
        return dict(DEFAULT_PREFS)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PREFS)
    return _clean(raw, symbols)


def _write(prefs: dict) -> None:
    """先写临时文件再 os.replace。

    面板 1 秒轮询一次 `/api/autotrade`，每次都会读这个文件；截断式写入的
    中间几毫秒里读方会拿到半个 JSON，然后回退到默认值——界面上的品种就会
    闪一下变回黄金。和 paths.write_json 同一个理由。
    """
    p = prefs_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_prefs(data: dict, symbols=None) -> dict:
    """合并写入。只改 data 里出现的字段，其余保持原值。

    返回 ``{ok, prefs, errors}``，**不抛异常**：这是「记住我的选择」，
    失败要说一声，但不能挡住启动。
    """
    # cur 也带白名单：文件被手改成非法品种后，任何一次保存都顺带把它纠正回来，
    # 而不是把坏值原样带下去。
    cur = load_prefs(symbols)
    patch: dict = {}
    errors: list[str] = []

    if "symbol" in data:
        sym = str(data.get("symbol") or "").strip()
        if not sym:
            errors.append("品种不能为空")
        elif symbols is not None and sym not in symbols:
            errors.append(f"品种 {sym} 不在可交易白名单（{'/'.join(symbols)}）")
        else:
            patch["symbol"] = sym

    if "interval_sec" in data:
        try:
            iv = int(data.get("interval_sec"))
        except (TypeError, ValueError):
            iv = None
        if iv in INTERVALS:
            patch["interval_sec"] = iv
        else:
            errors.append(
                f"决策间隔只接受 {'/'.join(str(i) for i in INTERVALS)} 秒，"
                f"收到 {data.get('interval_sec')!r}"
            )

    if "mode" in data:
        mode = str(data.get("mode") or "").strip().lower()
        if mode in MODES:
            patch["mode"] = mode
        else:
            errors.append(f"模式只接受 {'/'.join(MODES)}，收到 {data.get('mode')!r}")

    if "assist_paper" in data:
        patch["assist_paper"] = bool(data.get("assist_paper"))

    prefs = {**cur, **patch}
    prefs["updated_at"] = int(time.time())
    try:
        _write(prefs)
    except OSError as exc:
        errors.append(f"写入失败：{exc}")
        return {"ok": False, "prefs": prefs, "errors": errors}

    return {"ok": not errors, "prefs": prefs, "errors": errors}


def prefs_from_cfg(cfg) -> dict:
    """从实际启动的 Cfg 反推控件状态。

    启动成功时用它覆盖一次 prefs：前端 onchange 那条路可能因为页面刷新、
    请求失败漏掉，而「真的跑过什么」永远是最可信的那份记录。
    """
    assist = bool(getattr(cfg, "assist", False))
    dry = bool(getattr(cfg, "dry_run", False))
    if assist:
        mode, assist_paper = "assist", dry
    elif dry:
        mode, assist_paper = "paper", False
    else:
        mode, assist_paper = "live", False
    return {
        "symbol": str(getattr(cfg, "symbol", DEFAULT_PREFS["symbol"])),
        "interval_sec": int(getattr(cfg, "interval_sec", DEFAULT_PREFS["interval_sec"])),
        "mode": mode,
        "assist_paper": assist_paper,
    }


def clear_prefs() -> dict:
    """删除记录（回到出厂默认）。单测与「恢复默认」用。"""
    p = prefs_file()
    try:
        p.unlink()
    except OSError:
        pass
    return dict(DEFAULT_PREFS)
