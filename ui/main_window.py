"""主窗口：左侧导航 + 页面栈 + 命令栏 + 状态栏，以及扫描 / 分析 / 删除 / 迁移 / 监视的调度。"""
from __future__ import annotations

import os
import time
import traceback

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import __version__, cleanup, jobs, monitor, recommend, scanner, sync, util, volumes, watch
from core.config import Config
from core.index import Index
from . import theme, workers
from .ai_panel import AiPanel
from .jobs import ProcessJob
from .pages.big_files import BigFilesPage
from .pages.cleanup import CleanupPage
from .pages.dashboard import DashboardPage
from .pages.explorer import ExplorerPage
from .pages.monitor import MonitorPage
from .pages.recent import RecentPage
from .pages.settings import SettingsPage
from .tray import Tray
from .widgets.common import app_icon, button, confirm, error, glyph_icon, info, label, tool_button, warn

PAGES = [
    ("dashboard", "概览", "home", DashboardPage),
    ("explorer", "空间分布", "treemap", ExplorerPage),
    ("big", "大文件", "file", BigFilesPage),
    ("recent", "最近文件", "recent", RecentPage),
    ("cleanup", "清理推荐", "clean", CleanupPage),
    ("monitor", "监视", "monitor", MonitorPage),
    ("settings", "设置", "settings", SettingsPage),
]


class MainWindow(QMainWindow):
    indexChanged = Signal()
    cleanupChanged = Signal()
    busyChanged = Signal()
    volumesChanged = Signal()

    def __init__(self, index: Index, config: Config, app_dir: str, data_dir: str):
        super().__init__()
        self.index = index
        self.config = config
        self.app_dir = app_dir
        self.data_dir = data_dir
        self.log_path = os.path.join(data_dir, "app.log")
        self.migrations_path = os.path.join(data_dir, "migrations.json")
        self.cleanup_items: list[recommend.CleanItem] = []
        self.scan_job: ProcessJob | None = None
        self.analysis_job: ProcessJob | None = None
        self.deleting = False
        self.migrating = False
        self._quitting = False
        self._auto_scan = False
        self._scan_folder = ""
        self._analysis_quiet = False
        self._dirty: set[str] = set()
        self.sampler = monitor.Sampler(index, config)
        self.watchers = watch.WatchSet()
        self.syncing = False
        self._refresh_on_activate = False

        self.setWindowTitle("硬盘监视器")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1120, 720)
        self._restore_geometry()

        self._build_ui()
        self.tray = Tray(self)
        self._build_timers()
        self._build_shortcuts()
        self.busyChanged.connect(self._update_busy)
        self.indexChanged.connect(self._on_index_changed)
        self.cleanupChanged.connect(lambda: self._mark_dirty({"dashboard", "cleanup"}))
        QTimer.singleShot(0, self._startup)

    # ================= 界面骨架 =================
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Central")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("NavBar")
        nav.setFixedWidth(184)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(12, 16, 12, 12)
        nav_lay.setSpacing(4)
        title = QLabel("  硬盘监视器")
        title.setObjectName("AppTitle")
        nav_lay.addWidget(title)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons: dict[str, QPushButton] = {}
        self.pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget()
        for key, text, icon, cls in PAGES:
            btn = QPushButton("  " + text)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setIcon(glyph_icon(icon, theme.COLORS["muted"], 18))
            btn.setIconSize(QSize(18, 18))
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self.goto(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            nav_lay.addWidget(btn)
            page = cls(self)
            self.pages[key] = page
            self.stack.addWidget(page)
        nav_lay.addStretch(1)
        self.nav_footer = QLabel(f"v{__version__}")
        self.nav_footer.setObjectName("NavFooter")
        self.nav_footer.setWordWrap(True)
        nav_lay.addWidget(self.nav_footer)
        root.addWidget(nav)

        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(24, 16, 24, 12)
        main_lay.setSpacing(12)
        top = QFrame()
        top.setObjectName("TopBar")
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        self.page_title = QLabel("概览")
        self.page_title.setObjectName("PageTitle")
        top_lay.addWidget(self.page_title)
        top_lay.addStretch(1)
        self.refresh_btn = button("刷新", None, "refresh", "同步刚才删除 / 新增的文件（包括在资源管理器里操作的），并刷新当前页面（F5）", self.refresh_view)
        self.scan_btn = button("完整扫描", "primary", "scan", "重新扫描所选磁盘的全部文件（Ctrl+R）", self.start_scan)
        self.stop_btn = button("停止", None, "stop", "停止扫描 / 分析", self.stop_jobs)
        self.analyze_btn = button("分析可清理项", None, "sparkle", "测量缓存、查找旧安装包和重复文件",
                                  lambda: self.start_analysis())
        self.quick_btn = button("一键安全清理", None, "clean", "把「安全」等级的清理项移到回收站", self.quick_clean)
        self.ai_btn = button("问 AI", None, "ai", "打开 / 收起 AI 助手（Ctrl+J）", self.toggle_ai)
        self.ai_btn.setCheckable(True)
        for btn in (self.refresh_btn, self.scan_btn, self.stop_btn, self.analyze_btn, self.quick_btn, self.ai_btn):
            top_lay.addWidget(btn)
        main_lay.addWidget(top)
        main_lay.addWidget(self.stack, 1)
        root.addWidget(main, 1)
        self.setCentralWidget(central)

        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_label = QLabel("就绪")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.last_scan_label = QLabel("")
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress)
        status.addPermanentWidget(self.last_scan_label)

        self.ai_panel = AiPanel(self)
        self.ai_dock = QDockWidget("AI 助手", self)
        self.ai_dock.setObjectName("AiDock")
        self.ai_dock.setWidget(self.ai_panel)
        self.ai_dock.setTitleBarWidget(self._ai_title_bar())
        # 只能停在右侧：可拖动时一不小心就被拖成别的布局，用户找不回来
        self.ai_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.ai_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.ai_dock.setMinimumWidth(360)
        self.addDockWidget(Qt.RightDockWidgetArea, self.ai_dock)
        self.ai_dock.hide()
        self.ai_dock.visibilityChanged.connect(self.ai_btn.setChecked)
        self._ai_sized = False

        self.nav_buttons["dashboard"].setChecked(True)
        self._update_busy()

    def _ai_title_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("AiTitle")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 8, 8)
        title = label("AI 助手", "h2")
        lay.addWidget(title)
        lay.addStretch(1)
        close = tool_button("close", "收起（Ctrl+J）", lambda: self.ai_dock.hide())
        lay.addWidget(close)
        return bar

    def _build_timers(self) -> None:
        self.sample_timer = QTimer(self)
        self.sample_timer.timeout.connect(self.sample_now)
        self.apply_monitor_settings()
        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(5 * 60 * 1000)
        self.auto_timer.timeout.connect(self._maybe_auto_scan)
        self.auto_timer.start()
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(3000)
        self.sync_timer.timeout.connect(self._poll_changes)
        self.sync_timer.start()

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_view)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.start_scan)
        QShortcut(QKeySequence("Ctrl+F5"), self, activated=self.start_scan)
        QShortcut(QKeySequence("Ctrl+J"), self, activated=self.toggle_ai)
        for i, (key, *_rest) in enumerate(PAGES):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda k=key: self.goto(k))

    def apply_monitor_settings(self) -> None:
        minutes = max(1, int(self.config.monitor.get("sample_minutes") or 10))
        self.sample_timer.start(minutes * 60 * 1000)

    def _restore_geometry(self) -> None:
        geo = self.config.app.get("window_geometry") or ""
        width, height = 1360, 860
        try:
            spec = geo.split("+")[0]
            width, height = (int(part) for part in spec.split("x")[:2])
        except (ValueError, TypeError):
            pass
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen:
            width = min(max(1120, width), screen.width() - 20)
            height = min(max(720, height), screen.height() - 40)
            self.resize(width, height)
            self.move(screen.x() + (screen.width() - width) // 2, screen.y() + (screen.height() - height) // 2)
        else:
            self.resize(width, height)

    # ================= 导航 =================
    def goto(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.nav_buttons[key].setChecked(True)
        self.stack.setCurrentWidget(page)
        self.page_title.setText(next(text for k, text, *_ in PAGES if k == key))
        if key in self._dirty:
            self._dirty.discard(key)
            self._refresh_page(key)
        elif hasattr(page, "on_show"):
            page.on_show()

    def current_key(self) -> str:
        current = self.stack.currentWidget()
        return next((k for k, p in self.pages.items() if p is current), "dashboard")

    def goto_explorer(self, path: str) -> None:
        self.goto("explorer")
        self.pages["explorer"].navigate(path)

    def _refresh_page(self, key: str, soft: bool = False) -> None:
        page = self.pages[key]
        try:
            if soft and hasattr(page, "soft_refresh"):
                page.soft_refresh()
            else:
                page.refresh()
        except Exception:  # noqa: BLE001
            self.log("页面刷新失败：" + key, traceback.format_exc())

    def _mark_dirty(self, keys: set[str]) -> None:
        current = self.current_key()
        for key in keys:
            if key == current:
                self._refresh_page(key)
            else:
                self._dirty.add(key)

    def _on_index_changed(self) -> None:
        self._update_last_scan()
        self._mark_dirty({"dashboard", "explorer", "big", "recent", "cleanup", "monitor"})

    def refresh_view(self) -> None:
        """刷新（F5）：同步刚才的文件增删（包括在资源管理器里删的），再重新读取当前页面。不做整盘扫描。"""
        self.sample_now()
        try:
            self.cleanup_items = self.index.cleanup_objects()
        except Exception:  # noqa: BLE001
            pass
        if not self.index.last_scan_time():
            self._refresh_all_pages()
            self.status("已刷新磁盘容量。还没有索引，点「完整扫描」开始第一次扫描。")
            return
        if self.is_busy() or self.syncing:
            self._refresh_all_pages()
            self.status("已刷新页面。后台任务结束后会自动同步文件变动。")
            return
        extra = set()
        explorer = self.pages["explorer"]
        if self.current_key() == "explorer" and explorer.path:
            extra.add(explorer.path)
        self.status("正在检查文件变动…")
        self._run_sync(extra, prune=True, manual=True)

    def _refresh_all_pages(self) -> None:
        current = self.current_key()
        self._dirty.update(k for k, *_ in PAGES if k not in (current, "settings"))
        self._refresh_page(current)

    # ================= 文件变动同步 =================
    def apply_watch_settings(self) -> None:
        if not self.config.scan.get("watch", True):
            self.watchers.stop()
            return
        for err in self.watchers.update(self.scan_roots(), [self.data_dir]):
            self.log("文件变动监听启动失败", err)

    def _poll_changes(self) -> None:
        if self.syncing or self.is_busy():
            return
        if self.config.scan.get("watch", True) and self.watchers.watchers:
            # 监听线程意外退出（比如 U 盘拔掉）时重新挂上
            if any(not w.alive for w in self.watchers.watchers.values()):
                self.apply_watch_settings()
        if self.watchers.has_pending():
            self._run_sync(set())

    def _run_sync(self, extra: set[str], prune: bool = False, manual: bool = False) -> None:
        dirs, overflow = self.watchers.drain()
        dirs |= extra
        if not dirs and not prune:
            return
        opts = scanner.ScanOptions(**self._scan_opts(self.scan_roots()))
        index = self.index
        self.syncing = True

        def work():
            started = time.monotonic()
            result = sync.sync_dirs(index, dirs, opts) if dirs else sync.SyncResult()
            if prune:
                result.merge(sync.prune_missing(index, opts))
            return result, time.monotonic() - started

        def failed(_exc, tb: str) -> None:
            self.syncing = False
            self.log("同步文件变动失败", tb)
            if manual:
                self._refresh_all_pages()
                self.status("同步文件变动失败，详情见 data/app.log；页面已按现有索引刷新。")

        workers.run_async(work, lambda res: self._after_sync(res[0], res[1], overflow, manual), failed)

    def _after_sync(self, result: sync.SyncResult, elapsed: float, overflow: bool, manual: bool) -> None:
        self.syncing = False
        pages = {"dashboard", "explorer", "big", "recent"}
        notable = result.removed or result.added or abs(result.delta) >= util.MB
        if manual:
            self.volumesChanged.emit()
            self._refresh_all_pages()
        elif result.changed:
            self.volumesChanged.emit()
            current = self.current_key()
            self._dirty.update(pages - {current})
            if current in pages:
                if self._sync_affects(current, result):
                    self._refresh_page(current, soft=True)
                else:
                    self._refresh_on_activate = True
        if self.is_busy():
            return
        text = _sync_text(result)
        if overflow:
            self.status("文件变动太多，部分目录可能没跟上；可以对相关文件夹点「重新扫描此文件夹」。")
        elif manual:
            self.status(f"已刷新：{text}（{elapsed:.1f} 秒）" if result.changed else
                        f"已刷新，没有发现新的文件变动（检查用时 {elapsed:.1f} 秒）")
        elif notable:
            self.status(f"已同步文件变动：{text}")

    def _sync_affects(self, key: str, result: sync.SyncResult) -> bool:
        """这次变动是否影响当前页面上显示的内容。后台程序的零碎改动（缓存、日志）不打断正在看的页面，
        切回窗口或按 F5 时再刷新。"""
        active = self.isActiveWindow()
        structural = bool(result.removed or result.added)
        if key == "explorer":
            path = self.pages["explorer"].path
            if not path:
                return result.changed
            if not result.touches(path):
                return False
            return not active or structural or abs(result.delta) >= util.MB
        big_change = structural or abs(result.delta) >= int(self.config.scan.get("min_big_size") or 100 * util.MB)
        if key == "big":
            return big_change
        return big_change or (not active and abs(result.delta) >= util.MB)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.ActivationChange or not self.isActiveWindow():
            return
        if self.watchers.has_pending() and not self.syncing and not self.is_busy():
            self._refresh_on_activate = False
            self._run_sync(set())
            QTimer.singleShot(0, self._activation_refresh_after_sync)
        elif self._refresh_on_activate:
            self._refresh_on_activate = False
            self._refresh_page(self.current_key(), soft=True)

    def _activation_refresh_after_sync(self) -> None:
        if self.syncing:
            QTimer.singleShot(150, self._activation_refresh_after_sync)
            return
        if self._refresh_on_activate:
            self._refresh_on_activate = False
            self._refresh_page(self.current_key(), soft=True)

    def _startup(self) -> None:
        try:
            self.cleanup_items = self.index.cleanup_objects()
        except Exception:  # noqa: BLE001
            self.cleanup_items = []
        self._update_last_scan()
        self._refresh_page("dashboard")
        self._dirty.update(k for k, *_ in PAGES if k != "dashboard")
        self.sample_now()
        if self.config.app.get("autostart") and not monitor.autostart_enabled():
            self.set_autostart(True)
        self.apply_watch_settings()
        if self.index.last_scan_time():
            # 程序没运行期间被删掉的大文件 / 大目录：启动后在后台核对一次
            QTimer.singleShot(4000, self._startup_check)

    def _startup_check(self) -> None:
        if self.is_busy() or self.syncing:
            QTimer.singleShot(30000, self._startup_check)
            return
        self._run_sync(set(), prune=True)

    def _update_last_scan(self) -> None:
        last = self.index.last_scan_time()
        scan = self.index.last_scan()
        method = {"mft": "MFT 直读", "scandir": "目录遍历"}.get((scan or {}).get("method", ""), "")
        if last:
            self.last_scan_label.setText(f"上次扫描 {util.fmt_time(last)}" + (f" · {method}" if method else ""))
        else:
            self.last_scan_label.setText("还没有索引")
        self.scan_btn.setText("重新扫描" if last else "完整扫描")

    # ================= 通用 =================
    def status(self, text: str) -> None:
        self.status_label.setText(text)

    def log(self, message: str, detail: str = "") -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n{detail}\n")
        except OSError:
            pass

    def is_busy(self) -> bool:
        return bool(self.scan_job or self.analysis_job or self.deleting or self.migrating)

    def _set_progress(self, value: float | None) -> None:
        if value is None:
            self.progress.hide()
            return
        self.progress.show()
        if value < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(value * 1000))

    def _update_busy(self) -> None:
        busy = self.is_busy()
        self.scan_btn.setEnabled(not busy)
        self.analyze_btn.setEnabled(not busy)
        self.quick_btn.setEnabled(not busy)
        self.stop_btn.setVisible(bool(self.scan_job or self.analysis_job))
        if not busy:
            self._set_progress(None)
        for page in self.pages.values():
            if hasattr(page, "on_busy"):
                page.on_busy(busy)

    def current_selection(self) -> list[dict]:
        page = self.stack.currentWidget()
        getter = getattr(page, "selected_records", None)
        if callable(getter):
            try:
                return getter() or []
            except Exception:  # noqa: BLE001
                return []
        return []

    # ================= 扫描 =================
    def scan_roots(self) -> list[str]:
        roots = []
        for letter in self.config.scan.get("drives") or ["C:", "D:"]:
            root = f"{letter.rstrip(':')}:\\"
            if os.path.isdir(root):
                roots.append(root)
        return roots

    def start_scan(self, auto: bool = False) -> None:
        if self.is_busy():
            if not auto:
                self.status("有任务正在进行，稍后再扫描。")
            return
        roots = self.scan_roots()
        if not roots:
            if not auto:
                warn(self, "没有可扫描的磁盘", "请到「设置」里勾选要扫描的磁盘。")
            return
        self._auto_scan = auto
        self._scan_folder = ""
        self.scan_job = ProcessJob(jobs.scan_job, (self._scan_opts(roots), self.index.path), self)
        self.scan_job.message.connect(self._on_scan_message)
        self.scan_job.finished.connect(self._on_scan_finished)
        self.scan_job.start()
        self._set_progress(-1)
        self.status(("自动扫描：" if auto else "") + "正在启动扫描 " + "、".join(r.rstrip("\\") for r in roots) + " …")
        self.busyChanged.emit()

    def _scan_opts(self, roots: list[str]) -> dict:
        cfg = self.config.scan
        return {
            "roots": roots,
            "min_big_size": int(cfg.get("min_big_size") or 100 * util.MB),
            "top_k": int(cfg.get("top_k") or 20000),
            "recent_days": int(cfg.get("recent_days") or 180),
            "recent_cap": int(cfg.get("recent_cap") or 200000),
            "max_depth": int(cfg.get("max_depth") or 0),
            "skip_paths": [self.data_dir],
            "threads": int(cfg.get("threads") or 0),
            "method": cfg.get("method") or "auto",
        }

    def rescan_folder(self, path: str) -> None:
        """只重新扫描一个文件夹并更新索引；盘根目录等同于扫描整个盘。"""
        path = scanner.dir_key(os.path.abspath(path))
        if self.is_busy():
            self.status("有任务正在进行，稍后再刷新这个文件夹。")
            return
        if len(path) <= 3:
            if confirm(self, "重新扫描整个磁盘", f"{path} 是磁盘根目录，重新扫描它等于整盘扫描，需要几分钟。",
                       "扫描在后台进行，界面可以继续使用。", ok_text="开始扫描"):
                self._start_drive_scan([path])
            return
        if not os.path.isdir(path) and not self.index.dir_info(path):
            self.status(f"找不到文件夹：{path}")
            return
        opts = self._scan_opts([path])
        info = self.index.dir_info(path)
        if opts["threads"] == 0 and (not info or int(info["files"] or 0) < 30000):
            opts["threads"] = 1  # 小文件夹单进程更快，省掉启动多个子进程的开销
        self._auto_scan = False
        self._scan_folder = path
        self.scan_job = ProcessJob(jobs.folder_job, (opts, self.index.path, path), self)
        self.scan_job.message.connect(self._on_scan_message)
        self.scan_job.finished.connect(self._on_scan_finished)
        self.scan_job.start()
        self._set_progress(-1)
        self.status(f"正在重新扫描 {util.short_path(path, 60)} …")
        self.busyChanged.emit()

    def _start_drive_scan(self, roots: list[str]) -> None:
        self._auto_scan = False
        self._scan_folder = ""
        self.scan_job = ProcessJob(jobs.scan_job, (self._scan_opts(roots), self.index.path), self)
        self.scan_job.message.connect(self._on_scan_message)
        self.scan_job.finished.connect(self._on_scan_finished)
        self.scan_job.start()
        self._set_progress(-1)
        self.status("正在扫描 " + "、".join(r.rstrip("\\") for r in roots) + " …")
        self.busyChanged.emit()

    def stop_jobs(self) -> None:
        for job in (self.scan_job, self.analysis_job):
            if job:
                job.cancel()
        self.status("正在停止…")

    def _on_scan_message(self, kind: str, payload) -> None:
        if kind == "progress":
            st = payload
            method = "MFT 直读" if st.get("method") == "mft" else "遍历"
            phase = st.get("phase") or ""
            current = util.short_path(st.get("current") or "", 50)
            text = (f"扫描中（{method}）：{util.human_count(st['files'])} 个文件 · {util.human_size(st['bytes'])} · "
                    f"{util.human_count(st['dirs'])} 个目录 · {int(st['elapsed'])} 秒")
            if phase and phase not in ("遍历目录",):
                text += f" · {phase}"
            elif current:
                text += f" · {current}"
            self.status(text)
            self._set_progress(float(st.get("progress", -1)))
        elif kind == "writing":
            self.status(f"扫描完成，正在写入索引（{util.human_count(payload)} 个目录）…")
            self._set_progress(-1)
        elif kind == "done" and payload.get("folder"):
            st = payload
            delta = int(st.get("after", 0)) - int(st.get("before", 0))
            change = ("，大小没有变化" if abs(delta) < util.MB else
                      f"，比上次{'多了' if delta > 0 else '少了'} {util.human_size(abs(delta))}")
            self.status(f"已重新扫描 {util.short_path(st['folder'], 50)}：{util.human_count(st['files'])} 个文件，"
                        f"{util.human_size(st['bytes'])}{change}，耗时 {util.fmt_duration(st['elapsed'])}")
        elif kind == "done":
            st = payload
            method = "MFT 直读" if "mft" in (st.get("method") or "") else "多进程遍历"
            self.status(
                f"扫描完成（{method}）：{util.human_count(st['files'])} 个文件，{util.human_size(st['bytes'])}，"
                f"耗时 {util.fmt_duration(st['elapsed'])}" + (f"，{st['errors']} 处无法访问" if st["errors"] else "")
            )
        elif kind == "cancelled":
            self.status(str(payload))
        elif kind == "error":
            self.log("扫描失败", str(payload))
            self.status("扫描失败，详情见 data/app.log")
            if not self._auto_scan:
                error(self, "扫描失败", "扫描过程中出错，详情已写入 data/app.log。")

    def _on_scan_finished(self, kind: str) -> None:
        self.scan_job = None
        folder, self._scan_folder = self._scan_folder, ""
        self.busyChanged.emit()
        if kind == "done" and folder:
            self.indexChanged.emit()
            self.sample_now()
        elif kind == "done":
            self.indexChanged.emit()
            if self._auto_scan and not self.isVisible():
                self.tray.notify("自动扫描完成", self.status_label.text())
            QTimer.singleShot(300, lambda: self.start_analysis(quiet=True))

    # ================= 清理分析 =================
    def start_analysis(self, quiet: bool = False) -> None:
        if self.is_busy():
            return
        self._analysis_quiet = quiet
        self.analysis_job = ProcessJob(jobs.analysis_job, (self.index.path, self.app_dir), self)
        self.analysis_job.message.connect(self._on_analysis_message)
        self.analysis_job.finished.connect(self._on_analysis_finished)
        self.analysis_job.start()
        self._set_progress(-1)
        self.status("正在分析可清理项…")
        self.busyChanged.emit()

    def _on_analysis_message(self, kind: str, payload) -> None:
        if kind == "progress":
            self.status(str(payload))
        elif kind == "error":
            self.log("分析失败", str(payload))
            self.status("分析失败，详情见 data/app.log")
            if not self._analysis_quiet:
                error(self, "分析失败", "分析可清理项时出错，详情已写入 data/app.log。")
        elif kind == "cancelled":
            self.status(str(payload))

    def _on_analysis_finished(self, kind: str) -> None:
        self.analysis_job = None
        self.busyChanged.emit()
        if kind != "done":
            return
        self.cleanup_items = self.index.cleanup_objects()
        summary = recommend.summarize(self.cleanup_items)
        self.status(
            f"分析完成：可回收约 {util.human_size(summary['reclaimable'])}"
            f"（安全 {util.human_size(summary['totals'].get('safe', 0))}，"
            f"谨慎 {util.human_size(summary['totals'].get('caution', 0))}）"
        )
        self.cleanupChanged.emit()
        if not self._analysis_quiet:
            self.goto("cleanup")

    # ================= 删除 =================
    def delete_records(self, records: list[dict]) -> None:
        if self.is_busy() or not records:
            return
        paths = [r["path"] for r in records]
        total = sum(int(r.get("size") or 0) for r in records)
        details = "\n".join(f"· {p}" for p in paths[:8]) + (f"\n… 以及另外 {len(paths) - 8} 项" if len(paths) > 8 else "")
        if not confirm(
            self, "移到回收站",
            f"即将把 {len(paths)} 项（{util.human_size(total)}）移到回收站。",
            "可以在回收站里还原；清空回收站后才会真正释放空间。\n\n" + details,
            danger=True, ok_text="移到回收站",
        ):
            return
        self._run_delete(paths, [], [], f"正在移到回收站（{len(paths)} 项）…")

    def delete_cleanup(self, items: list, title: str = "确认清理") -> None:
        if self.is_busy() or not items:
            return
        recycle = sorted({util.drive_of(i.path).rstrip(":") for i in items if i.kind == "recyclebin"})
        total = sum(i.size for i in items)
        details = "\n".join(f"· {i.title}（{util.human_size(i.size)}，{i.level_text}）" for i in items[:10])
        if len(items) > 10:
            details += f"\n… 以及另外 {len(items) - 10} 项"
        text = (
            "· 文件会移到回收站，可以还原；清空回收站后才真正腾出空间\n"
            + ("· 其中「回收站」会被直接清空，无法恢复\n" if recycle else "")
            + "· 系统 / 程序目录已被自动拦截，正在使用的文件会跳过\n\n" + details
        )
        if not confirm(self, title, f"即将处理 {len(items)} 项，合计约 {util.human_size(total)}。", text,
                       danger=True, ok_text="开始清理"):
            return
        self._run_delete(None, recycle, items, f"正在清理 {len(items)} 项…")

    def quick_clean(self) -> None:
        if self.is_busy():
            return
        if not self.cleanup_items:
            if confirm(self, "一键安全清理", "还没有清理分析结果。", "现在先分析一次可清理项（通常几十秒），完成后再点一键清理。",
                       ok_text="开始分析"):
                self.start_analysis()
            return
        items = [i for i in self.cleanup_items if i.level == "safe" and i.deletable and i.kind != "recyclebin"]
        if not items:
            info(self, "一键安全清理", "当前没有「安全」等级的清理项。可以到「清理推荐」查看其它项目。")
            return
        self.delete_cleanup(items, "一键安全清理（回收站需在「清理推荐」里单独清空）")

    def _run_delete(self, paths: list[str] | None, recycle: list[str], items: list, text: str) -> None:
        self.deleting = True
        self.busyChanged.emit()
        self._set_progress(-1)
        self.status(text)
        all_items = list(self.cleanup_items)
        app_dir = self.app_dir
        index = self.index

        def work():
            messages = []
            for drive in recycle:
                ok, msg = cleanup.empty_recycle_bin(drive)
                messages.append(f"{drive}: 回收站已清空" if ok else f"{drive}: {msg}")
            targets = list(paths or [])
            for item in items:
                if item.kind == "dir_contents":
                    targets.extend(cleanup.expand_contents(item.path))
                elif item.kind != "recyclebin":
                    targets.append(item.path)
            report = cleanup.delete_paths(targets, app_dir)
            indexed = index.apply_deletions(report.deleted)
            remaining = _remaining_items(all_items, items, report)
            if items:
                index.replace_cleanup(remaining)
            return report, remaining, messages, indexed

        workers.run_async(work, self._after_delete, self._delete_failed)

    def _after_delete(self, result) -> None:
        report, remaining, messages, indexed = result
        self.deleting = False
        self.cleanup_items = remaining
        self.busyChanged.emit()
        freed = util.human_size(max(report.bytes_freed, indexed))
        self.status(
            (f"已移到回收站 {len(report.deleted)} 项（约 {freed}）" if report.deleted else "没有文件被删除")
            + ("；" + "；".join(messages) if messages else "")
        )
        if report.failed:
            detail = "\n".join(f"· {p}（{reason}）" for p, reason in report.failed[:10])
            warn(self, "部分项目未能删除", f"成功 {len(report.deleted)} 项，失败 {len(report.failed)} 项：\n\n{detail}")
        elif report.deleted:
            info(self, "完成", f"已把 {len(report.deleted)} 项移到回收站。\n清空回收站后才会真正腾出磁盘空间。")
        self.cleanupChanged.emit()
        self._mark_dirty({"dashboard", "explorer", "big", "recent", "cleanup"})
        self.sample_now()

    def _delete_failed(self, _exc, tb: str) -> None:
        self.deleting = False
        self.busyChanged.emit()
        self.log("删除失败", tb)
        error(self, "删除失败", "删除过程中出错，详情已写入 data/app.log。")

    # ================= 迁移 =================
    def migrate_dir(self, path: str, size: int = 0) -> None:
        if self.is_busy():
            return
        from .dialogs import MigrateDialog

        dialog = MigrateDialog(self, path, size)
        dialog.exec()

    def set_migrating(self, value: bool) -> None:
        self.migrating = value
        self.busyChanged.emit()
        if not value:
            self._mark_dirty({"dashboard", "explorer", "cleanup"})
            self.sample_now()

    # ================= AI =================
    def toggle_ai(self) -> None:
        if self.ai_dock.isVisible():
            self.ai_dock.hide()
        else:
            self._show_ai()
            self.ai_panel.focus_input()

    def _show_ai(self) -> None:
        self.ai_dock.show()
        if not self._ai_sized:
            self._ai_sized = True
            self.resizeDocks([self.ai_dock], [440], Qt.Horizontal)
        self.ai_panel.refresh_mode()

    def ask_ai(self, records: list[dict]) -> None:
        self._show_ai()
        self.ai_panel.ask_records(records)

    # ================= 监视 =================
    def sample_now(self) -> None:
        if self.scan_job is not None:
            # 扫描子进程写库时不抢写锁，只刷新界面上的容量
            self.volumesChanged.emit()
            self.tray.update_tooltip()
            return
        sampler = self.sampler

        def done(alerts):
            self.volumesChanged.emit()
            self.tray.update_tooltip()
            if alerts and self.config.monitor.get("notify", True):
                for alert in alerts:
                    self.tray.notify("磁盘空间提醒", alert.message, warning=True)
            if alerts:
                self._mark_dirty({"monitor"})

        workers.run_async(lambda: sampler.sample(), done,
                          lambda _e, tb: self.log("采样失败", tb))

    def _maybe_auto_scan(self) -> None:
        cfg = self.config.scan
        if not cfg.get("auto_scan") or self.is_busy():
            return
        if not monitor.scan_due(self.index.last_scan_time(), float(cfg.get("auto_scan_hours") or 24)):
            return
        if monitor.idle_seconds() < 180:
            return
        self.start_scan(auto=True)

    def set_autostart(self, enabled: bool) -> bool:
        try:
            monitor.set_autostart(enabled, monitor.autostart_command(self.app_dir))
            return True
        except OSError as exc:
            self.log("设置开机自启失败", str(exc))
            return False

    # ================= 窗口 =================
    def show_normal(self) -> None:
        was_hidden = not self.isVisible()
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        if was_hidden:
            self.volumesChanged.emit()

    def closeEvent(self, event) -> None:
        if not self._quitting and self.config.app.get("close_to_tray", True) and self.tray.available():
            event.ignore()
            self._save_geometry()
            self.hide()
            if not self.config.app.get("tray_hint_shown"):
                self.tray.notify("硬盘监视器仍在后台运行", "会继续记录磁盘空间并在空间不足时提醒。右键托盘图标可以退出。")
                self.config.app["tray_hint_shown"] = True
                self._save_config()
            return
        self._shutdown()
        event.accept()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()

    def _save_geometry(self) -> None:
        if not self.isMinimized() and not self.isMaximized():
            self.config.app["window_geometry"] = f"{self.width()}x{self.height()}"

    def _save_config(self) -> None:
        try:
            self.config.save()
        except OSError:
            pass

    def _shutdown(self) -> None:
        self._save_geometry()
        self._save_config()
        for job in (self.scan_job, self.analysis_job):
            if job:
                job.cancel()
        self.watchers.stop()
        self.ai_panel.stop()
        self.tray.hide()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()


def _sync_text(result: sync.SyncResult) -> str:
    parts = []
    if result.removed:
        parts.append(f"{result.removed} 个文件夹已不存在")
    if result.added:
        parts.append(f"新增 {result.added} 个文件夹")
    if abs(result.delta) >= 1024:
        parts.append(f"占用{'增加' if result.delta > 0 else '减少'} {util.human_size(abs(result.delta))}")
    if not parts:
        parts.append(f"更新了 {result.dirs} 个文件夹")
    return "，".join(parts)


def _remaining_items(all_items: list, targets: list, report) -> list:
    """删除后重新测量被处理的目录类条目，其它条目保持不变。"""
    deleted = {os.path.normcase(p) for p, _s in report.deleted}
    target_ids = {id(t) for t in targets}
    out = []
    for item in all_items:
        if id(item) not in target_ids:
            out.append(item)
            continue
        if item.kind == "recyclebin":
            continue
        if item.kind == "dir_contents":
            size, _count = scanner.measure_dir(item.path)
            if size > 0:
                item.size = size
                out.append(item)
        elif os.path.normcase(item.path) in deleted or not os.path.exists(item.path):
            continue
        else:
            out.append(item)
    return out
