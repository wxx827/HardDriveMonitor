"""表格委托：占比进度条。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from .. import theme

SHARE_ROLE = Qt.UserRole + 1


class ShareBarDelegate(QStyledItemDelegate):
    """在单元格里画占比条，数据取 SHARE_ROLE（0..1）。"""

    def paint(self, painter: QPainter, option, index):
        value = index.data(SHARE_ROLE)
        if value is None:
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, theme.color("selection"))
        rect = option.rect.adjusted(6, 0, -6, 0)
        bar_w = max(30, rect.width() - 48)
        bar = QRectF(rect.x(), rect.center().y() - 3, bar_w, 6)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color("track"))
        painter.drawRoundedRect(bar, 3, 3)
        fill = QRectF(bar.x(), bar.y(), max(2.0, bar.width() * max(0.0, min(1.0, float(value)))), bar.height())
        painter.setBrush(theme.color("accent"))
        painter.drawRoundedRect(fill, 3, 3)
        painter.setPen(theme.color("muted"))
        painter.drawText(QRectF(bar.right() + 4, rect.y(), rect.right() - bar.right() - 4, rect.height()),
                         Qt.AlignRight | Qt.AlignVCenter, f"{float(value) * 100:.1f}%")
        painter.restore()
