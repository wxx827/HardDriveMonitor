"""浅层同步：文件有增删后，只更新受影响目录在索引里的数据，不重新扫描整个盘。

对每个变动目录，先找到「磁盘上还在、索引里也有」的最近一级（目录被整个删掉时就是它的上级），然后只读这一层：
- 索引里有、磁盘上没了的子目录：整棵子树从索引移除，各级上级减掉它的大小
- 磁盘上新出现的子目录：单独扫描这棵子树并写入
- 本层文件：大小 / 文件数按差值更新到各级上级，大文件 / 最近文件条目整体替换
统计口径与 scanner 完全一致（跳过联接、照算云端占位文件、遵守跳过目录和层级限制）。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from . import scanner, util
from .scanner import SKIP_DIR_NAMES, ScanOptions, dir_key, fast_flags, system_prefixes
from .util import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_REPARSE_POINT, path_depth

MAX_TARGETS = 5000
CHECK_MIN_DIR_SIZE = 64 * util.MB
CHECK_RECENT_LIMIT = 5000


@dataclass
class SyncResult:
    dirs: int = 0          # 浅层同步过的目录数
    removed: int = 0       # 从索引移除的子目录数
    added: int = 0         # 新扫描写入的子目录数
    delta: int = 0         # 索引里总大小的变化（字节）
    errors: int = 0
    truncated: bool = False
    touched: set[str] = field(default_factory=set)   # 实际有变化的目录

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.added or self.delta)

    def touches(self, path: str) -> bool:
        """path 这棵子树里是否有变化（用来判断当前页面要不要立即刷新）。"""
        base = dir_key(path).rstrip("\\").lower()
        return any(t.lower() == base or t.lower().startswith(base + "\\") for t in self.touched)

    def merge(self, other: "SyncResult") -> None:
        self.dirs += other.dirs
        self.removed += other.removed
        self.added += other.added
        self.delta += other.delta
        self.errors += other.errors
        self.truncated |= other.truncated
        self.touched |= other.touched


class _Context:
    def __init__(self, opts: ScanOptions):
        self.opts = opts
        self.min_big = int(opts.min_big_size)
        self.cutoff = time.time() - int(opts.recent_days) * 86400
        self.max_depth = int(opts.max_depth or 0)
        self.skip = {p.rstrip("\\").lower() for p in opts.skip_paths if p}
        self._prefixes: dict[str, tuple[str, ...]] = {}

    def prefixes(self, key: str) -> tuple[str, ...]:
        drive = key[:2].upper()
        if drive not in self._prefixes:
            self._prefixes[drive] = system_prefixes([drive + "\\"])
        return self._prefixes[drive]

    def excluded(self, key: str) -> bool:
        low = key.rstrip("\\").lower()
        if any(part in SKIP_DIR_NAMES for part in low.split("\\")[1:]):
            return True
        return any(low == s or low.startswith(s + "\\") for s in self.skip)


def _parent(key: str) -> str:
    head = key.rstrip("\\").rpartition("\\")[0]
    return dir_key(head) if head else ""


def _is_dir(path: str) -> bool:
    return os.path.isdir(util.long_path(path))


def resolve_targets(index, dirs, ctx: _Context) -> set[str]:
    """把变动目录映射成需要浅层同步的目录：磁盘上存在且已在索引里的最近一级。"""
    memo: dict[str, str] = {}
    targets: set[str] = set()
    for d in sorted({dir_key(p) for p in dirs if p}, key=len):
        if ctx.excluded(d):
            continue
        chain = []
        p = d
        while p and p not in memo:
            if _is_dir(p) and index.has_dir(p):
                memo[p] = p
                break
            chain.append(p)
            p = _parent(p)
        target = memo.get(p, "") if p else ""
        for c in chain:
            memo[c] = target
        if target and not ctx.excluded(target):
            targets.add(target)
    return targets


def sync_dirs(index, dirs, opts: ScanOptions, cancel: threading.Event | None = None,
              limit: int = MAX_TARGETS) -> SyncResult:
    ctx = _Context(opts)
    result = SyncResult()
    targets = sorted(resolve_targets(index, dirs, ctx), key=lambda p: (len(p), p))
    if len(targets) > limit:
        targets = targets[:limit]
        result.truncated = True
    for key in targets:
        if cancel is not None and cancel.is_set():
            result.truncated = True
            break
        _sync_one(index, key, ctx, result, cancel)
    return result


def prune_missing(index, opts: ScanOptions, cancel: threading.Event | None = None) -> SyncResult:
    """找出程序没在运行时被删掉的较大目录、大文件和最新文件，同步它们所在的目录。"""
    dirs, files = index.check_candidates(CHECK_MIN_DIR_SIZE, CHECK_RECENT_LIMIT)
    changed: set[str] = set()
    for path in dirs:
        if cancel is not None and cancel.is_set():
            break
        if not _is_dir(path):
            changed.add(path)
    for path, parent in files:
        if cancel is not None and cancel.is_set():
            break
        if parent and not os.path.lexists(util.long_path(path)):
            changed.add(parent)
    if not changed:
        return SyncResult()
    return sync_dirs(index, changed, opts, cancel)


def _sync_one(index, key: str, ctx: _Context, result: SyncResult, cancel) -> None:
    limit_depth = bool(ctx.max_depth) and path_depth(key) - 1 >= ctx.max_depth
    min_big, cutoff, skip = ctx.min_big, ctx.cutoff, ctx.skip
    own_bytes = own_files = 0
    newest = 0.0
    big: list = []
    recent: list = []
    disk_dirs: set[str] = set()
    try:
        with os.scandir(util.long_path(key)) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    result.errors += 1
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                is_dir = bool(attrs & 0x10)
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT and (is_dir or not attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
                    continue
                child = scanner._strip_long(entry.path)
                if is_dir:
                    if entry.name.lower() in SKIP_DIR_NAMES or child.lower() in skip or limit_depth:
                        continue
                    disk_dirs.add(child)
                    continue
                size, mtime = st.st_size, st.st_mtime
                newest = max(newest, mtime)
                own_bytes += size
                own_files += 1
                if size >= min_big or mtime >= cutoff:
                    rec = scanner._file_rec(child, size, mtime, fast_flags(child.lower(), attrs, ctx.prefixes(key)))
                    if size >= min_big:
                        big.append(rec)
                    if mtime >= cutoff:
                        recent.append(rec)
        newest = max(newest, os.stat(util.long_path(key)).st_mtime)
    except OSError:
        # 目录刚好被删掉或拒绝访问：保持索引不变，删掉的情况由它上级的变动通知处理
        result.errors += 1
        return

    result.dirs += 1
    before = (result.removed, result.added, result.delta)
    indexed = index.child_dirs(key)
    gone = [p for p in indexed if p not in disk_dirs]
    if gone:
        result.delta -= index.apply_deletions([(p, 0) for p in gone])
        result.removed += len(gone)
    for path in sorted(disk_dirs - set(indexed)):
        if cancel is not None and cancel.is_set():
            result.truncated = True
            break
        sub_depth = 0
        if ctx.max_depth:
            sub_depth = max(1, ctx.max_depth - (path_depth(path) - 1))
        sub = scanner.scan(ScanOptions(
            roots=[path], min_big_size=min_big, top_k=ctx.opts.top_k, recent_days=ctx.opts.recent_days,
            recent_cap=ctx.opts.recent_cap, max_depth=sub_depth, skip_paths=list(ctx.opts.skip_paths),
            threads=1, method="scandir",
        ), cancel=cancel)
        if sub.stats.partial or not sub.dirs:
            continue
        info = index.replace_subtree(path, sub.dirs, sub.big_files, sub.recent_files)
        result.delta += info["after"] - info["before"]
        result.added += 1
        result.errors += sub.stats.errors
    result.delta += index.sync_dir_files(key, own_bytes, own_files, newest, big, recent)
    if (result.removed, result.added, result.delta) != before:
        result.touched.add(key)
