"""决策流水与运行状态的**磁盘 I/O 层**。

为什么单独一层
--------------
`autotrade` 原本把「怎么算」和「存在哪」写在一起：`read_log` / `save_state`
这类函数被面板每秒轮询、被回测复盘、被测试打桩，却和决策逻辑共处一个
6,000 行文件。拆出来的好处是这三类调用方可以只依赖本模块，不必背上
`autotrade` 的整条导入链。

本模块**只做读写，不做判断**：不决定日志里该有什么字段、不决定 state 里
该有哪些键的语义（`_empty_state` 是 schema 的唯一副本，见下）。

`_empty_state()` 是 state 的**唯一 schema**：新增字段只改这里。
`load_state()` 拿它兜底，所以老状态文件缺键时不会 KeyError。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from live import paths
from live.cfg import Cfg


def state_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "autotrade_state.json")


def log_path(login: int | str | None = None) -> Path:
    return paths.account_file(paths.resolve_login(login), "autotrade_log.jsonl")


def _empty_state() -> dict:
    return {
        "running": False,
        "started_at": None,
        "last_tick_ts": None,
        "last_open_ts": None,
        "last_close_ts": None,
        "last_close_pnl": None,
        # 刚平掉那笔的方向：同向重进要等更久（见 Cfg.cooldown_same_sec）。
        # 老状态没有这个键 → 按「方向未知」处理，回落短的冷却，不会误拦。
        "last_close_dir": None,
        "trades": 0,       # 已开仓笔数
        "closed": 0,       # 已平仓笔数
        "wins": 0,
        # 其中有多少笔是程序硬止损平的（source="sl"）。与「模型主动平」分开数，
        # 否则命中率会被止损单污染。
        "sl_closes": 0,
        # 其中有多少笔是保本止损平的（source="be" / "be_local"，2026-10-29）。
        # 和 sl_closes 分开：硬止损说明入场方向错了，保本止损说明方向对过。
        "be_closes": 0,
        "pnl": 0.0,
        "position": None,
        "stop_reason": None,
        "last_decision": None,
        # 等待锚点：模型说"等回踩到 X"就把 X 记下来（见 _wait_step）。
        # 不记的话它每轮都会换一个更靠近现价的数，等于把追高改写成等待。
        "wait": None,
        "wait_drift": 0,
        # 上一轮「不做」的结论 + 当时的报价（见 _remember_skip / _skip_note）。
        # 模型没有跨轮记忆，这两个字段是喂给它的外部记忆，不是给面板看的。
        "prev_entry": None,
        "skip_streak": None,
        # 出场端连续模型调用失败次数：≥3 次自动停（仓位没人管比少做一笔危险）
        "model_errors": 0,
        # 已平仓、但还没到复盘时机的单子。平仓后要等 7 根 K 线走完才复盘，
        # 这段时间里系统照常做下一笔——复盘不能挡住交易。
        "pending_reviews": [],
        # 正在等人点的那条信号（AI 辅助模式）。面板每秒轮询 state，
        # 拿到它就弹窗；人点完由 HTTP 线程写答复文件 + 唤醒等待者。
        # 只在等待期间有值，结案后立即清空——所以它的存在本身就是
        # 「现在有一个弹窗该显示」的信号。
        "pending_signal": None,
        # 否决 / 超时后的静默期，按信号类型分槽。入场否决后连模型调用
        # 一起省掉（和冷却同性质）；出场否决只是不再弹窗，模型照问——
        # 否则仓位会有 3 分钟没人评估。
        "assist_deny_until": {},
        "last_signal_ts": None,
        # 谁在跑：panel = 面板线程，cli = 命令行进程。面板重启后 state 里可能
        # 还留着 running=True，靠这两个字段判断那条记录是不是已经作废了。
        "owner": None,
        "pid": None,
        # 最近一次运行的配置（含 dry_run）。面板「关闭并清仓」时线程可能已经
        # 不在了，必须能还原「刚才到底是纸面还是真实」，否则会把真单当成
        # 纸面单跳过，用户以为平了其实没平。
        "cfg": None,
    }


def load_state(login: int | str | None = None) -> dict:
    p = state_path(login)
    base = _empty_state()
    if not p.exists():
        return base
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if isinstance(data, dict):
        base.update(data)
    return base


def save_state(state: dict, login: int | str | None = None) -> None:
    paths.write_json(paths.resolve_login(login), "autotrade_state.json", state)


def append_log(row: dict, login: int | str | None = None) -> None:
    p = log_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _tail_lines(p: Path, limit: int, chunk: int = 256 * 1024) -> list[str]:
    """从文件尾部倒着读，凑够 `limit` 行就停。返回**完整**的文本行。

    为什么不用 `read_text()` 再切片
    ------------------------------
    流水只在手动重置时轮转，所以它只会变长。`read_log(limit=N)` 原来是
    「全文解析 → 取后 N 条」：N 只是返回值上限，**省不了任何成本**，
    面板每秒轮询一次，代价随文件线性涨。文件 5000 行以后，每轮都在解析
    几年前的行。

    从尾部按块读，成本只和 `limit` 有关，与文件多大无关。
    """
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        # 多读一行再丢：块边界可能正好切在一行中间，靠"多凑一个换行"兜住。
        while pos > 0 and buf.count(b"\n") <= limit:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf

    if pos > 0:
        # 起点在文件中间：第一段是半行，按字节切掉（不能在解码后切——
        # 多字节汉字可能正好被切在中间，解出来是乱码，会污染那一行的 JSON）。
        cut = buf.find(b"\n")
        buf = buf[cut + 1:] if cut >= 0 else b""
    return buf.decode("utf-8", "replace").splitlines()


def read_log(login: int | str | None = None, limit: int | None = None) -> list[dict]:
    """读决策流水。`limit` 只读尾部 N **行**（真·只读 N 行，不是读完再切）。

    和旧实现的唯一语义差别：坏行（进程被杀时写了一半）会被跳过，所以返回
    条数可能比 `limit` 少。旧实现是"全解析 → 取后 N 条"，坏行不影响条数。
    为这一点保留 O(文件) 的代价不值得——坏行本来就说明那次写入没完成，
    少一条比多解析十万行划算。
    """
    p = log_path(login)
    if not p.exists():
        return []
    if limit and limit > 0:
        lines = _tail_lines(p, int(limit))
        # 行数可能少于 limit（文件本来就短），也可能多一行（倒读的余量）。
        lines = lines[-int(limit):]
    else:
        lines = p.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _log_signal(sig: dict, ans: dict, cfg: Cfg, login=None,
                extra: dict | None = None) -> dict:
    """每次信号结案落一行，无论人点了什么。

    为什么连"同意"也要单独记一行：open/close 行只说明"发生了成交"，
    说明不了"人看过、等了 12 秒、点了同意"。事后想算「人点 vs 模型自己走」
    的期望差，必须有这一行。
    """
    row = {
        # ts 用**信号发出的时刻**（sig 构造时就定成这一轮的 now），不是写盘时刻。
        # 这一行是在人答完之后才落的，用墙钟会把它盖到等待窗的末尾——于是一轮
        # 120 秒的等待，流水里变成"先有结论（skip 行）、后有信号"，读起来
        # 像时间倒流。等待多久已经由 waited_sec 记着了，不用 ts 再表达一遍。
        "ts": int(sig.get("ts") or time.time()), "kind": "signal",
        "signal_id": sig.get("id"), "sig_kind": sig.get("kind"),
        "answer": ans.get("answer"),
        "waited_sec": round(float(ans.get("waited_sec") or 0.0), 1),
        "direction": sig.get("direction"), "p_up": sig.get("p_up"),
        "prog_dir": sig.get("prog_dir"), "agree": sig.get("agree"),
        "act": sig.get("act"), "advice": sig.get("advice"),
        "advice_text": sig.get("advice_text"),
        "price": sig.get("price"),
        "bid": round(float(ans.get("bid") or sig.get("bid") or 0), 3),
        "ask": round(float(ans.get("ask") or sig.get("ask") or 0), 3),
        "reason": sig.get("reason"), "drv": "assist",
        "dry_run": bool(sig.get("dry_run")),
    }
    if sig.get("ticket") is not None:
        row["ticket"] = sig["ticket"]
    if sig.get("held_sec") is not None:
        row["held_sec"] = sig["held_sec"]
    if ans.get("late"):
        row["late"] = True          # 人在止损生效的同一瞬间才点，留痕
    if extra:
        row.update(extra)
    append_log(row, login)
    return row


def cache_stats(login: int | str | None = None) -> dict:
    """从 api_calls.jsonl 现算前缀缓存命中率。

    为什么单独算：DeepSeek 的上下文缓存是**自动**的（按前缀匹配），
    没有 prompt_cache_key 这种参数可传。想让命中率变好，唯一办法是让
    「每轮都不变的部分」尽量长、并放在最前面；而要判断有没有生效，
    只能看服务端返回的 cache_hit_prompt_tokens。
    """
    p = paths.account_file(paths.resolve_login(login), "api_calls.jsonl")
    hit = miss = calls = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = row.get("usage") or {}
            h, m = u.get("cache_hit_tokens"), u.get("cache_miss_tokens")
            if h is None and m is None:
                continue
            calls += 1
            hit += int(h or 0)
            miss += int(m or 0)
    total = hit + miss
    return {
        "calls": calls,
        "hit_tokens": hit,
        "miss_tokens": miss,
        "hit_rate": round(hit / total, 4) if total else None,
    }
