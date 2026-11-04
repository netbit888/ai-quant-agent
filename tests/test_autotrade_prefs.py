"""面板「交易参数」记忆的契约测试（live/prefs.py + /api/autotrade/prefs）。

为什么值得单独测
----------------
这条线解决的是「每次打开面板都要重选品种/模式/间隔」。它有两个容易坏的
地方，而且坏了都不会报错：

1. **落盘路径**。prefs 走 `paths.LIVE_DIR`，一旦有人改成
   `appdirs.data_dir()` 直取，单测的 `set_root` 就不再隔离——测试会把开发机
   真实的品种选择覆盖掉，而且没有任何提示。
2. **写回非法值**。间隔记成 7 秒时，前端 `select.value = "7"` 赋不进去，
   下拉框会变空白：看起来像参数丢了，实际是记住了一个界面上不存在的值。
   所以白名单（INTERVALS / TRADABLE_SYMBOLS）必须在写入和读取两侧都生效。

这里不起 MT5、不调模型：只碰一个 JSON 文件和一个 HTTP handler。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests._stubs import install_missing_stubs

install_missing_stubs()

from live import autotrade, paths, prefs
from tools import dashboard

SYMBOLS = autotrade.TRADABLE_SYMBOLS


class PrefsFileCase(unittest.TestCase):
    """每个用例独立的 live 目录，绝不碰真实选择。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        paths.set_login("999")

    def tearDown(self):
        paths.set_root(None)
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 读

    def test_defaults_when_no_file(self):
        """没记过 = 出厂默认，而且不创建文件（只读不该有副作用）。"""
        self.assertEqual(prefs.load_prefs(SYMBOLS), prefs.DEFAULT_PREFS)
        self.assertFalse(prefs.prefs_file().exists())

    def test_corrupt_file_falls_back(self):
        """文件被写坏时回退默认值，不抛。

        这个文件只影响「默认值」；为它抛异常会把整个 /api/autotrade 拖垮，
        面板连决策流都看不到——故障范围远大于它本身的价值。
        """
        prefs.prefs_file().write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(prefs.load_prefs(SYMBOLS), prefs.DEFAULT_PREFS)

    def test_unknown_values_are_dropped_on_load(self):
        """文件被手改：非法品种/间隔/模式一律丢弃，合法的留下。"""
        prefs.prefs_file().write_text(json.dumps({
            "symbol": "EURUSD",        # 不在白名单
            "interval_sec": 7,         # 不是下拉框里的档位
            "mode": "assist",
            "assist_paper": True,
        }), encoding="utf-8")
        got = prefs.load_prefs(SYMBOLS)
        self.assertEqual(got["symbol"], prefs.DEFAULT_PREFS["symbol"])
        self.assertEqual(got["interval_sec"], prefs.DEFAULT_PREFS["interval_sec"])
        self.assertEqual(got["mode"], "assist")
        self.assertTrue(got["assist_paper"])

    # ---------------------------------------------------------------- 写

    def test_save_roundtrip(self):
        res = prefs.save_prefs({
            "symbol": "XAUUSDc", "interval_sec": 30,
            "mode": "assist", "assist_paper": True,
        }, symbols=SYMBOLS)
        self.assertTrue(res["ok"], res)
        got = prefs.load_prefs(SYMBOLS)
        self.assertEqual(got["symbol"], "XAUUSDc")
        self.assertEqual(got["interval_sec"], 30)
        self.assertEqual(got["mode"], "assist")
        self.assertTrue(got["assist_paper"])
        self.assertIn("updated_at", got)

    def test_partial_update_keeps_other_fields(self):
        """前端每次发全量，但接口是合并语义——少发的字段不能被清成默认。

        合并而不是覆盖是有意的：以后加减字段时，旧版本的面板不会把新字段
        顺手抹掉。
        """
        prefs.save_prefs({"symbol": "XAUUSDc", "interval_sec": 30,
                          "mode": "paper"}, symbols=SYMBOLS)
        res = prefs.save_prefs({"interval_sec": 5}, symbols=SYMBOLS)
        self.assertTrue(res["ok"], res)
        got = prefs.load_prefs(SYMBOLS)
        self.assertEqual(got["symbol"], "XAUUSDc")
        self.assertEqual(got["interval_sec"], 5)
        self.assertEqual(got["mode"], "paper")

    def test_reject_interval_outside_options(self):
        """间隔不在下拉框档位里 → 拒绝，且不污染已有值。"""
        prefs.save_prefs({"interval_sec": 60}, symbols=SYMBOLS)
        res = prefs.save_prefs({"interval_sec": 7}, symbols=SYMBOLS)
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"])
        self.assertEqual(prefs.load_prefs(SYMBOLS)["interval_sec"], 60)

    def test_reject_symbol_outside_whitelist(self):
        """没测过 point/contract 的品种不能记下来：启动时必然被 validate_cfg 拒。"""
        res = prefs.save_prefs({"symbol": "EURUSD"}, symbols=SYMBOLS)
        self.assertFalse(res["ok"])
        self.assertEqual(prefs.load_prefs(SYMBOLS)["symbol"],
                         prefs.DEFAULT_PREFS["symbol"])

    def test_reject_unknown_mode(self):
        res = prefs.save_prefs({"mode": "turbo"}, symbols=SYMBOLS)
        self.assertFalse(res["ok"])
        self.assertEqual(prefs.load_prefs(SYMBOLS)["mode"],
                         prefs.DEFAULT_PREFS["mode"])

    def test_write_is_atomic_no_tmp_left(self):
        """os.replace 的临时文件不能留在目录里，且目标文件始终可解析。

        面板 1 秒轮询一次 /api/autotrade，读方会同时读这个文件；
        截断式写入的中间态会让品种闪回默认值。
        """
        prefs.save_prefs({"symbol": "XAUUSDm"}, symbols=SYMBOLS)
        leftovers = list(Path(self._tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])
        raw = json.loads(prefs.prefs_file().read_text(encoding="utf-8"))
        self.assertEqual(raw["symbol"], "XAUUSDm")

    # ---------------------------------------------------------------- Cfg 反推

    def test_prefs_from_cfg_modes(self):
        """启动成功时按实际 cfg 回写——三种模式的映射不能反。

        反了就是「界面记成纸面、实际发真单」的孪生事故。
        """
        cases = [
            (dict(dry_run=False, assist=False), "live", False),
            (dict(dry_run=True, assist=False), "paper", False),
            (dict(dry_run=False, assist=True), "assist", False),
            (dict(dry_run=True, assist=True), "assist", True),   # 辅助·演练
        ]
        for kw, mode, assist_paper in cases:
            cfg = autotrade.Cfg(symbol="XAUUSDc", interval_sec=30, **kw)
            got = prefs.prefs_from_cfg(cfg)
            self.assertEqual(got["mode"], mode, kw)
            self.assertEqual(got["assist_paper"], assist_paper, kw)
            self.assertEqual(got["symbol"], "XAUUSDc")
            self.assertEqual(got["interval_sec"], 30)

    def test_summary_exposes_prefs(self):
        """/api/autotrade 的响应里必须带上 prefs，否则前端无从回填。"""
        prefs.save_prefs({"symbol": "XAUUSDc", "mode": "assist",
                          "interval_sec": 30, "assist_paper": True},
                         symbols=SYMBOLS)
        d = autotrade.summary()
        self.assertIn("prefs", d)
        self.assertEqual(d["prefs"]["mode"], "assist")
        self.assertEqual(d["prefs"]["symbol"], "XAUUSDc")
        self.assertEqual(d["prefs"]["interval_sec"], 30)

    # ------------------------------------------------- 老用户的一次性迁移

    def test_migrates_from_last_run_cfg(self):
        """prefs 文件还不存在时，用 state 里上一场的 cfg 当初始值。

        升级前用户已经跑过若干场，state 里躺着他上次真用的品种/模式/间隔，
        但从来没有地方记过「界面默认值」。不迁移的话，新版打开仍是出厂默认
        （真实做单 / 60 秒），他还要手动再选一遍——那正是这个功能要消灭的动作。
        """
        autotrade.save_state({
            "running": False,
            "cfg": {"symbol": "XAUUSDc", "interval_sec": 30,
                    "dry_run": True, "assist": True},
        }, "999")
        d = autotrade.summary()
        self.assertEqual(d["prefs"]["symbol"], "XAUUSDc")
        self.assertEqual(d["prefs"]["interval_sec"], 30)
        self.assertEqual(d["prefs"]["mode"], "assist")
        self.assertTrue(d["prefs"]["assist_paper"])
        # 迁移只是"读出来当默认值"，不该顺手写盘——用户真改了才写。
        self.assertFalse(prefs.prefs_file().exists())

    def test_explicit_prefs_wins_over_last_run_cfg(self):
        """有 prefs 文件时以它为准，不能被老 state 盖掉。

        这条防的是"迁移逻辑每次轮询都生效"，那会让用户改完选择、下一轮
        又被上一场的 cfg 顶回去。
        """
        prefs.save_prefs({"symbol": "XAUUSDm", "interval_sec": 10,
                          "mode": "paper"}, symbols=SYMBOLS)
        autotrade.save_state({
            "running": False,
            "cfg": {"symbol": "XAUUSDc", "interval_sec": 60,
                    "dry_run": False, "assist": True},
        }, "999")
        d = autotrade.summary()
        self.assertEqual(d["prefs"]["symbol"], "XAUUSDm")
        self.assertEqual(d["prefs"]["interval_sec"], 10)
        self.assertEqual(d["prefs"]["mode"], "paper")


class PrefsApiCase(unittest.TestCase):
    """HTTP 契约：路由注册 + 返回体。不连 MT5、不调模型。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        paths.set_root(self._tmp.name)
        self.server = dashboard.Server(("127.0.0.1", 0), dashboard.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        paths.set_root(None)
        self._tmp.cleanup()

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_post_prefs_does_not_need_confirm(self):
        """记默认值不是下单，不能要求 confirm——否则前端的 onchange 得先弹窗。

        同时这条也证明：没有 confirm 时**不会**触发任何启动（返回体里
        没有 running / symbol 那些启动字段）。
        """
        code, body = self.post("/api/autotrade/prefs", {
            "symbol": "XAUUSDc", "interval_sec": 30,
            "mode": "assist", "assist_paper": True,
        })
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"], body)
        self.assertNotIn("running", body)
        got = prefs.load_prefs(SYMBOLS)
        self.assertEqual(got["symbol"], "XAUUSDc")
        self.assertEqual(got["mode"], "assist")
        self.assertTrue(got["assist_paper"])

    def test_post_prefs_reports_bad_value_with_200(self):
        """非法值用 200 + ok=false 报，不用 4xx。

        这是"顺手记一下"，浏览器控制台不该因为用户拖了一下下拉框就红一片；
        前端看 ok 字段决定要不要提示。
        """
        code, body = self.post("/api/autotrade/prefs", {"interval_sec": 7})
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(body.get("errors"))


if __name__ == "__main__":
    unittest.main()
