"""常用小组件：卡片、按钮、图标、面包屑、分段按钮、对话框。"""
from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme

# Segoe Fluent Icons / MDL2 Assets 码位
ICONS = {
    "home": "\ue80f",
    "treemap": "\uf0e2",
    "folder": "\ue8b7",
    "file": "\ue7c3",
    "recent": "\ue823",
    "clean": "\ue74d",
    "monitor": "\ue9d2",
    "settings": "\ue713",
    "ai": "\ue8bd",
    "scan": "\ue721",
    "stop": "\ue71a",
    "refresh": "\ue72c",
    "back": "\ue72b",
    "up": "\ue74a",
    "open": "\ue8da",
    "copy": "\ue8c8",
    "delete": "\ue74d",
    "move": "\ue8de",
    "sparkle": "\ue945",
    "warning": "\ue7ba",
    "check": "\ue73e",
    "export": "\uede1",
    "chevron": "\ue76c",
    "drive": "\ueda2",
    "close": "\ue711",
}


def glyph_icon(name: str, color: QColor | str | None = None, size: int = 18) -> QIcon:
    if not theme.ICON_FAMILY or name not in ICONS:
        return QIcon()
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    font = QFont(theme.ICON_FAMILY)
    font.setPixelSize(int(size * 1.5))
    painter.setFont(font)
    painter.setPen(QColor(color) if color is not None else theme.color("text"))
    painter.drawText(pix.rect(), Qt.AlignCenter, ICONS[name])
    painter.end()
    pix.setDevicePixelRatio(2.0)
    return QIcon(pix)


def app_icon(level: str = "ok", size: int = 64) -> QIcon:
    """程序 / 托盘图标：圆角方块 + 磁盘环；level=warn/danger 时变色。"""
    base = {"danger": "#dc2626", "warn": "#d97706"}.get(level, "#2563eb")
    icon = QIcon()
    for px in (16, 20, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(px, px)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, px - 1, px - 1)
        path = QPainterPath()
        path.addRoundedRect(r, px * 0.22, px * 0.22)
        p.fillPath(path, QColor(base))
        pen = QPen(QColor("#ffffff"), max(1.5, px * 0.11))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        inset = px * 0.24
        ring = r.adjusted(inset, inset, -inset, -inset)
        p.drawArc(ring, 90 * 16, -280 * 16)
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.NoPen)
        dot = px * 0.09
        p.drawEllipse(ring.center(), dot, dot)
        p.end()
        icon.addPixmap(pix)
    return icon


def label(text: str = "", role: str | None = None, wrap: bool = False, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def set_role(lab: QLabel, role: str) -> None:
    if lab.property("role") != role:
        lab.setProperty("role", role)
        theme.repolish(lab)


def button(text: str, kind: str | None = None, icon: str | None = None, tip: str | None = None,
           on_click=None) -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setProperty("kind", kind)
    if icon:
        color = "#ffffff" if kind in ("primary", "danger") else (theme.COLORS["accent"] if kind == "ghost" else None)
        btn.setIcon(glyph_icon(icon, color, 16))
        btn.setIconSize(QSize(16, 16))
    if tip:
        btn.setToolTip(tip)
    if on_click:
        btn.clicked.connect(lambda _checked=False: on_click())
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def tool_button(icon: str, tip: str, on_click=None) -> QToolButton:
    btn = QToolButton()
    btn.setIcon(glyph_icon(icon, None, 16))
    btn.setIconSize(QSize(16, 16))
    btn.setToolTip(tip)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.PointingHandCursor)
    if on_click:
        btn.clicked.connect(lambda _checked=False: on_click())
    return btn


class Card(QFrame):
    def __init__(self, title: str | None = None, parent=None, margins: int = 16, spacing: int = 10):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(margins, margins - 2, margins, margins)
        self.body.setSpacing(spacing)
        self.header = None
        if title is not None:
            self.header = QHBoxLayout()
            self.header.setSpacing(8)
            self.title_label = label(title, "h2")
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            self.body.addLayout(self.header)


def hbox(*items, spacing: int = 8, margins=(0, 0, 0, 0), stretch_at: int | None = None) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for i, item in enumerate(items):
        if stretch_at is not None and i == stretch_at:
            lay.addStretch(1)
        if item is None:
            lay.addStretch(1)
        elif isinstance(item, int):
            lay.addSpacing(item)
        elif isinstance(item, QWidget):
            lay.addWidget(item)
        else:
            lay.addLayout(item)
    return lay


class Breadcrumb(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_path(self, path: str) -> None:
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not path:
            self._lay.addStretch(1)
            return
        parts = [p for p in path.split("\\") if p]
        crumbs = []
        acc = ""
        for i, part in enumerate(parts):
            acc = part + "\\" if i == 0 else (acc.rstrip("\\") + "\\" + part)
            crumbs.append((part if i else part + "\\", acc))
        if len(crumbs) > 7:
            crumbs = crumbs[:2] + [("…", None)] + crumbs[-4:]
        for i, (text, target) in enumerate(crumbs):
            if i:
                sep = QLabel("›")
                sep.setProperty("role", "muted")
                self._lay.addWidget(sep)
            btn = QToolButton()
            btn.setProperty("kind", "crumb")
            btn.setText(text)
            btn.setCursor(Qt.PointingHandCursor)
            if target is None:
                btn.setEnabled(False)
            else:
                btn.clicked.connect(lambda _c=False, t=target: self.navigate.emit(t))
            if i == len(crumbs) - 1:
                font = btn.font()
                font.setBold(True)
                btn.setFont(font)
            self._lay.addWidget(btn)
        self._lay.addStretch(1)


class Segmented(QWidget):
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str | None = None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for value, text in options:
            btn = QPushButton(text)
            btn.setProperty("kind", "seg")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, v=value: self.changed.emit(v))
            self._group.addButton(btn)
            self._buttons[value] = btn
            lay.addWidget(btn)
        if options:
            self.set_value(current or options[0][0])

    def set_value(self, value: str) -> None:
        if value in self._buttons:
            self._buttons[value].setChecked(True)

    def value(self) -> str:
        for value, btn in self._buttons.items():
            if btn.isChecked():
                return value
        return ""

    def set_options_enabled(self, values: set[str]) -> None:
        for value, btn in self._buttons.items():
            btn.setVisible(value in values)


def confirm(parent, title: str, text: str, detail: str | None = None, danger: bool = False,
            ok_text: str = "继续") -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    ok = box.addButton(ok_text, QMessageBox.AcceptRole)
    box.addButton("取消", QMessageBox.RejectRole)
    if danger:
        ok.setProperty("kind", "danger")
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok


def info(parent, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def warn(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def error(parent, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def short_name(path: str) -> str:
    stripped = path.rstrip("\\")
    return os.path.basename(stripped) or path


def global_pos(widget: QWidget, pos: QPoint) -> QPoint:
    return widget.mapToGlobal(pos)
