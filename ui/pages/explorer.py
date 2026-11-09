"""空间分布：左侧懒加载目录树，右侧 Treemap + 当前目录内容（子目录来自索引，文件实时读取）。"""
from __future__ import annotations

import os

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QSplitter, QTreeView

from core import cleanup, scanner, util
from .. import workers
from ..actions import show_record_menu
from ..models import Column, RecordTableModel, make_table, selected_rows
from ..treedata import build_tree
from ..widgets.common import Breadcrumb, Card, button, glyph_icon, hbox, label, short_name, tool_button
from ..widgets.delegates import SHARE_ROLE, ShareBarDelegate
from ..widgets.treemap import TreemapWidget
from .base import Page

PATH_ROLE = Qt.UserRole + 2
PLACEHOLDER = "\0placeholder"
CHILD_LIMIT = 1000


def norm_dir(path: str) -> str:
    return scanner.dir_key(os.path.abspath(path)) if path else ""


def parent_dir(path: str) -> str:
    if not path or len(path.rstrip("\\")) <= 2:
        return ""
    return norm_dir(os.path.dirname(path.rstrip("\\")))


def list_dir(index, path: str) -> dict:
    """当前目录内容：子目录（索引里的大小）+ 文件（实时 scandir）。"""
    info = index.dir_info(path)
    kids = {k["path"].lower(): k for k in index.folder_children(path, 5000)}
    rows: list[dict] = []
    error = ""
    try:
        with os.scandir(util.long_path(path)) as it:
            for entry in it:
                full = os.path.join(path, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        rec = kids.pop(full.lower(), None)
                        st = entry.stat(follow_symlinks=False)
                        link = bool(getattr(st, "st_file_attributes", 0) & util.FILE_ATTRIBUTE_REPARSE_POINT)
                        rows.append({
                            "path": full, "name": entry.name, "is_dir": True, "ext": "",
                            "size": int(rec["size"]) if rec else 0,
                            "files": int(rec["files"] or 0) if rec else 0,
                            "mtime": rec["mtime"] if rec else st.st_mtime,
                            "type": "目录联接" if link else "文件夹",
                            "indexed": rec is not None,
                        })
                    else:
                        st = entry.stat(follow_symlinks=False)
                        ext = os.path.splitext(entry.name)[1].lower()
                        rows.append({
                            "path": full, "name": entry.name, "is_dir": False, "ext": ext,
                            "size": int(st.st_size), "files": 0, "mtime": st.st_mtime,
                            "type": (ext[1:].upper() + " 文件") if ext else "文件", "indexed": True,
                        })
                except OSError:
                    continue
    except OSError as exc:
        error = f"无法读取目录：{exc.strerror or exc}"
        for rec in kids.values():
            rows.append({"path": rec["path"], "name": short_name(rec["path"]), "is_dir": True, "ext": "",
                         "size": int(rec["size"]), "files": int(rec["files"] or 0), "mtime": rec["mtime"],
                         "type": "文件夹", "indexed": True})
    total = int(info["size"]) if info else sum(r["size"] for r in rows)
    rows.sort(key=lambda r: -r["size"])
    return {"info": info, "rows": rows, "total": total, "error": error}


class ExplorerPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.path = ""
        self.history: list[str] = []
        self._tree_latest = workers.Latest()
        self._list_latest = workers.Latest()
        self._map_latest = workers.Latest()
        self._syncing = False
        self._loaded = False

        self.back_btn = tool_button("back", "后退（Backspace）", self.go_back)
        self.up_btn = tool_button("up", "上一级（Alt+↑）", self.go_up)
        self.crumb = Breadcrumb()
        self.crumb.navigate.connect(self.navigate)
        self.open_btn = tool_button("folder", "在资源管理器中打开", lambda: self.path and cleanup.open_in_explorer(self.path))
        self.refresh_btn = tool_button("refresh", "刷新显示（F5，读取索引，不重新扫描）", self.refresh)
        self.rescan_btn = button("重新扫描此文件夹", None, "scan", "只扫描当前文件夹，几秒内更新它的大小和文件列表",
                                 lambda: self.path and self.win.rescan_folder(self.path))
        self.body.addLayout(hbox(self.back_btn, self.up_btn, self.crumb, self.open_btn, self.refresh_btn,
                                 self.rescan_btn, spacing=4))

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.body.addWidget(split, 1)

        # ---- 左：目录树 ----
        self.tree_model = QStandardItemModel(0, 4, self)
        self.tree_model.setHorizontalHeaderLabels(["名称", "大小", "占比", "文件数"])
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.setItemDelegateForColumn(2, ShareBarDelegate(self.tree))
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 84), (2, 120), (3, 70)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.tree.setColumnWidth(col, width)
        self.tree.expanded.connect(self._on_expanded)
        self.tree.selectionModel().currentRowChanged.connect(self._on_tree_current)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        split.addWidget(self.tree)

        # ---- 右：Treemap + 目录内容 ----
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.map_card = Card("")
        self.map_info = label("", "small")
        self.map_card.header.addWidget(self.map_info)
        self.treemap = TreemapWidget(depth=2)
        self.treemap.activated.connect(self.navigate)
        self.treemap.contextRequested.connect(self._map_menu)
        self.map_card.body.addWidget(self.treemap, 1)
        right.addWidget(self.map_card)

        list_card = Card("目录内容")
        self.list_info = label("", "small")
        list_card.header.addWidget(self.list_info)
        self.list_model = RecordTableModel([
            Column("name", "名称", 300, stretch=True),
            Column("type", "类型", 96),
            Column("size", "大小", 96, "right", fmt=lambda r: util.human_size(r["size"]) if r["indexed"] else "未索引"),
            Column("share", "占比", 130, share=lambda r: (r["size"] / self._total) if self._total else 0.0,
                   sort=lambda r: r["size"]),
            Column("mtime", "修改时间", 140, fmt=lambda r: util.fmt_time(r["mtime"])),
        ])
        self._total = 0
        self.table = make_table(self.list_model, multi=True, sort_key="size")
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.customContextMenuRequested.connect(self._table_menu)
        list_card.body.addWidget(self.table, 1)
        right.addWidget(list_card)
        right.setSizes([440, 360])
        split.addWidget(right)
        split.setSizes([400, 900])
        split.setStretchFactor(1, 1)

        QShortcut(QKeySequence("Backspace"), self, activated=self.go_back, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Alt+Up"), self, activated=self.go_up, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Delete"), self.table, activated=self._delete_selected, context=Qt.WidgetShortcut)
        self._update_nav()

    # ---------------- 刷新 ----------------
    def refresh(self) -> None:
        self._load_roots()
        if self.path:
            self._load_current()

    def soft_refresh(self) -> None:
        """只重新读取当前文件夹（列表和方块图），不重建左侧目录树，展开状态保持不变。"""
        if self.path and self._loaded:
            self._load_current()
        else:
            self.refresh()

    def on_show(self) -> None:
        if not self._loaded:
            self.refresh()

    def on_busy(self, busy: bool) -> None:
        self.rescan_btn.setEnabled(not busy and bool(self.path))

    def _load_roots(self) -> None:
        index = self.win.index

        def load():
            roots = index.root_dirs()
            return roots, index.parents_with_children([r["path"] for r in roots])

        self._tree_latest.run(load, self._on_roots)

    def _on_roots(self, result) -> None:
        roots, has_kids = result
        self._loaded = True
        self.tree_model.removeRows(0, self.tree_model.rowCount())
        total = sum(int(r["size"]) for r in roots)
        for rec in roots:
            self.tree_model.appendRow(self._make_row(rec, total, rec["path"] in has_kids, root=True))
        if not roots:
            self.treemap.set_root(None, "暂无数据，先扫描一次")
            return
        if not self.path:
            self.navigate(roots[0]["path"])
        else:
            self._sync_tree(self.path)

    def _make_row(self, rec: dict, parent_size: int, has_children: bool, root: bool = False) -> list[QStandardItem]:
        path = rec["path"]
        name = QStandardItem(glyph_icon("drive" if root else "folder", None, 16), short_name(path) if not root else path)
        name.setData(path, PATH_ROLE)
        name.setToolTip(path)
        size = QStandardItem(util.human_size(rec["size"]))
        size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        share = QStandardItem()
        share.setData((int(rec["size"]) / parent_size) if parent_size else 0.0, SHARE_ROLE)
        files = QStandardItem(util.human_count(rec.get("files") or 0))
        files.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if has_children:
            placeholder = QStandardItem("加载中…")
            placeholder.setData(PLACEHOLDER, PATH_ROLE)
            name.appendRow([placeholder])
        return [name, size, share, files]

    def _fill_children(self, item: QStandardItem, rows: list[dict], has_kids: set[str]) -> None:
        if item.rowCount() and item.child(0).data(PATH_ROLE) == PLACEHOLDER:
            item.removeRow(0)
        if item.rowCount():
            return
        parent_size = 0
        if rows:
            info = self.win.index.dir_info(item.data(PATH_ROLE))
            parent_size = (int(info["size"]) if info else 0) or max(sum(int(r["size"]) for r in rows), 1)
        for rec in rows:
            item.appendRow(self._make_row(rec, parent_size, rec["path"] in has_kids))

    def _load_children_sync(self, item: QStandardItem) -> None:
        path = item.data(PATH_ROLE)
        rows = self.win.index.folder_children(path, CHILD_LIMIT)
        self._fill_children(item, rows, self.win.index.parents_with_children([r["path"] for r in rows]))

    def _on_expanded(self, index: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(index.siblingAtColumn(0))
        if item is None or not item.rowCount() or item.child(0).data(PATH_ROLE) != PLACEHOLDER:
            return
        path = item.data(PATH_ROLE)
        db = self.win.index

        def load():
            rows = db.folder_children(path, CHILD_LIMIT)
            return rows, db.parents_with_children([r["path"] for r in rows])

        def done(result):
            if item.model() is self.tree_model:
                self._fill_children(item, *result)

        workers.run_async(load, done)

    def _find_child(self, parent: QStandardItem | None, path: str) -> QStandardItem | None:
        key = path.lower()
        if parent is None:
            for row in range(self.tree_model.rowCount()):
                item = self.tree_model.item(row, 0)
                if str(item.data(PATH_ROLE)).lower() == key:
                    return item
            return None
        if parent.rowCount() and parent.child(0).data(PATH_ROLE) == PLACEHOLDER:
            self._load_children_sync(parent)
        for row in range(parent.rowCount()):
            item = parent.child(row, 0)
            if str(item.data(PATH_ROLE)).lower() == key:
                return item
        return None

    def _sync_tree(self, path: str) -> None:
        """在目录树里展开并选中 path（逐级同步加载祖先节点）。"""
        chain = []
        cur = path
        while cur:
            chain.append(cur)
            cur = parent_dir(cur)
        chain.reverse()
        item = None
        found = None
        for step in chain:
            found = self._find_child(item, step)
            if found is None:
                break
            item = found
            if step != path:
                self._syncing = True
                self.tree.expand(item.index())
                self._syncing = False
        if item is not None:
            self._syncing = True
            self.tree.setCurrentIndex(item.index())
            self.tree.scrollTo(item.index())
            self._syncing = False

    def _on_tree_current(self, current: QModelIndex, _prev) -> None:
        if self._syncing or not current.isValid():
            return
        path = current.siblingAtColumn(0).data(PATH_ROLE)
        if path and path != PLACEHOLDER and path != self.path:
            self.navigate(path, sync_tree=False)

    # ---------------- 导航 ----------------
    def navigate(self, path: str, sync_tree: bool = True, push: bool = True) -> None:
        path = norm_dir(path)
        if not path:
            return
        if push and self.path and self.path != path:
            self.history.append(self.path)
            del self.history[:-100]
        self.path = path
        self.crumb.set_path(path)
        self._update_nav()
        if sync_tree and self._loaded:
            self._sync_tree(path)
        self._load_current()

    def go_back(self) -> None:
        if self.history:
            self.navigate(self.history.pop(), push=False)

    def go_up(self) -> None:
        parent = parent_dir(self.path)
        if parent:
            self.navigate(parent)

    def _update_nav(self) -> None:
        self.back_btn.setEnabled(bool(self.history))
        self.up_btn.setEnabled(bool(parent_dir(self.path)))
        self.open_btn.setEnabled(bool(self.path))
        self.rescan_btn.setEnabled(bool(self.path) and not self.win.is_busy())
        self.rescan_btn.setText("重新扫描此磁盘" if 0 < len(self.path) <= 3 else "重新扫描此文件夹")

    def _load_current(self) -> None:
        path, index = self.path, self.win.index
        self.map_card.title_label.setText(path if len(path) <= 3 else short_name(path))
        self.map_info.setText("")
        self.list_info.setText("正在读取…")
        self._map_latest.run(lambda: build_tree(index, path), self._on_tree)
        self._list_latest.run(lambda: list_dir(index, path), self._on_list,
                              lambda _e, tb: self.list_info.setText("读取失败"))

    def _on_tree(self, node) -> None:
        if node is None:
            self.treemap.set_root(None, "这个目录不在索引里（可能是新建的，或扫描时被跳过）。点上方「重新扫描此文件夹」即可。")
            self.map_info.setText("")
            return
        self.treemap.set_root(node, "这个目录下没有子目录")
        self.map_info.setText(f"{util.human_size(node.size)} · {util.human_count(node.files)} 个文件")

    def _on_list(self, result) -> None:
        self._total = max(1, result["total"])
        self.list_model.set_rows(result["rows"])
        dirs = sum(1 for r in result["rows"] if r["is_dir"])
        files = len(result["rows"]) - dirs
        text = f"{dirs} 个文件夹 · {files} 个文件"
        missing = sum(1 for r in result["rows"] if not r["indexed"])
        if missing:
            text += f" · {missing} 个文件夹是扫描后新建的，可重新扫描此文件夹"
        if result["error"]:
            text += " · " + result["error"]
        self.list_info.setText(text)

    # ---------------- 交互 ----------------
    def _on_double_click(self, index: QModelIndex) -> None:
        row = self.list_model.row(index.row())
        if not row:
            return
        if row["is_dir"]:
            self.navigate(row["path"])
        else:
            cleanup.open_file(row["path"])

    def selected_records(self) -> list[dict]:
        rows = selected_rows(self.table, self.list_model)
        if rows:
            return rows
        info = self.win.index.dir_info(self.path) if self.path else None
        if info:
            return [{"path": self.path, "size": info["size"], "mtime": info["mtime"], "is_dir": True,
                     "name": short_name(self.path)}]
        return []

    def _dir_record(self, path: str) -> dict:
        info = self.win.index.dir_info(path) or {}
        return {"path": path, "size": int(info.get("size") or 0), "mtime": info.get("mtime") or 0,
                "is_dir": True, "name": short_name(path), "ext": ""}

    def _table_menu(self, pos) -> None:
        rows = selected_rows(self.table, self.list_model)
        if rows:
            show_record_menu(self.win, self, rows, self.table.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos) -> None:
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        path = index.siblingAtColumn(0).data(PATH_ROLE)
        if path and path != PLACEHOLDER:
            show_record_menu(self.win, self, [self._dir_record(path)], self.tree.viewport().mapToGlobal(pos),
                             allow_migrate=len(path) > 3)

    def _map_menu(self, path: str, pos) -> None:
        show_record_menu(self.win, self, [self._dir_record(path)], pos)

    def _delete_selected(self) -> None:
        rows = selected_rows(self.table, self.list_model)
        if rows:
            self.win.delete_records(rows)
