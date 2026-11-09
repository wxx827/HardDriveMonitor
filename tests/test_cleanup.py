import os
import tempfile
import unittest
from unittest import mock

from core import cleanup


class ProtectedPathTests(unittest.TestCase):
    def setUp(self):
        self.app_dir = r"C:\Users\wjx\Desktop\硬盘监视器"
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_cln_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_system_dirs(self):
        self.assertIsNotNone(cleanup.protected_reason(r"C:\Windows\System32\cmd.exe", self.app_dir))
        self.assertIsNotNone(cleanup.protected_reason(r"C:\Program Files\a\b.exe", self.app_dir))
        self.assertIsNotNone(cleanup.protected_reason(r"C:\ProgramData\Microsoft\x", self.app_dir))
        self.assertIsNotNone(cleanup.protected_reason(r"D:\Program Files\a\b.exe", self.app_dir))

    def test_rejects_drive_root_and_protected_files(self):
        self.assertIsNotNone(cleanup.protected_reason("C:\\", self.app_dir))
        self.assertIsNotNone(cleanup.protected_reason("C:\\pagefile.sys", self.app_dir))

    def test_rejects_app_dir(self):
        self.assertIsNotNone(cleanup.protected_reason(os.path.join(self.app_dir, "app.py"), self.app_dir))
        self.assertIsNotNone(
            cleanup.protected_reason(os.path.join(self.app_dir, "data", "index.db"), self.app_dir)
        )

    def test_rejects_missing(self):
        self.assertIsNotNone(cleanup.protected_reason(os.path.join(self.tmp, "nope.bin"), self.app_dir))

    def test_allows_temp_file(self):
        path = os.path.join(self.tmp, "a.tmp")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertIsNone(cleanup.protected_reason(path, self.app_dir))

    def test_validate_dedupes(self):
        path = os.path.join(self.tmp, "b.tmp")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        ok, bad = cleanup.validate([path, path, r"C:\Windows\notepad.exe"], self.app_dir)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(bad), 1)

    def test_expand_contents(self):
        for name in ("1.txt", "2.txt"):
            with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        contents = cleanup.expand_contents(self.tmp)
        self.assertEqual(len(contents), 2)

    def test_describe_code(self):
        self.assertIn("访问被拒绝", cleanup._describe_code(5))
        self.assertIn("程序", cleanup._describe_code(32))
        self.assertIn("错误码", cleanup._describe_code(9999))

    def test_delete_report(self):
        report = cleanup.DeleteReport()
        report.deleted.append(("a", 100))
        report.deleted.append(("b", 50))
        self.assertEqual(report.bytes_freed, 150)
        self.assertEqual(report.count, 2)

    @mock.patch("core.cleanup._shell_delete", return_value=(0, False))
    def test_delete_paths_success(self, _shell):
        path = os.path.join(self.tmp, "del.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("hello")
        events = []
        report = cleanup.delete_paths([path], self.app_dir, on_event=lambda *a: events.append(a))
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(report.bytes_freed, 5)
        self.assertTrue(events)

    @mock.patch("core.cleanup._shell_delete", return_value=(5, False))
    def test_delete_paths_failure_reports(self, _shell):
        path = os.path.join(self.tmp, "del2.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("hello")
        report = cleanup.delete_paths([path], self.app_dir)
        self.assertEqual(len(report.deleted), 0)
        self.assertEqual(len(report.failed), 1)
        self.assertIn("访问被拒绝", report.failed[0][1])
