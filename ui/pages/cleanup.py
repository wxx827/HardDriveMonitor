"""清理推荐：按类别分组的可勾选清单 + 迁移建议。"""
from __future__ import annotations

import csv
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHeaderView, QMenu, QTreeWidget, QTreeWidgetItem

from core import cleanup, util
from core.recommend import CATEGORIES, LEVEL_TEXT, summarize
from .. import theme
from ..actions import copy_text
from ..widgets.common import Card, button, glyph_icon, hbox, info, label
from .base import Page

ITEM_ROLE = Qt.UserRole + 3


class CleanupPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.checked: set[str] = set()
        self._building = False

        self.analyze_btn = button("重新分析", "primary", "sparkle", "测量缓存、查找旧安装包和重复文件",
                                  lambda: win.start_analysis())
        self.safe_btn = button("全选安全项", on_click=lambda: self._select_level({"safe"}))
        self.caution_btn = button("全选谨慎项", on_click=lambda: self._select_level({"caution"}))
        self.clear_btn = button("清除选择", "ghost", on_click=lambda: self._select_level(set()))
        self.summary = label("", "muted", wrap=True)
        self.body.addLayout(hbox(self.analyze_btn, self.safe_btn, self.caution_btn, self.clear_btn, None))
        self.body.addWidget(self.summary)

        card = Card()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["类别 / 项目", "大小", "等级", "说明", "路径"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        header = self.tree.header()
        header.setStretchLastSection(False)
        for col, width in ((0, 300), (1, 90), (2, 70), (3, 380), (4, 300)):
            self.tree.setColumnWidth(col, width)
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.customContextMenuRequested.connect(self._menu)
        card.body.addWidget(self.tree, 1)

        self.selected_label = label("已选 0 项", "h2")
        self.open_btn = button("打开位置", None, "folder", on_click=self._open)
        self.ai_btn = button("问 AI", None, "ai", on_click=self._ask_ai)
        self.export_btn = button("导出 CSV", "ghost", "export", on_click=self._export)
        self.records_btn = button("迁移记录", "ghost", on_click=self._show_migrations)
        self.migrate_btn = button("迁移到其它盘…", None, "move", "用目录联接把大目录搬到其它盘，路径不变", self._migrate)
        self.delete_btn = button("移到回收站", "danger", "delete", on_click=self._delete)
        card.body.addLayout(hbox(self.selected_label, None, self.export_btn, self.records_btn, self.open_btn,
                                 self.ai_btn, self.migrate_btn, self.delete_btn))
        self.body.addWidget(card, 1)
        self._update_buttons()

    # ---------------- 构建 ----------------
    def refresh(self) -> None:
        items = self.win.cleanup_items
        paths = {i.path for i in items if i.deletable}
        self.checked &= paths
        self._building = True
        self.tree.clear()
        grouped: dict[str, list] = {}
        for item in items:
            grouped.setdefault(item.category, []).append(item)
        order = [c for c in CATEGORIES if c in grouped] + [c for c in grouped if c not in CATEGORIES]
        bold = self.tree.font()
        bold.setBold(True)
        for category in order:
            entries = grouped[category]
            deletable = [i for i in entries if i.deletable]
            total = sum(i.size for i in (deletable or entries))
            top = QTreeWidgetItem([f"{category}（{len(entries)} 项）", util.human_size(total), "", "", ""])
            top.setFont(0, bold)
            top.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if deletable:
                flags |= Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate
            top.setFlags(flags)
            self.tree.addTopLevelItem(top)
            for item in entries:
                child = QTreeWidgetItem([item.title, util.human_size(item.size), LEVEL_TEXT.get(item.level, item.level),
                                         item.reason, item.path])
                child.setData(0, ITEM_ROLE, item)
                child.setToolTip(3, item.reason)
                child.setToolTip(4, item.path)
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setForeground(2, theme.level_color(item.level))
                if item.kind == "migrate":
                    child.setIcon(0, glyph_icon("move", theme.COLORS["accent"], 16))
                    child.setText(2, "迁移")
                    child.setForeground(2, theme.color("accent"))
                child_flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                if item.deletable:
                    child_flags |= Qt.ItemIsUserCheckable
                    child.setFlags(child_flags)
                    child.setCheckState(0, Qt.Checked if item.path in self.checked else Qt.Unchecked)
                else:
                    child.setFlags(child_flags)
                    if item.kind != "migrate":
                        child.setForeground(0, theme.color("muted"))
                top.addChild(child)
            top.setExpanded(category not in ("系统占用（仅说明）", "重要数据（勿删）"))
        self._building = False
        self._update_summary()
        self._update_buttons()

    def on_show(self) -> None:
        self._update_buttons()

    def on_busy(self, busy: bool) -> None:
        self.analyze_btn.setEnabled(not busy)
        self._update_buttons()

    def _update_summary(self) -> None:
        items = self.win.cleanup_items
        if not items:
            self.summary.setText("还没有分析结果。扫描完成后会自动分析，也可以点「重新分析」（通常几十秒）。")
            return
        s = summarize(items)
        migrate = [i for i in items if i.kind == "migrate"]
        text = (f"可回收约 {util.human_size(s['reclaimable'])}：安全 {util.human_size(s['totals'].get('safe', 0))}、"
                f"谨慎 {util.human_size(s['totals'].get('caution', 0))}。所有删除都会先移到回收站，可以还原。")
        if migrate:
            text += f"　另有 {len(migrate)} 个大目录可迁移到其它盘（{util.human_size(sum(i.size for i in migrate))}）。"
        self.summary.setText(text)

    # ---------------- 勾选 ----------------
    def _iter_children(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                yield top.child(j)

    def _on_item_changed(self, child: QTreeWidgetItem, column: int) -> None:
        if self._building or column != 0:
            return
        item = child.data(0, ITEM_ROLE)
        if item is not None and item.deletable:
            if child.checkState(0) == Qt.Checked:
                self.checked.add(item.path)
            else:
                self.checked.discard(item.path)
        self._update_buttons()

    def _select_level(self, levels: set[str]) -> None:
        self._building = True
        for child in self._iter_children():
            item = child.data(0, ITEM_ROLE)
            if item is None or not item.deletable:
                continue
            on = item.level in levels
            child.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
            if on:
                self.checked.add(item.path)
            else:
                self.checked.discard(item.path)
        self._building = False
        self._update_buttons()

    def checked_items(self) -> list:
        return [i for i in self.win.cleanup_items if i.deletable and i.path in self.checked]

    def _current_item(self):
        cur = self.tree.currentItem()
        return cur.data(0, ITEM_ROLE) if cur is not None else None

    def _update_buttons(self) -> None:
        selected = self.checked_items()
        total = sum(i.size for i in selected)
        self.selected_label.setText(f"已选 {len(selected)} 项 · {util.human_size(total)}")
        busy = self.win.is_busy()
        self.delete_btn.setEnabled(bool(selected) and not busy)
        cur = self._current_item()
        self.open_btn.setEnabled(cur is not None)
        self.ai_btn.setEnabled(cur is not None or bool(selected))
        can_migrate = cur is not None and cur.kind in ("migrate", "info", "dir") and os.path.isdir(cur.path)
        self.migrate_btn.setEnabled(can_migrate and not busy)
        self.safe_btn.setEnabled(bool(self.win.cleanup_items))
        self.caution_btn.setEnabled(bool(self.win.cleanup_items))

    # ---------------- 动作 ----------------
    def selected_records(self) -> list[dict]:
        items = self.checked_items() or ([self._current_item()] if self._current_item() else [])
        return [{"path": i.path, "size": i.size, "mtime": 0, "ext": "", "is_dir": os.path.isdir(i.path),
                 "name": i.title} for i in items if i is not None]

    def _on_double_click(self, child: QTreeWidgetItem, _col: int) -> None:
        item = child.data(0, ITEM_ROLE)
        if item is None:
            return
        if item.kind == "migrate":
            self.win.migrate_dir(item.path, item.size)
        elif os.path.exists(item.path):
            cleanup.open_in_explorer(item.path)

    def _open(self) -> None:
        item = self._current_item()
        if item and os.path.exists(item.path):
            cleanup.open_in_explorer(item.path)

    def _ask_ai(self) -> None:
        records = self.selected_records()
        if records:
            self.win.ask_ai(records[:20])

    def _migrate(self) -> None:
        item = self._current_item()
        if item:
            self.win.migrate_dir(item.path, item.size)

    def _delete(self) -> None:
        items = self.checked_items()
        if items:
            self.win.delete_cleanup(items)

    def _show_migrations(self) -> None:
        from ..dialogs import MigrationsDialog

        MigrationsDialog(self.win).exec()

    def _menu(self, pos) -> None:
        child = self.tree.itemAt(pos)
        item = child.data(0, ITEM_ROLE) if child is not None else None
        if item is None:
            return
        menu = QMenu(self)
        exists = os.path.exists(item.path)
        acts = [
            ("打开位置", "folder", lambda: cleanup.open_in_explorer(item.path), exists),
            ("在「空间分布」中查看", "treemap", lambda: self.win.goto_explorer(item.path), os.path.isdir(item.path)),
            ("复制路径", "copy", lambda: copy_text(item.path), True),
            ("问 AI：这是什么、能不能删", "ai", lambda: self.win.ask_ai(self.selected_records()[:20] or [
                {"path": item.path, "size": item.size, "name": item.title}]), True),
        ]
        if item.kind in ("migrate", "info", "dir") and os.path.isdir(item.path):
            acts.append(("迁移到其它盘…", "move", lambda: self.win.migrate_dir(item.path, item.size),
                         not self.win.is_busy()))
        for text, icon, fn, enabled in acts:
            action = menu.addAction(glyph_icon(icon, None, 16), text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _c=False, f=fn: f())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _export(self) -> None:
        items = self.win.cleanup_items
        if not items:
            info(self, "导出", "还没有分析结果。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出清理清单", "清理清单.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["类别", "项目", "路径", "大小(字节)", "大小", "等级", "可删除", "说明"])
                for item in items:
                    writer.writerow([item.category, item.title, item.path, item.size, util.human_size(item.size),
                                     LEVEL_TEXT.get(item.level, item.level), "是" if item.deletable else "否",
                                     item.reason])
        except OSError as exc:
            info(self, "导出失败", str(exc))
            return
        self.win.status(f"清理清单已导出到 {path}")
