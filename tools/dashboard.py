"""运行状态面板：本地 HTTP 服务 + 浏览器轮询

用法：
    python -m tools.dashboard            # 默认 127.0.0.1:8765，自动打开浏览器
    python -m tools.dashboard --port 9000 --no-open

数据来源：
    data/current_state.json   —— 由 agent/core.py 每轮实时写入
    若文件不存在，页面显示「未连接」/「暂无数据」

写入口（都是显式 POST，且必须带 confirm）：
    /api/advice            生成账户级 AI 复盘（花钱）
    /api/session_advice    生成场次级 AI 复盘（花钱）
    /api/autotrade/start   启动 AI 实时做单线程（花钱 + 可能下单）
    /api/autotrade/stop    停止 AI 并平掉所有持仓（会下单）
    /api/autotrade/reset   重置战绩（旧流水归档，不删除；会停掉 AI；**经验保留**）
    /api/autotrade/reset_lessons  只清 AI 攒的经验（教训库），战绩不动
    /api/llm/switch        换模型供应商/模型（真调一次验证，进程内即时生效）

只读接口里要留意的两个：
    /api/autotrade         AI 做单状态 + 战绩
    /api/llm               当前模型配置（key 只返回尾 4 位）

除上面几个和备注类接口外全是只读：GET 一律不产生费用、不下单。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from live import appdirs

# 四种路径，两种来源，别混：
#   TEMPLATE / CODE_ROOT  → 只读资源，走 bundle（冻结后是 _MEIPASS/_internal）
#   STATE_FILE / EVAL_DIR → 可写数据，走 app_root（冻结后是 exe 同级）
# 混了就会出两种事故：模板 500，或者账本写进临时目录随进程消失。
ROOT = appdirs.app_root()
STATE_FILE = appdirs.data_dir() / "current_state.json"
EVAL_DIR = appdirs.data_dir() / "evaluations"
TEMPLATE = appdirs.template_file()
CODE_ROOT = appdirs.strategies_dir()


class Server(ThreadingHTTPServer):
    """单实例、端口不共享的 HTTP 服务。

    `allow_reuse_address` 必须显式设 False。标准库 `HTTPServer` 默认把它设成 1，
    而 Windows 上 `SO_REUSEADDR` 的语义是"允许第二个进程绑上同一个端口"——
    表现是「两份都在跑，请求随机落到其中一个」，不报错。对这套东西是灾难：
    AI 做单线程和实盘记账线程写的是同一个 `autotrade_state.json` / `deals.jsonl`，
    两个进程会互相覆盖状态、重复下单。
    """

    daemon_threads = True
    allow_reuse_address = False


def _probe_instance(host: str, port: int, timeout: float = 0.6) -> dict | None:
    """探活：只有当对方**也是这个面板**时才算「已有一份在跑」。

    只判断端口有没有被占用是不够的——那可能是别的软件，换端口才是正确反应。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/app/info",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data if data.get("app") == "aqa-dashboard" else None
    except Exception:  # noqa: BLE001
        return None


def _start_server(host: str, want: int):
    """起服务，返回 (server, port, existing)。

    `existing` 非空 = 已经有一份在跑（pid 在里头），调用方应该只打开浏览器
    然后退出，**不要再起一个进程**。
    """
    for port in range(want, want + 8):
        inst = _probe_instance(host, port)
        if inst:
            return None, port, inst
        try:
            return Server((host, port), Handler), port, None
        except OSError:
            continue                    # 被别的软件占了：换下一个
    raise SystemExit(f"端口 {want}~{want + 7} 都被占用了，请用 --port 指定一个空闲端口")


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantDashboard/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        """写响应。**客户端提前断开不算错误。**

        浏览器切标签页、刷新、探活超时（`_probe_instance` 只等 0.6 秒）
        都会让 wfile 写到一半就 ConnectionAborted。不吞掉的话，
        每发生一次就往控制台刷一段几十行的 traceback——面板一天要被轮询
        十几万次，这个噪音会把真正的问题淹掉。
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/":
            if not TEMPLATE.exists():
                self._send(500, b"template missing", "text/plain; charset=utf-8")
                return
            self._send(200, TEMPLATE.read_bytes(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/state":
            if not STATE_FILE.exists():
                payload = {"status": "idle", "phase": "等待任务", "rounds": []}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            try:
                payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/accounts":
            # 账户列表 + 终端当前登录的是哪个。不读 MT5 历史，只问账户号。
            try:
                from live import paths
                from live.journal import probe_login

                cur = probe_login()
                rows = paths.list_accounts()
                if cur is not None:
                    paths.set_login(cur)
                    for r in rows:
                        r["current"] = str(r.get("login")) == str(cur)
                # 删除确认要显示「这个账户有多少笔」，顺手数一下账本行数
                for r in rows:
                    ledger = paths.account_dir(r.get("login")) / "deals.jsonl"
                    try:
                        r["deals"] = (sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
                                          if line.strip()) if ledger.exists() else 0)
                    except OSError:
                        r["deals"] = None
                payload = {"ok": True, "current": cur, "accounts": rows}
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "accounts": []}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/live":
            # 实盘账本：账户 + 持仓 + 逐笔统计 + 近期质量
            # ?force=1 跳过 15 秒缓存重新读 MT5
            # ?account=<login> 看归档的历史账户（离线读磁盘，不同步 MT5）
            #
            # 注意：这里**绝不调用 LLM**。GET 不能产生费用，
            # AI 复盘只在缓存命中时返回，否则 need_confirm=True 等用户确认。
            qs = parse_qs(parsed.query)
            force = "1" in (qs.get("force") or [])
            want = (qs.get("account") or [""])[0].strip()
            try:
                from live import paths
                from live.advisor import advise, api_call_summary, load_session_cache
                from live.journal import collect, collect_offline
                from live.stats import compute, risk_flags

                offline = False
                if want:
                    from live.journal import probe_login

                    live_login = probe_login()
                    if live_login is not None and str(live_login) == want:
                        info = collect(force=force)
                    else:
                        info = collect_offline(want)
                        offline = True
                else:
                    info = collect(force=force)
                if not info.get("ok"):
                    self._send(
                        200,
                        json.dumps({"ok": False, "error": info.get("error")}, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8",
                    )
                    return
                stats = compute(info["trades"], info["account"], info["deposits"], info.get("flows"))
                flags = risk_flags(stats)
                login = info.get("login") or want or None
                # 逐笔进场理由：AI 复盘的输入之一，也随结果一起回给面板
                trade_notes = paths.read_trade_notes(login)
                advice = advise(stats, flags, info["trades"], allow_call=False,
                                scoring=info.get("scoring"), login=login, notes=trade_notes)

                # 场次复盘：只放最近 30 场，账本长期累积后不至于把响应撑爆
                from live import rules
                from live.sessions import mark_summary

                sessions = (info.get("sessions") or [])[:30]
                # 行为标记累计要用**全部**场次（截断前），否则累计数会随窗口漂移
                marks = mark_summary(info.get("sessions") or [])
                note_map = paths.read_notes(login)
                rule = rules.load(login).get("rule")
                # 已生成过的场次 AI 一起带上，避免每次刷新都显示「未生成」
                sid_set = {s["id"] for s in sessions}
                cached_sess = load_session_cache(login)
                session_advice = {
                    k: {"advice": v.get("advice"), "generated_at": v.get("generated_at")}
                    for k, v in cached_sess.items() if k in sid_set
                }
                payload = {
                    "ok": True,
                    "login": login,
                    "offline": offline,
                    "account": info["account"],
                    "positions": info["positions"],
                    "stats": stats,
                    "flags": flags,
                    "advice": advice,
                    # 花钱的总数跟账户无关，汇总看；离线时不重复计数
                    "api_calls": api_call_summary(None if offline else login),
                    "sync": info["sync"],
                    # 只给最近 200 笔，账本长期累积后不至于把响应撑大
                    "trades": info["trades"][-200:],
                    "account_daily": info.get("account_daily") or [],
                    "quality": info.get("quality") or {},
                    "scoring": info.get("scoring") or {},
                    "sessions": sessions,
                    "mark_summary": marks,
                    "trade_notes": {k: (v or {}).get("note", "") for k, v in trade_notes.items()},
                    "sessions_error": info.get("sessions_error"),
                    "session_notes": note_map,
                    "session_advice": session_advice,
                    "rule": rule,
                    "rule_check": rules.summarize(rule, sessions),
                    "updated_at": info["updated_at"],
                }
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/autotrade":
            # AI 实时做单：只读状态，绝不在这里下单。
            # 启动/停止一律走 POST，且必须带 confirm。
            qs = parse_qs(parsed.query)
            want = (qs.get("account") or [""])[0].strip()
            try:
                from live import autotrade

                payload = autotrade.summary(want or None)
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return

        if parsed.path == "/api/lessons":
            # 教训库（跨场次）：只读，不花钱。为什么单独一个接口而不是塞进
            # /api/autotrade —— 它要扫所有归档流水数平仓笔，比状态查询重，
            # 面板轮询不该每 15 秒跑一次，改成用户点开时才拉。
            qs = parse_qs(parsed.query)
            want = (qs.get("account") or [""])[0].strip()
            try:
                from tools import lesson_stats

                rep = lesson_stats.build_report(want or None)
                payload = {"ok": True, "report": rep,
                           "text": lesson_stats.render(rep)}
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return

        if parsed.path == "/api/trade":
            # 单笔交易溯源：原料（K线/tick）→ 结构（ER/位置判定）→
            # 程序建议 vs 模型判断 → 执行结果 → 复盘。只读日志，
            # 终端在线时补拉历史行情重建当时的 14 根窗口，不调模型、不花钱。
            qs = parse_qs(parsed.query)
            want = (qs.get("account") or [""])[0].strip()
            ticket = (qs.get("ticket") or [""])[0].strip()
            if not ticket.isdigit():
                self._json(400, {"ok": False, "error": "缺 ticket 参数"})
                return
            try:
                from live import autotrade

                payload = autotrade.trade_detail(int(ticket), want or None)
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return

        if parsed.path == "/api/llm":
            # 当前模型配置：只读。key 只给尾 4 位，整串永不出服务端。
            try:
                from agent.llm import current_config
                from tools.switch_llm import PROVIDERS, provider_of

                cfg = current_config()
                pid = provider_of(cfg["base_url"])
                payload = {
                    "ok": True,
                    "provider": pid,
                    "provider_name": PROVIDERS[pid]["name"] if pid else "自定义 / 未知",
                    **cfg,
                    "providers": [
                        {
                            "id": k,
                            "name": v["name"],
                            "base_url": v["base_url"],
                            "default_model": v["model"],
                            "models": v.get("models") or [v["model"]],
                            "note": v.get("note", ""),
                        }
                        for k, v in PROVIDERS.items()
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return

        if parsed.path == "/api/strategies":
            items = []
            for path in sorted(CODE_ROOT.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                name = path.stem
                try:
                    spec = importlib.util.spec_from_file_location(f"_dash_{name}", path)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    cls = getattr(module, "Strategy", None)
                    if cls is None:
                        continue
                    sig = inspect.signature(cls.__init__)
                    params = {
                        p.name: ("..." if p.default is inspect.Parameter.empty else p.default)
                        for p in sig.parameters.values()
                        if p.name != "self"
                    }
                    doc = ((module.__doc__ or cls.__doc__ or "").strip().split("\n")[0])[:80]
                    items.append({"name": name, "doc": doc, "params": params,
                                  "lines": len(path.read_text(encoding="utf-8").splitlines())})
                except Exception:  # noqa: BLE001
                    continue
            self._send(200, json.dumps(items, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/evaluations":
            items = []
            if EVAL_DIR.exists():
                # sweep_*.json 是参数扫描结果，结构不同（没有 yearly），不进评估列表
                files = sorted(
                    (p for p in EVAL_DIR.glob("*.json") if not p.name.startswith("sweep_")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for path in files[:30]:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    yearly = data.get("yearly") or []
                    items.append(
                        {
                            "file": path.name,
                            "strategy": data.get("strategy", "?"),
                            "verdict": bool(data.get("verdict")),
                            "years": len(yearly),
                            "profit_years": sum(1 for r in yearly if (r.get("net_profit") or 0) > 0),
                            "net_profit": round(sum((r.get("net_profit") or 0) for r in yearly), 2),
                            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
                        }
                    )
            self._send(200, json.dumps(items, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/evaluation":
            name = (parse_qs(parsed.query).get("file") or [""])[0]
            # 只允许 evaluations 目录下的纯文件名，防止路径穿越
            if not name or Path(name).name != name or not name.endswith(".json"):
                self._send(400, b"bad name", "text/plain; charset=utf-8")
                return
            target = EVAL_DIR / name
            if not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                self._send(200, target.read_bytes(), "application/json; charset=utf-8")
            except OSError:
                self._send(500, b"read error", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/code":
            rel = (parse_qs(parsed.query).get("path") or [""])[0]
            try:
                # 前端传的形如 "strategies/xxx.py"（相对仓库根），而 CODE_ROOT
                # 本身就是 strategies 目录、且冻结后在只读包里。所以先剥掉开头的
                # "strategies" 再拼到 CODE_ROOT——直接 ROOT/rel 在绿化版会指到
                # exe 同级的空目录上，永远 404。
                raw = Path(rel)
                parts = raw.parts
                if parts and parts[0] == CODE_ROOT.name:
                    parts = parts[1:]
                if not parts:
                    raise FileNotFoundError(rel)
                target = (CODE_ROOT / Path(*parts)).resolve()
                # 只允许读 strategies/ 下的文件，防止路径穿越
                if (not target.is_relative_to(CODE_ROOT.resolve())
                        or not target.is_file()):
                    raise FileNotFoundError(rel)
                self._send(200, target.read_bytes(), "text/plain; charset=utf-8")
            except (OSError, ValueError):
                self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/app/info":
            self._json(200, {
                "app": "aqa-dashboard",
                "frozen": appdirs.is_frozen(),
                "pid": os.getpid(),
                "port": self.server.server_address[1],
                "data_root": str(appdirs.app_root()),
                "bundle_root": str(appdirs.bundle_root()),
                "env_file": str(appdirs.env_file()),
            })
            return

        if parsed.path == "/api/app/quit-preview":
            self._json(200, _quit_preview())
            return

        if parsed.path == "/api/setup":
            from agent.llm import current_config

            self._json(200, {
                "ok": True,
                "needed": setup_needed(),
                "frozen": appdirs.is_frozen(),
                "env_file": str(appdirs.env_file()),
                "env_exists": appdirs.env_file().is_file(),
                "config": current_config(),
            })
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        """写入口：场次备注 / 场次 AI / 删除账户 / 账户级 AI。

        两类约束不能破：
          1. 会花钱的（/api/advice、/api/session_advice）必须带 {"confirm": true}；
          2. 归档账户不能生成新的复盘——它连不上了，生成了也没法验证。
        备注和删账户不花钱，但仍然只接受显式 POST。
        """
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return

        routes = {
            "/api/session_note": self._post_session_note,
            "/api/session_advice": self._post_session_advice,
            "/api/trade_note": self._post_trade_note,
            "/api/accounts/delete": self._post_account_delete,
            "/api/rule": self._post_rule,
            "/api/advice": self._post_advice,
            "/api/autotrade/start": self._post_autotrade_start,
            "/api/autotrade/stop": self._post_autotrade_stop,
            "/api/autotrade/reset": self._post_autotrade_reset,
            "/api/autotrade/reset_lessons": self._post_autotrade_reset_lessons,
            "/api/llm/switch": self._post_llm_switch,
            "/api/app/quit": self._post_app_quit,
            "/api/setup": self._post_setup,
        }
        handler = routes.get(parsed.path)
        if handler is None:
            self._json(404, {"ok": False, "error": "not found"})
            return
        handler(body)

    # ---------------------------------------------------------- 各写入入口

    def _post_advice(self, body: dict) -> None:
        if body.get("account"):
            self._json(400, {"ok": False, "error": "归档账户无法生成新的 AI 复盘"})
            return
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝调用付费 API"})
            return
        try:
            from live import paths
            from live.advisor import advise, api_call_summary
            from live.journal import collect
            from live.stats import compute, risk_flags

            info = collect(force=True)
            if not info.get("ok"):
                payload = {"ok": False, "error": info.get("error")}
            else:
                stats = compute(info["trades"], info["account"], info["deposits"], info.get("flows"))
                flags = risk_flags(stats)
                result = advise(
                    stats, flags, info["trades"],
                    force=bool(body.get("force")), allow_call=True,
                    scoring=info.get("scoring"), login=info.get("login"),
                    notes=paths.read_trade_notes(info.get("login")),
                )
                payload = {"ok": result.get("ok", False), "advice": result, "api_calls": api_call_summary()}
                if not payload["ok"]:
                    payload["error"] = result.get("error")
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._json(200, payload)

    def _post_autotrade_start(self, body: dict) -> None:
        """启动 AI 做单。会真下单、会花钱调模型，所以 confirm 是硬要求。"""
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝启动"})
            return
        try:
            from live import autotrade, paths
            from live.journal import probe_login

            login = probe_login()
            if login is None:
                self._json(200, {"ok": False,
                                 "error": "MT5 未连接或未登录，无法启动"})
                return
            want = str(body.get("account") or "").strip()
            if want and want != str(login):
                self._json(400, {"ok": False,
                                 "error": "只能对终端当前登录的账户做单，归档账户不行"})
                return
            paths.set_login(login)

            cfg = autotrade.Cfg(
                symbol=(body.get("symbol") or "XAUUSDm").strip(),
                lots=float(body.get("lots") or 0.01),
                interval_sec=int(body.get("interval_sec") or autotrade.Cfg.interval_sec),
                cooldown_sec=int(body.get("cooldown_sec") or 30),
                dry_run=bool(body.get("dry_run", False)),
                max_trades=int(body.get("max_trades") or 0),
                max_loss_usd=float(body.get("max_loss_usd") or 0.0),
                max_minutes=int(body.get("max_minutes") or 0),
            )
            # 间隔太短等于把点差当水喝；冷却太长则做不到「1 分钟内重新开仓」。
            # 这些是逻辑约束，启动时就说清楚，别让它跑起来再悄悄违约。
            issues = autotrade.validate_cfg(cfg)
            if issues:
                self._json(400, {"ok": False, "error": "；".join(issues),
                                 "errors": issues})
                return
            # 真单先体检：最常见的失败是终端「算法交易」按钮没点开，
            # 提前查出来直接告诉用户去点按钮，比让他看一串「开仓失败」强。
            check = autotrade.precheck(cfg)
            if not check["ok"]:
                self._json(200, {"ok": False,
                                 "error": "；".join(check["errors"]),
                                 "errors": check["errors"]})
                return
            result = autotrade.WORKER.start(cfg, login=login)
            result["login"] = login
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_autotrade_stop(self, body: dict) -> None:
        """停止 AI 并（默认）平掉所有持仓。

        `flatten` 默认为 True：用户点的是「关闭」，不是「暂停」。
        但平仓会动到他手动开的单，所以必须带 confirm。
        """
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝平仓"})
            return
        try:
            from live import autotrade, paths
            from live.journal import probe_login

            login = probe_login()
            if login is not None:
                paths.set_login(login)
            flatten = bool(body.get("flatten", True))
            result = autotrade.WORKER.stop(login=login, flatten=flatten)
            result["login"] = login
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_autotrade_reset(self, body: dict) -> None:
        """重置战绩（归档旧流水，不删）。**经验默认保留**，所以必须带 confirm。

        以前这里连经验一起清：教训挂在流水上，一归档就跟着消失。现在教训
        独立存 lessons.jsonl，清经验是另一个动作（`/reset_lessons`）。
        想连经验一起清，显式传 clear_lessons=true。
        """
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝重置"})
            return
        try:
            from live import autotrade, paths
            from live.journal import probe_login

            login = probe_login()
            if login is not None:
                paths.set_login(login)
            want = str(body.get("account") or "").strip()
            if want and login is not None and want != str(login):
                self._json(400, {"ok": False,
                                 "error": "只能重置终端当前登录账户的 AI 流水"})
                return
            result = autotrade.reset_history(
                login=login, clear_lessons=bool(body.get("clear_lessons")))
            result["login"] = login
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_autotrade_reset_lessons(self, body: dict) -> None:
        """只清 AI 攒的经验（教训库），战绩和持仓一律不动。

        为什么要单独一个入口：清战绩只损失统计样本，清经验丢掉的是模型
        唯一的历史。两件事代价差一个量级，不能绑在同一个按钮上。
        """
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝清空经验"})
            return
        try:
            from live import autotrade, paths
            from live.journal import probe_login

            login = probe_login()
            if login is not None:
                paths.set_login(login)
            result = autotrade.reset_lessons(login=login)
            result["login"] = login
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_llm_switch(self, body: dict) -> None:
        """换模型供应商：写 .env + 真调一次验证 + 进程内即时生效。

        为什么要 confirm：这会真花一次调用，而且换错了模型/供应商，
        下一轮决策就会失败（表现为「模型调用失败」，不会误下单，但白等一轮）。
        key 可以留空 = 沿用当前 key（只换模型时不必重填）。
        """
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝切换"})
            return
        try:
            from agent.llm import current_config
            from tools.switch_llm import PROVIDERS, switch

            provider = str(body.get("provider") or "").strip()
            if provider not in PROVIDERS:
                self._json(400, {"ok": False,
                                 "error": f"不认识的供应商：{provider or '（空）'}"})
                return
            key = str(body.get("key") or "").strip()
            model = str(body.get("model") or "").strip()
            # 换供应商却没给 key = 拿老供应商的 key 去调新家，必然 401。
            # 这里先拦住，免得白花一次调用（还会把 .env 写脏再回滚）。
            if not key:
                cur = current_config()
                same = (cur["base_url"].rstrip("/")
                        == PROVIDERS[provider]["base_url"].rstrip("/"))
                if not same or not cur["key_set"]:
                    self._json(200, {
                        "ok": False,
                        "error": "换供应商必须填 key（当前 key 只对原来的供应商有效）",
                        "need_key": True,
                    })
                    return

            result = switch(provider, key or None, model or None)
            if result.get("ok"):
                result["current"] = current_config()
                result["note"] = ("已生效，下一次决策就用它。"
                                  if result.get("verified") else "已写入，未验证。")
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_app_quit(self, body: dict) -> None:
        """退出程序。这是**唯一可控的退出路径**。

        为什么不能靠关控制台窗口：Windows 给 CTRL_CLOSE_EVENT 的处理时限约 5 秒
        （conhost 硬编码），弹窗要人读、要点、要等平仓单回来，根本来不及；
        超时直接 TerminateProcess，裸仓就留在券商那边了。所以主路径放在这里，
        控制台 X 只做「尽力平仓」的兜底。

        `flatten=True` 会先市价平掉所有仓位再退出；False 则只停线程。
        """
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true"})
            return
        if _QUITTING.is_set():
            self._json(200, {"ok": True, "exiting": True, "note": "已经在退出"})
            return
        _QUITTING.set()
        res = _shutdown(bool(body.get("flatten", True)))
        self._json(200, {**res, "exiting": True})
        # 响应必须先把 JSON 写出去，再让进程死；0.4s 留给浏览器读完
        threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)),
                         name="quit", daemon=True).start()

    def _post_setup(self, body: dict) -> None:
        """首启配置向导。写 .env + 真调一次验证 + 进程内生效。

        逻辑全部复用 `switch_llm.switch()`，不另写一套——它已经做了
        「备份 → 写 → 验证 → 失败回滚文件+进程环境」四件事，而且是全项目
        唯一被测试覆盖的配置写入路径。

        `skip=true` 只是记一个「别再来烦我」的标记，不动配置。
        """
        if body.get("skip"):
            try:
                appdirs.config_dir().mkdir(parents=True, exist_ok=True)
                appdirs.setup_marker().touch()
            except OSError as exc:
                self._json(200, {"ok": False, "error": f"写入标记失败：{exc}"})
                return
            self._json(200, {"ok": True, "skipped": True})
            return

        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true（会真调一次模型）"})
            return
        try:
            from agent.llm import current_config
            from tools.switch_llm import ENV_FILE, PROVIDERS, switch

            provider = str(body.get("provider") or "").strip()
            key = str(body.get("key") or "").strip()
            model = str(body.get("model") or "").strip()
            if provider not in PROVIDERS:
                self._json(400, {"ok": False, "error": f"不认识的供应商：{provider or '（空）'}"})
                return
            if not key:
                self._json(400, {"ok": False, "error": "还没有填 key"})
                return

            existed_before = ENV_FILE.is_file()
            res = switch(provider, key, model or None, verify=True)
            if res.get("ok"):
                appdirs.config_dir().mkdir(parents=True, exist_ok=True)
                appdirs.setup_marker().touch()
                res["current"] = current_config()
            elif not existed_before:
                # 首次配置失败 → 把半截 .env 删掉，别留一个「看着像配好了」的文件
                try:
                    ENV_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
            self._json(200, res)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_session_note(self, body: dict) -> None:
        """场次备注：一句话 + 可选标签。不花钱，所以不需要 confirm。"""
        from live import paths

        sid = str(body.get("session_id") or "").strip()
        if not sid:
            self._json(400, {"ok": False, "error": "缺少 session_id"})
            return
        try:
            notes = paths.write_note(
                sid, body.get("note") or "", body.get("tags") or [],
                login=body.get("account") or None,
            )
            self._json(200, {"ok": True, "session_notes": notes})
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_trade_note(self, body: dict) -> None:
        """逐笔进场理由：为什么开这一单。不花钱，所以不需要 confirm。

        这是 AI 复盘唯一能看到「动机」的输入——只看价格和盈亏，
        模型只能给出「减少交易频率」这类正确但没用的话。
        """
        from live import paths

        pid = body.get("position_id")
        if pid in (None, ""):
            self._json(400, {"ok": False, "error": "缺少 position_id"})
            return
        try:
            data = paths.write_trade_note(
                pid, body.get("note") or "", login=body.get("account") or None
            )
            row = data.get(str(pid)) or {}
            self._json(200, {"ok": True, "position_id": str(pid), "note": row.get("note", "")})
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _post_session_advice(self, body: dict) -> None:
        """本场 AI 分析（付费）。生成成功后顺手落一条「下一场规则」。"""
        if body.get("account"):
            self._json(400, {"ok": False, "error": "归档账户无法生成新的 AI 分析"})
            return
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true，拒绝调用付费 API"})
            return
        sid = str(body.get("session_id") or "").strip()
        try:
            from live import paths, rules
            from live.advisor import advise_session, api_call_summary
            from live.journal import collect

            info = collect(force=True)
            if not info.get("ok"):
                self._json(200, {"ok": False, "error": info.get("error")})
                return
            sessions = info.get("sessions") or []
            sess = next((s for s in sessions if s.get("id") == sid), None)
            if sess is None:
                self._json(404, {"ok": False, "error": "找不到这一场（可能刚被刷新掉）"})
                return
            prev = next((s for s in sessions if s.get("index") == sess["index"] - 1), None)
            login = info.get("login")
            rule = rules.load(login).get("rule")
            # 逐笔进场理由随场次一起喂给模型（只读，不改原对象）
            sess = dict(sess, trade_notes=paths.read_trade_notes(login))
            res = advise_session(
                sess, prev_session=prev,
                note=(paths.read_notes(login) or {}).get(sid),
                ctx={"rule": rule, "rule_check": rules.check(rule, sess)},
                allow_call=True, force=bool(body.get("force")), login=login,
            )
            payload = {
                "ok": bool(res.get("ok")),
                "advice": res,
                "api_calls": api_call_summary(),
                "rule": rules.load(login).get("rule"),
            }
            if not payload["ok"]:
                payload["error"] = res.get("error")
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._json(200, payload)

    def _post_account_delete(self, body: dict) -> None:
        """删除归档账户。默认软删除——目录移到 _trash/，可以手动移回来。"""
        if not body.get("confirm"):
            self._json(400, {"ok": False, "error": "缺少 confirm=true"})
            return
        login = str(body.get("login") or "").strip()
        if not login:
            self._json(400, {"ok": False, "error": "缺少 login"})
            return
        try:
            from live import paths
            from live.journal import probe_login

            cur = None
            try:
                cur = probe_login()
            except Exception:  # noqa: BLE001
                cur = None
            res = paths.delete_account(login, current=cur, hard=bool(body.get("hard")))
            if res.get("ok") and paths.current_login() == login:
                paths.set_login(None)   # 内存里的当前账户上下文也要清掉
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._json(200, res)

    def _post_rule(self, body: dict) -> None:
        """规则管理：目前只有「清掉当前规则」。"""
        from live import rules

        if body.get("action") != "clear":
            self._json(400, {"ok": False, "error": "unknown action"})
            return
        try:
            rules.clear(body.get("account") or None)
            self._json(200, {"ok": True, "rule": None})
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:  # 静默，避免刷屏
        return


# ------------------------------------------------------------------ 配置向导

def setup_needed() -> bool:
    """要不要弹首启配置向导。

    判据是「模型能不能真调」，不是「.env 文件存不存在」——文件可能是空模板
    或者半截配置，那种情况恰恰最需要引导。
    `config/.setup_done` 存在 = 用户点过「稍后设置」，就不再拦他。
    """
    try:
        if appdirs.setup_marker().is_file():
            return False
        from agent.llm import current_config

        return not current_config().get("key_set")
    except Exception:  # noqa: BLE001
        return False                # 判定本身失败绝不能卡住面板


# ------------------------------------------------------------------ 退出

_QUITTING = threading.Event()


def _quit_preview() -> dict:
    """退出前体检：**只读**，不下单、不启动终端。

    `allow_launch=False` 是关键：用户可能已经把 MT5 关了，这时候去
    initialize() 会白等 60 秒，点个退出按钮卡一分钟。
    """
    out = {"ok": True, "positions": [], "count": 0, "float_pnl": 0.0,
           "running": False, "dry_run": None, "mt5": False, "error": None}
    try:
        from live import autotrade

        st = autotrade.load_state()
        out["running"] = bool(st.get("running")) and autotrade.WORKER.alive()
        out["dry_run"] = (st.get("cfg") or {}).get("dry_run")
    except Exception:  # noqa: BLE001
        pass
    try:
        from live.journal import open_positions

        pos = open_positions(allow_launch=False)
        out.update(mt5=True, positions=pos, count=len(pos),
                   float_pnl=round(sum(float(p.get("profit") or 0) for p in pos), 2))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _shutdown(flatten: bool) -> dict:
    """停 AI（必要时清仓）→ 断开 MT5。任何异常都必须返回：退出不能被它挡住。"""
    res: dict = {"ok": True, "count": 0, "failed": []}
    try:
        from live import autotrade

        res = autotrade.WORKER.stop(flatten=flatten)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        from live.journal import disconnect

        disconnect()
    except Exception:  # noqa: BLE001
        pass
    return res


# 回调对象必须留模块级引用：被 GC 之后系统再调就是野指针，直接崩。
_CTRL_REF = None


def _emergency_flatten(deadline: float = 4.0) -> dict:
    """控制台被关时的兜底平仓。**时限 4 秒**，因为 Windows 只给约 5 秒。

    不用 `WORKER.stop()`：它内部 `join(timeout=15)`，必然超时。
    只发平仓单（每笔实测 < 300ms）是唯一来得及的做法。
    不弹窗、不 initialize——弹窗会阻塞 handler 导致被强杀，initialize 要 60 秒。
    """
    from live import autotrade

    box: dict = {}

    def run() -> None:
        try:
            cfg = autotrade.cfg_from_state() or autotrade.Cfg()
            box["r"] = autotrade.close_all(cfg, why="控制台被关闭，自动清仓")
        except Exception as exc:  # noqa: BLE001
            box["e"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=run, name="emergency-flatten", daemon=True)
    t.start()
    t.join(deadline)
    box["finished"] = not t.is_alive()
    try:
        appdirs.logs_dir().mkdir(parents=True, exist_ok=True)
        with (appdirs.logs_dir() / "quit.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), **box},
                                ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return box


def _install_console_guard() -> None:
    """关控制台窗口时尽力平仓。

    **故意不弹窗**：handler 在 5 秒内不返回就会被 TerminateProcess，
    MessageBoxW 是同步阻塞，弹了也只会闪一下然后连仓一起被杀。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def _handler(event: int) -> bool:
            if event in (2, 5, 6):          # CTRL_CLOSE / LOGOFF / SHUTDOWN
                try:
                    _emergency_flatten(4.0)
                except Exception:  # noqa: BLE001
                    pass
            return True                      # 交回系统按默认规则退出

        global _CTRL_REF
        _CTRL_REF = _handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_REF, True)
    except Exception:  # noqa: BLE001
        pass


def _live_recorder(interval_s: int = 300, allow_launch: bool = True) -> None:
    """后台线程：定时同步实盘账本并写每日快照。

    为什么放在面板进程里：面板是唯一「会一直开着」的常驻进程。
    这样即使没人看页面，账本和每日余额也一直在记。
    用 daemon 线程，主进程退出时自动结束。

    `allow_launch=True` 时如果发现 MT5 没开，会尝试把它拉起来。
    这个只有面板自己是由资源管理器/bat 启动时才有效——从 agent 进程树里
    拉 MT5 会 0xC0000139，所以不要指望在别处调 collect 也能自动启动。
    """

    def loop() -> None:
        ok = False
        while True:
            try:
                from live.journal import collect

                info = collect(force=True, allow_launch=allow_launch)
                ok = bool(info.get("ok"))
                if not ok:
                    print(f"[live] 同步失败: {info.get('error')}")
            except Exception as exc:  # noqa: BLE001
                ok = False
                print(f"[live] 同步异常: {type(exc).__name__}: {exc}")
            # 连上了就按正常间隔；没连上就 20 秒重试一次，别让人等 5 分钟
            time.sleep(interval_s if ok else min(20, interval_s))

    t = threading.Thread(target=loop, name="live-recorder", daemon=True)
    t.start()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AI Quant Agent 状态面板")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-live", action="store_true", help="不启动实盘后台同步线程")
    parser.add_argument("--no-launch", action="store_true", help="MT5 没开时不要尝试自动启动它")
    parser.add_argument("--live-interval", type=int, default=300, help="实盘同步间隔（秒）")
    args = parser.parse_args()

    # 可写目录先建好。放到 Program Files 这类只读位置时这里就报错，
    # 而不是等用户第一次点「开始做单」才 500——那时候根本看不出是权限问题。
    appdirs.ensure_writable_dirs()

    server, port, existing = _start_server(args.host, args.port)
    url = f"http://{args.host}:{port}"

    if existing:
        # 同一个账户只能跑一个面板：两份进程会互相覆盖 autotrade_state.json、
        # 重复下单。所以这里只打开浏览器，不再起一个服务。
        print(f"已经有一份在跑（pid={existing.get('pid')}）：{url}")
        print("同一个账户只允许一个面板实例——两份会互相覆盖状态、重复下单。")
        if not args.no_open:
            webbrowser.open(url)
        return

    print(f"面板地址: {url}")
    if port != args.port:
        print(f"注意：{args.port} 被别的程序占用，已自动改用 {port}")
    print(f"状态文件: {STATE_FILE}  ({'存在' if STATE_FILE.exists() else '尚未生成，跑一次 agent.core 后出现'})")
    print(f"数据目录: {appdirs.app_root()}")

    if not args.no_live:
        _live_recorder(args.live_interval, allow_launch=not args.no_launch)
        auto = "关" if args.no_launch else "开"
        print(f"实盘记录: 每 {args.live_interval}s 同步一次 -> data/live/  (自动启动 MT5: {auto})")

    print("Ctrl+C 停止")
    if appdirs.is_frozen():
        # 纯 ASCII：Windows 控制台默认 GBK，`⚠` 这类符号编不出来会直接
        # UnicodeEncodeError 把整个进程带走（打包后实测踩过，源码态不触发）。
        print("[!] 退出请用面板右上角「退出程序」：会先平仓再关。"
              "直接关这个黑窗口只能尽力在 4 秒内平仓，来不及确认。")

    _install_console_guard()

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
