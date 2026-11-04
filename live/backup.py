"""账户账本的日切快照轮转。

为什么需要
----------
`data/live/accounts/<login>/` 下的账本是**唯一副本**：MT5 历史有保留期、换服务器就丢，
`deals.jsonl` 一旦写坏没有任何地方能恢复。

备份防的主要不是硬盘坏，是**程序自己写坏**：
  - `paths.write_json` 用 tmp + os.replace 保证原子性，但如果写进去的内容本身是错的
    （某个字段被 `or` 兜底吞成 0），旧的好数据当场消失，且没有任何历史版本可回退。
  - `deals.jsonl` 是 append-only，错误成交一旦写进去就永久留在账本里。
  - `autotrade_state.json` 每 60 秒覆盖一次，累计字段偏了就没有基线能对齐。

所以这里做的是**日切快照 + 滚动淘汰**，不是"手动备份一次"：保留最近 14 天，
第 15 天删掉最旧那份。窗口固定，不需要人工管理，也不会无限占盘。

设计取舍
--------
- **全量，不增量**：单账户约 2.5 MB，14 份约 35 MB，省这点空间不值得增加恢复复杂度。
- **账户级 zip + 一份 _global**：恢复粒度到单账户，不必因为看一个老账户就把全部数据覆盖回去。
- **不碰 `config/`**：`.env` 里是 LLM API key，复制到备份目录等于扩散密钥。
- **不加锁、不进交易路径**：读的是文件，`write_json` 的原子替换保证读到的不是半截 JSON。
  已知限制见 snapshot() 的注释。
- 任何失败只打日志，绝不抛给调用方——备份失败不能挡住面板启动，更不能影响做单。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

from live import appdirs, paths

RETENTION_DAYS = 14
# 单账户目录超过这个体积就跳过。正常只有几 MB，真涨到 512 MB 说明有东西在失控，
# 这时候继续打包只会把磁盘打满——宁可告警，也不要让保护机制变成故障源。
MAX_ACCOUNT_BYTES = 512 * 1024 * 1024

# 账户目录名必须全是数字。accounts/ 下还可能有 smoke/（测试）、__pycache__/ 之类，
# 靠这条规则天然排除，不必维护黑名单。
_LOGIN_RE = re.compile(r"^\d+$")
# 只允许按这个名字模式清理：任何手工放进去的文件都不该被 prune 误删。
_ZIP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.zip$")
GLOBAL = "_global"

# 测试隔离口：set_base(tmp) 之后 live 根与 backups 根都指到 tmp 下，不碰真实账本。
_BASE: Path | None = None


def set_base(root: Path | str | None) -> None:
    """**只有测试该用**。传 None 恢复默认。"""
    global _BASE
    _BASE = Path(root) if root is not None else None


def _live() -> Path:
    return _BASE / "live" if _BASE else paths.LIVE_DIR


def backups_dir() -> Path:
    return _BASE / "backups" if _BASE else appdirs.data_dir() / "backups"


def account_backups(login: int | str) -> Path:
    return backups_dir() / str(login)


def _account_src(login: int | str) -> Path:
    return _live() / "accounts" / str(login)


def _log(result: dict) -> None:
    """一行一条结果，方便事后查"为什么那天没备份"。写失败也吞掉。"""
    try:
        root = _BASE / "logs" if _BASE else appdirs.logs_dir()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "backup.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _members(src: Path) -> list[tuple[Path, str]]:
    """列出要进 zip 的 (绝对路径, 归档内路径)。

    跳过 `*.tmp`：`write_json` 的临时文件不是数据，把半截 tmp 备进去只会误导恢复。
    `_trash` 是软删回收站，体积可能很大且价值低，不进备份。
    """
    out: list[tuple[Path, str]] = []
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in ("_trash", "__pycache__") for part in rel.parts):
            continue
        if not p.is_file() or p.suffix == ".tmp":
            continue
        out.append((p, rel.as_posix()))
    return out


def _dir_bytes(src: Path) -> int:
    return sum(p.stat().st_size for p, _ in _members(src))


def _write_zip(members: list[tuple[Path, str]], dest: Path) -> dict:
    """先写 .zip.tmp 再 replace：崩溃留下的是 .tmp，不会被认成有效备份。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arc in members:
            try:
                zf.write(path, arc)
            except FileNotFoundError:
                # 打包过程中文件被别的线程删了（比如归档日志被清理），跳过即可。
                continue
    os.replace(tmp, dest)
    return {"file": str(dest), "bytes": dest.stat().st_size, "files": len(members)}


def snapshot(login: int | str, day: str | None = None, force: bool = False) -> dict:
    """给一个账户（或 _global）打一份当日快照。

    已知限制：打包期间 jsonl 仍在被追加，最后一行可能读到半行。
    影响是恢复后那一条 deal 缺失，下次 `journal.sync` 会从 MT5 重新拉回来，
    所以容忍，不值得为此停掉账本写入。
    """
    day = day or time.strftime("%Y-%m-%d")
    key = str(login)
    dest = account_backups(key) / f"{day}.zip"
    result: dict = {"login": key, "day": day, "ts": int(time.time())}
    if dest.exists() and dest.stat().st_size > 0 and not force:
        result.update(ok=True, skipped="exists", file=str(dest))
        return result

    try:
        if key == GLOBAL:
            members: list[tuple[Path, str]] = []
            for name in ("index.json", "profile.json"):
                p = _live() / name
                if p.is_file():
                    members.append((p, name))
            packs = _live() / "lesson_packs"
            if packs.is_dir():
                members.extend((p, f"lesson_packs/{arc}") for p, arc in _members(packs))
            if not members:
                result.update(ok=True, skipped="empty")
                return result
        else:
            src = _account_src(key)
            if not src.is_dir():
                result.update(ok=True, skipped="no_account")
                return result
            size = _dir_bytes(src)
            if size > MAX_ACCOUNT_BYTES:
                result.update(ok=False, skipped="too_large", bytes=size)
                _log(result)
                return result
            members = _members(src)

        result.update(ok=True, **_write_zip(members, dest))
    except (OSError, zipfile.BadZipFile) as exc:
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    _log(result)
    return result


def real_logins() -> list[str]:
    """需要备份的账户：accounts/ 下纯数字名的目录。"""
    acc = _live() / "accounts"
    if not acc.is_dir():
        return []
    return sorted(p.name for p in acc.iterdir() if p.is_dir() and _LOGIN_RE.match(p.name))


def prune(login: int | str, keep: int = RETENTION_DAYS) -> list[Path]:
    """只保留最近 keep 份。按文件名排序——日期字符串天然可比，不依赖 mtime。

    mtime 不可信：手工拷贝、解压、同步盘同步都会改它。
    """
    d = account_backups(login)
    if not d.is_dir():
        return []
    zips = sorted((p for p in d.iterdir() if p.is_file() and _ZIP_RE.match(p.name)),
                  key=lambda p: p.name, reverse=True)
    removed: list[Path] = []
    for p in zips[max(0, keep):]:
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass
    return removed


def ensure_today(login: int | str, day: str | None = None) -> dict:
    """幂等入口：今天没备份就做，然后淘汰超期的。"""
    r = snapshot(login, day=day)
    r["pruned"] = [str(p) for p in prune(login)]
    return r


def ensure_all_today(day: str | None = None) -> list[dict]:
    out = [ensure_today(login, day=day) for login in real_logins()]
    out.append(ensure_today(GLOBAL, day=day))
    return out


def status(login: int | str | None = None) -> dict:
    """给 CLI / 将来面板用。只读，不触发打包。"""
    keys = [str(login)] if login is not None else real_logins() + [GLOBAL]
    out = {}
    for k in keys:
        d = account_backups(k)
        zips = sorted((p for p in d.iterdir() if p.is_file() and _ZIP_RE.match(p.name)),
                      key=lambda p: p.name) if d.is_dir() else []
        out[k] = {
            "count": len(zips),
            "latest": zips[-1].name[:-4] if zips else None,
            "bytes": sum(p.stat().st_size for p in zips),
            "dir": str(d),
        }
    return out


def _safe_extract(z: Path, dst: Path) -> None:
    """解压前校验成员名。备份文件不该是外部输入，但这是廉价的一道防线：
    任何绝对路径或 `..` 都会让解压写到目标目录之外。
    """
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if info.filename.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(f"非法归档成员: {info.filename}")
        zf.extractall(dst)


def restore(login: int | str, day: str, yes: bool = False) -> dict:
    """恢复某个账户到指定日期。

    当前目录**不删除**，移到 `_trash/restore-<login>-<时间戳>/`（沿用 delete_account
    的软删约定）。解压是覆盖式的，如果只解压不移走，新产生而备份里没有的文件会留下，
    造成"恢复了一半"的假象。
    """
    if not yes:
        return {"ok": False, "error": "恢复会替换当前账本，请带 yes=True 显式确认"}
    z = account_backups(login) / f"{day}.zip"
    if not z.is_file():
        return {"ok": False, "error": f"没有这份备份: {z}"}
    dst = _live() if str(login) == GLOBAL else _account_src(login)
    try:
        moved = None
        if str(login) != GLOBAL and dst.exists():
            trash = _live() / paths.TRASH_DIR_NAME
            trash.mkdir(parents=True, exist_ok=True)
            moved = trash / f"restore-{login}-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(dst), str(moved))
        dst.mkdir(parents=True, exist_ok=True)
        _safe_extract(z, dst)
        return {"ok": True, "login": str(login), "day": day,
                "restored_to": str(dst), "previous_moved_to": str(moved) if moved else None}
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def start_daily(interval_sec: int = 1800) -> None:
    """启动即备份一次，之后每 interval_sec 检查一次。

    面板可能连续开好几天不重启，只在 main() 里调一次的话跨天就不会有新快照。
    每次检查的成本是每账户一次 `exists()`，可以忽略。
    daemon=True：主进程退出时不需要等它，正在写的 tmp 会留下，下次启动重来。
    """
    def loop() -> None:
        while True:
            try:
                ensure_all_today()
            except Exception as exc:  # noqa: BLE001
                # 任何未知异常都不能终止这个线程——那等于备份悄悄停了。
                _log({"ok": False, "error": f"{type(exc).__name__}: {exc}", "where": "loop"})
            time.sleep(interval_sec)

    threading.Thread(target=loop, name="backup-daily", daemon=True).start()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="账本备份轮转")
    ap.add_argument("--status", action="store_true", help="查看备份状态")
    ap.add_argument("--now", action="store_true", help="立即给所有账户备份一次")
    ap.add_argument("--restore", metavar="DATE", help="恢复指定日期，如 2026-11-04")
    ap.add_argument("--login", required=False, help="账户号，或 _global")
    ap.add_argument("--yes", action="store_true", help="恢复必须显式确认")
    args = ap.parse_args()

    if args.restore:
        if not args.login:
            ap.error("--restore 需要同时给 --login")
        print(json.dumps(restore(args.login, args.restore, yes=args.yes),
                         ensure_ascii=False, indent=2))
    elif args.now:
        print(json.dumps(ensure_all_today(), ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(status(args.login), ensure_ascii=False, indent=2))
