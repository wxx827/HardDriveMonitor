import os
import tempfile
import unittest
from unittest import mock

from core import recommend, util
from core.recommend import CleanItem, summarize


class SummarizeTests(unittest.TestCase):
    def test_totals(self):
        items = [
            CleanItem("临时文件", "TEMP", r"C:\Temp", 2 * 1024 ** 3, "safe", "", True, "dir_contents"),
            CleanItem("缓存与开发", "pip", r"C:\pip", 3 * 1024 ** 3, "safe", "", True, "dir"),
            CleanItem("应用缓存", "微信", r"C:\WeChat", 1 * 1024 ** 3, "caution", "", True, "dir"),
            CleanItem("重要数据（勿删）", "datasets", r"D:\datasets", 5 * 1024 ** 3, "blocked", "", False, "info"),
        ]
        summary = summarize(items)
        self.assertEqual(summary["totals"]["safe"], 5 * 1024 ** 3)
        self.assertEqual(summary["totals"]["caution"], 1 * 1024 ** 3)
        self.assertEqual(summary["reclaimable"], 6 * 1024 ** 3)
        self.assertEqual(summary["counts"]["safe"], 2)

    def test_level_text(self):
        item = CleanItem("x", "y", "z", 1, "caution", "", True, "file")
        self.assertEqual(item.level_text, "谨慎")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_rec_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_known_candidates_have_levels_and_kinds(self):
        cands = recommend._known_candidates(self.tmp)
        categories = {c.category for c in cands}
        self.assertIn("临时文件", categories)
        self.assertIn("回收站", categories)
        self.assertIn("系统占用（仅说明）", categories)
        for cand in cands:
            self.assertIn(cand.level, {"safe", "caution", "blocked"})
            self.assertTrue(cand.kind)

    @mock.patch("core.recommend.scanner.measure_dir", return_value=(0, 0))
    def test_measure_missing_dir(self, _measure):
        cands = recommend._known_candidates(self.tmp)
        measured = recommend._measure(cands, None, None)
        self.assertTrue(measured)


class FileAdviceTests(unittest.TestCase):
    def test_advice_keys(self):
        advice = recommend.CleanItem("旧安装包", "a.exe", r"D:\setup.exe", 1024, "caution", "", True, "file")
        self.assertTrue(advice.deletable)
        self.assertEqual(advice.level_text, "谨慎")
