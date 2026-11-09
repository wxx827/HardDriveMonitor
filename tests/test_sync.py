import os
import shutil
import tempfile
import time
import unittest

from core import scanner, sync, watch
from core.index import Index

MB = 1024 * 1024


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_sync_")
        base = self._tmp.name
        self.root = os.path.join(base, "tree")
        self.db_dir = os.path.join(base, "db")
        _write(os.path.join(self.root, "a", "big.bin"), 3 * MB)
        _write(os.path.join(self.root, "a", "s.txt"), 100)
        _write(os.path.join(self.root, "b", "c", "x.dat"), 200)
        _write(os.path.join(self.root, "b", "y.dat"), 2 * MB)
        _write(os.path.join(self.root, "r.txt"), 50)
        self.opts = scanner.ScanOptions(roots=[self.root], min_big_size=MB, threads=1, method="scandir")
        self.index = self._scan_into("a.db")

    def tearDown(self):
        for idx in getattr(self, "_indexes", []):
            idx.close()
        self._tmp.cleanup()

    def _scan_into(self, name):
        idx = Index(os.path.join(self.db_dir, name))
        self.__dict__.setdefault("_indexes", []).append(idx)
        res = scanner.scan(self.opts)
        idx.replace_scan([], res.dirs, res.big_files, res.recent_files)
        return idx

    @staticmethod
    def _snapshot(idx):
        conn = idx.conn
        dirs = {r["path"]: (r["size"], r["files"]) for r in conn.execute("SELECT path, size, files FROM dirs")}
        big = sorted((r["path"], r["dir"], r["size"]) for r in conn.execute("SELECT * FROM big_files"))
        recent = sorted((r["path"], r["dir"], r["size"]) for r in conn.execute("SELECT * FROM recent_files"))
        return dirs, big, recent

    def _assert_matches_rescan(self):
        fresh = self._scan_into("fresh.db")
        self.assertEqual(self._snapshot(self.index), self._snapshot(fresh))

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_sync_matches_full_rescan(self):
        os.remove(self._p("a", "big.bin"))
        shutil.rmtree(self._p("b"))
        _write(self._p("new", "deep", "n.bin"), 2 * MB)
        _write(self._p("r.txt"), 5000)
        changed = [self._p("a"), self._p("b", "c"), self._p("b"), self._p("new", "deep"), self.root]
        result = sync.sync_dirs(self.index, changed, self.opts)
        self.assertEqual(result.removed, 1)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.delta, -3 * MB - 2 * MB - 200 + 2 * MB + 4950)
        self.assertTrue(result.touches(self.root))
        self.assertTrue(result.touches(self._p("a")))
        self.assertFalse(result.touches(self._p("ab")))
        self._assert_matches_rescan()
        newest = int(os.stat(self._p("new", "deep", "n.bin")).st_mtime)
        self.assertGreaterEqual(self.index.dir_info(scanner.dir_key(self.root))["mtime"], newest)

    def test_events_for_deleted_subtree_resolve_to_existing_parent(self):
        shutil.rmtree(self._p("b"))
        result = sync.sync_dirs(self.index, [self._p("b", "c")], self.opts)
        self.assertEqual(result.removed, 1)
        self.assertFalse(self.index.has_dir(self._p("b")))
        self._assert_matches_rescan()

    def test_unchanged_dir_is_noop(self):
        before = self._snapshot(self.index)
        result = sync.sync_dirs(self.index, [self._p("a"), self._p("b", "c")], self.opts)
        self.assertFalse(result.changed)
        self.assertEqual(self._snapshot(self.index), before)

    def test_skipped_dirs_are_ignored(self):
        _write(self._p("$Recycle.Bin", "junk.bin"), 2 * MB)
        result = sync.sync_dirs(self.index, [self._p("$Recycle.Bin")], self.opts)
        self.assertEqual(result.dirs, 0)
        self._assert_matches_rescan()

    def test_prune_missing_finds_offline_deletions(self):
        os.remove(self._p("a", "big.bin"))
        os.remove(self._p("b", "y.dat"))
        result = sync.prune_missing(self.index, self.opts)
        self.assertEqual(result.delta, -5 * MB)
        self._assert_matches_rescan()


class WatcherTests(unittest.TestCase):
    def test_parse_notifications(self):
        def item(name, last=False):
            raw = name.encode("utf-16-le")
            size = 12 + len(raw)
            size += -size % 4
            head = (0 if last else size).to_bytes(4, "little") + (1).to_bytes(4, "little")
            return (head + len(raw).to_bytes(4, "little") + raw).ljust(size, b"\0")

        data = item("a\\b.txt") + item("中文\\c", last=True)
        self.assertEqual(watch.parse_notifications(data), ["a\\b.txt", "中文\\c"])

    def test_add_filters_and_maps_to_parent(self):
        w = watch.DriveWatcher("C:\\", ignore=["C:\\app\\data"])
        w.add(["x.txt", "Users\\me\\f.txt", "$Recycle.Bin\\S-1\\$R1", "app\\data\\index.db", "app\\main.py"])
        dirs, overflow = w.drain()
        self.assertEqual(dirs, {"C:\\", "C:\\Users\\me", "C:\\app"})
        self.assertFalse(overflow)

    def test_watcher_sees_create_and_delete(self):
        with tempfile.TemporaryDirectory(prefix="hdm_watch_") as root:
            sub = os.path.join(root, "sub")
            os.makedirs(sub)
            w = watch.DriveWatcher(root)
            self.assertTrue(w.start(), w.failed)
            try:
                time.sleep(0.2)
                _write(os.path.join(sub, "f.bin"), 10)
                os.remove(os.path.join(sub, "f.bin"))
                deadline = time.time() + 5
                seen = set()
                while time.time() < deadline and scanner.dir_key(sub) not in seen:
                    time.sleep(0.1)
                    seen |= w.drain()[0]
                self.assertIn(scanner.dir_key(sub), seen)
            finally:
                w.stop()
            self.assertFalse(w.alive)


if __name__ == "__main__":
    unittest.main()
