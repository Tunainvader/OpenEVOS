"""Image > Capture — full control parity with the original Capture tab (manual p.7-9), laid out with the
Craftimon conventions:

  ┌ Instrument ─────────┐ ┌ toolbar: labelled icon buttons (labels drop when narrow) ──────┐ ┌ Light & focus ─┐
  │ camera · objective   │ │                                                                │ │ channel buttons │
  │ vessel + map         │ │                 image viewport (always dark)                   │ │ power · light   │
  │ position             │ │   badge: channel • LIVE/CAPTURED • SIMULATED   loupe   scale    │ │ actual mode     │
  │ medium jog           │ ├ display: levels histogram (toggle) │ auto range · readout     ┤ │ focus column    │
  │ objectives           │ └────────────────────────────────────────────────────────────────┘ │ capture + hot   │
  └──────────────────────┘                                                                    └─────────────────┘

Every action is a typed command via the controller (coalesced per device) or a worker-thread workflow.
Display changes never alter raw data; in Scientific mode only linear mappings are allowed.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.controller import TURRET_TIMEOUT_S
from openevos.acquisition.turret_safety import TURRET_CENTRE_MM, TURRET_STAGE_BOX_MM, outside_turret_box
from openevos.core.models import Activity, BackendKind, Connection, Health
from openevos.imaging.composite import (
    Layer,
    adjust_color,
    composite,
    gray_rgb,
    highlight_saturated,
    histogram,
    transfection,
)
from openevos.imaging.composite import pseudocolor as tint
from openevos.imaging.display import (
    DisplaySettings,
    clipped_fractions,
    clipping_overlay,
    map_to_8bit,
)
from openevos.instrument.profile import CameraRole, ChannelKind
from openevos.instrument.settings import ChannelSettings, HotButtonAction
from openevos.ui import device_state as ds
from openevos.ui import theme
from openevos.ui.common import (
    Coalescer,
    DisplayState,
    channel_color,
    hbox,
    kv,
    label,
    overlay_layers,
    panel,
)
from openevos.ui.dialogs import (
    LightCameraDialog,
    TransfectionDialog,
    VesselExpertDialog,
    ZStackDialog,
)
from openevos.ui.icons import icon
from openevos.ui.widgets.controls import ChannelButton, Joystick, LevelsHistogram
from openevos.ui.widgets.focus_pad import ZFocusPanel, fmt_um
from openevos.ui.widgets.image_view import ImageView
from openevos.ui.widgets.param import ParamRow
from openevos.ui.widgets.segmented import SegmentedControl
from openevos.ui.widgets.status import Badge
from openevos.ui.widgets.vessel_map import VesselMap

#: Capture focus nominal asks first when Z is this close to home, or this far from the vessel's current nominal (APP-21)
NOMINAL_NEAR_HOME_UM = 200.0
NOMINAL_JUMP_UM = 300.0


def tool_button(text: str, ic: str, *, checkable: bool = False, tip: str = "") -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setIcon(icon(ic))
    b.setIconSize(QSize(16, 16))
    b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    b.setCheckable(checkable)
    b.setToolTip(tip or text)
    b.setAccessibleName(text)
    b.setProperty("icon_name", ic)
    return b


#: an axis this close outside its range (a count or two after power-up homing) is held at the edge, not refused
HOME_SLACK_UM = 5.0


def joystick_direction(vx: float, vy: float) -> tuple[int, int] | None:
    """Joystick deflection -> one of 4 stage directions (sx, sy) on the dominant axis, or None inside the dead zone.
    Screen up (vy < 0) = Y+ (away from you); right = X+. No diagonals: until the source of Y's encoder drift is
    found, X and Y never move together (Y-axis plan v3 ground rule, ADR-0040)."""
    ax, ay = abs(vx), abs(vy)
    if max(ax, ay) <= 0.3:
        return None
    return (1 if vx > 0 else -1, 0) if ax >= ay else (0, -1 if vy > 0 else 1)


class CapturePage(QWidget):
    request_page = Signal(str)  # e.g. "save"

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        self.profile = self.session.profile
        self.settings = self.session.settings
        self.ctl = self.session.controller
        self.channel = "trans"
        self.actual_mode = False
        self.view_mode = "live"  # live | captured | overlay
        self.view_image: np.ndarray | None = None
        self.view_raw: np.ndarray | None = None
        self._view_slot: str | None = None
        self._view_max = 4095
        self._comparing = False
        self.display = DisplaySettings(scientific=self.settings.basic.scientific_display)
        self._smooth = DisplayState()
        self.coalesce = Coalescer(self)
        self.coalesce.on_error = lambda k, e: self.model.add_log("error", f"{k}: {e}")
        model.idle_checks.append(self.coalesce.pending)
        self._last_frame_slot: str | None = None
        self._light_applied = False  # power/device sync starts after the page has sent its first light command
        self._build()
        self._wire()
        QTimer.singleShot(0, self._initial_state)

    # ================================================================== layout
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        # the Instrument column scrolls on short windows instead of squeezing the jog pad onto the Objectives heading
        # (audit APP-11, 1280 x 800)
        self.left = QScrollArea()
        self.left.setWidget(self._left())
        self.left.setWidgetResizable(True)
        self.left.setFrameShape(QFrame.NoFrame)
        self.left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left.viewport().setAutoFillBackground(False)
        self.left.setMinimumWidth(248)
        self.left.setMaximumWidth(280)
        # the Light & focus column scrolls on short windows instead of squashing the Z focus panel (1100 x 720); the
        # Capture block stays pinned below it (audit APP-06)
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(self._right())
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.NoFrame)
        self.right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.right_scroll.viewport().setAutoFillBackground(False)
        self.right = QWidget()
        col = QVBoxLayout(self.right)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addWidget(self.right_scroll, 1)
        col.addWidget(self._capture_block(), 0)
        self.right.setMinimumWidth(300)
        self.right.setMaximumWidth(330)
        root.addWidget(self.left, 0)
        root.addWidget(self._center(), 1)
        root.addWidget(self.right, 0)

    def _left(self) -> QWidget:
        f, lay = panel("Instrument")
        f.setMinimumWidth(238)  # inside the column's scroll area (scroll bar 10 px)
        lay.addWidget(label("Camera", "section"))
        self.seg_camera = SegmentedControl([("mono", "Monochrome"), ("color", "Colour")], accessible_name="Camera")
        self.seg_camera.buttons["mono"].setToolTip("Monochrome camera: fluorescence and brightfield")
        if self.profile.camera(CameraRole.COLOR) is None:
            self.seg_camera.set_option_enabled("color", False, "No colour camera in this instrument profile")
        else:
            self.seg_camera.buttons["color"].setToolTip("Colour camera: brightfield / phase only")
        lay.addWidget(self.seg_camera)
        self.lbl_camera = label("Monochrome camera", "caption")
        self.badge_frames = Badge("SIMULATED", "simulated")
        self.badge_frames.setToolTip("Frames come from the optics simulator")
        # never updated, so it also said SIMULATED for the real cameras; the image overlay and the menu-bar mode badge
        # already say where frames come from (Impeccable audit 2026-11-18)
        self.badge_frames.setVisible(False)
        lay.addWidget(hbox(self.lbl_camera, None, self.badge_frames))
        lay.addWidget(label("Live view", "section"))
        self.seg_live = SegmentedControl([("live", "● Live"), ("stopped", "Stopped")], accessible_name="Live view")
        self.seg_live.buttons["live"].setToolTip("Stream frames from the selected camera (Ctrl+L)")
        self.seg_live.buttons["stopped"].setToolTip("Stop the stream; the camera idles and the last frame stays")
        lay.addWidget(self.seg_live)
        # objective and vessel readouts stay as labels (other code reads them) but are not shown: the checked
        # objective button and the vessel map already say the same (UI audit 2026-11-17)
        for name in ("objective", "vessel"):
            w, lab = kv(name.capitalize(), "—")
            setattr(self, f"lbl_{name}", lab)
            lay.addWidget(w)  # parented, so Qt keeps it
            w.setVisible(False)
        self.btn_vessel = QPushButton("Vessel expert…")
        self.btn_vessel.setIcon(icon("vessel"))
        lay.addWidget(self.btn_vessel)
        self.map = VesselMap()
        self.map.setMinimumHeight(160)
        self.map.setAccessibleName("Virtual vessel: click a well to centre it, drag the crosshair to move")
        lay.addWidget(self.map)
        self.lbl_pos = label("X — mm   Y — mm", "mono")
        self.lbl_well = label("", "caption", wrap=True)  # wraps instead of clipping in the narrow column (APP-10)
        lay.addWidget(self.lbl_pos)
        lay.addWidget(self.lbl_well)
        lay.addWidget(label("Stage", "section"))
        self.joy = Joystick()
        self.jog_speed = QComboBox()
        self.jog_speed.addItems(["Slow", "Medium", "Fast"])
        self.jog_speed.setCurrentIndex(1)
        self.jog_speed.setAccessibleName("Jog speed")
        self.jog_speed.setFixedWidth(92)  # sized to its longest item; never runs past the panel edge
        if getattr(self.session, "physical_stage", False):
            svc = self.session.telemetry[0] if self.session.telemetry else None
            if svc is not None and getattr(svc, "speed_control", False):  # ADR-0032, after Motor Gate 10
                self.jog_speed.setToolTip("Real stage speed: Slow 1 mm/s, Medium 4 mm/s, Fast 8.3 mm/s (the drives' "
                                          "power-up speed, restored when the app closes). Push the knob in one of 8 "
                                          "directions (up = Y+ away, right = X+); release to stop.")
                presets = ("slow", "medium", "fast")
                svc.speed_preset = presets[self.jog_speed.currentIndex()]
                self.jog_speed.currentIndexChanged.connect(lambda i, s=svc: setattr(s, "speed_preset", presets[i]))
            else:  # drive speed stays the power-up speed; nothing is written
                self.jog_speed.setEnabled(False)
                self.jog_speed.setToolTip("Real stage: moves at the drives' power-up speed (~8.3 mm/s); start the app "
                                          "with --speed-control (after Motor Gate 10) to choose a speed. Push the knob "
                                          "in one of 8 directions (up = Y+ away, right = X+); release to stop.")
        jl = QVBoxLayout()
        jl.addWidget(label("Speed", "rowlabel"))
        jl.addWidget(self.jog_speed)
        jl.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.joy)
        row.addLayout(jl)
        lay.addLayout(row)
        lay.addWidget(label("Objectives", "section"))
        self.obj_group = QButtonGroup(self)
        self.obj_group.setExclusive(True)
        orow = QHBoxLayout()
        orow.setSpacing(6)
        self.obj_buttons: dict[int, QPushButton] = {}
        for o in self.profile.objectives:
            b = QPushButton(o.short)
            b.setObjectName("objective")
            b.setCheckable(True)
            b.setEnabled(o.installed)
            tip = (f"Slot {o.slot}: {o.label or 'objective'}" + (f", NA {o.numerical_aperture}" if o.numerical_aperture
                                                                   else "") + f" — {o.provenance.label}  (Alt+{o.slot})")  # noqa: E501
            b.setToolTip(tip if o.installed else f"Slot {o.slot}: empty")
            b.setAccessibleName(f"Objective slot {o.slot} {o.label or 'empty'}")
            self.obj_group.addButton(b, o.slot)
            self.obj_buttons[o.slot] = b
            orow.addWidget(b)
        orow.addStretch(1)
        lay.addLayout(orow)
        lay.addStretch(1)
        return f

    def _center(self) -> QWidget:
        f, lay = panel(None, margins=10)
        self.toolbar = QWidget()
        self.toolbar.setMinimumWidth(1)  # explicit minimum: the toolbar never forces the window wider
        top = QHBoxLayout(self.toolbar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        self.lbl_view = label("Live", "title")
        top.addWidget(self.lbl_view)
        top.addStretch(1)
        self.btn_live = tool_button("Live", "live", checkable=True, tip="Live view (Ctrl+L)")
        self.btn_overlay = tool_button("Overlay", "zstack", tip="Overlay of the captured channels in memory")
        self.btn_fit = tool_button("Fit", "fit", tip="Zoom to fit (Ctrl+0)")
        self.btn_11 = tool_button("1:1", "actual", tip="Actual size (Ctrl+1)")
        self.btn_cross = tool_button("Crosshair", "crosshair", checkable=True)
        self.btn_scale = tool_button("Scale bar", "scalebar", checkable=True)
        self.btn_scale.setChecked(self.settings.basic.show_scale_bar)
        self.btn_clip = tool_button("Clipping", "clipping", checkable=True,
                                    tip="Mark pixels at the black point (blue) and white point (red)")
        self.btn_sat = tool_button("Saturation", "warning", checkable=True,
                                   tip="Highlight sensor-saturated pixels in red")
        self.btn_loupe = tool_button("Loupe", "loupe", checkable=True, tip="Magnified view and pixel value at the cursor")  # noqa: E501
        self.btn_hist = tool_button("Histogram", "histogram", checkable=True,
                                    tip="Show the levels histogram (black/white points) under the image")
        self.btn_hist.setChecked(self.settings.basic.show_histogram)
        self.btn_compare = tool_button("Compare", "compare",
                                       tip="Hold to see the raw frame mapped linearly over its full range (or hold B)")
        self.toolbuttons = [self.btn_live, self.btn_overlay, self.btn_fit, self.btn_11, self.btn_cross, self.btn_scale,
                            self.btn_clip, self.btn_sat, self.btn_loupe, self.btn_hist, self.btn_compare]
        for i, b in enumerate(self.toolbuttons):
            if i in (2, 4, 10):
                sep = QWidget()
                sep.setFixedWidth(8)
                top.addWidget(sep)
            top.addWidget(b)
        lay.addWidget(self.toolbar)
        self.view = ImageView()
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.show_scale_bar = self.btn_scale.isChecked()
        self.view.setAccessibleName("Image viewport")
        lay.addWidget(self.view, 1)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.hist = LevelsHistogram()
        self.hist.setFixedWidth(320)
        bottom.addWidget(self.hist)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.chk_sci = QCheckBox("Scientific display (linear only)")
        self.chk_sci.setChecked(self.display.scientific)
        self.chk_sci.setToolTip("Only whole-image linear mappings; the display log is saved with every image")
        # gamma was removed (operator, 2026-11-18): the live display is always linear, so the Scientific switch has
        # nothing to guard on this page and stays hidden (View menu / Settings keep it)
        self.chk_sci.setVisible(False)
        self.btn_auto = QPushButton("Auto range")
        self.btn_auto.setToolTip("Return to automatic black/white points (or double-click the histogram)")
        info.addWidget(self.chk_sci)
        self.lbl_log = label("", "caption", wrap=True)
        self.lbl_frame = label("—", "mono", wrap=True)  # wraps at narrow widths instead of being cut off
        self.lbl_hover = label("", "mono")
        self.lbl_task = label("", "caption")
        for lab in (self.lbl_log, self.lbl_frame, self.lbl_hover, self.lbl_task):
            lab.setMinimumWidth(1)  # long readouts elide/wrap instead of widening the window
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row = QHBoxLayout()
        row.addWidget(self.btn_auto)
        row.addWidget(self.lbl_hover, 1)
        info.addLayout(row)
        # the display-mapping sentence and the per-frame debug line are diagnostics, not operator information: they
        # live in the histogram's tooltip now (UI audit 2026-11-17)
        for lab in (self.lbl_log, self.lbl_frame):
            lab.setVisible(False)
            info.addWidget(lab)
        info.addWidget(self.lbl_task)
        bottom.addLayout(info, 1)
        lay.addLayout(bottom)
        self._show_histogram(self.btn_hist.isChecked())
        return f

    def _show_histogram(self, on: bool) -> None:
        self.settings.basic.show_histogram = on
        self.hist.setVisible(on)
        self.btn_auto.setVisible(on)

    def _right(self) -> QWidget:
        f, lay = panel("Light & focus")
        f.setMinimumWidth(286)  # inside the column's scroll area (scroll bar 10 px)
        lay.addWidget(label("Light channels", "section"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.ch_buttons: dict[str, ChannelButton] = {}
        chans = [c for c in self.profile.channels if c.installed]
        for i, c in enumerate(chans):
            b = ChannelButton(c.slot, c.display, channel_color(c.slot, self.profile, self.settings))
            b.setAccessibleName(f"Channel {c.display}")
            self.ch_buttons[c.slot] = b
            grid.addWidget(b, i // 4, i % 4)
        lay.addLayout(grid)
        self.btn_power = QPushButton("Light off")
        self.btn_power.setCheckable(True)
        self.btn_power.setChecked(False)  # dark at launch: the light goes on only when the operator asks
        self.btn_power.setIcon(icon("light"))
        self.btn_power.setToolTip("LED of the selected channel on/off (Ctrl+Shift+L). Clicking a channel turns its "
                                  "light on; clicking the lit channel again turns it off")
        self.btn_lc = QPushButton("Light and camera…")
        row = QHBoxLayout()
        row.addWidget(self.btn_power)
        row.addWidget(self.btn_lc)
        lay.addLayout(row)
        self.light = ParamRow("Light", 0, 100, 40, step=1, unit="%", default=40,
                              tooltip="One control for LED → exposure → gain (OpenEVOS mapping)")
        lay.addWidget(self.light)
        self.chk_actual = QCheckBox("Actual mode (LED, exposure, gain)")
        lay.addWidget(self.chk_actual)
        self.led = ParamRow("LED", 0, 100, 40, step=1, unit="%")
        self.exp = ParamRow("Exposure", 0.1, 5000, 40, step=1, decimals=1, unit="ms", slider=False)
        self.gain_row = ParamRow("Gain", 1, 64, 1, step=0.5, decimals=1, unit="×", slider=False)
        for w in (self.led, self.exp, self.gain_row):
            lay.addWidget(w)
        self.lbl_actual = label("", "mono")
        lay.addWidget(self.lbl_actual)
        self.lbl_device = label("", "mono", wrap=True)
        self.lbl_device.setToolTip("Device readback: what the LED board and the active camera report now "
                                   "(the rows above are the stored settings for this channel)")
        self.lbl_device.setAccessibleName("Light and camera device state")
        self.lbl_device.setVisible(False)  # the readback repeats the controls above: shown as the Light row's tooltip
        lay.addWidget(self.lbl_device)

        # Z focus: tape scale + focus pad (design handoff 2026-11-18); moves still go through ctl.move_focus
        self.focus = ZFocusPanel()
        self.focus.setAccessibleName("Z focus")
        self.btn_snap, self.btn_find, self.btn_fine = self.focus.btn_snap, self.focus.btn_find, self.focus.btn_fine
        self._z_goal: float | None = None  # last requested focus target while a focus move is in flight
        self._z_submitting = False
        lay.addWidget(self.focus, 1)
        return f

    def _capture_block(self) -> QWidget:
        """Capture + hot buttons, pinned below the scrolling Light & focus column so they never leave the screen
        (audit APP-06: at 1280 x 800 they were below the fold)."""
        f, lay = panel(margins=10, spacing=6)
        lay.addWidget(label("Capture", "section"))
        self.btn_capture = QPushButton("Capture")
        self.btn_capture.setObjectName("primary")
        self.btn_capture.setIcon(icon("capture", theme.ACCENT_ON))
        self.btn_capture.setMinimumHeight(38)
        self.btn_capture.setToolTip("Capture the selected channel (Ctrl+Enter)")
        lay.addWidget(self.btn_capture)
        hg = QHBoxLayout()  # one row: a hidden hot button (the duplicate Capture) leaves no hole
        hg.setSpacing(6)
        self.hot: list[QPushButton] = []
        for _i in range(4):
            b = QPushButton("—")
            b.setMinimumHeight(32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setMinimumWidth(1)
            b.setStyleSheet("padding-left: 4px; padding-right: 4px;")  # 'Capture All' fits the ~90 px button
            self.hot.append(b)
            hg.addWidget(b, 1)
        lay.addLayout(hg)
        return f

    # ================================================================== wiring
    def _wire(self) -> None:
        m = self.model
        m.frame.connect(self._on_frame)
        m.device_changed.connect(lambda *_: self._refresh_state())
        m.device_changed.connect(lambda *_: self._refresh_device())
        m.command_event.connect(lambda _e: self._refresh_device())
        m.vessel_changed.connect(self._on_vessel)
        m.buffers_changed.connect(self._refresh_thumbnails)
        m.settings_changed.connect(self._refresh_settings)
        m.tasks.busy_changed.connect(self._on_busy)
        m.tasks.progress.connect(lambda n, i, t, msg: self.lbl_task.setText(f"{n}: {msg}"))
        self.btn_vessel.clicked.connect(self._vessel_expert)
        self.map.move_requested.connect(self._goto_vessel)
        self.joy.jog.connect(self._jog)
        self.joy.released.connect(self._joy_released)
        self.obj_group.idClicked.connect(self._objective)
        for b in self.ch_buttons.values():
            b.clicked.connect(self._channel_clicked)
        self.btn_power.toggled.connect(self._power)
        self.light.valueChanged.connect(self._light_slider)
        for w in (self.led, self.exp, self.gain_row):
            w.committed.connect(self._actual_changed)
        self.chk_actual.toggled.connect(self._set_actual)
        self.btn_lc.clicked.connect(self._light_camera)
        self.focus.step_requested.connect(self._focus_step)
        self.focus.hold_released.connect(self._focus_hold_end)
        self.focus.limit_set_requested.connect(self._set_focus_limit)
        self.focus.limit_clear_requested.connect(self._clear_focus_limit)
        self.btn_snap.clicked.connect(self._snap)
        self.btn_find.clicked.connect(lambda: self._autofocus(True))
        self.btn_fine.clicked.connect(lambda: self._autofocus(False))
        self.btn_capture.clicked.connect(self.capture_current)
        for i, b in enumerate(self.hot):
            b.clicked.connect(lambda _=False, k=i: self._hot(k))
        self.btn_live.clicked.connect(self._live_clicked)
        self.seg_camera.activated.connect(self._camera_segment)
        self.seg_live.activated.connect(lambda k: self.go_live() if k == "live" else self.stop_live())
        self.btn_overlay.clicked.connect(self.show_overlay)
        self.btn_fit.clicked.connect(self.view.fit)
        self.btn_11.clicked.connect(self.view.one_to_one)
        self.btn_cross.toggled.connect(lambda on: (setattr(self.view, "show_crosshair", on), self.view.viewport().update()))  # noqa: E501
        self.btn_scale.toggled.connect(lambda on: (setattr(self.view, "show_scale_bar", on), self.view.viewport().update()))  # noqa: E501
        self.btn_sat.toggled.connect(self._toggle_saturation)
        self.btn_clip.toggled.connect(lambda _on: self._redraw())
        self.btn_loupe.toggled.connect(lambda on: (setattr(self.view, "loupe", on), self.view.viewport().update()))
        self.btn_compare.pressed.connect(lambda: self._compare(True))
        self.btn_compare.released.connect(lambda: self._compare(False))
        self.view.compare_changed.connect(self._compare)
        self.view.hovered.connect(self._hover)
        self.hist.levelsChanged.connect(self._levels)
        self.hist.autoRequested.connect(self._auto_levels)
        self.btn_auto.clicked.connect(self._auto_levels)
        self.chk_sci.toggled.connect(self._set_scientific)
        self.btn_hist.toggled.connect(self._show_histogram)

    def _initial_state(self) -> None:
        self._on_vessel()
        self._refresh_settings()
        self._select_channel(self.channel, initial=True)
        self._set_actual(False)
        self.go_live()

    # ================================================================== state refresh
    @property
    def role(self) -> CameraRole:
        return self.settings.active_camera

    def current_channel_settings(self) -> ChannelSettings:
        return self.settings.channel(self.role, self.channel)

    def channel_label(self) -> str:
        c = self.profile.channel(self.channel)
        return c.display if c else self.channel

    def _refresh_settings(self) -> None:
        cam = self.profile.camera(self.role)
        self.lbl_camera.setText(cam.display_name if cam else "—")
        self.seg_camera.set_current(self.role.value)
        self.btn_sat.blockSignals(True)
        self.btn_sat.setChecked(self.settings.camera_settings(self.role).highlight_saturated)
        self.btn_sat.blockSignals(False)
        for slot, b in self.ch_buttons.items():
            ch = self.profile.channel(slot)
            ok = bool(cam and ch and cam.supports(ch))
            b.color = channel_color(slot, self.profile, self.settings)
            b.set_available(ok, "" if ok else f"{ch.display}: the colour camera is brightfield/phase only")
        for i, action in enumerate(self.settings.camera_settings(self.role).hot_buttons):
            self.hot[i].setText(action.label)
            self.hot[i].setEnabled(action is not HotButtonAction.NONE and not self.model.tasks.busy)
            self.hot[i].setProperty("action", action.value)
            # the big primary Capture button is right above: a second "Capture" hot button only adds noise
            self.hot[i].setVisible(action is not HotButtonAction.CAPTURE)
        self._apply_limits()
        cs = self._clamp_to_device(self.current_channel_settings())
        self.light.set_value(cs.light_slider)
        self.led.set_value(cs.led_percent)
        self.exp.set_value(cs.exposure_ms)
        self.gain_row.set_value(cs.gain)
        self._update_light_labels()
        self._refresh_device(sync=False)

    # ------------------------------------------------------------------ device limits and readback
    def _apply_limits(self) -> None:
        """Exposure, gain and LED ranges come from the capability records the router validates against,
        so the controls never offer a value the safety path would reject."""
        lim = ds.camera_limits(self.session, self.role)
        if lim is not None:
            self.exp.set_range(*lim.exposure_ms)
            self.gain_row.set_range(*lim.gain)
            fixed = lim.gain_fixed
            self.gain_row.box.setReadOnly(fixed)
            if self.gain_row.slider:
                self.gain_row.slider.setEnabled(not fixed)
            self.gain_row.setToolTip(f"This camera accepts gain {lim.gain[0]:g}× only" if fixed else
                                     f"Gain {lim.gain[0]:g}–{lim.gain[1]:g}× (device range)")
            self.exp.setToolTip(f"Exposure {lim.exposure_ms[0]:g}–{lim.exposure_ms[1]:g} ms (device range)")
        led = ds.light_limits(self.session, self.channel)
        if led is not None:
            self.led.set_range(*led)

    def _clamp_to_device(self, cs: ChannelSettings) -> ChannelSettings:
        """Clamp stored exposure/gain/LED to the active devices' ranges and store the result, so the
        live view and a capture use the same, acceptable values (the light-slider mapping goes to 1000 ms
        and 8x; the uEye colour camera accepts 1x only)."""
        lim = ds.camera_limits(self.session, self.role)
        led = ds.light_limits(self.session, self.channel)
        upd = {}
        if lim is not None:
            e, g = lim.clamp_exposure(cs.exposure_ms), lim.clamp_gain(cs.gain)
            if abs(e - cs.exposure_ms) > 1e-9:
                upd["exposure_ms"] = e
            if abs(g - cs.gain) > 1e-9:
                upd["gain"] = g
        if led is not None:
            p = min(led[1], max(led[0], cs.led_percent))
            if abs(p - cs.led_percent) > 1e-9:
                upd["led_percent"] = p
        if not upd:
            return cs
        cs = cs.model_copy(update=upd)
        self.settings.camera_settings(self.role).channels[self.channel] = cs
        self.model.add_log("info", f"{self.channel_label()}: settings limited to the device range "
                                   + ", ".join(f"{k}={v:g}" for k, v in upd.items()))
        return cs

    def _refresh_device(self, sync: bool = True) -> None:
        """Show what the devices report, and keep the Power and Live toggles in step with them.
        `sync=False` only updates the readback (used while the page itself is about to send commands)."""
        light = ds.read_light(self.session)
        cam = ds.read_camera(self.session, self.role)
        parts = []
        if light is not None:
            chp = self.profile.channel(light.channel) if light.channel else None
            parts.append("Light: " + light.summary(chp.display if chp else None))
        if cam is not None:
            parts.append("Camera: " + cam.summary())
        self.lbl_device.setText("\n".join(parts) if parts else "Device state unavailable")
        self.light.setToolTip("Device readback now:\n" + self.lbl_device.text())
        if not sync or not self._light_applied or self.model.tasks.busy or self.coalesce.pending():
            return  # a command or workflow is changing the devices; sync once it has settled
        if (light is not None and light.enabled is not None and light.channel == self.channel
                and light.enabled != self.btn_power.isChecked()):
            self.btn_power.blockSignals(True)
            self.btn_power.setChecked(light.enabled)
            self.btn_power.blockSignals(False)
            self.btn_power.setText("Light on" if light.enabled else "Light off")
            self.session.capture.light_power = light.enabled
        if cam is not None:
            self.seg_live.set_current("live" if cam.streaming else "stopped")
            if self.view_mode in ("live", "stopped"):
                self.btn_live.setChecked(cam.streaming)

    def _update_light_labels(self) -> None:
        cs = self.current_channel_settings()
        self.lbl_actual.setText(f"LED {cs.led_percent:.0f} %  ·  {cs.exposure_ms:g} ms  ·  gain {cs.gain:g}×")

    def _refresh_state(self) -> None:
        xy = self.ctl.stage_xy()
        if xy is not None and getattr(self.session, "physical_stage", False):
            # the vessel map's geometry belongs to the simulator; no vessel<->stage mapping is calibrated
            # for the physical stage, so show raw stage coordinates and say so (never a fake well)
            self.lbl_pos.setText(f"Stage X {xy[0] / 1000:.3f}  Y {xy[1] / 1000:.3f} mm")  # fits 1280 px (APP-10)
            self.lbl_well.setText("Vessel map: simulator's, not calibrated")
            self.lbl_well.setToolTip("Physical stage: the vessel ↔ stage mapping is not calibrated, so the map above is "  # noqa: E501
                                     "the simulator's geometry and the position shows raw stage coordinates")
        elif xy is not None:
            vx, vy = self.session.stage_map.to_vessel(*xy)
            self.map.position_mm = (vx, vy)
            reg = self.session.vessel.region_at(vx, vy)
            self.lbl_pos.setText(f"X {vx:7.3f} mm   Y {vy:7.3f} mm")
            self.lbl_well.setText((f"In {reg.region_id}" if reg else "Between regions")
                                  + f"  ·  stage {xy[0]:.0f}, {xy[1]:.0f} µm")
        slot = self.ctl.objective_slot()
        obj = self.profile.objective(slot) if slot else None
        if obj:
            self.lbl_objective.setText(f"{obj.short} · {obj.label.split(' ', 1)[-1] if obj.label else ''}")
            b = self.obj_buttons.get(slot)
            if b and not b.isChecked():
                b.setChecked(True)
            self.map.fov_mm = self._fov_mm()
            self.view.pixel_um = self.session.pixel_size_um(self.role, slot)
            # scale bar only from a measured pixel size (Calibration › pixel size), never the nominal fallback
            self.view.pixel_calibrated = bool(
                self.session.calibration.pixel_size_um.get(self.role.value, {}).get(slot))
        self._refresh_focus()
        self.map.update()

    def _focus_state(self, z: float | None) -> tuple[str, str, str | None]:
        """(panel state, status line, pad direction while moving) from the focus and turret device snapshots."""
        snap = self.ctl.snapshot(self.profile.focus_device)
        tur = self.ctl.snapshot(self.profile.turret_device) if self.profile.turret_device else None
        moving = (snap is not None and snap.activity is Activity.MOVING) or self.coalesce.busy("focus")
        direction = None
        if moving and self._z_goal is not None and z is not None and abs(self._z_goal - z) > 1e-6:
            direction = "up" if self._z_goal > z else "down"
        if snap is None or snap.connection is not Connection.CONNECTED:
            return "locked", "Focus drive not connected", None
        if snap.recovery_required or snap.health is Health.FAULT:
            return "fault", f"Stop sent · {snap.note or 'recover Z'} in Service › Hardware", None
        if dict(snap.attributes).get("homed") is False:
            return "locked", "Not homed · Z refuses moves until it is homed", None
        if tur is not None and (tur.activity is Activity.MOVING or self.coalesce.busy("turret")):
            return "objective", "Objective change · Z retracts to 0, controls held", direction
        if moving:
            goal = f" to {fmt_um(self._z_goal)} µm" if self._z_goal is not None else ""
            return "moving", f"Moving{goal}", direction
        if snap.backend_kind is BackendKind.SIMULATED:
            return "simulated", "Simulated drive · idle", None
        return "ready", "Homed · idle", None

    def _refresh_focus(self) -> None:
        z = self.ctl.focus_z()
        lim = self.ctl.axis_limits(self.profile.focus_device, "z")
        state, status, direction = self._focus_state(z)
        # the move finished: the next step starts from the readback. Not while a step is being submitted (the
        # router's first state event arrives inside that call, before the Coalescer records the command)
        if (state not in ("moving", "objective") and not self._z_submitting and not self.coalesce.busy("focus")
                and not self.coalesce.waiting("focus")):
            self._z_goal = None
        self.focus.set_state(state, status, direction)
        self.focus.set_values(z, lim[1] if lim else None, self.session.vessel.focus_nominal_um,
                              self.session.capture.last_capture_z)
        name, safe = self._focus_limit()
        self.focus.set_limit(safe, name, z)

    # ------------------------------------------------------------------ per-objective safe height
    def _focus_limit(self) -> tuple[str, float | None]:
        """(objective name, safe height in µm or None) for the objective in the light path."""
        slot = self.ctl.objective_slot()
        obj = self.profile.objective(slot) if slot else None
        name = obj.short if obj else (f"position {slot}" if slot else "")
        return name, self.settings.basic.focus_limit_um.get(str(slot)) if slot else None

    def _set_focus_limit(self) -> None:
        slot, z = self.ctl.objective_slot(), self.ctl.focus_z()
        if not slot or z is None:
            self.model.add_log("warn", "Z limit not set: no objective position or Z readback")
            return
        self.settings.basic.focus_limit_um[str(slot)] = round(z, 2)
        self.session.save_settings()
        self.model.add_log("info", f"Z safe height for {self._focus_limit()[0]} set to {fmt_um(z)} µm")
        self._refresh_focus()

    def _clear_focus_limit(self, slot: int | None = None) -> None:
        slot = slot or self.ctl.objective_slot()
        if slot and self.settings.basic.focus_limit_um.pop(str(slot), None) is not None:
            self.session.save_settings()
            obj = self.profile.objective(slot)
            self.model.add_log("info", f"Z safe height for {obj.short if obj else slot} cleared")
        self._refresh_focus()

    def _fov_mm(self):
        from openevos.acquisition.scan_plan import field_of_view_mm

        slot = self.ctl.objective_slot()
        try:
            return field_of_view_mm(self.profile, self.role, slot) if slot else None
        except ValueError:
            return None

    def _on_vessel(self) -> None:
        v = self.session.vessel
        self.map.set_vessel(v)
        lim_x = self.ctl.axis_limits(self.profile.stage_device, "x")
        lim_y = self.ctl.axis_limits(self.profile.stage_device, "y")
        if lim_x and lim_y:
            x0, y0 = self.session.stage_map.to_vessel(lim_x[0], lim_y[0])
            x1, y1 = self.session.stage_map.to_vessel(lim_x[1], lim_y[1])
            self.map.reachable_mm = (x0, y0, x1, y1)
        self.lbl_vessel.setText(v.display_name)
        self._refresh_state()

    def _refresh_thumbnails(self) -> None:
        bufs = self.session.capture.buffers.all()
        for slot, b in self.ch_buttons.items():
            img = bufs.get(slot)
            b.set_thumbnail(self._render(img.data, slot, img.max_value, overlay=False) if img is not None else None)

    def _on_busy(self, busy: bool) -> None:
        # operator lock-out while a workflow owns the instrument (scan, time-lapse, autofocus...)
        for w in (self.btn_capture, self.btn_find, self.btn_fine, self.btn_snap, *self.hot, self.joy, self.map,
                  self.focus, self.btn_vessel, self.btn_lc, self.light, self.btn_power,
                  self.led, self.exp, self.gain_row, self.chk_actual, self.seg_camera, self.seg_live,
                  *self.obj_buttons.values(), *self.ch_buttons.values()):
            w.setEnabled(not busy)
        if not busy:
            for slot, b in self.obj_buttons.items():
                o = self.profile.objective(slot)
                b.setEnabled(bool(o and o.installed))
            if self.profile.camera(CameraRole.COLOR) is None:
                self.seg_camera.set_option_enabled("color", False)
            self._set_actual(self.actual_mode)
            self._refresh_settings()
            self.lbl_task.setText("")

    # ================================================================== display pipeline
    def _render(self, raw: np.ndarray, slot: str | None, max_value: int, *, rng=None, overlay: bool = True) -> np.ndarray:  # noqa: E501
        if raw.ndim == 3:
            c = self.settings.color
            rgb = adjust_color(raw, c.brightness, c.contrast, c.saturation, c.hue)
            if overlay and c.highlight_saturated:
                rgb = highlight_saturated(rgb, raw, 255)
            return rgb
        ds = self.display if rng is None else DisplaySettings(
            auto=False, lo=rng[0], hi=rng[1], gamma=self.display.gamma, scientific=self.display.scientific)
        g8 = map_to_8bit(raw, ds)
        ch = self.profile.channel(slot) if slot else None
        if ch is not None and ch.kind is ChannelKind.FLUORESCENCE and self.settings.mono.pseudocolor_enabled:
            rgb = tint(g8, channel_color(slot, self.profile, self.settings))
        else:
            rgb = gray_rgb(g8)
        if overlay and self.btn_clip.isChecked():
            rgb = clipping_overlay(rgb, raw, ds)
        if overlay and self.settings.mono.highlight_saturated:
            rgb = highlight_saturated(rgb, raw, max_value)
        return rgb

    def _range_for(self, raw: np.ndarray, live: bool) -> tuple[float, float]:
        if not self.display.auto:
            return self.display.lo, self.display.hi
        return self._smooth.update(raw) if live else self.display.resolved(raw)

    def _present(self, raw: np.ndarray, slot: str | None, max_value: int, *, live: bool) -> None:
        self.view_raw, self._view_slot, self._view_max = raw, slot, max_value
        if self._comparing and raw.ndim == 2:
            full = DisplaySettings(auto=False, lo=0, hi=max_value, scientific=True)
            self.view.set_image(gray_rgb(map_to_8bit(raw, full)), raw)
            self.view.badge = f"RAW • LINEAR 0–{max_value}"
            self.view.badge_color = theme.AMBER
            return
        rng = self._range_for(raw, live) if raw.ndim == 2 else None
        rgb = self._render(raw, slot, max_value, rng=rng)
        if not live:
            self.view_image = rgb
        self.view.set_image(rgb, raw)
        if raw.ndim == 2:
            lo, hi = rng
            counts, _ = histogram(raw, 128, max_value)
            ds = DisplaySettings(auto=False, lo=lo, hi=hi, scientific=True)
            ch = self.profile.channel(slot) if slot else None
            self.hist.set_data(counts, max_value, lo, hi, self.display.auto,
                               channel_color(slot, self.profile, self.settings) if ch else theme.ACCENT,
                               clipped_fractions(raw, ds))
            self.lbl_log.setText(self.display_log(raw, max_value)[0])
            self.btn_auto.setEnabled(True)
            hint = "Drag the triangles to set black and white points · double-click for auto range"
        else:
            # colour camera: one R, G and B curve of the raw 8-bit frame; clip = pixels with any channel at 0 / 255
            self.hist.set_rgb([histogram(raw[..., k], 128, 255)[0] for k in range(3)],
                              (float((raw == 0).any(axis=2).mean()), float((raw == 255).any(axis=2).mean())))
            self.lbl_log.setText("Colour camera: colour adjustments from Settings › Color camera (8-bit)")
            self.btn_auto.setEnabled(False)  # no black/white points to reset on the colour camera
            hint = "Red, green and blue channels of the colour camera frame (8-bit)"
        self.hist.setToolTip(f"{hint}\n\n{self.lbl_log.text()}\n{self.lbl_frame.text()}")

    def display_log(self, raw: np.ndarray | None = None, max_value: int | None = None) -> list[str]:
        bits = int(np.log2(max_value + 1)) if max_value else None
        return self.display.log_lines(raw, bits)

    def _redraw(self) -> None:
        if self.view_mode != "live" and self.view_raw is not None:
            self._present(self.view_raw, self._view_slot, self._view_max, live=False)
        elif self.view_mode == "overlay":
            self.show_overlay()

    def _levels(self, lo: float, hi: float) -> None:
        self.display = self.display.with_levels(lo, hi)
        self._redraw()

    def _auto_levels(self) -> None:
        self.display = DisplaySettings(auto=True, gamma=self.display.gamma, scientific=self.display.scientific)
        self._smooth.reset()
        self._redraw()

    def _set_scientific(self, on: bool) -> None:
        self.settings.basic.scientific_display = on
        self.display = DisplaySettings(auto=self.display.auto, lo=self.display.lo, hi=self.display.hi, scientific=on)
        self._redraw()

    def toggle_scientific(self) -> None:
        self.chk_sci.toggle()

    def _compare(self, on: bool) -> None:
        self._comparing = on
        if self.view_raw is not None:
            self._present(self.view_raw, self._view_slot, self._view_max, live=self.view_mode == "live")

    def _hover(self, x: int, y: int, value: str) -> None:
        px = self.view.pixel_um
        if value:
            pos = f"x {x}  y {y}" + (f"  ({x * px:.1f}, {y * px:.1f} µm)" if px else "")
            self.lbl_hover.setText(f"{pos}  ·  {value}")
        else:
            self.lbl_hover.setText("")

    # ================================================================== frames
    def _on_frame(self, frame) -> None:
        cam = self.profile.camera(self.role)
        if cam is None or frame.metadata.device_id != cam.device_id:
            return
        if frame.metadata.acquisition_id is not None or self.view_mode != "live":
            return
        raw = frame.data
        slot = frame.metadata.channel
        if slot != self._last_frame_slot:
            self._smooth.reset()
            self._last_frame_slot = slot
        maxv = (1 << (cam.native_bit_depth or 12)) - 1 if raw.ndim == 2 else 255
        self._present(raw, slot, maxv, live=True)
        ch = self.profile.channel(slot) if slot else None
        label_ch = ch.display if ch else (slot or "—")
        md = frame.metadata
        if not self._comparing:
            self.view.badge = f"{label_ch} • LIVE • {'SIMULATED' if md.synthetic else 'CAMERA'}"
            self.view.badge_color = theme.SIM if md.synthetic else theme.OK
        extra = dict(md.extra)
        self.lbl_frame.setText(f"{md.device_id} #{md.frame_index} · {label_ch} · {md.exposure_ms:g} ms · "
                               f"gain {extra.get('gain', '?')} · LED {'on' if self.ctl.light.enabled else 'off'} · "
                               f"preview drops {self.session.core.transport.preview.stats['dropped']}")

    # ================================================================== actions: motion
    def _vessel_expert(self) -> None:
        dlg = VesselExpertDialog(self.session.vessels, self.session.vessel.vessel_id, self)
        if dlg.exec() == QDialog.Accepted:
            self.session.select_vessel(dlg.vessel_id)
            self.model.add_log("info", f"vessel: {self.session.vessel.display_name}")

    def _goto_vessel(self, x_mm: float, y_mm: float) -> None:
        if getattr(self.session, "physical_stage", False):
            self.model.add_log("warn", "vessel map moves are disabled for the physical stage: its vessel mapping "
                                       "is not calibrated (use the jog control)")
            return
        sx, sy = self.session.stage_map.to_stage(x_mm, y_mm)
        self.coalesce.request("stage", lambda: self.ctl.move_stage(sx, sy))

    def _jog(self, vx: float, vy: float) -> None:
        pc = getattr(self.model, "physical_control", None)
        if getattr(self.session, "physical_stage", False) and pc is not None:
            # Real stage: one continuous move on the dominant axis while held, controlled stop on release
            # (the same hold-to-move path as the Hardware page). Up = Y+ (away from you), right = X+.
            d = joystick_direction(vx, vy)
            if d is None or d == getattr(self, "_phys_joy", None):
                return
            if getattr(self, "_phys_joy", None) is not None:  # knob moved to a new direction while held:
                pc._phys_hold_end()  # stop, then the new direction starts as soon as the stage is at rest
            self._phys_joy = d
            pc._phys_hold_start(*d)
            return
        if self.coalesce.busy("stage"):
            return
        fov = self._fov_mm() or (0.5, 0.4)
        k = (0.25, 0.6, 1.5)[self.jog_speed.currentIndex()]
        dx, dy = vx * fov[0] * 1000 * k, vy * fov[1] * 1000 * k
        self.coalesce.request("stage", lambda: self.ctl.move_stage_by(dx, dy))

    def _joy_released(self) -> None:
        if getattr(self, "_phys_joy", None) is not None:
            self._phys_joy = None
            pc = getattr(self.model, "physical_control", None)
            if pc is not None:
                pc._phys_hold_end()

    def _objective(self, slot: int) -> None:
        if getattr(self.session, "physical_stage", False) and self.ctl.objective_slot() == 0:
            self._recover_turret(slot)
            return
        if getattr(self.session, "physical_stage", False) and outside_turret_box(self.ctl.stage_xy()):
            self._centre_then_objective(slot)
            return
        self.coalesce.request("turret", lambda: self.session.change_objective(slot))

    def _recover_turret(self, slot: int) -> None:
        """The turret reads 0, between positions (a stalled or stopped turn). The drive's own GOTO_POS turns it one way
        until its sensor shows `slot` and switches it off by itself if the sensor stops changing; the service retracts
        Z to 0 first and refuses unless the program is idle and the stage is centred. The stage is not moved here: with
        an objective part-way through its swing, the operator jogs it into the centre first (operator 2026-11-19:
        "rotate the motor at the pos")."""
        from PySide6.QtWidgets import QMessageBox

        o = self.profile.objective(slot)
        name = o.label if o and o.label else f"slot {slot}"
        (xlo, xhi), (ylo, yhi) = TURRET_STAGE_BOX_MM["X"], TURRET_STAGE_BOX_MM["Y"]
        if outside_turret_box(self.ctl.stage_xy()):
            x, y = self.ctl.stage_xy() or (0.0, 0.0)
            QMessageBox.information(self, "Turret between positions",
                                    f"The turret is between objectives. To turn it onto {name}, first jog the stage "
                                    f"into the turret's clear area (X {xlo:g}–{xhi:g}, Y {ylo:g}–{yhi:g} mm; it is at "
                                    f"X {x / 1000:.1f}, Y {y / 1000:.1f} mm), slowly and watching the objectives.")
            return
        if QMessageBox.question(
                self, "Turret between positions",
                f"The turret is between objectives. Turn it onto {name}?\n\nZ retracts to 0 first. The turret turns "
                "one way (position numbers go down) until its sensor shows the objective, up to one full turn. If it "
                "stalls, the drive switches the motor off by itself.") != QMessageBox.StandardButton.Yes:
            return
        self.model.add_log("warn", f"Turret recovery: turning from between positions onto {name}")
        self.coalesce.request("turret", lambda: self.session.change_objective(slot))

    def _in_range(self, axis: str, v: float) -> float:
        lim = self.ctl.axis_limits(self.profile.stage_device, axis)
        if lim is None:
            return v
        lo, hi = lim
        return lo if lo - HOME_SLACK_UM <= v < lo else hi if hi < v <= hi + HOME_SLACK_UM else v

    def _centre_then_objective(self, slot: int) -> None:
        """The turret turns only with the stage over its opening (Gate 19: with Z at 0 and the stage at 0, 0 an
        objective struck the stage). So: centre the stage (X, then Y; never together, ADR-0040), turn, and take the
        stage back to where it was (Y, then X). Only the swing needs the centre; the turned objective sits in the light
        path like any other. Z stays where the turret left it (retracted): going back up under a different objective
        could reach the vessel. Operator 2026-11-19: "auto center and change, then again reach my last position".

        The stage goes back ONLY after a verified turn. A failed, refused or timed-out turn leaves the stage centred:
        the turret may still be swinging (2026-11-18 18:33: a 15 s timeout ended the command mid-turn and the old
        code moved the stage back 5 s later)."""
        # nothing moves unless the turn can happen: a turret off its positions (2026-11-18 18:56: the sensor read
        # 42214 after a power cycle) or a locked stage would leave the stage parked part-way for nothing
        cur = self.ctl.objective_slot()
        if cur is None or self.profile.objective(int(cur)) is None:
            self.model.add_log("warn", f"Objective change refused: the turret reads position {cur}, not an objective "
                                       "(its power-up homing may not have finished); the stage was not moved")
            return
        snap = self.session.core.store.get(self.profile.stage_device)
        if snap is not None and snap.health.value != "normal":
            why = snap.attr("drift_lock_reason") or snap.health.value
            self.model.add_log("warn", f"Objective change refused: the stage is not ready ({why}); the stage was not "
                                       "moved")
            return
        x, y = self.ctl.stage_xy() or (0.0, 0.0)
        # right after power-up homing an axis reads a count or so below 0 (2026-11-18: Y -0.44 µm), which the range
        # check refuses as a target; hold such an axis at the edge instead (more than HOME_SLACK_UM out: refused)
        x, y = self._in_range("x", x), self._in_range("y", y)
        cx, cy = TURRET_CENTRE_MM["X"] * 1000.0, TURRET_CENTRE_MM["Y"] * 1000.0
        o = self.profile.objective(slot)
        name = o.label if o and o.label else f"slot {slot}"
        ctl, session, log = self.ctl, self.session, self.model.add_log

        def go() -> str:
            ctl.run(ctl.move_stage(cx, y, timeout_s=30.0), timeout=40.0)  # X alone
            ctl.run(ctl.move_stage(cx, cy, timeout_s=30.0), timeout=40.0)  # then Y alone
            try:
                ctl.run(session.change_objective(slot), timeout=TURRET_TIMEOUT_S + 30.0)
            except Exception as exc:
                raise RuntimeError(f"{exc}; the stage stays centred (the turret may not be at rest)") from exc
            if ctl.objective_slot() != slot:
                raise RuntimeError(f"the turret reads slot {ctl.objective_slot()}, not {slot}; the stage stays centred")
            ctl.run(ctl.move_stage(cx, y, timeout_s=30.0), timeout=40.0)  # back: Y alone
            ctl.run(ctl.move_stage(x, y, timeout_s=30.0), timeout=40.0)  # then X alone
            return name

        log("info", f"{name}: centring the stage for the turret (X {cx / 1000:g}, then Y {cy / 1000:g} mm); it returns "
                    f"to X {x / 1000:.1f}, Y {y / 1000:.1f} mm after the turn")
        self._run(f"Change to {name}", go,
                  on_done=lambda n: log("info", f"{n} in place; stage back at X {x / 1000:.1f}, Y {y / 1000:.1f} mm "
                                                "(Z retracted: refocus)"))

    def _move_z(self, z: float, *, confirmed: bool = False) -> None:
        self.coalesce.request("focus", lambda: self.ctl.move_focus(z, confirmed=confirmed))

    def undo_focus_nominal(self) -> None:
        last = getattr(self, "_last_nominal", None)
        if not last or last[0] != self.session.vessel.vessel_id:
            self.model.add_log("info", "Undo focus nominal: nothing to undo for this vessel")
            return
        self.session.set_focus_nominal(last[1])
        self._last_nominal = None
        self.model.add_log("info", f"focus nominal for {self.session.vessel.display_name} restored to {last[1]:.1f} µm")
        self._refresh_state()

    def _focus_hold_end(self) -> None:
        """The operator let go of a held pad half or arrow key: drop the queued step and cancel the running Z move.
        The drive's own stop (Gate 18's ESTATE 6) halts Z where it is, instead of finishing the steps queued during
        the hold (operator, 2026-11-18: "I keep stuck even after I leave the button")."""
        self.coalesce.drop("focus")
        if self.coalesce.cancel("focus"):
            self.model.add_log("info", "Focus hold released: Z stopped")
        self._z_goal = None
        self._hold_at_limit = False
        self.focus.pad.set_moving(None)
        self._refresh_focus()

    def _focus_step(self, delta_um: float, held: bool = False) -> None:
        """One pad step: from the in-flight target (rapid clicks add up) or else the readback, through
        ctl.move_focus. A step that would leave the drive's range is refused with a message, never clamped.

        Held (pad or arrow key held down): the target stays at most two steps ahead of the readback, so Z stops
        within about two steps of the release, and a held move upward stops at the objective's safe height. A single
        click may still cross that height (the readout then turns amber)."""
        z = self.ctl.focus_z()
        if z is None:
            self.model.add_log("warn", "Z step refused: no focus readback")
            return
        base = self._z_goal if self._z_goal is not None else z
        if held and abs(base - z) >= 2 * abs(delta_um) - 1e-6:
            return  # the drive is behind: skip this repeat instead of queueing an overshoot
        target = round(base + delta_um, 3)
        name, safe = self._focus_limit()
        if held and delta_um > 0 and safe is not None and target > safe + 1e-6:
            if base >= safe - 1e-6:
                if not getattr(self, "_hold_at_limit", False):
                    self._hold_at_limit = True
                    self.model.add_log("warn", f"Held focus stopped at the {name} safe height ({fmt_um(safe)} µm); "
                                               "click to step past it")
                return
            target = round(safe, 3)  # a held move ends exactly at the safe height
        elif not held:
            self._hold_at_limit = False
        confirmed = False
        if not held and delta_um > 0 and safe is not None and target > safe + 1e-6:
            # the router refuses an upward move past the safe height unless the operator confirmed it (APP-03); ask once
            # per objective and height, until Z is back below it
            key = (name, safe)
            if getattr(self, "_above_ok", None) != key:
                from PySide6.QtWidgets import QMessageBox

                if QMessageBox.question(self, "Above the safe height",
                                        f"Z {fmt_um(target)} µm is above the {name} safe height ({fmt_um(safe)} µm), "
                                        "where the objective nears the vessel.\n\nMove higher anyway?") \
                        != QMessageBox.Yes:
                    return
                self._above_ok = key
            confirmed = True
            self.model.add_log("warn", f"Z {fmt_um(target)} µm is above the {name} safe height ({fmt_um(safe)} µm); "
                                       "operator confirmed")
        elif safe is None or target <= (safe or 0) + 1e-6:
            self._above_ok = None
        lim = self.ctl.axis_limits(self.profile.focus_device, "z")
        if lim and not (lim[0] - 1e-6 <= target <= lim[1] + 1e-6):
            self.model.add_log("warn", f"Z step refused: {fmt_um(target)} µm is outside the focus range "
                                       f"{fmt_um(lim[0])}–{fmt_um(lim[1])} µm")
            return
        self._z_goal = target
        self.focus.pad.set_moving("up" if delta_um > 0 else "down")  # the press pose holds until the drive is idle
        self._z_submitting = True
        try:
            self._move_z(target, confirmed=confirmed)
        finally:
            self._z_submitting = False
        self._refresh_focus()

    def _snap(self) -> None:
        try:
            self.coalesce.request("focus", self.session.capture.snap_to_last)
        except CaptureRejected as exc:
            self.model.add_log("warn", str(exc))

    # ================================================================== actions: light
    def _channel_clicked(self, slot: str) -> None:
        """A channel button: lights that channel (a cube channel first brings its cube into the light path,
        ADR-0051); clicking the channel that is already lit switches the light off."""
        if slot == self.channel and self.btn_power.isChecked():
            self.btn_power.setChecked(False)  # -> _power -> light off
            return
        self._select_channel(slot)

    def _select_channel(self, slot: str, initial: bool = False) -> None:
        """Select a channel and light it (`initial`: select only, the light keeps its on/off)."""
        if not initial:
            self.btn_power.blockSignals(True)
            self.btn_power.setChecked(True)
            self.btn_power.blockSignals(False)
            self.btn_power.setText("Light on")
            self.session.capture.light_power = True
        self.channel = slot
        for s, b in self.ch_buttons.items():
            b.selected = s == slot
            b.update()
        self._refresh_settings()
        self._apply_light()

    def _power(self, on: bool) -> None:
        self.btn_power.setText("Light on" if on else "Light off")
        self.session.capture.light_power = on
        self._apply_light()

    def _light_slider(self, v: float) -> None:
        if self.actual_mode:
            return
        fluo = self.channel.startswith("cube")
        cs = self.current_channel_settings().apply_slider(v, fluo)
        self.settings.camera_settings(self.role).channels[self.channel] = cs
        cs = self._clamp_to_device(cs)
        self.led.set_value(cs.led_percent)
        self.exp.set_value(cs.exposure_ms)
        self.gain_row.set_value(cs.gain)
        self._update_light_labels()
        self._apply_light()

    def _set_actual(self, on: bool) -> None:
        self.actual_mode = on
        self.chk_actual.blockSignals(True)
        self.chk_actual.setChecked(on)
        self.chk_actual.blockSignals(False)
        self.light.setEnabled(not on)
        for w in (self.led, self.exp, self.gain_row):
            w.setVisible(on)
        self.lbl_actual.setVisible(not on)

    def _actual_changed(self, _v: float = 0.0) -> None:
        if not self.actual_mode:
            return
        cs = self.current_channel_settings().model_copy(update={
            "led_percent": self.led.value(), "exposure_ms": self.exp.value(), "gain": self.gain_row.value()})
        self.settings.camera_settings(self.role).channels[self.channel] = cs
        self._clamp_to_device(cs)
        self._update_light_labels()
        self._apply_light()

    def _apply_light(self) -> None:
        cs = self.current_channel_settings()
        ch, on, role = self.channel, self.btn_power.isChecked(), self.role
        cam = self.profile.camera(role)
        chp = self.profile.channel(ch)
        if not (cam and chp and cam.supports(chp)):
            return
        self._light_applied = True
        self.coalesce.request("light", lambda: self.ctl.set_light(ch, cs.led_percent, on))
        self.coalesce.request("exposure", lambda: self.ctl.set_exposure(role, cs.exposure_ms), group=f"cam-{role.value}")  # noqa: E501
        self.coalesce.request("gain", lambda: self.ctl.set_gain(role, cs.gain), group=f"cam-{role.value}")

    def _light_camera(self) -> None:
        dlg = LightCameraDialog(self, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.settings.mono.pseudocolor_enabled = dlg.pseudo.isChecked()
        if dlg.actual.isChecked():
            cs = self.current_channel_settings().model_copy(update={
                "led_percent": dlg.led.value(), "exposure_ms": dlg.exp.value(), "gain": dlg.gain.value()})
            self.settings.camera_settings(self.role).channels[self.channel] = cs
        self._set_actual(dlg.actual.isChecked())
        if dlg.camera is not self.role:
            self.switch_camera(dlg.camera)
        self._refresh_settings()
        self._refresh_thumbnails()
        self._apply_light()

    def switch_camera(self, role: CameraRole) -> None:
        self.settings.active_camera = role
        chp = self.profile.channel(self.channel)
        cam = self.profile.camera(role)
        if cam and chp and not cam.supports(chp):
            self.channel = next((c.slot for c in self.profile.channels_for(role)), self.channel)
        self._smooth.reset()
        try:
            self.ctl.start_live(role)
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("error", f"live view: {exc}")
        if self.settings.basic.light_off_on_camera_switch and self.btn_power.isChecked():
            # Settings › "Switch the light off when changing camera" (on by default)
            self.btn_power.blockSignals(True)
            self.btn_power.setChecked(False)
            self.btn_power.blockSignals(False)
            self.btn_power.setText("Light off")
            self.session.capture.light_power = False
        self._select_channel(self.channel, initial=True)  # applies the light's on/off (off after the line above)
        self.model.settings_changed.emit()
        self.route_light_path(role)

    def route_light_path(self, role: CameraRole | None = None) -> bool:
        """The colour camera sees the sample only with F at the mirror (Gate 12): move F for `role` (default: the
        active camera) in the background. True if a routing task was started."""
        role = role or self.role
        if not any(getattr(s, "carriage_control", False) for s in getattr(self.session, "telemetry", []) or []):
            return False
        return bool(self.submit_when_idle(
            f"Light path for the {role.value} camera", lambda: self.session.route_light_path(role),
            on_done=lambda msg: msg and self.model.add_log("warn" if "not moved" in msg else "info", msg)))

    def route_light_path_when_ready(self, timeout_s: float = 30.0) -> None:
        """At start-up: once the F carriage reports a position (the drives poll ~1 s after start), route F for the
        camera the app opened on (2026-11-18: the app reopened on Colour with F at slot 1, and the image was black)."""
        waited = [0.0]

        def poll():
            snap = self.session.core.store.get("motor.cube_carriage")
            if snap is not None and snap.observed_position is not None:
                self.route_light_path()
                return
            waited[0] += 0.5
            if waited[0] < timeout_s:
                QTimer.singleShot(500, poll)

        QTimer.singleShot(500, poll)

    def _toggle_saturation(self, on: bool) -> None:
        self.settings.camera_settings(self.role).highlight_saturated = on
        self._redraw()

    def _camera_segment(self, key: str) -> None:
        role = CameraRole(key)
        if role is not self.role:
            self.switch_camera(role)

    # ================================================================== actions: view
    def _live_clicked(self, checked: bool) -> None:
        if checked or self.view_mode != "live":
            self.go_live()
        else:
            self.stop_live()

    def stop_live(self) -> None:
        """Stop the live stream (the camera idles; the last frame stays on screen)."""
        try:
            self.ctl.stop_live(self.role)
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("error", f"stop live view: {exc}")
        self.view_mode = "stopped"
        self.btn_live.setChecked(False)
        self.seg_live.set_current("stopped")
        self.lbl_view.setText("Live stopped")
        self.view.badge = f"{self.channel_label()} • LIVE STOPPED"
        self.view.badge_color = theme.MUTED
        self.view.viewport().update()
        self._refresh_device()

    def go_live(self) -> None:
        self.view_mode = "live"
        self.btn_live.setChecked(True)
        self.lbl_view.setText("Live")
        try:
            self.ctl.start_live(self.role)
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("error", f"live view: {exc}")
        self.seg_live.set_current("live" if self.ctl.is_live(self.role) else "stopped")

    def show_captured(self, img) -> None:
        self.view_mode = "captured"
        self.btn_live.setChecked(False)
        self.lbl_view.setText(f"Captured · {img.channel_label}")
        self._present(img.data, img.channel, img.max_value, live=False)
        self.view.badge = f"{img.channel_label} • CAPTURED • {'SIMULATED' if img.synthetic else 'CAMERA'}"
        self.view.badge_color = theme.SIM if img.synthetic else theme.OK
        self.lbl_frame.setText(f"Captured {img.channel_label} at z={img.z_um:.2f} µm · {img.exposure_ms:g} ms · "
                               f"gain {img.gain:g} · LED {img.led_percent:g} % · {img.captured_at:%H:%M:%S}")

    def show_overlay(self) -> None:
        bufs = self.session.capture.buffers.all()
        mono = [i for i in bufs.values() if not i.is_color]
        if not mono:
            self.model.add_log("warn", "overlay: no monochrome captures in the memory buffers")
            return
        shape = mono[-1].data.shape
        layers = overlay_layers([i for i in mono if i.data.shape == shape], self.profile, self.settings)
        rgb = composite(layers, pseudo=self.settings.mono.pseudocolor_enabled)
        self.view_mode = "overlay"
        self.btn_live.setChecked(False)
        self.lbl_view.setText(f"Overlay · {', '.join(i.channel_label for i in mono)}")
        self.view_image = rgb
        self.view.set_image(rgb, mono[-1].data)
        self.view.badge = "OVERLAY • SIMULATED" if any(i.synthetic for i in mono) else "OVERLAY"
        self.view.badge_color = theme.SIM

    def displayed_rgb(self) -> np.ndarray | None:
        return self.view_image if self.view_mode != "live" else None

    def copy_image(self) -> None:
        if self.view._qimage is not None:
            QApplication.clipboard().setImage(self.view._qimage)
            self.model.add_log("info", "image copied to the clipboard (display rendering, 8-bit)")

    # ================================================================== actions: capture
    def _run(self, name: str, fn, on_done=None, progress=False) -> None:
        self.submit_when_idle(name, fn, on_done=on_done, with_progress=progress,
                              on_error=lambda msg: self.lbl_task.setText(f"{name} failed: {msg}"))

    def submit_when_idle(self, name: str, fn, **kw) -> bool:
        return self.model.submit_when_idle(name, fn, **kw)

    def capture_current(self) -> None:
        slot, role = self.channel, self.role
        self._run(f"Capture {self.channel_label()}", lambda: self.session.capture.capture(slot, role),
                  on_done=self.show_captured)

    def capture_all(self) -> None:
        self._run("Capture All", lambda p: self.session.capture.capture_all(self.role, p),
                  on_done=lambda _imgs: self.show_overlay(), progress=True)

    def _autofocus(self, coarse: bool) -> None:
        af, role, v = self.session.autofocus, self.role, self.session.vessel
        rng = self.settings.basic.auto_fine_range_percent

        def done(r) -> None:
            self.model.add_log("info" if r.ok else "warn", f"{'Find sample' if coarse else 'Auto fine'}: {r.message}")
            self.lbl_task.setText(r.message)

        if coarse:
            self._run("Find Sample", lambda p: af.find_sample(role, v.focus_nominal_um, v.focus_range_um, p), done, True)  # noqa: E501
        else:
            self._run("Auto Fine", lambda p: af.auto_fine(role, rng, p), done, True)

    def _hot(self, i: int) -> None:
        self.run_action(self.settings.camera_settings(self.role).hot_buttons[i])

    def run_action(self, action: HotButtonAction) -> None:
        cap = self.session.capture
        if action is HotButtonAction.CAPTURE:
            self.capture_current()
        elif action is HotButtonAction.CAPTURE_ALL:
            self.capture_all()
        elif action is HotButtonAction.SAVE:
            self.request_page.emit("save")
        elif action is HotButtonAction.QUICK_SAVE:
            self.request_page.emit("quick_save")
        elif action is HotButtonAction.CAPTURE_FOCUS_NOMINAL:
            # audit APP-21: near home, or far from the old value, ask first (one click right after homing or an
            # objective change used to replace the vessel's nominal with Z = 0)
            z_now, old = self.ctl.focus_z(), self.session.vessel.focus_nominal_um
            if z_now is not None and (z_now < NOMINAL_NEAR_HOME_UM or abs(z_now - old) > NOMINAL_JUMP_UM):
                from PySide6.QtWidgets import QMessageBox

                if QMessageBox.question(self, "Capture focus nominal",
                                        f"Replace the focus nominal for {self.session.vessel.display_name}?\n\n"
                                        f"{old:,.1f} µm → {z_now:,.1f} µm"
                                        + ("\n\nZ is at or near home: this is rarely the focus." if
                                           z_now < NOMINAL_NEAR_HOME_UM else "")) != QMessageBox.Yes:
                    return
            try:
                z = self.session.capture_focus_nominal()
                self._last_nominal = (self.session.vessel.vessel_id, old)  # for undo
                self.model.add_log("info", f"focus nominal for {self.session.vessel.display_name} set to {z:.1f} µm "
                                           f"(was {old:.1f} µm; Edit › Undo focus nominal restores it)")
                self._refresh_state()
            except RuntimeError as exc:
                self.model.add_log("error", str(exc))
        elif action is HotButtonAction.ZSTACK:
            z = self.ctl.focus_z() or 0.0
            dlg = ZStackDialog(z, self)
            if dlg.exec() == QDialog.Accepted:
                lo, hi, step = z - dlg.below.value(), z + dlg.above.value(), dlg.step.value()
                slot, role = self.channel, self.role
                self._run("Z-Stack", lambda p: cap.zstack(slot, lo, hi, step, role, p),
                          on_done=lambda st: (self.model.add_log("info", f"Z-stack: {len(st.images)} slices"),
                                              self.request_page.emit("zstack")), progress=True)
        elif action is HotButtonAction.TRANSFECTION:
            dlg = TransfectionDialog(self.profile, self)
            if dlg.exec() == QDialog.Accepted and dlg.fl.currentData() and dlg.tr.currentData():
                fl, tr = dlg.fl.currentData(), dlg.tr.currentData()

                def done(pair) -> None:
                    f, t = pair
                    rgb = transfection(Layer(f.data, channel_color(f.channel, self.profile, self.settings)),
                                       Layer(t.data))
                    self.view_mode = "overlay"
                    self.view_image = rgb
                    self.view.set_image(rgb, f.data)
                    self.lbl_view.setText(f"Transfection · {f.channel_label} over {t.channel_label}")

                self._run("Transfection", lambda: cap.transfection(fl, tr), on_done=done)

    # ------------------------------------------------------------------ chrome hooks
    def set_compact(self, on: bool) -> None:
        """Narrow window: panels narrow; nothing hides (toolbar labels are handled in resizeEvent)."""
        self.left.setMaximumWidth(248 if on else 280)
        self.right.setMaximumWidth(300 if on else 330)
        self.hist.setFixedWidth(260 if on else 320)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        center = self.width() - self.left.width() - self.right.width() - 40
        style = Qt.ToolButtonIconOnly if center < 1080 else Qt.ToolButtonTextBesideIcon
        for b in self.toolbuttons:
            if b.toolButtonStyle() != style:
                b.setToolButtonStyle(style)

    def retint(self) -> None:
        for b in self.toolbuttons:
            b.setIcon(icon(b.property("icon_name")))
        self.btn_capture.setIcon(icon("capture", theme.ACCENT_ON))
        self.btn_power.setIcon(icon("light"))
        self.btn_vessel.setIcon(icon("vessel"))
        self.view.setBackgroundBrush(QColor(theme.STAGE))
        self._refresh_thumbnails()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self.view_mode == "live" and not self.model.tasks.busy:
            self.go_live()

    def channel_is_fluorescence(self) -> bool:
        c = self.profile.channel(self.channel)
        return bool(c and c.kind is ChannelKind.FLUORESCENCE)
