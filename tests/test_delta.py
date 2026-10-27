"""增量更新（tools/make_delta.py + updater 的 delta 分支）。

两条铁律 inherited from test_updater.py：网络零外联、daemon 线程换同步替身。
本文件额外做到：**不依赖 dist\\ 产物与 Inno Setup**——PyInstaller exe 用
_make_fake_exe 现场构造（格式按 PyInstaller readers.py 手工复刻，50 行换
测试独立性，值）；ISCC 用 mock 替身。
"""

from __future__ import annotations

import io
import json
import marshal
import os
import struct
import subprocess
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest import mock

from tools import make_delta as md
from tools import updater
from test_updater import _sync_threads  # 同一项目同一替身，不另写一份


# ---------------------------------------------------------------- 假 exe

def _make_fake_exe(path: Path, version: str) -> None:
    """构造一个能被 read_exe_version 读出版本的最小 PyInstaller 布局。

    布局（按 tools/make_delta.py 文档注释，源头是 PyInstaller readers.py）：
        [bootloader 占位][PYZ blob][TOC][cookie]
    PYZ 头 17 字节：magic(4)+bytecode magic(4)+toc 偏移(4)+保留(5)。
    """
    code = compile(f"APP_VERSION = {version!r}\n", "app_version.py", "exec")
    blob = zlib.compress(marshal.dumps(code))
    PYZ_HEADER = 17  # magic(4)+bytecode magic(4)+toc 偏移(4)+保留(5)
    pyz_toc = marshal.dumps([("app_version", (0, PYZ_HEADER, len(blob)))])
    # PYZ 头：magic + bytecode magic + toc 偏移（相对 blob 起点）
    pyz = b"PYZ\x00" + b"abcd" + struct.pack("!i", PYZ_HEADER + len(blob)) + b"\0" * 5
    pyz += blob + pyz_toc

    # TOC：唯一条目 PYZ.pyz（compression_flag=0 → raw 存储）
    name = b"PYZ.pyz"
    entry_length = 18 + len(name)
    toc_entry = struct.pack("!IIII", entry_length, 0, len(pyz), len(pyz)) \
        + b"\x00" + b"z" + name
    toc = toc_entry

    body = b"B" * 100 + pyz + toc  # bootloader 占位 100 字节
    # cookie 里的 pkg_len/toc_off 都是**相对 pkg 起点**（= 占位之后）：
    # pkg = pyz + toc + cookie，pkg_len 从 pkg_start 覆盖到文件尾
    pkg_len = len(pyz) + len(toc) + 88
    toc_off = len(pyz)
    cookie = b"MEI\x0c\x0b\x0a\x0b\x0e" \
        + struct.pack("!III", pkg_len, toc_off, len(toc)) \
        + struct.pack("!I", 313) + b"python313.dll".ljust(64, b"\0")
    path.write_bytes(body + cookie)


def _make_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


# ---------------------------------------------------------------- updater 侧

def _release_json(tag="v1.5.0", assets=None, notes="- fix"):
    body = {"tag_name": tag, "body": notes,
            "assets": assets if assets is not None else [
                {"name": f"AIQuant-Setup-{tag.lstrip('v')}.exe",
                 "browser_download_url": "https://x/setup.exe", "size": 100}]}
    return json.dumps(body).encode("utf-8")


def _asset(name, size=100, url=None):
    return {"name": name, "browser_download_url": url or f"https://x/{name}",
            "size": size}


CURRENT = updater.APP_VERSION  # 测试里的 delta base 永远用本机真实版本


class TestFetchLatestDelta(unittest.TestCase):
    def _fetch(self, assets):
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=io.BytesIO(_release_json(assets=assets))):
            return updater.fetch_latest()

    def test_delta_asset_is_picked(self):
        info = self._fetch([
            _asset("AIQuant-Setup-1.5.0.exe", 200),
            _asset(f"AIQuant-Delta-{CURRENT}-to-1.5.0.exe", 50),
        ])
        self.assertIsNotNone(info["delta"])
        self.assertEqual(info["delta"]["base"], CURRENT)
        self.assertEqual(info["delta"]["size"], 50)

    def test_delta_base_mismatch_ignored(self):
        info = self._fetch([
            _asset("AIQuant-Setup-1.5.0.exe", 200),
            _asset("AIQuant-Delta-0.9.9-to-1.5.0.exe", 50),
        ])
        self.assertIsNone(info["delta"])

    def test_delta_target_must_match_release(self):
        info = self._fetch([
            _asset("AIQuant-Setup-1.5.0.exe", 200),
            _asset(f"AIQuant-Delta-{CURRENT}-to-1.4.9.exe", 50),
        ])
        self.assertIsNone(info["delta"])

    def test_no_delta_asset(self):
        info = self._fetch([_asset("AIQuant-Setup-1.5.0.exe", 200)])
        self.assertIsNone(info["delta"])

    def test_dirty_tag_in_delta_name_is_ignored(self):
        info = self._fetch([
            _asset("AIQuant-Setup-1.5.0.exe", 200),
            _asset(f"AIQuant-Delta-{CURRENT}-to-1.5.0-beta.exe", 50),
        ])
        self.assertIsNone(info["delta"])

    def test_rate_limit_still_swallows(self):
        err = urllib.error.HTTPError(updater.RELEASE_API, 403, "forbidden",
                                     None, io.BytesIO(b""))
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(updater.fetch_latest())


class TestDecidePlan(unittest.TestCase):
    def test_delta_when_base_matches_and_small(self):
        info = {"asset_size": 200,
                "delta": {"base": CURRENT, "url": "u", "size": 50}}
        self.assertEqual(updater._decide_plan(info), "delta")

    def test_full_when_base_mismatches(self):
        info = {"asset_size": 200,
                "delta": {"base": "0.0.1", "url": "u", "size": 50}}
        self.assertEqual(updater._decide_plan(info), "full")

    def test_full_when_delta_barely_smaller(self):
        info = {"asset_size": 100,
                "delta": {"base": CURRENT, "url": "u", "size": 90}}
        self.assertEqual(updater._decide_plan(info), "full")

    def test_full_without_delta(self):
        self.assertEqual(updater._decide_plan({"asset_size": 200}), "full")

    def test_delta_allowed_when_full_size_unknown(self):
        # 完整包 size 缺失（病态 API 响应）：收益门槛无从谈起，放行 delta
        info = {"asset_size": 0,
                "delta": {"base": CURRENT, "url": "u", "size": 50}}
        self.assertEqual(updater._decide_plan(info), "delta")


class TestDoCheckWritesPlan(unittest.TestCase):
    def tearDown(self):
        updater._set("check", status="idle")

    def test_check_state_carries_plan_fields(self):
        info = {"tag": "v1.5.0", "version": "1.5.0", "notes": "",
                "asset_url": "https://x/s.exe", "asset_size": 200,
                "delta": {"base": CURRENT, "url": "https://x/d.exe", "size": 50}}
        with mock.patch.object(updater, "fetch_latest", return_value=info):
            updater._do_check()
        c = updater.snapshot()["check"]
        self.assertEqual(c["plan"], "delta")
        self.assertEqual(c["delta_base"], CURRENT)
        self.assertEqual(c["delta_size"], 50)
        self.assertEqual(c["full_size"], 200)

    def test_not_newer_forces_full_plan(self):
        info = {"tag": "v0.0.1", "version": "0.0.1", "notes": "",
                "asset_url": "https://x/s.exe", "asset_size": 200,
                "delta": {"base": CURRENT, "url": "https://x/d.exe", "size": 50}}
        with mock.patch.object(updater, "fetch_latest", return_value=info):
            updater._do_check()
        self.assertEqual(updater.snapshot()["check"]["plan"], "full")


class TestDeltaDownload(unittest.TestCase):
    """下载落盘文件名与 plan 一致：装错文件比不装更糟。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"AQA_DATA_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        updater._set("check", status="ok", latest="1.5.0", newer=True,
                     notes="", error=None)
        updater._set("download", status="idle", received=0, total=0, pct=0,
                     file=None, error=None)

    def _payload(self, size=8):
        resp = io.BytesIO(b"MZ" + b"\x00" * (size - 2))
        it = iter([resp.read(size)])
        fake = mock.MagicMock()
        fake.__enter__.return_value.read = lambda n: next(it, b"")
        fake.__exit__.return_value = False
        return fake

    def _run(self, info):
        # 同步替身：daemon 线程不换掉的话下载是异步的，断言看到的是中间态
        with mock.patch.object(updater, "fetch_latest", return_value=info), \
                mock.patch.object(updater.urllib.request, "urlopen",
                                  return_value=self._payload(8)), \
                _sync_threads():
            updater.start_download()
        return updater.snapshot()

    def test_delta_plan_downloads_delta_file(self):
        info = {"tag": "v1.5.0", "version": "1.5.0", "notes": "",
                "asset_url": "https://x/s.exe", "asset_size": 200,
                "delta": {"base": CURRENT, "url": "https://x/d.exe", "size": 8}}
        snap = self._run(info)
        self.assertEqual(snap["check"]["plan"], "delta")
        self.assertTrue(Path(snap["download"]["file"]).name
                        == f"AIQuant-Delta-{CURRENT}-to-1.5.0.exe")
        self.assertTrue(updater.apply_ready())

    def test_full_plan_downloads_setup_file(self):
        info = {"tag": "v1.5.0", "version": "1.5.0", "notes": "",
                "asset_url": "https://x/s.exe", "asset_size": 200,
                "delta": {"base": "0.0.1", "url": "https://x/d.exe", "size": 8}}
        snap = self._run(info)
        self.assertEqual(snap["check"]["plan"], "full")
        self.assertTrue(Path(snap["download"]["file"]).name
                        == "AIQuant-Setup-1.5.0.exe")

    def test_old_delta_leftovers_are_cleared(self):
        d = updater._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        stale = d / f"AIQuant-Delta-{CURRENT}-to-0.0.1.exe"
        stale.write_bytes(b"MZstale")
        info = {"tag": "v1.5.0", "version": "1.5.0", "notes": "",
                "asset_url": "https://x/s.exe", "asset_size": 200}
        self._run(info)
        self.assertFalse(stale.exists())


# ---------------------------------------------------------------- make_delta

class TestReadExeVersion(unittest.TestCase):
    def test_reads_embedded_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "AIQuant.exe"
            _make_fake_exe(exe, "9.9.9")
            self.assertEqual(md.read_exe_version(exe), "9.9.9")

    def test_garbage_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "AIQuant.exe"
            exe.write_bytes(b"not a pyinstaller exe")
            with self.assertRaises(ValueError):
                md.read_exe_version(exe)


class TestTreeDiff(unittest.TestCase):
    def test_classifies_changed_added_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            target = Path(tmp) / "target"
            _make_tree(base, {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})
            _make_tree(target, {"a.txt": b"1", "b.txt": b"2X", "d.txt": b"4"})
            base_m = md.walk_tree(base)
            target_m = md.walk_tree(target)
            changed, added, removed, unchanged = md.diff_trees(base_m, target_m)
            self.assertEqual(changed, ["b.txt"])
            self.assertEqual(added, ["d.txt"])
            self.assertEqual(removed, ["c.txt"])
            self.assertEqual(unchanged, ["a.txt"])

    def test_data_config_log_never_in_manifest(self):
        # 硬断言：打包机的真实账本（data\ config\）与日志绝不进任何列表
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "t"
            _make_tree(t, {
                "AIQuant.exe": b"x",
                "data/live/deals.jsonl": b"ledger",
                "config/.env": b"secret",
                "tools/x.log": b"log",
                "_internal/ok.dll": b"ok",
            })
            m = md.walk_tree(t)
            self.assertEqual(sorted(m), ["AIQuant.exe", "_internal/ok.dll"])


class TestRecord(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run_record(self, version_arg, exe_version="1.4.0"):
        dist = self.root / "dist" / "AIQuant"
        _make_tree(dist, {"AIQuant.exe": b"", "a.txt": b"1"})
        _make_fake_exe(dist / "AIQuant.exe", exe_version)
        snaps = self.root / "snapshots"
        with mock.patch.object(md, "SNAPSHOTS", snaps):
            rc = md.cmd_record(mock.Mock(version=version_arg, dist=str(dist)))
        return rc, snaps

    def test_record_snapshot_and_last_json(self):
        rc, snaps = self._run_record("1.4.0", exe_version="1.4.0")
        self.assertEqual(rc, 0)
        self.assertTrue((snaps / "1.4.0" / "a.txt").is_file())
        self.assertTrue((snaps / "1.4.0" / "AIQuant.exe").is_file())
        last = json.loads((snaps / "last.json").read_text(encoding="utf-8"))
        self.assertEqual(last["version"], "1.4.0")
        manifest = json.loads((snaps / "1.4.0.manifest.json").read_text(encoding="utf-8"))
        self.assertIn("a.txt", manifest["files"])

    def test_record_refuses_version_mismatch(self):
        # 构建了但没发布的树一旦进基线，delta 就会把用户装成混合版本
        rc, snaps = self._run_record("1.5.0", exe_version="1.4.0")
        self.assertEqual(rc, 1)
        self.assertFalse(snaps.exists())

    def test_prune_keeps_recent_three(self):
        rc, snaps = self._run_record("1.4.0", exe_version="1.4.0")
        self.assertEqual(rc, 0)
        for v in ("1.4.1", "1.4.2", "1.4.3", "1.4.4"):
            _make_tree(snaps / v, {"AIQuant.exe": b"", "x": b"1"})
            _make_fake_exe(snaps / v / "AIQuant.exe", v)
        with mock.patch.object(md, "SNAPSHOTS", snaps):
            md._prune_snapshots()
        names = sorted(p.name for p in snaps.iterdir() if p.is_dir())
        # parse_version 排序保留最新 3 份；1.4.0 与 1.4.1 被裁掉
        self.assertEqual(names, ["1.4.2", "1.4.3", "1.4.4"])


class TestDelta(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.snaps = self.root / "snapshots"
        self.stage = self.root / "stage"
        self.iss = self.root / "aqa_delta.iss"
        self.out = self.root / "installer"
        self.out.mkdir()
        # 把生成物的落点全部指进临时目录，仓库永远不被测试触碰
        for attr, val in (("SNAPSHOTS", self.snaps), ("STAGE", self.stage),
                          ("DELTA_ISS", self.iss), ("INSTALLER_OUT", self.out)):
            p = mock.patch.object(md, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def _make_baseline(self, version="1.4.0"):
        base = self.snaps / version
        _make_tree(base, {"AIQuant.exe": b"", "keep.txt": b"k",
                          "live/old.py": b"old"})
        _make_fake_exe(base / "AIQuant.exe", version)
        (self.snaps / "last.json").write_text(json.dumps(
            {"version": version, "path": str(base)}), encoding="utf-8")

    def _make_target(self, version="1.5.0"):
        target = self.root / "dist" / "AIQuant"
        _make_tree(target, {"AIQuant.exe": b"", "keep.txt": b"k",
                            "live/new.py": b"new"})
        _make_fake_exe(target / "AIQuant.exe", version)
        return target

    def test_no_baseline_is_clean_skip(self):
        # 无基线是首次落地的正常状态：退出码 0，发版降级为仅完整包
        rc = md.cmd_delta(mock.Mock(target=str(self._make_target())))
        self.assertEqual(rc, 0)
        self.assertFalse(self.iss.exists())

    def test_untrustworthy_baseline_is_skipped(self):
        # last.json 说 1.4.0、快照 exe 却是 9.9.9：宁可不做也不装坏用户
        self._make_baseline("1.4.0")
        _make_fake_exe(self.snaps / "1.4.0" / "AIQuant.exe", "9.9.9")
        rc = md.cmd_delta(mock.Mock(target=str(self._make_target())))
        self.assertEqual(rc, 0)
        self.assertFalse(self.iss.exists())

    def test_same_version_target_is_skipped(self):
        self._make_baseline("1.5.0")
        rc = md.cmd_delta(mock.Mock(target=str(self._make_target("1.5.0"))))
        self.assertEqual(rc, 0)
        self.assertFalse(self.iss.exists())

    def test_iss_contents_and_staging(self):
        self._make_baseline("1.4.0")
        target = self._make_target("1.5.0")
        # ISCC 用替身：测试不该依赖 Inno Setup 的安装
        fake_iscc = self.root / "iscc.exe"
        fake_iscc.write_bytes(b"")
        recorded_cmds = []

        def fake_run(cmd, **kw):
            recorded_cmds.append(cmd)
            (self.out / "AIQuant-Delta-1.4.0-to-1.5.0.exe").write_bytes(b"MZfake")
            return mock.Mock(returncode=0)

        with mock.patch.object(md, "_find_iscc", return_value=fake_iscc), \
                mock.patch.object(md.subprocess, "run", side_effect=fake_run):
            rc = md.cmd_delta(mock.Mock(target=str(target)))
        self.assertEqual(rc, 0)
        # 暂存区只装"真实变化"的文件
        staged = sorted(p.relative_to(self.stage).as_posix()
                        for p in self.stage.rglob("*") if p.is_file())
        self.assertEqual(staged, ["AIQuant.exe", "live/new.py"])
        text = self.iss.read_text(encoding="ascii")
        # AppId 必须与完整包同源（同产品覆盖升级的前提）：
        # 从 aqa.iss 的 #define 提取 GUID，防两边各自漂移
        full_iss = (md.ROOT / "installer" / "aqa.iss").read_text(encoding="utf-8")
        guid = next(l.split('"')[1] for l in full_iss.splitlines()
                    if l.startswith("#define MyAppId"))
        self.assertIn(f"AppId={guid}", text)
        # 变更文件：真实 Source；未变化文件：external 引用已安装内容
        self.assertIn('Source: "{#Stage}\\live\\new.py"', text)
        self.assertIn('Source: "{app}\\keep.txt"', text)
        self.assertIn("Flags: external ignoreversion", text)
        # 被删除的文件出现在 [InstallDelete]
        self.assertIn("[InstallDelete]", text)
        self.assertIn('Name: "{app}\\live\\old.py"', text)
        # data/config 永不出现在文件条目里（[Dirs] 保留目录属合法例外，
        # 真正要把关的是 [Files] 与 [InstallDelete] 这两个会动文件内容的段）
        def section(name, nxt):
            start = text.index(f"[{name}]")
            return text[start:text.index(f"[{nxt}]")]
        for seg in (section("Files", "Icons"), section("InstallDelete", "Files")):
            self.assertNotIn("\\data\\", seg)
            self.assertNotIn("\\config\\", seg)
        # ISCC 被调用且指向生成的 iss
        self.assertEqual(len(recorded_cmds), 1)
        self.assertTrue(str(self.iss) in recorded_cmds[0])
        self.assertTrue((self.out / "AIQuant-Delta-1.4.0-to-1.5.0.exe").is_file())

    def test_iscc_failure_returns_error(self):
        self._make_baseline("1.4.0")
        target = self._make_target("1.5.0")
        with mock.patch.object(md, "_find_iscc",
                               return_value=self.root / "iscc.exe"), \
                mock.patch.object(md.subprocess, "run",
                                  return_value=mock.Mock(returncode=2)):
            rc = md.cmd_delta(mock.Mock(target=str(target)))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
