"""面板 HTTP 层的契约测试：路由通不通、**花钱/下单的闸门还在不在**。

为什么现在才补
--------------
`tools/dashboard.py` 的 Handler 有一千行，之前一个用例都没有（只有
`tools/smoke_dashboard.js` 那种"从 HTML 里抠出内联脚本、在假 DOM 里跑"的前端冒烟）。
而 `confirm=true` 是这套东西里唯一挡住"手滑真下单"的自动化闸门——
它要是被哪次重构顺手删掉，界面照常工作，没人会发现。

这里不起窗口、不连 MT5、不调模型：只起一个 0 端口的 HTTP 服务打真实请求，
验证「状态码 + 响应体」这两件最外层的事实。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from tools import dashboard


class LiveServerCase(unittest.TestCase):
    """每个用例起一个独立服务，跑完关掉，端口由系统分配。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = dashboard.Server(("127.0.0.1", 0), dashboard.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # -------------------------------------------------- 请求工具

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> tuple[int, dict | str]:
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._decode(exc.read())

    def post(self, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body or {}).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=data, method="POST",
                                     headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._decode(exc.read())

    @staticmethod
    def _decode(raw: bytes):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")


class TestReadOnlyRoutes(LiveServerCase):
    def test_autotrade_summary_is_passed_through(self):
        """只读接口不能自己下单，只把 summary 转出来。"""
        fake = {"ok": True, "running": False, "kpi": {"trades": 3}}
        with mock.patch("live.autotrade.summary", return_value=fake):
            status, payload = self.get("/api/autotrade")
        self.assertEqual(status, 200)
        self.assertEqual(payload, fake)

    def test_unknown_get_is_404(self):
        status, payload = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_unknown_post_is_404(self):
        status, payload = self.post("/api/nope")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


class TestLlmEntries(LiveServerCase):
    """面板是两级下拉：**供应商 → 模型**。

    DeepSeek 的「思考 / 不思考」是同一个模型名 + 一个请求参数，所以它必须落在
    「模型」那一级的选项里（variants），而不是把供应商拆成两条——拆了之后
    「这一家的 key 通不通用」就没有落点了。
    """

    def test_providers_are_three_with_deepseek_first(self):
        status, payload = self.get("/api/llm")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        ids = [p["id"] for p in payload["providers"]]
        self.assertEqual(ids, ["deepseek", "zhipu", "siliconflow"])
        self.assertEqual(payload["providers"][0]["name"], "DeepSeek 开放平台")

    def test_deepseek_exposes_exactly_two_model_variants(self):
        _, payload = self.get("/api/llm")
        d = payload["providers"][0]
        self.assertEqual(d["models"], ["deepseek-flash"])
        self.assertEqual([v["model"] for v in d["variants"]], ["deepseek-flash"] * 2)
        self.assertEqual([bool(v["thinking"]) for v in d["variants"]], [False, True])

    def test_current_variant_index_is_reported(self):
        """回填「模型」下拉靠下标：两项模型名相同，只给名字没法区分。"""
        _, payload = self.get("/api/llm")
        self.assertIn("variant_index", payload)
        self.assertIsInstance(payload["variant_index"], int)
        self.assertTrue(payload["provider_name"])
        for key in ("thinking", "thinking_effective", "model"):
            self.assertIn(key, payload)


class TestOriginGuard(LiveServerCase):
    """POST 的跨站护栏（2026-11-05 安全审查 P0）。

    背景：浏览器里任何网页都能向 127.0.0.1 的面板发请求，body 想写什么写什么，
    confirm=true 挡不住——审查时一个带恶意 Origin 的 curl 就真实开了一单（亏 1.2）。
    所以写接口必须校验 Origin/Referer 指向本面板。
    载体选 /api/rule：不带 Origin 时它返回 400 unknown action（无副作用、不碰文件），
    一旦看到 400 就说明 Origin 检查放行、请求走到了路由层。
    """

    def test_foreign_origin_is_403(self):
        status, payload = self.post("/api/rule", {"action": "__origin_probe__"},
                                    headers={"Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)
        self.assertIn("跨站", payload["error"])

    def test_foreign_referer_is_403(self):
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"},
                              headers={"Referer": "https://evil.example.com/page"})
        self.assertEqual(status, 403)

    def test_local_origin_is_allowed(self):
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"},
                              headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 400)   # 走到了路由层，只是 action 不认识

    def test_localhost_origin_any_port_is_allowed(self):
        """自动换端口场景：Origin 的端口可能不是启动时的默认端口。"""
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"},
                              headers={"Origin": "http://localhost:9999"})
        self.assertEqual(status, 400)

    def test_missing_origin_is_allowed(self):
        """curl / 脚本 / 单测没有 Origin 头，是本机合法调用方，必须放行。"""
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"})
        self.assertEqual(status, 400)

    def test_null_origin_is_rejected(self):
        """sandbox iframe 的 Origin 是字符串 "null"，不是本地面板，拒。"""
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"},
                              headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_non_http_scheme_is_rejected(self):
        status, _ = self.post("/api/rule", {"action": "__origin_probe__"},
                              headers={"Origin": "file:///etc/passwd"})
        self.assertEqual(status, 403)


class TestMoneyGates(LiveServerCase):
    """会花钱 / 会下单的入口，缺 confirm 必须 400 且不执行任何动作。"""

    def test_advice_requires_confirm(self):
        with mock.patch("live.advisor.advise") as m_advise:
            status, payload = self.post("/api/advice")
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])
        m_advise.assert_not_called()

    def test_session_advice_requires_confirm(self):
        status, payload = self.post("/api/session_advice")
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])

    def test_autotrade_start_requires_confirm(self):
        """启动会真下单。这条闸门掉了就等于界面上一键开真仓。"""
        with mock.patch("live.autotrade.WORKER") as m_worker:
            status, payload = self.post("/api/autotrade/start", {"cfg": {}})
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])
        m_worker.start.assert_not_called()

    def test_autotrade_stop_requires_confirm(self):
        with mock.patch("live.autotrade.WORKER") as m_worker:
            status, payload = self.post("/api/autotrade/stop", {})
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])
        m_worker.stop.assert_not_called()

    def test_signal_approve_requires_confirm(self):
        """同意一条信号 = 下单。approve 不带 confirm 必须被拒。"""
        with mock.patch("live.autotrade.DESK") as m_desk:
            status, payload = self.post("/api/autotrade/signal",
                                        {"id": "x", "approve": True})
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])
        m_desk.answer.assert_not_called()

    def test_bad_json_body_is_400(self):
        req = urllib.request.Request(self.url("/api/advice"), data=b"{oops",
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, payload = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, json.loads(exc.read())
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
