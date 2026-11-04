"""live/backup.py 的测试。全程 set_base(tmp) 隔离，不碰真实账本。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from live import backup


class BackupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        backup.set_base(self.base)
        self.login = "277733809"
        self.src = self.base / "live" / "accounts" / self.login
        self.src.mkdir(parents=True)
        (self.src / "deals.jsonl").write_text('{"ticket":1,"profit":3.5}\n', encoding="utf-8")
        (self.src / "autotrade_state.json").write_text('{"trades":6}', encoding="utf-8")

    def tearDown(self) -> None:
        backup.set_base(None)
        self._tmp.cleanup()

    def _zip_path(self, day: str, login: str | None = None) -> Path:
        return backup.account_backups(login or self.login) / f"{day}.zip"


class TestSnapshot(BackupCase):
    def test_zip_content_matches_source(self):
        r = backup.snapshot(self.login, day="2026-11-05")
        self.assertTrue(r["ok"], r)
        z = self._zip_path("2026-11-05")
        with zipfile.ZipFile(z) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(names, ["autotrade_state.json", "deals.jsonl"])
            self.assertEqual(zf.read("autotrade_state.json").decode(), '{"trades":6}')

    def test_second_call_same_day_is_idempotent(self):
        backup.snapshot(self.login, day="2026-11-05")
        before = self._zip_path("2026-11-05").stat().st_mtime_ns
        r = backup.snapshot(self.login, day="2026-11-05")
        self.assertEqual(r.get("skipped"), "exists")
        self.assertEqual(self._zip_path("2026-11-05").stat().st_mtime_ns, before)

    def test_force_overwrites(self):
        backup.snapshot(self.login, day="2026-11-05")
        (self.src / "deals.jsonl").write_text('{"ticket":2,"profit":-1}\n', encoding="utf-8")
        backup.snapshot(self.login, day="2026-11-05", force=True)
        with zipfile.ZipFile(self._zip_path("2026-11-05")) as zf:
            self.assertIn('"ticket":2', zf.read("deals.jsonl").decode())

    def test_tmp_and_trash_excluded(self):
        (self.src / "x.json.tmp").write_text("half", encoding="utf-8")
        trash = self.src / "_trash"
        trash.mkdir()
        (trash / "old.json").write_text("{}", encoding="utf-8")
        backup.snapshot(self.login, day="2026-11-05")
        with zipfile.ZipFile(self._zip_path("2026-11-05")) as zf:
            self.assertNotIn("x.json.tmp", zf.namelist())
            self.assertFalse(any(n.startswith("_trash/") for n in zf.namelist()))

    def test_no_temp_file_left_behind(self):
        backup.snapshot(self.login, day="2026-11-05")
        d = backup.account_backups(self.login)
        self.assertEqual([p.name for p in d.iterdir()], ["2026-11-05.zip"])

    def test_missing_account_is_skipped_not_raised(self):
        r = backup.snapshot("999999999", day="2026-11-05")
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("skipped"), "no_account")

    def test_oversize_account_skipped(self):
        old = backup.MAX_ACCOUNT_BYTES
        backup.MAX_ACCOUNT_BYTES = 8
        try:
            r = backup.snapshot(self.login, day="2026-11-05")
            self.assertFalse(r["ok"])
            self.assertEqual(r.get("skipped"), "too_large")
            self.assertFalse(self._zip_path("2026-11-05").exists())
        finally:
            backup.MAX_ACCOUNT_BYTES = old

    def test_only_numeric_account_dirs_are_backed_up(self):
        for name in ("smoke", "__pycache__"):
            (self.base / "live" / "accounts" / name).mkdir(parents=True)
        self.assertEqual(backup.real_logins(), [self.login])


class TestGlobal(BackupCase):
    def test_global_collects_index_profile_packs(self):
        live = self.base / "live"
        (live / "index.json").write_text('{"a":1}', encoding="utf-8")
        (live / "profile.json").write_text('{"symbol":"XAUUSDm"}', encoding="utf-8")
        packs = live / "lesson_packs"
        packs.mkdir()
        (packs / "p1.json").write_text("{}", encoding="utf-8")
        r = backup.snapshot(backup.GLOBAL, day="2026-11-05")
        self.assertTrue(r["ok"], r)
        with zipfile.ZipFile(self._zip_path("2026-11-05", backup.GLOBAL)) as zf:
            self.assertEqual(sorted(zf.namelist()),
                             ["index.json", "lesson_packs/p1.json", "profile.json"])

    def test_global_without_files_is_skipped(self):
        r = backup.snapshot(backup.GLOBAL, day="2026-11-05")
        self.assertEqual(r.get("skipped"), "empty")


class TestPrune(BackupCase):
    def test_keeps_latest_14(self):
        for i in range(1, 21):
            backup.snapshot(self.login, day=f"2026-10-{i:02d}")
        removed = backup.prune(self.login)
        left = sorted(p.name for p in backup.account_backups(self.login).iterdir())
        self.assertEqual(len(left), 14)
        self.assertEqual(left[0], "2026-10-07.zip")     # 1~6 被淘汰
        self.assertEqual(left[-1], "2026-10-20.zip")
        self.assertEqual(len(removed), 6)

    def test_hand_made_files_never_deleted(self):
        d = backup.account_backups(self.login)
        d.mkdir(parents=True)
        (d / "README.txt").write_text("手工放的说明", encoding="utf-8")
        (d / "2026-10-01.zip").write_bytes(b"zip")
        backup.prune(self.login, keep=0)
        self.assertTrue((d / "README.txt").exists())
        self.assertFalse((d / "2026-10-01.zip").exists())

    def test_order_by_name_not_mtime(self):
        """手工拷贝会改 mtime，淘汰顺序必须只认文件名。"""
        for day in ("2026-10-01", "2026-10-02"):
            backup.snapshot(self.login, day=day)
        # 把旧的那份 mtime 改成最新，模拟从别的机器拷回来
        old = self._zip_path("2026-10-01")
        new = self._zip_path("2026-10-02")
        old.touch()
        backup.prune(self.login, keep=1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class TestRestore(BackupCase):
    def test_requires_explicit_confirm(self):
        backup.snapshot(self.login, day="2026-11-05")
        r = backup.restore(self.login, "2026-11-05")
        self.assertFalse(r["ok"])
        self.assertIn("yes", r["error"])

    def test_restore_moves_current_to_trash(self):
        backup.snapshot(self.login, day="2026-11-05")
        (self.src / "deals.jsonl").write_text('{"ticket":99,"profit":-99}\n', encoding="utf-8")
        (self.src / "extra.json").write_text("{}", encoding="utf-8")   # 备份里没有的新文件
        r = backup.restore(self.login, "2026-11-05", yes=True)
        self.assertTrue(r["ok"], r)
        self.assertIn('"ticket":1', (self.src / "deals.jsonl").read_text(encoding="utf-8"))
        self.assertFalse((self.src / "extra.json").exists())   # 不能"恢复一半"
        moved = Path(r["previous_moved_to"])
        self.assertTrue(moved.is_dir())
        self.assertIn('"ticket":99', (moved / "deals.jsonl").read_text(encoding="utf-8"))

    def test_restore_missing_backup(self):
        r = backup.restore(self.login, "1999-01-01", yes=True)
        self.assertFalse(r["ok"])

    def test_rejects_path_traversal_member(self):
        z = self._zip_path("2026-11-05")
        z.parent.mkdir(parents=True)
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../escape.txt", "x")
        r = backup.restore(self.login, "2026-11-05", yes=True)
        self.assertFalse(r["ok"])
        self.assertFalse((self.base / "live" / "escape.txt").exists())


class TestStatusAndAll(BackupCase):
    def test_status(self):
        backup.snapshot(self.login, day="2026-11-05")
        s = backup.status(self.login)[self.login]
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["latest"], "2026-11-05")
        self.assertGreater(s["bytes"], 0)

    def test_ensure_all_covers_accounts_and_global(self):
        (self.base / "live" / "index.json").write_text("{}", encoding="utf-8")
        out = backup.ensure_all_today(day="2026-11-05")
        self.assertEqual({r["login"] for r in out}, {self.login, backup.GLOBAL})


if __name__ == "__main__":
    unittest.main()
