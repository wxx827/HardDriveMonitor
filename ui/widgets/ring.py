"""磁盘占用环形图。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import theme


class DriveRing(QWidget):
    def __init__(self, size: int = 112, parent=None):
        super().__init__(parent)
        self._size = size
        self._pct = 0.0
        self._caption = "已用"
        self.setFixedSize(size, size)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def set_value(self, used_pct: float, caption: str = "已用") -> None:
        self._pct = max(0.0, min(100.0, float(used_pct)))
        self._caption = caption
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        thickness = max(8.0, self._size * 0.1)
        pad = thickness / 2 + 2
        rect = QRectF(pad, pad, self._size - 2 * pad, self._size - 2 * pad)
        track = QPen(theme.color("track"), thickness)
        track.setCapStyle(Qt.FlatCap)
        p.setPen(track)
        p.drawEllipse(rect)
        if self._pct > 0.2:
            pen = QPen(theme.usage_color(self._pct), thickness)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, int(-self._pct * 3.6 * 16))
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(int(self._size * 0.2))
        font.setBold(True)
        p.setFont(font)
        p.setPen(theme.color("text"))
        text_rect = QRectF(0, self._size * 0.28, self._size, self._size * 0.28)
        p.drawText(text_rect, Qt.AlignCenter, f"{self._pct:.0f}%")
        font.setBold(False)
        font.setPixelSize(int(self._size * 0.11))
        p.setFont(font)
        p.setPen(theme.color("muted"))
        p.drawText(QRectF(0, self._size * 0.54, self._size, self._size * 0.16), Qt.AlignCenter, self._caption)
        p.end()
