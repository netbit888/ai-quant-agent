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
        232307606/               每个账户一套完整账本
          deals.jsonl
          daily.json  account.json  account_daily.json
          quality_daily.json  advice.json  api_calls.jsonl
          position_state.json  position_snapshots.jsonl
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = ROOT / "data" / "live"
ACCOUNTS_DIR = LIVE_DIR / "accounts"
INDEX_FILE = LIVE_DIR / "index.json"


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
    LIVE_DIR = ROOT / "data" / "live" if root is None else Path(root)
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
    p = account_file(login, name)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    for a in list_accounts():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a.get("last_seen") or 0))
        print(f"  {a['login']} @ {a.get('server')}  余额 {a.get('last_balance')}  最后活跃 {when}")
