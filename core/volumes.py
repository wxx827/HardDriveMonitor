"""磁盘卷信息（ctypes 调用 Windows API，不依赖第三方库）。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

DRIVE_FIXED = 3
DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4

_kernel32 = ctypes.windll.kernel32


@dataclass
class Volume:
    letter: str          # 'C:'
    label: str
    fs: str
    total: int
    free: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def used_pct(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 0.0

    @property
    def free_pct(self) -> float:
        return (self.free / self.total * 100.0) if self.total else 0.0

    @property
    def root(self) -> str:
        return self.letter + "\\"


def _volume_label(root: str) -> tuple[str, str]:
    name_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    ok = _kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        name_buf,
        len(name_buf),
        None,
        None,
        None,
        fs_buf,
        len(fs_buf),
    )
    if not ok:
        return "", ""
    return name_buf.value, fs_buf.value


def free_space(root: str) -> tuple[int, int]:
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ok = _kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(root),
        ctypes.byref(free),
        ctypes.byref(total),
        None,
    )
    if not ok:
        return 0, 0
    return int(free.value), int(total.value)


def logical_drives(include_removable: bool = False) -> list[str]:
    mask = _kernel32.GetLogicalDrives()
    out: list[str] = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = f"{chr(ord('A') + i)}:\\"
        dtype = _kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if dtype == DRIVE_FIXED or (include_removable and dtype == DRIVE_REMOVABLE):
            out.append(chr(ord("A") + i) + ":")
    return out


def list_volumes(include_removable: bool = False) -> list[Volume]:
    volumes: list[Volume] = []
    for letter in logical_drives(include_removable):
        root = letter + "\\"
        free, total = free_space(root)
        if total <= 0:
            continue
        label, fs = _volume_label(root)
        volumes.append(Volume(letter=letter, label=label, fs=fs, total=total, free=free))
    return volumes


def volume_for(letter: str) -> Volume | None:
    for vol in list_volumes():
        if vol.letter.upper() == letter.upper():
            return vol
    return None


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def recycle_bin_info(letter: str) -> tuple[int, int]:
    """返回 (回收站占用字节数, 项目数)。"""
    if not letter:
        return 0, 0
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    root = (letter.rstrip(":") + ":\\") if letter else "C:\\"
    try:
        hresult = ctypes.windll.shell32.SHQueryRecycleBinW(ctypes.c_wchar_p(root), ctypes.byref(info))
    except (AttributeError, OSError):
        return 0, 0
    if hresult != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def recycle_bin_size(letter: str) -> int:
    return recycle_bin_info(letter)[0]
