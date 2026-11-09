"""最近文件：最近 N 天新增 / 修改的文件，找出「是什么突然占了空间」。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit

from core import cleanup, util
from .. import workers
from ..actions import show_record_menu
from ..models import Column, RecordTableModel, make_table, selected_rows
from ..widgets.common import Card, Segmented, button, hbox, label
from .base import Page

DAYS = [("1", "今天"), ("3", "3 天"), ("7", "7 天"), ("30", "30 天"), ("180", "半年")]


class RecentPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self._latest = workers.Latest()
        self._debounce = workers.Debouncer(300, self)
        self._debounce.triggered.connect(self._query)

        self.days = Segmented(DAYS, "7")
        self.days.changed.connect(lambda _v: self._query())
        self.drive = QComboBox()
        self.drive.currentIndexChanged.connect(lambda _i: self._query())
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("按文件名或路径搜索")
        self.keyword.setClearButtonEnabled(True)
        self.keyword.setMinimumWidth(240)
        self.keyword.textChanged.connect(self._debounce.poke)
        self.exclude = QCheckBox("隐藏缓存 / 系统文件")
        self.exclude.setChecked(True)
        self.exclude.toggled.connect(lambda _c: self._query())
        self.body.addLayout(hbox(self.days, self.drive, self.keyword, self.exclude, None))

        card = Card()
        self.summary = label("", "muted")
        self.delete_btn = button("移到回收站", "danger", "delete", on_click=self._delete)
        self.open_btn = button("打开位置", None, "folder", on_click=self._open)
        card.body.addLayout(hbox(self.summary, None, self.open_btn, self.delete_btn))
        self.model = RecordTableModel([
            Column("name", "名称", 280),
            Column("size", "大小", 96, "right", fmt=lambda r: util.human_size(r["size"])),
            Column("mtime", "修改时间", 140, fmt=lambda r: util.fmt_time(r["mtime"])),
            Column("path", "位置", 420, fmt=lambda r: os.path.dirname(r["path"]), stretch=True),
        ])
        self.table = make_table(self.model, multi=True, sort_key="mtime")
        self.table.doubleClicked.connect(lambda idx: cleanup.open_in_explorer(self.model.rows[idx.row()]["path"]))
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        card.body.addWidget(self.table, 1)
        self.body.addWidget(card, 1)
        QShortcut(QKeySequence("Delete"), self.table, activated=self._delete, context=Qt.WidgetShortcut)
        self._update_buttons()

    def refresh(self) -> None:
        current = self.drive.currentData()
        self.drive.blockSignals(True)
        self.drive.clear()
        self.drive.addItem("全部磁盘", None)
        for r in self.win.index.root_dirs():
            d = r["path"].rstrip("\\")
            self.drive.addItem(d, d)
        self.drive.setCurrentIndex(max(0, self.drive.findData(current)))
        self.drive.blockSignals(False)
        self._query()

    def _query(self) -> None:
        index = self.win.index
        args = dict(days=int(self.days.value() or 7), drive=self.drive.currentData(),
                    exclude_cache=self.exclude.isChecked(), keyword=self.keyword.text().strip() or None,
                    page=0, page_size=20000)
        self.summary.setText("正在查询…")
        self._latest.run(lambda: index.recent_files(**args), self._on_rows,
                         lambda _e, tb: self.summary.setText("查询失败"))

    def _on_rows(self, result) -> None:
        rows, count, total = result
        self.model.set_rows(rows)
        more = f"（显示最新 {len(rows):,} 个）" if count > len(rows) else ""
        cap = self.win.config.scan.get("recent_days") or 180
        self.summary.setText(f"{count:,} 个文件，合计 {util.human_size(total)}{more}　·　索引只记录最近 {cap} 天")
        self._update_buttons()

    def on_busy(self, busy: bool) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = bool(self.table.selectionModel().selectedRows())
        self.delete_btn.setEnabled(has and not self.win.is_busy())
        self.open_btn.setEnabled(has)

    def selected_records(self) -> list[dict]:
        return selected_rows(self.table, self.model)

    def _menu(self, pos) -> None:
        rows = self.selected_records()
        if rows:
            show_record_menu(self.win, self, rows, self.table.viewport().mapToGlobal(pos), allow_migrate=False)

    def _delete(self) -> None:
        rows = self.selected_records()
        if rows:
            self.win.delete_records(rows)

    def _open(self) -> None:
        rows = self.selected_records()
        if rows:
            cleanup.open_in_explorer(rows[0]["path"])
