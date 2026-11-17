"""Bridges the Qt-free core to Qt: queued dispatch onto the GUI thread and a background task runner.

* `QtDispatcher` is passed to `EventBus.subscribe(dispatcher=...)` so callbacks run on the GUI thread.
* `TaskRunner` runs blocking workflows (capture, autofocus, scans) on worker threads, one
  instrument task at a time, and reports progress/results back on the GUI thread.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot


class QtDispatcher(QObject):
    _invoke = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._invoke.connect(self._run)  # auto-connection => queued from other threads

    def __call__(self, fn: Callable[[], None]) -> None:
        self._invoke.emit(fn)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


class TaskRunner(QObject):
    started = Signal(str)
    progress = Signal(str, int, int, str)  # name, i, n, message
    finished = Signal(str, object)
    failed = Signal(str, str)
    busy_changed = Signal(bool)
    _deliver = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._deliver.connect(self._run_on_gui)  # queued when emitted from a worker thread
        self._lock = threading.Lock()
        self._current: str | None = None
        self.last_traceback = ""

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> str | None:
        return self._current

    def submit(self, name: str, fn: Callable[..., object], *, on_done: Callable[[object], None] | None = None,
               on_error: Callable[[str], None] | None = None, with_progress: bool = False) -> bool:
        with self._lock:
            if self._current is not None:
                self.failed.emit(name, f"instrument busy with '{self._current}'")
                return False
            self._current = name
        self.busy_changed.emit(True)
        self.started.emit(name)
        disp = self._deliver.emit

        def progress(i: int, n: int, msg: str) -> None:
            self.progress.emit(name, i, n, msg)

        def work() -> None:
            try:
                result = fn(progress) if with_progress else fn()
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                msg = f"{exc}"
                self.last_traceback = traceback.format_exc(limit=4)
                self._done()
                self.failed.emit(name, msg)
                if on_error:
                    disp(lambda: on_error(msg))
                return
            self._done()
            self.finished.emit(name, result)
            if on_done:
                disp(lambda: on_done(result))

        threading.Thread(target=work, daemon=True, name=f"task-{name}").start()
        return True

    @Slot(object)
    def _run_on_gui(self, fn: Callable[[], None]) -> None:
        fn()

    def _done(self) -> None:
        with self._lock:
            self._current = None
        self.busy_changed.emit(False)
