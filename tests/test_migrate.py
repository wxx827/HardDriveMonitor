import os
import tempfile
import unittest
from unittest import mock

from core import migrate
from core.index import Index
from core.scanner import DirRec
from core.util import GB


class MigrateTests(unittest.TestCase):
    """在同一个临时目录里演练迁移（跨盘检查通过 mock 放行）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_mig_")
        self.tmp = self._tmp.name
        self.app_dir = os.path.join(self.tmp, "app")
        os.makedirs(self.app_dir)
        self.src = os.path.join(self.tmp, "src", "BigCache")
        os.makedirs(os.path.join(self.src, "sub"))
        for name, size in (("a.bin", 4096), (os.path.join("sub", "b.bin"), 1000)):
            with open(os.path.join(self.src, name), "wb") as fh:
                fh.write(b"x" * size)
        self.dst = os.path.join(self.tmp, "dst", "BigCache")
        self.log = os.path.join(self.tmp, "migrations.json")
        patcher = mock.patch("core.migrate.check", return_value=[])
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        if migrate.is_reparse_point(self.src):
            os.rmdir(self.src)
        self._tmp.cleanup()

    def test_dry_run_changes_nothing(self):
        steps = migrate.migrate(self.src, self.dst, self.app_dir, dry_run=True)
        self.assertTrue(steps)
        self.assertFalse(os.path.exists(self.dst))
        self.assertFalse(migrate.is_reparse_point(self.src))

    def test_migrate_and_undo(self):
        record = migrate.migrate(self.src, self.dst, self.app_dir, log_path=self.log)
        self.assertTrue(migrate.is_reparse_point(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "sub", "b.bin")))
        with open(os.path.join(self.src, "a.bin"), "rb") as fh:
            self.assertEqual(len(fh.read()), 4096)
        self.assertTrue(os.path.isdir(record.backup))
        self.assertEqual(migrate.load_records(self.log)[0].id, record.id)

        migrate.undo(record, log_path=self.log)
        self.assertFalse(migrate.is_reparse_point(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "sub", "b.bin")))
        self.assertFalse(os.path.exists(self.dst))
        self.assertFalse(os.path.exists(record.backup))
        self.assertEqual(migrate.load_records(self.log)[0].state, "undone")

    def test_junction_failure_rolls_back(self):
        with mock.patch("core.migrate._create_junction", side_effect=OSError("boom")):
            with self.assertRaises(migrate.MigrationError):
                migrate.migrate(self.src, self.dst, self.app_dir)
        self.assertFalse(migrate.is_reparse_point(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "a.bin")))
        self.assertFalse(os.path.exists(self.dst))
        self.assertFalse(os.path.exists(self.src + migrate.BACKUP_SUFFIX))

    def test_verify_mismatch_rolls_back(self):
        real_measure = migrate.scanner.measure_dir
        calls = {"n": 0}

        def fake_measure(path, cancel=None):
            calls["n"] += 1
            size, count = real_measure(path)
            return (size + 1, count) if calls["n"] == 2 else (size, count)

        with mock.patch("core.migrate.scanner.measure_dir", side_effect=fake_measure):
            with self.assertRaises(migrate.MigrationError):
                migrate.migrate(self.src, self.dst, self.app_dir)
        self.assertFalse(os.path.exists(self.dst))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "a.bin")))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_chk_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_drive_rejected(self):
        src = os.path.join(self.tmp, "a")
        os.makedirs(src)
        problems = migrate.check(src, os.path.join(self.tmp, "b"), self.tmp + "\\app")
        self.assertTrue(any("同一个磁盘" in p for p in problems))

    def test_target_inside_source_rejected(self):
        src = os.path.join(self.tmp, "a")
        os.makedirs(src)
        problems = migrate.check(src, os.path.join(src, "x"), self.tmp + "\\app")
        self.assertTrue(any("内部" in p for p in problems))

    def test_missing_source(self):
        self.assertEqual(migrate.check(os.path.join(self.tmp, "nope"), "D:\\x", self.tmp), ["源目录不存在"])

    def test_default_target(self):
        self.assertEqual(migrate.default_target(r"C:\Users\me\.cache", r"D:\已迁移"), r"D:\已迁移\C\Users\me\.cache")


class SuggestTests(unittest.TestCase):
    def test_prefers_dominant_child_and_skips_shell_folders(self):
        tmp = tempfile.TemporaryDirectory(prefix="hdm_sug_")
        self.addCleanup(tmp.cleanup)
        index = Index(os.path.join(tmp.name, "index.db"))
        self.addCleanup(index.close)
        profile = r"C:\Users\me"
        rows = [
            DirRec(profile + r"\.cache", profile, "C:", 4, 30 * GB, 10, 0),
            DirRec(profile + r"\.cache\huggingface", profile + r"\.cache", "C:", 5, 28 * GB, 5, 0),
            DirRec(profile + r"\Documents", profile, "C:", 4, 50 * GB, 5, 0),
            DirRec(profile + r"\AppData", profile, "C:", 4, 40 * GB, 5, 0),
            DirRec(profile + r"\AppData\Local\JetBrains", profile + r"\AppData\Local", "C:", 6, 8 * GB, 5, 0),
        ]
        index.replace_scan([], rows, [], [])
        with mock.patch("core.migrate.os.path.isdir", return_value=True), \
                mock.patch("core.migrate.is_reparse_point", return_value=False):
            result = [r["path"] for r in migrate.suggest(index, profile=profile)]
        self.assertEqual(result, [profile + r"\.cache\huggingface", profile + r"\AppData\Local\JetBrains"])


if __name__ == "__main__":
    unittest.main()
