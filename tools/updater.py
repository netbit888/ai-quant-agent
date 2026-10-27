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

import hashlib
import json
import os
import re
import shutil
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
# 探针只做 TLS 握手 + 读一小段样本。两个值的取舍：
#   太长（30s）→ 链路已死时用户对着 0% 干等两分钟；
#   太短（10s）→ 4KB/s 的慢链路读一个 64KB 块要 16s，会被判成失败——
#                而那条链路本来是能下完的。
# 折中：15 秒 + 探针专用 16KB 小块（16KB/15s 覆盖到 1KB/s），
# 最坏 4 次尝试 + 退避 ≈ 70 秒出结果。
PROBE_TIMEOUT = 15.0
PROBE_CHUNK = 16 * 1024
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

# ---- 分片下载参数 ---------------------------------------------------------
# 国内到 release-assets.githubusercontent.com 的限速常常是「每连接」限的。实测
# （2026-10-27，15 秒窗口，同一份 22.7MB 资产）：单流 ~0、4 段 60、8 段 94、
# 16 段 128 KB/s，单连接吞吐 15→11.8→8 单调下降——说明瓶颈在每连接，不在总量。
# 所以段数不写死，由 _calibrate 现场量出来再定档。
SEGMENTS = 4              # 起步档，也是快链路的选择
SEGMENTS_MAX = 16         # 慢链路的上限：再多就是给自己造 RST
# 小于这个体积不值得分片（Setup 22.7MB / Delta 11.5MB 都远超阈值）
SEG_MIN_BYTES = 4 << 20
SEG_CHUNK = 64 * 1024
# 单个分片的重试次数（不是整体次数）：死一段，另外几段已落盘的字节一个都不用重下
RETRIES = 3
RETRY_BASE = 1.5          # 退避 1.5s / 3s / 6s
PART_SUFFIX = ".part"
# 档位判据：单连接实测吞吐（KB/s）。**三档粗分，刻意不做细调**——在一条链路、
# 一个 15 秒窗口上拟合出来的"最优点"换条网就不成立，和当初扫 tp 找最优一个坑。
FAST_KBPS = 512           # ≥512：4 段足够，别贪
MID_KBPS = 128            # 128~512：8 段
# 测速样本：最多 256KB 或最多 4 秒。4 秒上限是给慢链路的——8KB/s 时要下完
# 256KB 得 32 秒，等不起；而 32KB/4s 已经足够判定"这条链在掐单连接"。
CANARY_MAX = 256 * 1024
CANARY_SECONDS = 4.0

# RLock 而不是 Lock：start_check 持锁期间要调 _set（它自己也拿锁），
# 非重入锁在这里当场死锁——第一个跑 start_check 的测试就抓住了这个 bug。
_lock = threading.RLock()
_state: dict = {
    "check": {"status": "idle", "current": APP_VERSION, "latest": None,
              "newer": False, "notes": None, "error": None, "checked_at": 0.0},
    "download": {"status": "idle", "received": 0, "total": 0, "pct": 0,
                 "file": None, "error": None, "segments": 0, "retries": 0,
                 "probe_kbps": 0.0, "probe_try": 0},
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


def _digest(asset: dict) -> str:
    """取资产摘要。GitHub 新版 API 给 ``digest="sha256:<hex>"``；
    1.5.4 之前上传的老资产没有这个字段 → 返回空串，调用方跳过校验。

    为什么必须校验：现在下的是 GitHub 直连 HTTPS，摘要只是"防半截"；一旦将来
    加镜像源，没有摘要就等于让第三方随便换包。
    """
    d = str(asset.get("digest") or "")
    return d[7:].lower() if d.startswith("sha256:") else ""


def fetch_latest() -> dict | None:
    """GET releases/latest。返回
    ``{'tag','version','notes','asset_url','asset_size','asset_sha','delta'}``；网络失败/
    限流/没有 Setup 资产一律返回 None（原因存 ``_last_fail_reason``，不抛）。
    ``delta`` 是 ``{'base','url','size','sha'}`` 或 None——只挑 base==本机版本 且
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
                               "size": int(a.get("size") or 0),
                               "sha": _digest(a)})
    delta = max(deltas, key=lambda d: (d["size"], d["url"])) if deltas else None
    return {"tag": tag,
            "version": release_ver,
            "notes": str(data.get("body") or ""),
            "asset_url": url,
            "asset_size": int(asset.get("size") or 0),
            "asset_sha": _digest(asset),
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


class _IntegrityError(Exception):
    """内容层面的失败（大小/文件头/摘要）。和传输出错处理方式相反：全清，不留。"""


def _parts_of(dest: Path) -> list[Path]:
    """dest 的分片文件，**按段号数值**排序。

    不能用字典序：part10 会排在 part2 前面。段数 >9 之后字典序拼装必坏文件，
    而"坏文件"在这里的代价是装出一个半截 exe。
    """
    pre = dest.name + PART_SUFFIX
    keyed: list[tuple[int, Path]] = []
    for p in dest.parent.glob(pre + "*"):
        try:
            keyed.append((int(p.name[len(pre):]), p))
        except ValueError:      # 不是自己的分片（比如 .part.bak 之类），忽略
            continue
    return [p for _, p in sorted(keyed, key=lambda kv: kv[0])]


def _drop_parts(dest: Path) -> None:
    for p in _parts_of(dest):
        p.unlink(missing_ok=True)


def _clear_dir(d: Path, keep_parts_of: str | None = None) -> None:
    """清掉下载目录里的旧产物。

    ``keep_parts_of`` 传本次目标的文件名时，它的分片（``.partN``）保留——
    用户点「重试下载」能接着上次的字节续，而不是从 0 开始。启动扫尾传 None，
    分片一起清：**不做跨进程重启续传**，启动即清残留的语义更干净，而进程内的
    自动重试已经覆盖绝大部分断流场景。
    """
    d.mkdir(parents=True, exist_ok=True)
    for pattern in ("AIQuant-Setup-*.exe", "AIQuant-Delta-*.exe"):
        for p in d.glob(pattern):
            p.unlink(missing_ok=True)
    keep = f"{keep_parts_of}{PART_SUFFIX}" if keep_parts_of else None
    for p in d.glob("*.exe" + PART_SUFFIX + "*"):
        if keep and p.name.startswith(keep):
            continue
        p.unlink(missing_ok=True)
    for p in d.glob("*.exe.tmp"):
        p.unlink(missing_ok=True)


def _plan_segments(total: int, n: int) -> list[tuple[int, int]]:
    """把 [0, total) 均分成 n 段，返回含端点的闭区间列表。"""
    n = max(1, min(n, total))          # 极小体兜底：段数不能多于字节数
    step = total // n
    segs: list[tuple[int, int]] = []
    start = 0
    for i in range(n):
        end = total - 1 if i == n - 1 else start + step - 1
        segs.append((start, end))
        start = end + 1
    return segs


def _pick_segments(kbps: float) -> int:
    """按实测单连接吞吐选档。"""
    if kbps >= FAST_KBPS:
        return SEGMENTS
    if kbps >= MID_KBPS:
        return 8
    return SEGMENTS_MAX


class _NoRange(Exception):
    """服务端**明确**不支持 Range（回 200 / 无 Content-Range / 总长对不上）。

    永久性判定，不重试。必须和"网络抖了一下"区分开——把两者混成一件事的后果
    是链路抽一下就永久降级成单流，而单流正是最慢的那条路。
    """


def _probe_once(url: str, total: int, part0: Path) -> tuple[bool, float]:
    """发一次测速样本。网络异常往外抛，服务端不支持 Range 抛 _NoRange。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": f"bytes=0-{CANARY_MAX - 1}",
        "Accept-Encoding": "identity"})
    got = 0
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if status != 206:
            raise _NoRange(f"HTTP {status}")
        hdrs = getattr(resp, "headers", None) or {}
        m = re.match(r"bytes\s+\d+-\d+/(\d+)",
                     str(hdrs.get("Content-Range") or ""))
        if not m:
            raise _NoRange("no Content-Range")
        if total and int(m.group(1)) != total:
            raise _NoRange(f"size {m.group(1)} != {total}")
        t0 = time.time()
        part0.parent.mkdir(parents=True, exist_ok=True)
        with part0.open("wb") as fh:
            while time.time() - t0 < CANARY_SECONDS and got < CANARY_MAX:
                # 小块读：慢链路上大块会把单次 read 拖过超时，误判成失败
                chunk = resp.read(PROBE_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
        dt = max(time.time() - t0, 1e-6)
    return True, got / dt / 1024


def _probe_and_measure(url: str, total: int, part0: Path) -> tuple[bool, float]:
    """一次请求干两件事：确认服务端吃 Range，量出**单连接**真实吞吐。

    样本取「最多 CANARY_MAX 或最多 CANARY_SECONDS」，写进 part0 —— 第 0 段本来
    就从字节 0 开始，这段数据一个字节都不浪费，后面 _fetch_segment 从 part0 的
    现有长度直接续。4 秒上限是给慢链路的：8KB/s 时下完 256KB 要 32 秒，等不起。

    Range 头能活过 302：urllib 的 HTTPRedirectHandler.redirect_request 只剔除
    content-length / content-type，所以 github.com → release-assets.githubusercontent.com
    这一跳之后 Range 依然在。分片和测速都架在这条上。

    网络类失败按 RETRIES 退避重试；重试耗尽就往外抛，让上层记成传输错误
    （保留分片、可以点重试），**绝不因为一次抖动就降级成单流**。
    """
    for attempt in range(RETRIES + 1):
        try:
            # 前端靠 probe_try 把"还没连上，正在第 N 次尝试"和下不动区分开
            _set("download", probe_try=attempt + 1)
            return _probe_once(url, total, part0)
        except _NoRange:
            return False, 0.0
        except Exception:  # noqa: BLE001
            if attempt >= RETRIES:
                raise
            time.sleep(RETRY_BASE * (2 ** attempt))
    raise RuntimeError("unreachable")


def _publish(state: dict, **extra) -> None:
    """把 4 个分片的累计字节汇总进 state。99 上限：100 只在真正 done 时出现。"""
    with state["lock"]:
        received = sum(state["heights"].values())
        retries = state["retries"]
    total = state["total"]
    pct = min(99, round(received * 100 / total)) if total else 0
    _set("download", received=received, pct=pct, retries=retries, **extra)


def _fetch_segment(url: str, start: int, end: int, part: Path,
                   state: dict) -> None:
    """下一次分片。从 part 现有长度续起，失败就抛（重试在上一层）。"""
    want = end - start + 1
    have = part.stat().st_size if part.is_file() else 0
    if have > want:                    # 超长 = 上一轮的残留，扔掉重来
        part.unlink(missing_ok=True)
        have = 0
    with state["lock"]:
        state["heights"][part.name] = have
    _publish(state)
    if have >= want:
        return
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Range": f"bytes={start + have}-{end}",
        "Accept-Encoding": "identity"})
    # 追加写（"ab"）就是续传本身：文件里有多少字节，就从哪儿接着要。
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
            part.open("ab") as fh:
        while have < want:
            chunk = resp.read(SEG_CHUNK)
            if not chunk:
                break
            room = want - have
            if len(chunk) > room:      # 服务端忽略 Range 尾部时截掉多余的
                chunk = chunk[:room]
            fh.write(chunk)
            have += len(chunk)
            with state["lock"]:
                state["heights"][part.name] = have
            _publish(state)
    if have < want:
        raise IOError(f"分片 {part.name} 未下完：{have}/{want}")


def _download_segment(url: str, start: int, end: int, part: Path,
                      state: dict) -> None:
    """带退避重试的段下载。重试只补缺口，已落盘的字节一个都不重下。"""
    last: BaseException | None = None
    for attempt in range(RETRIES + 1):
        try:
            _fetch_segment(url, start, end, part, state)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= RETRIES:
                break
            with state["lock"]:
                state["retries"] += 1
            _publish(state)
            time.sleep(RETRY_BASE * (2 ** attempt))
    raise last if last is not None else IOError("分片下载失败")


def _assemble(parts: list[Path], tmp: Path, total: int) -> None:
    tmp.unlink(missing_ok=True)
    with tmp.open("wb") as out:
        for p in parts:
            with p.open("rb") as fh:
                shutil.copyfileobj(fh, out, SEG_CHUNK)
    size = tmp.stat().st_size
    if size != total:
        raise _IntegrityError(f"拼装后大小不符：{size}/{total} 字节")


def _download_segmented(url: str, total: int, dest: Path, tmp: Path,
                        n: int) -> None:
    segs = _plan_segments(total, n)
    parts = [dest.with_name(dest.name + f"{PART_SUFFIX}{i}")
             for i in range(len(segs))]
    state = {"total": total, "heights": {}, "retries": 0,
             "lock": threading.Lock()}
    _set("download", segments=len(segs), retries=0, received=0, pct=0)
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            _download_segment(url, segs[i][0], segs[i][1], parts[i], state)
        except BaseException as exc:  # noqa: BLE001  线程里不抛，交给主线程
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), name=f"update-seg{i}",
                                daemon=True) for i in range(len(segs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    _assemble(parts, tmp, total)


def _stream_once(url: str, total: int, tmp: Path) -> None:
    _set("download", segments=1, retries=0)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Encoding": "identity"})
    received = 0
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
            tmp.open("wb") as fh:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            received += len(chunk)
            pct = min(99, round(received * 100 / total) if total else 0)
            _set("download", received=received, pct=pct)


def _download_stream(url: str, total: int, tmp: Path) -> None:
    """单连接整下：Range 探测确认不支持分片时的回退路径。

    没有 Range 就没有续传，只能从头重来——所以这里至少要有重试，否则一次
    抖动就整次失败。回退路径不该比主路径更脆。
    """
    last: BaseException | None = None
    for attempt in range(RETRIES + 1):
        try:
            _stream_once(url, total, tmp)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            tmp.unlink(missing_ok=True)
            if attempt >= RETRIES:
                break
            _set("download", retries=attempt + 1, received=0, pct=0)
            time.sleep(RETRY_BASE * (2 ** attempt))
    raise last if last is not None else IOError("单流下载失败")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _verify(path: Path, total: int, sha: str | None) -> None:
    size = path.stat().st_size
    if total and size != total:
        raise _IntegrityError(f"下载不完整：{size}/{total} 字节")
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic != b"MZ":
        raise _IntegrityError("下载的不是 Windows 可执行文件（缺 MZ 头）")
    if sha and _sha256_of(path) != sha.lower():
        raise _IntegrityError("sha256 校验失败（文件被改过或传坏了）")


def _brief_net_error(exc: BaseException) -> str:
    """把底层网络异常翻成用户能看懂的一句话，原文放括号里留着排查。

    原始信息长这样：``URLError: <urlopen error _ssl.c:1015: The handshake
    operation timed out>``——对着这行字，用户既不知道发生了什么，也不知道
    下一步该干嘛。
    """
    s = str(exc)
    low = s.lower()
    if "handshake" in low or "timed out" in low:
        return f"连接超时，网络到 GitHub 不通（{s}）"
    if "remote end closed" in low or "reset" in low or "disconnected" in low:
        return f"连接被中断（{s}）"
    if "getaddrinfo" in low or "name or service" in low:
        return f"域名解析失败（{s}）"
    return f"{type(exc).__name__}: {s}"


def _do_download(url: str, total: int, dest: Path, sha: str | None = None) -> None:
    """分片 + 续传下载。**只有全须全尾才让文件出现在 dest 上。**

    铁律不变（原来的"半截文件一律删掉，绝不复用"）：dest 只由 os.replace
    产生，前置条件是全部段齐、字节数吻合、MZ 头正确、（有摘要时）sha256 匹配。
    所以 apply_ready / spawn_setup_then_quit 一行都不用改。

    失败分两类，处理相反：
      传输出错（断流/超时）→ 只删 dest 和 .tmp，**保留 .part***。那是续传的
        本钱，下次重试接着下，不重下已经拿到的字节。
      内容不对（大小/头/摘要）→ 全清，一个字节都不留。

    段数不是常量：先用一段 256KB/4 秒的样本量出单连接吞吐，再按档位决定 4/8/16。
    量出来的值也写进 state（probe_kbps），面板和排查都看得见。
    """
    tmp = dest.with_name(dest.name + ".tmp")
    part0 = dest.with_name(dest.name + PART_SUFFIX + "0")
    _set("download", status="downloading", received=0, total=total, pct=0,
         file=str(dest), error=None, segments=0, retries=0, probe_kbps=0.0,
         probe_try=0)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        can_segment, kbps = (False, 0.0)
        if total >= SEG_MIN_BYTES:
            can_segment, kbps = _probe_and_measure(url, total, part0)
        _set("download", probe_kbps=round(kbps, 1))
        if can_segment:
            _download_segmented(url, total, dest, tmp, _pick_segments(kbps))
        else:
            _download_stream(url, total, tmp)
        _verify(tmp, total, sha)
        os.replace(tmp, dest)          # 原子落地：dest 出现的瞬间就是完好的
        _drop_parts(dest)
        size = dest.stat().st_size
        _set("download", status="done", received=size, total=size or total,
             pct=100)
    except _IntegrityError as exc:
        tmp.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        _drop_parts(dest)
        _set("download", status="error", received=0, pct=0, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        kept = sum(p.stat().st_size for p in _parts_of(dest)) if total else 0
        _set("download", status="error", received=kept,
             pct=min(99, round(kept * 100 / total)) if total else 0,
             error=_brief_net_error(exc)
                   + ("（已下载的部分已保留，重试会接着下）" if kept else ""))


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
        asset_sha = delta.get("sha") or ""
    else:
        dest_name = f"AIQuant-Setup-{info['version']}.exe"
        asset_url, asset_size = info["asset_url"], info["asset_size"]
        asset_sha = info.get("asset_sha") or ""
    _set("check", plan=plan, delta_base=delta.get("base"),
         delta_size=delta.get("size"), full_size=info.get("asset_size"))
    d = _update_dir()
    # 保留本次目标的分片：失败后点「重试下载」是从缺口续，不是从 0 重来。
    _clear_dir(d, keep_parts_of=dest_name)
    dest = d / dest_name
    threading.Thread(target=_run_download_quiet,
                     args=(asset_url, asset_size, dest, asset_sha),
                     name="update-download", daemon=True).start()
    return {"ok": True, "status": "downloading"}


def _run_download_quiet(url: str, total: int, dest: Path,
                        sha: str | None = None) -> None:
    try:
        _do_download(url, total, dest, sha)
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
