"""应用内自动更新（tools/updater.py）。

全部 mock 网络：urllib.request.urlopen 抛错或吐假数据，一个真请求都不发。
下载测试用 AQA_DATA_DIR 指向临时目录，让 _update_dir() 跟着换根；
daemon 线程用同步替身，断言才能看到终态。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from tools import updater


class _SyncThread:
    """把 updater 的 daemon 线程换成同步执行，测试才能断言终态。"""

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _sync_threads():
    return mock.patch.object(updater.threading, "Thread", _SyncThread)


def _fake_response(payload: bytes):
    return io.BytesIO(payload)


class TestParseVersion(unittest.TestCase):
    def test_v_prefix(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))

    def test_plain(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))

    def test_prerelease_suffix_ignored(self):
        self.assertEqual(updater.parse_version("v1.2.3-beta2"), (1, 2, 3))

    def test_garbage_and_empty(self):
        self.assertEqual(updater.parse_version("garbage"), ())
        self.assertEqual(updater.parse_version(""), ())


class TestIsNewer(unittest.TestCase):
    def test_strictly_newer(self):
        self.assertTrue(updater.is_newer("v1.1.0", "1.0.0"))

    def test_unequal_length_pads_with_zero(self):
        self.assertTrue(updater.is_newer("v1.1", "1.0.9"))

    def test_same_is_not_newer(self):
        self.assertFalse(updater.is_newer("v1.1.0", "1.1.0"))
        self.assertFalse(updater.is_newer("v1.1", "1.1.0.0"))

    def test_older_is_not_newer(self):
        self.assertFalse(updater.is_newer("v1.0.9", "1.1.0"))

    def test_unparsable_is_never_newer(self):
        self.assertFalse(updater.is_newer("garbage", "1.0.0"))


def _release_json(tag="v1.2.0", assets=None, notes="- fix"):
    body = {"tag_name": tag, "body": notes,
            "assets": assets if assets is not None else [
                {"name": f"AIQuant-Setup-{tag.lstrip('v')}.exe",
                 "browser_download_url": "https://x/setup.exe", "size": 8}]}
    return json.dumps(body).encode("utf-8")


class TestFetchLatest(unittest.TestCase):
    def test_parses_tag_notes_and_asset(self):
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=_fake_response(_release_json())):
            info = updater.fetch_latest()
        self.assertIsNotNone(info)
        self.assertEqual(info["tag"], "v1.2.0")
        self.assertEqual(info["version"], "1.2.0")
        self.assertEqual(info["asset_url"], "https://x/setup.exe")
        self.assertEqual(info["asset_size"], 8)

    def test_no_setup_asset_returns_none(self):
        only_zip = [{"name": "AIQuant-portable.zip", "size": 8}]
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=_fake_response(
                                   _release_json(assets=only_zip))):
            self.assertIsNone(updater.fetch_latest())

    def test_multiple_setup_assets_picks_largest_name(self):
        assets = [{"name": "AIQuant-Setup-1.2.0.exe",
                   "browser_download_url": "https://x/setup-120.exe", "size": 8},
                  {"name": "AIQuant-Setup-1.3.0.exe",
                   "browser_download_url": "https://x/setup-130.exe", "size": 8}]
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=_fake_response(
                                   _release_json(assets=assets))):
            info = updater.fetch_latest()
        self.assertEqual(info["asset_url"], "https://x/setup-130.exe")

    def test_rate_limit_swallows_error(self):
        err = urllib.error.HTTPError(updater.RELEASE_API, 403, "forbidden",
                                     None, io.BytesIO(b""))
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=err):
            self.assertIsNone(updater.fetch_latest())

    def test_network_error_swallows(self):
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=OSError("unreachable")):
            self.assertIsNone(updater.fetch_latest())


class TestDownload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ,
                                    {"AQA_DATA_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        # 干净的初始 state：每条用例自己铺 check 结果
        updater._set("check", status="ok", latest="1.2.0", newer=True,
                     notes="", error=None)
        updater._set("download", status="idle", received=0, total=0, pct=0,
                     file=None, error=None)
        # 默认下载地址（个别用例会自己再铺 urlopen）
        self._info = mock.patch.object(
            updater, "fetch_latest",
            return_value={"tag": "v1.2.0", "version": "1.2.0", "notes": "",
                          "asset_url": "https://x/setup.exe", "asset_size": 8})
        self._info.start()
        self.addCleanup(self._info.stop)

    def _payload(self, size=8):
        resp = io.BytesIO(b"MZ" + b"\x00" * (size - 2))
        it = iter([resp.read(size)])
        fake = mock.MagicMock()
        fake.__enter__.return_value.read = lambda n: next(it, b"")
        fake.__exit__.return_value = False
        return fake

    def test_download_progresses_and_validates(self):
        with _sync_threads(), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  return_value=self._payload(8)):
            res = updater.start_download()
        self.assertTrue(res["ok"])
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["pct"], 100)
        self.assertTrue(updater.apply_ready())

    def test_short_download_is_error_and_file_removed(self):
        # 宣称 8 字节、只给 4 字节 → size 校验失败，半截文件删掉
        with _sync_threads(), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  return_value=self._payload(4)):
            updater.start_download()
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "error")
        self.assertFalse(updater.apply_ready())

    def test_bad_magic_is_error(self):
        fake = mock.MagicMock()
        it = iter([b"NO"])
        fake.__enter__.return_value.read = lambda n: next(it, b"")
        fake.__exit__.return_value = False
        with _sync_threads(), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  return_value=fake):
            updater.start_download()
        self.assertEqual(updater.snapshot()["download"]["status"], "error")

    def test_redownload_clears_old_artifacts(self):
        d = updater._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        stale = d / "AIQuant-Setup-1.1.0.exe"
        stale.write_bytes(b"MZstale")
        with _sync_threads(), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  return_value=self._payload(8)):
            updater.start_download()
        self.assertFalse(stale.exists())

    def test_download_requires_a_fresh_check(self):
        updater._set("check", status="ok", latest="1.0.0", newer=False)
        res = updater.start_download()
        self.assertFalse(res["ok"])


class TestApplyGate(unittest.TestCase):
    def test_source_mode_rejects_apply(self):
        with mock.patch.object(updater.appdirs, "is_frozen", return_value=False):
            self.assertEqual(updater.ui_mode(), "source")
            self.assertFalse(updater.prepare_apply()["ok"])

    def test_frozen_without_unins000_is_portable(self):
        with mock.patch.object(updater.appdirs, "is_frozen", return_value=True), \
                mock.patch.object(updater.Path, "is_file", return_value=False):
            self.assertEqual(updater.ui_mode(), "portable")

    def test_full_mode_needs_ready_download(self):
        updater._set("download", status="idle")
        with mock.patch.object(updater, "ui_mode", return_value="full"):
            pre = updater.prepare_apply()
            self.assertFalse(pre["ok"])
            self.assertIn("未就绪", pre["error"])

    def test_full_mode_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "AIQuant-Setup-1.2.0.exe")
            with open(pkg, "wb") as fh:
                fh.write(b"MZ123456")
            updater._set("download", status="done", file=pkg, total=8)
            with mock.patch.object(updater, "ui_mode", return_value="full"):
                self.assertTrue(updater.prepare_apply()["ok"])

    def test_ready_but_file_deleted_rejects(self):
        updater._set("download", status="done", file="Z:/no/such/file.exe",
                     total=8)
        with mock.patch.object(updater, "ui_mode", return_value="full"):
            self.assertFalse(updater.prepare_apply()["ok"])


class TestStartupCheckNeverBlocks(unittest.TestCase):
    def test_start_check_returns_immediately_and_swallows_errors(self):
        # fetch 里抛任何异常都不许冒到调用线程，只落进 state['error']。
        # 轮询必须留在 mock 作用域内：daemon 线程还活着时撤掉 mock，
        # 它就会去调真的 GitHub API（真网络请求，测试铁律是零外联）。
        updater._set("check", status="idle")
        with mock.patch.object(updater, "fetch_latest",
                               side_effect=RuntimeError("boom")):
            res = updater.start_check(force=True)
            self.assertEqual(res["status"], "checking")
            deadline = time.time() + 5
            while time.time() < deadline:
                if updater.snapshot()["check"]["status"] != "checking":
                    break
                time.sleep(0.05)
        snap = updater.snapshot()
        self.assertEqual(snap["check"]["status"], "error")
        self.assertIn("boom", snap["check"]["error"])


if __name__ == "__main__":
    unittest.main()
