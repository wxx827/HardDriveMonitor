"""大文件：按盘符 / 大小 / 类型 / 关键字筛选，全部结果一次装入虚拟表格。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit

from core import cleanup, util
from .. import workers
from ..actions import show_record_menu
from ..models import Column, RecordTableModel, flags_text, make_table, selected_rows
from ..widgets.common import Card, button, hbox, label
from .base import Page

TYPE_GROUPS = [
    ("全部类型", None),
    ("视频", [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".m2ts", ".webm"]),
    ("压缩包", [".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".bz2", ".zst"]),
    ("安装包", [".exe", ".msi", ".msix", ".appx", ".apk", ".dmg"]),
    ("镜像 / 虚拟机", [".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".vdi", ".wim", ".esd"]),
    ("模型 / 数据集", [".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".gguf", ".bin", ".h5", ".npz", ".npy",
                   ".parquet", ".arrow", ".pkl", ".tfrecord"]),
    ("日志 / 转储", [".log", ".dmp", ".etl", ".tmp", ".bak", ".old"]),
    ("文档 / 图片", [".pdf", ".docx", ".pptx", ".xlsx", ".psd", ".jpg", ".png", ".raw", ".cr2", ".nef"]),
]

SIZE_OPTIONS = [("≥ 100 MB", 100), ("≥ 500 MB", 500), ("≥ 1 GB", 1024), ("≥ 5 GB", 5 * 1024)]


class BigFilesPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self._latest = workers.Latest()
        self._debounce = workers.Debouncer(300, self)
        self._debounce.triggered.connect(self._query)

        self.drive = QComboBox()
        self.size = QComboBox()
        for text, mb in SIZE_OPTIONS:
            self.size.addItem(text, mb)
        self.kind = QComboBox()
        for text, exts in TYPE_GROUPS:
            self.kind.addItem(text, exts)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("按文件名或路径搜索")
        self.keyword.setClearButtonEnabled(True)
        self.keyword.setMinimumWidth(240)
        self.system = QCheckBox("包含系统 / 程序目录")
        for combo in (self.drive, self.size, self.kind):
            combo.currentIndexChanged.connect(lambda _i: self._query())
        self.keyword.textChanged.connect(self._debounce.poke)
        self.system.toggled.connect(lambda _c: self._query())
        self.body.addLayout(hbox(self.drive, self.size, self.kind, self.keyword, self.system, None))

        card = Card()
        self.summary = label("", "muted")
        self.delete_btn = button("移到回收站", "danger", "delete", on_click=self._delete)
        self.open_btn = button("打开位置", None, "folder", on_click=self._open)
        self.ai_btn = button("问 AI", None, "ai", on_click=lambda: self._ask_ai())
        card.body.addLayout(hbox(self.summary, None, self.open_btn, self.ai_btn, self.delete_btn))
        self.model = RecordTableModel([
            Column("name", "名称", 280),
            Column("size", "大小", 96, "right", fmt=lambda r: util.human_size(r["size"])),
            Column("ext", "类型", 72, fmt=lambda r: r.get("ext") or ""),
            Column("mtime", "修改时间", 140, fmt=lambda r: util.fmt_time(r["mtime"])),
            Column("flags", "标记", 90, fmt=lambda r: flags_text(r.get("flags") or 0), color=lambda r: "muted"),
            Column("path", "位置", 360, fmt=lambda r: os.path.dirname(r["path"]), stretch=True),
        ])
        self.table = make_table(self.model, multi=True, sort_key="size")
        self.table.doubleClicked.connect(lambda idx: cleanup.open_in_explorer(self.model.rows[idx.row()]["path"]))
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        card.body.addWidget(self.table, 1)
        self.body.addWidget(card, 1)
        QShortcut(QKeySequence("Delete"), self.table, activated=self._delete, context=Qt.WidgetShortcut)
        self._update_buttons()

    def _fill_drives(self) -> None:
        current = self.drive.currentData()
        drives = [r["path"].rstrip("\\") for r in self.win.index.root_dirs()]
        self.drive.blockSignals(True)
        self.drive.clear()
        self.drive.addItem("全部磁盘", None)
        for d in drives:
            self.drive.addItem(d, d)
        pos = self.drive.findData(current)
        self.drive.setCurrentIndex(max(0, pos))
        self.drive.blockSignals(False)

    def refresh(self) -> None:
        self._fill_drives()
        self._query()

    def _query(self) -> None:
        index = self.win.index
        args = dict(
            drive=self.drive.currentData(),
            min_size=int(self.size.currentData() or 100) * util.MB,
            exts=self.kind.currentData(),
            keyword=self.keyword.text().strip() or None,
            include_system=self.system.isChecked(),
            page=0,
            page_size=20000,
        )
        self.summary.setText("正在查询…")
        self._latest.run(lambda: index.big_files(**args), self._on_rows,
                         lambda _e, tb: self.summary.setText("查询失败"))

    def _on_rows(self, result) -> None:
        rows, count, total = result
        self.model.set_rows(rows)
        more = f"（显示前 {len(rows):,} 个）" if count > len(rows) else ""
        self.summary.setText(f"共 {count:,} 个文件，合计 {util.human_size(total)}{more}")
        self._update_buttons()

    def on_busy(self, busy: bool) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = bool(self.table.selectionModel().selectedRows())
        self.delete_btn.setEnabled(has and not self.win.is_busy())
        self.open_btn.setEnabled(has)
        self.ai_btn.setEnabled(has)

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

    def _ask_ai(self) -> None:
        rows = self.selected_records()
        if rows:
            self.win.ask_ai(rows[:20])
