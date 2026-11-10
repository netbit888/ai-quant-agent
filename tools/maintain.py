"""项目体检与维护入口：把「想起来才做」的维护工作变成一条可定时执行的命令。

为什么需要
----------
这个项目里已经有备份、有测试、有 lint、有发版脚本，缺的不是能力，是**执行**：
备份每天在写但从没有人验证过它能不能打开；43 个测试只在改代码时跑，环境漂移
（依赖被动过、.venv 缺包）平时看不见；发版流程踩过三次「tag 建在旧提交上」，
那类错误在发布当天才暴露。这些都不是「出事了再查」能解决的，只能靠定期体检。

所以这个脚本不发明新机制，它只做三件事：
  1. **验真**：把已有机制的产物打开看一眼。备份文件存在 != 备份可用。
  2. **比对**：状态文件与目录、index 与 account、版本号与 tag，两处说法不一致就报。
  3. **提示**：该清的东西堆了多少。给数字，不给「建议定期清理」这种废话。

三条纪律
--------
1. **默认只读。** 体检永远不改数据：不写 state、不挪账本、不碰 config/。
   维护工具自身的 bug 不该有机会损坏唯一副本。
2. **修复必须显式。** `--fix` 只做「可再生」的清理（排障残留、旧快照以外的临时件），
   而且先 `--dry-run` 出清单。账本、state、日志主文件一律不在可清范围内。
3. **分级而非罗列。** 每条结论必须能回答「要不要现在处理」：FAIL 要处理，
   WARN 要知情，INFO 是背景。退出码只由 FAIL 决定，方便接告警。

用法
----
    python -m tools.maintain                # 常规体检（账本/备份/卫生/发版）
    python -m tools.maintain --full         # 再加 ruff + 全量单测（慢，周跑）
    python -m tools.maintain --only backup  # 只跑某一组
    python -m tools.maintain --json         # 机器可读，给定时任务消费
    python -m tools.maintain --fix --dry-run  # 看清理清单，不动手

报告同时落到 ``data/maintenance/report-<日期>.md``：终端会滚，文件不会。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION, parse_version  # noqa: E402
from live import appdirs, paths  # noqa: E402

# ---------------------------------------------------------------- 结论分级

OK = "OK"
INFO = "INFO"
WARN = "WARN"
FAIL = "FAIL"

_RANK = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}

G_LEDGER = "账本"
G_BACKUP = "备份"
G_HYGIENE = "磁盘卫生"
G_CODE = "代码质量"
G_RELEASE = "发版一致性"
G_RUNTIME = "运行态"

# 组名到短名的映射，`--only` 用短名，避免中文参数在 cmd.exe 里的编码麻烦
GROUP_ALIAS = {
    "ledger": G_LEDGER,
    "backup": G_BACKUP,
    "hygiene": G_HYGIENE,
    "code": G_CODE,
    "release": G_RELEASE,
    "runtime": G_RUNTIME,
}


@dataclass
class Item:
    """一条检查结论。

    ``advice`` 是可执行的下一步，不是「请关注」。写不出具体动作的检查项
    说明它不该存在——噪音会让整份报告失去可信度。
    """

    group: str
    name: str
    level: str
    detail: str = ""
    advice: str = ""

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "name": self.name,
            "level": self.level,
            "detail": self.detail,
            "advice": self.advice,
        }


_CHECKS: list[tuple[str, object]] = []


def check(group: str):
    """注册检查器。顺序即报告顺序：先看账本，最后看运行态。"""

    def deco(fn):
        _CHECKS.append((group, fn))
        return fn

    return deco


# ---------------------------------------------------------------- 小工具


def _load_json(p: Path):
    """解析失败返回 None，不抛——体检自己不能因为坏文件中断。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _account_dirs() -> list[Path]:
    """真实账户目录。

    只认纯数字名：``accounts/`` 下还有 smoke/（测试）和 _trash/（回收站），
    用名字规则天然排除，不必维护黑名单。
    """
    base = paths.ACCOUNTS_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and p.name.isdigit())


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _dir_size(p: Path) -> int:
    total = 0
    if not p.is_dir():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _git(*args: str, timeout: int = 20) -> tuple[int, str]:
    """跑 git，失败返回 (-1, 原因)。没有 git 不是错误，是信息。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout or "").strip()


def _ts_text(ts: float | int | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


# ---------------------------------------------------------------- 账本


def _check_state(acct: Path) -> list[Item]:
    out: list[Item] = []
    p = acct / "autotrade_state.json"
    who = acct.name
    if not p.is_file():
        return [Item(G_LEDGER, f"{who} 状态文件", WARN, "autotrade_state.json 不存在",
                     "从未跑过自动交易则正常；否则检查是否被误删")]

    st = _load_json(p)
    if st is None:
        return [Item(G_LEDGER, f"{who} 状态文件", FAIL, "JSON 解析失败",
                     "state 每 60 秒覆盖写入，损坏说明写盘被打断；"
                     "从 data/backups/<login>/ 的最近快照恢复，别手工补字段")]

    running = bool(st.get("running"))
    tick = st.get("last_tick_ts")
    if running:
        if not isinstance(tick, (int, float)):
            out.append(Item(G_LEDGER, f"{who} 心跳", WARN, "running=true 但没有 last_tick_ts"))
        else:
            age = time.time() - float(tick)
            if age > 300:
                out.append(Item(
                    G_LEDGER, f"{who} 心跳", WARN,
                    f"running=true，但最后 tick 在 {_ts_text(tick)}（{age / 60:.0f} 分钟前）",
                    "进程已退出但状态没落成 false（崩溃残留），或线程卡死。"
                    "面板启动时会重写这个字段，不用手改",
                ))
            else:
                out.append(Item(G_LEDGER, f"{who} 心跳", OK,
                                f"运行中，最后一轮 {age:.0f} 秒前"))
    else:
        out.append(Item(G_LEDGER, f"{who} 心跳", INFO,
                        f"未在跑，最后活动 {_ts_text(tick)}"))

    # 持仓字段与成交记录的一致性：能查出「本地以为空仓、实际有仓」的一半情形
    pos = st.get("position")
    if pos not in (None, {}, []):
        out.append(Item(G_LEDGER, f"{who} 残留持仓", WARN,
                        f"state 里仍有 position：{json.dumps(pos, ensure_ascii=False)[:120]}",
                        "若非本人手动持有，先对账 MT5 再启动自动交易"))
    return out


def _check_deals(acct: Path) -> list[Item]:
    """逐行解析成交记录。

    `deals.jsonl` 是 append-only，一行坏了永久留在那里；更要紧的是**半行**
    ——写盘中途被杀会留下没有换行结尾的残行。两者都必须能被发现，所以这里
    不只看文件存在，而是真的把每行 json.loads 一遍。
    """
    p = acct / "deals.jsonl"
    who = acct.name
    if not p.is_file():
        return []  # 没成交过就没有这个文件，不是问题

    bad: list[int] = []
    rows = 0
    last_ts = None
    try:
        with p.open("rb") as fh:
            for idx, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                rows += 1
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    bad.append(idx)
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("time"), (int, float)):
                    last_ts = max(last_ts or 0, rec["time"])
    except OSError as exc:
        return [Item(G_LEDGER, f"{who} 成交记录", FAIL, f"读取失败：{exc}")]

    if bad:
        shown = ", ".join(str(i) for i in bad[:5])
        return [Item(G_LEDGER, f"{who} 成交记录", FAIL,
                     f"{len(bad)}/{rows} 行不是合法 JSON（行号 {shown}）",
                     "账本唯一副本，不要直接编辑。先复制整份到 _trash 留证，"
                     "再从最近快照 diff 出坏行")]

    return [Item(G_LEDGER, f"{who} 成交记录", OK,
                 f"{rows} 行可解析，最近成交 {_ts_text(last_ts)}")]


@check(G_LEDGER)
def _check_ledger() -> list[Item]:
    out: list[Item] = []
    accts = _account_dirs()
    if not accts:
        return [Item(G_LEDGER, "账户目录", WARN, f"{paths.ACCOUNTS_DIR} 下没有账户",
                     "路径错了或数据目录被挪走，检查 appdirs.data_dir()")]

    for acct in accts:
        out.extend(_check_state(acct))
        out.extend(_check_deals(acct))

    index = _load_json(paths.INDEX_FILE)
    if index is None:
        out.append(Item(G_LEDGER, "账户索引", WARN, "index.json 缺失或不可解析",
                        "面板下次启动会重建，但历史 first_seen 会丢"))
    elif isinstance(index, dict):
        on_disk = {p.name for p in accts}
        missing = sorted(on_disk - set(index))
        stale = sorted(set(index) - on_disk)
        if missing:
            out.append(Item(G_LEDGER, "账户索引", WARN,
                            f"index.json 缺登记：{', '.join(missing)}",
                            "跑一次面板自动补；不影响账本"))
        if stale:
            out.append(Item(G_LEDGER, "账户索引", INFO,
                            f"index.json 有已不在盘上的账户：{', '.join(stale)}",
                            "通常是账户目录被手工删除。保留条目无害，"
                            "想清就等下次面板启动时自动 prune"))
        if not missing and not stale:
            out.append(Item(G_LEDGER, "账户索引", OK, f"{len(index)} 个账户已登记且目录一致"))
    return out


# ---------------------------------------------------------------- 备份


def _verify_zip(p: Path, require_ledger: bool) -> tuple[bool, str]:
    """真打开、真解一个文件出来。

    只看 `ZipFile.namelist()` 不够：0 字节的 zip 也能列出空表，截断的 zip
    在 namelist 阶段也可能不报错。所以额外 `read()` 一个成员，让 CRC 校验跑一遍。
    """
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            if not names:
                return False, "空压缩包（0 个文件）"
            if require_ledger and not any(
                n.endswith("deals.jsonl") or n.endswith("autotrade_state.json") for n in names
            ):
                return False, f"缺关键文件（只有 {len(names)} 个：{', '.join(names[:3])}）"
            zf.read(names[0])
            return True, f"{len(names)} 个文件，{_human(p.stat().st_size)}"
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        return False, f"打不开：{exc}"


def _latest_data_date(acct: Path) -> date | None:
    """账户目录里最新一次写入的日期。

    用来区分「那天程序没跑」和「跑了但备份没成功」：如果账户数据在 11-07
    有写入，而 backups/ 里没有 11-07.zip，那才是真缺口。没有这个参照就只能
    对每个工作日报缺，噪音大到没人看。
    """
    newest = 0.0
    for name in ("autotrade_log.jsonl", "autotrade_state.json", "deals.jsonl"):
        f = acct / name
        try:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return datetime.fromtimestamp(newest).date() if newest else None


@check(G_BACKUP)
def _check_backup() -> list[Item]:
    out: list[Item] = []
    bdir = appdirs.data_dir() / "backups"
    if not bdir.is_dir():
        return [Item(G_BACKUP, "备份目录", FAIL, f"{bdir} 不存在",
                     "日切快照从未成功。面板启动时会调 snapshot()，"
                     "手动验证：python -c \"from live import backup; backup.snapshot_all()\"")]

    entries = sorted(p for p in bdir.iterdir() if p.is_dir())
    if not entries:
        out.append(Item(G_BACKUP, "备份目录", FAIL, "目录是空的"))

    for sub in entries:
        zips = sorted(sub.glob("*.zip"))
        if not zips:
            out.append(Item(G_BACKUP, f"{sub.name} 快照", WARN, "一份快照都没有",
                            "该账户从未在面板启动后跨过日切，或 snapshot() 一直失败"))
            continue

        latest = zips[-1]
        ok, why = _verify_zip(latest, require_ledger=sub.name != "_global")
        if ok:
            out.append(Item(G_BACKUP, f"{sub.name} 最新快照", OK,
                            f"{latest.name}，{why}"))
        else:
            out.append(Item(G_BACKUP, f"{sub.name} 最新快照", FAIL,
                            f"{latest.name} {why}",
                            "备份不可用等于没有备份。检查磁盘剩余空间，"
                            "并确认 snapshot() 里 MAX_ACCOUNT_BYTES 没被触发"))

        # 缺口检查：只在该账户当天确有数据写入时才报。
        #
        # 上界用「最早一份 zip 的日期」而不是「今天 - 7 天」：备份机制上线之前
        # 的日子本来就没有备份，那不是缺口，是历史。不设这个下界，报告第一天
        # 就会带着两条永远修不掉的历史债，之后每次都在喊——狼来了几次，整份
        # 报告就没人看了。
        acct_dir = paths.ACCOUNTS_DIR / sub.name
        if not acct_dir.is_dir():
            continue  # _global 没有账户目录，也就没有「那天在写数据」的参照
        days = {z.stem for z in zips}
        today = date.today()
        latest_data = _latest_data_date(acct_dir)
        try:
            window_start = max(date.fromisoformat(min(days)), today - timedelta(days=7))
        except ValueError:
            continue  # zip 名不是日期格式，不是本程序生成的
        gaps = []
        for back in range(1, 8):
            d = today - timedelta(days=back)
            if d < window_start or d.weekday() >= 5:  # 窗口外 / 周末不交易
                continue
            if d.isoformat() in days:
                continue
            if latest_data and d > latest_data:
                continue  # 那天之后数据就没动过，说明程序没跑
            gaps.append(d.isoformat())
        if gaps:
            out.append(Item(G_BACKUP, f"{sub.name} 快照缺口", WARN,
                            f"最近 7 天缺：{', '.join(gaps)}",
                            "该日账户有数据写入却没有对应 zip。看 data/logs/backup.jsonl "
                            "里那天的记录，若 ok=false 说明打包失败"))

    # 备份日志里的失败必须被念出来，否则它只是躺在文件里
    log = appdirs.data_dir() / "logs" / "backup.jsonl"
    if log.is_file():
        fails = []
        try:
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("ok") is False:
                    fails.append(f"{rec.get('login')}@{rec.get('day')}")
        except OSError:
            pass
        if fails:
            out.append(Item(G_BACKUP, "备份失败记录", WARN,
                            f"最近 50 条里有 {len(fails)} 次失败：{', '.join(fails[:5])}",
                            "备份失败按设计只打日志不抛（不能挡住面板启动），"
                            "所以只能靠这里发现"))
    return out


# ---------------------------------------------------------------- 磁盘卫生

# 排障时留下的临时副本。命名很杂（.bak-pnlfix / .legacy / 带时间戳的改名版），
# 用「后缀前缀」匹配而不是逐个列举，否则下次又换个名字就漏了。
_RESIDUE_RE = re.compile(r"\.(bak|legacy|old|orig|tmp)([-.].*)?$", re.IGNORECASE)
# 带启动时间戳的整份日志（面板每次重启会轮一份出来）
_STAMPED_RE = re.compile(r"^autotrade_(log|state)\.\d{8}-\d{6}\.(jsonl|json)$")

BIG_FILE_WARN = 50 * 1024 * 1024
BIG_FILE_INFO = 5 * 1024 * 1024
DATA_WARN = 500 * 1024 * 1024
DIST_INFO = 2 * 1024 * 1024 * 1024


def _residue_files() -> list[Path]:
    out: list[Path] = []
    for acct in _account_dirs():
        for f in acct.iterdir():
            if not f.is_file():
                continue
            if _RESIDUE_RE.search(f.name) or _STAMPED_RE.match(f.name):
                out.append(f)
    return sorted(out)


@check(G_HYGIENE)
def _check_hygiene() -> list[Item]:
    out: list[Item] = []

    residues = _residue_files()
    if residues:
        total = sum(f.stat().st_size for f in residues if f.is_file())
        out.append(Item(G_HYGIENE, "排障残留", WARN,
                        f"{len(residues)} 个文件，共 {_human(total)}："
                        + ", ".join(f.name for f in residues[:4])
                        + ("…" if len(residues) > 4 else ""),
                        "`--fix` 会移到 data/live/_trash/<日期>/（不删除）。"
                        "确认过不再需要的话再清回收站"))
    else:
        out.append(Item(G_HYGIENE, "排障残留", OK, "账户目录干净"))

    # 单文件膨胀：只增不减的文件是磁盘占用的真正来源
    for acct in _account_dirs():
        for f in acct.iterdir():
            if not f.is_file():
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size < BIG_FILE_INFO:
                continue
            level = WARN if size >= BIG_FILE_WARN else INFO
            out.append(Item(G_HYGIENE, f"{acct.name}/{f.name}", level,
                            _human(size),
                            "只增不减。确认面板的 read_log 走尾部读（limit）后"
                            "可手工归档旧段，别在程序运行时改名"))

    data_bytes = _dir_size(appdirs.data_dir())
    level = WARN if data_bytes >= DATA_WARN else INFO
    if data_bytes >= DATA_WARN:
        out.append(Item(G_HYGIENE, "data/ 总体积", level, _human(data_bytes),
                        "备份保留 14 天是设计窗口。要压体积先看上面哪类文件在涨"))
    else:
        out.append(Item(G_HYGIENE, "data/ 总体积", OK, _human(data_bytes)))

    dist = ROOT / "dist"
    if dist.is_dir():
        n = _dir_size(dist)
        out.append(Item(G_HYGIENE, "dist/ 打包产物",
                        INFO if n >= DIST_INFO else OK, _human(n),
                        "每次打包都会累积 Setup/Delta，可整体删除后重打"))
    return out


# ---------------------------------------------------------------- 代码质量


def _find_ruff() -> str | None:
    name = "ruff.exe" if sys.platform == "win32" else "ruff"
    local = Path(sys.executable).with_name(name)
    if local.exists():
        return str(local)
    return shutil.which("ruff")


@check(G_CODE)
def _check_code(full: bool = False) -> list[Item]:
    out: list[Item] = []

    ruff = _find_ruff()
    if not ruff:
        out.append(Item(G_CODE, "ruff", INFO, "未安装，跳过",
                        "pip install -r requirements-dev.txt"))
    else:
        try:
            proc = subprocess.run([ruff, "check", ".", "--output-format", "concise"],
                                  cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
            if proc.returncode == 0:
                out.append(Item(G_CODE, "ruff", OK, "pyflakes 级错误 0 条"))
            else:
                lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
                out.append(Item(G_CODE, "ruff", FAIL,
                                f"{len(lines)} 条：" + " | ".join(lines[:3]),
                                "F 类是真错误（未定义名/未使用导入），不是风格问题，必须修"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.append(Item(G_CODE, "ruff", WARN, f"运行失败: {exc}"))

    if not full:
        out.append(Item(G_CODE, "单测", INFO, "未跑（加 --full）",
                        "日常跳过是为了快；每周至少全量跑一次"))
        return out

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        tail = (proc.stderr or "").strip().splitlines()
        summary = tail[-1] if tail else ""
        if proc.returncode == 0:
            out.append(Item(G_CODE, "单测", OK, summary or "全部通过"))
        else:
            fails = [ln for ln in tail if ln.startswith(("FAIL:", "ERROR:"))]
            out.append(Item(G_CODE, "单测", FAIL,
                            f"{len(fails)} 个失败：" + " | ".join(fails[:4]) or summary,
                            "环境漂移也会让测试红。先确认失败是否与最近改动相关，"
                            "再怀疑 .venv"))
    except subprocess.TimeoutExpired:
        out.append(Item(G_CODE, "单测", WARN, "15 分钟未跑完，已放弃"))
    except OSError as exc:
        out.append(Item(G_CODE, "单测", WARN, f"无法启动: {exc}"))
    return out


# ---------------------------------------------------------------- 发版一致性


@check(G_RELEASE)
def _check_release() -> list[Item]:
    out: list[Item] = []

    # ① 版本号与 CHANGELOG
    changelog = ROOT / "CHANGELOG.md"
    if changelog.is_file():
        text = changelog.read_text(encoding="utf-8", errors="replace")
        if f"## v{APP_VERSION}" in text:
            out.append(Item(G_RELEASE, "CHANGELOG", OK, f"含 v{APP_VERSION} 段落"))
        else:
            out.append(Item(G_RELEASE, "CHANGELOG", WARN,
                            f"没有 v{APP_VERSION} 的段落",
                            "版本号已 bump 但没写变更记录；发版脚本会取 "
                            "<!-- notes --> 块，缺了更新弹窗是空的"))

    # ② tag。注意：本地 tag 不等于线上发布——没 fetch 时本地看不到远端 tag，
    # 曾经因此误判「还能往已发布版本追加内容」。所以只报「本地落后」，不下结论。
    code, tags = _git("tag", "--list")
    if code != 0:
        out.append(Item(G_RELEASE, "git tag", INFO, "读不到（git 未安装或非仓库）"))
    else:
        parsed = [(parse_version(t), t) for t in tags.splitlines() if t.strip()]
        parsed = [(v, t) for v, t in parsed if v]
        if not parsed:
            out.append(Item(G_RELEASE, "git tag", INFO, "没有任何版本 tag"))
        else:
            best_v, best_t = max(parsed)
            if best_v >= parse_version(APP_VERSION):
                out.append(Item(G_RELEASE, "git tag", OK,
                                f"本地最新 {best_t} ≥ APP_VERSION {APP_VERSION}"))
            else:
                out.append(Item(G_RELEASE, "git tag", WARN,
                                f"本地最新 tag {best_t}，APP_VERSION 已是 {APP_VERSION}",
                                "本地 tag 落后通常是没 fetch。发版前先 "
                                "`git fetch --tags` + `gh release list`，"
                                "以线上为准，别以 `git tag --list` 为准"))

    # ③ 未提交 / 未推。release.bat 会从当前分支建 tag，脏工作区最容易出事。
    code, status = _git("status", "--porcelain")
    if code == 0:
        if status:
            n = len(status.splitlines())
            if n > 10:
                out.append(Item(G_RELEASE, "工作区", WARN, f"{n} 个文件未提交",
                                "改动分散说明有没收尾的实验。发版前要么提交要么 stash"))
            else:
                out.append(Item(G_RELEASE, "工作区", INFO,
                                f"{n} 个未提交文件：" + status.replace("\n", " / ")[:160]))
        else:
            out.append(Item(G_RELEASE, "工作区", OK, "干净"))

    code, ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if code == 0 and ahead.strip().isdigit():
        n = int(ahead.strip())
        if n:
            out.append(Item(G_RELEASE, "未推送提交", FAIL, f"本地领先远端 {n} 个提交",
                            "release.bat 用 gh release create，tag 不存在时 gh 会在"
                            "**远端默认分支尖端**建 tag。先 push 再发版，"
                            "否则 release 挂错源码（v1.5.7 真实踩过）"))
        else:
            out.append(Item(G_RELEASE, "未推送提交", OK, "与远端同步"))
    return out


# ---------------------------------------------------------------- 运行态

DASH_PORT = 8765


@check(G_RUNTIME)
def _check_runtime() -> list[Item]:
    out: list[Item] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        alive = sock.connect_ex(("127.0.0.1", DASH_PORT)) == 0
    finally:
        sock.close()
    if alive:
        out.append(Item(G_RUNTIME, "面板端口", INFO,
                        f"127.0.0.1:{DASH_PORT} 在监听（面板或残留进程）"))
    else:
        out.append(Item(G_RUNTIME, "面板端口", INFO, "未监听（面板没开）"))

    # MT5 终端不在这里探测：initialize() 会占住终端会话几十秒，体检不该去碰它。
    # 这是刻意的——维护脚本不能因为想看一眼就把实盘连接搅乱。
    out.append(Item(G_RUNTIME, "MT5 终端", INFO, "不探测（initialize 会占用会话）"))
    return out


# ---------------------------------------------------------------- 渲染


def _render(items: list[Item], elapsed: float) -> str:
    order = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}
    shown = sorted(items, key=lambda i: (order.get(i.level, 9), i.group, i.name))
    counts = {lv: sum(1 for i in items if i.level == lv) for lv in (FAIL, WARN, INFO, OK)}

    lines = [
        f"项目体检  {datetime.now():%Y-%m-%d %H:%M}  v{APP_VERSION}  ({elapsed:.1f}s)",
        "=" * 78,
    ]
    for it in shown:
        if it.level == OK:
            continue  # 通过的项不占版面，只在汇总里计数
        lines.append(f"[{it.level:<4}] {it.group} · {it.name}")
        if it.detail:
            lines.append(f"         {it.detail}")
        if it.advice:
            lines.append(f"         处置：{it.advice}")

    lines.append("-" * 78)
    lines.append(f"汇总：FAIL {counts[FAIL]} / WARN {counts[WARN]} / "
                 f"INFO {counts[INFO]} / OK {counts[OK]}")
    if counts[FAIL]:
        lines.append("有 FAIL：现在就该处理，其余项可以晚点看。")
    elif counts[WARN]:
        lines.append("无 FAIL；WARN 项知情即可，不必立刻动手。")
    else:
        lines.append("全部正常。")
    return "\n".join(lines)


def _write_report(items: list[Item]) -> Path:
    """落到 data/maintenance/。终端会滚，文件不会；周对比靠它。"""
    out_dir = appdirs.data_dir() / "maintenance"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report-{date.today().isoformat()}.md"
    counts = {lv: sum(1 for i in items if i.level == lv) for lv in (FAIL, WARN, INFO, OK)}

    rows = ["| 级别 | 组 | 项 | 详情 | 处置 |", "|---|---|---|---|---|"]
    for it in sorted(items, key=lambda i: (-_RANK.get(i.level, 0), i.group, i.name)):
        if it.level == OK:
            continue
        rows.append(f"| {it.level} | {it.group} | {it.name} | "
                    f"{it.detail.replace('|', '/')} | {it.advice.replace('|', '/')} |")

    body = "\n".join([
        f"# 项目体检 {date.today().isoformat()}",
        "",
        f"版本 v{APP_VERSION} · FAIL {counts[FAIL]} / WARN {counts[WARN]} / "
        f"INFO {counts[INFO]} / OK {counts[OK]}",
        "",
        *rows,
        "",
    ])
    path.write_text(body, encoding="utf-8")

    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(
        {"ts": int(time.time()), "version": APP_VERSION, "counts": counts,
         "items": [i.as_dict() for i in items]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 修复


def _apply_fix(dry: bool) -> list[str]:
    """只做一件可逆的事：把排障残留挪进 _trash。

    为什么不做更多：`--fix` 一旦开始删日志、压缩账本、清理 state，它就变成
    了另一个能损坏唯一副本的程序。维护工具的权限必须比被维护的对象小。
    """
    moved: list[str] = []
    trash = paths.LIVE_DIR / paths.TRASH_DIR_NAME / date.today().isoformat()
    for f in _residue_files():
        rel = f"{f.parent.name}/{f.name}"
        if dry:
            moved.append(f"[dry] {rel}")
            continue
        try:
            dest_dir = trash / f.parent.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest_dir / f.name))
            moved.append(rel)
        except OSError as exc:
            moved.append(f"[失败] {rel}: {exc}")
    return moved


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.maintain",
        description="项目体检：账本完整性 / 备份验真 / 磁盘卫生 / 代码质量 / 发版一致性",
    )
    ap.add_argument("--full", action="store_true", help="加跑 ruff + 全量单测（慢）")
    ap.add_argument("--only", action="append", choices=sorted(GROUP_ALIAS),
                    help="只跑某一组，可重复")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--fix", action="store_true", help="清理排障残留（移到 _trash，不删除）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --fix，只打印清单")
    ap.add_argument("--no-report", action="store_true", help="不写 data/maintenance/")
    args = ap.parse_args(argv)

    if args.fix:
        moved = _apply_fix(args.dry_run)
        if args.json:
            print(json.dumps({"moved": moved}, ensure_ascii=False, indent=2))
        else:
            print(f"{'将移动' if args.dry_run else '已移动'} {len(moved)} 个文件：")
            for m in moved:
                print(f"  {m}")
            if not args.dry_run and moved:
                print(f"\n回收站在 {paths.LIVE_DIR / paths.TRASH_DIR_NAME}/"
                      f"{date.today().isoformat()}/，确认无误后整体删除。")
        return 0

    wanted = {GROUP_ALIAS[g] for g in args.only} if args.only else None
    started = time.time()
    items: list[Item] = []
    for group, fn in _CHECKS:
        if wanted and group not in wanted:
            continue
        try:
            if group == G_CODE:
                items.extend(fn(full=args.full))
            else:
                items.extend(fn())
        except Exception as exc:  # noqa: BLE001
            # 一个检查器崩了不能带走整份体检：报出来，继续跑后面的
            items.append(Item(group, "检查器异常", FAIL, f"{type(exc).__name__}: {exc}"))
    elapsed = time.time() - started

    if args.json:
        print(json.dumps(
            {"ts": int(time.time()), "version": APP_VERSION,
             "counts": {lv: sum(1 for i in items if i.level == lv)
                        for lv in (FAIL, WARN, INFO, OK)},
             "items": [i.as_dict() for i in items]},
            ensure_ascii=False, indent=2))
    else:
        print(_render(items, elapsed))

    if not args.no_report:
        try:
            path = _write_report(items)
            if not args.json:
                print(f"\n报告：{path}")
        except OSError as exc:
            print(f"\n报告写入失败：{exc}", file=sys.stderr)

    return 1 if any(i.level == FAIL for i in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())
