"""磁盘扫描：多进程目录遍历（或 NTFS MFT 直读），产出目录聚合、大文件 Top-K、最近文件。

遍历由若干 worker 进程完成，主进程只负责派发目录批次、合并结果，因此不会和界面争抢 GIL。
"""
from __future__ import annotations

import heapq
import multiprocessing as mp
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import util
from .util import (
    FLAG_CACHE,
    FLAG_HARDLINK,
    FLAG_PLACEHOLDER,
    FLAG_SYSTEM,
    FILE_ATTRIBUTE_REPARSE_POINT,
    MB,
    drive_of,
    path_depth,
)

# 扫描时直接跳过的目录名（位于任意层级）
SKIP_DIR_NAMES = {
    "system volume information",
    "$recycle.bin",
    "config.msi",
}

_SYSTEM_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "perflogs",
    "documents and settings",
}

_CACHE_HINTS = (
    "\\temp\\",
    "\\tmp\\",
    "\\cache\\",
    "\\caches\\",
    "\\cache2\\",
    "\\code cache\\",
    "\\gpucache\\",
    "\\node_modules\\",
    "\\__pycache__\\",
    "\\.cache\\",
    "\\.gradle\\",
    "\\.m2\\",
    "\\.conda\\",
    "\\appdata\\local\\temp\\",
)

# 每个任务最多处理多少目录 / 多长时间后把剩余目录交回主进程重新分配（负载均衡）
_TASK_DIR_BUDGET = 400
_TASK_TIME_BUDGET = 0.3


@dataclass(slots=True)
class ScanOptions:
    roots: list[str]
    min_big_size: int = 100 * MB
    top_k: int = 20000
    recent_days: int = 180
    recent_cap: int = 200000
    max_depth: int = 0
    skip_paths: list[str] = field(default_factory=list)
    threads: int = 0          # worker 进程数；0 = 自动，1 = 在当前进程内遍历
    method: str = "auto"      # auto / scandir / mft


@dataclass(slots=True)
class ScanStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    errors: int = 0
    elapsed: float = 0.0
    started_at: float = 0.0
    partial: bool = False
    current: str = ""
    method: str = "scandir"
    phase: str = ""
    progress: float = -1.0    # 0..1，未知时为 -1

    def snapshot(self) -> "ScanStats":
        return ScanStats(
            files=self.files,
            dirs=self.dirs,
            bytes=self.bytes,
            errors=self.errors,
            elapsed=self.elapsed,
            started_at=self.started_at,
            partial=self.partial,
            current=self.current,
            method=self.method,
            phase=self.phase,
            progress=self.progress,
        )


@dataclass(slots=True)
class FileRec:
    path: str
    name: str
    ext: str
    drive: str
    directory: str
    size: int
    mtime: float
    flags: int


@dataclass(slots=True)
class DirRec:
    path: str
    parent: str
    drive: str
    depth: int
    size: int
    files: int
    mtime: float


@dataclass(slots=True)
class ScanResult:
    stats: ScanStats
    big_files: list[FileRec]
    recent_files: list[FileRec]
    dirs: list[DirRec]


def _norm_root(root: str) -> str:
    root = os.path.abspath(root)
    if not root.endswith("\\"):
        root += "\\"
    return root


def dir_key(path: str) -> str:
    """目录在索引中的规范写法：盘根保留反斜杠（C:\\），其它目录不带结尾反斜杠。"""
    p = path.rstrip("\\")
    if len(p) == 2 and p[1] == ":":
        return p + "\\"
    return p


def _strip_long(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def fast_flags(path_low: str, attrs: int, system_prefixes: tuple[str, ...]) -> int:
    flags = 0
    for pref in system_prefixes:
        if path_low.startswith(pref):
            flags |= FLAG_SYSTEM
            break
    if attrs & util.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS or attrs & util.FILE_ATTRIBUTE_RECALL_ON_OPEN:
        flags |= FLAG_PLACEHOLDER
    for hint in _CACHE_HINTS:
        if hint in path_low:
            flags |= FLAG_CACHE
            break
    return flags


def system_prefixes(roots: list[str]) -> tuple[str, ...]:
    prefixes: list[str] = []
    for root in roots:
        drive = drive_of(root)
        if not drive:
            continue
        base = drive.lower()
        for name in _SYSTEM_DIR_NAMES:
            prefixes.append(f"{base}\\{name}\\")
        prefixes.append(f"{base}\\users\\public\\")
    return tuple(sorted(set(prefixes)))


# --------------------------------------------------------------------------
# 遍历核心：在 worker 进程（或单进程模式）里执行
# --------------------------------------------------------------------------
class _Walker:
    def __init__(self, cfg: dict):
        self.min_big = cfg["min_big"]
        self.cutoff = cfg["cutoff"]
        self.top_k = cfg["top_k"]
        self.recent_cap = cfg["recent_cap"]
        self.system_prefixes = tuple(cfg["system_prefixes"])
        self.skip = set(cfg["skip"])
        self.max_depth = cfg["max_depth"]
        self.base_depths = cfg["base_depths"]
        self.top: list[tuple[int, str, float, int]] = []
        self.recent: list[tuple[float, str, int, int]] = []

    def _base_depth(self, low: str) -> int:
        for root_low, depth in self.base_depths:
            if low.startswith(root_low):
                return depth
        return 1

    def run(self, stack: list[tuple[str, float]], dir_budget: int, time_budget: float):
        """处理 stack 中的 (目录, 修改时间)（深度优先），超出预算后返回剩余目录。"""
        top, recent = self.top, self.recent
        min_big, cutoff = self.min_big, self.cutoff
        top_k, recent_cap = self.top_k, self.recent_cap
        prefixes, skip, max_depth = self.system_prefixes, self.skip, self.max_depth
        aggs: list[tuple[str, int, int, float]] = []
        files = nbytes = errors = processed = 0
        last = ""
        deadline = time.monotonic() + time_budget
        while stack:
            if processed >= dir_budget or (processed and time.monotonic() > deadline):
                break
            path, dir_mtime = stack.pop()
            processed += 1
            last = path
            dir_bytes = dir_files = 0
            depth = path_depth(path)
            limit_depth = bool(max_depth) and depth - self._base_depth(path.lower()) >= max_depth
            try:
                with os.scandir(util.long_path(path)) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            errors += 1
                            continue
                        attrs = getattr(st, "st_file_attributes", 0)
                        is_dir = bool(attrs & 0x10)
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            # 联接 / 符号链接：不进入，避免循环与重复计数；云端占位文件照常计入
                            if is_dir or not attrs & util.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
                                continue
                        child = _strip_long(entry.path)
                        if is_dir:
                            low = entry.name.lower()
                            if low in SKIP_DIR_NAMES or child.lower() in skip or limit_depth:
                                continue
                            stack.append((child, st.st_mtime))
                            continue
                        size = st.st_size
                        mtime = st.st_mtime
                        dir_bytes += size
                        dir_files += 1
                        if size >= min_big or mtime >= cutoff:
                            flags = fast_flags(child.lower(), attrs, prefixes)
                            if size >= min_big:
                                item = (size, child, mtime, flags)
                                if len(top) < top_k:
                                    heapq.heappush(top, item)
                                elif size > top[0][0]:
                                    heapq.heapreplace(top, item)
                            if mtime >= cutoff:
                                item2 = (mtime, child, size, flags)
                                if len(recent) < recent_cap:
                                    heapq.heappush(recent, item2)
                                elif mtime > recent[0][0]:
                                    heapq.heapreplace(recent, item2)
            except OSError:
                errors += 1
            except Exception:  # noqa: BLE001 - 扫描不能因单目录异常而中断
                errors += 1
            if not dir_mtime:
                try:
                    dir_mtime = os.stat(util.long_path(path), follow_symlinks=False).st_mtime
                except OSError:
                    dir_mtime = 0.0
            files += dir_files
            nbytes += dir_bytes
            aggs.append((dir_key(path), dir_bytes, dir_files, dir_mtime))
        return aggs, stack, files, nbytes, errors, last


def _walk_worker(task_q, result_q, cfg: dict) -> None:
    walker = _Walker(cfg)
    while True:
        task = task_q.get()
        if task is None:
            result_q.put(("final", walker.top, walker.recent))
            return
        try:
            result_q.put(("batch",) + walker.run(list(task), _TASK_DIR_BUDGET, _TASK_TIME_BUDGET))
        except Exception:  # noqa: BLE001
            result_q.put(("batch", [], [], 0, 0, 1, ""))


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
class _Collected:
    def __init__(self):
        self.dirs: dict[str, list] = {}
        self.top: list[tuple[int, str, float, int]] = []
        self.recent: list[tuple[float, str, int, int]] = []
        self.files = 0
        self.bytes = 0
        self.errors = 0
        self.walked_drives: set[str] = set()


def _walk_roots(roots, opts, cutoff, prefixes, collected, stats, report, cancel) -> bool:
    """遍历 roots，结果写入 collected；返回是否被取消。"""
    cfg = {
        "min_big": int(opts.min_big_size),
        "cutoff": cutoff,
        "top_k": int(opts.top_k),
        "recent_cap": int(opts.recent_cap),
        "system_prefixes": list(prefixes),
        "skip": [p.rstrip("\\").lower() for p in opts.skip_paths],
        "max_depth": int(opts.max_depth or 0),
        "base_depths": [(r.lower(), path_depth(r)) for r in roots],
    }
    collected.walked_drives.update(drive_of(r) for r in roots)
    nproc = int(opts.threads or 0)
    if nproc <= 0:
        nproc = default_workers()
    if nproc == 1:
        walker = _Walker(cfg)
        stack = [(root, 0.0) for root in roots]
        while stack:
            if cancel.is_set():
                break
            aggs, stack, files, nbytes, errors, last = walker.run(stack, _TASK_DIR_BUDGET, _TASK_TIME_BUDGET)
            _merge_batch(collected, aggs, files, nbytes, errors)
            stats.current = last
            report()
        collected.top.extend(walker.top)
        collected.recent.extend(walker.recent)
        return cancel.is_set()

    ctx = mp.get_context("spawn")
    task_q = ctx.Queue()
    result_q = ctx.Queue()
    procs = [
        ctx.Process(target=_walk_worker, args=(task_q, result_q, cfg), name=f"hdm-scan-{i}", daemon=True)
        for i in range(nproc)
    ]
    for proc in procs:
        proc.start()
    inflight = 0
    for root in roots:
        task_q.put([(root, 0.0)])
        inflight += 1
    cancelled = False
    try:
        while inflight:
            if cancel.is_set():
                cancelled = True
                break
            try:
                msg = result_q.get(timeout=0.2)
            except queue.Empty:
                report()
                continue
            if msg[0] != "batch":
                continue
            _kind, aggs, leftover, files, nbytes, errors, last = msg
            inflight -= 1
            _merge_batch(collected, aggs, files, nbytes, errors)
            if last:
                stats.current = last
            if leftover:
                chunk = max(1, min(64, -(-len(leftover) // nproc)))
                for i in range(0, len(leftover), chunk):
                    task_q.put(leftover[i:i + chunk])
                    inflight += 1
            report()
        if not cancelled:
            for _ in procs:
                task_q.put(None)
            finals = 0
            deadline = time.monotonic() + 60
            while finals < len(procs) and time.monotonic() < deadline:
                try:
                    msg = result_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if msg[0] == "final":
                    finals += 1
                    collected.top.extend(msg[1])
                    collected.recent.extend(msg[2])
    finally:
        for proc in procs:
            if cancelled:
                proc.terminate()
            proc.join(timeout=2)
            if proc.is_alive():
                proc.terminate()
        task_q.cancel_join_thread()
        result_q.cancel_join_thread()
    return cancelled


def _merge_batch(collected: _Collected, aggs, files, nbytes, errors) -> None:
    dirs = collected.dirs
    for path, size, count, mtime in aggs:
        dirs[path] = [size, count, mtime]
    collected.files += files
    collected.bytes += nbytes
    collected.errors += errors


def _dedupe_hardlinks(records: list[FileRec], cancel=None) -> None:
    """Top 大文件里按 (卷, 文件ID) 去重，避免硬链接重复计数。"""
    seen: dict[tuple[int, int], str] = {}
    for rec in records:
        if cancel is not None and cancel.is_set():
            return
        try:
            st = os.stat(util.long_path(rec.path))
            key = (int(getattr(st, "st_dev", 0)), int(getattr(st, "st_ino", 0)))
        except OSError:
            continue
        if not key[1]:
            continue
        if key in seen:
            rec.flags |= FLAG_HARDLINK
        else:
            seen[key] = rec.path


def _file_rec(path: str, size: int, mtime: float, flags: int) -> FileRec:
    directory, _sep, name = path.rpartition("\\")
    dot = name.rfind(".")
    if len(directory) == 2:
        directory += "\\"
    return FileRec(
        path=path,
        name=name,
        ext=name[dot:].lower() if dot > 0 else "",
        drive=path[:2].upper() if path[1:2] == ":" else drive_of(path),
        directory=directory,
        size=int(size),
        mtime=float(mtime),
        flags=int(flags),
    )


def _key_depth(key: str) -> int:
    return key.rstrip("\\").count("\\") + 1


def _parent_key(key: str) -> str:
    p = key.rstrip("\\")
    return dir_key(p.rpartition("\\")[0] or p)


def default_workers() -> int:
    return max(2, min(8, os.cpu_count() or 4))


def finalize(collected: _Collected, opts: ScanOptions, stats: ScanStats, cancel) -> ScanResult:
    dirs_raw = collected.dirs
    # 目录子树大小：从深到浅累加到父目录
    for path in sorted(dirs_raw.keys(), key=_key_depth, reverse=True):
        info = dirs_raw[path]
        parent = _parent_key(path)
        if parent != path:
            pinfo = dirs_raw.get(parent)
            if pinfo is not None:
                pinfo[0] += info[0]
                pinfo[1] += info[1]
                if info[2] > pinfo[2]:
                    pinfo[2] = info[2]

    top_items = heapq.nlargest(int(opts.top_k), collected.top, key=lambda x: x[0])
    big_files = [_file_rec(path, size, mtime, flags) for size, path, mtime, flags in top_items]
    walked = [rec for rec in big_files if rec.drive in collected.walked_drives]
    if walked:
        _dedupe_hardlinks(walked, cancel)

    recent_items = heapq.nlargest(int(opts.recent_cap), collected.recent, key=lambda x: x[0])
    recent_files = [_file_rec(path, size, mtime, flags) for mtime, path, size, flags in recent_items]

    dirs = []
    for path, (size, count, mtime) in dirs_raw.items():
        dirs.append(
            DirRec(
                path=path,
                parent=_parent_key(path),
                drive=path[:2].upper(),
                depth=_key_depth(path),
                size=size,
                files=count,
                mtime=mtime,
            )
        )
    dirs.sort(key=lambda d: d.size, reverse=True)
    stats.files = collected.files
    stats.bytes = collected.bytes
    stats.errors = collected.errors
    stats.dirs = len(dirs_raw)
    return ScanResult(stats=stats, big_files=big_files, recent_files=recent_files, dirs=dirs)


def scan(
    opts: ScanOptions,
    on_progress=None,
    cancel=None,
) -> ScanResult:
    cancel = cancel or threading.Event()
    started = time.time()
    roots = [_norm_root(r) for r in opts.roots if os.path.isdir(r)]
    stats = ScanStats(started_at=started)
    if not roots:
        return ScanResult(stats, [], [], [])
    if cancel.is_set():
        stats.partial = True
        return ScanResult(stats, [], [], [])

    cutoff = started - opts.recent_days * 86400
    prefixes = system_prefixes(roots)
    collected = _Collected()
    last_report = [0.0]

    def report(force: bool = False):
        if not on_progress:
            return
        now = time.time()
        if not force and now - last_report[0] < 0.25:
            return
        last_report[0] = now
        stats.files = collected.files
        stats.bytes = collected.bytes
        stats.errors = collected.errors
        stats.dirs = len(collected.dirs)
        stats.elapsed = now - started
        on_progress(stats.snapshot())

    mft_roots: list[str] = []
    walk_roots: list[str] = []
    for root in roots:
        if opts.method != "scandir" and len(root) == 3:
            from . import mft

            if mft.can_scan(root):
                mft_roots.append(root)
                continue
        walk_roots.append(root)

    report(True)
    cancelled = False
    methods = []
    for root in mft_roots:
        if cancel.is_set():
            cancelled = True
            break
        from . import mft

        stats.method = "mft"
        try:
            ok = mft.scan_volume(root, opts, cutoff, prefixes, collected, stats, report, cancel)
        except Exception:  # noqa: BLE001 - MFT 解析失败就回退到遍历
            ok = False
        if ok:
            methods.append("mft")
        elif cancel.is_set():
            cancelled = True
            break
        else:
            walk_roots.append(root)
    if walk_roots and not cancelled:
        stats.method = "scandir"
        stats.phase = "遍历目录"
        stats.progress = -1.0
        cancelled = _walk_roots(walk_roots, opts, cutoff, prefixes, collected, stats, report, cancel)
        methods.append("scandir")

    stats.phase = "汇总结果"
    stats.progress = -1.0
    report(True)
    stats.partial = cancelled or cancel.is_set()
    stats.method = "+".join(dict.fromkeys(methods)) or "scandir"
    result = finalize(collected, opts, stats, cancel)
    stats.elapsed = time.time() - started
    if on_progress:
        on_progress(stats.snapshot())
    return result


def measure_dir(path: str, cancel=None) -> tuple[int, int]:
    """快速统计一个目录的总大小与文件数（用于清理推荐）。"""
    total = 0
    count = 0
    if not os.path.isdir(path):
        try:
            st = os.stat(path)
            return int(st.st_size), 1
        except OSError:
            return 0, 0
    stack = [path]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        cur = stack.pop()
        try:
            with os.scandir(util.long_path(cur)) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    if attrs & 0x10:
                        stack.append(entry.path)
                    else:
                        total += int(st.st_size)
                        count += 1
        except OSError:
            continue
    return total, count
