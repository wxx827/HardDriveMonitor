import struct
import time
import unittest

from core import mft
from core.util import FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

REC = 1024
USA_OFF = 0x30
ATTR_OFF = 0x38
NOW = time.time()
OLD = NOW - 365 * 86400
GB = 1024 ** 3


def filetime(unix):
    return int(unix * 10_000_000) + mft.FILETIME_EPOCH


def _attr(atype, body, nonresident=False):
    """拼一个属性：body 是从 0x10 开始的内容。"""
    raw = struct.pack("<IIBBHHH", atype, 0, 1 if nonresident else 0, 0, 0, 0, 0) + body
    raw += b"\0" * (-len(raw) % 8)
    return raw[:4] + struct.pack("<I", len(raw)) + raw[8:]


def _resident(atype, value):
    return _attr(atype, struct.pack("<IHBB", len(value), 0x18, 0, 0) + value)


def si(mtime=OLD, attrs=0):
    ft = filetime(mtime)
    return _resident(mft.ATTR_SI, struct.pack("<qqqqI", ft, ft, ft, ft, attrs) + b"\0" * 12)


def fn(parent, name, ns=1):
    enc = name.encode("utf-16-le")
    value = struct.pack("<Q", parent | (1 << 48)) + b"\0" * 0x38 + struct.pack("<BB", len(name), ns) + enc
    return _resident(mft.ATTR_FN, value)


def data_resident(size):
    return _resident(mft.ATTR_DATA, b"x" * size)


def data_nonresident(size):
    clusters = -(-size // 4096)
    runs = bytes([0x24]) + clusters.to_bytes(4, "little") + (1000).to_bytes(2, "little", signed=True) + b"\0"
    body = struct.pack("<qqHHI", 0, clusters - 1, 0x40, 0, 0) + struct.pack("<qqq", clusters * 4096, size, size)
    return _attr(mft.ATTR_DATA, body + runs, nonresident=True)


def attr_list():
    return _resident(mft.ATTR_LIST, b"\0" * 32)


def record(attrs, directory=False, base=0, in_use=True, usn=0x1234, corrupt=False):
    rec = bytearray(REC)
    body = b"".join(attrs) + struct.pack("<I", 0xFFFFFFFF) + b"\0" * 4
    flags = (1 if in_use else 0) | (2 if directory else 0)
    struct.pack_into("<IHH", rec, 0, mft.SIG_FILE, USA_OFF, REC // 512 + 1)
    struct.pack_into("<HHII", rec, 0x14, ATTR_OFF, flags, ATTR_OFF + len(body), REC)
    struct.pack_into("<Q", rec, 0x20, base)
    rec[ATTR_OFF:ATTR_OFF + len(body)] = body
    struct.pack_into("<H", rec, USA_OFF, usn)
    for i in range(1, REC // 512 + 1):
        pos = i * 512 - 2
        rec[USA_OFF + 2 * i:USA_OFF + 2 * i + 2] = rec[pos:pos + 2]
        struct.pack_into("<H", rec, pos, usn ^ 0xFFFF if corrupt else usn)
    return rec


def build_volume():
    """返回 [(记录号, 记录字节)]，模拟一个小卷。"""
    recs = {
        5: record([si(), fn(5, ".")], directory=True),
        # 同一条记录里短名在前、长名在后
        16: record([si(), fn(5, "USERS~1", ns=2), fn(5, "Users", ns=1)], directory=True),
        17: record([si(), fn(16, "big.bin"), data_nonresident(5 * GB)]),
        18: record([si(), fn(16, "a.txt"), data_resident(100)]),
        19: record([si(attrs=FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS),
                    fn(16, "cloud.docx"), data_resident(50)]),
        # 长名在扩展记录 21 里，而且扩展记录的编号比基本记录 22 小
        21: record([fn(5, "LongDirectoryName", ns=1)], base=22),
        22: record([si(), attr_list(), fn(5, "LONGDI~1", ns=2)], directory=True),
        # 文件的 $DATA 在扩展记录里
        23: record([si(), attr_list(), fn(22, "x.dat")]),
        24: record([data_nonresident(2000)], base=23),
        25: record([si(attrs=FILE_ATTRIBUTE_REPARSE_POINT), fn(5, "Junction")], directory=True),
        26: record([si(), fn(25, "inside.txt"), data_resident(10)]),
        27: record([si(), fn(5, "$Recycle.Bin")], directory=True),
        28: record([si(), fn(27, "trash.bin"), data_resident(30)]),
        29: record([si(), fn(16, "deleted.txt"), data_resident(40)], in_use=False),
        30: record([si(), fn(16, "torn.txt"), data_resident(60)], corrupt=True),
        31: record([si(mtime=NOW), fn(5, "root.txt"), data_resident(7)]),
        # 系统元文件（< 16）不计入
        6: record([si(), fn(5, "$Bitmap"), data_nonresident(8 * GB)]),
    }
    return recs


def make_buf(recs, start, end):
    buf = bytearray()
    for i in range(start, end):
        buf += recs.get(i, bytearray(REC))
    return buf


@unittest.skipUnless(HAVE_NUMPY, "需要 numpy")
class TablesTests(unittest.TestCase):
    TOTAL = 32

    def _tables(self, splits):
        recs = build_volume()
        tables = mft.MftTables(self.TOTAL, REC, min_big=GB, cutoff=NOW - 86400)
        edges = [0, *splits, self.TOTAL]
        for a, b in zip(edges, edges[1:]):
            tables.feed(a, make_buf(recs, a, b))
        return tables

    def _build(self, tables, skip=frozenset(), max_depth=0):
        return tables.build("C:", set(skip), max_depth, (), {"$recycle.bin"})

    def _check(self, tables):
        dirs, top, recent, files, nbytes = self._build(tables)
        self.assertEqual(set(dirs), {"C:\\", "C:\\Users", "C:\\LongDirectoryName"})
        self.assertEqual(dirs["C:\\Users"][:2], [5 * GB + 100 + 50, 3])
        self.assertEqual(dirs["C:\\LongDirectoryName"][:2], [2000, 1])
        self.assertEqual(dirs["C:\\"][:2], [7, 1])
        self.assertEqual(files, 5)
        self.assertEqual(nbytes, 5 * GB + 100 + 50 + 2000 + 7)
        self.assertEqual([t[1] for t in top], ["C:\\Users\\big.bin"])
        self.assertEqual([r[1] for r in recent], ["C:\\root.txt"])
        self.assertAlmostEqual(recent[0][0], NOW, delta=1)

    def test_single_batch(self):
        self._check(self._tables([]))

    def test_split_batches_keep_long_names(self):
        self._check(self._tables([22]))
        self._check(self._tables([10, 17, 23, 24]))

    def test_skip_paths_and_depth(self):
        tables = self._tables([])
        dirs, _top, _recent, files, _bytes = self._build(tables, skip={"c:\\users"})
        self.assertNotIn("C:\\Users", dirs)
        self.assertEqual(files, 2)
        dirs, *_ = self._build(tables, max_depth=0)
        self.assertIn("C:\\Users", dirs)

    def test_records_seen_counts_base_records(self):
        tables = self._tables([])
        self.assertEqual(tables.records_seen, 13)
        self.assertFalse(tables.used[29])
        self.assertFalse(tables.used[30])


class LowLevelTests(unittest.TestCase):
    def test_parse_boot(self):
        boot = bytearray(512)
        boot[3:11] = b"NTFS    "
        struct.pack_into("<HB", boot, 0x0B, 512, 8)
        struct.pack_into("<q", boot, 0x30, 786432)
        struct.pack_into("<b", boot, 0x40, -10)
        self.assertEqual(mft.parse_boot(bytes(boot)), (512, 4096, 786432, 1024))
        boot[0x0D] = 0xF4   # 大簇：2^(256-244) 个扇区
        self.assertEqual(mft.parse_boot(bytes(boot))[1], 512 << 12)
        with self.assertRaises(mft.MftError):
            mft.parse_boot(b"\0" * 512)

    def test_apply_fixup(self):
        rec = record([si(), fn(5, "f.txt"), data_resident(600)])
        self.assertNotEqual(rec[510:512], b"xx")
        self.assertTrue(mft.apply_fixup(rec))
        self.assertEqual(rec[510:512], b"xx")
        self.assertFalse(mft.apply_fixup(record([si()], corrupt=True)))

    def test_decode_runlist(self):
        data = bytes([0x21, 0x18, 0x34, 0x56, 0x11, 0x10, 0xF0, 0x01, 0x05, 0x00, 0x99])
        self.assertEqual(mft.decode_runlist(data, 0, len(data)),
                         [(0x5634, 0x18), (0x5634 - 16, 0x10), (None, 5)])

    def test_data_runs(self):
        rec = record([si(), fn(5, "f"), data_nonresident(3 * 4096)])
        mft.apply_fixup(rec)
        low, high, runs, alloc, valid = mft._data_runs(bytes(rec))
        self.assertEqual((low, high, alloc, valid), (0, 2, 3 * 4096, 3 * 4096))
        self.assertEqual(runs, [(1000, 3)])

    def test_unix_conversion(self):
        self.assertEqual(mft._unix(0), 0.0)
        self.assertAlmostEqual(mft._unix(filetime(NOW)), NOW, delta=1e-3)


if __name__ == "__main__":
    unittest.main()
