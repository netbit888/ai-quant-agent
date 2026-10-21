"""运行状态面板：本地 HTTP 服务 + 浏览器轮询

用法：
    python -m tools.dashboard            # 默认 127.0.0.1:8765，自动打开浏览器
    python -m tools.dashboard --port 9000 --no-open

数据来源：
    data/current_state.json   —— 由 agent/core.py 每轮实时写入
    若文件不存在，页面显示「未连接」/「暂无数据」

只读设计：不提供任何控制接口（开始/停止），纯观测。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "current_state.json"
EVAL_DIR = ROOT / "data" / "evaluations"
TEMPLATE = ROOT / "tools" / "dashboard_template.html"
CODE_ROOT = ROOT / "strategies"


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantDashboard/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
                from live.advisor import advise, api_call_summary
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
                advice = advise(stats, flags, info["trades"], allow_call=False,
                                scoring=info.get("scoring"), login=login)
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
                    "updated_at": info["updated_at"],
                }
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
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
                target = (ROOT / rel).resolve()
                # 只允许读 strategies/ 下的文件，防止路径穿越
                if not str(target).startswith(str(CODE_ROOT.resolve())) or not target.is_file():
                    raise FileNotFoundError(rel)
                self._send(200, target.read_bytes(), "text/plain; charset=utf-8")
            except (OSError, ValueError):
                self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        """只接受一种写入：用户确认后生成 AI 复盘。

        这是整个面板**唯一**会调用外部付费 API 的入口，必须显式带
        {"confirm": true}，否则直接 400。GET 侧永远只读缓存。
        """
        parsed = urlparse(self.path)
        if parsed.path != "/api/advice":
            self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"error":"bad json"}', "application/json; charset=utf-8")
            return

        # 归档账户不给生成：它连不上了，生成了也没法验证，纯烧钱
        if body.get("account"):
            self._send(
                400,
                json.dumps({"ok": False, "error": "归档账户无法生成新的 AI 复盘"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
            return

        if not body.get("confirm"):
            self._send(
                400,
                json.dumps({"ok": False, "error": "缺少 confirm=true，拒绝调用付费 API"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
            return

        try:
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
                )
                payload = {"ok": result.get("ok", False), "advice": result, "api_calls": api_call_summary()}
                if not payload["ok"]:
                    payload["error"] = result.get("error")
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # 静默，避免刷屏
        return


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

    url = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"面板地址: {url}")
    print(f"状态文件: {STATE_FILE}  ({'存在' if STATE_FILE.exists() else '尚未生成，跑一次 agent.core 后出现'})")

    if not args.no_live:
        _live_recorder(args.live_interval, allow_launch=not args.no_launch)
        auto = "关" if args.no_launch else "开"
        print(f"实盘记录: 每 {args.live_interval}s 同步一次 → data/live/  (自动启动 MT5: {auto})")

    print("Ctrl+C 停止")

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
