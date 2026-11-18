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
        pixel_size_um=prim.get("pixel_size_um"), stage_xy_um=tuple(prim["stage_xy_um"]) if prim.get("stage_xy_um") else None,
        z_um=prim.get("z_um"), exposure_ms=prim.get("exposure_ms") or 0.0, gain=prim.get("gain") or 1.0,
        led_percent=prim.get("led_percent") or 0.0, synthetic="SIMULATED" in md.get("provenance", ""),
        frame_device=prim.get("frame_device", "file"), frame_index=prim.get("frame_index", 0),
        bit_depth=8 if a.dtype == np.uint8 else 16)


# ====================================================================================== Z-stack
class ZStackPage(QWidget):
    """Acquire a Z series around the current focus and browse slices / max projection."""

    def __init__(self, model, capture_page, parent=None):
        super().__init__(parent)
        self.model, self.cap = model, capture_page
        self.session = model.session
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        f, lay = panel("Z-stack")
        f.setFixedWidth(340)
        form = QFormLayout()
        self.channel = QComboBox()
        self._fill_channels()
        self.below =QDoubleSpinBox(minimum=0, maximum=2000, value=10, suffix=" µm")
        self.above = QDoubleSpinBox(minimum=0, maximum=2000, value=10, suffix=" µm")
        self.step = QDoubleSpinBox(minimum=0.1, maximum=500, value=2, decimals=2, suffix=" µm")
        form.addRow("Channel", self.channel)
        form.addRow("Below focus", self.below)
        form.addRow("Above focus", self.above)
        form.addRow("Step", self.step)
        lay.addLayout(form)
        self.lbl_n = label("", "muted")
        lay.addWidget(self.lbl_n)
        self.btn = QPushButton("Acquire Z-stack")
        self.btn.setObjectName("primary")
        lay.addWidget(self.btn)
        lay.addWidget(label("Focus returns to its starting position afterwards. Every slice is a verified Z move "
                            "followed by an exposure (no exposure while Z moves).", "muted", wrap=True))
        self.proj = QCheckBox("Show maximum-intensity projection")
        lay.addWidget(self.proj)
        self.btn_ome = QPushButton("Export OME-TIFF…")
        self.btn_ome.setToolTip("Z × Y × X with physical pixel size and Z step (opens in Fiji, napari, QuPath)")
        self.btn_ome.clicked.connect(self._export_ome)
        lay.addWidget(self.btn_ome)
        lay.addStretch(1)
        root.addWidget(f)
        g, gl = panel("Slices")
        self.view = ImageView()
        self.view.placeholder = "No Z-stack yet"
        gl.addWidget(self.view, 1)
        self.slice = _slider(0, 0, 0)
        self.lbl_slice = label("", "mono")
        gl.addWidget(hbox(self.slice, self.lbl_slice))
        root.addWidget(g, 1)
        for w in (self.below, self.above, self.step):
            w.valueChanged.connect(self._count)
        self.btn.clicked.connect(self._acquire)
        self.slice.valueChanged.connect(self._show)
        self.proj.toggled.connect(self._show)
        model.buffers_changed.connect(self._refresh)
        model.settings_changed.connect(self._fill_channels)  # the camera may have changed
        model.tasks.busy_changed.connect(lambda b: self.btn.setEnabled(not b))
        self._count()

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

    def _count(self) -> None:
        n = round((self.below.value() + self.above.value()) / self.step.value()) + 1
        self.lbl_n.setText(f"{n} slices")

    def _acquire(self) -> None:
        z = self.session.controller.focus_z()
        if z is None:
            return
        slot, role = self.channel.currentData(), self.session.settings.active_camera
        lo, hi, st = z - self.below.value(), z + self.above.value(), self.step.value()
        self.model.submit_when_idle("Z-Stack", lambda p: self.session.capture.zstack(slot, lo, hi, st, role, p),
                                with_progress=True)

    def _refresh(self) -> None:
        zs = self.session.capture.buffers.zstack
        if zs is None:
            return
        self.slice.setRange(0, len(zs.images) - 1)
        self._show()

    def _show(self) -> None:
        zs = self.session.capture.buffers.zstack
        if zs is None:
            return
        st = self.session.settings
        if self.proj.isChecked():
            data = np.max(np.stack([i.data for i in zs.images]), axis=0)
            self.lbl_slice.setText(f"MIP of {len(zs.images)} slices")
        else:
            i = self.slice.value()
            data = zs.images[i].data
            self.lbl_slice.setText(f"slice {i + 1}/{len(zs.images)}  z={zs.z_um[i]:.2f} µm")
        self.view.set_image(render_frame(data, zs.channel, self.session.profile, st))
        self.view.pixel_um = zs.images[0].pixel_size_um
        self.view.pixel_calibrated = zs.images[0].pixel_size_calibrated
        self.view.badge = "Z-STACK • SIMULATED" if zs.images[0].synthetic else "Z-STACK"

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh()


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
                if zs else []
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
        br.clicked.connect(lambda: self.folder.setText(QFileDialog.getExistingDirectory(self, "Folder") or self.folder.text()))
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
