"""对话框：大目录迁移（robocopy + 目录联接）与迁移记录。"""
from __future__ import annotations

import os
import threading
import time
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core import migrate, scanner, util
from . import workers
from .widgets.common import Card, button, confirm, hbox, info, label, set_role, warn


class MigrateDialog(QDialog):
    def __init__(self, win, path: str, size: int = 0):
        super().__init__(win)
        self.win = win
        self.src = os.path.abspath(path).rstrip("\\")
        self.size = int(size or 0)
        self.record: migrate.MigrationRecord | None = None
        self.running = False
        self._cancel = threading.Event()
        self._check_gen = 0
        self.setWindowTitle("迁移到其它盘")
        self.setMinimumWidth(640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(label("把大目录搬到其它盘，原位置留下一个「目录联接」，程序照常按原路径访问。", "muted", wrap=True))

        card = Card()
        card.body.addWidget(label("源目录", "small"))
        self.src_label = label(self.src, None, wrap=True, selectable=True)
        card.body.addWidget(self.src_label)
        self.size_label = label("大小：" + (util.human_size(self.size) if self.size else "计算中…"), "muted")
        card.body.addWidget(self.size_label)
        card.body.addWidget(label("目标目录", "small"))
        root = win.config.app.get("migrate_root") or "D:\\已迁移"
        self.dst_edit = QLineEdit(migrate.default_target(self.src, root))
        self.dst_edit.textChanged.connect(lambda _t: self._recheck())
        self.browse_btn = button("浏览…", None, "folder", on_click=self._browse)
        card.body.addLayout(hbox(self.dst_edit, self.browse_btn))
        lay.addWidget(card)

        self.steps_label = label("", "muted", wrap=True)
        lay.addWidget(self.steps_label)
        self.problem_label = label("", "danger", wrap=True)
        lay.addWidget(self.problem_label)
        self.progress_label = label("", None, wrap=True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        lay.addWidget(self.progress_label)
        lay.addWidget(self.progress)

        self.start_btn = button("开始迁移", "primary", "move", on_click=self._start)
        self.stop_btn = button("停止", None, "stop", on_click=self._stop)
        self.stop_btn.hide()
        self.finalize_btn = button("删除原目录备份", "danger", "delete",
                                   "确认程序工作正常后，把备份移到回收站以腾出空间", self._finalize)
        self.undo_btn = button("撤销迁移", None, "back", on_click=self._undo)
        self.close_btn = button("关闭", on_click=self.close)
        for btn in (self.finalize_btn, self.undo_btn):
            btn.hide()
        lay.addLayout(hbox(None, self.undo_btn, self.finalize_btn, self.stop_btn, self.start_btn, self.close_btn))

        if not self.size:
            workers.run_async(lambda: scanner.measure_dir(self.src), self._on_size)
        self._recheck()

    # ---------------- 校验 ----------------
    def _on_size(self, result) -> None:
        size, files = result
        self.size = size
        self.size_label.setText(f"大小：{util.human_size(size)}（{files:,} 个文件）")
        self._recheck()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择目标位置（会在里面新建同名文件夹）",
                                                  os.path.dirname(self.dst_edit.text()) or "D:\\")
        if folder:
            self.dst_edit.setText(os.path.join(os.path.normpath(folder), os.path.basename(self.src)))

    def _recheck(self) -> None:
        dst = self.dst_edit.text().strip()
        self.steps_label.setText("将执行：\n" + "\n".join(f"{i}. {s}" for i, s in
                                                         enumerate(migrate.plan_steps(self.src, dst or "…"), 1)))
        self._check_gen += 1
        gen = self._check_gen
        src, size, app_dir = self.src, self.size or None, self.win.app_dir

        def done(problems):
            if gen != self._check_gen or self.running or self.record:
                return
            self.problem_label.setText("\n".join("· " + p for p in problems))
            self.start_btn.setEnabled(not problems and bool(dst))

        self.start_btn.setEnabled(False)
        workers.run_async(lambda: migrate.check(src, dst, app_dir, size) if dst else ["请填写目标目录"], done)

    # ---------------- 执行 ----------------
    def _start(self) -> None:
        dst = self.dst_edit.text().strip()
        if not confirm(
            self, "开始迁移",
            f"把「{self.src}」迁移到「{dst}」？",
            "迁移前请关闭正在使用这个目录的程序。\n复制和校验通过后才会替换原目录；任何一步失败都会自动回滚。",
            ok_text="开始迁移",
        ):
            return
        if self.win.is_busy():
            warn(self, "迁移", "有其它任务正在进行，请稍后再试。")
            return
        self.running = True
        self._cancel = threading.Event()
        self.win.set_migrating(True)
        self._set_running_ui(True)
        src, app_dir, log_path, cancel = self.src, self.win.app_dir, self.win.migrations_path, self._cancel

        def report(text):
            workers.call_in_main(self.progress_label.setText, text)

        def work():
            started = time.time()
            try:
                record = migrate.migrate(src, dst, app_dir, report, cancel, log_path=log_path)
                workers.call_in_main(self._on_done, record, time.time() - started)
            except Exception as exc:  # noqa: BLE001
                workers.call_in_main(self._on_failed, exc, traceback.format_exc())

        threading.Thread(target=work, daemon=True, name="migrate").start()

    def _stop(self) -> None:
        self._cancel.set()
        self.progress_label.setText("正在停止并回滚…")

    def _set_running_ui(self, running: bool) -> None:
        self.progress.setVisible(running)
        self.stop_btn.setVisible(running)
        self.start_btn.setVisible(not running and self.record is None)
        self.close_btn.setEnabled(not running)
        self.dst_edit.setEnabled(not running and self.record is None)
        self.browse_btn.setEnabled(not running and self.record is None)
        self.finalize_btn.setVisible(not running and self.record is not None and self.record.state == "done")
        self.undo_btn.setVisible(not running and self.record is not None and self.record.state == "done")

    def _on_done(self, record, elapsed: float) -> None:
        self.running = False
        self.record = record
        self.win.set_migrating(False)
        self.problem_label.clear()
        self.steps_label.clear()
        set_role(self.progress_label, "ok")
        self.progress_label.setText(
            f"迁移完成（{util.fmt_duration(elapsed)}）：{util.human_size(record.size)} 已搬到 {record.dst}。\n"
            f"原目录暂时改名为备份「{os.path.basename(record.backup)}」，C 盘空间还没释放。\n"
            "先打开相关程序确认工作正常，再点「删除原目录备份」；有问题就点「撤销迁移」。"
        )
        self._set_running_ui(False)
        self.win.log("迁移完成", f"{record.src} -> {record.dst}")

    def _on_failed(self, exc, tb: str) -> None:
        self.running = False
        self.win.set_migrating(False)
        self._set_running_ui(False)
        self.win.log("迁移失败", tb)
        cancelled = self._cancel.is_set()
        set_role(self.progress_label, "danger")
        self.progress_label.setText(("已停止，" if cancelled else "迁移失败：" + str(exc) + "\n") + "已回滚，原目录保持不变。")
        self._recheck()

    def _finalize(self) -> None:
        record = self.record
        if record is None or not confirm(
            self, "删除原目录备份", f"把备份「{record.backup}」移到回收站？",
            "之后要撤销迁移会从目标盘把数据搬回来（较慢）。清空回收站后才真正释放空间。",
            danger=True, ok_text="移到回收站",
        ):
            return
        self._run_post(lambda: migrate.finalize(record, self.win.app_dir, self.win.migrations_path), "正在删除备份…",
                       "备份已移到回收站。清空回收站后即可释放空间。")

    def _undo(self) -> None:
        record = self.record
        if record is None or not confirm(self, "撤销迁移", "移除目录联接并把原目录恢复到原位置？", ok_text="撤销"):
            return
        self._run_post(lambda: migrate.undo(record, None, self.win.migrations_path), "正在撤销…", "已撤销迁移，原目录已恢复。")

    def _run_post(self, fn, busy_text: str, done_text: str) -> None:
        self.running = True
        self.win.set_migrating(True)
        self.progress_label.setText(busy_text)
        self.finalize_btn.hide()
        self.undo_btn.hide()
        self.progress.show()
        self.close_btn.setEnabled(False)

        def done(_result):
            self.running = False
            self.win.set_migrating(False)
            self.progress.hide()
            self.close_btn.setEnabled(True)
            self.progress_label.setText(done_text)

        def failed(exc, tb):
            self.running = False
            self.win.set_migrating(False)
            self.win.log("迁移后续操作失败", tb)
            self._set_running_ui(False)
            set_role(self.progress_label, "danger")
            self.progress_label.setText(f"操作失败：{exc}")

        workers.run_async(fn, done, failed)

    def closeEvent(self, event) -> None:
        if self.running:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if not self.running:
            super().reject()


class MigrationsDialog(QDialog):
    """迁移记录：查看历史迁移，删除备份或撤销。"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("迁移记录")
        self.resize(820, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["原位置", "迁移到", "大小", "时间", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 1)
        self.finalize_btn = button("删除备份", "danger", "delete", on_click=self._finalize)
        self.undo_btn = button("撤销迁移", None, "back", on_click=self._undo)
        lay.addLayout(hbox(label("撤销会移除联接并恢复原目录；删除备份会把原目录的备份移到回收站。", "muted"), None,
                           self.undo_btn, self.finalize_btn, button("关闭", on_click=self.close)))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._load()

    def _load(self) -> None:
        self.records = sorted(migrate.load_records(self.win.migrations_path), key=lambda r: -r.created)
        states = {"done": "已迁移（保留备份）", "finalized": "已完成", "undone": "已撤销"}
        self.table.setRowCount(len(self.records))
        for row, rec in enumerate(self.records):
            values = [rec.src, rec.dst, util.human_size(rec.size), util.fmt_time(rec.created), states.get(rec.state, rec.state)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        self._update_buttons()

    def _selected(self) -> migrate.MigrationRecord | None:
        rows = self.table.selectionModel().selectedRows()
        return self.records[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        rec = self._selected()
        busy = self.win.is_busy()
        self.finalize_btn.setEnabled(bool(rec and rec.state == "done") and not busy)
        self.undo_btn.setEnabled(bool(rec and rec.state in ("done", "finalized")) and not busy)

    def _finalize(self) -> None:
        rec = self._selected()
        if rec and confirm(self, "删除备份", f"把「{rec.backup}」移到回收站？", danger=True, ok_text="移到回收站"):
            self._run(lambda: migrate.finalize(rec, self.win.app_dir, self.win.migrations_path))

    def _undo(self) -> None:
        rec = self._selected()
        if rec and confirm(self, "撤销迁移", f"把「{rec.src}」恢复为普通目录？",
                           "如果备份已删除，会用 robocopy 把数据从目标盘搬回来，可能需要较长时间。", ok_text="撤销"):
            self._run(lambda: migrate.undo(rec, None, self.win.migrations_path))

    def _run(self, fn) -> None:
        self.win.set_migrating(True)
        self.setEnabled(False)

        def done(_r):
            self.win.set_migrating(False)
            self.setEnabled(True)
            self._load()
            info(self, "迁移记录", "操作完成。")

        def failed(exc, tb):
            self.win.set_migrating(False)
            self.setEnabled(True)
            self.win.log("迁移记录操作失败", tb)
            warn(self, "迁移记录", f"操作失败：{exc}")
            self._load()

        workers.run_async(fn, done, failed)
