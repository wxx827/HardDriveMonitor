import os
import tempfile
import threading
import unittest

from core import scanner


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_scan_")
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "sub", "deep"))
        os.makedirs(os.path.join(self.root, "empty"))
        self._write(os.path.join(self.root, "big.bin"), 3 * 1024 * 1024)
        self._write(os.path.join(self.root, "sub", "small.txt"), 100)
        self._write(os.path.join(self.root, "sub", "deep", "mid.dat"), 2 * 1024 * 1024)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _write(path, size):
        with open(path, "wb") as fh:
            fh.write(b"x" * size)

    def test_counts_and_sizes(self):
        result = scanner.scan(scanner.ScanOptions(roots=[self.root], min_big_size=1024 * 1024))
        self.assertEqual(result.stats.files, 3)
        self.assertEqual(result.stats.bytes, 5 * 1024 * 1024 + 100)
        self.assertGreaterEqual(len(result.dirs), 3)

    def test_top_k_sorted(self):
        opts = scanner.ScanOptions(roots=[self.root], min_big_size=1, top_k=10)
        top = scanner.scan(opts).big_files
        sizes = [f.size for f in top]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_top_k_limit(self):
        opts = scanner.ScanOptions(roots=[self.root], min_big_size=1, top_k=1)
        result = scanner.scan(opts)
        self.assertEqual(len(result.big_files), 1)
        self.assertEqual(result.big_files[0].size, 3 * 1024 * 1024)

    def test_recent_filter(self):
        opts = scanner.ScanOptions(roots=[self.root], min_big_size=1, recent_days=1)
        result = scanner.scan(opts)
        self.assertEqual(len(result.recent_files), 3)

    def test_max_depth(self):
        opts = scanner.ScanOptions(roots=[self.root], min_big_size=1, max_depth=1)
        result = scanner.scan(opts)
        names = {f.name for f in result.recent_files}
        self.assertIn("small.txt", names)

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        result = scanner.scan(scanner.ScanOptions(roots=[self.root]), cancel=cancel)
        self.assertTrue(result.stats.partial)

    def test_progress_callback(self):
        seen = []
        scanner.scan(
            scanner.ScanOptions(roots=[self.root], threads=2),
            on_progress=lambda st: seen.append(st.files),
        )
        self.assertTrue(seen)

    def test_root_dir_size_includes_subtree(self):
        result = scanner.scan(scanner.ScanOptions(roots=[self.root], min_big_size=1))
        by_path = {d.path: d for d in result.dirs}
        root = scanner.dir_key(self.root)
        self.assertEqual(by_path[root].size, 5 * 1024 * 1024 + 100)
        self.assertEqual(by_path[root].files, 3)
        self.assertEqual(by_path[os.path.join(root, "sub")].parent, root)
        self.assertIn(os.path.join(root, "empty"), by_path)

    def test_single_process_matches_multi_process(self):
        multi = scanner.scan(scanner.ScanOptions(roots=[self.root], min_big_size=1, threads=3))
        single = scanner.scan(scanner.ScanOptions(roots=[self.root], min_big_size=1, threads=1))
        self.assertEqual(multi.stats.files, single.stats.files)
        self.assertEqual({d.path: d.size for d in multi.dirs}, {d.path: d.size for d in single.dirs})
        self.assertEqual([f.path for f in multi.big_files], [f.path for f in single.big_files])

    def test_skip_paths(self):
        opts = scanner.ScanOptions(roots=[self.root], min_big_size=1, skip_paths=[os.path.join(self.root, "sub")])
        result = scanner.scan(opts)
        self.assertEqual(result.stats.files, 1)

    def test_dir_key(self):
        self.assertEqual(scanner.dir_key("C:"), "C:\\")
        self.assertEqual(scanner.dir_key("C:\\"), "C:\\")
        self.assertEqual(scanner.dir_key("C:\\a\\"), "C:\\a")

    def test_measure_dir(self):
        size, count = scanner.measure_dir(self.root)
        self.assertEqual(size, 5 * 1024 * 1024 + 100)
        self.assertEqual(count, 3)


class ScannerEdgeTests(unittest.TestCase):
    def test_missing_root(self):
        result = scanner.scan(scanner.ScanOptions(roots=[r"Z:\definitely-missing"]))
        self.assertEqual(result.stats.files, 0)
        self.assertEqual(result.big_files, [])

    def test_empty_options(self):
        result = scanner.scan(scanner.ScanOptions(roots=[]))
        self.assertEqual(result.stats.files, 0)
