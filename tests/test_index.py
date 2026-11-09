import os
import tempfile
import time
import unittest

from core.index import Index
from core.recommend import CleanItem
from core.scanner import DirRec, FileRec


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_idx_")
        self.path = os.path.join(self._tmp.name, "index.db")
        self.index = Index(self.path)
        now = int(time.time())
        big = [
            FileRec(r"C:\Users\wjx\a.iso", "a.iso", ".iso", "C:", r"C:\Users\wjx", 3 * 1024 ** 3, now, 0),
            FileRec(r"C:\Windows\b.dll", "b.dll", ".dll", "C:", r"C:\Windows", 1024, now, 1),
            FileRec(r"D:\data\c.pt", "c.pt", ".pt", "D:", r"D:\data", 2 * 1024 ** 3, now, 0),
        ]
        recent = [
            FileRec(r"C:\Users\wjx\r.txt", "r.txt", ".txt", "C:", r"C:\Users\wjx", 10, now, 0),
            FileRec(r"C:\Windows\w.log", "w.log", ".log", "C:", r"C:\Windows", 20, now, 1),
        ]
        dirs = [
            DirRec(r"C:\Users", r"C:\\", "C:", 2, 5 * 1024 ** 3, 10, now),
            DirRec(r"D:\data", r"D:\\", "D:", 2, 2 * 1024 ** 3, 3, now),
            DirRec(r"C:\Users\wjx", r"C:\Users", "C:", 3, 3 * 1024 ** 3, 4, now),
        ]
        self.index.replace_scan([], dirs, big, recent)

    def tearDown(self):
        self.index.close()
        self._tmp.cleanup()

    def test_has_index(self):
        self.assertTrue(self.index.has_index())
        self.assertEqual(self.index.counts()["big"], 3)

    def test_big_files_default(self):
        rows, total, size = self.index.big_files(min_size=0)
        self.assertEqual(total, 3)
        self.assertEqual(rows[0]["name"], "a.iso")
        self.assertGreaterEqual(size, 5 * 1024 ** 3)

    def test_big_files_filters(self):
        rows, total, _ = self.index.big_files(drive="D:", min_size=0)
        self.assertEqual(total, 1)
        rows, total, _ = self.index.big_files(include_system=False, min_size=0)
        self.assertEqual({r["name"] for r in rows}, {"a.iso", "c.pt"})
        rows, total, _ = self.index.big_files(exts=[".pt", ".iso"], min_size=0)
        self.assertEqual(total, 2)
        rows, total, _ = self.index.big_files(keyword="data", min_size=0)
        self.assertEqual(total, 1)

    def test_pagination(self):
        rows, total, _ = self.index.big_files(min_size=0, page=0, page_size=1)
        self.assertEqual(len(rows), 1)
        rows2, _total, _ = self.index.big_files(min_size=0, page=1, page_size=1)
        self.assertNotEqual(rows[0]["path"], rows2[0]["path"])

    def test_recent(self):
        rows, total, _ = self.index.recent_files(days=1)
        self.assertEqual(total, 1)
        rows, total, _ = self.index.recent_files(days=1, exclude_cache=False)
        self.assertEqual(total, 2)

    def test_folders(self):
        folders = self.index.top_folders_all()
        self.assertEqual(len(folders), 2)
        children = self.index.folder_children(r"C:\Users")
        self.assertEqual(len(children), 1)

    def test_cleanup_roundtrip(self):
        item = CleanItem("临时文件", "用户临时目录", r"C:\Temp", 1024, "safe", "临时文件", True,
                         "dir_contents", "")
        self.index.replace_cleanup([item])
        totals = self.index.cleanup_totals()
        self.assertEqual(totals["safe"]["total"], 1024)
        objects = self.index.cleanup_objects()
        self.assertEqual(objects[0].kind, "dir_contents")
        self.assertEqual(objects[0].title, "用户临时目录")

    def test_clear(self):
        self.index.clear()
        self.assertFalse(self.index.has_index())
        self.assertEqual(self.index.counts()["big"], 0)

    def test_bad_sort_key(self):
        rows, _total, _ = self.index.big_files(min_size=0, sort="evil; DROP TABLE big_files")
        self.assertTrue(rows)

    def test_replace_only_given_drive_keeps_others(self):
        now = int(time.time())
        dirs = [DirRec(r"C:\Data", "C:\\", "C:", 2, 7 * 1024 ** 3, 1, now)]
        self.index.replace_scan([], dirs, [], [], drives=["C:"])
        self.assertEqual(self.index.big_files(drive="D:", min_size=0)[1], 1)
        self.assertEqual(self.index.big_files(drive="C:", min_size=0)[1], 0)
        self.assertEqual([r["path"] for r in self.index.top_folders("C:")], [r"C:\Data"])

    def test_scans_and_dir_history(self):
        scans = self.index.history_scans()
        self.assertEqual(len(scans), 1)
        history = self.index.dir_history_map(scans[0]["id"])
        self.assertIn(r"C:\Users", history)
        self.assertEqual(self.index.dir_info(r"C:\Users")["scan_id"], scans[0]["id"])

    def test_volume_history_and_alerts(self):
        self.index.add_volume_sample("C:", 100, 40, ts=1000)
        self.index.add_volume_sample("C:", 100, 30, ts=2000)
        rows = self.index.volume_history("C:", since=0)
        self.assertEqual([r["free"] for r in rows][-2:], [40, 30])
        self.index.add_alert("C:", "low", "剩余不足")
        self.assertEqual(self.index.alerts()[0]["kind"], "low")

    def test_threads_use_own_connections(self):
        import threading

        result = {}

        def worker():
            result["n"] = self.index.counts()["big"]
            result["same"] = self.index.conn is main_conn

        main_conn = self.index.conn
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(result["n"], 3)
        self.assertFalse(result["same"])

    def test_replace_subtree_updates_ancestors(self):
        now = int(time.time())
        before_root = self.index.dir_info(r"C:\Users")["size"]
        dirs = [
            DirRec(r"C:\Users\wjx", r"C:\Users", "C:", 3, 4 * 1024 ** 3, 6, now),
            DirRec(r"C:\Users\wjx\new", r"C:\Users\wjx", "C:", 4, 1024 ** 3, 2, now),
        ]
        big = [FileRec(r"C:\Users\wjx\new\b.mkv", "b.mkv", ".mkv", "C:", r"C:\Users\wjx\new", 1024 ** 3, now, 0)]
        change = self.index.replace_subtree("C:\\Users\\wjx\\", dirs, big, [])
        self.assertEqual(change, {"before": 3 * 1024 ** 3, "after": 4 * 1024 ** 3})
        self.assertEqual(self.index.dir_info(r"C:\Users")["size"], before_root + 1024 ** 3)
        names = {r["name"] for r in self.index.big_files(drive="C:", min_size=0)[0]}
        self.assertEqual(names, {"b.mkv", "b.dll"})
        self.assertEqual(self.index.recent_files(days=1, exclude_cache=False)[1], 1)
        self.assertEqual(self.index.big_files(drive="D:", min_size=0)[1], 1)

    def test_apply_deletions(self):
        freed = self.index.apply_deletions([(r"C:\Users\wjx\a.iso", 3 * 1024 ** 3)])
        self.assertEqual(freed, 3 * 1024 ** 3)
        self.assertEqual(self.index.dir_info(r"C:\Users\wjx")["size"], 0)
        self.assertEqual(self.index.dir_info(r"C:\Users")["size"], 2 * 1024 ** 3)
        freed = self.index.apply_deletions([(r"C:\Users\wjx", 0)])
        self.assertEqual(freed, 0)
        self.assertIsNone(self.index.dir_info(r"C:\Users\wjx"))
        self.assertEqual(self.index.recent_files(days=1)[1], 0)

    def test_subtree_range_does_not_touch_siblings(self):
        now = int(time.time())
        self.index.extend("dirs", "path, parent, drive, depth, size, files, mtime",
                          [(r"C:\Users\wjx2", r"C:\Users", "C:", 3, 5, 1, now)])
        self.index.apply_deletions([(r"C:\Users\wjx", 0)])
        self.assertIsNotNone(self.index.dir_info(r"C:\Users\wjx2"))

    def test_schema_upgrade_drops_old_tables(self):
        self.index.set_meta("schema_version", "2")
        self.index.close()
        upgraded = Index(self.path)
        try:
            self.assertFalse(upgraded.has_index())
            self.assertEqual(upgraded.get_meta("schema_version"), "3")
        finally:
            upgraded.close()
        self.index = Index(self.path)
