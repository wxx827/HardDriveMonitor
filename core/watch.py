"""文件变动监听：每个盘一个后台线程调用 ReadDirectoryChangesW，记录哪些目录里有文件增删、改名或大小变化。

这里只收集「哪个目录变了」，不碰数据库；由界面定时取走，交给 sync.sync_dirs 做浅层同步。
在资源管理器里删除 / 移到回收站 / 新建 / 复制文件后，几秒内索引就会跟着更新，不用重新扫描。
"""
from __future__ import annotations

import ctypes
import struct
import threading
from ctypes import wintypes

from .scanner import SKIP_DIR_NAMES, dir_key

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x07
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
# 文件名 / 目录名变化（新建、删除、改名）+ 文件大小变化
NOTIFY_FILTER = 0x01 | 0x02 | 0x08
BUFFER_BYTES = 1024 * 1024
ERROR_OPERATION_ABORTED = 995
ERROR_NOTIFY_ENUM_DIR = 1022
MAX_PENDING = 200_000

_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                              wintypes.LPVOID]
        k32.CancelIoEx.restype = wintypes.BOOL
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32 = k32
    return _k32


def parse_notifications(raw: bytes) -> list[str]:
    """解析 FILE_NOTIFY_INFORMATION 链表，返回相对路径列表。"""
    out = []
    off = 0
    while off + 12 <= len(raw):
        next_off, _action, name_len = struct.unpack_from("<III", raw, off)
        out.append(raw[off + 12:off + 12 + name_len].decode("utf-16-le", "surrogatepass"))
        if not next_off:
            break
        off += next_off
    return out


class DriveWatcher:
    """监听一个目录树（通常是盘根）。pending 里是受影响的目录（索引里的写法）。"""

    def __init__(self, root: str, ignore: list[str] | tuple[str, ...] = ()):
        self.root = dir_key(root)
        self.ignore = tuple(dir_key(p).lower() for p in ignore if p)
        self.overflow = False
        self.failed = ""
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._handle = None
        self._thread: threading.Thread | None = None

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        try:
            k32 = _kernel32()
        except (AttributeError, OSError) as exc:
            self.failed = str(exc)
            return False
        handle = k32.CreateFileW(self.root, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None, OPEN_EXISTING,
                                 FILE_FLAG_BACKUP_SEMANTICS, None)
        if not handle or handle == wintypes.HANDLE(-1).value:
            self.failed = f"无法监听 {self.root}（错误 {ctypes.get_last_error()}）"
            return False
        self._handle = handle
        self._thread = threading.Thread(target=self._run, name=f"hdm-watch-{self.root[:1]}", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        handle, self._handle = self._handle, None
        if handle is None:
            return
        k32 = _kernel32()
        k32.CancelIoEx(handle, None)
        if self._thread is not None:
            self._thread.join(timeout=2)
        k32.CloseHandle(handle)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------- 结果 ----------------
    def has_pending(self) -> bool:
        return bool(self._pending) or self.overflow

    def drain(self) -> tuple[set[str], bool]:
        """取走积累的目录集合；第二项表示期间是否有通知丢失（缓冲区溢出）。"""
        with self._lock:
            dirs, self._pending = self._pending, set()
            overflow, self.overflow = self.overflow, False
        return dirs, overflow

    def add(self, rel_paths: list[str]) -> None:
        root = self.root if self.root.endswith("\\") else self.root + "\\"
        ignore = self.ignore
        found = []
        for rel in rel_paths:
            if not rel:
                continue
            low = rel.lower()
            if low.split("\\", 1)[0] in SKIP_DIR_NAMES:
                continue
            full = root + rel
            full_low = full.lower()
            if any(full_low == p or full_low.startswith(p + "\\") for p in ignore):
                continue
            head = full.rpartition("\\")[0]
            found.append(dir_key(head))
        if not found:
            return
        with self._lock:
            self._pending.update(found)
            if len(self._pending) > MAX_PENDING:
                self._pending.clear()
                self.overflow = True

    # ---------------- 线程 ----------------
    def _run(self) -> None:
        k32 = _kernel32()
        buf = ctypes.create_string_buffer(BUFFER_BYTES)
        returned = wintypes.DWORD()
        while not self._stop.is_set():
            handle = self._handle
            if handle is None:
                return
            ok = k32.ReadDirectoryChangesW(handle, buf, BUFFER_BYTES, True, NOTIFY_FILTER,
                                           ctypes.byref(returned), None, None)
            if self._stop.is_set():
                return
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_OPERATION_ABORTED:
                    return
                if err == ERROR_NOTIFY_ENUM_DIR:
                    self.overflow = True
                    continue
                self.failed = f"监听 {self.root} 中断（错误 {err}）"
                return
            if not returned.value:
                self.overflow = True
                continue
            try:
                self.add(parse_notifications(ctypes.string_at(buf, returned.value)))
            except Exception:  # noqa: BLE001 - 解析异常不能让监听线程退出
                self.overflow = True


class WatchSet:
    """一组 DriveWatcher：按根目录列表启动 / 重启，统一取走变动。"""

    def __init__(self):
        self.watchers: dict[str, DriveWatcher] = {}

    def update(self, roots: list[str], ignore: list[str]) -> list[str]:
        """让监听的根目录与 roots 一致；返回启动失败的说明。"""
        want = {dir_key(r) for r in roots}
        for root in list(self.watchers):
            if root not in want or not self.watchers[root].alive:
                self.watchers.pop(root).stop()
        errors = []
        for root in sorted(want - set(self.watchers)):
            watcher = DriveWatcher(root, ignore)
            if watcher.start():
                self.watchers[root] = watcher
            else:
                errors.append(watcher.failed)
        return errors

    def stop(self) -> None:
        for watcher in self.watchers.values():
            watcher.stop()
        self.watchers.clear()

    def has_pending(self) -> bool:
        return any(w.has_pending() for w in self.watchers.values())

    def drain(self) -> tuple[set[str], bool]:
        dirs: set[str] = set()
        overflow = False
        for watcher in self.watchers.values():
            got, lost = watcher.drain()
            dirs |= got
            overflow |= lost
        return dirs, overflow
