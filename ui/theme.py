"""界面主题：浅色 / 深色两套调色板 + Fluent 风格 QSS。"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

LIGHT = {
    "bg": "#f3f5f9",
    "panel": "#ffffff",
    "panel_alt": "#f8fafc",
    "border": "#e3e7ef",
    "border_strong": "#cfd6e2",
    "text": "#1f2937",
    "muted": "#6b7280",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_soft": "#e6eefe",
    "ok": "#059669",
    "warn": "#d97706",
    "danger": "#dc2626",
    "danger_hover": "#b91c1c",
    "selection": "#dbe7ff",
    "header": "#f6f8fb",
    "track": "#e5e9f2",
    "tooltip_bg": "#111827",
    "tooltip_fg": "#ffffff",
}

DARK = {
    "bg": "#16181d",
    "panel": "#1f2229",
    "panel_alt": "#23262e",
    "border": "#2e323c",
    "border_strong": "#3a3f4b",
    "text": "#e5e7eb",
    "muted": "#9ca3af",
    "accent": "#3b82f6",
    "accent_hover": "#60a5fa",
    "accent_soft": "#1e2a44",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "danger_hover": "#ef4444",
    "selection": "#2a3a5c",
    "header": "#23262e",
    "track": "#2e323c",
    "tooltip_bg": "#e5e7eb",
    "tooltip_fg": "#111827",
}

# Treemap 一级块配色（饱和度适中，浅深色主题都可读）
TREEMAP_HUES = [
    "#4f7cf7", "#22a39a", "#e8833a", "#8b5cf6", "#e05d7b",
    "#3aa0d8", "#7fb238", "#d6a419", "#6c7a91", "#c2410c",
]

COLORS: dict[str, str] = dict(LIGHT)
FONT_FAMILY = "Microsoft YaHei UI"
ICON_FAMILY = ""


def is_dark() -> bool:
    return COLORS.get("bg") == DARK["bg"]


def color(name: str) -> QColor:
    return QColor(COLORS[name])


def level_color(level: str) -> QColor:
    return color({"safe": "ok", "caution": "warn", "blocked": "danger"}.get(level, "muted"))


def usage_color(used_pct: float) -> QColor:
    if used_pct >= 92:
        return color("danger")
    if used_pct >= 85:
        return color("warn")
    return color("accent")


def _qss(c: dict) -> str:
    return f"""
* {{ font-family: "{FONT_FAMILY}"; }}
QWidget {{ color: {c['text']}; font-size: 10pt; }}
QMainWindow, #Central, QStackedWidget > QWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {c['bg']}; }}
QDialog, QMessageBox {{ background: {c['panel']}; }}

#NavBar {{ background: {c['panel']}; border-right: 1px solid {c['border']}; }}
#AppTitle {{ font-size: 13pt; font-weight: 700; padding: 4px 6px 12px 6px; }}
#NavButton {{
    text-align: left; padding: 9px 12px; border: none; border-radius: 8px;
    color: {c['muted']}; background: transparent; font-size: 10.5pt;
}}
#NavButton:hover {{ background: {c['accent_soft']}; color: {c['text']}; }}
#NavButton:checked {{ background: {c['accent_soft']}; color: {c['accent']}; font-weight: 600; }}
#NavFooter {{ color: {c['muted']}; font-size: 9pt; padding: 4px 8px; }}

#TopBar {{ background: {c['bg']}; }}
#PageTitle {{ font-size: 17pt; font-weight: 700; }}

QPushButton, QToolButton {{
    background: {c['panel']}; border: 1px solid {c['border_strong']}; border-radius: 6px;
    padding: 6px 14px; color: {c['text']};
}}
QToolButton {{ padding: 5px 8px; }}
QPushButton:hover, QToolButton:hover {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {c['selection']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {c['muted']}; background: {c['panel_alt']}; border-color: {c['border']}; }}
QPushButton[kind="primary"] {{ background: {c['accent']}; color: #ffffff; border: 1px solid {c['accent']}; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton[kind="primary"]:disabled {{ background: {c['track']}; color: {c['muted']}; border-color: {c['track']}; }}
QPushButton[kind="danger"] {{ background: {c['danger']}; color: #ffffff; border: 1px solid {c['danger']}; font-weight: 600; }}
QPushButton[kind="danger"]:hover {{ background: {c['danger_hover']}; border-color: {c['danger_hover']}; }}
QPushButton[kind="danger"]:disabled {{ background: {c['track']}; color: {c['muted']}; border-color: {c['track']}; }}
QPushButton[kind="ghost"], QToolButton[kind="ghost"] {{ background: transparent; border: none; color: {c['accent']}; padding: 4px 6px; }}
QPushButton[kind="ghost"]:hover, QToolButton[kind="ghost"]:hover {{ background: {c['accent_soft']}; }}
QPushButton[kind="seg"] {{ border-radius: 0; padding: 4px 12px; }}
QPushButton[kind="seg"]:checked {{ background: {c['accent']}; color: #ffffff; border-color: {c['accent']}; }}
QToolButton[kind="crumb"] {{ background: transparent; border: none; padding: 3px 6px; color: {c['text']}; }}
QToolButton[kind="crumb"]:hover {{ background: {c['accent_soft']}; color: {c['accent']}; }}

#Card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
#Card QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-size: 17pt; font-weight: 700; }}
QLabel[role="h2"] {{ font-size: 12pt; font-weight: 600; }}
QLabel[role="muted"] {{ color: {c['muted']}; }}
QLabel[role="small"] {{ color: {c['muted']}; font-size: 9pt; }}
QLabel[role="big"] {{ font-size: 24pt; font-weight: 700; }}
QLabel[role="ok"] {{ color: {c['ok']}; }}
QLabel[role="warn"] {{ color: {c['warn']}; }}
QLabel[role="danger"] {{ color: {c['danger']}; }}
QLabel[role="banner"] {{ background: {c['accent_soft']}; color: {c['accent']}; border-radius: 8px; padding: 8px 12px; }}
QLabel[role="bannerWarn"] {{ background: rgba(217,119,6,0.12); color: {c['warn']}; border-radius: 8px; padding: 8px 12px; }}

QTableView, QTreeView, QTreeWidget, QListWidget {{
    background: {c['panel']}; alternate-background-color: {c['panel_alt']};
    border: 1px solid {c['border']}; border-radius: 8px;
    selection-background-color: {c['selection']}; selection-color: {c['text']};
    gridline-color: transparent; outline: 0;
}}
QTableView::item, QTreeView::item {{ padding: 0 6px; border: none; }}
QTreeView::item:hover, QTableView::item:hover {{ background: {c['accent_soft']}; }}
QTreeView::item:selected, QTableView::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {c['header']}; color: {c['muted']}; border: none;
    border-bottom: 1px solid {c['border']}; padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {c['header']}; border: none; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {c['panel']}; border: 1px solid {c['border_strong']}; border-radius: 6px;
    padding: 5px 8px; selection-background-color: {c['accent']}; selection-color: #ffffff;
}}
QTextBrowser {{ padding: 10px 14px; border-color: {c['border']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['border']}; selection-background-color: {c['selection']}; selection-color: {c['text']}; outline: 0; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QTreeView::indicator {{
    width: 15px; height: 15px; border: 1px solid {c['border_strong']}; border-radius: 4px; background: {c['panel']};
}}
QCheckBox::indicator:hover, QTreeView::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked, QTreeView::indicator:checked {{
    background: {c['accent']}; border-color: {c['accent']}; image: url({c['_check']});
}}
QTreeView::indicator:indeterminate {{ background: {c['accent']}; border-color: {c['accent']}; image: url({c['_dash']}); }}
QCheckBox::indicator:disabled {{ background: {c['panel_alt']}; border-color: {c['border']}; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none; background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: none; background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {c['accent_soft']}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({c['_up']}); width: 9px; height: 9px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow {{ image: url({c['_down']}); width: 9px; height: 9px; }}

QProgressBar {{ background: {c['track']}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {c['accent_soft']}; }}
QStatusBar {{ background: {c['panel']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
QStatusBar QLabel {{ color: {c['muted']}; padding: 0 6px; }}
QToolTip {{ background: {c['tooltip_bg']}; color: {c['tooltip_fg']}; border: none; padding: 6px 8px; }}
QMenu {{ background: {c['panel']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['accent']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
#AiTitle {{ background: {c['panel']}; border-left: 1px solid {c['border']}; border-bottom: 1px solid {c['border']}; }}
#AiTitle QLabel {{ background: transparent; }}
#AiPanel {{ background: {c['panel']}; border-left: 1px solid {c['border']}; }}
QPushButton[kind="chip"] {{
    background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: 14px;
    padding: 4px 12px; color: {c['text']}; font-size: 9.5pt;
}}
QPushButton[kind="chip"]:hover {{ background: {c['accent_soft']}; border-color: {c['accent']}; color: {c['accent']}; }}
QTabBar::tab {{ padding: 6px 14px; }}
"""


def _pick_font(names: list[str]) -> str:
    families = set(QFontDatabase.families())
    for name in names:
        if name in families:
            return name
    return ""


def apply_theme(app: QApplication, mode: str = "light") -> None:
    global FONT_FAMILY, ICON_FAMILY
    COLORS.clear()
    COLORS.update(DARK if mode == "dark" else LIGHT)
    FONT_FAMILY = _pick_font(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"]) or app.font().family()
    ICON_FAMILY = _pick_font(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
    font = QFont(FONT_FAMILY, 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
    app.setStyle("Fusion")
    app.setStyleSheet(_qss({**COLORS, **_assets(mode)}))


def _assets(mode: str) -> dict:
    """生成 QSS 用的小图（勾选标记、箭头），返回 {占位名: 文件路径}。"""
    folder = os.path.join(tempfile.gettempdir(), "hdm_assets")
    os.makedirs(folder, exist_ok=True)
    muted = COLORS["muted"]
    shapes = {
        "_check": ("#ffffff", [(0.22, 0.52), (0.42, 0.72), (0.78, 0.3)]),
        "_dash": ("#ffffff", [(0.26, 0.5), (0.74, 0.5)]),
        "_up": (muted, [(0.18, 0.66), (0.5, 0.34), (0.82, 0.66)]),
        "_down": (muted, [(0.18, 0.34), (0.5, 0.66), (0.82, 0.34)]),
    }
    out = {}
    for name, (fg, points) in shapes.items():
        path = os.path.join(folder, f"{name[1:]}_{mode}.png")
        size = 32
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(fg), size * 0.11)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(x * size, y * size) for x, y in points])
        painter.end()
        pix.save(path, "PNG")
        out[name] = path.replace("\\", "/")
    return out


def repolish(widget) -> None:
    """动态属性（kind / role）变化后刷新样式。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
