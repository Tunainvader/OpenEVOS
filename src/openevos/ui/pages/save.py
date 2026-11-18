"""Image > Save (manual p.10, p.15-17) and Quick Save.

Presets: Default (8-bit colour TIFF with annotations), Quantitative Monochrome (16-bit TIFF raw),
Custom. Formats TIFF/PNG/JPEG/BMP; 16-bit only for TIFF + monochrome; colour output is 8-bit.
Invalid combinations are disabled and explained, never silently converted.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QWidget,
)

from openevos.imaging.composite import composite
from openevos.imaging.save import (
    SaveError,
    SaveOptions,
    SavePreset,
    allowed_bit_depths,
    save,
    validate,
)
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import ImageFormat
from openevos.ui.common import hbox, label, overlay_layers, panel, render_frame
from openevos.ui.icons import icon
from openevos.ui.widgets.image_view import ImageView


def default_capture_folder() -> Path:
    from platformdirs import user_documents_dir

    return Path(user_documents_dir()) / "OpenEVOS" / "Captures"


class SavePage(QWidget):
    def __init__(self, model, capture_page, parent=None):
        super().__init__(parent)
        self.model, self.cap = model, capture_page
        self.session = model.session
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        f, lay = panel("Save preview")
        self.source = QComboBox()
        lay.addWidget(hbox(label("Source", "muted"), self.source, None))
        self.view = ImageView()
        self.view.placeholder = "Capture an image first"
        lay.addWidget(self.view, 1)
        self.lbl_meta = label("", "mono", wrap=True)
        lay.addWidget(self.lbl_meta)
        root.addWidget(f, 1)

        g, gl = panel("Save settings")
        g.setFixedWidth(380)
        form = QFormLayout()
        self.preset = QComboBox()
        for p in SavePreset:
            self.preset.addItem(p.label, p)
        self.fmt = QComboBox()
        for x in ImageFormat:
            self.fmt.addItem(x.value.upper(), x)
        self.depth = QComboBox()
        self.color = QRadioButton("Color")
        self.mono = QRadioButton("Monochrome")
        grp = QButtonGroup(self)
        grp.addButton(self.color)
        grp.addButton(self.mono)
        self.color.setChecked(True)
        self.annot = QCheckBox("Include annotations")
        self.annot.setChecked(True)
        self.under = QCheckBox("Save underlying channels")
        self.under_mode = QComboBox()
        self.under_mode.addItem("16-bit monochrome", "16bit_mono")
        self.under_mode.addItem("8-bit color", "8bit_color")
        self.folder = QLineEdit(str(default_capture_folder()))
        browse = QPushButton()  # was a bare "…": now a folder icon (UI audit 2026-11-17)
        browse.setIcon(icon("folder"))
        browse.setToolTip("Browse…")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse)
        self.base = QLineEdit("Image")
        form.addRow("Preset", self.preset)
        form.addRow("Format", self.fmt)
        form.addRow("Bit depth", self.depth)
        form.addRow("Output", hbox(self.color, self.mono, None))
        form.addRow("", self.annot)
        form.addRow("", self.under)
        form.addRow("Underlying as", self.under_mode)
        form.addRow("Folder", hbox(self.folder, browse))
        form.addRow("Base name", self.base)
        gl.addLayout(form)
        self.lbl_rules = label("", "warn", wrap=True)
        gl.addWidget(self.lbl_rules)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("primary")
        self.btn_save.setMinimumHeight(36)
        gl.addWidget(self.btn_save)
        gl.addWidget(label("Quick Save", "section"))
        self.lbl_quick = label("", "muted")  # one line; the full folder is in the tooltip
        change = QPushButton("Change…")
        change.setObjectName("link")
        change.setToolTip("Quick Save settings are edited in Settings, per camera")
        change.clicked.connect(lambda: getattr(self.window(), "navigate", lambda _k: None)("settings"))
        gl.addWidget(hbox(self.lbl_quick, change, None))
        self.btn_quick = QPushButton("Quick Save now")
        gl.addWidget(self.btn_quick)
        self.lbl_result = label("", "muted", wrap=True)
        gl.addWidget(self.lbl_result)
        gl.addStretch(1)
        root.addWidget(g)

        self.preset.currentIndexChanged.connect(self._apply_preset)
        for w in (self.fmt, self.under_mode, self.depth):
            w.currentIndexChanged.connect(self._validate)
        for w in (self.color, self.mono, self.annot, self.under):
            w.toggled.connect(self._validate)
        self.fmt.currentIndexChanged.connect(self._depths)
        self.color.toggled.connect(self._depths)
        self.source.currentIndexChanged.connect(self._preview)
        self.btn_save.clicked.connect(self._save)
        self.btn_quick.clicked.connect(self.quick_save)
        model.buffers_changed.connect(self._sources)
        self._depths()
        self._sources()

    # ------------------------------------------------------------------ sources
    def _sources(self) -> None:
        cur = self.source.currentData()
        self.source.blockSignals(True)
        self.source.clear()
        bufs = self.session.capture.buffers.all()
        if len([b for b in bufs.values() if not b.is_color]) > 1:
            self.source.addItem("Overlay of buffered channels", "overlay")
        for slot, img in bufs.items():
            self.source.addItem(f"{img.channel_label} ({img.camera.value})", slot)
        self.source.blockSignals(False)
        i = self.source.findData(cur)
        self.source.setCurrentIndex(max(i, 0))
        self._preview()

    def _selection(self):
        """(rgb8, primary raw capture, channels dict)."""
        bufs = self.session.capture.buffers.all()
        key = self.source.currentData()
        prof, st = self.session.profile, self.session.settings
        if key == "overlay":
            mono = {k: v for k, v in bufs.items() if not v.is_color}
            shape = next(iter(mono.values())).data.shape
            mono = {k: v for k, v in mono.items() if v.data.shape == shape}
            rgb = composite(overlay_layers(list(mono.values()), prof, st))
            return rgb, next(iter(mono.values())), mono
        img = bufs.get(key)
        if img is None:
            return None, None, {}
        return render_frame(img.data, img.channel, prof, st, max_value=img.max_value), img, {key: img}

    def _burned(self, rgb, raw):
        """Annotations (Measure) and count tags/totals (Count, 'save image as screenshot') for this source."""
        if rgb is None or not self.annot.isChecked() or not self.color.isChecked():
            return rgb, []
        from openevos.analysis.annotations import burn_in

        key = self.source.currentData()
        anns = getattr(self.model, "annotations", {}).get(key)
        ap = getattr(self.model, "analysis_page", None)
        count = ap.count_burn() if ap is not None and ap.current_key == key else {}
        if not (anns and anns.items) and not count:
            return rgb, []
        # burned-in line lengths in µm only from a measured pixel size (nominal → px), never the estimate
        out = burn_in(rgb, anns, pixel_um=raw.pixel_size_um if raw and raw.pixel_size_calibrated else None, **count)
        names = [a.name for a in anns.items] if anns else []
        return out, names + (["count tags and totals"] if count else [])

    def _display_log(self, raw) -> list[str]:
        lines = self.cap.display_log(raw.data if raw is not None and raw.data.ndim == 2 else None,
                                     raw.max_value if raw is not None else None) if raw is not None else []
        ap = getattr(self.model, "analysis_page", None)
        return lines + (ap.display_log() if ap is not None else [])

    def _preview(self) -> None:
        rgb, raw, chans = self._selection()
        rgb, _ = self._burned(rgb, raw)
        self.view.set_image(rgb)
        if raw is not None:
            self.view.pixel_um = raw.pixel_size_um
            self.view.pixel_calibrated = raw.pixel_size_calibrated
            self.view.badge = "SIMULATED" if raw.synthetic else ""
            self.lbl_meta.setText(f"{', '.join(c.channel_label for c in chans.values())} · {raw.camera.value} camera · "
                                  f"{raw.data.shape[1]}x{raw.data.shape[0]} · {raw.bit_depth}-bit native · "
                                  f"z {raw.z_um:.2f} µm · {raw.captured_at:%Y-%m-%d %H:%M:%S}")
        else:
            self.lbl_meta.setText("")
        self._validate()

    # ------------------------------------------------------------------ options
    def _apply_preset(self) -> None:
        p = SavePreset(self.preset.currentData())
        if p is SavePreset.CUSTOM:
            return
        o = p.options()
        self.fmt.setCurrentIndex(self.fmt.findData(o.image_format))
        (self.color if o.as_color else self.mono).setChecked(True)
        self._depths()
        self.depth.setCurrentIndex(self.depth.findData(o.bit_depth))
        self.annot.setChecked(o.include_annotations)

    def _depths(self) -> None:
        cur = self.depth.currentData()
        self.depth.blockSignals(True)
        self.depth.clear()
        for d in allowed_bit_depths(ImageFormat(self.fmt.currentData()), self.color.isChecked()):
            self.depth.addItem(f"{d}-bit", d)
        i = self.depth.findData(cur)
        self.depth.setCurrentIndex(max(i, 0))
        self.depth.blockSignals(False)
        self._validate()

    def options(self) -> SaveOptions:
        return SaveOptions(ImageFormat(self.fmt.currentData()), self.depth.currentData() or 8, self.color.isChecked(),
                           self.annot.isChecked() and self.color.isChecked(), self.under.isChecked(),
                           self.under_mode.currentData())

    def _validate(self) -> None:
        _rgb, raw, _ = self._selection() if self.source.count() else (None, None, {})
        o = self.options()
        errs = validate(o, source_is_color=raw.is_color if raw is not None else False) if raw is not None else \
            ["nothing captured yet"]
        self.annot.setEnabled(self.color.isChecked())
        self.mono.setEnabled(not (raw is not None and raw.is_color))
        self.lbl_rules.setText("; ".join(errs))
        self.btn_save.setEnabled(not errs)
        role = self.session.settings.active_camera
        q = self.session.settings.camera_settings(role).quick_save
        self.lbl_quick.setText(f"'{q.base_name}' · {q.image_format.value.upper()} "
                               f"{'colour' if q.save_as_color else 'mono'}")
        self.lbl_quick.setToolTip(f"{role.value} camera Quick Save to {q.folder or default_capture_folder()}")

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save folder", self.folder.text())
        if d:
            self.folder.setText(d)

    # ------------------------------------------------------------------ save
    def _save(self) -> None:
        rgb, raw, chans = self._selection()
        rgb, burned = self._burned(rgb, raw)
        try:
            out = save(Path(self.folder.text()), self.base.text(), rgb8=rgb, raw=raw, opts=self.options(),
                       channels=chans, extra_metadata={"display_log": self._display_log(raw),
                                                       "burned_in": burned})
        except (SaveError, OSError) as exc:
            self.lbl_result.setText(f"Not saved: {exc}")
            self.model.add_log("error", f"save: {exc}")
            return
        self.lbl_result.setText(f"Saved {out.image.name}" + (f" + {len(out.underlying)} channel file(s)" if out.underlying else ""))  # noqa: E501
        self.model.add_log("info", f"saved {out.image}")

    def quick_save(self) -> None:
        role = self.session.settings.active_camera
        q = self.session.settings.camera_settings(role).quick_save
        self._sources()
        rgb, raw, chans = self._selection()
        if raw is None:
            self.model.add_log("warn", "Quick Save: nothing captured yet")
            return
        as_color = q.save_as_color or raw.is_color
        bit = 16 if (not as_color and q.image_format is ImageFormat.TIFF and role is CameraRole.MONO) else 8
        opts = SaveOptions(q.image_format, bit, as_color, q.include_annotations and as_color, q.save_underlying,
                           self.session.settings.mono.underlying_channel_save)
        if opts.include_annotations:
            rgb, burned = self._burned(rgb, raw)
        else:
            burned = []
        try:
            out = save(Path(q.folder) if q.folder else default_capture_folder(), q.base_name, rgb8=rgb, raw=raw,
                       opts=opts, channels=chans, extra_metadata={"display_log": self._display_log(raw),
                                                                  "burned_in": burned})
        except (SaveError, OSError) as exc:
            self.model.add_log("error", f"Quick Save: {exc}")
            return
        self.lbl_result.setText(f"Quick saved {out.image.name}")
        self.model.add_log("info", f"quick saved {out.image}")

    def showEvent(self, e):
        super().showEvent(e)
        self._sources()


