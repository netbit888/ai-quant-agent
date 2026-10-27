"""应用内自动更新：检测 GitHub Release → 下载 → 静默升级 → 自动重启。

只服务**安装版**（ui_mode()=="full"）。源码直跑没有"安装"这个环节，
git pull 后重启进程就是更新；绿色版没有 Inno 升级通道（覆盖解压是手动动作），
两者都不显示更新 UI、也拒绝 apply。

一条铁律：**更新器永远不许把面板搞挂**。检测是 daemon 线程 + 短超时 +
异常全吞（中国办公网到 api.github.com 限流/超时是常态，60 次/时/IP 的
无鉴权配额在 NAT 出口说没就没）；下载失败只改 state，绝不抛出到 HTTP 层。

两种升级包（同一套安装/重启时序，只是 Setup 文件不同）::

    full   AIQuant-Setup-<ver>.exe          完整安装包（22.6MB 级，永远兜底）
    delta  AIQuant-Delta-<base>-to-<ver>.exe 文件级增量包（~9.5MB，代码改动）
           只携带上一版以来变化的文件；未变化文件由 Inno 的 external 标志
           引用已安装文件（零下载量，但进卸载日志，卸载仍干净）。
           base 必须等于本机版本才可用，否则自动回退 full——增量包一旦与
           本机内容对不上，装出来就是混合版本，这是绝不接受的。

升级时序（为什么是 3 秒延迟，见 spawn_setup_then_quit）::

    用户点「立即升级并重启」
      → dashboard._post_update_apply：confirm → prepare_apply → 查持仓
        → spawn_setup_then_quit()（起延迟安装链，不阻塞）
        → 本进程走 _shutdown → 退出，AppMutex 释放
      → T+0.5s  进程死透
      → T+3s    Setup /SILENT 启动，覆盖安装（data/ config/ 天然保留）
      → T+~10s  Setup [Run] 拉起新版 AIQuant.exe
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app_version import APP_VERSION, parse_version
from live import appdirs

REPO = "netbit888/ai-quant-agent"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
# GitHub API 强制要求 User-Agent，缺头直接 403。
UA = f"AIQuant-Updater/{APP_VERSION}"
ACCEPT = "application/vnd.github+json"

CHECK_TIMEOUT = 5.0        # 检测是启动路径上的客人，超时要短
DOWNLOAD_TIMEOUT = 30.0    # 单次 read 的 socket 超时；大文件靠循环续读
CHECK_THROTTLE_SEC = 60.0  # 无鉴权配额 60 次/时/IP，60 秒节流是底线
ASSET_RE = re.compile(r"AIQuant-Setup-.+\.exe")
# delta 资产名自证身份：AIQuant-Delta-<base>-to-<target>.exe。
# base 必须严格等于本机 APP_VERSION 才可用；两个版本段都取纯数字，
# 脏 tag 进不了正则，天然不会被选中。
DELTA_RE = re.compile(r"AIQuant-Delta-(\d+(?:\.\d+)+)-to-(\d+(?:\.\d+)+)\.exe")
# delta 相对完整包的收益门槛：省不到 20% 就不值得冒"增量链路"的风险
# （病态情形下 delta 可能几乎和完整包一样大）。
DELTA_MIN_SAVING = 0.8
CHUNK = 64 * 1024

# RLock 而不是 Lock：start_check 持锁期间要调 _set（它自己也拿锁），
# 非重入锁在这里当场死锁——第一个跑 start_check 的测试就抓住了这个 bug。
_lock = threading.RLock()
_state: dict = {
    "check": {"status": "idle", "current": APP_VERSION, "latest": None,
              "newer": False, "notes": None, "error": None, "checked_at": 0.0},
    "download": {"status": "idle", "received": 0, "total": 0, "pct": 0,
                 "file": None, "error": None},
}
_last_check = 0.0


def _set(section: str, **kv) -> None:
    with _lock:
        _state[section].update(kv)


def snapshot() -> dict:
    """GET /api/app/update/progress 的载荷。check + download 一起回。"""
    with _lock:
        return {"mode": ui_mode(), "check": dict(_state["check"]),
                "download": dict(_state["download"])}


# ---------------------------------------------------------------- 模式判定

def ui_mode() -> str:
    """'full'（安装版）| 'portable'（绿色版）| 'source'（源码直跑）。

    安装版判定看 **exe 同级有没有 unins000.exe**（Inno 卸载器），不看路径
    前缀——用户可能自定义了安装目录。绿色版和源码没有升级通道。
    """
    if not appdirs.is_frozen():
        return "source"
    exe_dir = Path(appdirs.app_root())
    return "full" if (exe_dir / "unins000.exe").is_file() else "portable"


# ---------------------------------------------------------------- 检测

_last_fail_reason: str | None = None


def _decide_plan(info: dict) -> str:
    """'delta' | 'full'。规则很窄，错边永远倒向 full（完整包永远是对的）。"""
    delta = info.get("delta")
    if not delta:
        return "full"
    if delta["base"] != APP_VERSION:
        return "full"  # base 对不上：装出来是混合版本，绝不接受
    full = int(info.get("asset_size") or 0)
    if full and int(delta["size"]) >= full * DELTA_MIN_SAVING:
        return "full"  # 省不了 20% 就别走增量链路
    return "delta"


def fetch_latest() -> dict | None:
    """GET releases/latest。返回
    ``{'tag','version','notes','asset_url','asset_size','delta'}``；网络失败/
    限流/没有 Setup 资产一律返回 None（原因存 ``_last_fail_reason``，不抛）。
    ``delta`` 是 ``{'base','url','size'}`` 或 None——只挑 base==本机版本 且
    target==本次发布 的那个，其余 delta 资产视而不见。
    """
    global _last_fail_reason
    _last_fail_reason = None
    req = urllib.request.Request(RELEASE_API, headers={
        "User-Agent": UA, "Accept": ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            _last_fail_reason = f"GitHub 限流（HTTP {exc.code}），配额一小时后恢复"
            print(f"[updater] rate limited ({exc.code})，稍后再试", flush=True)
        elif exc.code == 404:
            _last_fail_reason = "仓库不可达（404：私有/不存在，或还没有 Release）"
            print(f"[updater] HTTP 404", flush=True)
        else:
            _last_fail_reason = f"GitHub HTTP {exc.code}"
            print(f"[updater] HTTP {exc.code}", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001  网络层的任何失败都不许冒泡
        _last_fail_reason = f"网络不通（{type(exc).__name__}）"
        print(f"[updater] check failed: {type(exc).__name__}", flush=True)
        return None

    tag = str(data.get("tag_name") or "")
    if not tag:
        _last_fail_reason = "Release 响应缺少 tag_name"
        return None
    # asset 可能有好几个（Setup + Delta + 未来更多）：挑字典序最大的 Setup，
    # 版本号大的名字也大（AIQuant-Setup-1.10.0 > 1.9.0 按字典序也成立，
    # 但我们的版本段固定两位起步，不会撞上这个坑）。
    assets = [a for a in (data.get("assets") or [])
              if ASSET_RE.fullmatch(str(a.get("name") or ""))]
    if not assets:
        print("[updater] release has no setup asset", flush=True)
        return None
    asset = max(assets, key=lambda a: str(a.get("name")))
    url = str(asset.get("browser_download_url") or "")
    if not url:
        return None
    release_ver = tag.lstrip("vV")
    # delta：base==本机版本 且 target==本次发布才算数；多于一个取字典序最大
    deltas = []
    for a in (data.get("assets") or []):
        m = DELTA_RE.fullmatch(str(a.get("name") or ""))
        if m and m.group(1) == APP_VERSION and m.group(2) == release_ver:
            u = str(a.get("browser_download_url") or "")
            if u:
                deltas.append({"base": m.group(1), "url": u,
                               "size": int(a.get("size") or 0)})
    delta = max(deltas, key=lambda d: (d["size"], d["url"])) if deltas else None
    return {"tag": tag,
            "version": release_ver,
            "notes": str(data.get("body") or ""),
            "asset_url": url,
            "asset_size": int(asset.get("size") or 0),
            "delta": delta}


def is_newer(latest: str, current: str) -> bool:
    """元组比较；latest 解析失败 → False（宁可不提示，不许误报）。"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _do_check() -> None:
    global _last_check
    info = fetch_latest()
    _last_check = time.time()
    now = time.time()
    if info is None:
        _set("check", status="error",
             error=_last_fail_reason or "无法访问 GitHub（网络或限流）",
             checked_at=now)
        return
    newer = is_newer(info["version"], APP_VERSION)
    plan = _decide_plan(info) if newer else "full"
    delta = info.get("delta") or {}
    _set("check", status="ok", latest=info["version"], newer=newer,
         notes=info["notes"], error=None, checked_at=now,
         plan=plan,
         delta_base=delta.get("base"),
         delta_size=delta.get("size"),
         full_size=info["asset_size"])


def start_check(force: bool = False) -> dict:
    """起 daemon 线程检测，立即返回。60 秒节流（force=True 手动检查绕过）。

    返回的是**调用时刻**的快照——结果要靠 /api/app/update/progress 轮询。
    """
    with _lock:
        fresh = (time.time() - _last_check) < CHECK_THROTTLE_SEC
        if fresh and not force and _state["check"]["status"] in ("ok", "checking"):
            return {"status": _state["check"]["status"]}
        _set("check", status="checking", error=None)
    threading.Thread(target=_run_quiet, name="update-check",
                     daemon=True).start()
    return {"status": "checking"}


def _run_quiet() -> None:
    try:
        _do_check()
    except Exception as exc:  # noqa: BLE001  线程内的最后防线
        _set("check", status="error", error=f"{type(exc).__name__}: {exc}",
             checked_at=time.time())


# ---------------------------------------------------------------- 下载

def _update_dir() -> Path:
    """延迟解析：测试用 AQA_DATA_DIR 换数据根后，这里要跟着变。"""
    return appdirs.data_dir() / "update"


def _clear_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for pattern in ("AIQuant-Setup-*.exe", "AIQuant-Delta-*.exe"):
        for p in d.glob(pattern):
            p.unlink(missing_ok=True)


def _do_download(url: str, total: int, dest: Path) -> None:
    _set("download", status="downloading", received=0, total=total, pct=0,
         file=str(dest), error=None)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                dest.open("wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
                pct = min(100, round(received * 100 / total) if total else 0)
                _set("download", received=received, pct=pct)
        size = dest.stat().st_size
        with dest.open("rb") as fh:
            magic = fh.read(2)
        if total and size != total:
            raise IOError(f"下载不完整：{size}/{total} 字节")
        if magic != b"MZ":
            raise IOError("下载的不是 Windows 可执行文件（缺 MZ 头）")
        _set("download", status="done", received=size, total=size or total,
             pct=100)
    except Exception as exc:  # noqa: BLE001  半截文件一律删掉，绝不复用
        dest.unlink(missing_ok=True)
        _set("download", status="error", received=0, pct=0,
             error=f"{type(exc).__name__}: {exc}")


def start_download() -> dict:
    """按 check 结果下载对应 asset。已下载完成且文件还在 → 幂等直接返回。"""
    with _lock:
        check = dict(_state["check"])
        dl = dict(_state["download"])
    if check.get("status") != "ok" or not check.get("newer"):
        return {"ok": False, "error": "先检查出有新版本才能下载"}
    if dl.get("status") == "downloading":
        return {"ok": False, "error": "已经在下载中了"}
    if dl.get("status") == "done" and apply_ready():
        return {"ok": True, "already": True}
    info = fetch_latest()
    if info is None:
        _set("download", status="error", error="获取下载地址失败（网络或限流）")
        return {"ok": False, "error": "获取下载地址失败"}
    # 决策基于**这次**的 fetch 结果而不是 check 缓存：资产可能刚被删/改名，
    # 下载的东西必须和决策一致，同时把决策写回 check（UI 与实际动作同步）。
    plan = _decide_plan(info)
    delta = info.get("delta") or {}
    if plan == "delta":
        dest_name = f"AIQuant-Delta-{delta['base']}-to-{info['version']}.exe"
        asset_url, asset_size = delta["url"], delta["size"]
    else:
        dest_name = f"AIQuant-Setup-{info['version']}.exe"
        asset_url, asset_size = info["asset_url"], info["asset_size"]
    _set("check", plan=plan, delta_base=delta.get("base"),
         delta_size=delta.get("size"), full_size=info.get("asset_size"))
    d = _update_dir()
    _clear_dir(d)
    dest = d / dest_name
    threading.Thread(target=_run_download_quiet,
                     args=(asset_url, asset_size, dest),
                     name="update-download", daemon=True).start()
    return {"ok": True, "status": "downloading"}


def _run_download_quiet(url: str, total: int, dest: Path) -> None:
    try:
        _do_download(url, total, dest)
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        _set("download", status="error", received=0, pct=0,
             error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 应用

def apply_ready() -> bool:
    """安装包就绪：下载完成 + 文件还在 + 大小没变（防中途被清）。"""
    with _lock:
        dl = dict(_state["download"])
    if dl.get("status") != "done" or not dl.get("file"):
        return False
    p = Path(dl["file"])
    return p.is_file() and (not dl.get("total") or p.stat().st_size == dl["total"])


def prepare_apply() -> dict:
    if ui_mode() != "full":
        return {"ok": False, "error": "只有安装版支持应用内升级"}
    if not apply_ready():
        return {"ok": False, "error": "安装包未就绪或已被清理，请重新下载"}
    return {"ok": True, "error": None}


def spawn_setup_then_quit() -> None:
    """起「等 3 秒 → 静默安装」的延迟链，**立即返回**（退出交给调用方）。

    3 秒是给本进程留的死亡窗口：写完 HTTP 响应 → _shutdown → destroy，
    AppMutex 释放。Setup 初始化早期就查 mutex（aqa.iss AppMutex=
    Global\\AIQuantDashboard），不延迟就 spawn 会撞上 Inno 的强关流程。
    万一本进程没死透，iss 的 CloseApplications=yes 兜底。

    /SILENT（不是 /VERYSILENT）：保留安装进度窗，用户看得见「在装」，安心。
    装完由 iss [Run]（无 skipifsilent）拉起新版。

    full 和 delta 走同一条链：两者都是 Inno Setup exe（delta 与完整包同
    AppId，覆盖升级、更新卸载信息与注册表版本号），dest 只影响文件名。
    """
    with _lock:
        dest = _state["download"].get("file")
    if not dest or not Path(dest).is_file():
        raise RuntimeError("安装包不存在")
    cmd = (f'cmd /c timeout /t 3 /nobreak >nul && start "" "{dest}" /SILENT')
    subprocess.Popen(cmd, shell=False,
                     creationflags=subprocess.CREATE_NO_WINDOW
                     | subprocess.DETACHED_PROCESS,
                     close_fds=True)


def cleanup_stale() -> None:
    """启动时清残留：上次升级成功后新版本第一次启动顺手扫尾。"""
    try:
        _clear_dir(_update_dir())
    except OSError:
        pass
