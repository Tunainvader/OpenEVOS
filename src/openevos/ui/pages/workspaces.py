"""Z-Stack, Movies, Review and Analysis workspaces."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QWidget,
)

from openevos.imaging.composite import composite
from openevos.instrument.profile import CameraRole
from openevos.ui.common import empty_hint, hbox, label, overlay_layers, panel, render_frame
from openevos.ui.icons import icon
from openevos.ui.widgets.image_view import ImageView
from openevos.ui.widgets.segmented import SegmentedControl
from openevos.ui.widgets.slice_map import SliceMap


def _slider(lo: int, hi: int, v: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(v)
    return s


def load_image_file(path: Path):
    """Open a saved image (+ JSON sidecar if present) as a CapturedImage for review/analysis."""
    from openevos.acquisition.capture import CapturedImage
    from openevos.instrument.profile import CameraRole

    if path.suffix.lower() in (".tif", ".tiff"):
        import tifffile

        a = tifffile.imread(path)
    else:
        from PIL import Image

        a = np.asarray(Image.open(path).convert("RGB"))
    if a.ndim == 3 and a.shape[2] > 3:
        a = a[..., :3]
    side = path.with_suffix(".json")
    md = json.loads(side.read_text("utf-8")) if side.exists() else {}
    prim = md.get("primary") or {}
    a.flags.writeable = False
    return CapturedImage(
        data=a, channel=prim.get("channel") or "file", channel_label=path.stem,
        camera=CameraRole(prim.get("camera", "color" if a.ndim == 3 else "mono")),
        objective_slot=prim.get("objective_slot"), magnification=prim.get("magnification"),
        pixel_size_um=prim.get("pixel_size_um"), stage_xy_um=tuple(prim["stage_xy_um"]) if prim.get("stage_xy_um") else None,  # noqa: E501
        z_um=prim.get("z_um"), exposure_ms=prim.get("exposure_ms") or 0.0, gain=prim.get("gain") or 1.0,
        led_percent=prim.get("led_percent") or 0.0, synthetic="SIMULATED" in md.get("provenance", ""),
        frame_device=prim.get("frame_device", "file"), frame_index=prim.get("frame_index", 0),
        bit_depth=8 if a.dtype == np.uint8 else 16)


# ====================================================================================== Z-stack
class ZStackPage(QWidget):
    """Acquire a Z-stack from a start height to a stop height (up or down) and browse it: slice map, slices, playback,
    maximum-intensity projection, OME-TIFF export.

    Start, stop and step are absolute focus heights in µm; "Here" takes the live focus, "Centre" puts a range around
    it. The plan line says the range in µm, the number of slices and the direction before anything moves. While the
    stack runs a progress bar counts slices and the slice map fills in; Stop ends it after the slice in progress (focus
    still returns, the slices taken are kept). Playback steps through the slices in acquisition order (start → stop) or
    back, looping, bouncing or once.
    """

    TASK = "Z-Stack"

    def __init__(self, model, capture_page, parent=None):
        super().__init__(parent)
        self.model, self.cap = model, capture_page
        self.session = model.session
        self._initialised = False  # start/stop are filled from the live focus the first time the page shows
        self._channel_touched = False  # the operator picked a channel here (otherwise follow the Capture page)
        self._map_mode = "plan"  # the slice map shows the plan being edited, the running stack, or the stack shown
        self._running = False
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # ---------------------------------------------------------------- setup panel
        f, lay = panel("Z-stack")
        f.setFixedWidth(360)
        lo, hi = self._limits()
        form = QFormLayout()
        self.channel = QComboBox()
        self._fill_channels()
        self.start = QDoubleSpinBox(minimum=lo, maximum=hi, value=lo, decimals=2, suffix=" µm")
        self.stop = QDoubleSpinBox(minimum=lo, maximum=hi, value=lo, decimals=2, suffix=" µm")
        self.step = QDoubleSpinBox(minimum=0.1, maximum=500, value=2, decimals=2, suffix=" µm")
        for w in (self.start, self.stop):
            w.setKeyboardTracking(False)
            w.setSingleStep(1.0)
        self.btn_start_here = QPushButton("Here")
        self.btn_stop_here = QPushButton("Here")
        self.btn_start_here.setToolTip("Start at the current focus height")
        self.btn_stop_here.setToolTip("Stop at the current focus height")
        self.range = QDoubleSpinBox(minimum=0.5, maximum=2000, value=20, decimals=1, suffix=" µm")
        self.btn_centre = QPushButton("Centre")
        self.btn_centre.setToolTip("Start below the current focus and stop above it, this range in total")
        self.btn_swap = QPushButton("Swap")
        self.btn_swap.setToolTip("Swap start and stop (reverses the direction)")
        form.addRow("Channel", self.channel)
        form.addRow("Start", hbox(self.start, self.btn_start_here))
        form.addRow("Stop", hbox(self.stop, self.btn_stop_here))
        form.addRow("Step", hbox(self.step, self.btn_swap))
        form.addRow("Around focus", hbox(self.range, self.btn_centre))
        lay.addLayout(form)
        self.lbl_focus = label("", "muted")
        lay.addWidget(self.lbl_focus)
        self.lbl_plan = label("", "mono", wrap=True)
        self.lbl_plan.setAccessibleName("Z-stack plan")
        lay.addWidget(self.lbl_plan)
        self.lbl_warn = label("", "warn", wrap=True)
        self.lbl_warn.hide()
        lay.addWidget(self.lbl_warn)
        self.btn = QPushButton("Acquire Z-stack")
        self.btn.setObjectName("primary")
        self.btn.setIcon(icon("zstack"))
        self.btn_cancel = QPushButton("Stop")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setIcon(icon("stop"))
        self.btn_cancel.setToolTip("End the stack after the slice in progress; focus returns and the slices taken "
                                   "are kept")
        self.btn_cancel.setEnabled(False)
        lay.addWidget(hbox(self.btn, self.btn_cancel))
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("idle")
        lay.addWidget(self.bar)
        lay.addWidget(label("Every slice is a verified Z move followed by an exposure (no exposure while Z moves). "
                            "Focus returns to where it started.", "muted", wrap=True))
        self.btn_ome = QPushButton("Export OME-TIFF…")
        self.btn_ome.setToolTip("Z × Y × X with physical pixel size and Z step (opens in Fiji, napari, QuPath)")
        self.btn_ome.clicked.connect(self._export_ome)
        lay.addWidget(self.btn_ome)
        lay.addStretch(1)
        root.addWidget(f)

        # ---------------------------------------------------------------- viewer, slice map, playback
        g, gl = panel("Slices")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.view = ImageView()
        self.view.placeholder = "No Z-stack yet"
        row.addWidget(self.view, 1)
        self.map = SliceMap()
        row.addWidget(self.map)
        gl.addLayout(row, 1)
        self.slice = _slider(0, 0, 0)
        self.slice.setAccessibleName("Slice")
        self.lbl_slice = label("", "mono")
        gl.addWidget(hbox(self.slice, self.lbl_slice))
        self.b_play = QPushButton("Play")
        self.b_play.setCheckable(True)
        self.b_play.setIcon(icon("play"))
        self.b_play.setToolTip("Step through the slices (Space)")
        self.direction = SegmentedControl([("fwd", "Start → Stop"), ("back", "Stop → Start")],
                                          accessible_name="Playback direction")
        self.direction.set_current("fwd")
        self.mode = QComboBox()
        for k, t in (("loop", "Loop"), ("bounce", "Bounce"), ("once", "Once")):
            self.mode.addItem(t, k)
        self.mode.setToolTip("At the end: start again, turn round, or stop")
        self.fps = QSpinBox(minimum=1, maximum=30, value=4, suffix=" fps")
        self.proj = QCheckBox("Max projection")
        self.proj.setToolTip("Show the maximum-intensity projection of all slices")
        gl.addWidget(hbox(self.b_play, self.direction, self.mode, self.fps, None, self.proj))
        root.addWidget(g, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.focus_timer = QTimer(self)
        self.focus_timer.setInterval(250)
        self.focus_timer.timeout.connect(self._update_focus)

        # ---------------------------------------------------------------- wiring
        for w in (self.start, self.stop, self.step):
            w.valueChanged.connect(self._plan_edited)
        self.btn_start_here.clicked.connect(lambda: self._set_here(self.start))
        self.btn_stop_here.clicked.connect(lambda: self._set_here(self.stop))
        self.btn_centre.clicked.connect(self._centre)
        self.btn_swap.clicked.connect(self._swap)
        self.channel.activated.connect(self._channel_picked)
        self.btn.clicked.connect(self._acquire)
        self.btn_cancel.clicked.connect(self._cancel)
        self.slice.valueChanged.connect(self._show)
        self.proj.toggled.connect(self._show)
        self.b_play.toggled.connect(self._play)
        self.fps.valueChanged.connect(self._fps_changed)
        self.map.slice_clicked.connect(self._map_clicked)
        model.buffers_changed.connect(self._refresh)
        model.settings_changed.connect(self._fill_channels)  # the camera may have changed
        model.tasks.busy_changed.connect(self._busy)
        model.tasks.started.connect(self._task_started)
        model.tasks.progress.connect(self._task_progress)
        model.tasks.finished.connect(self._task_finished)
        model.tasks.failed.connect(self._task_failed)
        self._plan_edited()

    # ------------------------------------------------------------------ setup
    def _limits(self) -> tuple[float, float]:
        lim = self.session.controller.axis_limits(self.session.profile.focus_device, "z")
        return (float(lim[0]), float(lim[1])) if lim else (0.0, 10_000.0)

    def _fill_channels(self) -> None:
        """Only channels the active camera can image (audit APP-13: the colour camera was offered DAPI)."""
        role = self.session.settings.active_camera
        want = [(c.display, c.slot) for c in self.session.profile.channels_for(role) if c.installed]
        if [(self.channel.itemText(i), self.channel.itemData(i)) for i in range(self.channel.count())] == want:
            return
        cur = self.channel.currentData()
        self.channel.blockSignals(True)
        self.channel.clear()
        for text, slot in want:
            self.channel.addItem(text, slot)
        i = self.channel.findData(cur)
        self.channel.setCurrentIndex(max(i, 0))
        self.channel.blockSignals(False)

    def _channel_picked(self, _i: int) -> None:
        self._channel_touched = True

    def _follow_capture_channel(self) -> None:
        """Until the operator picks one here, image the channel the Capture page is on (not the first in the list)."""
        if self._channel_touched:
            return
        i = self.channel.findData(getattr(self.cap, "channel", None))
        if i >= 0:
            self.channel.setCurrentIndex(i)

    def _set_here(self, box: QDoubleSpinBox) -> None:
        z = self.session.controller.focus_z()
        if z is not None:
            box.setValue(z)

    def _set_both(self, start: float, stop: float) -> None:
        for box, v in ((self.start, start), (self.stop, stop)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self._plan_edited()

    def _centre(self) -> None:
        z = self.session.controller.focus_z()
        if z is None:
            self.model.add_log("warn", "Z-stack: focus position unknown")
            return
        half = self.range.value() / 2
        self._set_both(z - half, z + half)

    def _swap(self) -> None:
        self._set_both(self.stop.value(), self.start.value())

    def plan(self) -> tuple[list[float], str]:
        """(slice heights in acquisition order, problem); the problem is "" when the plan can run."""
        from openevos.acquisition.capture import CaptureRejected, plan_zstack

        try:
            zs = plan_zstack(self.start.value(), self.stop.value(), self.step.value())
        except CaptureRejected as exc:
            return [], str(exc)
        lim = self.session.controller.axis_limits(self.session.profile.focus_device, "z")
        if lim and (min(zs) < lim[0] or max(zs) > lim[1]):
            return zs, f"outside the focus limits {lim[0]:,.0f}–{lim[1]:,.0f} µm"
        return zs, ""

    def _plan_edited(self, *_a) -> None:
        zs, problem = self.plan()
        a, b = self.start.value(), self.stop.value()
        span = abs(b - a)
        if zs:
            way = "single plane" if len(zs) == 1 else ("upward (start below stop)" if b > a
                                                      else "downward (start above stop)")
            text = (f"{a:,.2f} → {b:,.2f} µm\n{span:,.2f} µm range · {len(zs)} slices of "
                    f"{self.step.value():g} µm · {way}")
            if len(zs) > 1 and abs(zs[-1] - b) > 1e-6:
                text += f"\nlast slice at {zs[-1]:,.2f} µm (range is not a whole number of steps)"
        else:
            text = f"{a:,.2f} → {b:,.2f} µm · {span:,.2f} µm range"
        self.lbl_plan.setText(text)
        self.lbl_warn.setText(f"Cannot start: {problem}" if problem else "")
        self.lbl_warn.setVisible(bool(problem))
        if not self._running:
            self._map_mode = "plan"
            self.map.set_plan(zs, b if zs else None)
        self._update_buttons()

    def _update_buttons(self) -> None:
        _zs, problem = self.plan()
        busy = self.model.tasks.busy
        self.btn.setEnabled(not busy and not problem and self.channel.count() > 0)
        self.btn.setToolTip(f"Cannot start: {problem}" if problem else
                            (f"Instrument busy with {self.model.tasks.current}" if busy else ""))
        self.btn_cancel.setEnabled(self._running)

    def _update_focus(self) -> None:
        z = self.session.controller.focus_z()
        self.lbl_focus.setText(f"Focus now: {z:,.2f} µm" if z is not None else "Focus now: unknown")
        self.map.set_focus(z)

    # ------------------------------------------------------------------ run
    def _acquire(self) -> None:
        zs, problem = self.plan()
        if problem or not zs:
            self.model.add_log("warn", f"Z-stack: {problem or 'nothing to acquire'}")
            return
        slot, role = self.channel.currentData(), self.session.settings.active_camera
        a, b, st = self.start.value(), self.stop.value(), self.step.value()
        self.b_play.setChecked(False)
        self.model.submit_when_idle(self.TASK, lambda p: self.session.capture.zstack(slot, a, b, st, role, p),
                                    with_progress=True, on_done=self._done)

    def _cancel(self) -> None:
        self.session.capture.cancel_zstack()
        self.bar.setFormat("stopping after this slice…")

    def _done(self, stack) -> None:
        how = "" if stack.complete else f" (stopped: {len(stack.images)} of {stack.planned} slices)"
        self.model.add_log("info" if stack.complete else "warn",
                           f"Z-stack: {len(stack.images)} slices, {stack.z_um[0]:,.1f} → {stack.z_um[-1]:,.1f} µm{how}")

    def _busy(self, _b: bool) -> None:
        self._update_buttons()

    def _task_started(self, name: str) -> None:
        if name != self.TASK:
            return
        self._running = True
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("starting…")
        self._update_buttons()

    def _task_progress(self, name: str, i: int, n: int, msg: str) -> None:
        if name != self.TASK:
            return
        self.bar.setRange(0, max(n, 1))
        self.bar.setValue(i)
        self.bar.setFormat(msg if i < n else f"{n}/{n} slices")
        run = self.session.capture.zstack_running
        if run is not None:
            zs, stop = run
            if self._map_mode != "run" or len(self.map.planned) != len(zs):
                self._map_mode = "run"
                self.map.set_plan(list(zs), stop)
            self.map.set_taken(i, current=i if i < n else None)

    def _task_finished(self, name: str, stack) -> None:
        if name != self.TASK:
            return
        self._running = False
        planned = stack.planned or len(stack.images)
        self.bar.setRange(0, max(planned, 1))
        self.bar.setValue(len(stack.images))
        self.bar.setFormat(f"{len(stack.images)}/{planned} slices" + ("" if stack.complete else " · stopped"))
        self._update_buttons()
        self._show()

    def _task_failed(self, name: str, msg: str) -> None:
        if name != self.TASK:
            return
        self._running = False
        self.bar.setValue(0)
        self.bar.setFormat(f"failed: {msg}")
        self.bar.setToolTip(msg)
        self._update_buttons()
        self._plan_edited()

    # ------------------------------------------------------------------ browse
    def _refresh(self) -> None:
        zs = self.session.capture.buffers.zstack
        if zs is None:
            self.b_play.setChecked(False)
            return
        self.slice.blockSignals(True)
        self.slice.setRange(0, len(zs.images) - 1)
        self.slice.blockSignals(False)
        self._show()

    def _show(self, *_a) -> None:
        zs = self.session.capture.buffers.zstack
        if zs is None:
            return
        st = self.session.settings
        current: int | None
        if self.proj.isChecked():
            data = np.max(np.stack([i.data for i in zs.images]), axis=0)
            self.lbl_slice.setText(f"MIP of {len(zs.images)} slices")
            current = None
        else:
            current = min(self.slice.value(), len(zs.images) - 1)
            data = zs.images[current].data
            self.lbl_slice.setText(f"slice {current + 1}/{len(zs.images)}  z = {zs.z_um[current]:,.2f} µm")
        if not self._running:
            self._map_mode = "stack"
            self.map.set_plan(list(zs.z_um), zs.stop_um, taken=len(zs.z_um), current=current)
        self.view.set_image(render_frame(data, zs.channel, self.session.profile, st))
        self.view.pixel_um = zs.images[0].pixel_size_um
        self.view.pixel_calibrated = zs.images[0].pixel_size_calibrated
        self.view.badge = "Z-STACK • SIMULATED" if zs.images[0].synthetic else "Z-STACK"

    def _map_clicked(self, i: int) -> None:
        if self._map_mode == "stack" and self.session.capture.buffers.zstack is not None:
            self.proj.setChecked(False)
            self.slice.setValue(i)

    # ------------------------------------------------------------------ playback
    def _interval(self) -> int:
        return max(1, int(1000 / self.fps.value()))

    def _fps_changed(self, _v: int) -> None:
        if self.timer.isActive():
            self.timer.start(self._interval())

    def _play(self, on: bool) -> None:
        zs = self.session.capture.buffers.zstack
        if on and (zs is None or len(zs.images) < 2):
            self.b_play.blockSignals(True)
            self.b_play.setChecked(False)
            self.b_play.blockSignals(False)
            return
        self.b_play.setText("Pause" if on else "Play")
        self.b_play.setIcon(icon("pause" if on else "play"))
        if on:
            self.proj.setChecked(False)
            self.timer.start(self._interval())
        else:
            self.timer.stop()

    def _tick(self) -> None:
        """One playback step in the chosen direction (forward = acquisition order, start → stop)."""
        zs = self.session.capture.buffers.zstack
        if zs is None or len(zs.images) < 2:
            self.b_play.setChecked(False)
            return
        last = len(zs.images) - 1
        d = -1 if self.direction.current() == "back" else 1
        i = self.slice.value() + d
        if 0 <= i <= last:
            self.slice.setValue(i)
            return
        mode = self.mode.currentData()
        if mode == "loop":
            self.slice.setValue(0 if d > 0 else last)
        elif mode == "bounce":
            self.direction.set_current("back" if d > 0 else "fwd")
            self.slice.setValue(self.slice.value() - d)
        else:
            self.b_play.setChecked(False)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Space and self.session.capture.buffers.zstack is not None:
            self.b_play.toggle()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ export
    def _export_ome(self) -> None:
        from openevos.imaging.ome import zstack_to_ome

        zs = self.session.capture.buffers.zstack
        if zs is None:
            self.model.add_log("warn", "no Z-stack to export")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export OME-TIFF", "zstack.ome.tif", "OME-TIFF (*.ome.tif)")
        if path:
            zstack_to_ome(Path(path), zs, zs.channel, zs.images[0].synthetic)
            self.model.add_log("info", f"Z-stack exported to {path}")

    # ------------------------------------------------------------------ visibility
    def showEvent(self, e):
        super().showEvent(e)
        self._follow_capture_channel()
        if not self._initialised and self.session.controller.focus_z() is not None:
            self._initialised = True
            self._centre()
        self._update_focus()
        self.focus_timer.start()
        self._refresh()
        self._update_buttons()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.focus_timer.stop()
        self.b_play.setChecked(False)


# ====================================================================================== Movies
class _Pane:
    def __init__(self, name: str):
        self.name = name
        self.frames: list[tuple[str, np.ndarray]] = []
        self.view = ImageView()
        self.view.placeholder = f"{name}: load frames"
        self.view.show_scale_bar = False


class MoviesPage(QWidget):
    """Movie Studio (manual p.33-39): two viewers (left / right / both), Time-lapse and Z-stack sources,
    start/end markers, frames per second, watermark, play, export as multi-page TIFF or animated GIF."""

    def __init__(self, model, timelapse_page, parent=None):
        super().__init__(parent)
        self.model, self.tl = model, timelapse_page
        self.session = model.session
        self.panes = {"left": _Pane("Left viewer"), "right": _Pane("Right viewer")}
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        f, lay = panel("Movie studio")
        f.setFixedWidth(340)
        self.pane = QComboBox()
        for k, t in (("left", "Left viewer"), ("right", "Right viewer"), ("both", "Both viewers")):
            self.pane.addItem(t, k)
        self.source = QComboBox()
        self.source.addItem("Last Z-stack", "zstack")
        self.source.addItem("Last time-lapse (per beacon)", "timelapse")
        self.beacon = QComboBox()
        self.fps = QSpinBox(minimum=1, maximum=60, value=5, suffix=" fps")
        self.watermark = QCheckBox("Include watermark data in video")
        self.watermark.setChecked(True)
        form = QFormLayout()
        form.addRow("Apply to", self.pane)
        form.addRow("Source", self.source)
        form.addRow("Beacon", self.beacon)
        form.addRow("Speed", self.fps)
        form.addRow("", self.watermark)
        lay.addLayout(form)
        b_load = QPushButton("Load frames")
        self.start, self.end = QSpinBox(), QSpinBox()
        self.b_play = QPushButton("Play")
        self.b_play.setCheckable(True)
        b_tif, b_gif = QPushButton("Export TIFF stack…"), QPushButton("Export GIF…")
        lay.addWidget(b_load)
        lay.addWidget(hbox(label("Start", "rowlabel"), self.start, label("End", "rowlabel"), self.end, None))
        lay.addWidget(self.b_play)
        lay.addWidget(hbox(b_tif, b_gif))
        self.lbl = label("", "caption", wrap=True)
        lay.addWidget(self.lbl)
        lay.addWidget(label("Two viewers compare beacons, channels or a Z-stack against a time-lapse side by side; "
                            "playback runs in step on both.", "caption", wrap=True))
        lay.addStretch(1)
        root.addWidget(f)
        g, gl = panel(None, margins=10)
        row = QHBoxLayout()
        row.addWidget(self.panes["left"].view, 1)
        row.addWidget(self.panes["right"].view, 1)
        gl.addLayout(row, 1)
        self.pos = _slider(0, 0, 0)
        self.lbl_pos = label("", "mono")
        gl.addWidget(hbox(self.pos, self.lbl_pos))
        root.addWidget(g, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next)
        b_load.clicked.connect(self._load)
        self.b_play.toggled.connect(self._play)
        self.pos.valueChanged.connect(self._show)
        b_tif.clicked.connect(lambda: self._export("tif"))
        b_gif.clicked.connect(lambda: self._export("gif"))
        self.source.currentIndexChanged.connect(self._beacons)

    def _targets(self) -> list[_Pane]:
        k = self.pane.currentData()
        return list(self.panes.values()) if k == "both" else [self.panes[k]]

    def _beacons(self) -> None:
        self.beacon.clear()
        res = self.tl.result
        if self.source.currentData() == "timelapse" and res:
            for b in dict.fromkeys(lab for _, lab, _ in res.frames):
                self.beacon.addItem(b)

    def _collect(self) -> list[tuple[str, np.ndarray]]:
        st, prof = self.session.settings, self.session.profile
        if self.source.currentData() == "zstack":
            zs = self.session.capture.buffers.zstack
            return [(f"z={z:.2f} µm", render_frame(i.data, zs.channel, prof, st)) for z, i in zip(zs.z_um, zs.images, strict=True)] \
                if zs else []  # noqa: E501
        if not self.beacon.count():
            self._beacons()
        res, b = self.tl.result, self.beacon.currentText()
        out = []
        for tp, lab, imgs in (res.frames if res else []):
            if lab == b:
                mono = [v for v in imgs.values() if not v.is_color]
                rgb = composite(overlay_layers(mono, prof, st)) if mono else next(iter(imgs.values())).data
                out.append((f"{lab} t{tp}", rgb))
        return out

    def _load(self) -> None:
        frames = self._collect()
        for pane in self._targets():
            pane.frames = list(frames)
        n = max(len(p.frames) for p in self.panes.values())
        for w in (self.start, self.end):
            w.setRange(0, max(0, n - 1))
        self.end.setValue(max(0, n - 1))
        self.pos.setRange(0, max(0, n - 1))
        self.lbl.setText(f"{len(frames)} frame(s) loaded into {self.pane.currentText().lower()}" if frames
                         else "No frames: run a Z-stack or time-lapse first")
        self._show()

    def _play(self, on: bool) -> None:
        self.b_play.setText("Pause" if on else "Play")
        self.timer.start(int(1000 / self.fps.value())) if on else self.timer.stop()

    def _next(self) -> None:
        v = self.pos.value() + 1
        self.pos.setValue(self.start.value() if v > self.end.value() else v)

    def _show(self) -> None:
        i = self.pos.value()
        for pane in self.panes.values():
            if pane.frames:
                t, rgb = pane.frames[min(i, len(pane.frames) - 1)]
                pane.view.set_image(rgb)
                pane.view.watermark = t if self.watermark.isChecked() else ""
        self.lbl_pos.setText(f"frame {i + 1}")

    @property
    def frames(self) -> list[tuple[str, np.ndarray]]:
        return self.panes["left"].frames

    def _export(self, kind: str) -> None:
        pane = self._targets()[0]
        if not pane.frames:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export movie", f"movie.{kind}")
        if path:
            self.export_to(Path(path), kind, pane)

    def export_to(self, path: Path, kind: str, pane: _Pane | None = None) -> Path:
        pane = pane or self.panes["left"]
        sel = [rgb for _, rgb in pane.frames[self.start.value(): self.end.value() + 1]]
        if kind == "tif":
            import tifffile

            tifffile.imwrite(path, np.stack(sel), photometric="rgb")
        else:
            from PIL import Image

            ims = [Image.fromarray(a) for a in sel]
            ims[0].save(path, save_all=True, append_images=ims[1:], duration=int(1000 / self.fps.value()), loop=0)
        self.lbl.setText(f"exported {len(sel)} frames to {path}")
        return path


# ====================================================================================== Review
class ReviewPage(QWidget):
    """Review: session memory buffers and saved files (with their metadata sidecars)."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        f, lay = panel("Browse")
        f.setFixedWidth(360)
        lay.addWidget(label("Session buffers", "section"))
        self.buf = QListWidget()
        empty_hint(self.buf, "No captures yet: capture on the Capture page")
        self.buf.setMaximumHeight(150)
        lay.addWidget(self.buf)
        lay.addWidget(label("Saved files", "section"))
        from openevos.ui.pages.save import default_capture_folder

        self.folder = QLineEdit(str(default_capture_folder()))
        br = QPushButton()  # was a bare "…": now a folder icon (UI audit 2026-11-17)
        br.setIcon(icon("folder"))
        br.setToolTip("Browse…")
        br.setFixedWidth(34)
        rf = QPushButton("Refresh")
        lay.addWidget(hbox(self.folder, br, rf))
        self.files = QListWidget()
        empty_hint(self.files, "No saved images in this folder")
        lay.addWidget(self.files, 1)
        self.rename = QLineEdit()
        self.rename.setPlaceholderText("new name (without extension)")
        b_ren = QPushButton("Rename")
        b_del = QPushButton("Delete…")
        b_del.setObjectName("danger")
        b_del.setToolTip("Moves the file and its sidecar to the OpenEVOS Trash folder next to it (recoverable)")
        lay.addWidget(hbox(self.rename, b_ren, b_del))
        self.b_reuse = QPushButton("Reuse settings")
        self.b_reuse.setToolTip("Recall objective, XY, focus and light channel/intensity from the selected image's "
                                "metadata and apply them through the command router (manual p.18)")
        lay.addWidget(self.b_reuse)
        root.addWidget(f)
        g, gl = panel("Viewer")
        self.view = ImageView()
        self.view.placeholder = "Select an image"
        gl.addWidget(self.view, 1)
        self.meta = QPlainTextEdit()
        self.meta.setReadOnly(True)
        self.meta.setMaximumHeight(170)
        gl.addWidget(self.meta)
        root.addWidget(g, 1)
        br.clicked.connect(lambda: self.folder.setText(QFileDialog.getExistingDirectory(self, "Folder") or self.folder.text()))  # noqa: E501
        rf.clicked.connect(self._files)
        self.buf.currentRowChanged.connect(self._show_buf)
        self.files.currentRowChanged.connect(self._show_file)
        b_ren.clicked.connect(self._rename)
        b_del.clicked.connect(self._delete)
        self.b_reuse.clicked.connect(self._reuse)
        model.buffers_changed.connect(self._bufs)
        self._selected_meta: dict | None = None

    def showEvent(self, e):
        super().showEvent(e)
        self._bufs()
        self._files()

    def _bufs(self) -> None:
        self._buf_items = list(self.session.capture.buffers.all().values())
        self.buf.clear()
        for i in self._buf_items:
            self.buf.addItem(f"{i.channel_label} · {i.camera.value} · {i.captured_at:%H:%M:%S}"
                             + (" · SIMULATED" if i.synthetic else ""))

    def _files(self) -> None:
        self.files.clear()
        p = Path(self.folder.text())
        self._paths = sorted([x for x in p.glob("*") if x.suffix.lower() in (".tif", ".tiff", ".png", ".jpg", ".bmp")],
                             key=lambda x: x.stat().st_mtime, reverse=True) if p.is_dir() else []
        for x in self._paths:
            self.files.addItem(x.name)

    def _show_buf(self, row: int) -> None:
        if 0 <= row < len(self._buf_items):
            i = self._buf_items[row]
            self.view.set_image(render_frame(i.data, i.channel, self.session.profile, self.session.settings,
                                             max_value=i.max_value))
            self.view.pixel_um = i.pixel_size_um
            self.view.pixel_calibrated = i.pixel_size_calibrated
            self._selected_meta = i.metadata()
            self.meta.setPlainText(json.dumps(self._selected_meta, indent=2, default=str))

    def _show_file(self, row: int) -> None:
        if not (0 <= row < len(self._paths)):
            return
        p = self._paths[row]
        try:
            if p.suffix.lower() in (".tif", ".tiff"):
                import tifffile

                a = tifffile.imread(p)
            else:
                from PIL import Image

                a = np.asarray(Image.open(p).convert("RGB"))
        except Exception as exc:  # noqa: BLE001
            self.meta.setPlainText(f"cannot open: {exc}")
            return
        if a.ndim == 2:
            from openevos.imaging.composite import auto_range, gray_rgb, to_8bit

            a = gray_rgb(to_8bit(a, *auto_range(a)))
        elif a.ndim == 3 and a.shape[2] > 3:
            a = a[..., :3]
        self.view.set_image(np.ascontiguousarray(a.astype(np.uint8)))
        side = p.with_suffix(".json")
        self.meta.setPlainText(side.read_text("utf-8") if side.exists() else "(no sidecar metadata)")
        try:
            md = json.loads(side.read_text("utf-8")) if side.exists() else {}
            self._selected_meta = md.get("primary")
            prim = md.get("primary") or {}
            self.view.pixel_um = prim.get("pixel_size_um")
            self.view.pixel_calibrated = (prim.get("extra") or {}).get("pixel_size_source") == "calibrated"
            self.view.badge = "SIMULATED" if "SIMULATED" in md.get("provenance", "") else ""
        except ValueError:
            pass

    def _reuse(self) -> None:
        md = self._selected_meta
        if not md:
            self.model.add_log("warn", "Reuse: no acquisition metadata for this image")
            return
        s = self.session
        ctl = s.controller

        def work():
            done = []
            if md.get("objective_slot"):
                ctl.run(ctl.set_objective(int(md["objective_slot"])))
                done.append("objective")
            if md.get("stage_xy_um"):
                ctl.run(ctl.move_stage(*md["stage_xy_um"]))
                done.append("XY")
            if md.get("z_um") is not None:
                ctl.run(ctl.move_focus(float(md["z_um"])))
                done.append("focus")
            ch = md.get("channel")
            if ch and s.profile.channel(ch):
                role = CameraRole(md.get("camera", "mono"))
                cs = s.settings.channel(role, ch).model_copy(update={
                    "led_percent": md.get("led_percent", 40.0), "exposure_ms": md.get("exposure_ms") or 20.0,
                    "gain": md.get("gain") or 1.0})
                s.settings.camera_settings(role).channels[ch] = cs
                ctl.run(ctl.set_light(ch, cs.led_percent, True))
                ctl.apply_camera(role, cs.exposure_ms, cs.gain)
                done.append(f"{ch} light/camera")
            return done

        self.model.submit_when_idle("Reuse settings", work,
                                on_done=lambda d: self.model.add_log("info", f"reused: {', '.join(d)}"))

    def _delete(self, confirm: bool = True) -> Path | None:
        row = self.files.currentRow()
        if not (0 <= row < len(self._paths)):
            return None
        p = self._paths[row]
        if confirm:
            from PySide6.QtWidgets import QMessageBox

            if QMessageBox.question(self, "Delete image", f"Move {p.name} to the OpenEVOS Trash folder?") \
                    != QMessageBox.Yes:
                return None
        trash = p.parent / "OpenEVOS Trash"
        trash.mkdir(exist_ok=True)
        target = trash / p.name
        p.rename(target)
        side = p.with_suffix(".json")
        if side.exists():
            side.rename(trash / side.name)
        self.model.add_log("info", f"moved {p.name} to {trash}")
        self._files()
        return target

    def _rename(self) -> None:
        row = self.files.currentRow()
        new = self.rename.text().strip()
        if not (0 <= row < len(self._paths)) or not new or any(c in new for c in '\\/:*?"<>|'):
            return
        p = self._paths[row]
        target = p.with_name(new + p.suffix)
        if target.exists():
            self.model.add_log("warn", f"{target.name} already exists")
            return
        p.rename(target)
        side = p.with_suffix(".json")
        if side.exists():
            side.rename(target.with_suffix(".json"))
        self._files()
