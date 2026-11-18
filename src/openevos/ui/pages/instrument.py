"""Instrument and Manage workspaces: Hardware, Environment, Calibration, Settings, Service, Event log."""

from __future__ import annotations

import json
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openevos import __version__
from openevos.instrument.profile import CameraRole
from openevos.instrument.readiness import PHYSICAL_READINESS, Readiness
from openevos.instrument.settings import GasPort1, HotButtonAction, ImageFormat
from openevos.ui import theme
from openevos.ui.common import empty_hint, hbox, label, panel
from openevos.ui.widgets.param import ParamRow


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    return t


def _set_row(t: QTableWidget, r: int, values, colors: dict[int, str] | None = None) -> None:
    for c, v in enumerate(values):
        it = QTableWidgetItem(str(v))
        if colors and c in colors:
            it.setForeground(QColor(colors[c]))
        t.setItem(r, c, it)


# ====================================================================================== Hardware
PHYSICAL_STEPS_MM = (0.01, 0.1, 1.0, 5.0, 10.0, 25.0)
#: the drives' power-up speed (Gate 5: ~8.3 mm/s); used only to size the router timeout of a long move
PHYSICAL_SPEED_MM_S = 8.27


def _swatch(color: str, size: int = 16):
    """A rounded colour chip with a hairline, as a button icon (visible for light pseudo colours too)."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 4, 4)
    p.end()
    return QIcon(pm)


class HardwarePage(QWidget):
    # ------------------------------------------------------------------ physical X/Y stage (ADR-0024)
    def _physical_stage_panel(self):
        p, pl = panel("Physical X/Y stage (real instrument)")
        self.model.physical_control = self  # the Capture joystick drives the stage through the same path
        svc = self.session.telemetry[0] if self.session.telemetry else None
        box = getattr(svc, "travel_box_um", {"x": (0, 22000), "y": (0, 22000)})
        self._phys_box = box
        note = getattr(svc, "travel_box_note", "power-up box")
        pl.addWidget(label(f"Moves at the drives' power-up speed, inside X 0–{box['x'][1] / 1000:.1f} mm, "
                           f"Y 0–{box['y'][1] / 1000:.1f} mm ({note}), through the router's safety checks; each move "
                           "is verified by the encoder and audited. X and Y move one at a time, and a drift monitor "
                           "watches both encoders: if one changes without a command, or a drive records a trip, the "
                           "stage locks until a power cycle re-homes it (ADR-0040). The drives must hold matching "
                           "limits (Gate 7 after power-on), else far moves are refused. +X = right, +Y = away from "
                           "you. Stop all (Esc) also stops the stage.",
                           "muted", wrap=True))
        # step jog: one click = one step of the chosen size
        self.phys_step = QComboBox()
        self.phys_step.addItems([f"{s:g} mm" for s in PHYSICAL_STEPS_MM])
        self.phys_step.setCurrentIndex(1)
        grid = QGridLayout()
        grid.setSpacing(6)
        dirs = {"up": ("▲ Y+ (away)", 0, 1), "left": ("◀ X−", -1, 0), "right": ("X+ ▶", 1, 0),
                "down": ("▼ Y− (toward you)", 0, -1)}
        cells = {"up": (0, 1), "left": (1, 0), "right": (1, 2), "down": (2, 1)}
        for key, (text, dx, dy) in dirs.items():
            b = QPushButton(text)
            b.setMinimumSize(150, 40)
            b.clicked.connect(lambda _=False, x=dx, y=dy: self._phys_jog(x, y))
            grid.addWidget(b, *cells[key])
        stop = QPushButton("■ Stop stage")
        stop.setObjectName("danger")
        stop.setMinimumSize(150, 40)
        stop.clicked.connect(self._phys_stop)
        grid.addWidget(stop, 1, 1)
        jog, jl = panel("Step jog")
        jl.addWidget(hbox(label("Step", "muted"), self.phys_step, None))
        jl.addLayout(grid)
        # hold to move: press = move toward the edge of the travel box, release = controlled stop
        hgrid = QGridLayout()
        hgrid.setSpacing(6)
        self.phys_hold_buttons = {}
        for key, (text, dx, dy) in dirs.items():
            b = QPushButton(f"hold {text}")
            b.setMinimumSize(150, 40)
            b.setAutoRepeat(False)
            b.pressed.connect(lambda x=dx, y=dy: self._phys_hold_start(x, y))
            b.released.connect(self._phys_hold_end)
            hgrid.addWidget(b, *cells[key])
            self.phys_hold_buttons[key] = b
        hold, hl = panel("Hold to move")
        hl.addWidget(label("Press and hold: the stage runs toward the edge of the travel box; release to stop "
                           "(controlled stop, not latched).", "muted", wrap=True))
        hl.addLayout(hgrid)
        # go to: absolute target in mm
        self.phys_goto_x, self.phys_goto_y = QDoubleSpinBox(), QDoubleSpinBox()
        for sb, ax in ((self.phys_goto_x, "x"), (self.phys_goto_y, "y")):
            sb.setDecimals(3)
            sb.setSingleStep(0.1)
            sb.setRange(box[ax][0] / 1000.0, box[ax][1] / 1000.0)
            sb.setSuffix(" mm")
            sb.setMinimumWidth(130)
        b_go = QPushButton("Go to")
        b_go.clicked.connect(self._phys_goto)
        b_here = QPushButton("Use current")
        b_here.clicked.connect(self._phys_goto_fill)
        b_home = QPushButton("Go to 0, 0")
        b_home.clicked.connect(lambda: self._phys_move_to(0.0, 0.0, "go to 0, 0"))
        go, gl = panel("Go to position")
        gl.addWidget(hbox(label("X", "muted"), self.phys_goto_x, label("Y", "muted"), self.phys_goto_y, None))
        gl.addWidget(hbox(b_go, b_here, b_home, None))
        self.phys_pos = label("—", "mono")
        self.phys_msg = label("Ready.", "muted", wrap=True)
        pl.addWidget(self.phys_pos)
        pl.addWidget(self.phys_msg)
        pl.addWidget(hbox(jog, hold, go))
        return p

    def _phys_reach(self) -> dict[str, tuple[float, float]]:
        """Where moves can actually go: the travel box intersected with the limits the drives hold right now
        (power-up 0..22 mm until Gate 7 loads the calibrated ones). Units: um."""
        box = dict(self._phys_box)
        snap = self.session.core.store.get(self.session.profile.stage_device)
        lim = snap.attr("limits_mm") if snap else None
        if lim:
            for ax, (lo, hi) in zip(("x", "y"), lim, strict=False):
                box[ax] = (max(box[ax][0], lo * 1000.0), min(box[ax][1], hi * 1000.0))
        return box

    def _phys_status(self, text: str, level: str = "info") -> None:
        if getattr(self, "phys_msg", None) is not None:
            self.phys_msg.setText(text)
            self.phys_msg.setStyleSheet("" if level == "info" else f"color: {theme.FAULT};")
        self.model.add_log(level, f"physical stage: {text}")

    def _phys_cur(self) -> tuple[float, float] | None:
        """Current X/Y in um from the stage backend's own read-back (refreshed from the encoders at the end of
        every move, stop or cancel), not the slower telemetry snapshot. A stale value here would be sent as
        the target of the axis the operator did not touch and move it back (seen 2026-11-13)."""
        rec = self.session.core.devices.get(self.session.profile.stage_device)
        read = getattr(getattr(rec, "backend", None), "read_position", None)
        pos = read() if read else None
        if pos is not None and {"x", "y"} <= set(pos.axes):
            return pos.get("x"), pos.get("y")
        return self.session.controller.stage_xy()

    def _phys_busy(self) -> bool:
        t = getattr(self, "_phys_ticket", None)
        return t is not None and not t.status.is_terminal

    def _phys_move_to(self, x_um: float, y_um: float, what: str) -> None:
        """Absolute move with a timeout sized to the distance (the backend verifies and audits)."""
        if self._phys_busy():
            self._phys_status(f"{what} ignored: the previous move is still running or stopping", "warn")
            return
        reach = self._phys_reach()
        for ax, v in (("x", x_um), ("y", y_um)):
            lo, hi = reach[ax]
            if not lo - 0.5 <= v <= hi + 0.5:
                self._phys_status(f"{what} refused: {ax.upper()} {v / 1000:.3f} mm is outside what the drives allow "
                                  f"now ({lo / 1000:.3f}–{hi / 1000:.3f} mm; run Gate 7 after power-on for the full "
                                  "range)", "warn")
                return
        cur = self._phys_cur()
        # X and Y run one after the other (ADR-0040), at the selected speed when speed control is on (ADR-0032)
        dist_mm = 0.0 if cur is None else (abs(x_um - cur[0]) + abs(y_um - cur[1])) / 1000.0
        svc = self.session.telemetry[0] if self.session.telemetry else None
        speed = min(PHYSICAL_SPEED_MM_S, float(getattr(svc, "speed_mm_s", PHYSICAL_SPEED_MM_S)))
        try:
            ticket = self.session.controller.move_stage(x_um, y_um,
                                                        timeout_s=dist_mm / speed * 1.2 + 10.0)
        except Exception as exc:  # noqa: BLE001 - refused before submission
            self._phys_status(f"{what} refused: {exc}", "warn")
            return
        self._phys_ticket = ticket
        if ticket.status.value == "rejected":
            self._phys_status(f"{what} rejected: {ticket.record.message}", "warn")
        else:
            self._phys_status(f"{what} → X {x_um / 1000:.3f}, Y {y_um / 1000:.3f} mm")

    def _phys_goto(self) -> None:
        self._phys_move_to(self.phys_goto_x.value() * 1000.0, self.phys_goto_y.value() * 1000.0, "go to")

    def _phys_goto_fill(self) -> None:
        xy = self._phys_cur()
        if xy is not None:
            self.phys_goto_x.setValue(xy[0] / 1000.0)
            self.phys_goto_y.setValue(xy[1] / 1000.0)

    def _phys_hold_start(self, sx: int, sy: int) -> None:
        """Start a hold move; if the previous move is still stopping, queue it (up to 2 s) instead of refusing."""
        snap = self.session.core.store.get(self.session.profile.stage_device)
        stopping = self._phys_busy() or (snap is not None and snap.activity.value == "moving")
        self._phys_holding = True
        if stopping:
            self._phys_pending = (sx, sy, time.monotonic() + 2.0)
            if getattr(self, "_phys_pending_timer", None) is None:
                self._phys_pending_timer = QTimer(self)
                self._phys_pending_timer.setInterval(40)
                self._phys_pending_timer.timeout.connect(self._phys_try_pending)
            self._phys_pending_timer.start()
            self._phys_status("waiting for the previous move to stop…")
            return
        self._phys_hold_go(sx, sy)

    def _phys_try_pending(self) -> None:
        p = getattr(self, "_phys_pending", None)
        if p is None or not self._phys_holding:
            self._phys_pending = None
            self._phys_pending_timer.stop()
            return
        snap = self.session.core.store.get(self.session.profile.stage_device)
        if not self._phys_busy() and (snap is None or snap.activity.value != "moving"):
            self._phys_pending = None
            self._phys_pending_timer.stop()
            self._phys_hold_go(p[0], p[1])
        elif time.monotonic() > p[2]:
            self._phys_pending = None
            self._phys_pending_timer.stop()
            self._phys_status("hold ignored: the stage did not come to rest within 2 s", "warn")

    def _phys_hold_go(self, sx: int, sy: int) -> None:
        cur = self._phys_cur()
        if cur is None:
            self.model.add_log("warn", "physical stage hold refused: position unknown")
            return
        box = self._phys_reach()
        x = cur[0] if sx == 0 else (box["x"][1] if sx > 0 else box["x"][0])
        y = cur[1] if sy == 0 else (box["y"][1] if sy > 0 else box["y"][0])
        self._phys_holding = True
        self._phys_move_to(x, y, "hold")

    def _phys_hold_end(self) -> None:
        if getattr(self, "_phys_holding", False):
            self._phys_holding = False
            if getattr(self, "_phys_pending", None) is not None:  # released before it started: just drop it
                self._phys_pending = None
                self._phys_status("hold cancelled before it started")
                return
            self._phys_stop()

    def _phys_jog(self, sx: int, sy: int) -> None:
        mm = PHYSICAL_STEPS_MM[self.phys_step.currentIndex()]
        cur = self._phys_cur()
        if cur is None:
            self._phys_status("jog refused: stage position unknown", "warn")
            return
        self._phys_move_to(cur[0] + sx * mm * 1000.0, cur[1] + sy * mm * 1000.0, f"jog {sx * mm:+g}, {sy * mm:+g} mm")

    def _phys_stop(self) -> None:
        self.session.core.router.stop_device(self.session.profile.stage_device)
        self.model.add_log("info", "physical stage: controlled stop sent")

    def _refresh_physical(self) -> None:
        if getattr(self, "phys_pos", None) is not None:
            xy = self.session.controller.stage_xy()
            snap = self.session.core.store.get(self.session.profile.stage_device)
            act = snap.activity.value if snap else "unknown"
            self.phys_pos.setText("—" if xy is None else f"X {xy[0] / 1000:8.3f} mm   Y {xy[1] / 1000:8.3f} mm   "
                                                         f"({act})")
            # the drift monitor (ADR-0040): a lock stays on screen, in red, until X and Y are seen to re-home
            lock = snap.attr("drift_lock_reason") if snap else ""
            warn = snap.attr("drift_warnings") if snap else ()
            shown = getattr(self, "_phys_lock_shown", "")
            text = lock or ("; ".join(warn) if warn else "")
            if text != shown:
                self._phys_lock_shown = text
                if text:
                    self.phys_msg.setText(("Stage locked: " if lock else "Warning: ") + text)
                    self.phys_msg.setStyleSheet(f"color: {theme.FAULT};")
                    self.model.add_log("warn", f"physical stage: {text}")
                elif shown:
                    self._phys_status("Drift lock cleared: X and Y were seen to re-home.")
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        # one "Devices" panel (UI audit 2026-11-17): the device table, then the light/camera readback under it, with
        # columns that never vary hidden and the explanations in tooltips
        f, fl = panel("Devices")
        self.devices = _table(["Device", "Kind", "Backend", "Connection", "Health", "Activity", "Position",
                               "Capability source", "Recovery"])
        self.devices.setToolTip("Live state from the runtime state store. Capability source SIMULATED means simulator "
                                "values only, never EVOS specifications.")
        self.devices.setMinimumHeight(260)
        fl.addWidget(self.devices)
        self.dev_combo = QComboBox()
        b_rec = QPushButton("Recover device")
        b_rec.setToolTip("Stop, re-read state and clear 'recovery required' (operator action)")
        fl.addWidget(hbox(label("Selected", "muted"), self.dev_combo, b_rec, None))
        fl.addWidget(label("Light and cameras", "section"))
        self.lightcam = _table(["Device", "Role", "Backend", "Connection", "State", "Value", "Device range"])
        self.lightcam.setToolTip("What the LED board and each camera report now, with the ranges the command router "
                                 "accepts. Change them on the Capture page; every change is a validated, read-back "
                                 "command.")
        self.lightcam.setMinimumHeight(120)
        fl.addWidget(self.lightcam)
        # developer tools (View › Developer tools): simulator fault injection
        self.dev_widgets: list[QWidget] = []
        if self.session.simulated:
            b_fail = QPushButton("Fail next stage move")
            b_stall = QPushButton("Stall next focus move (→ timeout)")
            b_off = QPushButton("Pull mono camera cable")
            inj = hbox(label("Simulator fault injection", "section"), b_fail, b_stall, b_off, None)
            fl.addWidget(inj)
            self.dev_widgets.append(inj)
            rig = self.session.rig
            b_fail.clicked.connect(lambda: setattr(rig.stage, "fail_next_move", "simulated encoder fault"))
            b_stall.clicked.connect(lambda: setattr(rig.focus, "stall_next_move", True))
            b_off.clicked.connect(lambda: rig.camera(CameraRole.MONO).simulate_unexpected_disconnect())
        lay.addWidget(f)
        if getattr(self.session, "physical_stage", False):
            lay.addWidget(self._physical_stage_panel())
        g, gl = panel("Physical unit readiness (gate evidence)")
        self.dev_widgets.append(g)
        gl.addWidget(label("What the project's EVOS FL Auto has been shown to do on the instrument, from the gate records "  # noqa: E501
                           "(ADR-0011..0024; each row cites its evidence). 'Read-only' = state verified, never commanded. "  # noqa: E501
                           "Physical commands still require verified capability records with evidence IDs.", "muted", wrap=True))  # noqa: E501
        rt = _table(["Subsystem", "Axis", "Readiness", "Known", "Gating in OpenEVOS", "Unlock requires", "Evidence"])
        rt.setWordWrap(True)
        colors = {Readiness.VERIFIED_LIMITED: theme.OK, Readiness.READ_ONLY: theme.ACCENT_TX, Readiness.LOCKED: theme.WARN,  # noqa: E501
                  Readiness.NOT_INTEGRATED: theme.UNKNOWN, Readiness.UNAVAILABLE: theme.UNKNOWN}
        for r in PHYSICAL_READINESS:
            i = rt.rowCount()
            rt.insertRow(i)
            _set_row(rt, i, (r.subsystem, r.axis or "", r.readiness.label, r.known, r.gating, r.unlock_requires,
                              r.source),
                     {2: colors.get(r.readiness, theme.UNKNOWN)})
        rt.resizeRowsToContents()
        rt.setMinimumHeight(380)
        gl.addWidget(rt)
        lay.addWidget(g)
        lay.addStretch(1)
        b_rec.clicked.connect(self._recover)
        model.device_changed.connect(lambda *_: self.refresh())
        model.command_event.connect(lambda _e: self._refresh_lightcam())  # exposure/gain emit no device event
        self.set_developer(self.session.settings.basic.developer_mode)
        self.refresh()

    def set_developer(self, on: bool) -> None:
        """View › Developer tools: show the simulator fault injection and the gate-evidence table."""
        for w in self.dev_widgets:
            w.setVisible(on)

    @staticmethod
    def _hide_constant_columns(t, keep=(0,)) -> None:
        """Hide a column whose every row says the same (e.g. Backend 'simulated' in a simulator session)."""
        for c in range(t.columnCount()):
            vals = {t.item(r, c).text() if t.item(r, c) else "" for r in range(t.rowCount())}
            t.setColumnHidden(c, c not in keep and t.rowCount() > 1 and len(vals) == 1)

    def lightcam_rows(self) -> list[tuple[str, ...]]:
        """Rows of the light/camera readback table (Qt-free data; also used by the tests)."""
        from openevos.ui import device_state as ds

        rows: list[tuple[str, ...]] = []
        light = ds.read_light(self.session)
        if light is not None:
            chp = self.session.profile.channel(light.channel) if light.channel else None
            rng = ds.light_limits(self.session, light.channel) if light.channel else None
            chans = ", ".join(ds.light_channels(self.session))
            rows.append((light.device_id, "LED + carriage", self._backend(light.device_id), light.connection,
                         light.summary(chp.display if chp else None),
                         f"emitting: {'yes' if light.emitting else 'no' if light.emitting is not None else '?'}",
                         (f"{rng[0]:g}–{rng[1]:g} % · " if rng else "") + f"channels {chans}"))
        for cam in self.session.profile.cameras:
            rb = ds.read_camera(self.session, cam.role)
            if rb is None:
                continue
            lim = ds.camera_limits(self.session, cam.role)
            res = f"{rb.width}×{rb.height}" if rb.width and rb.height else "size ?"
            rows.append((rb.device_id, f"{cam.role.value} camera", self._backend(rb.device_id), rb.connection,
                         rb.summary(), f"{res} · frames {rb.frames if rb.frames is not None else '?'}",
                         (f"exp {lim.exposure_ms[0]:g}–{lim.exposure_ms[1]:g} ms · gain "
                          + (f"{lim.gain[0]:g}× only" if lim.gain_fixed else f"{lim.gain[0]:g}–{lim.gain[1]:g}×"))
                         if lim else "?"))
        return rows

    def _backend(self, device_id: str) -> str:
        s = self.session.core.store.get(device_id)
        return s.backend_kind.value if s else "?"

    def _refresh_lightcam(self) -> None:
        rows = self.lightcam_rows()
        self.lightcam.setRowCount(len(rows))
        for r, values in enumerate(rows):
            _set_row(self.lightcam, r, values, {3: theme.OK if values[3] == "connected" else theme.WARN})

    def refresh(self) -> None:
        self._refresh_lightcam()
        self._refresh_physical()
        core = self.session.core
        ids = core.devices.device_ids()
        if self.dev_combo.count() != len(ids):
            self.dev_combo.clear()
            self.dev_combo.addItems(ids)
        self.devices.setRowCount(len(ids))
        for r, did in enumerate(ids):
            s = core.store.get(did)
            rec = core.devices.get(did)
            pos = ", ".join(f"{a}={v:.1f}" for a, v in zip(s.observed_position.axes, s.observed_position.values,
                                                                          strict=True)) \
                if s and s.observed_position else "—"
            health_c = theme.OK if s and s.health.value == "normal" else theme.FAULT
            _set_row(self.devices, r, (did, s.kind.value, s.backend_kind.value, s.connection.value, s.health.value,
                                       s.activity.value, pos, rec.capabilities.source.value if rec else "?",
                                       "REQUIRED" if s.recovery_required else "—"),
                     {4: health_c, 8: theme.FAULT if s.recovery_required else theme.MUTED})
        self._hide_constant_columns(self.devices)
        self._hide_constant_columns(self.lightcam)

    def _recover(self) -> None:
        did = self.dev_combo.currentText()
        if did:
            try:
                self.session.core.router.recover_device(did)
                self.model.add_log("info", f"{did}: recovery requested by operator")
            except Exception as exc:  # noqa: BLE001
                self.model.add_log("error", f"recover {did}: {exc}")


# ====================================================================================== Environment
class EnvironmentPage(QWidget):
    """Onstage incubator (manual p.60-70). Setpoints vs measured: measured values are shown as
    'Unavailable' because no telemetry path exists (bring-up record)."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        f, fl = panel("Onstage incubator")
        fl.addWidget(label("CONTROL AND TELEMETRY UNAVAILABLE — no OpenEVOS path to the AMC1000 controller yet. "
                           "Setpoints entered in time-lapse scenes are recorded with the routine only.", "warn",
                           wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        for c, h in enumerate(("Parameter", "Setpoint (routine)", "Measured", "Source")):
            grid.addWidget(label(h, "section"), 0, c)
        rows = [("Temperature", "—", "Unavailable", "no telemetry"), ("CO₂", "—", "Unavailable", "no telemetry"),
                ("O₂", "—", "Unavailable", "no telemetry"), ("Humidity", "—", "Unavailable", "no telemetry")]
        for r, row in enumerate(rows, 1):
            for c, v in enumerate(row):
                grid.addWidget(label(v, "value" if c == 0 else ("muted" if c == 2 else "")), r, c)
        fl.addLayout(grid)
        lay.addWidget(f)
        g, gl = panel("Configuration (System > Basic)")
        form = QFormLayout()
        b = self.session.settings.basic
        self.port1 = QComboBox()
        for p in GasPort1:
            self.port1.addItem({"premix": "Premix", "air": "Air", "custom": "Custom"}[p.value], p)
        self.port1.setCurrentIndex(self.port1.findData(b.gas.port1))
        self.p2 = QCheckBox("Port 2: N₂ connected")
        self.p2.setChecked(b.gas.port2_n2_connected)
        self.p3 = QCheckBox("Port 3: CO₂ connected")
        self.p3.setChecked(b.gas.port3_co2_connected)
        self.offset = QDoubleSpinBox(minimum=-5, maximum=5, decimals=1, suffix=" °C", value=b.temperature_offset_c)
        form.addRow("Port 1", self.port1)
        form.addRow("", self.p2)
        form.addRow("", self.p3)
        form.addRow("Temperature offset", self.offset)
        gl.addLayout(form)
        cal = QPushButton("Calibrate Oxygen Sensor (~4 min)")
        cal.setEnabled(False)
        cal.setToolTip("Requires the incubator control path; unavailable")
        gl.addWidget(cal, 0, Qt.AlignLeft)
        gl.addWidget(label("Configuration is stored in settings; it is not sent to any controller.", "muted"))
        lay.addWidget(g)
        lay.addStretch(1)
        self.port1.currentIndexChanged.connect(self._store)
        for w in (self.p2, self.p3):
            w.toggled.connect(self._store)
        self.offset.valueChanged.connect(self._store)

    def _store(self) -> None:
        b = self.session.settings.basic
        b.gas.port1 = GasPort1(self.port1.currentData())
        b.gas.port2_n2_connected = self.p2.isChecked()
        b.gas.port3_co2_connected = self.p3.isChecked()
        b.temperature_offset_c = self.offset.value()


# ====================================================================================== Calibration
class CalibrationPage(QWidget):
    """Calibration center (manual p.40-50): every procedure runs through the command router on the simulator,
    shows its result against the nominal value, and is applied (pixel size, parfocal/parcentric compensation,
    vessel offset, hot-pixel correction). On the physical backend these stay disabled until the turret and
    camera gates are passed (Hardware page)."""

    PROCS: ClassVar[list[tuple[str, str, str, str]]] = [
        ("pixel_size", "Objective field of view (pixel size)", "p.47-49",
         "Image, move the stage a known distance, image again; phase correlation gives µm per pixel."),
        ("parfocality", "Parfocality", "p.47-49", "Auto fine on every objective; Z offsets vs the lowest magnification."),  # noqa: E501
        ("parcentration", "Parcentration", "p.47-49", "Register the same field on every objective; XY offsets."),
        ("vessel_alignment", "Vessel alignment", "p.50",
         "Find the edges of two wells in brightfield at low magnification; offset of the vessel on the stage."),
        ("hot_pixels", "FL hot pixel correction", "p.43", "Dark frames (light off) → defect map; captures corrected."),
    ]

    def __init__(self, model, capture_page, parent=None):
        super().__init__(parent)
        self.model, self.cap = model, capture_page
        self.session = model.session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        f, fl = panel("Calibration center")
        hw_ok = self.session.simulated
        fl.addWidget(label("Runs on the simulator. On the physical unit these procedures unlock only after the "
                           "objective turret (O axis) and camera gates are passed." if hw_ok else
                           "Locked on this backend — see Hardware for the gates.", "caption" if hw_ok else "warn",
                           wrap=True))
        self.table = _table(["Procedure", "Manual", "Last run", "Result"])
        self.table.setWordWrap(True)
        self.table.setMinimumHeight(220)
        fl.addWidget(self.table)
        row = QHBoxLayout()
        self.buttons = {}
        for key, title, _pg, tip in self.PROCS:
            b = QPushButton(title.split(" (")[0])
            b.setToolTip(tip)
            b.setEnabled(hw_ok)
            b.clicked.connect(lambda _=False, k=key: self.run(k))
            self.buttons[key] = b
            row.addWidget(b)
        row.addStretch(1)
        fl.addLayout(row)
        self.chk_hot = QCheckBox("Apply hot pixel correction to captures")
        self.chk_hot.setChecked(self.session.calibration.hot_pixel_correction)
        self.chk_hot.toggled.connect(lambda on: setattr(self.session.calibration, "hot_pixel_correction", on))
        b_clear = QPushButton("Clear all calibrations")
        b_clear.setObjectName("danger")
        b_clear.clicked.connect(self._clear)
        fl.addWidget(hbox(self.chk_hot, None, b_clear))
        self.lbl = label("", "caption", wrap=True)
        fl.addWidget(self.lbl)
        lay.addWidget(f)
        g, gl = panel("Focus and vessel")
        self.lbl_nominal = label("", "value")
        b_nom = QPushButton("Capture focus nominal (current vessel, current Z)")
        gl.addWidget(self.lbl_nominal)
        gl.addWidget(b_nom, 0, Qt.AlignLeft)
        self.rng = ParamRow("Auto fine range", 25, 400, self.session.settings.basic.auto_fine_range_percent,
                            unit="%", default=100)
        self.lbl_rng = label("", "caption")
        gl.addWidget(self.rng)
        gl.addWidget(self.lbl_rng)
        self.lbl_align = label("", "caption")
        gl.addWidget(self.lbl_align)
        lay.addWidget(g)
        lay.addStretch(1)
        b_nom.clicked.connect(lambda: self.cap.run_action(HotButtonAction.CAPTURE_FOCUS_NOMINAL))
        self.rng.valueChanged.connect(self._rng)
        model.vessel_changed.connect(self._refresh)
        model.device_changed.connect(lambda *_: None)
        self._rng(self.rng.value())
        self._refresh()

    # ------------------------------------------------------------------ procedures
    def run(self, key: str, *, on_done=None) -> bool:
        from openevos.acquisition import calibration as C

        s, ctl = self.session, self.session.controller
        role, prof = s.settings.active_camera, s.profile
        cal = s.calibration
        slots = [o.slot for o in prof.objectives if o.installed]

        def work(progress):
            if key == "pixel_size":
                out = {}
                start = ctl.objective_slot()
                for i, sl in enumerate(slots):
                    progress(i, len(slots), f"objective slot {sl}")
                    out[sl] = C.measure_pixel_size(ctl, prof, role, sl)
                if start:
                    ctl.run(ctl.set_objective(start))
                cal.pixel_size_um[role.value] = {sl: r.um_per_px for sl, r in out.items()}
                cal.touch("pixel_size")
                return "; ".join(f"slot {sl}: {r.um_per_px:.4f} µm/px ({r.deviation_pct:+.2f} % vs nominal)"
                                 for sl, r in out.items())
            if key == "parfocality":
                r = C.measure_parfocality(ctl, prof, s.autofocus, role, slots, progress)
                cal.parfocal_um = dict(r.offsets_um)
                cal.touch("parfocality")
                return "; ".join(f"slot {sl}: {dz:+.2f} µm" for sl, dz in r.offsets_um.items()) + \
                    (f" · failed: {r.failed}" if r.failed else "")
            if key == "parcentration":
                px = {sl: s.pixel_size_um(role, sl) for sl in slots}
                r = C.measure_parcentration(ctl, prof, role, px, slots, progress)
                cal.parcentric_um = dict(r.offsets_um)
                cal.touch("parcentration")
                return "; ".join(f"slot {sl}: ({dx:+.1f}, {dy:+.1f}) µm" for sl, (dx, dy) in r.offsets_um.items())
            if key == "vessel_alignment":
                if ctl.light.channel not in ("trans", "phase") or not ctl.light.enabled:
                    s.capture.prepare_channel(role, "trans")
                low = min(slots, key=lambda sl: prof.objective(sl).magnification or 99)
                ctl.run(ctl.set_objective(low))
                r = C.calibrate_vessel_alignment(ctl, s.vessel, s.nominal_map, role, progress=progress)
                cal.vessel_offset_um[s.vessel.vessel_id] = r.offset_um
                cal.touch("vessel_alignment")
                s.select_vessel(s.vessel.vessel_id)  # re-applies the offset to the stage map
                return (f"{s.vessel.display_name}: offset ({r.offset_um[0]:+.0f}, {r.offset_um[1]:+.0f}) µm from "
                        f"{', '.join(r.per_region)}; residual {r.residual_um:.1f} µm")
            if key == "hot_pixels":
                r = C.find_hot_pixels(ctl, role)
                cal.hot_pixels[role.value] = r.defects
                cal.touch("hot_pixels")
                return f"{len(r.defects)} defect pixels on the {role.value} camera (threshold {r.threshold:.0f} counts)"
            raise KeyError(key)

        def done(text) -> None:
            self.results[key] = text
            self.model.add_log("info", f"calibration {key}: {text}")
            self._refresh()
            if on_done:
                on_done(text)

        return self.cap.submit_when_idle(f"Calibrate {key.replace('_', ' ')}", work, with_progress=True,
                                         on_done=done, on_error=lambda m: self.lbl.setText(f"{key} failed: {m}"))

    def _clear(self) -> None:
        from openevos.acquisition.calibration import CalibrationStore

        path = self.session.calibration.path
        self.session.calibration = CalibrationStore(path=path)
        self.session.__post_init__()
        self.session.calibration.save()
        self.results = {}
        self._refresh()

    def _rng(self, v: float) -> None:
        self.session.settings.basic.auto_fine_range_percent = float(v)
        from openevos.acquisition.autofocus import fine_range_um

        span = fine_range_um(self.session.profile, self.session.controller.objective_slot(), v)
        self.lbl_rng.setText(f"≈ {span:.1f} µm search span at the current objective (100 % = default)")

    def _refresh(self) -> None:
        if not hasattr(self, "results"):
            self.results = {}
        st = self.session.calibration.status()
        self.table.setRowCount(len(self.PROCS))
        for r, (key, title, page, tip) in enumerate(self.PROCS):
            res = self.results.get(key) or ("applied" if st.get(key) else "not run — nominal values in use")
            _set_row(self.table, r, (title, page, st.get(key) or "—", res), {3: theme.OK if st.get(key) else theme.MUTED})  # noqa: E501
        self.table.resizeRowsToContents()
        v = self.session.vessel
        self.lbl_nominal.setText(f"{v.display_name}: focus nominal {v.focus_nominal_um:.1f} µm · Find sample range "
                                 f"{v.focus_range_um:.0f} µm")
        sm = self.session.stage_map
        self.lbl_align.setText(f"Vessel alignment: offset ({sm.offset_x:+.0f}, {sm.offset_y:+.0f}) µm — "
                               + ("calibrated" if sm.verified else "not calibrated (nominal geometry)"))


# ====================================================================================== Settings
class SettingsPage(QWidget):
    """Image > Settings (Mono, Color) and System > Basic, plus Vessel Manager."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        st = self.session.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        tabs = QTabWidget()
        tabs.setMaximumWidth(860)  # inspector-style rows: controls do not stretch across the window
        lay.addWidget(tabs)
        tabs.addTab(self._mono(), "Mono camera")
        tabs.addTab(self._color(), "Color camera")
        tabs.addTab(self._basic(), "System — Basic")
        tabs.addTab(self._vessels(), "Vessel Manager")
        b_save = QPushButton("Save settings")
        b_save.setObjectName("primary")
        self.lbl = label("", "muted")
        lay.addWidget(hbox(b_save, self.lbl, None))
        b_save.clicked.connect(self._save)
        del st

    def _hot_combos(self, form: QFormLayout, cam) -> list[QComboBox]:
        combos = []
        for i in range(4):
            c = QComboBox()
            for a in HotButtonAction:
                c.addItem(a.label, a)
            c.setCurrentIndex(c.findData(cam.hot_buttons[i]))
            c.currentIndexChanged.connect(lambda _=0, i=i, c=c, cam=cam: self._set_hot(cam, i, HotButtonAction(c.currentData())))  # noqa: E501
            form.addRow(f"Hot button {i + 1}", c)
            combos.append(c)
        return combos

    def _set_hot(self, cam, i: int, action) -> None:
        cam.hot_buttons[i] = action
        self.model.settings_changed.emit()

    def _quick(self, form: QFormLayout, q) -> None:
        name = QLineEdit(q.base_name)
        fmt = QComboBox()
        for f in ImageFormat:
            fmt.addItem(f.value.upper(), f)
        fmt.setCurrentIndex(fmt.findData(q.image_format))
        folder = QLineEdit(q.folder)
        folder.setPlaceholderText("default: Documents/OpenEVOS/Captures")
        color = QCheckBox("Save as colour (8-bit)")
        color.setChecked(q.save_as_color)
        under = QCheckBox("Save underlying channels")
        under.setChecked(q.save_underlying)
        form.addRow("Quick Save base name", name)
        form.addRow("Quick Save format", fmt)
        form.addRow("Quick Save folder", folder)
        form.addRow("", color)
        form.addRow("", under)

        def store():
            try:
                q.base_name = name.text()
            except ValueError:
                pass
            q.image_format, q.folder = ImageFormat(fmt.currentData()), folder.text()
            q.save_as_color, q.save_underlying = color.isChecked(), under.isChecked()

        name.editingFinished.connect(store)
        folder.editingFinished.connect(store)
        fmt.currentIndexChanged.connect(store)
        color.toggled.connect(store)
        under.toggled.connect(store)

    def _mono(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        m = self.session.settings.mono
        prof = self.session.profile
        self._hot_combos(form, m)
        form.addRow(label("Channel pseudo colours", "section"))
        for c in prof.channels:
            if c.installed and c.kind.value == "fluorescence":
                b = QPushButton()
                self._color_button(b, c.slot, c.pseudocolor)
                form.addRow(c.display, b)
        hs = QCheckBox("Highlight saturated pixels in red")
        hs.setChecked(m.highlight_saturated)
        hs.toggled.connect(lambda v: setattr(m, "highlight_saturated", v))
        bc = QCheckBox("Block condenser in fluorescence")
        bc.setChecked(m.block_condenser_in_fluorescence)
        bc.setToolTip("Needs condenser/phase-wheel motion (P axis is locked on the physical unit); no effect in the "
                      "simulator. Not recommended for most users (slower channel/objective changes).")
        bc.toggled.connect(lambda v: setattr(m, "block_condenser_in_fluorescence", v))
        uc = QComboBox()
        uc.addItem("16-bit monochrome", "16bit_mono")
        uc.addItem("8-bit color (256 colours)", "8bit_color")
        uc.setCurrentIndex(uc.findData(m.underlying_channel_save))
        uc.currentIndexChanged.connect(lambda _=0: setattr(m, "underlying_channel_save", uc.currentData()))
        form.addRow("", hs)
        form.addRow("", bc)
        form.addRow("Underlying channel save", uc)
        caw = QWidget()
        cal = QGridLayout(caw)
        cal.setContentsMargins(0, 0, 0, 0)
        for i, c in enumerate([c for c in prof.channels if c.installed and c.kind.value == "fluorescence"]):
            cb = QCheckBox(c.display)
            cb.setChecked(not m.capture_all or c.slot in m.capture_all)
            cb.toggled.connect(lambda v, s=c.slot: self._capture_all(s, v))
            cal.addWidget(cb, 0, i)
        form.addRow("Capture All selection", caw)
        self._quick(form, m.quick_save)
        return w

    def _capture_all(self, slot: str, on: bool) -> None:
        m = self.session.settings.mono
        prof = self.session.profile
        if not m.capture_all:
            m.capture_all = [c.slot for c in prof.channels if c.installed and c.kind.value == "fluorescence"]
        if on and slot not in m.capture_all:
            m.capture_all.append(slot)
        elif not on and slot in m.capture_all:
            m.capture_all.remove(slot)

    def _color_button(self, b: QPushButton, slot: str, default: str) -> None:
        m = self.session.settings.mono

        def paint():
            o = m.channels.get(slot)
            col = (o.pseudocolor if o and o.pseudocolor else None) or default
            b.setText("Edit colour")  # the swatch says it; the hex is in the tooltip
            b.setToolTip(f"{col}: click to choose another pseudo colour")
            b.setIcon(_swatch(col))  # a drawn swatch, not a thick coloured border (Impeccable audit 2026-11-18)

        def edit():
            o = m.channels.get(slot)
            c = QColorDialog.getColor(QColor((o.pseudocolor if o and o.pseudocolor else None) or default), self)
            if c.isValid():
                cs = self.session.settings.channel(CameraRole.MONO, slot)
                m.channels[slot] = cs.model_copy(update={"pseudocolor": c.name()})
                paint()
                self.model.settings_changed.emit()

        b.clicked.connect(edit)
        paint()

    def _color(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        c = self.session.settings.color
        self._hot_combos(form, c)
        form.addRow(label("Colour adjustment (future captures and live view)", "section"))
        sliders = {}
        for key, lo, hi in (("brightness", -100, 100), ("contrast", -100, 100), ("saturation", -100, 100),
                            ("hue", -180, 180)):
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(int(getattr(c, key)))
            val = label(str(int(getattr(c, key))))
            s.valueChanged.connect(lambda v, k=key, lab=val: (setattr(c, k, float(v)), lab.setText(str(v))))
            sliders[key] = s
            form.addRow(key.capitalize(), hbox(s, val))
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: [s.setValue(0) for s in sliders.values()])
        form.addRow("", reset)
        hs = QCheckBox("Highlight saturated pixels in red")
        hs.setChecked(c.highlight_saturated)
        hs.toggled.connect(lambda v: setattr(c, "highlight_saturated", v))
        form.addRow("", hs)
        self._quick(form, c.quick_save)
        return w

    def _basic(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        b = self.session.settings.basic
        g = QComboBox()
        g.addItem("Expand to zoom", "expand")
        g.addItem("Pinch to zoom", "pinch")
        g.setCurrentIndex(g.findData(b.live_gesture))
        g.currentIndexChanged.connect(lambda _=0: setattr(b, "live_gesture", g.currentData()))
        form.addRow("Live expand/pinch gesture", g)
        for key, text in (("show_scale_bar", "Show scale bar"), ("show_getting_started", "Show Getting Started button"),
                          ("timelapse_retract_objective", "Time lapse: retract objective between captures")):
            cb = QCheckBox(text)
            cb.setChecked(getattr(b, key))
            cb.toggled.connect(lambda v, k=key: setattr(b, k, v))
            form.addRow("", cb)
        form.addRow(label("Interface", "section"))
        th = QComboBox()
        for k, t in (("dark", "Dark"), ("light", "Light"), ("system", "Match system")):
            th.addItem(t, k)
        th.setCurrentIndex(th.findData(b.theme))
        th.currentIndexChanged.connect(lambda _=0: self._theme(th.currentData()))
        form.addRow("Theme", th)
        nv = QComboBox()
        for k, t in (("auto", "Automatic (icons below 1440 px)"), ("expanded", "Icons and labels"), ("rail", "Icons only")):  # noqa: E501
            nv.addItem(t, k)
        nv.setCurrentIndex(nv.findData(b.navigation))
        nv.currentIndexChanged.connect(lambda _=0: self._nav(nv.currentData()))
        form.addRow("Navigation", nv)
        sci = QCheckBox("Scientific display — linear mappings only, display log saved with images")
        sci.setChecked(b.scientific_display)
        sci.toggled.connect(lambda v: setattr(b, "scientific_display", v))
        form.addRow("", sci)
        self.chk_light_off_switch = QCheckBox("Switch the light off when changing camera")
        self.chk_light_off_switch.setChecked(b.light_off_on_camera_switch)
        self.chk_light_off_switch.setToolTip("On: Mono ↔ Colour turns the light off; turn it on again for the new "
                                             "camera. Off: the light keeps its on/off state across the switch")
        self.chk_light_off_switch.toggled.connect(lambda v: setattr(b, "light_off_on_camera_switch", v))
        form.addRow("", self.chk_light_off_switch)
        form.addRow(label("Auto fine adjustment, capture focus nominal and vessel alignment are in Calibration; gas "
                          "connections and temperature offset are in Environment.", "caption", wrap=True))
        return w

    def _theme(self, name: str) -> None:
        win = self.window()
        if hasattr(win, "set_theme"):
            win.set_theme(name)
        else:
            self.session.settings.basic.theme = name

    def _nav(self, mode: str) -> None:
        win = self.window()
        if hasattr(win, "set_nav"):
            win.set_nav(mode)
        else:
            self.session.settings.basic.navigation = mode

    def _vessels(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.v_item = QLineEdit()
        self.v_item.setPlaceholderText("e.g. Costar-3603")
        self.v_name = QLineEdit()
        self.v_name.setPlaceholderText("e.g. 96-well – Costar 3603")
        self.v_notes = QLineEdit()
        b_add = QPushButton("Save the current vessel as…")
        self.v_list = QComboBox()
        b_rm = QPushButton("Remove")
        form.addRow("New item", self.v_item)
        form.addRow("New display name", self.v_name)
        form.addRow("Notes", self.v_notes)
        form.addRow("", b_add)
        form.addRow("User created vessels", hbox(self.v_list, b_rm))
        self.v_msg = label("", "muted", wrap=True)
        form.addRow(self.v_msg)
        b_add.clicked.connect(self._add_vessel)
        b_rm.clicked.connect(self._rm_vessel)
        self._vlist()
        return w

    def _vlist(self) -> None:
        self.v_list.clear()
        for v in self.session.vessels.all():
            if v.user_created:
                self.v_list.addItem(v.display_name, v.vessel_id)

    def _add_vessel(self) -> None:
        try:
            v = self.session.vessels.add_user_vessel(self.session.vessel, self.v_item.text().strip() or "user-vessel",
                                                     self.v_name.text().strip() or "User vessel", self.v_notes.text())
            self.v_msg.setText(f"Added '{v.display_name}' (copy of {self.session.vessel.display_name} geometry and "
                               "focus nominal). It is available in Vessel Expert for this session.")
        except ValueError as exc:
            self.v_msg.setText(str(exc))
        self._vlist()

    def _rm_vessel(self) -> None:
        vid = self.v_list.currentData()
        if vid and vid != self.session.vessel.vessel_id:
            self.session.vessels.remove_user_vessel(vid)
            self._vlist()

    def _save(self) -> None:
        try:
            self.session.save_settings()
            p = self.session.settings_path
            self.lbl.setText(f"saved to {p}" if p else "settings persistence disabled for this session")
        except OSError as exc:
            self.lbl.setText(f"not saved: {exc}")
        self.model.settings_changed.emit()


# ====================================================================================== Service
SERVICE = [
    ("This instrument (serial, software revision)", "R0 read-only", "view", "Inspector report holds the recovered identity."),  # noqa: E501
    ("Microscope: objective per turret position", "R1 configuration", "view",
     "Profile JSON below; physical slots unconfirmed (O axis locked)."),
    ("Import Vessels", "R1 configuration", "available", "Vessel Manager (Settings) adds user vessels."),
    ("Error Log", "R0 read-only", "available", "Export diagnostics bundle (event log, settings, profile, versions)."),
    ("Calibrate Objective", "R3 motion", "planned", "See Calibration; needs verified turret and camera."),
    ("FL Hot Pixel Correction", "R2 data", "planned", "Factory map in lico/moca is read-only."),
    ("Move XY to Filter Cube Access Position", "R3 motion", "blocked",
     "Needs verified stage travel beyond the 22 mm boot box (Gate 7)."),
    ("Move to Shipping Position", "R3 motion", "blocked", "Needs verified stage travel (Gate 7)."),
    ("Update Software (vendor)", "R4 irreversible", "blocked",
     "OpenEVOS never installs vendor software, firmware or EEPROM images."),
]


class ServicePage(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        f, fl = panel("Service functions (risk classes)")
        t = _table(["Function", "Risk class", "Status", "Notes"])
        col = {"available": theme.OK, "view": theme.ACCENT, "planned": theme.WARN, "blocked": theme.FAULT}
        for row in SERVICE:
            r = t.rowCount()
            t.insertRow(r)
            _set_row(t, r, (row[0], row[1], row[2].upper(), row[3]), {2: col[row[2]]})
        t.setMinimumHeight(230)
        fl.addWidget(t)
        b = QPushButton("Export diagnostics bundle…")
        fl.addWidget(b, 0, Qt.AlignLeft)
        self.lbl = label("", "muted", wrap=True)
        fl.addWidget(self.lbl)
        lay.addWidget(f)
        g, gl = panel("Objective profiles (turret slots)")
        ot = _table(["Slot", "Installed", "Label", "Mag.", "NA", "Immersion", "Phase", "Calibration", "Source"])
        for o in self.session.profile.objectives:
            r = ot.rowCount()
            ot.insertRow(r)
            _set_row(ot, r, (o.slot, "yes" if o.installed else "no", o.label, o.magnification or "",
                             o.numerical_aperture or "", o.immersion or "", o.phase_ring or "",
                             "valid" if o.calibration_valid else "—", o.provenance.label))
        ot.setMinimumHeight(200)
        gl.addWidget(ot)
        lay.addWidget(g)
        lay.addStretch(1)
        b.clicked.connect(self.export_dialog)

    def export_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export diagnostics to")
        if d:
            self.export_to(Path(d))

    def export_to(self, folder: Path) -> Path:
        d = folder
        out = Path(d) / f"openevos-diagnostics-{datetime.now().astimezone():%Y%m%d-%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "events.log").write_text("\n".join(f"{t} [{lv}] {tx}" for t, lv, tx in self.model.log), "utf-8")
        (out / "settings.json").write_text(self.session.settings.model_dump_json(indent=2), "utf-8")
        (out / "profile.json").write_text(self.session.profile.model_dump_json(indent=2), "utf-8")
        (out / "versions.json").write_text(json.dumps({"openevos": __version__, "python": platform.python_version(),
                                                       "platform": platform.platform()}, indent=2), "utf-8")
        self.lbl.setText(f"exported to {out}")


# ====================================================================================== Events
class EventsPage(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        f, fl = panel("Event log")
        self.filter = QComboBox()
        self.filter.addItems(["all", "warnings and errors", "errors"])
        fl.addWidget(hbox(label("Show", "muted"), self.filter, None))
        self.t = _table(["Time", "Level", "Event"])
        empty_hint(self.t, "No events yet")
        self.t.setMinimumHeight(600)
        fl.addWidget(self.t)
        lay.addWidget(f)
        model.log_added.connect(lambda *_: self.refresh())
        self.filter.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        lv = self.filter.currentIndex()
        rows = [r for r in self.model.log if lv == 0 or (lv == 1 and r[1] != "info") or (lv == 2 and r[1] == "error")]
        rows = rows[-500:][::-1]
        self.t.setRowCount(len(rows))
        col = {"info": theme.MUTED, "warn": theme.WARN, "error": theme.FAULT}
        for i, (t, level, text) in enumerate(rows):
            _set_row(self.t, i, (t, level.upper(), text), {1: col.get(level, theme.TEXT)})
