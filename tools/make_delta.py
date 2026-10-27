r"""增量更新（delta）生成器：基线快照 + 文件级差异 + 独立 delta 安装包。

为什么存在：完整安装包 22.6MB 里 90% 是跨版本永远不变的第三方 DLL
（_internal 54MB），每次让用户重下纯属浪费。delta 安装包只携带
「上一版 → 新版」真正变化的文件，未变化文件用 Inno 的 `Flags: external`
引用已安装文件（零体积，但进卸载日志）。实测收益：代码改动版本
22.6MB → ~9.5MB（exe 是下限，PYZ 内部已压缩），纯模板版本 ~0.1MB。

两个子命令，对应发版流程的两个时点::

    record（release.bat 发版成功后调用）
        把刚发布的 dist\AIQuant 快照到 dist\_snapshots\<版本>\。
        **只在发布成功后记快照**——基线必须是"用户手上真实装着的东西"，
        构建了但没发布的树一旦进来，delta 就会把用户装成混合版本。
    delta（build_installer.bat 编译完完整包后调用）
        对比 last.json 指向的基线与 dist\AIQuant，生成
        dist\installer\AIQuant-Delta-<base>-to-<target>.exe。
        没有基线/基线可疑 → 打印警告、退出码 0、不生成（发版降级为
        仅完整包，宁可少省 58% 也不装坏用户机器）。

版本交叉校验（防基线污染的三道锁）：
    record: --version 参数 必须 == dist 树 exe 内嵌 APP_VERSION；
    delta:  基线快照 exe 内嵌版本 必须 == last.json.version，
            且 target exe 内嵌版本 必须 != base（忘了 bump 版本号直接拒绝）。
    exe 内嵌版本从 PYZ 里 marshal 反序列化 app_version 代码后 exec 读出，
    不依赖 PyInstaller 的 reader API（格式漂移时自己 40 行也不怕）。

一条铁律：**data\\ config\\ 永不进任何列表**（iss Excludes 之外再加
代码层硬排除——打包机的真实账本在这两个目录里，漏一次就是事故）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import marshal
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 作为脚本直跑时 import app_version 需要

from app_version import parse_version  # noqa: E402

# 与 installer/aqa.iss 的 [Files] Excludes 对齐的代码层防线（三重保险之二）
EXCLUDE_DIRS = ("data", "config")
EXCLUDE_SUFFIX = (".log",)

# 与 aqa.iss 完全一致：同 AppId = Inno 认定同一产品 → 覆盖升级而非并排安装
APP_ID_LINE = "AppId={{7C3F1A62-9D24-4F0B-9A61-2E5B7C1D8A44}"

SNAPSHOTS = ROOT / "dist" / "_snapshots"
STAGE = ROOT / "build" / "delta_stage"
DELTA_ISS = ROOT / "build" / "aqa_delta.iss"
INSTALLER_OUT = ROOT / "dist" / "installer"
KEEP_SNAPSHOTS = 3


# ---------------------------------------------------------------- exe 版本

def read_exe_version(exe_path: Path) -> str:
    """从 PyInstaller 产物 exe 里读出 APP_VERSION。

    路径：CArchive 尾部 cookie → TOC → PYZ.pyz blob → PYZ 头部 toc 偏移
    → marshal TOC → app_version 条目（zlib）→ marshal → exec。

    任何一步解析失败都抛异常，由调用方决定降级——**绝不返回猜测值**。
    """
    data = Path(exe_path).read_bytes()
    cookie_magic = b"MEI\x0c\x0b\x0a\x0b\x0e"
    i = data.rfind(cookie_magic)
    if i < 0:
        raise ValueError("exe 尾部没有 PyInstaller cookie（不是打包产物？）")
    # cookie: magic(8) + pkg_len(4) + toc_off(4) + toc_len(4) + pyvers(4) + pylib(64)
    pkg_len, toc_off, toc_len = struct.unpack("!III", data[i + 8 : i + 20])
    pkg_start = len(data) - pkg_len
    entries = _read_carchive_toc(data, pkg_start + toc_off, toc_len)
    pyz_entry = next((e for e in entries if e[0] in ("PYZ.pyz", "PYZ")), None)
    if pyz_entry is None:
        raise ValueError("CArchive TOC 里没有 PYZ 条目")
    _, pos, length = pyz_entry
    pyz = data[pkg_start + pos : pkg_start + pos + length]
    if pyz[:4] != b"PYZ\x00":
        raise ValueError("PYZ 魔数不符")
    (pyz_toc_off,) = struct.unpack("!i", pyz[8:12])
    toc = marshal.loads(pyz[pyz_toc_off:])
    # PYZ TOC 是 (name, (typecode, offset, length)) 列表，条目为 zlib(marshal(code))
    ns: dict = {}
    for name, (typecode, off, ln) in toc:
        if name == "app_version":
            code = marshal.loads(zlib.decompress(pyz[off : off + ln]))
            exec(code, ns)  # noqa: S102  构建机上执行自家模块，可信
            break
    else:
        raise ValueError("PYZ 里没有 app_version 模块")
    version = ns.get("APP_VERSION")
    if not isinstance(version, str) or not version:
        raise ValueError("app_version.APP_VERSION 读出来是空/非字符串")
    return version


def _read_carchive_toc(data: bytes, toc_abs: int, toc_len: int) -> list:
    """解析 CArchive TOC → [(name, pos, length), ...]。

    条目是紧密排列的定长头 + 变长名（名字 \\0 填充），
    布局以 PyInstaller readers.py 的 _parse_toc 为准：
        entry_length(4) offset(4) length(4) uncompressed_length(4)
        compression_flag(1) typecode(1) name[entry_length-18]
    头长是 struct.calcsize('!IIIIBc') = 18（网络字节序无对齐填充，
    别按 4*4+1+1 之外的任何方式口算）。
    """
    out = []
    off = toc_abs
    end = toc_abs + toc_len
    while off < end:
        entry_length, pos, length = struct.unpack("!III", data[off : off + 12])
        name_len = entry_length - 18
        name = data[off + 18 : off + 18 + name_len].rstrip(b"\0").decode("utf-8")
        out.append((name, pos, length))
        off += entry_length
    return out


# ---------------------------------------------------------------- 文件树

def _excluded(rel: str) -> bool:
    """相对路径是否属于绝不进包的东西。代码层硬排除，不依赖调用方自觉。"""
    parts = rel.split("/")
    if parts[0] in EXCLUDE_DIRS:
        return True
    return any(parts[-1].endswith(s) for s in EXCLUDE_SUFFIX)


def walk_tree(root: Path) -> dict[str, str]:
    """目录树 → {相对路径(正斜杠): sha256}。root 不存在返回空表。"""
    root = Path(root)
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if _excluded(rel):
                continue
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def diff_trees(base: dict[str, str], target: dict[str, str]) -> tuple[list, list, list, list]:
    """两份 manifest → (changed, added, removed, unchanged)，各按路径排序。"""
    changed = sorted(k for k in target if k in base and target[k] != base[k])
    added = sorted(k for k in target if k not in base)
    removed = sorted(k for k in base if k not in target)
    unchanged = sorted(k for k in target if k in base and target[k] == base[k])
    return changed, added, removed, unchanged


# ---------------------------------------------------------------- record

def cmd_record(args: argparse.Namespace) -> int:
    dist = (ROOT / args.dist).resolve() if not Path(args.dist).is_absolute() else Path(args.dist)
    exe = dist / "AIQuant.exe"
    if not exe.is_file():
        print(f"[delta] [ERROR] dist tree has no AIQuant.exe: {exe}", file=sys.stderr)
        return 1
    embedded = read_exe_version(exe)
    if embedded != args.version:
        print(
            f"[delta] [ERROR] version mismatch: app_version.py says {args.version}, "
            f"but the built exe embeds {embedded}. dist\\AIQuant is not a build of "
            f"this version - refusing to snapshot it as a release baseline.",
            file=sys.stderr,
        )
        return 1

    snap_dir = SNAPSHOTS / args.version
    if snap_dir.exists():
        shutil.rmtree(snap_dir)  # 重发同版本：快照必须反映最后一次发布
    shutil.copytree(
        dist,
        snap_dir,
        ignore=shutil.ignore_patterns("data", "config", "*.log"),
    )
    manifest = walk_tree(snap_dir)
    (SNAPSHOTS / f"{args.version}.manifest.json").write_text(
        json.dumps({"version": args.version, "files": manifest}, indent=1),
        encoding="utf-8",
    )
    (SNAPSHOTS / "last.json").write_text(
        json.dumps(
            {"version": args.version, "path": str(snap_dir), "recorded_at": _now()},
            indent=1,
        ),
        encoding="utf-8",
    )
    _prune_snapshots()
    n = len(manifest)
    print(f"[delta] snapshot recorded: v{args.version} ({n} files) -> {snap_dir}")
    return 0


def _now() -> str:
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")


def _prune_snapshots() -> None:
    """只保留最近 KEEP_SNAPSHOTS 份快照（按版本号排序，不是字典序——
    1.10.0 字典序 < 1.9.0，版本段进两位时字典序会骗人）。"""
    if not SNAPSHOTS.is_dir():
        return
    snaps = []
    for d in SNAPSHOTS.iterdir():
        if d.is_dir() and parse_version(d.name):
            snaps.append(d)
    snaps.sort(key=lambda d: parse_version(d.name))
    for d in snaps[: -KEEP_SNAPSHOTS]:
        shutil.rmtree(d, ignore_errors=True)
        (SNAPSHOTS / f"{d.name}.manifest.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- delta

def _load_base() -> tuple[str, Path] | None:
    """last.json → (版本, 快照路径)。没有/损坏返回 None（不生成 delta）。"""
    if not (SNAPSHOTS / "last.json").is_file():
        return None
    try:
        meta = json.loads((SNAPSHOTS / "last.json").read_text(encoding="utf-8"))
        ver, path = str(meta["version"]), Path(meta["path"])
        if ver and path.is_dir():
            return ver, path
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[delta] [WARN] last.json unreadable: {exc}")
    return None


def _find_iscc() -> Path | None:
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base and (Path(base) / "Inno Setup 6" / "ISCC.exe").is_file():
            return Path(base) / "Inno Setup 6" / "ISCC.exe"
    return None


def cmd_delta(args: argparse.Namespace) -> int:
    target = (ROOT / args.target).resolve() if not Path(args.target).is_absolute() else Path(args.target)
    target_exe = target / "AIQuant.exe"
    if not target_exe.is_file():
        print(f"[delta] [ERROR] target tree has no AIQuant.exe: {target_exe}", file=sys.stderr)
        return 1

    base_info = _load_base()
    if base_info is None:
        print(
            "[delta] [WARN] no baseline snapshot (dist/_snapshots/last.json missing "
            "or broken). Skipping delta - this release ships full installer only. "
            "Normal on the first build after this feature lands.",
        )
        return 0
    base_ver, base_dir = base_info

    # 三道锁之二：基线快照的 exe 内嵌版本必须与 last.json 声称的一致。
    # record 中途失败/手工动过快照目录都会在这里被拦下——错了宁可不做。
    try:
        base_embedded = read_exe_version(base_dir / "AIQuant.exe")
        target_ver = read_exe_version(target_exe)
    except Exception as exc:  # noqa: BLE001
        print(f"[delta] [WARN] cannot read embedded version ({exc}). Skipping delta.")
        return 0
    if base_embedded != base_ver:
        print(
            f"[delta] [WARN] baseline snapshot claims v{base_ver} but its exe embeds "
            f"v{base_embedded}. Snapshot is not trustworthy - skipping delta.",
        )
        return 0
    if target_ver == base_ver:
        print(
            f"[delta] [WARN] target embeds v{target_ver} == baseline. Did you forget "
            f"to bump app_version.py? Skipping delta.",
        )
        return 0

    base_files = walk_tree(base_dir)
    target_files = walk_tree(target)
    changed, added, removed, unchanged = diff_trees(base_files, target_files)
    payload = changed + added
    if not payload and not removed:
        print("[delta] [WARN] trees are identical. Skipping delta.")
        return 0

    _stage_payload(target, payload)
    iss_path = _write_iss(base_ver, target_ver, payload, unchanged, removed)
    iscc = _find_iscc()
    if iscc is None:
        print("[delta] [ERROR] ISCC.exe not found (Inno Setup 6). Full installer "
              "was built; delta skipped.", file=sys.stderr)
        return 1
    proc = subprocess.run([str(iscc), "/Q", str(iss_path)])
    if proc.returncode != 0:
        print(f"[delta] [ERROR] ISCC failed with code {proc.returncode}. "
              "Full installer is unaffected.", file=sys.stderr)
        return 1

    out = INSTALLER_OUT / f"AIQuant-Delta-{base_ver}-to-{target_ver}.exe"
    if not out.is_file():
        print(f"[delta] [ERROR] ISCC reported success but {out} is missing.", file=sys.stderr)
        return 1
    full = INSTALLER_OUT / f"AIQuant-Setup-{target_ver}.exe"
    full_size = full.stat().st_size if full.is_file() else 0
    delta_size = out.stat().st_size
    ratio = (delta_size / full_size) if full_size else 0
    print(
        f"[delta] OK: {out.name}\n"
        f"        changed={len(changed)} added={len(added)} removed={len(removed)} "
        f"external={len(unchanged)}\n"
        f"        delta {delta_size:,} B vs full {full_size:,} B "
        f"({ratio:.0%} of full)"
    )
    if full_size and ratio >= 0.95:
        print("[delta] [WARN] delta is barely smaller than full - check what changed.")
    return 0


def _stage_payload(target: Path, payload: list[str]) -> None:
    """变更+新增文件拷入暂存区（iss 的真实 Source）。build\\ 每次构建前已被
    build_installer.bat 清空，这里再兜底清一次。"""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    for rel in payload:
        src = target / rel
        dst = STAGE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _dest_dir(rel: str) -> str:
    head = rel.rsplit("/", 1)[0].replace("/", "\\") if "/" in rel else ""
    return "{app}" if not head else "{app}\\" + head


def _write_iss(
    base_ver: str,
    target_ver: str,
    payload: list[str],
    unchanged: list[str],
    removed: list[str],
) -> Path:
    """生成 delta 包 iss。**全文 ASCII + CRLF**——Inno 对无 BOM 的 UTF-8
    按本地代码页读，中文注释会乱码；反正是机器生成的，一个汉字都不放。"""
    stage_str = str(STAGE).replace("\\", "\\\\")
    L: list[str] = []
    A = L.append
    A("; AI Quant Agent delta installer - GENERATED by tools/make_delta.py, do not edit.")
    A(f"; base v{base_ver} -> target v{target_ver}")
    A("#define MyAppName \"AI Quant Agent\"")
    A(f"#define MyAppVersion \"{target_ver}\"")
    A(f"#define Stage \"{stage_str}\"")
    A("[Setup]")
    A(APP_ID_LINE)  # 与完整包同 AppId：覆盖升级、更新 unins000 与注册表版本号
    A("AppName={#MyAppName}")
    A("AppVersion={#MyAppVersion}")
    A("AppVerName={#MyAppName} {#MyAppVersion}")
    A("DefaultDirName={localappdata}\\Programs\\AIQuant")
    A("DisableProgramGroupPage=yes")
    A("DisableDirPage=no")
    A("PrivilegesRequired=lowest")
    A("ArchitecturesInstallIn64BitMode=x64compatible")
    A("ArchitecturesAllowed=x64compatible")
    A("MinVersion=10.0")
    A(f"OutputDir={str(INSTALLER_OUT).replace(chr(92), chr(92) + chr(92))}")
    A(f"OutputBaseFilename=AIQuant-Delta-{base_ver}-to-{target_ver}")
    A("UninstallDisplayIcon={app}\\AIQuant.exe")
    A("UninstallDisplayName={#MyAppName}")
    A("Compression=lzma2/max")
    A("SolidCompression=yes")
    A("WizardStyle=modern")
    A("CloseApplications=yes")
    A("RestartApplications=no")
    A("AppMutex=Global\\AIQuantDashboard")
    A("")
    A("[Languages]")
    A("Name: \"english\"; MessagesFile: \"compiler:Default.isl\"")
    A("")
    A("[Dirs]")
    # uninsneveruninstall：卸载时账本与配置即使空了也留着（与完整包同语义）
    A("Name: \"{app}\\data\";   Permissions: users-modify; Flags: uninsneveruninstall")
    A("Name: \"{app}\\config\"; Permissions: users-modify; Flags: uninsneveruninstall")
    A("")
    if removed:
        A("[InstallDelete]")
        for rel in removed:
            A(f"Type: files; Name: \"{{app}}\\{rel.replace('/', chr(92))}\"")
        A("")
    A("[Files]")
    for rel in payload:  # 变更/新增：真实内容进包
        A(f"Source: \"{{#Stage}}\\{rel.replace('/', chr(92))}\"; "
          f"DestDir: \"{_dest_dir(rel)}\"; Flags: ignoreversion")
    for rel in unchanged:  # 未变化：external 引用已安装文件，零体积但进卸载日志
        A(f"Source: \"{{app}}\\{rel.replace('/', chr(92))}\"; "
          f"DestDir: \"{_dest_dir(rel)}\"; Flags: external ignoreversion")
    A("")
    A("[Icons]")
    A("Name: \"{autoprograms}\\{#MyAppName}\"; Filename: \"{app}\\AIQuant.exe\"")
    A("")
    A("[UninstallDelete]")
    A("Type: filesandordirs; Name: \"{app}\\data\\webview\"")
    A("")
    A("[Run]")
    # 不带 skipifsilent：应用内 /SILEENT 升级装完必须由 Setup 拉起新版
    A("Filename: \"{app}\\AIQuant.exe\"; Flags: nowait postinstall")
    DELTA_ISS.parent.mkdir(parents=True, exist_ok=True)
    DELTA_ISS.write_text("\r\n".join(L) + "\r\n", encoding="ascii")
    return DELTA_ISS


# ---------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="delta installer generator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_rec = sub.add_parser("record", help="snapshot dist\\AIQuant as release baseline")
    p_rec.add_argument("--dist", default="dist/AIQuant")
    p_rec.add_argument("--version", required=True)
    p_del = sub.add_parser("delta", help="build AIQuant-Delta-<base>-to-<target>.exe")
    p_del.add_argument("--target", default="dist/AIQuant")
    args = parser.parse_args(argv)
    if args.cmd == "record":
        return cmd_record(args)
    return cmd_delta(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001  构建链上的最后防线：给一句人话
        print(f"[delta] [ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
