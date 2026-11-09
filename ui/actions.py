"""文件 / 目录的通用右键菜单。"""
from __future__ import annotations

import os

from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QMenu

from core import cleanup
from .widgets.common import glyph_icon


def copy_text(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def show_record_menu(win, parent, records: list[dict], pos, allow_migrate: bool = True, extra=None) -> None:
    """records: [{'path','size','mtime','ext','name', 'is_dir'?}]，pos 为全局坐标。"""
    if not records:
        return
    first = records[0]
    path = first["path"]
    is_dir = bool(first.get("is_dir")) or os.path.isdir(path)
    menu = QMenu(parent)

    def add(text, icon, fn, enabled=True):
        action = QAction(glyph_icon(icon, None, 16), text, menu)
        action.setEnabled(enabled)
        action.triggered.connect(lambda _c=False: fn())
        menu.addAction(action)
        return action

    if len(records) == 1:
        if is_dir:
            add("打开文件夹", "folder", lambda: cleanup.open_in_explorer(path))
            if hasattr(win, "goto_explorer"):
                add("在「空间分布」中查看", "treemap", lambda: win.goto_explorer(path))
        else:
            add("打开文件", "open", lambda: cleanup.open_file(path))
            add("在资源管理器中显示", "folder", lambda: cleanup.open_in_explorer(path))
            if hasattr(win, "goto_explorer"):
                add("在「空间分布」中查看所在目录", "treemap", lambda: win.goto_explorer(os.path.dirname(path)))
        add("复制路径", "copy", lambda: copy_text(path))
    else:
        add(f"复制 {len(records)} 个路径", "copy", lambda: copy_text("\n".join(r["path"] for r in records)))
    menu.addSeparator()
    add("问 AI：这是什么、能不能删", "ai", lambda: win.ask_ai(records[:20]))
    if is_dir and len(records) == 1 and hasattr(win, "rescan_folder"):
        add("重新扫描此文件夹", "scan", lambda: win.rescan_folder(path), enabled=not win.is_busy())
    if allow_migrate and is_dir and len(records) == 1:
        add("迁移到其它盘…", "move", lambda: win.migrate_dir(path, int(first.get("size") or 0)))
    if extra:
        menu.addSeparator()
        for text, icon, fn in extra:
            add(text, icon, fn)
    menu.addSeparator()
    label = "移到回收站…" if len(records) == 1 else f"把 {len(records)} 项移到回收站…"
    add(label, "delete", lambda: win.delete_records(records), enabled=not win.is_busy())
    menu.exec(pos)
