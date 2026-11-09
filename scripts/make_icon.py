import os
import struct
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtWidgets import QApplication

from ui.widgets.common import app_icon

app = QApplication([])
icon = app_icon()
sizes = (16, 24, 32, 48, 64, 128, 256)
blobs = []
for s in sizes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    icon.pixmap(QSize(s, s)).toImage().save(buf, "PNG")
    blobs.append(bytes(ba))

out = os.path.join(os.path.dirname(__file__), "..", "build", "app.ico")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "wb") as fh:
    fh.write(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, data in zip(sizes, blobs):
        fh.write(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for data in blobs:
        fh.write(data)
print(out)
