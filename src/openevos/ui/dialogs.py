"""Dialogs: Vessel Expert (manual p.8), Light and Camera (p.8-9), Z-Stack and Transfection setup."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
)

from openevos.instrument.profile import CameraRole, ChannelKind
from openevos.instrument.vessels import VesselCategory, VesselLibrary
from openevos.ui.common import label
from openevos.ui.widgets.vessel_map import VesselMap


class VesselExpertDialog(QDialog):
    def __init__(self, vessels: VesselLibrary, current_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vessel Expert")
        self.vessels = vessels
        self.resize(520, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Select the vessel category and type. A vessel must be selected before imaging.",
                            "muted", wrap=True))
        form = QFormLayout()
        self.cat = QComboBox()
        self.typ = QComboBox()
        for c in vessels.categories():
            self.cat.addItem(c.value, c)
        form.addRow("Category", self.cat)
        form.addRow("Type", self.typ)
        lay.addLayout(form)
        self.map = VesselMap(interactive=False)
        self.map.setMinimumHeight(240)
        lay.addWidget(self.map, 1)
        self.note = label("", "warn", wrap=True)
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.cat.currentIndexChanged.connect(self._fill_types)
        self.typ.currentIndexChanged.connect(self._preview)
        cur = vessels.get(current_id)
        self.cat.setCurrentIndex(max(0, self.cat.findData(cur.category)))
        self._fill_types()
        self.typ.setCurrentIndex(max(0, self.typ.findData(current_id)))

    def _fill_types(self) -> None:
        self.typ.blockSignals(True)
        self.typ.clear()
        for v in self.vessels.by_category(VesselCategory(self.cat.currentData())):
            self.typ.addItem(v.display_name + ("  (user)" if v.user_created else ""), v.vessel_id)
        self.typ.blockSignals(False)
        self._preview()

    def _preview(self) -> None:
        vid = self.typ.currentData()
        if vid is None:
            return
        v = self.vessels.get(vid)
        self.map.set_vessel(v)
        self.note.setText("" if v.geometry_verified else
                          f"{len(v.regions)} region(s). Geometry is nominal (not measured on this unit); "
                          f"focus nominal {v.focus_nominal_um:.0f} µm (simulator).")

    @property
    def vessel_id(self) -> str:
        return self.typ.currentData()


class LightCameraDialog(QDialog):
    """Camera choice (Monochrome: fluorescence + brightfield; Color: brightfield/phase only), Apply
    Pseudo Color, Actual Mode (LED, gain, exposure separately), Clear All Channels."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        s = page.session
        self.setWindowTitle("Light and Camera")
        self.resize(420, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Camera", "section"))
        self.mono = QRadioButton("Monochrome camera — fluorescence and brightfield")
        self.color = QRadioButton("Color camera — brightfield / phase only")
        grp = QButtonGroup(self)
        grp.addButton(self.mono)
        grp.addButton(self.color)
        self.color.setEnabled(s.profile.camera(CameraRole.COLOR) is not None)
        (self.mono if s.settings.active_camera is CameraRole.MONO else self.color).setChecked(True)
        lay.addWidget(self.mono)
        lay.addWidget(self.color)
        self.pseudo = QCheckBox("Apply Pseudo Color (monochrome fluorescence)")
        self.pseudo.setChecked(s.settings.mono.pseudocolor_enabled)
        lay.addWidget(self.pseudo)
        lay.addWidget(label("Grayscale usually shows more detail; pseudo colour helps tell channels apart in overlays.",
                            "muted", wrap=True))
        self.actual = QCheckBox("Actual Mode — adjust LED intensity, gain and exposure individually")
        self.actual.setChecked(page.actual_mode)
        lay.addWidget(self.actual)
        form = QFormLayout()
        self.led = QDoubleSpinBox(decimals=1, minimum=0, maximum=100, suffix=" %")
        self.exp = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=60000, suffix=" ms")
        self.gain = QDoubleSpinBox(decimals=2, minimum=1, maximum=64, suffix=" x")
        # ranges of the camera and LED these values are stored for (the router rejects anything outside)
        from openevos.ui import device_state as ds

        lim = ds.camera_limits(s, page.role)
        if lim is not None:
            self.exp.setRange(*lim.exposure_ms)
            self.gain.setRange(*lim.gain)
            if lim.gain_fixed:
                self.gain.setToolTip(f"This camera accepts gain {lim.gain[0]:g}x only")
        led = ds.light_limits(s, page.channel)
        if led is not None:
            self.led.setRange(*led)
        cs = page.current_channel_settings()
        self.led.setValue(cs.led_percent)
        self.exp.setValue(cs.exposure_ms)
        self.gain.setValue(cs.gain)
        form.addRow(f"LED intensity ({page.channel_label()})", self.led)
        form.addRow("Exposure", self.exp)
        form.addRow("Gain", self.gain)
        lay.addLayout(form)
        gain_fixed = lim is not None and lim.gain_fixed
        for w in (self.led, self.exp, self.gain):
            w.setEnabled(self.actual.isChecked() and not (w is self.gain and gain_fixed))
        self.actual.toggled.connect(lambda on: [w.setEnabled(on and not (w is self.gain and gain_fixed))
                                                for w in (self.led, self.exp, self.gain)])
        clear = QPushButton("Clear All Channels")
        clear.setObjectName("danger")
        clear.setToolTip("Clears all memory buffers (temporary holding areas for captured images)")
        clear.clicked.connect(lambda: (s.capture.buffers.clear(), page.model.add_log("info", "memory buffers cleared")))
        lay.addWidget(clear, 0, Qt.AlignLeft)
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @property
    def camera(self) -> CameraRole:
        return CameraRole.MONO if self.mono.isChecked() else CameraRole.COLOR


class ZStackDialog(QDialog):
    """The Capture page's Z-Stack hot button: a stack centred on the current focus, with the Z-stack page's saved
    range and step (one set of values for both, ADR-0064). The Z-stack page has the full set-up."""

    def __init__(self, z_now: float, zs_settings, estimate=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Z-stack")
        self._zs, self._estimate = zs_settings, estimate
        form = QFormLayout(self)
        self.below = QDoubleSpinBox(minimum=0, maximum=2000, value=zs_settings.below_um, decimals=2, suffix=" µm")
        self.above = QDoubleSpinBox(minimum=0, maximum=2000, value=zs_settings.above_um, decimals=2, suffix=" µm")
        self.step = QDoubleSpinBox(minimum=0.1, maximum=500, value=zs_settings.step_um, decimals=2, suffix=" µm")
        self.below.setToolTip("Range below the focus (away from the sample)")
        self.above.setToolTip("Range above the focus (toward the sample)")
        form.addRow(label(f"Centred on the current focus, {z_now:,.2f} µm", "muted"))
        form.addRow("Below focus", self.below)
        form.addRow("Above focus", self.above)
        form.addRow("Step", self.step)
        self.lbl_estimate = label("", "muted", wrap=True)
        form.addRow(self.lbl_estimate)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Acquire")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        form.addRow(bb)
        for w in (self.below, self.above, self.step):
            w.valueChanged.connect(self._update)
        self._update()

    def _update(self) -> None:
        if self._estimate:
            self.lbl_estimate.setText(self._estimate(self.below.value(), self.above.value(), self.step.value()))

    def _accept(self) -> None:
        self._zs.below_um, self._zs.above_um = self.below.value(), self.above.value()
        self._zs.step_um = self.step.value()
        self.accept()


class TransfectionDialog(QDialog):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transfection overlay")
        lay = QVBoxLayout(self)
        lay.addWidget(label("Two-image overlay: one fluorescence image over one transmitted-light image.", "muted",
                            wrap=True))
        row = QHBoxLayout()
        self.fl = QComboBox()
        self.tr = QComboBox()
        for c in profile.channels:
            if c.installed and c.kind is ChannelKind.FLUORESCENCE:
                self.fl.addItem(c.display, c.slot)
            elif c.installed:
                self.tr.addItem(c.display, c.slot)
        row.addWidget(QLabel("Fluorescence"))
        row.addWidget(self.fl)
        row.addWidget(QLabel("Transmitted"))
        row.addWidget(self.tr)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def slider(minimum: int, maximum: int, value: int, orientation=Qt.Horizontal) -> QSlider:
    s = QSlider(orientation)
    s.setRange(minimum, maximum)
    s.setValue(value)
    return s


class InfoDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None, mono: bool = False):
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit

        self.setWindowTitle(title)
        self.resize(640, 460)
        lay = QVBoxLayout(self)
        t = QPlainTextEdit(text)
        t.setReadOnly(True)
        if mono:
            t.setStyleSheet('font-family: "JetBrains Mono", monospace; font-size: 12px;')
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


GETTING_STARTED = [
    ("Choose the vessel", ("Microscope › Vessel… (or Vessel expert… on the Capture page). A vessel must be selected "
     "before imaging; built-in geometry is nominal until Calibration › Vessel alignment has run.")),
    ("Find the sample", ("Pick TRANS, switch the light on, choose the lowest objective and press Find sample. "
     "Then Auto fine at the magnification you will image with.")),
    ("Set the light", ("Drag Light until the histogram fills without clipping (the right-hand number under the "
     "histogram stays at 0 %). Actual mode sets LED, exposure and gain separately.")),
    ("Capture", ("Capture takes the selected channel; Capture all takes every selected fluorescence cube. Thumbnails "
     "appear under the channel buttons; Overlay combines them.")),
    ("Save", ("File › Save image… — Default saves an 8-bit colour TIFF with annotations; Quantitative monochrome "
     "saves the raw 16-bit data. Every file gets a JSON sidecar with the acquisition and display log.")),
    ("Automate", ("Scan tiles wells or areas; Time-lapse revisits beacons on a schedule. Stop all (Esc) cancels "
     "every running command and workflow.")),
]


class GettingStartedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Getting started")
        self.resize(620, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Getting started with OpenEVOS", "title"))
        for i, (head, body) in enumerate(GETTING_STARTED, 1):
            lay.addWidget(label(f"{i}  {head}", "value"))
            lay.addWidget(label(body, "muted", wrap=True))
        lay.addWidget(label("This session uses the simulator: images are synthetic and labelled SIMULATED.", "warn",
                            wrap=True))
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
