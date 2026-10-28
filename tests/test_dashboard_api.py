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
from pathlib import Path
from unittest import mock

from tests._stubs import install_missing_stubs

install_missing_stubs()

from tools import dashboard


class LiveServerCase(unittest.TestCase):
    """每个用例起一个独立服务，跑完关掉，端口由系统分配。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = dashboard.STATE_FILE
        dashboard.STATE_FILE = Path(self._tmp.name) / "current_state.json"
        self.server = dashboard.Server(("127.0.0.1", 0), dashboard.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        dashboard.STATE_FILE = self._orig_state
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

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.url(path), data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
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
    def test_state_idle_when_no_file(self):
        """没有 state 文件时返回 idle，不能 500——这是面板冷启动的第一屏。"""
        status, payload = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "idle")

    def test_state_reads_file(self):
        dashboard.STATE_FILE.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        status, payload = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "running")

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
