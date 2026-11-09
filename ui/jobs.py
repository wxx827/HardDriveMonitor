"""子进程任务运行器：启动 core.jobs 里的函数，并用定时器把进度消息转成 Qt 信号。"""
from __future__ import annotations

import multiprocessing as mp
import queue

from PySide6.QtCore import QObject, QTimer, Signal

TERMINAL = {"done", "error", "cancelled"}


class ProcessJob(QObject):
    message = Signal(str, object)    # (kind, payload)
    finished = Signal(str)           # 结束类型：done / error / cancelled

    def __init__(self, target, args: tuple, parent=None):
        super().__init__(parent)
        ctx = mp.get_context("spawn")
        self._queue = ctx.Queue()
        self._cancel = ctx.Event()
        self._proc = ctx.Process(target=target, args=(*args, self._queue, self._cancel), daemon=False)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._ended = False

    def start(self) -> None:
        self._proc.start()
        self._timer.start()

    def cancel(self) -> None:
        self._cancel.set()
        QTimer.singleShot(15000, self._kill_if_alive)

    def is_running(self) -> bool:
        return not self._ended

    def _kill_if_alive(self) -> None:
        if not self._ended and self._proc.is_alive():
            self._proc.terminate()

    def _poll(self) -> None:
        latest_progress = None
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    latest_progress = payload
                    continue
                if latest_progress is not None:
                    self.message.emit("progress", latest_progress)
                    latest_progress = None
                self.message.emit(kind, payload)
                if kind in TERMINAL:
                    self._end(kind)
                    return
        except queue.Empty:
            pass
        except (EOFError, OSError):
            pass
        if latest_progress is not None:
            self.message.emit("progress", latest_progress)
        if not self._proc.is_alive():
            if self._cancel.is_set():
                self.message.emit("cancelled", "任务已停止。")
                self._end("cancelled")
            else:
                self.message.emit("error", f"后台进程意外退出（退出码 {self._proc.exitcode}）")
                self._end("error")

    def _end(self, kind: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._timer.stop()
        self._proc.join(timeout=0.5)
        self.finished.emit(kind)
