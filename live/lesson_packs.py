"""经验库（教训库）的包管理。

教训库从「写死在每个账户目录下的一个文件」改成「一组可命名的包」：
面板上像 MT5 选 EA 那样选一个生效，能导入别人的、能导出自己的。

存储都在 `data/live/lesson_packs/`（走 `paths.LIVE_DIR`，跟着 `set_root` 走，
测试时重定向到临时目录）。**不走 `appdirs.data_dir()` 的硬编码**——否则
`tests/_stubs.py` 的 `paths.set_root(tmpdir)` 不会把它也带过去。

autotrade 只通过这里的 `active_file()` 拿路径（`lessons_path()` 唯一出口）。
**没有缓存**——每次决策和每秒轮询都完整读盘，所以切换包即刻生效。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from live import paths


def packs_dir() -> Path:
    """所有包的根目录。跟着 paths.LIVE_DIR 走（set_root 时一起重定向）。"""
    return paths.LIVE_DIR / "lesson_packs"


def import_dir() -> Path:
    """用户把 .jsonl 丢到这里，面板点「扫描」就能列出。"""
    return packs_dir() / "import"


def export_dir() -> Path:
    """导出暂存。Blob 下载在 WebView2 里被禁，所以导出只能后端写文件 +
    前端显示绝对路径让用户去复制。"""
    return packs_dir() / "export"


def index_path() -> Path:
    return packs_dir() / "index.json"


# ---- index 读写 ---------------------------------------------------------

def _read_index() -> dict:
    p = index_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "packs" in data:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"active": None, "packs": []}


def _write_index(idx: dict) -> None:
    packs_dir().mkdir(parents=True, exist_ok=True)
    index_path().write_text(
        json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 查询 ----------------------------------------------------------------

def active_id() -> str | None:
    return _read_index()["active"]


def active_file() -> Path:
    """`autotrade.lessons_path()` 唯一调用这里。

    没有激活包时返回一个**不存在**的路径 `_scratch_.jsonl`：
    `lesson_rows()` 看到不存在会返回 `[]`（注入提示词时就当没经验）；
    `append_lesson()` 写时会自动创建它——教训不会丢，会在面板上显示成
    「（草稿）」让用户去命名归档。这是兜底，正常流程下 `ensure_first_run`
    会保证总有 active 包。
    """
    idx = _read_index()
    for p in idx["packs"]:
        if p["id"] == idx["active"]:
            return packs_dir() / p["file"]
    return packs_dir() / "_scratch_.jsonl"


def _count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    n = 0
    try:
        for _ in p.open(encoding="utf-8"):
            n += 1
    except OSError:
        pass
    return n


def list_packs() -> list[dict]:
    """每个包附**实时条数**（index 里的 count 是写入时的快照，会漂）。"""
    idx = _read_index()
    out = []
    # 草稿文件：active 为空时教训会写到 _scratch_.jsonl，在这里露出来
    scratch = packs_dir() / "_scratch_.jsonl"
    if scratch.exists() and _count_lines(scratch) > 0:
        out.append({
            "id": "_scratch_", "name": "（草稿，未命名）",
            "file": "_scratch_.jsonl", "count": _count_lines(scratch),
            "created_at": int(scratch.stat().st_mtime), "source": "scratch",
        })
    for p in idx["packs"]:
        out.append({**p, "count": _count_lines(packs_dir() / p["file"])})
    return out


# ---- 增删改查 ------------------------------------------------------------

def _next_id(idx: dict) -> str:
    today = time.strftime("%Y%m%d")
    prefix = f"p-{today}"
    existing = {p["id"] for p in idx["packs"]}
    n = sum(1 for p in existing if p.startswith(prefix))
    # a, b, c... 超过 26 个就用数字
    suffix = "abcdefghijklmnopqrstuvwxyz"[n] if n < 26 else str(n)
    pid = f"{prefix}{suffix}"
    while pid in existing:   # 极小概率撞，往后挪
        n += 1
        suffix = "abcdefghijklmnopqrstuvwxyz"[n] if n < 26 else str(n)
        pid = f"{prefix}{suffix}"
    return pid


def select(id: str) -> dict:
    idx = _read_index()
    if not any(p["id"] == id for p in idx["packs"]):
        return {"ok": False, "error": "包不存在"}
    idx["active"] = id
    _write_index(idx)
    return {"ok": True, "active": id}


def create_empty(name: str) -> dict:
    """新建一个空包并设为 active。用于「从零积累」。"""
    idx = _read_index()
    pid = _next_id(idx)
    fname = f"{pid}.jsonl"
    f = packs_dir() / fname
    packs_dir().mkdir(parents=True, exist_ok=True)
    f.touch()
    idx["packs"].append({
        "id": pid, "name": (name or "新库")[:40], "file": fname,
        "count": 0, "created_at": int(time.time()), "source": "manual",
    })
    idx["active"] = pid
    _write_index(idx)
    return {"ok": True, "id": pid}


def import_from(src_path: Path, name: str) -> dict:
    """从指定 .jsonl 文件导入成新包。

    复用 `autotrade._unwrap_lesson` 拆历史遗留的 `{"answer":...}` 包裹，
    dedupe 用 `(ts, ticket, lesson)` 三元组（和 `migrate_lessons` 一致）。
    """
    from live import autotrade

    rows = []
    seen: set = set()
    bad = 0
    try:
        fh = src_path.open(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"打不开文件：{exc}"}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(r, dict):
                bad += 1
                continue
            r["lesson"] = autotrade._unwrap_lesson(r.get("lesson") or "")
            key = (r.get("ts"), r.get("ticket"), r["lesson"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    rows.sort(key=lambda r: r.get("ts") or 0)

    idx = _read_index()
    pid = _next_id(idx)
    fname = f"{pid}.jsonl"
    dst = packs_dir() / fname
    packs_dir().mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8") as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    idx["packs"].append({
        "id": pid, "name": (name or src_path.stem)[:40], "file": fname,
        "count": len(rows), "created_at": int(time.time()),
        "source": f"import:{src_path.name}",
    })
    idx["active"] = pid
    _write_index(idx)
    return {"ok": True, "id": pid, "imported": len(rows),
            "skipped_bad": bad, "skipped_dup": 0}


def export_to(id: str) -> dict:
    """把指定包复制到 export/，返回绝对路径。

    Blob 下载在 WebView2 里被 pywebview 禁掉（`ALLOW_DOWNLOADS=False`），
    所以导出只能后端写文件 + 前端显示路径让用户用文件管理器去复制。
    """
    idx = _read_index()
    p = next((x for x in idx["packs"] if x["id"] == id), None)
    if not p:
        return {"ok": False, "error": "包不存在"}
    src = packs_dir() / p["file"]
    if not src.exists():
        return {"ok": False, "error": "包文件丢失"}
    export_dir().mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", p["name"])[:40] or "lesson_pack"
    dst = export_dir() / f"{safe}.jsonl"
    # 同名已存在就覆盖——导出是给别人/别处用的，不留旧版
    shutil.copy2(src, dst)
    return {"ok": True, "path": str(dst), "name": dst.name,
            "count": _count_lines(src)}


def rename(id: str, name: str) -> dict:
    idx = _read_index()
    for p in idx["packs"]:
        if p["id"] == id:
            p["name"] = (name or p["name"])[:40]
            _write_index(idx)
            return {"ok": True}
    return {"ok": False, "error": "包不存在"}


def delete(id: str) -> dict:
    """软删除：文件改名到 _trash/，从 index 移除。active 被删则切到第一个。

    不真删——经验是用户辛苦积累的，删错了没地方恢复。
    """
    idx = _read_index()
    p = next((x for x in idx["packs"] if x["id"] == id), None)
    if not p:
        return {"ok": False, "error": "包不存在"}
    src = packs_dir() / p["file"]
    if src.exists():
        trash = packs_dir() / "_trash"
        trash.mkdir(exist_ok=True)
        # 加时间戳避免多次删同名覆盖
        shutil.move(str(src), str(trash / f"{p['file']}.{int(time.time())}"))
    idx["packs"] = [x for x in idx["packs"] if x["id"] != id]
    if idx["active"] == id:
        idx["active"] = idx["packs"][0]["id"] if idx["packs"] else None
    _write_index(idx)
    return {"ok": True, "active": idx["active"]}


def scan_import() -> list[dict]:
    """扫 import/ 目录，列出 .jsonl 文件 + 行数。

    不解析内容（解析在 import_from 时做）——扫描要快，文件可能很大。
    """
    out = []
    d = import_dir()
    if not d.exists():
        return out
    for f in sorted(d.glob("*.jsonl")):
        out.append({
            "name": f.name, "count": _count_lines(f),
            "size": f.stat().st_size if f.exists() else 0,
        })
    return out


# ---- 启动迁移 ------------------------------------------------------------

def ensure_first_run() -> None:
    """启动时调一次。index 为空时，扫所有账户目录的 lessons.jsonl，
    每个非空的提取成一个包。**原文件保留不动**。

    幂等：index 已有包就不动，不会重复提取。
    全新安装（data 是空的）：扫不到任何 lessons.jsonl，会建一个空包
    「默认」，保证总有 active——否则 append_lesson 会写到 _scratch_ 里
    让用户找不到。
    """
    idx = _read_index()
    if idx["packs"]:
        return   # 已有包，不重复提取

    packs_dir().mkdir(parents=True, exist_ok=True)

    # 扫所有账户目录的 lessons.jsonl
    accounts = paths.ACCOUNTS_DIR
    if accounts.exists():
        for d in sorted(accounts.iterdir()):
            if not d.is_dir():
                continue
            src = d / "lessons.jsonl"
            if not src.exists() or src.stat().st_size == 0:
                continue
            n = _count_lines(src)
            if n == 0:
                continue
            login = d.name
            pid = _next_id(idx)
            fname = f"{pid}.jsonl"
            dst = packs_dir() / fname
            shutil.copy2(src, dst)
            idx["packs"].append({
                "id": pid, "name": f"原账户 {login} 经验", "file": fname,
                "count": n, "created_at": int(time.time()),
                "source": f"auto-extract:{login}",
            })
            if idx["active"] is None:
                idx["active"] = pid

    # 一个包都没有（全新安装）：建一个空「默认」，让 append_lesson 有地方写
    if not idx["packs"]:
        pid = _next_id(idx)
        fname = f"{pid}.jsonl"
        (packs_dir() / fname).touch()
        idx["packs"].append({
            "id": pid, "name": "默认", "file": fname,
            "count": 0, "created_at": int(time.time()), "source": "default",
        })
        idx["active"] = pid

    _write_index(idx)


def absorb_scratch() -> dict:
    """把 _scratch_.jsonl（兜底写入的）收编成一个正式包。

    正常流程不会触发——`ensure_first_run` 保证总有 active 包。
    但如果用户从老版本升级、active 曾为 None，教训可能写到了 scratch。
    这条路把它们救回来。
    """
    scratch = packs_dir() / "_scratch_.jsonl"
    if not scratch.exists() or _count_lines(scratch) == 0:
        return {"ok": True, "absorbed": 0}
    idx = _read_index()
    pid = _next_id(idx)
    fname = f"{pid}.jsonl"
    dst = packs_dir() / fname
    shutil.move(str(scratch), str(dst))
    n = _count_lines(dst)
    idx["packs"].append({
        "id": pid, "name": "草稿归档", "file": fname,
        "count": n, "created_at": int(time.time()), "source": "scratch",
    })
    if idx["active"] is None:
        idx["active"] = pid
    _write_index(idx)
    return {"ok": True, "absorbed": n, "id": pid}
