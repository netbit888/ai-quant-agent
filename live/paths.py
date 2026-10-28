"""多账户路径与索引。

为什么是「自动识别」而不是「程序切换」
--------------------------------------
MT5 的 Python 接口（`mt5.login`）需要账号的**密码**才能切换登录。
把这个密码存在本地明文里是严重的安全风险，也不该由这个工具承担。

所以设计成：**你在 MT5 终端里切换账户，这里自动识别并把数据归档到对应目录。**
每个账户一个独立账本，互不污染；面板可以回看任意一个历史账户的数据，
但只有终端当前登录的那个能实时同步。

目录结构
--------
    data/live/
      index.json                 账户索引（login → 服务器/币种/首次与最后出现时间）
      profile.json               品种画像（全局，不分账户）
      accounts/
        <login>/                 每个账户一套完整账本
          deals.jsonl
          daily.json  account.json  account_daily.json
          quality_daily.json  advice.json  api_calls.jsonl
          position_state.json  position_snapshots.jsonl
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from live import appdirs

# 可写数据根。源码态 = 仓库根；绿化版 = exe 同级。
# **不要改回 Path(__file__).parent.parent**：冻结后那是 _MEIPASS（临时解压目录），
# 账本会写进去并随进程退出被删——这是唯一副本，丢了不可恢复。
ROOT = appdirs.app_root()
LIVE_DIR = appdirs.data_dir() / "live"
ACCOUNTS_DIR = LIVE_DIR / "accounts"
INDEX_FILE = LIVE_DIR / "index.json"

# 删除账户一律先进这里，不真删。真实账户账本是**唯一副本**，MT5 的历史
# 有保留期、换服务器就丢，误删之后没有任何地方能恢复。
TRASH_DIR_NAME = "_trash"


def account_dir(login: int | str) -> Path:
    return ACCOUNTS_DIR / str(login)


def ensure_account_dir(login: int | str) -> Path:
    d = account_dir(login)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_index(index: dict) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def touch_account(acct: dict) -> dict:
    """记录/更新账户元信息。每次同步都会调用。"""
    login = str(acct.get("login"))
    if not login or login == "None":
        return load_index()

    index = load_index()
    prev = index.get(login) or {}
    now = int(time.time())
    index[login] = {
        "login": acct.get("login"),
        "server": acct.get("server"),
        "name": acct.get("name"),
        "currency": acct.get("currency"),
        "trade_mode": acct.get("trade_mode"),
        "leverage": acct.get("leverage"),
        "first_seen": prev.get("first_seen") or now,
        "last_seen": now,
        "last_balance": acct.get("balance"),
        "last_equity": acct.get("equity"),
    }
    save_index(index)
    return index


def list_accounts() -> list[dict]:
    """索引里所有账户，最近活跃的在前。"""
    index = load_index()
    rows = list(index.values())
    # 同一秒同步了两个账户时 last_seen 相同，必须有个确定的 tiebreak，
    # 否则面板上的顺序会随机跳。
    rows.sort(key=lambda r: ((r.get("last_seen") or 0), int(r.get("login") or 0)), reverse=True)
    for r in rows:
        r["dir"] = str(account_dir(r.get("login")))
        r["exists"] = account_dir(r.get("login")).exists()
    return rows


def guess_current_login() -> str | None:
    """没有终端上下文时的兜底：索引里最近活跃的那个账户。

    真正准确的判断来自 MT5（`account_snapshot()["login"]`），这个只在
    读历史文件时用——比如面板想看某个老账户，但没指定要看哪个。
    """
    rows = list_accounts()
    return str(rows[0]["login"]) if rows else None


def account_file(login: int | str, name: str) -> Path:
    """某个账户目录下的文件。"""
    return ensure_account_dir(login) / name


# ------------------------------------------------------------- 当前账户上下文

# 由 journal.collect() 每次从 MT5 读出后写入。所有 `login=None` 的调用都走它。
_current_login: str | None = None


def set_login(login: int | str | None) -> None:
    global _current_login
    _current_login = str(login) if login not in (None, "", "None") else None


def current_login() -> str | None:
    """终端当前登录的账户。没有上下文时退回索引里最近活跃的那个。"""
    if _current_login:
        return _current_login
    return guess_current_login()


def resolve_login(login: int | str | None = None) -> str:
    """把「可能为空的 login」变成确定的目录名。"""
    if login not in (None, "", "None"):
        return str(login)
    return current_login() or "unknown"


def set_root(root: Path | str | None) -> None:
    """把实盘数据根目录整体重定向。**只有测试该用它**。

    单测不能往真实账本里写东西，也不能依赖机器上有没有 MT5。
    传 None 恢复默认路径。
    """
    global LIVE_DIR, ACCOUNTS_DIR, INDEX_FILE, _current_login
    LIVE_DIR = appdirs.data_dir() / "live" if root is None else Path(root)
    ACCOUNTS_DIR = LIVE_DIR / "accounts"
    INDEX_FILE = LIVE_DIR / "index.json"
    _current_login = None


def read_json(login: int | str, name: str, default=None):
    p = account_dir(login) / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(login: int | str, name: str, data) -> None:
    """写 JSON：**先写临时文件再 os.replace 原子替换**。

    为什么不能直接 write_text：面板每 1~2 秒轮询一次、AI 线程同时在写同一个
    state 文件。write_text 分「打开→截断→写」几步，中间那几毫秒里读方会拿到
    半个 JSON；load_state 捕获 JSONDecodeError 后返回空状态，界面就会闪一下
    「未启动 / 空仓 / 无信号」。加到「人工确认」弹窗上，这个闪断会变成
    **弹窗闪没、下一秒又弹、提示音重复响**。

    os.replace 在同一分区上是原子的：读方要么读到完整旧文件，要么读到完整
    新文件，不存在中间态。临时文件用固定后缀（同目录），失败时清掉。
    """
    p = account_file(login, name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------- 场次备注


def read_notes(login: int | str | None = None) -> dict:
    """场次备注：{session_id: {note, tags, updated_at}}。"""
    data = read_json(resolve_login(login), "notes.json", {}) or {}
    return data if isinstance(data, dict) else {}


def write_note(session_id: str, note: str = "", tags: list[str] | None = None,
               login: int | str | None = None) -> dict:
    """写一场的备注。空备注 + 空标签 = 删除这条记录（不占地方）。"""
    key = resolve_login(login)
    data = read_notes(key)
    if (note or "").strip() or tags:
        data[str(session_id)] = {
            "note": (note or "").strip()[:500],
            "tags": [str(t)[:20] for t in (tags or [])][:5],
            "updated_at": int(time.time()),
        }
    else:
        data.pop(str(session_id), None)
    write_json(key, "notes.json", data)
    return data


# ------------------------------------------------------------- 逐笔进场理由
#
# 场次备注回答「这一场在干什么」，逐笔理由回答「为什么开这一单」。
# 后者是 AI 复盘的输入：没有它，模型只能从盈亏反推，说不出
# 「你这 5 笔都是在数据公布前追进去的」这类真正有用的模式。


def read_trade_notes(login: int | str | None = None) -> dict:
    """逐笔进场理由：{position_id: {note, updated_at}}。"""
    data = read_json(resolve_login(login), "trade_notes.json", {}) or {}
    return data if isinstance(data, dict) else {}


def write_trade_note(position_id: int | str, note: str = "",
                     login: int | str | None = None) -> dict:
    """写一笔的进场理由。传空字符串 = 删除。"""
    key = resolve_login(login)
    data = read_trade_notes(key)
    text = (note or "").strip()[:300]
    if text:
        data[str(position_id)] = {"note": text, "updated_at": int(time.time())}
    else:
        data.pop(str(position_id), None)
    write_json(key, "trade_notes.json", data)
    return data


# ------------------------------------------------------------- 删除账户


def trash_dir() -> Path:
    return LIVE_DIR / TRASH_DIR_NAME


def delete_account(login: int | str, current: int | str | None = None,
                   hard: bool = False) -> dict:
    """删除一个**归档账户**。默认软删除：整目录移到 `_trash/<login>-<时间戳>/`。

    - 拒绝删除终端当前登录的账户（`current` 由调用方从 MT5 读出后传入）
    - 拒绝删除不在 accounts/ 下的路径（防穿越）
    - 返回被移动的目录，方便告诉用户怎么恢复
    """
    key = str(login)
    if current is not None and key == str(current):
        return {"ok": False, "error": "这是终端当前登录的账户，不能删除；请先在 MT5 里切到别的账户"}

    src = account_dir(key)
    if not src.exists():
        return {"ok": False, "error": f"账户 {key} 没有归档目录"}

    try:
        # 只允许操作 accounts/ 的直接子目录
        if src.resolve().parent != ACCOUNTS_DIR.resolve():
            return {"ok": False, "error": "路径不在 accounts/ 下，拒绝操作"}
    except OSError:
        return {"ok": False, "error": "路径解析失败"}

    deals = 0
    ledger = src / "deals.jsonl"
    if ledger.exists():
        deals = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip())

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if hard:
        shutil.rmtree(src)
        moved = None
    else:
        dest = trash_dir() / f"{key}-{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved = str(dest)

    index = load_index()
    index.pop(key, None)
    save_index(index)

    return {"ok": True, "login": key, "deals": deals, "moved_to": moved,
            "restore": None if hard else f"把 {moved} 移回 {src} 并跑一次同步即可恢复"}


if __name__ == "__main__":
    for a in list_accounts():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a.get("last_seen") or 0))
        print(f"  {a['login']} @ {a.get('server')}  余额 {a.get('last_balance')}  最后活跃 {when}")
