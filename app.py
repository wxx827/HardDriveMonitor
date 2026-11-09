"""硬盘监视器 — 启动入口。

双击 启动.bat，或执行 `python app.py`。依赖 PySide6（pip install -r requirements.txt）。
参数：--minimized 只在托盘启动（开机自启用）；--replace 等待旧实例退出后接管（以管理员重启用）。
"""
from __future__ import annotations

import ctypes
import multiprocessing
import os
import sys
import time
import traceback


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
MUTEX_NAME = "Local\\HardDriveMonitor_SingleInstance"
SERVER_NAME = "HardDriveMonitor_" + (os.environ.get("USERNAME") or "user")


def _log(text: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def _fix_stdio() -> None:
    """pythonw 下 stdout/stderr 为 None，任何 print / traceback 都会出错。"""
    if sys.stdout is None or sys.stderr is None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def _excepthook(exc_type, exc, tb) -> None:
    _log("未捕获的异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


def _acquire_mutex(wait_seconds: float = 0.0):
    """返回 (handle, ok)。ok=False 表示已有实例在运行。"""
    kernel32 = ctypes.windll.kernel32
    deadline = time.time() + wait_seconds
    while True:
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return handle, True
        kernel32.CloseHandle(handle)
        if time.time() >= deadline:
            return None, False
        time.sleep(0.3)


def _notify_running_instance() -> bool:
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(1500):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _start_server(win):
    from PySide6.QtNetwork import QLocalServer

    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer(win)

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.waitForReadyRead(500)
            conn.readAll()
            conn.disconnectFromServer()
            win.show_normal()

    server.newConnection.connect(on_connection)
    server.listen(SERVER_NAME)
    return server


def _show_error(title: str, message: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, title, message)
    except Exception:  # noqa: BLE001
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)


def main() -> int:
    _fix_stdio()
    sys.excepthook = _excepthook
    os.makedirs(DATA_DIR, exist_ok=True)
    args = sys.argv[1:]
    minimized = "--minimized" in args
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HardDriveMonitor.App")
    except (AttributeError, OSError):
        pass

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        ctypes.windll.user32.MessageBoxW(
            None, "缺少 PySide6。请在命令行执行：\n\npip install -r requirements.txt\n\n然后重新启动。", "硬盘监视器", 0x10)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("硬盘监视器")
    app.setQuitOnLastWindowClosed(False)

    _mutex, ok = _acquire_mutex(10.0 if "--replace" in args else 0.0)
    if not ok:
        if not _notify_running_instance():
            _show_error("硬盘监视器", "程序已经在运行了（可以在任务栏右下角的托盘里找到它）。")
        return 0

    try:
        from core.config import Config
        from core.index import Index
        from ui import theme, workers
        from ui.main_window import MainWindow

        config = Config(os.path.join(DATA_DIR, "config.json"))
        theme.apply_theme(app, config.app.get("theme") or "light")
        workers.init()
        index = Index(os.path.join(DATA_DIR, "index.db"))
        win = MainWindow(index=index, config=config, app_dir=BASE_DIR, data_dir=DATA_DIR)
        _server = _start_server(win)
        if minimized and win.tray.available():
            win.hide()
        else:
            win.show()
        code = app.exec()
        index.close()
        return code
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        _log("启动失败：\n" + detail)
        _show_error("启动失败", "程序启动失败，详情已写入 data/app.log：\n\n" + detail[-1500:])
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
