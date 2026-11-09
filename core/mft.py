"""NTFS MFT 直读扫描：管理员权限下顺序读取主文件表（$MFT），不逐个打开目录。

整盘几百万个文件只需要把 MFT（通常 1–4 GB）顺序读一遍，再用 numpy 批量解析记录头和属性，
比逐目录 scandir 快很多。只在「NTFS + 管理员 + 有 numpy」时启用；任何异常都由调用方回退到目录遍历。

解析的内容：
- 记录头：in-use / 目录标志、基本记录引用（扩展记录把属性归到基本记录）
- $STANDARD_INFORMATION：修改时间、文件属性（识别联接 / 云端占位文件）
- $FILE_NAME：父目录引用与名称（优先 Win32 名，跳过 8.3 短名）
- 无名 $DATA：文件真实大小（常驻取值长度，非常驻取 data size）
"""
from __future__ import annotations

import ctypes
import struct

from . import util
from .util import FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

ROOT_RECORD = 5
FIRST_USER_RECORD = 16
CHUNK_BYTES = 32 * 1024 * 1024
FIXUP_STRIDE = 512
FILETIME_EPOCH = 116444736000000000   # 1601-01-01 → 1970-01-01，单位 100ns
SIG_FILE = 0x454C4946                  # b"FILE"
ATTR_SI, ATTR_LIST, ATTR_FN, ATTR_DATA, ATTR_END = 0x10, 0x20, 0x30, 0x80, 0xFFFFFFFF
NS_DOS = 2
MAX_ATTRS = 64


class MftError(Exception):
    pass


# --------------------------------------------------------------------------
# 可用性
# --------------------------------------------------------------------------
def can_scan(root: str) -> bool:
    if len(root) < 2 or root[1] != ":":
        return False
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            return False
    except (AttributeError, OSError):
        return False
    from .volumes import _volume_label

    return _volume_label(root[:2] + "\\")[1].upper() == "NTFS"


# --------------------------------------------------------------------------
# 低层：引导扇区、fixup、runlist
# --------------------------------------------------------------------------
def parse_boot(boot: bytes) -> tuple[int, int, int, int]:
    """返回 (每扇区字节, 每簇字节, $MFT 起始簇号, 每条记录字节)。"""
    if boot[3:11] != b"NTFS    ":
        raise MftError("不是 NTFS 引导扇区")
    bps = struct.unpack_from("<H", boot, 0x0B)[0]
    spc = boot[0x0D]
    if spc > 0x80:
        spc = 1 << (256 - spc)
    cluster = bps * spc
    mft_lcn = struct.unpack_from("<q", boot, 0x30)[0]
    cpr = struct.unpack_from("<b", boot, 0x40)[0]
    rec_size = cluster * cpr if cpr > 0 else 1 << (-cpr)
    if not bps or not cluster or rec_size < 512 or rec_size % FIXUP_STRIDE:
        raise MftError("引导扇区参数异常")
    return bps, cluster, mft_lcn, rec_size


def apply_fixup(rec: bytearray) -> bool:
    """还原单条记录每 512 字节末尾被更新序列号替换掉的两个字节；校验失败返回 False。"""
    usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
    if usa_off + 2 * usa_cnt > len(rec):
        return False
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_cnt):
        pos = i * FIXUP_STRIDE - 2
        if pos + 2 > len(rec):
            break
        if rec[pos:pos + 2] != usn:
            return False
        rec[pos:pos + 2] = rec[usa_off + 2 * i:usa_off + 2 * i + 2]
    return True


def decode_runlist(data: bytes, start: int, end: int) -> list[tuple[int | None, int]]:
    """解析 data runs：返回 [(起始簇号或 None 表示稀疏, 簇数)]。"""
    runs: list[tuple[int | None, int]] = []
    lcn = 0
    pos = start
    while pos < end:
        header = data[pos]
        if header == 0:
            break
        len_size, off_size = header & 0x0F, header >> 4
        pos += 1
        length = int.from_bytes(data[pos:pos + len_size], "little")
        pos += len_size
        if off_size == 0:
            runs.append((None, length))
            continue
        lcn += int.from_bytes(data[pos:pos + off_size], "little", signed=True)
        pos += off_size
        runs.append((lcn, length))
    return runs


def _iter_attrs(rec: bytes):
    """逐个产出 (类型, 偏移, 长度)，用于单条记录的 Python 解析（只用在 $MFT 自身）。"""
    off = struct.unpack_from("<H", rec, 0x14)[0]
    used = min(struct.unpack_from("<I", rec, 0x18)[0], len(rec))
    while off + 16 <= used:
        atype, alen = struct.unpack_from("<II", rec, off)
        if atype == ATTR_END or alen < 16 or off + alen > used:
            return
        yield atype, off, alen
        off += alen


def _data_runs(rec: bytes, lowest_vcn: int | None = None):
    """找无名、非常驻 $DATA，返回 (起始 VCN, 结束 VCN, runs, 分配大小, 有效大小)。"""
    for atype, off, alen in _iter_attrs(rec):
        if atype != ATTR_DATA or rec[off + 9] != 0 or rec[off + 8] != 1:
            continue
        low, high = struct.unpack_from("<qq", rec, off + 0x10)
        if lowest_vcn is not None and low != lowest_vcn:
            continue
        run_off = struct.unpack_from("<H", rec, off + 0x20)[0]
        alloc, size, init = struct.unpack_from("<qqq", rec, off + 0x28)
        return low, high, decode_runlist(rec, off + run_off, off + alen), alloc, min(size, init)
    return None


# --------------------------------------------------------------------------
# 读取 $MFT
# --------------------------------------------------------------------------
class _Volume:
    def __init__(self, root: str):
        self.fh = open("\\\\.\\" + root[:2], "rb", buffering=0)  # noqa: SIM115 - 需要管理员权限
        try:
            self.bps, self.cluster, self.mft_lcn, self.rec_size = parse_boot(self._read(0, 512))
        except Exception:
            self.fh.close()
            raise

    def close(self) -> None:
        self.fh.close()

    def _read(self, offset: int, size: int) -> bytes:
        """原始卷读取必须按扇区对齐。"""
        bps = getattr(self, "bps", 512)
        start = offset - offset % bps
        length = -(-(offset + size - start) // bps) * bps
        self.fh.seek(start)
        data = self.fh.read(length)
        return data[offset - start:offset - start + size]

    def read_record_at(self, offset: int) -> bytearray:
        rec = bytearray(self._read(offset, self.rec_size))
        if struct.unpack_from("<I", rec, 0)[0] != SIG_FILE or not apply_fixup(rec):
            raise MftError("MFT 记录损坏")
        return rec

    def mft_layout(self) -> tuple[list[tuple[int, int]], int]:
        """返回 $MFT 的 [(起始簇号, 簇数)] 与有效字节数；处理 $MFT 被属性列表拆到扩展记录的情况。"""
        rec0 = self.read_record_at(self.mft_lcn * self.cluster)
        first = _data_runs(rec0, 0)
        if first is None:
            raise MftError("找不到 $MFT 的 $DATA")
        _low, high, runs, alloc, valid = first
        total_clusters = -(-alloc // self.cluster)
        while high + 1 < total_clusters:
            ext = self._find_extension(rec0, high + 1, runs)
            if ext is None:
                raise MftError("$MFT 的数据分布不完整")
            _low, high, more, _a, _v = ext
            runs = runs + more
        if any(lcn is None for lcn, _n in runs):
            raise MftError("$MFT 含稀疏区段")
        return [(lcn, n) for lcn, n in runs], valid

    def _find_extension(self, rec0: bytes, vcn: int, runs):
        for atype, off, _alen in _iter_attrs(rec0):
            if atype != ATTR_LIST:
                continue
            if rec0[off + 8] != 0:
                raise MftError("$MFT 的属性列表是非常驻的")
            vlen, voff = struct.unpack_from("<IH", rec0, off + 0x10)
            pos, end = off + voff, off + voff + vlen
            while pos + 26 <= end:
                etype, elen = struct.unpack_from("<IH", rec0, pos)
                start_vcn, ref = struct.unpack_from("<qQ", rec0, pos + 8)
                if elen == 0:
                    break
                if etype == ATTR_DATA and start_vcn == vcn:
                    rec = self.read_record_at(self._record_offset(ref & 0xFFFFFFFFFFFF, runs))
                    return _data_runs(rec, vcn)
                pos += elen
        return None

    def _record_offset(self, rec_no: int, runs) -> int:
        byte_off = rec_no * self.rec_size
        for lcn, n in runs:
            size = n * self.cluster
            if byte_off < size:
                return lcn * self.cluster + byte_off
            byte_off -= size
        raise MftError("扩展记录不在已知的 MFT 区段内")

    def iter_chunks(self, runs, valid: int, cancel=None):
        """按顺序读取 MFT，产出 (起始记录号, 整数条记录的 bytearray)。"""
        rec_size = self.rec_size
        total = valid // rec_size
        first = 0
        carry = b""
        for lcn, n in runs:
            ext_bytes = n * self.cluster
            pos = 0
            while pos < ext_bytes and first < total:
                if cancel is not None and cancel.is_set():
                    return
                size = min(CHUNK_BYTES, ext_bytes - pos)
                self.fh.seek(lcn * self.cluster + pos)
                data = self.fh.read(size)
                if not data:
                    raise MftError("读取 MFT 失败")
                pos += size
                buf = bytearray(carry + data) if carry else bytearray(data)
                count = min(len(buf) // rec_size, total - first)
                carry = bytes(buf[count * rec_size:]) if len(buf) > count * rec_size else b""
                if count:
                    del buf[count * rec_size:]
                    yield first, buf
                    first += count


# --------------------------------------------------------------------------
# 批量解析
# --------------------------------------------------------------------------
class MftTables:
    """把 MFT 记录批量解析进按记录号索引的数组，最后再拼路径、汇总目录。"""

    def __init__(self, total: int, rec_size: int, min_big: int, cutoff: float):
        import numpy as np

        self.np = np
        self.rec_size = rec_size
        self.total = total
        self.min_big = int(min_big)
        self.cutoff_ft = int(cutoff * 10_000_000) + FILETIME_EPOCH
        self.used = np.zeros(total, dtype=bool)
        self.is_dir = np.zeros(total, dtype=bool)
        self.parent = np.full(total, -1, dtype=np.int64)
        self.fn_ns = np.full(total, -1, dtype=np.int16)
        self.size = np.zeros(total, dtype=np.int64)
        self.mtime = np.zeros(total, dtype=np.int64)
        self.attrs = np.zeros(total, dtype=np.int64)
        self.names: list[str | None] = [None] * total
        self.records_seen = 0

    def feed(self, first: int, buf: bytearray) -> int:
        """解析一批记录；返回其中有效（in-use）的记录数。"""
        np = self.np
        rs = self.rec_size
        n = len(buf) // rs
        a = np.frombuffer(buf, dtype=np.uint8).reshape(n, rs)
        u16 = np.frombuffer(buf, dtype="<u2").reshape(n, rs // 2)
        u32 = np.frombuffer(buf, dtype="<u4").reshape(n, rs // 4)
        r = np.nonzero((u32[:, 0] == SIG_FILE) & ((u16[:, 0x16 // 2] & 1) == 1))[0]
        if not len(r):
            return 0
        r = r[self._fixup(a, u16, r)]
        if not len(r):
            return 0
        m = len(r)

        def g8(o):
            return a[r_sel, o].astype(np.int64)

        def g16(o):
            return g8(o) | (g8(o + 1) << 8)

        def g32(o):
            return g16(o) | (g16(o + 2) << 16)

        def g48(o):
            return g32(o) | (g16(o + 4) << 32)

        def g64(o):
            return g32(o) | (g32(o + 4) << 32)

        rec_no = first + r
        r_sel = r
        base = g48(np.full(m, 0x20, dtype=np.int64))
        owner = np.where(base != 0, base, rec_no)
        is_base = base == 0
        flags = u16[r, 0x16 // 2]
        limit = np.minimum(u32[r, 0x18 // 4].astype(np.int64), rs)
        off = u16[r, 0x14 // 2].astype(np.int64)

        si_mtime = np.zeros(m, dtype=np.int64)
        si_attrs = np.zeros(m, dtype=np.int64)
        fn_parent = np.full(m, -1, dtype=np.int64)
        fn_ns = np.full(m, -1, dtype=np.int64)
        fn_pos = np.zeros(m, dtype=np.int64)
        fn_len = np.zeros(m, dtype=np.int64)
        d_size = np.full(m, -1, dtype=np.int64)
        has_list = np.zeros(m, dtype=bool)

        for _ in range(MAX_ATTRS):
            live = np.nonzero((off >= 0x18) & (off + 16 <= limit))[0]
            if not len(live):
                break
            r_sel = r[live]
            o = off[live]
            atype = g32(o)
            alen = g32(o + 4)
            stop = (atype == ATTR_END) | (alen < 16) | (o + alen > limit[live])
            ok = ~stop
            o_hdr = np.minimum(o, rs - 0x40)
            resident = g8(o + 8) == 0
            vo = g16(o_hdr + 0x14)
            val = np.minimum(o + vo, rs - 0x48)

            sel = ok & (atype == ATTR_SI) & resident
            if sel.any():
                si_mtime[live[sel]] = g64(val + 8)[sel]
                si_attrs[live[sel]] = g32(val + 32)[sel]

            sel = ok & (atype == ATTR_FN) & resident
            if sel.any():
                ns = g8(val + 0x41)
                cur = fn_ns[live]
                better = sel & ((cur < 0) | ((cur == NS_DOS) & (ns != NS_DOS)))
                if better.any():
                    idx = live[better]
                    fn_parent[idx] = (g48(val) & 0xFFFFFFFFFFFF)[better]
                    fn_ns[idx] = ns[better]
                    fn_len[idx] = g8(val + 0x40)[better]
                    fn_pos[idx] = (r_sel * rs + val + 0x42)[better]

            sel = ok & (atype == ATTR_DATA) & (g8(o + 9) == 0)
            if sel.any():
                o_nr = np.minimum(o, rs - 0x38)
                size = np.where(resident, g32(o_hdr + 0x10),
                                np.where(g64(o_nr + 0x10) == 0, g64(o_nr + 0x30), -1))
                d_size[live[sel]] = size[sel]

            has_list[live[ok & (atype == ATTR_LIST)]] = True
            off[live] = np.where(stop, -1, o + alen)

        valid_owner = (owner >= 0) & (owner < self.total)
        b = np.nonzero(is_base)[0]
        recs = rec_no[b]
        self.used[recs] = True
        self.is_dir[recs] = (flags[b] & 2) != 0
        self.mtime[recs] = si_mtime[b]
        self.attrs[recs] = si_attrs[b]

        s = np.nonzero((d_size >= 0) & valid_owner)[0]
        self.size[owner[s]] = d_size[s]

        # 长文件名可能在扩展记录里、短名在基本记录里，两者先后顺序不定：只有更好的名字才覆盖
        f = np.nonzero((fn_ns >= 0) & valid_owner)[0]
        cur = self.fn_ns[owner[f]]
        take = f[(cur < 0) | ((cur == NS_DOS) & (fn_ns[f] != NS_DOS))]
        # 同一批里同一 owner 可能出现多次，numpy 重复下标赋值是后写的生效：把短名排在前面
        take = take[np.argsort(fn_ns[take] != NS_DOS, kind="stable")]
        own = owner[take]
        self.parent[own] = fn_parent[take]
        self.fn_ns[own] = fn_ns[take]

        # 只解码之后用得到的名字：目录、大文件 / 最近文件候选、有属性列表或来自扩展记录的
        need = (self.is_dir[own] | (self.size[own] >= self.min_big) | (self.mtime[own] >= self.cutoff_ft)
                | has_list[take] | ~is_base[take])
        names = self.names
        mv = memoryview(buf)
        for rec, pos, ln in zip(own[need].tolist(), fn_pos[take][need].tolist(), fn_len[take][need].tolist()):
            names[rec] = bytes(mv[pos:pos + 2 * ln]).decode("utf-16-le", "surrogatepass")
        self.records_seen += len(b)
        return len(b)

    def _fixup(self, a, u16, r):
        np = self.np
        usa_off = u16[r, 2].astype(np.int64)
        usa_cnt = u16[r, 3].astype(np.int64)
        good = (usa_off >= 0x28) & (usa_off + 2 * usa_cnt <= self.rec_size)
        usa_off = np.where(good, usa_off, 0x30)
        lo, hi = a[r, usa_off], a[r, usa_off + 1]
        for i in range(1, self.rec_size // FIXUP_STRIDE + 1):
            pos = i * FIXUP_STRIDE - 2
            has = good & (usa_cnt > i)
            good &= ~has | ((a[r, pos] == lo) & (a[r, pos + 1] == hi))
            rows = r[has]
            src = usa_off[has] + 2 * i
            a[rows, pos] = a[rows, src]
            a[rows, pos + 1] = a[rows, src + 1]
        return good

    # ------------------------------------------------------------------
    def build(self, drive: str, skip: set[str], max_depth: int, prefixes, skip_names) -> tuple:
        """拼目录路径并汇总。返回 (dirs{路径: [本层字节, 本层文件数, mtime]}, top, recent, 文件数, 字节数)。"""
        from .scanner import fast_flags

        np = self.np
        root_path = drive + "\\"
        parent = self.parent.tolist()
        used = self.used
        is_dir = self.is_dir
        attrs = self.attrs
        names = self.names
        paths: dict[int, str] = {ROOT_RECORD: root_path}
        bad: set[int] = set()
        reparse = FILE_ATTRIBUTE_REPARSE_POINT

        for rec in np.nonzero(used & is_dir)[0].tolist():
            if rec in paths or rec in bad:
                continue
            chain = []
            cur = rec
            while cur not in paths:
                if (cur in bad or cur < FIRST_USER_RECORD or cur >= self.total or not used[cur]
                        or not is_dir[cur] or names[cur] is None or len(chain) > 1024):
                    bad.update(chain)
                    chain = None
                    break
                chain.append(cur)
                cur = parent[cur]
            if chain is None:
                continue
            base = paths[cur]
            for i in range(len(chain) - 1, -1, -1):
                node = chain[i]
                name = names[node]
                path = (base if base.endswith("\\") else base + "\\") + name
                low = path.lower()
                if (name.lower() in skip_names or int(attrs[node]) & reparse or low in skip
                        or (max_depth and util.path_depth(path) > 1 + max_depth)):
                    bad.update(chain[:i + 1])
                    break
                paths[node] = path
                base = path

        dir_ok = np.zeros(self.total, dtype=bool)
        dir_ok[list(paths)] = True
        par = self.parent
        fmask = used & ~is_dir & (par >= 0) & (par < self.total)
        fmask[:FIRST_USER_RECORD] = False
        fmask &= dir_ok[np.clip(par, 0, self.total - 1)]
        att = self.attrs
        fmask &= ~(((att & reparse) != 0) & ((att & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS) == 0))
        f_idx = np.nonzero(fmask)[0]
        fp = par[f_idx]
        fs = np.maximum(self.size[f_idx], 0)
        own_bytes = np.bincount(fp, weights=fs.astype(np.float64), minlength=self.total)
        own_files = np.bincount(fp, minlength=self.total)

        dirs: dict[str, list] = {}
        for rec, path in paths.items():
            key = path if rec == ROOT_RECORD else path.rstrip("\\")
            dirs[key] = [int(own_bytes[rec]), int(own_files[rec]), _unix(int(self.mtime[rec]))]

        top, recent = [], []
        fm = self.mtime[f_idx]
        cand = (fs >= self.min_big) | (fm >= self.cutoff_ft)
        for rec, size, ft in zip(f_idx[cand].tolist(), fs[cand].tolist(), fm[cand].tolist()):
            name = names[rec]
            if name is None:
                continue
            base = paths[parent[rec]]
            path = (base if base.endswith("\\") else base + "\\") + name
            mtime = _unix(ft)
            flags = fast_flags(path.lower(), int(att[rec]), prefixes)
            if size >= self.min_big:
                top.append((size, path, mtime, flags))
            if ft >= self.cutoff_ft:
                recent.append((mtime, path, size, flags))
        return dirs, top, recent, len(f_idx), int(fs.sum())


def _unix(filetime: int) -> float:
    if filetime <= FILETIME_EPOCH:
        return 0.0
    return (filetime - FILETIME_EPOCH) / 10_000_000


# --------------------------------------------------------------------------
# 入口（由 scanner.scan 调用）
# --------------------------------------------------------------------------
def scan_volume(root, opts, cutoff, prefixes, collected, stats, report, cancel) -> bool:
    """直读一个 NTFS 卷，结果合并进 collected；被取消返回 False，出错抛异常（调用方回退）。"""
    from .scanner import SKIP_DIR_NAMES

    drive = root[:2].upper()
    vol = _Volume(root)
    try:
        runs, valid = vol.mft_layout()
        total = valid // vol.rec_size
        tables = MftTables(total, vol.rec_size, opts.min_big_size, cutoff)
        stats.phase = f"读取 {drive} 的 MFT"
        base_files = collected.files
        done = 0
        for first, buf in vol.iter_chunks(runs, valid, cancel):
            tables.feed(first, buf)
            done = first + len(buf) // vol.rec_size
            stats.progress = done / max(1, total)
            stats.current = f"{drive}\\ MFT {stats.progress * 100:.0f}%"
            collected.files = base_files + tables.records_seen
            report()
        collected.files = base_files
        if cancel.is_set() or done < total:
            return False
    finally:
        vol.close()

    stats.phase = f"汇总 {drive} 的目录"
    stats.progress = -1.0
    report(True)
    skip = {p.rstrip("\\").lower() for p in opts.skip_paths}
    dirs, top, recent, files, nbytes = tables.build(drive, skip, int(opts.max_depth or 0), prefixes, SKIP_DIR_NAMES)
    collected.dirs.update(dirs)
    collected.top.extend(top)
    collected.recent.extend(recent)
    collected.files += files
    collected.bytes += nbytes
    collected.walked_drives.add(drive)
    return True
