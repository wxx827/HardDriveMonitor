"""安全删除：受保护路径校验 + 移到回收站（shell32 SHFileOperationW）。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from ctypes import wintypes
from dataclasses import dataclass, field

from . import util

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


PROTECTED_PREFIXES = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata\microsoft",
    r"c:\system volume information",
    r"c:\recovery",
    r"c:\$recycle.bin",
    r"d:\windows",
    r"d:\program files",
    r"d:\program files (x86)",
    r"d:\programdata\microsoft",
    r"d:\system volume information",
    r"d:\recovery",
    r"d:\$recycle.bin",
)

PROTECTED_FILES = (
    r"c:\pagefile.sys",
    r"c:\hiberfil.sys",
    r"c:\swapfile.sys",
    r"c:\windows\memory.dmp",
)

# 这些目录本身作为整体删除是危险的（但可以清空其内容）
PROTECTED_DIRS = (
    r"c:\users",
    r"d:\users",
    r"c:\users\public",
)


def normalize(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\")


def protected_reason(path: str, app_dir: str) -> str | None:
    """如果路径受保护，返回原因；否则返回 None。"""
    if not path:
        return "路径为空"
    norm = normalize(path)
    if len(norm) <= 3:
        return "不能删除磁盘根目录"
    low = norm
    for pref in PROTECTED_PREFIXES:
        if low == pref or low.startswith(pref + "\\"):
            return "系统 / 程序目录受保护，不能删除"
    for name in PROTECTED_FILES:
        if low == name:
            return "系统关键文件受保护，不能删除"
    app_norm = normalize(app_dir)
    if low == app_norm or low.startswith(app_norm + "\\"):
        return "不能删除本程序所在目录"
    if app_norm.startswith(low + "\\"):
        return "该目录包含本程序，不能删除"
    for name in PROTECTED_DIRS:
        if low == name:
            return "用户目录本身受保护，请选择具体文件"
    if not os.path.exists(path):
        return "文件不存在（可能已被删除）"
    return None


def validate(paths: list[str], app_dir: str) -> tuple[list[str], list[tuple[str, str]]]:
    ok: list[str] = []
    bad: list[tuple[str, str]] = []
    seen = set()
    for path in paths:
        norm = normalize(path)
        if norm in seen:
            continue
        seen.add(norm)
        reason = protected_reason(path, app_dir)
        if reason:
            bad.append((path, reason))
        else:
            ok.append(path)
    return ok, bad


def _shell_delete(paths: list[str]) -> tuple[int, bool]:
    """把路径批量移到回收站；返回 (shell 错误码, 是否被用户取消)。"""
    if not paths:
        return 0, False
    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    joined = "\0".join(paths) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.c_wchar_p(joined)
    op.pTo = None
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None
    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except (AttributeError, OSError):
        return -1, False
    return int(code), bool(op.fAnyOperationsAborted)


def _long(path: str) -> str:
    return util.long_path(path)


@dataclass
class DeleteReport:
    deleted: list[tuple[str, int]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    aborted: bool = False

    @property
    def bytes_freed(self) -> int:
        return sum(size for _path, size in self.deleted)

    @property
    def count(self) -> int:
        return len(self.deleted)

    def merge(self, other: "DeleteReport") -> None:
        self.deleted.extend(other.deleted)
        self.failed.extend(other.failed)
        self.aborted = self.aborted or other.aborted


def delete_paths(
    paths: list[str],
    app_dir: str,
    on_event=None,
    cancel: threading.Event | None = None,
) -> DeleteReport:
    """校验后移到回收站。on_event(kind, path, size_or_reason) 会被逐条回调。"""
    report = DeleteReport()
    ok, bad = validate(paths, app_dir)
    for path, reason in bad:
        report.failed.append((path, reason))
        if on_event:
            on_event("failed", path, reason)

    sizes: dict[str, int] = {}
    for path in ok:
        try:
            sizes[path] = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            sizes[path] = 0

    if not ok:
        return report

    if cancel is not None and cancel.is_set():
        report.aborted = True
        return report

    code, aborted = _shell_delete(ok)
    if aborted:
        report.aborted = True
    if code == 0:
        for path in ok:
            report.deleted.append((path, sizes.get(path, 0)))
            if on_event:
                on_event("deleted", path, sizes.get(path, 0))
        return report

    # 批量失败：逐条重试，定位具体失败项（长路径先尝试 \\?\ 前缀）
    for path in ok:
        if cancel is not None and cancel.is_set():
            report.aborted = True
            break
        candidates = [path]
        if len(path) >= 240 and not path.startswith("\\\\?\\"):
            candidates.insert(0, _long(path))
        single_code = -1
        for cand in candidates:
            single_code, single_aborted = _shell_delete([cand])
            if single_aborted:
                report.aborted = True
            if single_code == 0:
                break
        if single_code == 0:
            report.deleted.append((path, sizes.get(path, 0)))
            if on_event:
                on_event("deleted", path, sizes.get(path, 0))
        else:
            reason = _describe_code(single_code)
            report.failed.append((path, reason))
            if on_event:
                on_event("failed", path, reason)
    return report


def _describe_code(code: int) -> str:
    mapping = {
        0: "成功",
        2: "文件不存在",
        5: "访问被拒绝（需要管理员权限，或文件被占用）",
        32: "文件正被其他程序使用",
        124: "路径过长或文件名无效",
        0x7C: "路径过长",
        0x402: "回收站不可用（可能空间不足或被禁用）",
        0x10000: "操作被系统取消",
        -1: "系统调用失败",
    }
    return mapping.get(code, f"回收站操作失败（错误码 {code:#x}）")


def empty_recycle_bin(letter: str) -> tuple[bool, str]:
    root = (letter.rstrip(":") + ":\\") if letter else "C:\\"
    flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    try:
        hresult = ctypes.windll.shell32.SHEmptyRecycleBinW(None, ctypes.c_wchar_p(root), flags)
    except (AttributeError, OSError):
        return False, "系统调用失败"
    if hresult == 0:
        return True, ""
    if hresult == 0x8000FFFF:
        return False, "清空回收站失败（可能是权限问题）"
    return False, f"清空回收站失败（{hresult:#x}）"


def open_in_explorer(path: str) -> None:
    """在资源管理器中定位文件 / 打开目录。"""
    try:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])
    except OSError:
        try:
            os.startfile(os.path.dirname(path) or path)  # noqa: S606
        except OSError:
            pass


def open_file(path: str) -> None:
    try:
        os.startfile(path)  # noqa: S606
    except OSError:
        pass


def expand_contents(path: str, limit: int = 20000) -> list[str]:
    """列出目录下的直接子项（用于「清空目录内容」）。"""
    out: list[str] = []
    try:
        with os.scandir(util.long_path(path)) as it:
            for entry in it:
                out.append(entry.path)
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return out
