"""大目录迁移：把 C 盘上的大目录复制到其它盘，再在原位置建目录联接（junction），原路径继续可用。

流程：占用探测 → robocopy 复制 → 校验文件数与字节数 → 原目录改名为备份 → 建联接 → 校验。
任何一步失败都会回滚到迁移前的状态；备份目录在用户确认后才移到回收站。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass

from . import cleanup, scanner, util, volumes
from .util import GB

BACKUP_SUFFIX = ".hdm_bak"
CREATE_NO_WINDOW = 0x08000000

# 不建议用联接迁移的目录（系统依赖固定位置，或由同步程序管理）
_EXCLUDE_NAMES = {
    "appdata", "local", "roaming", "locallow", "microsoft", "packages", "temp", "tmp",
    "onedrive", "icloud drive", "dropbox", "desktop", "documents", "downloads", "pictures",
    "videos", "music", "favorites", "links", "contacts", "searches", "saved games",
    "windowsapps", "programs", "comms", "connecteddevicesplatform", "d3dscache",
}


class MigrationError(Exception):
    pass


@dataclass
class MigrationRecord:
    id: str
    src: str
    dst: str
    backup: str
    size: int
    files: int
    created: float
    state: str = "done"      # done / finalized / undone


def is_reparse_point(path: str) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & util.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


def default_target(src: str, root: str) -> str:
    drive, rest = os.path.splitdrive(os.path.abspath(src))
    return os.path.join(root, drive.rstrip(":"), rest.lstrip("\\"))


def check(src: str, dst: str, app_dir: str, size: int | None = None) -> list[str]:
    """返回阻止迁移的问题列表（空列表 = 可以迁移）。"""
    problems: list[str] = []
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isdir(src):
        return ["源目录不存在"]
    if is_reparse_point(src):
        return ["源目录已经是联接 / 链接（可能已经迁移过）"]
    reason = cleanup.protected_reason(src, app_dir)
    if reason:
        problems.append(reason)
    norm_src = os.path.normcase(src).rstrip("\\")
    norm_dst = os.path.normcase(dst).rstrip("\\")
    if norm_dst == norm_src or norm_dst.startswith(norm_src + "\\"):
        problems.append("目标目录不能位于源目录内部")
    if os.path.exists(dst) and (not os.path.isdir(dst) or os.listdir(dst)):
        problems.append("目标目录已存在且不为空")
    src_drive = util.drive_of(src)
    dst_drive = util.drive_of(dst)
    if not dst_drive or not os.path.isdir(dst_drive + "\\"):
        problems.append(f"目标磁盘 {dst_drive or '?'} 不存在")
    elif src_drive == dst_drive:
        problems.append("目标和源在同一个磁盘上，迁移不会释放空间")
    else:
        if size is None:
            size, _count = scanner.measure_dir(src)
        free, _total = volumes.free_space(dst_drive + "\\")
        if free < size * 1.05 + GB:
            problems.append(f"目标磁盘剩余 {util.human_size(free)}，不足以容纳 {util.human_size(size)}")
    return problems


def plan_steps(src: str, dst: str) -> list[str]:
    return [
        f"检查「{src}」是否被程序占用",
        f"复制到「{dst}」（robocopy，保留时间和属性）",
        "核对两边的文件数和总字节数",
        f"把原目录改名为「{os.path.basename(src)}{BACKUP_SUFFIX}」作为备份",
        f"在原位置创建指向「{dst}」的目录联接，程序照旧通过原路径访问",
        "确认一切正常后，再把备份移到回收站（清空回收站后释放 C 盘空间）",
    ]


def _backup_path(src: str) -> str:
    base = src.rstrip("\\") + BACKUP_SUFFIX
    path = base
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{base}{n}"
    return path


def _probe_in_use(src: str) -> None:
    probe = src.rstrip("\\") + ".hdm_probe"
    try:
        os.rename(src, probe)
    except OSError as exc:
        raise MigrationError(f"目录正被程序占用，请先关闭相关程序后再迁移（{exc.strerror or exc}）") from exc
    try:
        os.rename(probe, src)
    except OSError as exc:
        raise MigrationError(f"探测后无法还原目录名，请手动把 {probe} 改回 {src}") from exc


def _robocopy(src: str, dst: str, cancel: threading.Event | None, extra: list[str] | None = None) -> None:
    cmd = ["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1", "/XJ",
           "/MT:8", "/NP", "/NFL", "/NDL", "/NJH", "/NJS"] + (extra or [])
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW)
    while True:
        try:
            code = proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                proc.wait(timeout=10)
                raise MigrationError("已取消迁移")
    if code >= 8:
        raise MigrationError(f"复制失败（robocopy 返回 {code}），可能有文件被占用或权限不足")


def _rmtree(path: str) -> None:
    if os.path.isdir(path) and not is_reparse_point(path):
        shutil.rmtree(util.long_path(path), ignore_errors=True)


def migrate(
    src: str,
    dst: str,
    app_dir: str,
    on_progress=None,
    cancel: threading.Event | None = None,
    dry_run: bool = False,
    log_path: str | None = None,
) -> MigrationRecord | list[str]:
    """执行迁移；dry_run=True 时只校验并返回步骤说明。失败抛 MigrationError 且已回滚。"""
    src = os.path.abspath(src).rstrip("\\")
    dst = os.path.abspath(dst).rstrip("\\")

    def progress(text: str) -> None:
        if on_progress:
            on_progress(text)

    progress("正在检查…")
    size, files = scanner.measure_dir(src)
    problems = check(src, dst, app_dir, size)
    if problems:
        raise MigrationError("；".join(problems))
    if dry_run:
        return plan_steps(src, dst)

    _probe_in_use(src)
    created_dst = not os.path.exists(dst)
    try:
        progress(f"正在复制 {util.human_size(size)}（{files:,} 个文件）…")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _robocopy(src, dst, cancel)
        progress("正在校验…")
        dst_size, dst_files = scanner.measure_dir(dst)
        if (dst_size, dst_files) != (size, files):
            raise MigrationError(
                f"校验不一致：源 {files:,} 个文件 / {util.human_size(size)}，"
                f"目标 {dst_files:,} 个文件 / {util.human_size(dst_size)}（复制期间可能有程序在写入）"
            )
    except BaseException:
        if created_dst:
            _rmtree(dst)
        raise

    backup = _backup_path(src)
    progress("正在建立目录联接…")
    try:
        os.rename(src, backup)
    except OSError as exc:
        _rmtree(dst)
        raise MigrationError(f"无法改名原目录（可能被占用）：{exc}") from exc
    try:
        _create_junction(dst, src)
        if len(os.listdir(src)) != len(os.listdir(dst)):
            raise MigrationError("联接校验失败")
    except BaseException as exc:
        if os.path.lexists(src) and is_reparse_point(src):
            os.rmdir(src)
        os.rename(backup, src)
        _rmtree(dst)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"创建目录联接失败：{exc}") from exc

    record = MigrationRecord(
        id=uuid.uuid4().hex[:12], src=src, dst=dst, backup=backup,
        size=int(size), files=int(files), created=time.time(),
    )
    if log_path:
        save_record(log_path, record)
    progress("迁移完成")
    return record


def _create_junction(target: str, link: str) -> None:
    import _winapi

    _winapi.CreateJunction(target, link)


def finalize(record: MigrationRecord, app_dir: str, log_path: str | None = None):
    """把备份目录移到回收站。"""
    report = cleanup.delete_paths([record.backup], app_dir)
    if report.deleted or not os.path.exists(record.backup):
        record.state = "finalized"
        if log_path:
            save_record(log_path, record)
    return report


def undo(record: MigrationRecord, cancel: threading.Event | None = None, log_path: str | None = None) -> None:
    """撤销迁移：移除联接，恢复原目录，删除目标副本。"""
    if os.path.lexists(record.src) and is_reparse_point(record.src):
        os.rmdir(record.src)
    elif os.path.exists(record.src):
        raise MigrationError("原位置已经是普通目录，无需撤销")
    if os.path.isdir(record.backup):
        os.rename(record.backup, record.src)
        _rmtree(record.dst)
    else:
        # 备份已删除：把数据从目标盘搬回来
        os.makedirs(record.src, exist_ok=True)
        _robocopy(record.dst, record.src, cancel, extra=["/MOVE"])
        _rmtree(record.dst)
    record.state = "undone"
    if log_path:
        save_record(log_path, record)


def load_records(log_path: str) -> list[MigrationRecord]:
    try:
        with open(log_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(MigrationRecord(**item))
        except TypeError:
            continue
    return out


def save_record(log_path: str, record: MigrationRecord) -> None:
    records = [r for r in load_records(log_path) if r.id != record.id]
    records.append(record)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    tmp = log_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in records], fh, ensure_ascii=False, indent=2)
    os.replace(tmp, log_path)


def suggest(index, min_size: int = 5 * GB, profile: str | None = None, limit: int = 12) -> list[dict]:
    """在 C 盘用户目录里挑出适合迁移的大目录（尽量选最能代表占用的那一层）。"""
    profile = (profile or os.environ.get("USERPROFILE") or "").rstrip("\\")
    if not profile or util.drive_of(profile) != "C:":
        return []
    base_depth = util.path_depth(profile)
    rows = index.dirs_under(profile, min_size, base_depth + 1, base_depth + 5, limit=400)
    candidates = []
    for row in rows:
        path = row["path"]
        rel = path[len(profile):].lower()
        parts = [p for p in rel.split("\\") if p]
        if not parts or parts[-1] in _EXCLUDE_NAMES:
            continue
        if parts[0] in ("onedrive",) or "\\temp\\" in rel + "\\" or "\\packages\\" in rel:
            continue
        if parts[-1].endswith(BACKUP_SUFFIX) or ".hdm_bak" in parts[-1]:
            continue
        candidates.append(row)
    candidates.sort(key=lambda r: -r["depth"])
    chosen: list[dict] = []
    for row in candidates:
        prefix = row["path"].lower() + "\\"
        inner = [c for c in chosen if c["path"].lower().startswith(prefix)]
        if sum(c["size"] for c in inner) >= row["size"] * 0.6:
            continue
        chosen = [c for c in chosen if c not in inner]
        chosen.append(row)
    chosen = [c for c in chosen if os.path.isdir(c["path"]) and not is_reparse_point(c["path"])]
    chosen.sort(key=lambda r: -r["size"])
    return chosen[:limit]
