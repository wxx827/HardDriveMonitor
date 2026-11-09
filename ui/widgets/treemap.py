"""Treemap 画布：两层（子目录 + 孙目录）方块图，悬停提示、单击钻取、右键菜单。"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from core import util
from core.treemap import squarify
from .. import theme


@dataclass
class TreeNode:
    path: str
    name: str
    size: int
    files: int = 0
    kind: str = "dir"          # dir / files（本层文件聚合块）
    children: list["TreeNode"] = field(default_factory=list)


@dataclass
class _Hit:
    rect: QRectF
    node: TreeNode
    depth: int


HEADER_H = 20


class TreemapWidget(QWidget):
    activated = Signal(str)                 # 单击目录块
    contextRequested = Signal(str, QPoint)  # 右键目录块（全局坐标）

    def __init__(self, parent=None, depth: int = 2, compact: bool = False):
        super().__init__(parent)
        self._root: TreeNode | None = None
        self._depth = depth
        self._compact = compact
        self._hits: list[_Hit] = []
        self._hover: _Hit | None = None
        self._layout_size = QSize()
        self._message = "暂无数据，先扫描一次"
        self.setMouseTracking(True)
        self.setMinimumSize(200, 140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)

    def set_root(self, root: TreeNode | None, message: str = "暂无数据，先扫描一次") -> None:
        self._root = root
        self._message = message
        self._hover = None
        self._layout_size = QSize()
        self.update()

    # ---------------- 布局 ----------------
    def _relayout(self) -> None:
        self._hits = []
        self._layout_size = self.size()
        root = self._root
        if not root or not root.children:
            return
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        self._layout_children(root.children, area, 1)

    def _layout_children(self, nodes: list[TreeNode], area: QRectF, depth: int) -> None:
        nodes = [n for n in nodes if n.size > 0]
        if not nodes or area.width() < 2 or area.height() < 2:
            return
        rects = squarify([float(n.size) for n in nodes], area.x(), area.y(), area.width(), area.height())
        for node, (x, y, w, h) in zip(nodes, rects):
            if w < 1 or h < 1:
                continue
            rect = QRectF(x, y, w, h)
            self._hits.append(_Hit(rect, node, depth))
            if depth < self._depth and node.children and w > 50 and h > HEADER_H + 24:
                inner = rect.adjusted(3, HEADER_H, -3, -3)
                self._layout_children(node.children, inner, depth + 1)

    # ---------------- 绘制 ----------------
    def paintEvent(self, _event):
        if self._layout_size != self.size():
            self._relayout()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), theme.color("panel"))
        if not self._hits:
            p.setPen(theme.color("muted"))
            p.drawText(self.rect(), Qt.AlignCenter, self._message)
            p.end()
            return
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(12)
        small = QFont(theme.FONT_FAMILY)
        small.setPixelSize(11)
        fm = QFontMetrics(font)
        fm_small = QFontMetrics(small)
        dark = theme.is_dark()
        border = theme.color("panel")
        top_index = 0
        hue = QColor(theme.TREEMAP_HUES[0])
        for hit in self._hits:
            node, rect = hit.node, hit.rect
            if hit.depth == 1:
                hue = QColor("#9aa3b2") if node.kind != "dir" else QColor(
                    theme.TREEMAP_HUES[top_index % len(theme.TREEMAP_HUES)])
                top_index += 1
                has_kids = bool(node.children) and rect.width() > 50 and rect.height() > HEADER_H + 24 \
                    and self._depth > 1
                fill = (hue.darker(160) if dark else hue.lighter(165)) if has_kids else hue
                p.fillRect(rect, fill)
                p.setPen(QPen(border, 2))
                p.drawRect(rect)
                text_color = QColor("#ffffff") if not has_kids or dark else hue.darker(170)
                if rect.width() > 40 and rect.height() > 16:
                    p.setFont(font)
                    p.setPen(text_color)
                    label = f"{node.name}  {util.human_size(node.size)}"
                    if has_kids:
                        box = QRectF(rect.x() + 6, rect.y() + 2, rect.width() - 12, HEADER_H - 2)
                        p.drawText(box, Qt.AlignLeft | Qt.AlignVCenter,
                                   fm.elidedText(label, Qt.ElideRight, int(box.width())))
                    else:
                        self._draw_center_label(p, rect, node, fm, font, fm_small, small, text_color)
            else:
                shade = 110 + (hash(node.path) % 5) * 12
                fill = hue.darker(shade) if not dark else hue.darker(120 + (hash(node.path) % 4) * 15)
                if node.kind != "dir":
                    fill = QColor("#b4bcc8") if not dark else QColor("#4b5260")
                p.fillRect(rect, fill)
                p.setPen(QPen(border, 1))
                p.drawRect(rect)
                if rect.width() > 46 and rect.height() > 18:
                    self._draw_center_label(p, rect, node, fm_small, small, fm_small, small, QColor("#ffffff"))
        if self._hover is not None:
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor("#ffffff") if dark else QColor("#111827"), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._hover.rect.adjusted(1, 1, -1, -1))
        p.end()

    @staticmethod
    def _draw_center_label(p, rect, node, fm, font, fm_small, small, color):
        p.setPen(color)
        width = int(rect.width() - 8)
        name = fm.elidedText(node.name, Qt.ElideMiddle, width)
        if rect.height() > fm.height() + fm_small.height() + 4:
            p.setFont(font)
            top = rect.center().y() - (fm.height() + fm_small.height()) / 2
            p.drawText(QRectF(rect.x() + 4, top, width, fm.height()), Qt.AlignCenter, name)
            p.setFont(small)
            p.drawText(QRectF(rect.x() + 4, top + fm.height(), width, fm_small.height()), Qt.AlignCenter,
                       util.human_size(node.size))
        else:
            p.setFont(font)
            p.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignCenter, name)

    # ---------------- 交互 ----------------
    def _hit_at(self, pos) -> _Hit | None:
        for hit in reversed(self._hits):
            if hit.rect.contains(pos):
                return hit
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit_at(event.position())
        if hit is not self._hover:
            self._hover = hit
            self.update()
        if hit:
            node = hit.node
            share = ""
            if self._root and self._root.size:
                share = f" · 占 {node.size / self._root.size * 100:.1f}%"
            detail = {"files": "直接位于该目录下的文件", "other": "较小的子目录合计"}.get(node.kind, node.path)
            files = f"\n{node.files:,} 个文件" if node.files else ""
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{node.name}\n{util.human_size(node.size)}{share}{files}\n{detail}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, _event):
        self._hover = None
        self.update()

    def mouseReleaseEvent(self, event):
        hit = self._hit_at(event.position())
        if not hit or hit.node.kind != "dir":
            return
        if event.button() == Qt.LeftButton:
            self.activated.emit(hit.node.path)
        elif event.button() == Qt.RightButton:
            self.contextRequested.emit(hit.node.path, event.globalPosition().toPoint())

    def resizeEvent(self, event):
        self._layout_size = QSize()
        super().resizeEvent(event)
