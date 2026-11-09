"""给 README 截图：用索引副本在后台渲染主窗口各页面，不显示在屏幕上。"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from core.config import Config
from core.index import Index
from ui import theme, workers
from ui.main_window import MainWindow

OUT = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="hdm_shots_")
for name in ("index.db", "index.db-wal", "index.db-shm"):
    src = os.path.join(ROOT, "data", name)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(tmp, name))
# 截图前脱敏：用户名换成 User；HDM_RENAMES="旧目录名=新目录名;..." 可以再替换其它目录名
RENAMES = [("\\Users\\" + os.environ.get("USERNAME", "user"), "\\Users\\User")]
for pair in filter(None, os.environ.get("HDM_RENAMES", "").split(";")):
    old, _, new = pair.partition("=")
    RENAMES.append(("\\" + old, "\\" + new))
KEEP_CATEGORIES = ("临时文件", "缓存与开发", "浏览器缓存", "应用缓存", "回收站", "系统更新", "系统占用（仅说明）")
db = sqlite3.connect(os.path.join(tmp, "index.db"))
for table, cols in {"dirs": ("path", "parent"), "dir_history": ("path",), "big_files": ("path", "dir"),
                    "recent_files": ("path", "dir"), "cleanup_items": ("path", "title", "reason")}.items():
    for old, new in RENAMES:
        db.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = replace({c}, ?, ?)" for c in cols), (old, new) * len(cols))
db.execute(f"DELETE FROM cleanup_items WHERE category NOT IN ({','.join('?' * len(KEEP_CATEGORIES))})", KEEP_CATEGORIES)
db.commit()
db.close()

with open(os.path.join(ROOT, "data", "config.json"), encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["scan"]["watch"] = False
cfg["scan"]["auto_scan"] = False
cfg["app"]["autostart"] = False
cfg["ai"]["api_key_enc"] = ""
with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False)

theme_name = sys.argv[1] if len(sys.argv) > 1 else "light"
app = QApplication(sys.argv)
config = Config(os.path.join(tmp, "config.json"))
theme.apply_theme(app, theme_name)
workers.init()
index = Index(os.path.join(tmp, "index.db"))
win = MainWindow(index=index, config=config, app_dir=ROOT, data_dir=tmp)
win.tray.hide()
win._startup_check = lambda: None
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()

pages = sys.argv[2].split(",") if len(sys.argv) > 2 else ["dashboard", "explorer", "cleanup", "monitor"]
steps = []
for key in pages:
    steps.append(lambda k=key: win.goto(k))
    steps.append(lambda k=key: win.grab().save(os.path.join(OUT, f"{k}{'' if theme_name == 'light' else '_' + theme_name}.png")))


def run(i=0):
    if i >= len(steps):
        win._quitting = True
        index.close()
        app.quit()
        return
    steps[i]()
    QTimer.singleShot(4000, lambda: run(i + 1))


QTimer.singleShot(6000, run)
app.exec()
shutil.rmtree(tmp, ignore_errors=True)
print("saved to", OUT)
