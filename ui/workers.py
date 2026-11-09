"""后台任务：在线程池里跑查询 / 文件操作，结果回到主线程；输入防抖。"""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot


class _Dispatcher(QObject):
    """住在主线程里的投递器：任何线程 emit 的回调都在主线程执行。"""

    deliver = Signal(object)

    def __init__(self):
        super().__init__()
        self.deliver.connect(self._run)

    @Slot(object)
    def _run(self, payload):
        fn, args = payload
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


_dispatcher: _Dispatcher | None = None
_pool: QThreadPool | None = None


def init() -> None:
    """必须在主线程、QApplication 创建之后调用一次。"""
    global _dispatcher, _pool
    _dispatcher = _Dispatcher()
    _pool = QThreadPool()
    _pool.setMaxThreadCount(4)


def call_in_main(fn: Callable, *args) -> None:
    assert _dispatcher is not None
    _dispatcher.deliver.emit((fn, args))


class _Task(QRunnable):
    def __init__(self, fn, on_done, on_error):
        super().__init__()
        self.fn = fn
        self.on_done = on_done
        self.on_error = on_error
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001
            if self.on_error:
                call_in_main(self.on_error, exc, traceback.format_exc())
            else:
                traceback.print_exc()
            return
        if self.on_done:
            call_in_main(self.on_done, result)


def run_async(fn: Callable[[], object], on_done: Callable | None = None, on_error: Callable | None = None) -> None:
    """在线程池执行 fn()，完成后在主线程调用 on_done(result) / on_error(exc, tb)。"""
    assert _pool is not None
    _pool.start(_Task(fn, on_done, on_error))


class Latest:
    """只接受最新一次请求的结果（快速切换筛选条件时丢弃过期结果）。"""

    def __init__(self):
        self._gen = 0

    def run(self, fn, on_done, on_error=None):
        self._gen += 1
        gen = self._gen

        def done(result):
            if gen == self._gen:
                on_done(result)

        def error(exc, tb):
            if gen == self._gen and on_error:
                on_error(exc, tb)

        run_async(fn, done, error)


class Debouncer(QObject):
    triggered = Signal()

    def __init__(self, msec: int = 300, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(msec)
        self._timer.timeout.connect(self.triggered)

    def poke(self, *_args) -> None:
        self._timer.start()
