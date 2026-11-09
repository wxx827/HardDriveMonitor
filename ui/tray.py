"""系统托盘：常驻图标、剩余空间提示、通知与快捷菜单。"""
from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from core import util, volumes
from .widgets.common import app_icon, glyph_icon


class Tray(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._level = "ok"
        self.icon = QSystemTrayIcon(app_icon(), win)
        menu = QMenu()
        self._actions = []
        for text, icon, fn in (
            ("打开硬盘监视器", "home", win.show_normal),
            ("立即扫描", "scan", lambda: win.start_scan()),
            ("一键安全清理", "clean", self._quick_clean),
            (None, None, None),
            ("退出", "close", win.quit_app),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(glyph_icon(icon, None, 16), text, menu)
            action.triggered.connect(lambda _c=False, f=fn: f())
            menu.addAction(action)
            self._actions.append(action)
        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.messageClicked.connect(win.show_normal)
        self.icon.setToolTip("硬盘监视器")
        if self.available():
            self.icon.show()

    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def hide(self) -> None:
        self.icon.hide()

    def _quick_clean(self) -> None:
        self.win.show_normal()
        self.win.quick_clean()

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.win.isVisible() and not self.win.isMinimized():
                self.win.hide()
            else:
                self.win.show_normal()

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        if not self.available():
            return
        icon = QSystemTrayIcon.Warning if warning else QSystemTrayIcon.Information
        self.icon.showMessage(title, message, icon, 8000)

    def update_tooltip(self) -> None:
        cfg = self.win.config.monitor
        lines = ["硬盘监视器"]
        level = "ok"
        for vol in volumes.list_volumes():
            lines.append(f"{vol.letter} 剩余 {util.human_size(vol.free)}（{vol.free_pct:.0f}%）")
            low_pct = float(cfg.get("low_pct") or 0)
            low_gb = float(cfg.get("low_gb") or 0)
            if (low_pct and vol.free_pct < low_pct) or (low_gb and vol.free < low_gb * util.GB):
                level = "danger"
            elif vol.used_pct >= 85 and level == "ok":
                level = "warn"
        self.icon.setToolTip("\n".join(lines))
        if level != self._level:
            self._level = level
            self.icon.setIcon(app_icon(level))
