"""概览：磁盘卡片、可回收空间、最近膨胀的目录、整盘 Treemap。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from core import monitor, recommend, util, volumes
from .. import workers
from ..actions import show_record_menu
from ..treedata import build_tree
from ..widgets.common import Card, Segmented, button, hbox, label, set_role, short_name
from ..widgets.ring import DriveRing
from ..widgets.treemap import TreemapWidget
from .base import Page


class DashboardPage(Page):
    def __init__(self, win):
        super().__init__(win, scroll=True)
        self._latest = workers.Latest()
        self._tree_latest = workers.Latest()
        self._roots: list[str] = []
        self._drive = ""

        self.banner = label("还没有扫描数据。点右上角「完整扫描」，扫描在后台进程里进行，界面不会卡。", "banner", wrap=True)
        self.banner.hide()
        self.body.addWidget(self.banner)

        self.drive_row = QGridLayout()
        self.drive_row.setSpacing(12)
        self.body.addLayout(self.drive_row)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        self.reclaim_card = Card("可回收空间")
        self.reclaim_value = label("—", "big")
        self.reclaim_detail = label("", "muted", wrap=True)
        self.migrate_detail = label("", "muted", wrap=True)
        self.quick_btn = button("一键安全清理", "primary", "clean", on_click=win.quick_clean)
        self.view_btn = button("查看清理推荐", None, None, on_click=lambda: win.goto("cleanup"))
        self.reclaim_card.body.addWidget(self.reclaim_value)
        self.reclaim_card.body.addWidget(self.reclaim_detail)
        self.reclaim_card.body.addWidget(self.migrate_detail)
        self.reclaim_card.body.addStretch(1)
        self.reclaim_card.body.addLayout(hbox(self.quick_btn, self.view_btn, None))
        mid.addWidget(self.reclaim_card, 2)

        self.growth_card = Card("最近膨胀的目录")
        self.growth_sub = label("", "small")
        self.growth_card.header.addWidget(self.growth_sub)
        self.growth_list = QVBoxLayout()
        self.growth_list.setSpacing(2)
        self.growth_card.body.addLayout(self.growth_list)
        self.growth_card.body.addStretch(1)
        more = button("查看监视", "ghost", on_click=lambda: win.goto("monitor"))
        self.growth_card.header.addWidget(more)
        mid.addWidget(self.growth_card, 3)
        self.body.addLayout(mid)

        self.map_card = Card("空间分布")
        self.map_seg_holder = QHBoxLayout()
        self.map_card.header.addLayout(self.map_seg_holder)
        self.map_card.header.addWidget(button("打开「空间分布」", "ghost", "treemap",
                                              on_click=lambda: win.goto_explorer(self._drive)))
        self.map_hint = label("单击方块进入该目录，右键查看更多操作。", "small")
        self.map_card.body.addWidget(self.map_hint)
        self.treemap = TreemapWidget(depth=2)
        self.treemap.setMinimumHeight(380)
        self.treemap.activated.connect(win.goto_explorer)
        self.treemap.contextRequested.connect(self._tree_menu)
        self.map_card.body.addWidget(self.treemap)
        self.body.addWidget(self.map_card, 1)
        self.segmented: Segmented | None = None

        win.volumesChanged.connect(self._load_volumes)

    # ---------------- 数据 ----------------
    def refresh(self) -> None:
        self._load_volumes()
        self._update_reclaim()
        index = self.win.index

        def load():
            return index.has_index(), [r["path"] for r in index.root_dirs()], monitor.growth(index, 6)

        self._latest.run(load, self._on_loaded, lambda _e, tb: self.win.log("概览加载失败", tb))

    def on_show(self) -> None:
        self._update_reclaim()

    def on_busy(self, busy: bool) -> None:
        self.quick_btn.setEnabled(not busy)

    def _load_volumes(self) -> None:
        index = self.win.index

        def load():
            vols = volumes.list_volumes()
            return vols, {v.letter: monitor.change_since(index, v.letter, 86400) for v in vols}

        workers.run_async(load, self._on_volumes)

    def _on_loaded(self, result) -> None:
        has_index, roots, growth = result
        self.banner.setVisible(not has_index)
        self._set_roots(roots)
        self._fill_growth(growth)
        self._load_tree()

    # ---------------- 磁盘卡片 ----------------
    def _on_volumes(self, result) -> None:
        vols, changes = result
        while self.drive_row.count():
            item = self.drive_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cols = 4 if len(vols) >= 4 else max(1, len(vols))
        for i, vol in enumerate(vols):
            self.drive_row.addWidget(self._drive_card(vol, changes.get(vol.letter)), i // cols, i % cols)

    def _drive_card(self, vol, change) -> QWidget:
        card = Card(margins=14)
        row = QHBoxLayout()
        row.setSpacing(14)
        ring = DriveRing(96)
        ring.set_value(vol.used_pct, "已用")
        row.addWidget(ring)
        col = QVBoxLayout()
        col.setSpacing(3)
        col.addWidget(label(f"{vol.label or '本地磁盘'} ({vol.letter})", "h2"))
        col.addWidget(label(f"已用 {util.human_size(vol.used)} / 共 {util.human_size(vol.total)}", "muted"))
        free = label(f"剩余 {util.human_size(vol.free)}", None)
        if vol.used_pct >= 92:
            set_role(free, "danger")
        elif vol.used_pct >= 85:
            set_role(free, "warn")
        col.addWidget(free)
        if change is None:
            text, role = "24 小时变化：数据积累中", "small"
        elif abs(change) < 50 * util.MB:
            text, role = "24 小时内基本无变化", "small"
        elif change < 0:
            text, role = f"24 小时内减少 {util.human_size(-change)}", "warn"
        else:
            text, role = f"24 小时内多出 {util.human_size(change)}", "ok"
        col.addWidget(label(text, role))
        col.addStretch(1)
        root = vol.letter + "\\"
        col.addLayout(hbox(button("查看分布", "ghost", "treemap", on_click=lambda r=root: self.win.goto_explorer(r)), None))
        row.addLayout(col, 1)
        card.body.addLayout(row)
        return card

    # ---------------- 可回收 ----------------
    def _update_reclaim(self) -> None:
        items = self.win.cleanup_items
        if not items:
            self.reclaim_value.setText("—")
            self.reclaim_detail.setText("还没有分析可清理项。扫描完成后会自动分析，也可以点右上角「分析可清理项」。")
            self.migrate_detail.clear()
            return
        summary = recommend.summarize(items)
        safe = summary["totals"].get("safe", 0)
        caution = summary["totals"].get("caution", 0)
        self.reclaim_value.setText(util.human_size(summary["reclaimable"]))
        self.reclaim_detail.setText(
            f"安全 {util.human_size(safe)}（{summary['counts'].get('safe', 0)} 项，可一键清理）　"
            f"谨慎 {util.human_size(caution)}（{summary['counts'].get('caution', 0)} 项，需逐项确认）"
        )
        migrate = [i for i in items if i.kind == "migrate"]
        if migrate:
            total = sum(i.size for i in migrate)
            self.migrate_detail.setText(f"另有 {len(migrate)} 个大目录（{util.human_size(total)}）可以迁移到其它盘，"
                                        "不删除数据也能腾出 C 盘。")
        else:
            self.migrate_detail.clear()

    # ---------------- 膨胀目录 ----------------
    def _fill_growth(self, growth: dict) -> None:
        while self.growth_list.count():
            item = self.growth_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear_layout(item.layout())
        rows = growth.get("rows") or []
        if growth.get("before") and growth.get("after"):
            self.growth_sub.setText(f"{util.fmt_time(growth['before'])} → {util.fmt_time(growth['after'])}")
        else:
            self.growth_sub.setText("")
        if not rows:
            text = ("两次扫描之间没有明显增长的目录。" if growth.get("after")
                    else "需要至少两次完整扫描才能对比。开启「设置 → 自动扫描」后会每天在空闲时更新。")
            self.growth_list.addWidget(label(text, "muted", wrap=True))
            return
        for row in rows:
            name = button(short_name(row["path"]), "ghost", on_click=lambda p=row["path"]: self.win.goto_explorer(p))
            name.setToolTip(row["path"])
            path = label(util.short_path(row["path"], 60), "small")
            delta = label(("新增 " if row["new"] else "+") + util.human_size(row["delta"]), "warn")
            delta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.growth_list.addLayout(hbox(name, path, None, delta))

    # ---------------- Treemap ----------------
    def _set_roots(self, roots: list[str]) -> None:
        if roots == self._roots and self.segmented is not None:
            return
        self._roots = roots
        if self.segmented is not None:
            self.segmented.deleteLater()
            self.segmented = None
        if not roots:
            self._drive = ""
            return
        if self._drive not in roots:
            self._drive = roots[0]
        self.segmented = Segmented([(r, r.rstrip("\\")) for r in roots], self._drive)
        self.segmented.changed.connect(self._on_drive)
        self.map_seg_holder.addWidget(self.segmented)

    def _on_drive(self, drive: str) -> None:
        self._drive = drive
        self._load_tree()

    def _load_tree(self) -> None:
        if not self._drive:
            self.treemap.set_root(None, "暂无数据，先扫描一次")
            return
        index, drive = self.win.index, self._drive
        self.treemap.set_root(None, "正在加载…")
        self._tree_latest.run(lambda: build_tree(index, drive, child_limit=40, grand_parents=24, grand_limit=24),
                              lambda node: self.treemap.set_root(node, "这个磁盘没有索引数据"))

    def _tree_menu(self, path: str, pos) -> None:
        info = self.win.index.dir_info(path) or {}
        show_record_menu(self.win, self, [{"path": path, "size": info.get("size", 0), "mtime": info.get("mtime", 0),
                                           "is_dir": True, "name": short_name(path)}], pos)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())
