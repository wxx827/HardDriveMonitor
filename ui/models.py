"""表格模型：一次性装入上万行，排序在内存里完成；只有可见行会被绘制。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from core import util
from . import theme
from .widgets.delegates import SHARE_ROLE, ShareBarDelegate

SORT_ROLE = Qt.UserRole


@dataclass
class Column:
    key: str
    title: str
    width: int = 100
    align: str = "left"                       # left / right / center
    fmt: Callable[[dict], str] | None = None
    sort: Callable[[dict], Any] | None = None
    color: Callable[[dict], str | None] | None = None   # 返回主题颜色名
    share: Callable[[dict], float | None] | None = None  # 占比条
    stretch: bool = False


_ALIGN = {
    "left": Qt.AlignLeft | Qt.AlignVCenter,
    "right": Qt.AlignRight | Qt.AlignVCenter,
    "center": Qt.AlignCenter,
}


class RecordTableModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows: list[dict] = []
        self._sort_col = -1
        self._sort_order = Qt.DescendingOrder

    # ---------------- 数据 ----------------
    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        if self._sort_col >= 0:
            self._sort_rows(self._sort_col, self._sort_order)
        self.endResetModel()

    def row(self, i: int) -> dict | None:
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def remove_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        self.beginResetModel()
        self.rows = [r for r in self.rows if r.get("path") not in paths]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = self.columns[index.column()]
        if role == Qt.DisplayRole:
            if col.share is not None:
                return None
            if col.fmt:
                return col.fmt(row)
            value = row.get(col.key)
            return "" if value is None else str(value)
        if role == Qt.TextAlignmentRole:
            return _ALIGN.get(col.align, _ALIGN["left"])
        if role == Qt.ToolTipRole:
            return row.get("path") or None
        if role == Qt.ForegroundRole and col.color:
            name = col.color(row)
            return theme.color(name) if name else None
        if role == SHARE_ROLE and col.share is not None:
            return col.share(row)
        if role == SORT_ROLE:
            return col.sort(row) if col.sort else row.get(col.key)
        return None

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.columns[section].title
            if role == Qt.TextAlignmentRole:
                return _ALIGN.get(self.columns[section].align, _ALIGN["left"])
        return None

    # ---------------- 排序 ----------------
    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        if not 0 <= column < len(self.columns):
            self._sort_col = -1
            return
        self.layoutAboutToBeChanged.emit()
        self._sort_col = column
        self._sort_order = order
        self._sort_rows(column, order)
        self.layoutChanged.emit()

    def _sort_rows(self, column: int, order) -> None:
        col = self.columns[column]
        key = col.sort or (lambda r, k=col.key: r.get(k))

        def safe(row):
            value = key(row)
            if value is None:
                return (1, 0)
            if isinstance(value, str):
                return (0, value.lower())
            return (0, value)

        self.rows.sort(key=safe, reverse=(order == Qt.DescendingOrder))


def make_table(model: RecordTableModel, multi: bool = False, sortable: bool = True,
               sort_key: str | None = None, descending: bool = True) -> QTableView:
    """sort_key 为默认排序列；为 None 时保持查询返回的顺序。"""
    view = QTableView()
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.setContextMenuPolicy(Qt.CustomContextMenu)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(30)
    view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setStretchLastSection(False)
    share_delegate = None
    for i, col in enumerate(model.columns):
        view.setColumnWidth(i, col.width)
        if col.stretch:
            header.setSectionResizeMode(i, QHeaderView.Stretch)
        else:
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        if col.share is not None:
            share_delegate = share_delegate or ShareBarDelegate(view)
            view.setItemDelegateForColumn(i, share_delegate)
    if sortable:
        keys = [c.key for c in model.columns]
        column = keys.index(sort_key) if sort_key in keys else -1
        header.setSortIndicator(column, Qt.DescendingOrder if descending else Qt.AscendingOrder)
        view.setSortingEnabled(True)
        header.setSortIndicatorShown(column >= 0)
        header.sortIndicatorChanged.connect(lambda *_: header.setSortIndicatorShown(True))
    return view


def selected_rows(view: QTableView, model: RecordTableModel) -> list[dict]:
    rows = sorted({idx.row() for idx in view.selectionModel().selectedRows()})
    return [model.rows[r] for r in rows if 0 <= r < len(model.rows)]


# 常用列格式
def fmt_size(key: str = "size"):
    return lambda r: util.human_size(r.get(key) or 0)


def fmt_time(key: str = "mtime"):
    return lambda r: util.fmt_time(r.get(key))


def flags_text(flags: int) -> str:
    parts = []
    if flags & util.FLAG_SYSTEM:
        parts.append("系统")
    if flags & util.FLAG_PLACEHOLDER:
        parts.append("云端")
    if flags & util.FLAG_HARDLINK:
        parts.append("硬链接")
    if flags & util.FLAG_CACHE:
        parts.append("缓存")
    return "、".join(parts)
