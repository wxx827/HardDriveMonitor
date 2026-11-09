import os
import tempfile
import unittest

from core import util


class HumanSizeTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(util.human_size(0), "0 B")
        self.assertEqual(util.human_size(999), "999 B")
        self.assertEqual(util.human_size(1024), "1.0 KB")
        self.assertEqual(util.human_size(1536), "1.5 KB")
        self.assertEqual(util.human_size(1024 * 1024), "1.0 MB")
        self.assertEqual(util.human_size(1024 ** 3), "1.00 GB")
        self.assertEqual(util.human_size(None), "-")

    def test_format_time(self):
        self.assertEqual(util.fmt_time(0), "-")
        self.assertRegex(util.fmt_time(1700000000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_drive_and_ext(self):
        self.assertEqual(util.drive_of(r"C:\Users\a.txt"), "C:")
        self.assertEqual(util.ext_of("photo.JPG"), ".jpg")
        self.assertEqual(util.ext_of("noext"), "")
        self.assertEqual(util.path_depth(r"C:\a\b"), 3)

    def test_system_path(self):
        self.assertTrue(util.is_system_path(r"C:\Windows\System32\x.dll"))
        self.assertTrue(util.is_system_path(r"C:\Program Files\App\a.exe"))
        self.assertFalse(util.is_system_path(r"D:\projects\paper.docx"))
        self.assertFalse(util.is_system_path(r"C:\Users\wjx\Desktop\a.txt"))

    def test_cache_like(self):
        self.assertTrue(util.is_cache_like(r"C:\Users\wjx\AppData\Local\Temp\a.tmp"))
        self.assertTrue(util.is_cache_like(r"D:\proj\node_modules\x\y.js"))
        self.assertFalse(util.is_cache_like(r"D:\projects\main.py"))

    def test_mask_user(self):
        masked = util.mask_user(r"C:\Users\wjx\Desktop\a.txt", "wjx")
        self.assertEqual(masked, r"C:\Users\<用户>\Desktop\a.txt")
        self.assertEqual(util.mask_user(r"D:\data\a.txt", "wjx"), r"D:\data\a.txt")

    def test_long_path(self):
        short = r"C:\a\b"
        self.assertEqual(util.long_path(short), short)
        deep = "C:\\" + "x" * 300
        self.assertTrue(util.long_path(deep).startswith("\\\\?\\"))
        self.assertEqual(util.long_path("\\\\?\\C:\\x"), "\\\\?\\C:\\x")

    def test_file_flags(self):
        flags = util.file_flags(r"C:\Windows\Temp\a.tmp", 0)
        self.assertTrue(flags & util.FLAG_SYSTEM)
        self.assertTrue(flags & util.FLAG_CACHE)
        placeholder = util.file_flags(r"C:\x\y.docx", util.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
        self.assertTrue(placeholder & util.FLAG_PLACEHOLDER)


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_test_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()
