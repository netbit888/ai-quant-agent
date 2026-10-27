"""应用内自动更新（tools/updater.py）。

全部 mock 网络：urllib.request.urlopen 抛错或吐假数据，一个真请求都不发。
下载测试用 AQA_DATA_DIR 指向临时目录，让 _update_dir() 跟着换根；
daemon 线程用同步替身，断言才能看到终态。
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools import updater


class _SyncThread:
    """把 updater 的 daemon 线程换成同步执行，测试才能断言终态。

    join() 必须是 no-op：start() 已经把活干完了，分片下载会在 start 之后
    join 每一段线程。
    """

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)

    def join(self, timeout=None):
        return None


def _sync_threads():
    return mock.patch.object(updater.threading, "Thread", _SyncThread)


def _fake_response(payload: bytes):
    return io.BytesIO(payload)


class _FakeResp:
    """真·响应替身，有 status / headers / read(n)。

    以前这里用 MagicMock，它的 .status 也是 MagicMock——分片逻辑一要
    ``resp.status != 206`` 就炸。凡是"读响应对象的属性"的代码都别用 MagicMock 糊。
    """

    def __init__(self, payload: bytes, status: int = 200, headers=None):
        self._buf = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Server:
    """按 Range 头派发切片，模拟 GitHub 资产的 206 行为。

    ``honour_range=False`` 造"边缘节点无视 Range、回 200 全量体"的场景。
    ``fail_ranges={'2-3': 2}`` 造"这一段先失败 N 次再成功"，用来验重试与续传。
    """

    def __init__(self, payload: bytes, honour_range: bool = True, fail_ranges=None):
        self.payload = payload
        self.honour_range = honour_range
        self.fail_ranges = dict(fail_ranges or {})
        self.seen: list[str] = []          # 每次请求带的 Range 头，按序
        self.timeouts: list = []           # 每次请求传的 timeout，按序

    def __call__(self, req, timeout=None):
        self.timeouts.append(timeout)
        rng = ""
        for k, v in req.headers.items():
            if k.lower() == "range":
                rng = v
        self.seen.append(rng)
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng or "")
        if not m or not self.honour_range:
            return _FakeResp(self.payload, 200,
                             {"Content-Length": str(len(self.payload))})
        start, end = int(m.group(1)), int(m.group(2))
        if self.fail_ranges.get(f"{start}-{end}", 0) > 0:
            self.fail_ranges[f"{start}-{end}"] -= 1
            raise OSError(f"连接被重置 {start}-{end}")
        end = min(end, len(self.payload) - 1)
        body = self.payload[start:end + 1]
        return _FakeResp(body, 206, {
            "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
            "Content-Length": str(len(body))})


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

    def test_asset_digest_becomes_sha(self):
        """GitHub 新版资产带 digest="sha256:<hex>"，取出来统一小写。"""
        assets = [{"name": "AIQuant-Setup-1.2.0.exe",
                   "browser_download_url": "https://x/setup.exe", "size": 8,
                   "digest": "sha256:ABCDEF0123"}]
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=_fake_response(
                                   _release_json(assets=assets))):
            info = updater.fetch_latest()
        self.assertEqual(info["asset_sha"], "abcdef0123")

    def test_missing_digest_is_empty_string(self):
        """1.5.4 之前上传的老资产没有 digest —— 必须静默降级，不许当错误。"""
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=_fake_response(_release_json())):
            info = updater.fetch_latest()
        self.assertEqual(info["asset_sha"], "")

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
    PAYLOAD = b"MZ" + bytes(range(6))       # 8 字节，后 6 字节互不相同，好比对

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ,
                                    {"AQA_DATA_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        # 退避等待在测试里一律归零，否则光 sleep 就要跑十几秒
        self._base = mock.patch.object(updater, "RETRY_BASE", 0.0)
        self._base.start()
        self.addCleanup(self._base.stop)
        # 干净的初始 state：每条用例自己铺 check 结果
        updater._set("check", status="ok", latest="1.2.0", newer=True,
                     notes="", error=None)
        updater._set("download", status="idle", received=0, total=0, pct=0,
                     file=None, error=None, segments=0, retries=0)
        # 默认下载地址（个别用例会自己再铺 urlopen）
        self._info = mock.patch.object(
            updater, "fetch_latest",
            return_value={"tag": "v1.2.0", "version": "1.2.0", "notes": "",
                          "asset_url": "https://x/setup.exe", "asset_size": 8,
                          "asset_sha": ""})
        self._info.start()
        self.addCleanup(self._info.stop)

    # -- 工具 ------------------------------------------------------------
    def _dest(self, name="AIQuant-Setup-1.2.0.exe") -> Path:
        return updater._update_dir() / name

    def _run(self, server, consts=None, pick=None):
        """跑一次完整下载。

        ``consts`` 临时改常量（SEG_MIN_BYTES 之类）；``pick`` 钉死分片档位——
        档位来自真实时钟采样（8 字节体测出来的吞吐纯属噪声），测试不许依赖它。
        """
        stack = [mock.patch.object(updater.urllib.request, "urlopen",
                                   side_effect=server)]
        for k, v in (consts or {}).items():
            stack.append(mock.patch.object(updater, k, v))
        if pick is not None:
            stack.append(mock.patch.object(updater, "_pick_segments",
                                           return_value=pick))
        for s in stack:
            s.start()
        try:
            return updater.start_download()
        finally:
            for s in reversed(stack):
                s.stop()

    SEG_SMALL = {"SEG_MIN_BYTES": 8}    # 让 8 字节的假包也走分段

    # -- 单流（老路径）---------------------------------------------------
    def test_download_progresses_and_validates(self):
        with _sync_threads():
            res = self._run(_Server(self.PAYLOAD))
        self.assertTrue(res["ok"])
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["pct"], 100)
        self.assertEqual(snap["download"]["segments"], 1)
        self.assertTrue(updater.apply_ready())

    def test_short_download_is_error_and_file_removed(self):
        # 宣称 8 字节、只给 4 字节 → size 校验失败，半截文件删掉
        with _sync_threads():
            self._run(_Server(self.PAYLOAD[:4]))
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "error")
        self.assertFalse(updater.apply_ready())
        self.assertFalse(self._dest().exists())

    def test_bad_magic_is_error(self):
        with _sync_threads():
            self._run(_Server(b"NO______"))
        self.assertEqual(updater.snapshot()["download"]["status"], "error")
        self.assertFalse(self._dest().exists())

    def test_redownload_clears_old_artifacts(self):
        d = updater._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        stale = d / "AIQuant-Setup-1.1.0.exe"
        stale.write_bytes(b"MZstale")
        with _sync_threads():
            self._run(_Server(self.PAYLOAD))
        self.assertFalse(stale.exists())

    def test_download_requires_a_fresh_check(self):
        updater._set("check", status="ok", latest="1.0.0", newer=False)
        res = updater.start_download()
        self.assertFalse(res["ok"])

    # -- 分片 -------------------------------------------------------------
    def test_segmented_download_assembles_all_parts(self):
        server = _Server(self.PAYLOAD)
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["segments"], 4)
        self.assertEqual(self._dest().read_bytes(), self.PAYLOAD)
        self.assertEqual(list(updater._update_dir().glob("*.part*")), [])
        # 探测 1 次 + 4 段各 1 次
        self.assertEqual(server.seen.count(""), 0)

    def test_falls_back_to_single_stream_without_range(self):
        """边缘节点无视 Range 回 200 全量体时必须退单流，不能硬拼坏文件。"""
        server = _Server(self.PAYLOAD, honour_range=False)
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["segments"], 1)
        self.assertEqual(self._dest().read_bytes(), self.PAYLOAD)
        self.assertEqual(list(updater._update_dir().glob("*.part*")), [])

    def test_segment_count_flows_into_the_plan(self):
        """_pick_segments 选几档，就真的按几段下（不是选完扔了）。"""
        with _sync_threads(), \
                mock.patch.object(updater, "_pick_segments", return_value=8):
            self._run(_Server(self.PAYLOAD), consts=self.SEG_SMALL, pick=8)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["segments"], 8)
        self.assertEqual(self._dest().read_bytes(), self.PAYLOAD)

    def test_probe_writes_canary_into_part0(self):
        """测速样本必须落进 part0 —— 第 0 段从字节 0 开始，白拿的进度不能扔。"""
        dest = self._dest()
        part0 = dest.with_name(dest.name + updater.PART_SUFFIX + "0")
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=_Server(self.PAYLOAD)):
            ok, kbps = updater._probe_and_measure("https://x/a.exe",
                                                  len(self.PAYLOAD), part0)
        self.assertTrue(ok)
        self.assertGreater(kbps, 0)
        self.assertEqual(part0.read_bytes(), self.PAYLOAD)

    def test_probe_uses_short_timeout(self):
        """探针只握手+读样本；30 秒超时会让死链路白等两分钟才报错。"""
        server = _Server(self.PAYLOAD)
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=server):
            updater._probe_once("https://x/a.exe", len(self.PAYLOAD),
                                self._dest("p0"))
        self.assertEqual(server.timeouts[0], updater.PROBE_TIMEOUT)
        self.assertLess(updater.PROBE_TIMEOUT, updater.DOWNLOAD_TIMEOUT)

    def test_probe_try_counter_visible_to_ui(self):
        """前端靠 probe_try 把"还没连上"和"下着没速度"分开，必须递增。"""
        server = _Server(self.PAYLOAD, fail_ranges={"0-262143": 1})
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()["download"]
        self.assertEqual(snap["status"], "done")
        self.assertGreaterEqual(snap["probe_try"], 2)

    def test_probe_transient_failure_retries_then_segments(self):
        """探针被链路掐一次不许降级成单流——那是最慢的路。"""
        server = _Server(self.PAYLOAD, fail_ranges={"0-262143": 1})
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["segments"], 4)

    def test_probe_hard_failure_is_error_not_silent_stream(self):
        """探针彻底连不上就报错（分片留着、能点重试），不许假装降级去跑单流。"""
        server = _Server(self.PAYLOAD, fail_ranges={"0-262143": 99})
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "error")
        self.assertEqual(snap["download"]["segments"], 0)

    def test_stream_path_retries_before_failing(self):
        """不支持 Range 时走单流，但单流也得有重试。"""
        class _Flaky(_Server):
            def __call__(self, req, timeout=None):
                self.seen.append("")
                if len(self.seen) <= 2:
                    raise OSError("连接被重置")
                return _FakeResp(self.payload, 200)

        with _sync_threads():
            self._run(_Flaky(self.PAYLOAD))
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["segments"], 1)
        self.assertEqual(self._dest().read_bytes(), self.PAYLOAD)

    def test_segment_retry_succeeds_and_counts(self):
        server = _Server(self.PAYLOAD, fail_ranges={"2-3": 1})
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "done")
        self.assertEqual(snap["download"]["retries"], 1)
        self.assertEqual(self._dest().read_bytes(), self.PAYLOAD)

    def test_segment_retry_exhausted_keeps_parts(self):
        """重试耗尽：dest 绝不能存在，但已下好的段必须留着给下次续。"""
        server = _Server(self.PAYLOAD, fail_ranges={"2-3": 99})
        with _sync_threads():
            self._run(server, consts=self.SEG_SMALL, pick=4)
        snap = updater.snapshot()
        self.assertEqual(snap["download"]["status"], "error")
        self.assertFalse(self._dest().exists())
        self.assertFalse(updater.apply_ready())
        kept = list(updater._update_dir().glob("*.part*"))
        self.assertEqual(len(kept), 3)                 # 0/2/3 段保住了
        self.assertEqual(sum(p.stat().st_size for p in kept), 6)

    # -- 校验 -------------------------------------------------------------
    def test_sha_mismatch_wipes_everything(self):
        dest = self._dest()
        with _sync_threads(), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  side_effect=_Server(self.PAYLOAD)):
            updater._do_download("https://x/setup.exe", len(self.PAYLOAD), dest,
                                 "00" * 32)
        self.assertEqual(updater.snapshot()["download"]["status"], "error")
        self.assertIn("sha256", updater.snapshot()["download"]["error"])
        self.assertFalse(dest.exists())
        self.assertEqual(list(updater._update_dir().glob("*.part*")), [])

    # -- 续传 -------------------------------------------------------------
    def test_fetch_segment_resumes_from_existing_bytes(self):
        """分片文件里已有 2 字节 → 必须从缺口要，不是从 0 重下。"""
        part = self._dest("AIQuant-Setup-9.9.9.exe.part1")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(self.PAYLOAD[:2])
        state = {"total": len(self.PAYLOAD), "heights": {}, "retries": 0,
                 "lock": threading.Lock()}
        server = _Server(self.PAYLOAD)
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=server):
            updater._fetch_segment("https://x/a.exe", 0, 5, part, state)
        self.assertIn("bytes=2-5", server.seen)
        self.assertEqual(part.read_bytes(), self.PAYLOAD[:6])
        self.assertEqual(state["heights"][part.name], 6)

    def test_parts_are_ordered_by_number_not_lexically(self):
        """分片数 >9 时字典序会把 part10 排到 part2 前面，拼装即坏文件。"""
        d = updater._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        dest = d / "AIQuant-Setup-1.2.0.exe"
        for i in (2, 10, 1):
            (d / f"{dest.name}.part{i}").write_bytes(b"x")
        order = [p.name.rsplit(".part", 1)[1] for p in updater._parts_of(dest)]
        self.assertEqual(order, ["1", "2", "10"])

    def test_clear_dir_keeps_only_current_parts(self):
        d = updater._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        keep = d / "AIQuant-Setup-1.2.0.exe.part0"
        drop = d / "AIQuant-Setup-1.1.0.exe.part0"
        tmp = d / "AIQuant-Setup-1.2.0.exe.tmp"
        for p in (keep, drop, tmp):
            p.write_bytes(b"MZxx")
        updater._clear_dir(d, keep_parts_of="AIQuant-Setup-1.2.0.exe")
        self.assertTrue(keep.exists())     # 同一版本的段留着续传
        self.assertFalse(drop.exists())    # 别的版本的死数据清掉
        self.assertFalse(tmp.exists())
        updater._clear_dir(d)              # 启动扫尾：连段一起清
        self.assertFalse(keep.exists())


class TestErrorText(unittest.TestCase):
    """错误文案要让人看懂，原文保留在括号里给排查用。"""

    def test_handshake_timeout(self):
        exc = OSError("_ssl.c:1015: The handshake operation timed out")
        msg = updater._brief_net_error(exc)
        self.assertIn("连接超时", msg)
        self.assertIn("handshake", msg)      # 原文不能丢

    def test_connection_reset(self):
        msg = updater._brief_net_error(OSError("Remote end closed connection"))
        self.assertIn("连接被中断", msg)

    def test_unknown_keeps_type_name(self):
        msg = updater._brief_net_error(ValueError("boom"))
        self.assertIn("ValueError", msg)


class TestSegmentPlan(unittest.TestCase):
    def test_segments_cover_every_byte_exactly_once(self):
        for total in (4 * 1024 * 1024, 11_000_000, 23_820_281):
            for n in (4, 8, 16):
                segs = updater._plan_segments(total, n)
                self.assertEqual(segs[0][0], 0)
                self.assertEqual(segs[-1][1], total - 1)
                for (a, b), (c, _) in zip(segs, segs[1:]):
                    self.assertEqual(b + 1, c, "段之间不许有缝隙或重叠")
                self.assertEqual(sum(b - a + 1 for a, b in segs), total)

    def test_below_threshold_uses_single_segment(self):
        self.assertEqual(len(updater._plan_segments(1024, 1)), 1)

    def test_segment_count_never_exceeds_byte_count(self):
        """1 字节的破包按 16 段切会切出 0 长度的段，必须塌到 1 段。"""
        self.assertEqual(len(updater._plan_segments(3, 16)), 3)


class TestPickSegments(unittest.TestCase):
    """档位判据只分三档。真要动阈值，先想清楚是不是又在拿一条网拟合。"""

    def test_fast_link_keeps_four(self):
        self.assertEqual(updater._pick_segments(900), updater.SEGMENTS)
        self.assertEqual(updater._pick_segments(updater.FAST_KBPS), updater.SEGMENTS)

    def test_mid_link_uses_eight(self):
        self.assertEqual(updater._pick_segments(200), 8)
        self.assertEqual(updater._pick_segments(updater.MID_KBPS), 8)

    def test_throttled_link_uses_max(self):
        self.assertEqual(updater._pick_segments(15), updater.SEGMENTS_MAX)
        self.assertEqual(updater._pick_segments(updater.MID_KBPS - 1),
                         updater.SEGMENTS_MAX)


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


class TestUpdateDialogMarkup(unittest.TestCase):
    """更新弹窗必须是「日志能滚、按钮常在」的结构。

    2026-10-27 事故：release notes 写了两千多字，弹窗跟着内容长高，超过视口后
    「下载并安装」被顶到屏幕外面，用户点不到——**更新通道被自己的更新日志堵死**。
    这三个断言锁住修法，别让谁把 class 或滚动容器改回去。
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        cls.html = (Path(__file__).resolve().parents[1]
                    / "tools" / "dashboard_template.html").read_text(encoding="utf-8")

    def _block(self, selector: str) -> str:
        # 取某条 CSS 规则的声明块（够用即可，不引 CSS 解析器）
        return self.html.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_dialog_is_capped_and_column(self):
        box = self._block(".modal-update")
        self.assertIn("max-height", box)
        self.assertIn("flex-direction: column", box)

    def test_notes_area_scrolls(self):
        desc = self._block(".modal-update .desc")
        self.assertIn("overflow-y: auto", desc)
        self.assertIn("max-height", desc)     # 兜底：flex 失效也不会顶破弹窗

    def test_actions_do_not_scroll(self):
        act = self._block(".modal-update .actions")
        self.assertIn("flex: none", act)

    def test_markup_uses_the_class(self):
        self.assertIn('class="modal modal-update"', self.html)

    def test_notes_markdown_is_stripped(self):
        """release notes 是 markdown，弹窗是纯文本——不降级会满屏 ** 和 | 。"""
        self.assertIn("function notesPlain", self.html)
        self.assertIn("notesPlain(j.check.notes)", self.html)

    def test_progress_shows_rate_and_connections(self):
        """这次下载提速的全部意义就是"更快"，用户得看得见。"""
        self.assertIn("function kbpsText", self.html)
        self.assertIn("kbpsText(dlRate(d.received))", self.html)
        self.assertIn("d.segments > 1", self.html)

    def test_zero_bytes_says_connecting_not_stalled(self):
        """0 字节时报"停滞"是错的信息：真实状态是连不上、正在重试。

        2026-10-27 真机截图：链路的 TLS 握手被掐，弹窗写着「下载中 0% · 停滞…」，
        看起来像下载卡死，实际是一个字节都没拿到。
        """
        self.assertIn("正在连接", self.html)
        self.assertIn("d.probe_try > 1", self.html)
        self.assertIn("d.received > 0", self.html)

    def test_polling_cap_is_not_ten_minutes(self):
        """10 分钟硬上限 = 后端还在下、界面却停止刷新，用户以为卡死重来。

        v1.5.5 之前就是这个值，白丢已下字节。想改回去先想清楚这条。
        """
        self.assertNotIn("10 * 60 * 1000", self.html)
        self.assertIn("60 * 60 * 1000", self.html)


if __name__ == "__main__":
    unittest.main()
