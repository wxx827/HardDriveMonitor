"""在独立子进程中运行的重任务：完整扫描、清理分析。

GUI 进程只通过 multiprocessing.Queue 收进度消息，任务本身（遍历、汇总、写库、哈希）
全部发生在子进程里，界面不会因为 GIL 或大事务而卡顿。
"""
from __future__ import annotations

import dataclasses
import os
import traceback


def _stats_dict(stats) -> dict:
    return {f.name: getattr(stats, f.name) for f in dataclasses.fields(stats)}


def scan_job(opts_dict: dict, db_path: str, msg_q, cancel) -> None:
    from . import scanner, volumes
    from .index import Index

    try:
        opts = scanner.ScanOptions(**opts_dict)
        result = scanner.scan(opts, lambda st: msg_q.put(("progress", _stats_dict(st))), cancel)
        index = Index(db_path)
        try:
            if result.stats.partial and index.has_index():
                msg_q.put(("cancelled", "已停止扫描，保留原有索引。"))
                return
            msg_q.put(("writing", len(result.dirs)))
            letters = sorted({os.path.splitdrive(r)[0].upper() for r in opts.roots if os.path.isdir(r)})
            vols = [v for v in volumes.list_volumes() if v.letter.upper() in letters]
            index.replace_scan(
                vols, result.dirs, result.big_files, result.recent_files,
                partial=result.stats.partial, drives=letters, stats=result.stats, method=result.stats.method,
            )
            index.checkpoint()
        finally:
            index.close()
        msg_q.put(("done", _stats_dict(result.stats)))
    except Exception:  # noqa: BLE001
        msg_q.put(("error", traceback.format_exc()))


def folder_job(opts_dict: dict, db_path: str, folder: str, msg_q, cancel) -> None:
    """只重新扫描一个文件夹，替换索引里的这棵子树（几秒内完成，不必整盘重扫）。"""
    from . import scanner
    from .index import Index

    try:
        opts = scanner.ScanOptions(**{**opts_dict, "roots": [folder], "method": "scandir"})
        result = scanner.scan(opts, lambda st: msg_q.put(("progress", _stats_dict(st))), cancel)
        if result.stats.partial:
            msg_q.put(("cancelled", "已停止刷新，索引没有改动。"))
            return
        if not os.path.isdir(folder):
            result.dirs, result.big_files, result.recent_files = [], [], []
        index = Index(db_path)
        try:
            change = index.replace_subtree(folder, result.dirs, result.big_files, result.recent_files)
            index.checkpoint()
        finally:
            index.close()
        msg_q.put(("done", {**_stats_dict(result.stats), "folder": folder, **change}))
    except Exception:  # noqa: BLE001
        msg_q.put(("error", traceback.format_exc()))


def analysis_job(db_path: str, app_dir: str, msg_q, cancel) -> None:
    from . import recommend
    from .index import Index

    try:
        index = Index(db_path)
        try:
            items = recommend.analyze(
                index, app_dir, cancel=cancel,
                on_progress=lambda text: msg_q.put(("progress", text)),
            )
            if cancel is not None and cancel.is_set():
                msg_q.put(("cancelled", "已停止分析。"))
                return
            index.replace_cleanup(items)
        finally:
            index.close()
        msg_q.put(("done", len(items)))
    except Exception:  # noqa: BLE001
        msg_q.put(("error", traceback.format_exc()))
