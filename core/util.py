"""通用工具：大小/时间格式化、路径处理、Windows 文件属性判定。"""
from __future__ import annotations

import datetime
import os
import time

# Windows 文件属性位
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

# 文件标记位（写入 index.db 的 flags 列）
FLAG_SYSTEM = 1        # 系统 / 程序目录
FLAG_PLACEHOLDER = 2   # OneDrive 等云端占位文件
FLAG_HARDLINK = 4      # 硬链接重复项
FLAG_CACHE = 8         # 缓存 / 临时目录

KB = 1024
MB = 1024 * KB
GB = 1024 * MB


def human_size(n: int | float | None) -> str:
    """把字节数换成人话：0 B / 999 B / 1.0 KB / 12.3 MB / 7.1 GB。"""
    if n is None:
        return "-"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < KB:
        return f"{sign}{int(n)} B"
    if n < MB:
        return f"{sign}{n / KB:.1f} KB"
    if n < GB:
        return f"{sign}{n / MB:.1f} MB"
    if n < 1024 * GB:
        return f"{sign}{n / GB:.2f} GB"
    return f"{sign}{n / (1024 * GB):.2f} TB"


def human_count(n: int | None) -> str:
    if n is None:
        return "-"
    return f"{int(n):,}"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def fmt_date(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "-"


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours = minutes // 60
    return f"{hours} 小时 {minutes % 60} 分"


def long_path(path: str) -> str:
    """超过 240 字符的路径加 \\\\?\\ 前缀，避免 MAX_PATH 限制。"""
    if not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    if len(path) < 240:
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def drive_of(path: str) -> str:
    """返回盘符，例如 'C:'。"""
    drive, _ = os.path.splitdrive(path)
    return drive.upper()


def path_depth(path: str) -> int:
    """绝对路径层级：C:\\ = 1，C:\\a = 2，C:\\a\\b = 3。"""
    return len([p for p in path.replace("/", "\\").split("\\") if p])


def ext_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return ext if ext else ""


_PROGRAM_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system volume information",
    "recovery",
    "perflogs",
    "documents and settings",
    "$recycle.bin",
    "config.msi",
}


def is_system_path(path: str) -> bool:
    """是否位于系统 / 程序安装目录（这类文件默认不进清理推荐、列表里可隐藏）。"""
    parts = [p for p in path.replace("/", "\\").split("\\") if p]
    if not parts:
        return False
    # 盘符 + 一级目录
    first = parts[1].lower() if len(parts) > 1 else ""
    if first in _PROGRAM_DIR_NAMES:
        return True
    # C:\Users\Public / 默认用户目录
    if first == "users" and len(parts) > 2 and parts[2].lower() in ("public", "default", "all users"):
        return True
    return False


_TEMP_HINTS = (
    "\\temp\\",
    "\\tmp\\",
    "\\cache\\",
    "\\caches\\",
    "\\code cache\\",
    "\\gpucache\\",
    "\\node_modules\\",
    "\\__pycache__\\",
    "\\pip\\",
    "\\.cache\\",
    "\\.gradle\\",
    "\\.m2\\",
    "\\.conda\\",
    "\\appdata\\local\\temp",
)


def is_cache_like(path: str) -> bool:
    low = path.lower()
    return any(h in low for h in _TEMP_HINTS)


def file_flags(path: str, attrs: int = 0) -> int:
    flags = 0
    if is_system_path(path):
        flags |= FLAG_SYSTEM
    if attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS or attrs & FILE_ATTRIBUTE_RECALL_ON_OPEN:
        flags |= FLAG_PLACEHOLDER
    if is_cache_like(path):
        flags |= FLAG_CACHE
    return flags


def mask_user(text: str, username: str | None = None) -> str:
    """把路径里的用户名替换成 <用户>，用于发送给 AI 前脱敏。"""
    if not text:
        return text
    if username is None:
        username = os.environ.get("USERNAME") or ""
    if username:
        import re

        text = re.sub(
            r"(?i)([a-z]:\\users\\)" + re.escape(username) + r"(?=\\|$)",
            r"\1<用户>",
            text,
        )
        text = re.sub(
            r"(?i)(/users/)" + re.escape(username) + r"(?=/|$)",
            r"\1<用户>",
            text,
        )
    return text


def now_ts() -> float:
    return time.time()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def format_ext(ext: str) -> str:
    return ext[1:].upper() if ext else "无扩展名"


def short_path(path: str, max_len: int = 70) -> str:
    if len(path) <= max_len:
        return path
    head = path[: max_len // 2 - 3]
    tail = path[-(max_len // 2):]
    return f"{head}…{tail}"
