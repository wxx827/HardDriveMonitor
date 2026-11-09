"""页面基类：可选滚动容器 + 统一边距。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget


class Page(QWidget):
    def __init__(self, win, scroll: bool = False):
        super().__init__(win)
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll_area = None
        if scroll:
            area = self.scroll_area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            area.setWidget(inner)
            outer.addWidget(area)
            self.body = QVBoxLayout(inner)
            self.body.setContentsMargins(0, 0, 6, 0)
        else:
            self.body = outer
        self.body.setSpacing(12)

    def refresh(self) -> None:
        pass

    def selected_records(self) -> list[dict]:
        return []
