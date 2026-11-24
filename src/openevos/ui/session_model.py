"""Qt-side model of a Session: bus events on the GUI thread, live frame polling, task runner,
event log. Pages talk to the instrument only through `self.session.controller` / workflow
services (which go through the command router) and run blocking work via `self.tasks`."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from openevos.acquisition.session import Session
from openevos.core.event_bus import (
    AcquisitionStatusEvent,
    CommandLifecycleEvent,
    DeviceStateChanged,
    HardwareFaultEvent,
    StorageFaultEvent,
)
from openevos.core.models import CommandStatus
from openevos.ui.qt_bridge import QtDispatcher, TaskRunner


class SessionModel(QObject):
    device_changed = Signal(str, object)  # device_id, DeviceSnapshot
    command_event = Signal(object)
    fault = Signal(str)
    log_added = Signal(str, str, str)  # time, level, text
    frame = Signal(object)  # core Frame (preview)
    vessel_changed = Signal()
    buffers_changed = Signal()
    settings_changed = Signal()
    status_message = Signal(str)
    #: an operator action done somewhere the operator cannot see (Quick Save from the Capture page): the text
    #: the Capture page's notice line shows ("ok" level) so the operator knows it happened
    confirmed = Signal(str)

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.dispatcher = QtDispatcher(self)
        self.tasks = TaskRunner(self)
        self.log: deque[tuple[str, str, str]] = deque(maxlen=2000)
        self.active_commands: dict[str, str] = {}
        bus = session.core.bus
        self._subs = [
            bus.subscribe(DeviceStateChanged, self._on_device, dispatcher=self.dispatcher),
            bus.subscribe(CommandLifecycleEvent, self._on_command, dispatcher=self.dispatcher),
            bus.subscribe(HardwareFaultEvent, self._on_fault, dispatcher=self.dispatcher),
            bus.subscribe(StorageFaultEvent, self._on_storage, dispatcher=self.dispatcher),
            bus.subscribe(AcquisitionStatusEvent, self._on_acq, dispatcher=self.dispatcher),
        ]
        session.capture.buffers.listeners.append(lambda: self.dispatcher(self.buffers_changed.emit))
        session.listeners.append(lambda what: self.dispatcher(self.vessel_changed.emit) if what == "vessel" else None)
        self.tasks.failed.connect(lambda name, msg: self.add_log("error", f"{name}: {msg}"))
        self.tasks.started.connect(lambda name: self.add_log("info", f"{name} started"))
        self.tasks.finished.connect(lambda name, _r: self.add_log("info", f"{name} finished"))
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(66)
        self._frame_timer.timeout.connect(self._poll_frame)
        self._frame_timer.start()
        self.frames_shown = 0
        self._stoppers: list = []
        self.idle_checks: list = []

    # ------------------------------------------------------------------ events
    def add_log(self, level: str, text: str) -> None:
        t = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log.append((t, level, text))
        self.log_added.emit(t, level, text)

    def _on_device(self, e: DeviceStateChanged) -> None:
        self.device_changed.emit(e.device_id, e.current)
        prev = e.previous
        if prev is None or prev.connection != e.current.connection or prev.health != e.current.health:
            self.add_log("info" if e.current.health.value == "normal" else "warn",
                         f"{e.device_id}: {e.current.connection.value}/{e.current.health.value}"
                         + (f" — {e.reason}" if e.reason else ""))

    def _on_command(self, e: CommandLifecycleEvent) -> None:
        self.command_event.emit(e)
        if e.status.is_terminal:
            self.active_commands.pop(e.command_id, None)
            if e.status is not CommandStatus.SUCCEEDED:
                level = {CommandStatus.REJECTED: "warn", CommandStatus.CANCELLED: "info"}.get(e.status, "error")
                self.add_log(level,  # a cancel is an operator action (Stop, hold release), not an error
                             f"{e.device_id} command {e.status.value}: {e.message or ''}")
        else:
            self.active_commands[e.command_id] = e.device_id

    def _on_fault(self, e: HardwareFaultEvent) -> None:
        if getattr(e.severity, "value", e.severity) == "warning":  # a refusal before anything moved (APP-07)
            self.add_log("warn", f"{e.device_id}: {e.message}")
            return
        self.add_log("error", f"FAULT {e.device_id}: {e.message}")
        self.fault.emit(f"{e.device_id}: {e.message}")

    def _on_storage(self, e: StorageFaultEvent) -> None:
        self.add_log("error", f"STORAGE {e.acquisition_id}: {e.message}")
        self.fault.emit(e.message)

    def _on_acq(self, e: AcquisitionStatusEvent) -> None:
        if e.state not in ("started", "completed"):
            self.add_log("warn", f"acquisition {e.acquisition_id} {e.state}: {e.message or ''}")

    def _poll_frame(self) -> None:
        f = self.session.core.transport.preview.take_latest()
        if f is not None:
            self.frames_shown += 1
            self.frame.emit(f)

    # ------------------------------------------------------------------ helpers
    def submit(self, ticket_factory, what: str) -> None:
        """Submit a command now (non-blocking). Rejections arrive as lifecycle events / log."""
        try:
            ticket_factory()
        except Exception as exc:  # noqa: BLE001
            self.add_log("error", f"{what}: {exc}")

    def submit_when_idle(self, name: str, fn, *, on_done=None, on_error=None, with_progress: bool = False,
                         _waited: float = 0.0) -> bool:
        """Start a workflow once pending UI commands (sliders, buttons) have settled, so the workflow's commands
        never collide with them on the same device. Every page starts instrument workflows through this."""
        if any(check() for check in self.idle_checks) and _waited < 5.0:
            QTimer.singleShot(30, lambda: self.submit_when_idle(name, fn, on_done=on_done, on_error=on_error,
                                                                with_progress=with_progress, _waited=_waited + 0.03))
            return True
        if self.tasks.busy:
            self.add_log("warn", f"{name}: instrument busy with {self.tasks.current}")
            return False
        return self.tasks.submit(name, fn, on_done=on_done, on_error=on_error, with_progress=with_progress)

    def register_stopper(self, fn) -> None:
        """Pages register callables that stop their workflow (scan/time-lapse runners, autofocus)."""
        self._stoppers.append(fn)

    def stop_all(self) -> int:
        for fn in list(self._stoppers):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.add_log("error", f"stop: {exc}")
        self.session.autofocus.cancel_event.set()
        self.session.capture.stop_zstack()  # a Z-stack stops between slices and keeps them (ADR-0064)
        return self.session.core.router.cancel_all()

    def shutdown(self) -> None:
        self._frame_timer.stop()
        for s in self._subs:
            s.unsubscribe()
