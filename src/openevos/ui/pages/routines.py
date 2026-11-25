"""Scan (manual p.22-27) and Time-lapse (p.28-32) workspaces.

Both follow the original wizard order on one page (numbered sections) with a live plan preview:
tile/beacon overlay on the virtual vessel, estimated time and size, and every warning (stage
limits, nominal vessel geometry, environment setpoints that cannot be applied). Routines are
saved as JSON in the user data folder and can be recalled, edited and deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openevos.acquisition.capture import zstack_slices_centred
from openevos.acquisition.scan_plan import (
    AreaMethod,
    Beacon,
    Coverage,
    Rect,
    ScanRoutine,
    plan_scan,
)
from openevos.acquisition.scan_runner import ScanRunner
from openevos.acquisition.timelapse import (
    MAX_BEACONS,
    Scene,
    TimelapseRoutine,
    TimelapseRunner,
    estimate,
    populate_wells,
)
from openevos.instrument.profile import CameraRole
from openevos.ui.common import empty_combo, empty_hint, hbox, label, panel, render_frame
from openevos.ui.icons import icon
from openevos.ui.widgets.image_view import ImageView
from openevos.ui.widgets.vessel_map import VesselMap


def routines_dir(kind: str) -> Path:
    from platformdirs import user_data_dir

    p = Path(user_data_dir("OpenEVOS", appauthor=False)) / "routines" / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


#: plural field name -> what to ask the operator for (UI audit 2026-11-17: raw pydantic text reached the operator)
_ADD_AT_LEAST_ONE = {"beacons": "beacon", "channels": "channel", "regions": "region", "wells": "well",
                     "scenes": "scene", "timepoints": "time point"}


def _friendly(e: dict) -> str:
    loc = [str(x) for x in e.get("loc", ())]
    field = loc[0] if loc else "routine"
    kind, msg = e.get("type", ""), str(e.get("msg", "")).replace("Value error, ", "")
    if kind == "too_short" and field in _ADD_AT_LEAST_ONE:
        return f"Add at least one {_ADD_AT_LEAST_ONE[field]}"
    if kind == "missing":
        return f"{field.replace('_', ' ').capitalize()} is required"
    name = " ".join(loc).replace("_", " ")
    return f"{name.capitalize()}: {msg}" if name else msg


def explain(exc: Exception) -> str:
    """Plain-language one-liners for validation errors."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return "; ".join(_friendly(e) for e in errors())
        except Exception:  # noqa: BLE001, S110 — fall back to the plain message
            pass
    return str(exc).split("\n")[0][:300]


def show_warnings(lab, warnings: list[str]) -> None:
    """One warning line at most (UI audit 2026-11-17: three permanent orange lines on Scan): the first warning, then
    '+N more' — all of them are in the tooltip."""
    ws = [w for w in warnings if w]
    if not ws:
        lab.setText("")
        lab.setToolTip("")
        return
    # the label's warning colour carries the meaning; no glyph standing in for an icon
    lab.setText(ws[0] + (f"  (+{len(ws) - 1} more, hover)" if len(ws) > 1 else ""))
    lab.setToolTip("\n".join(ws))


def _fmt_time(s: float) -> str:
    s = round(s)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


class _RoutineBase(QWidget):
    kind = "scan"

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        self.profile = self.session.profile

    def _optics_section(self, form: QFormLayout) -> None:
        self.objective = QComboBox()
        for o in self.profile.objectives:
            if o.installed:
                self.objective.addItem(o.label or o.short, o.slot)  # the label already names the magnification
        self.camera = QComboBox()
        for c in self.profile.cameras:
            self.camera.addItem(c.display_name, c.role)
        self.channels_box = QWidget()
        # a grid, three to a row: five channels in one row ran past the panel edge (TRANS clipped by PHASE, UI review
        # 2026-11-24)
        self.channels_lay = QGridLayout(self.channels_box)
        self.channels_lay.setContentsMargins(0, 0, 0, 0)
        self.channels_lay.setHorizontalSpacing(12)
        self.channels_lay.setVerticalSpacing(4)
        self.channel_checks: dict[str, QCheckBox] = {}
        form.addRow("Objective", self.objective)
        form.addRow("Camera", self.camera)
        form.addRow("Channels", self.channels_box)
        self.camera.currentIndexChanged.connect(self._fill_channels)
        self._fill_channels()

    def _fill_channels(self) -> None:
        for cb in self.channel_checks.values():
            cb.deleteLater()
        self.channel_checks.clear()
        role = CameraRole(self.camera.currentData() or 'mono')
        for i, c in enumerate(self.profile.channels_for(role)):
            cb = QCheckBox(c.display)
            cb.setChecked(c.slot in ("cube1", "cube2") or (role is CameraRole.COLOR and c.slot == "trans"))
            cb.toggled.connect(self.replan)
            self.channel_checks[c.slot] = cb
            self.channels_lay.addWidget(cb, i // 3, i % 3)
        self.replan()

    def selected_channels(self) -> list[str]:
        return [k for k, cb in self.channel_checks.items() if cb.isChecked()]

    def replan(self) -> None:  # overridden
        pass

    def _library(self, lay: QVBoxLayout) -> None:
        lay.addWidget(label("Stored routines", "section"))
        self.stored = QListWidget()
        empty_hint(self.stored, "No stored routines")
        self.stored.setMaximumHeight(110)
        lay.addWidget(self.stored)
        b_save, b_load, b_del = QPushButton("Save routine"), QPushButton("Recall / Edit"), QPushButton("Delete")
        self.btn_save = b_save  # disabled while the routine is not valid (audit APP-12)
        lay.addWidget(hbox(b_save, b_load, b_del, None))
        b_save.clicked.connect(self._save_routine)
        b_load.clicked.connect(self._load_routine)
        b_del.clicked.connect(self._delete_routine)
        self._refresh_library()

    def _refresh_library(self) -> None:
        self.stored.clear()
        for p in sorted(routines_dir(self.kind).glob("*.json")):
            self.stored.addItem(p.stem)

    def _save_routine(self) -> None:
        try:
            r = self.routine()
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("warn", f"routine not saved: {explain(exc)}")  # plain words, no validator text
            return
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in r.name)
        (routines_dir(self.kind) / f"{safe}.json").write_text(r.model_dump_json(indent=2), "utf-8")
        self.model.add_log("info", f"{self.kind} routine '{r.name}' saved")
        self._refresh_library()

    def _load_routine(self) -> None:
        it = self.stored.currentItem()
        if it:
            self.load(json.loads((routines_dir(self.kind) / f"{it.text()}.json").read_text("utf-8")))

    def _delete_routine(self) -> None:
        it = self.stored.currentItem()
        if it:
            (routines_dir(self.kind) / f"{it.text()}.json").unlink(missing_ok=True)
            self._refresh_library()

    def _run_controls(self, lay: QVBoxLayout) -> None:
        self.btn_run, self.btn_pause, self.btn_stop = QPushButton("Start"), QPushButton("Pause"), QPushButton("Stop")
        self.btn_run.setObjectName("primary")
        self.btn_stop.setObjectName("danger")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        lay.addWidget(hbox(self.btn_run, self.btn_pause, self.btn_stop, None))
        self.progress = QProgressBar()
        self.lbl_progress = label("", "muted", wrap=True)
        lay.addWidget(self.progress)
        lay.addWidget(self.lbl_progress)
        self.model.tasks.progress.connect(self._on_progress)
        # busy -> off; idle -> whatever the current plan allows (not blindly on: APP-14)
        self.model.tasks.busy_changed.connect(lambda b: self.btn_run.setEnabled(False) if b else self.replan())

    def _on_progress(self, name: str, i: int, n: int, msg: str) -> None:
        if name.startswith(self.kind.capitalize()):
            self.progress.setMaximum(max(1, n))
            self.progress.setValue(i)
            self.lbl_progress.setText(msg)


class ScanPage(_RoutineBase):
    kind = "scan"
    request_page = Signal(str)  # "zstack": show a tile's Z-stack on the Z-stack page

    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self.plan = None
        self.result = None
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        f, lay = panel("Scan routine")
        f.setFixedWidth(470)
        form = QFormLayout()
        self.name = QLineEdit("Scan 1")
        form.addRow("1  Name", self.name)
        form.addRow("   Vessel", label(self.session.vessel.display_name, "value"))
        self._optics_section(form)
        self.save_sep = QCheckBox("Save individual channel images separately")
        form.addRow("", self.save_sep)
        self.m_rect = QRadioButton("Select area rectangle")
        self.m_beacon = QRadioButton("Place two beacons (corners)")
        g = QButtonGroup(self)
        g.addButton(self.m_rect)
        g.addButton(self.m_beacon)
        self.m_rect.setChecked(True)
        form.addRow("2  Area", self.m_rect)
        form.addRow("", self.m_beacon)
        self.rx, self.ry, self.rw, self.rh = (QDoubleSpinBox(decimals=2, maximum=130, singleStep=0.5) for _ in range(4))
        self.rw.setValue(2.0)
        self.rh.setValue(2.0)
        self.rect_on = QCheckBox("Limit to a rectangle in each region")  # units are on the rows below
        form.addRow("", self.rect_on)
        form.addRow("   Offset x / y (mm)", hbox(self.rx, self.ry))
        form.addRow("   Size w / h (mm)", hbox(self.rw, self.rh))
        self.b1, self.b2 = QPushButton("Set beacon 1 (top-left) here"), QPushButton("Set beacon 2 (bottom-right) here")
        self.lbl_beacons = label("No beacons", "muted")
        form.addRow("   Beacons", QWidget())
        form.addRow(hbox(self.b1, self.b2))
        form.addRow("", self.lbl_beacons)
        self.find = QCheckBox("Find Sample (wide range, first field)")
        self.fine = QCheckBox("Auto Fine (narrow range, every field)")
        self.fine.setChecked(True)
        form.addRow("3  Focus", self.find)
        form.addRow("", self.fine)
        self.zstack = QCheckBox("Z-stack at every field")
        self.zstack.setToolTip("After the in-focus capture, a Z-stack per channel centred on that focus")
        self.z_range = QDoubleSpinBox(minimum=0.5, maximum=500, value=10, suffix=" µm range")
        self.z_step = QDoubleSpinBox(minimum=0.1, maximum=100, value=2, decimals=1, suffix=" µm step")
        form.addRow("", hbox(self.zstack, self.z_range, self.z_step))
        self.coverage = QComboBox()
        for c in Coverage:
            self.coverage.addItem(c.label, c)
        self.pct = QDoubleSpinBox(minimum=1, maximum=100, value=50, suffix=" %")
        self.nrand = QSpinBox(minimum=1, maximum=5000, value=5)
        self.rows, self.cols = QSpinBox(minimum=1, maximum=200, value=3), QSpinBox(minimum=1, maximum=200, value=3)
        self.overlap = QDoubleSpinBox(minimum=0, maximum=50, value=10, suffix=" % overlap")
        form.addRow("4  Coverage", self.coverage)
        form.addRow("   Percentage", self.pct)
        form.addRow("   Random images", self.nrand)
        form.addRow("   Grid rows × cols", hbox(self.rows, self.cols))
        form.addRow("   Tile overlap", self.overlap)
        self.folder = QLineEdit("")
        self.folder.setPlaceholderText("(keep in memory only)")
        br = QPushButton()  # was a bare "…": now a folder icon (UI audit 2026-11-17)
        br.setIcon(icon("folder"))
        br.setToolTip("Browse…")
        br.setFixedWidth(34)
        br.clicked.connect(lambda: self.folder.setText(QFileDialog.getExistingDirectory(self, "Save scans to") or
                                                       self.folder.text()))
        form.addRow("5  Save to", hbox(self.folder, br))
        lay.addLayout(form)
        self._library(lay)
        lay.addStretch(1)
        root.addWidget(f)

        g2, gl = panel("Plan & run")
        self.map = VesselMap(selectable=True)
        self.map.setMinimumHeight(300)
        gl.addWidget(label("Click wells to select them, or Tab to the map and use the arrow keys and Space "
                            "(none selected = all regions).", "muted"))
        gl.addWidget(self.map, 1)
        self.lbl_plan = label("", "value", wrap=True)
        self.lbl_warn = label("", "warn", wrap=True)
        gl.addWidget(self.lbl_plan)
        gl.addWidget(self.lbl_warn)
        self._run_controls(gl)
        self.mosaic = ImageView()
        self.mosaic.placeholder = "Scan mosaic appears here after a run"
        self.mosaic.setMinimumHeight(280)
        self.mosaic_ch = empty_combo(QComboBox(), "No result yet")
        self.quality = QComboBox()
        for q, ds in (("High (full resolution)", 1), ("Medium (½)", 2), ("Low (¼)", 4)):
            self.quality.addItem(q, ds)
        self.btn_export = QPushButton("Export mosaic…")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export_mosaic)
        gl.addWidget(hbox(label("Result", "section"), self.mosaic_ch, None, label("Quality", "rowlabel"),
                          self.quality, self.btn_export))
        self.stack_pick = QComboBox()
        self.stack_pick.setToolTip("Z-stacks this scan took (field · well · channel)")
        self.btn_open_stack = QPushButton("Open in Z-stack page")
        self.btn_open_stack.setIcon(icon("zstack"))
        self.btn_open_stack.setToolTip("Browse the stack with its slice map and playback (replaces the Z-stack held "
                                       "in memory; the scan keeps all of its stacks)")
        self.btn_open_stack.clicked.connect(self._open_stack)
        self.stack_row = hbox(label("Z-stacks", "section"), self.stack_pick, self.btn_open_stack)
        self.stack_row.setVisible(False)
        gl.addWidget(self.stack_row)
        gl.addWidget(self.mosaic, 1)
        root.addWidget(g2, 1)

        for w in (self.coverage, self.objective, self.mosaic_ch):
            w.currentIndexChanged.connect(self.replan)
        for w in (self.pct, self.nrand, self.rows, self.cols, self.overlap, self.rx, self.ry, self.rw, self.rh,
                  self.z_range, self.z_step):
            w.valueChanged.connect(self.replan)
        for w in (self.find, self.fine, self.m_rect, self.rect_on, self.zstack):
            w.toggled.connect(self.replan)
        self.map.region_clicked.connect(lambda _r: self.replan())
        self.b1.clicked.connect(lambda: self._beacon(0))
        self.b2.clicked.connect(lambda: self._beacon(1))
        self.btn_run.clicked.connect(self._run)
        self.btn_pause.toggled.connect(self._pause)
        self.btn_stop.clicked.connect(lambda: self.runner and self.runner.control.stop())
        self.mosaic_ch.currentIndexChanged.connect(self._show_mosaic)
        model.vessel_changed.connect(self._vessel)
        self.beacons: list[Beacon | None] = [None, None]
        self.runner: ScanRunner | None = None
        model.register_stopper(lambda: self.runner and self.runner.control.stop())
        self._vessel()

    def _vessel(self) -> None:
        self.map.set_vessel(self.session.vessel)
        self.replan()

    def _beacon(self, i: int) -> None:
        xy = self.session.controller.stage_xy()
        if xy is None:
            return
        x, y = self.session.stage_map.to_vessel(*xy)
        self.beacons[i] = Beacon(label=f"B{i + 1}", x_mm=x, y_mm=y)
        self.lbl_beacons.setText("  ".join(f"{b.label} ({b.x_mm:.2f}, {b.y_mm:.2f}) mm" for b in self.beacons if b))
        self.m_beacon.setChecked(True)
        self.replan()

    def routine(self) -> ScanRoutine:
        return ScanRoutine(
            name=self.name.text() or "Scan", vessel_id=self.session.vessel.vessel_id,
            objective_slot=self.objective.currentData() or 1, camera=CameraRole(self.camera.currentData() or 'mono'),
            channels=self.selected_channels(), save_channels_separately=self.save_sep.isChecked(),
            area_method=AreaMethod.RECTANGLE if self.m_rect.isChecked() else AreaMethod.BEACONS,
            regions=sorted(self.map.selected),
            rect=Rect(x=self.rx.value(), y=self.ry.value(), w=self.rw.value(), h=self.rh.value())
            if self.rect_on.isChecked() else None,
            beacons=[b for b in self.beacons if b] if not self.m_rect.isChecked() else [],
            find_sample=self.find.isChecked(), auto_fine=self.fine.isChecked(),
            zstack=self.zstack.isChecked(), zstack_range_um=self.z_range.value(), zstack_step_um=self.z_step.value(),
            coverage=Coverage(self.coverage.currentData()), percentage=self.pct.value(), random_images=self.nrand.value(),  # noqa: E501
            grid_rows=self.rows.value(), grid_cols=self.cols.value(), overlap_percent=self.overlap.value(),
            output_folder=self.folder.text())

    def load(self, d: dict) -> None:
        r = ScanRoutine.model_validate(d)
        if r.vessel_id != self.session.vessel.vessel_id:
            self.session.select_vessel(r.vessel_id)
        self.name.setText(r.name)
        self.objective.setCurrentIndex(max(0, self.objective.findData(r.objective_slot)))
        self.camera.setCurrentIndex(max(0, self.camera.findData(r.camera)))
        for k, cb in self.channel_checks.items():
            cb.setChecked(k in r.channels)
        self.map.selected = set(r.regions)
        self.coverage.setCurrentIndex(self.coverage.findData(r.coverage))
        self.find.setChecked(r.find_sample)
        self.fine.setChecked(r.auto_fine)
        self.zstack.setChecked(r.zstack)
        self.z_range.setValue(r.zstack_range_um)
        self.z_step.setValue(r.zstack_step_um)
        self.folder.setText(r.output_folder)
        self.replan()

    def replan(self) -> None:
        if not hasattr(self, "map"):
            return
        c = Coverage(self.coverage.currentData())
        self.pct.setEnabled(c in (Coverage.CENTER, Coverage.OUTER))
        self.nrand.setEnabled(c is Coverage.RANDOM)
        self.rows.setEnabled(c is Coverage.GRID)
        self.cols.setEnabled(c is Coverage.GRID)
        ctl = self.session.controller
        lx, ly = ctl.axis_limits(self.profile.stage_device, "x"), ctl.axis_limits(self.profile.stage_device, "y")
        if self.m_rect.isChecked() and not self.map.selected:
            # a fresh page plans nothing: no wells selected used to mean the whole plate (a 19 h, 18 GB plan that could
            # not start, audit APP-14). The planner keeps that meaning for saved routines.
            self.plan = None
            self.map.tiles_mm = []
            self.map.update()
            self.lbl_plan.setText("No wells selected")
            self.lbl_warn.setText("Select wells on the map to plan a scan")
            self._run_state(False, "Select wells on the map first")
            return
        try:
            r = self.routine()
            self.plan = plan_scan(r, self.session.vessel, self.profile, self.session.stage_map,
                                  (lx, ly) if lx and ly else None)
        except Exception as exc:  # noqa: BLE001
            self.plan = None
            self.map.tiles_mm = []
            self.lbl_plan.setText("Plan not valid")
            self.lbl_warn.setText(explain(exc))
            self._run_state(False, explain(exc))
            self.map.update()
            return
        p = self.plan
        self.z_range.setEnabled(r.zstack)
        self.z_step.setEnabled(r.zstack)
        self.map.fov_mm = p.fov_mm
        self.map.tiles_mm = [(t.x_mm, t.y_mm) for t in p.tiles]
        self.map.beacons_mm = [(b.x_mm, b.y_mm, b.label) for b in r.beacons]
        self.map.update()
        n_z = zstack_slices_centred(r.zstack_range_um, r.zstack_step_um)
        zs = (f" · Z-stack {n_z} slices of {r.zstack_step_um:g} µm"
              if r.zstack else "")
        self.lbl_plan.setText(f"{len(p.tiles)} fields × {len(r.channels)} channel(s){zs} · FOV "
                              f"{p.fov_mm[0] * 1000:.0f}×{p.fov_mm[1] * 1000:.0f} µm · est. "
                              f"{_fmt_time(p.est_seconds)} · {_fmt_bytes(p.est_bytes)}")
        show_warnings(self.lbl_warn, p.warnings)
        why = ("the plan has no fields" if not p.tiles else
               f"{p.out_of_limits} field(s) are outside the stage limits" if p.out_of_limits else
               "the instrument is busy" if self.model.tasks.busy else "")
        self._run_state(not why, why)

    def _run_state(self, ok: bool, why: str = "") -> None:
        """Start is enabled only for a runnable plan; when it is not, the reason is on the button (audit APP-14)."""
        if hasattr(self, "btn_run"):
            self.btn_run.setEnabled(ok)
            self.btn_run.setToolTip("" if ok else f"Cannot start: {why}")

    def _run(self) -> None:
        self.replan()
        if not self.plan:
            return
        from openevos.acquisition.stage_recovery import StageAutoRecovery

        s = self.session
        self.runner = ScanRunner(s.capture, s.autofocus, s.vessel,
                                 auto_fine_range_percent=s.settings.basic.auto_fine_range_percent,
                                 recovery=StageAutoRecovery(s, log=self.model.add_log))  # ADR-0067
        plan, runner = self.plan, self.runner
        s.controller.stop_live(s.settings.active_camera)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.model.submit_when_idle("Scan", lambda prog: runner.run(plan, prog), with_progress=True, on_done=self._done)

    def _pause(self, on: bool) -> None:
        if self.runner:
            (self.runner.control.pause if on else self.runner.control.resume)()
            self.btn_pause.setText("Resume" if on else "Pause")

    def _done(self, res) -> None:
        self.result = res
        self.btn_pause.setEnabled(False)
        self.btn_pause.setChecked(False)
        self.btn_stop.setEnabled(False)
        self.lbl_progress.setText(res.message + (f" — saved to {res.output_folder}" if res.output_folder else ""))
        self.model.add_log("info" if res.completed else "warn", f"scan: {res.message}")
        self.mosaic_ch.blockSignals(True)
        self.mosaic_ch.clear()
        for slot in res.mosaics:
            ch = self.profile.channel(slot)
            self.mosaic_ch.addItem(ch.display if ch else slot, slot)
        self.mosaic_ch.blockSignals(False)
        self.btn_export.setEnabled(bool(res.mosaics))
        self._fill_stacks(res)
        self._show_mosaic()

    def _fill_stacks(self, res) -> None:
        self.stack_pick.clear()
        tiles = {t.index: t for t, _imgs in res.images}
        for idx, by_slot in sorted(res.stacks.items()):
            t = tiles.get(idx)
            where = f"field {idx + 1} · {t.region_id} r{t.row} c{t.col}" if t else f"field {idx + 1}"
            for slot, st in by_slot.items():
                ch = self.profile.channel(slot)
                part = "" if st.complete else f" · stopped {len(st.images)}/{st.planned}"
                self.stack_pick.addItem(f"{where} · {ch.display if ch else slot} · {len(st.images)} slices{part}",
                                        (idx, slot))
        self.stack_row.setVisible(self.stack_pick.count() > 0)
        self.btn_open_stack.setEnabled(self.stack_pick.count() > 0)

    def _open_stack(self) -> None:
        if not self.result or self.stack_pick.currentData() is None:
            return
        from openevos.acquisition.zstack import zstack_set_of

        idx, slot = self.stack_pick.currentData()
        by_slot = self.result.stacks.get(idx, {})
        if slot not in by_slot:
            return
        # every channel of the field, the one picked first (the Z-stack page shows the set's first channel)
        order = {slot: by_slot[slot], **{k: v for k, v in by_slot.items() if k != slot}}
        self.session.capture.buffers.set_zstack_set(zstack_set_of(order, self.result.plan.routine.camera,
                                                                  f"Scan field {idx + 1}"))
        self.request_page.emit("zstack")

    def _export_mosaic(self) -> None:
        from openevos.acquisition.scan_runner import build_mosaic
        from openevos.imaging.ome import write_ome_tiff

        if not self.result or not self.result.images:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export mosaic", f"{self.result.plan.routine.name}.ome.tif",
                                              "OME-TIFF (*.ome.tif);;PNG (*.png)")
        if not path:
            return
        ds = self.quality.currentData()
        slots = list(self.result.mosaics)
        full = []
        for slot in slots:
            m, scale = build_mosaic(self.result.images, slot, max_px=20000)
            full.append((slot, m[::ds, ::ds] if m is not None else None, scale * ds if scale else None))
        if path.lower().endswith(".png"):
            from PIL import Image

            slot, m, _sc = next((x for x in full if x[0] == self.mosaic_ch.currentData()), full[0])
            Image.fromarray(render_frame(m, slot, self.profile, self.session.settings)).save(path)
        else:
            mono = [(s_, m) for s_, m, _ in full if m is not None and m.ndim == 2]
            if not mono:
                self.model.add_log("warn", "OME export needs monochrome channels")
                return
            arr = np.stack([m for _s, m in mono])[None, None]
            write_ome_tiff(Path(path), arr, pixel_um=full[0][2], channel_names=[s_ for s_, _m in mono],
                           description="SIMULATED synthetic frames" if any(
                               i.synthetic for _t, d in self.result.images for i in d.values()) else "OpenEVOS scan")
        self.model.add_log("info", f"mosaic exported ({self.quality.currentText()}) to {path}")

    def _show_mosaic(self) -> None:
        if not self.result or not self.mosaic_ch.count():
            return
        slot = self.mosaic_ch.currentData()
        m = self.result.mosaics.get(slot)
        if m is None:
            return
        self.mosaic.set_image(render_frame(m, slot, self.profile, self.session.settings))
        self.mosaic.pixel_um = self.result.mosaic_scale_um_per_px
        self.mosaic.badge = "MOSAIC • SIMULATED"


class TimelapsePage(_RoutineBase):
    kind = "timelapse"
    request_page = Signal(str)  # "zstack": show a routine Z-stack on the Z-stack page

    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self.result = None
        self.runner: TimelapseRunner | None = None
        self.beacons: list[Beacon] = []
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        f, lay = panel("Time-lapse routine")
        f.setFixedWidth(500)
        form = QFormLayout()
        self.name = QLineEdit("Time lapse 1")
        form.addRow("1  Name", self.name)
        self._optics_section(form)
        self.af_find = QCheckBox("Auto find sample")
        self.af_fine = QCheckBox("Auto fine focus")
        self.af_fine.setChecked(True)
        self.zstack = QCheckBox("Z-stack")
        self.z_range = QDoubleSpinBox(minimum=0.5, maximum=500, value=10, suffix=" µm range")
        self.z_step = QDoubleSpinBox(minimum=0.1, maximum=100, value=2, decimals=1, suffix=" µm step")
        self.auto_light = QCheckBox("Auto lighting (not available)")
        self.auto_light.setEnabled(False)
        self.auto_light.setToolTip("No validated auto-exposure algorithm yet; fixed channel settings are used")
        form.addRow("2  Options", hbox(self.af_find, self.af_fine))
        form.addRow("", hbox(self.zstack, self.z_range, self.z_step))
        form.addRow("", self.auto_light)
        lay.addLayout(form)
        lay.addWidget(label(f"3  Beacons (up to {MAX_BEACONS})", "section"))
        self.blist = QListWidget()
        empty_hint(self.blist, "No beacons yet: move to a position and click Add beacon here")
        self.blist.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.blist.setMaximumHeight(120)
        lay.addWidget(self.blist)
        b_add, b_pop, b_upd, b_del, b_all = (QPushButton(t) for t in ("Add beacon here", "Populate Wells",
                                                                         "Update selected", "Delete selected",
                                                                         "Delete all"))
        lay.addWidget(hbox(b_add, b_pop, b_upd))
        lay.addWidget(hbox(b_del, b_all, None))
        lay.addWidget(label("4  Scenes", "section"))
        self.scenes = QTableWidget(0, 5)
        self.scenes.setHorizontalHeaderLabels(["Length (min)", "Interval (min)", "Temp °C", "CO₂ %", "O₂ %"])
        self.scenes.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.scenes.setMaximumHeight(120)
        lay.addWidget(self.scenes)
        s_add, s_del = QPushButton("Add scene"), QPushButton("Remove scene")
        self.one_frame = QCheckBox("Capture one frame only (single scene)")
        lay.addWidget(hbox(s_add, s_del, self.one_frame, None))
        self.video = QCheckBox("Video per beacon")
        self.video.setChecked(True)
        self.water = QCheckBox("Watermark")
        self.water.setChecked(True)
        self.sep = QCheckBox("Save channels separately")
        # two rows: three checkboxes and the label ran past the panel edge (UI review 2026-11-24)
        out = QGridLayout()
        out.setHorizontalSpacing(12)
        out.addWidget(label("5  Output", "muted"), 0, 0)
        out.addWidget(self.video, 0, 1)
        out.addWidget(self.water, 0, 2)
        out.addWidget(self.sep, 1, 1, 1, 2)
        out.setColumnStretch(3, 1)
        lay.addLayout(out)
        from openevos.ui.pages.save import default_capture_folder

        self.z_folder = QLineEdit(str(default_capture_folder() / "Time-lapse"))
        self.z_folder.setPlaceholderText("(keep Z-stacks in memory only)")
        self.z_folder.setToolTip("With Z-stack ticked, every stack is written here (folder/routine name) as soon "
                                 "as it is taken; empty keeps them in memory only")
        zb = QPushButton()
        zb.setIcon(icon("folder"))
        zb.setToolTip("Browse…")
        zb.setFixedWidth(34)
        zb.clicked.connect(lambda: self.z_folder.setText(
            QFileDialog.getExistingDirectory(self, "Save time-lapse Z-stacks to") or self.z_folder.text()))
        self.z_folder_row = hbox(label("   Save Z-stacks to", "muted"), self.z_folder, zb)
        lay.addWidget(self.z_folder_row)
        self._library(lay)
        lay.addStretch(1)
        root.addWidget(f)

        g, gl = panel("Plan & in progress")
        self.map = VesselMap(interactive=False)
        self.map.setMinimumHeight(240)
        gl.addWidget(self.map)
        self.lbl_est = label("", "value", wrap=True)
        self.lbl_warn = label("", "warn", wrap=True)
        gl.addWidget(self.lbl_est)
        gl.addWidget(self.lbl_warn)
        self._run_controls(gl)
        gl.addWidget(label("Latest time point", "section"))
        self.grid = QGridLayout()
        self.grid.setSpacing(6)
        gw = QWidget()
        gw.setLayout(self.grid)
        gl.addWidget(gw, 1)
        root.addWidget(g, 1)

        b_add.clicked.connect(self._add_beacon)
        b_pop.clicked.connect(self._populate)
        b_upd.clicked.connect(self._update_beacon)
        b_del.clicked.connect(self._delete_selected)
        b_all.clicked.connect(lambda: (self.beacons.clear(), self._beacons_changed()))
        s_add.clicked.connect(lambda: self._add_scene())
        s_del.clicked.connect(lambda: (self.scenes.removeRow(self.scenes.rowCount() - 1), self.replan()))
        self.scenes.itemChanged.connect(lambda _i: self.replan())
        for w in (self.af_find, self.af_fine, self.zstack, self.one_frame):
            w.toggled.connect(self.replan)
        for w in (self.z_range, self.z_step):
            w.valueChanged.connect(self.replan)
        self.btn_ome = QPushButton("Export OME-TIFF per beacon…")
        self.btn_ome.setEnabled(False)
        self.btn_ome.setToolTip("Run a time-lapse first")
        self.btn_ome.clicked.connect(self._export_ome)
        g.layout().insertWidget(g.layout().count() - 2, self.btn_ome)
        self.stack_pick = QComboBox()
        self.stack_pick.setToolTip("Z-stacks this time-lapse took (time point · beacon · channel)")
        self.btn_open_stack = QPushButton("Open in Z-stack page")
        self.btn_open_stack.setIcon(icon("zstack"))
        self.btn_open_stack.setToolTip("Browse the stack with its slice map and playback (replaces the Z-stack held "
                                       "in memory; the time-lapse keeps all of its stacks)")
        self.btn_open_stack.clicked.connect(self._open_stack)
        self.stack_row = hbox(self.stack_pick, self.btn_open_stack)
        self.stack_row.setVisible(False)
        g.layout().insertWidget(g.layout().count() - 2, self.stack_row)
        self.btn_run.clicked.connect(self._run)
        self.btn_pause.toggled.connect(self._pause)
        self.btn_stop.clicked.connect(lambda: self.runner and self.runner.control.stop())
        model.vessel_changed.connect(self._vessel)
        model.register_stopper(lambda: self.runner and self.runner.control.stop())
        self._add_scene(2, 0.5)
        self._vessel()

    def _vessel(self) -> None:
        self.map.set_vessel(self.session.vessel)
        self.replan()

    def _add_scene(self, length=60.0, interval=10.0) -> None:
        r = self.scenes.rowCount()
        self.scenes.blockSignals(True)
        self.scenes.insertRow(r)
        for c, v in enumerate((length, interval, "", "", "")):
            self.scenes.setItem(r, c, QTableWidgetItem(str(v)))
        self.scenes.blockSignals(False)
        self.replan()

    def _here(self, label_text: str) -> Beacon | None:
        xy = self.session.controller.stage_xy()
        if xy is None:
            return None
        x, y = self.session.stage_map.to_vessel(*xy)
        return Beacon(label=label_text, x_mm=x, y_mm=y, z_um=self.session.controller.focus_z())

    def _add_beacon(self) -> None:
        if len(self.beacons) >= MAX_BEACONS:
            return
        b = self._here(f"P{len(self.beacons) + 1}")
        if b:
            self.beacons.append(b)
            self._beacons_changed()

    def _update_beacon(self) -> None:
        row = self.blist.currentRow()
        if 0 <= row < len(self.beacons):
            b = self._here(self.beacons[row].label)
            if b:
                self.beacons[row] = b
                self._beacons_changed()

    def _delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.blist.selectedIndexes()}, reverse=True)
        for r in rows:
            self.beacons.pop(r)
        self._beacons_changed()

    def _populate(self) -> None:
        self.beacons = populate_wells(self.session.vessel)
        self._beacons_changed()

    def _beacons_changed(self) -> None:
        self.blist.clear()
        for b in self.beacons:
            self.blist.addItem(f"{b.label}   ({b.x_mm:.2f}, {b.y_mm:.2f}) mm" + (f"   z {b.z_um:.1f}" if b.z_um else ""))  # noqa: E501
        self.replan()

    def _scenes(self) -> list[Scene]:
        out = []
        for r in range(self.scenes.rowCount()):
            def val(c, r=r):
                it = self.scenes.item(r, c)
                t = it.text().strip() if it else ""
                return float(t) if t else None

            out.append(Scene(duration_s=(val(0) or 0) * 60, interval_s=(val(1) or 0) * 60, temperature_c=val(2),
                             co2_percent=val(3), o2_percent=val(4)))
        return out

    def routine(self) -> TimelapseRoutine:
        return TimelapseRoutine(
            name=self.name.text() or "Time lapse", vessel_id=self.session.vessel.vessel_id,
            objective_slot=self.objective.currentData() or 1, camera=CameraRole(self.camera.currentData() or 'mono'),
            channels=self.selected_channels(), auto_find_sample=self.af_find.isChecked(),
            auto_fine_focus=self.af_fine.isChecked(), zstack=self.zstack.isChecked(),
            zstack_range_um=self.z_range.value(), zstack_step_um=self.z_step.value(), beacons=self.beacons,
            scenes=self._scenes(), capture_one_frame_only=self.one_frame.isChecked(),
            video_per_beacon=self.video.isChecked(), watermark=self.water.isChecked(),
            save_channels_separately=self.sep.isChecked(), output_folder=self.z_folder.text().strip())

    def load(self, d: dict) -> None:
        r = TimelapseRoutine.model_validate(d)
        if r.vessel_id != self.session.vessel.vessel_id:
            self.session.select_vessel(r.vessel_id)
        self.name.setText(r.name)
        self.zstack.setChecked(r.zstack)
        self.z_range.setValue(r.zstack_range_um)
        self.z_step.setValue(r.zstack_step_um)
        if r.output_folder:
            self.z_folder.setText(r.output_folder)
        self.beacons = list(r.beacons)
        self.scenes.setRowCount(0)
        for s in r.scenes:
            self._add_scene(s.duration_s / 60, s.interval_s / 60)
        self._beacons_changed()

    def replan(self) -> None:
        if not hasattr(self, "map"):
            return
        self.map.beacons_mm = [(b.x_mm, b.y_mm, b.label) for b in self.beacons]
        self.map.update()
        try:
            r = self.routine()
            e = estimate(r, self.profile, environment_available=False)
        except Exception as exc:  # noqa: BLE001
            self.lbl_est.setText("Routine not valid")
            self.lbl_warn.setText(explain(exc))
            if hasattr(self, "btn_run"):
                self.btn_run.setEnabled(False)
            if hasattr(self, "btn_save"):
                self.btn_save.setEnabled(False)
                self.btn_save.setToolTip(explain(exc))
            return
        self.lbl_est.setText(f"{len(r.beacons)} beacon(s) · {e.timepoints} time point(s) · {e.images} images · "
                             f"cycle ≈ {_fmt_time(e.cycle_s)} · total ≈ {_fmt_time(e.total_s)}")
        show_warnings(self.lbl_warn, e.warnings)
        if hasattr(self, "btn_run"):
            self.btn_run.setEnabled(not self.model.tasks.busy)
        if hasattr(self, "btn_save"):
            self.btn_save.setEnabled(True)
            self.btn_save.setToolTip("")

    def _run(self) -> None:
        try:
            r = self.routine()
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("error", f"time-lapse: {exc}")
            return
        s = self.session
        sr = ScanRunner(s.capture, s.autofocus, s.vessel, auto_fine_range_percent=s.settings.basic.auto_fine_range_percent)  # noqa: E501
        self.runner = TimelapseRunner(sr, s.stage_map, s.core.clock.monotonic)
        runner = self.runner
        s.controller.stop_live(s.settings.active_camera)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        disp = self.model.dispatcher
        self.model.submit_when_idle("Timelapse", lambda prog: runner.run(
            r, prog, on_timepoint=lambda tp, batch: disp(lambda: self._show_batch(tp, batch))),
            with_progress=True, on_done=self._done)

    def _pause(self, on: bool) -> None:
        if self.runner:
            (self.runner.control.pause if on else self.runner.control.resume)()
            self.btn_pause.setText("Resume" if on else "Pause")

    def _show_batch(self, tp: int, batch) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, (lab, imgs) in enumerate(batch[:12]):
            slot, img = next(iter(imgs.items()))
            v = ImageView()
            v.setMinimumSize(200, 150)
            v.show_scale_bar = False
            v.badge = f"{lab} • t{tp}"
            v.set_image(render_frame(img.data, slot, self.profile, self.session.settings))
            self.grid.addWidget(v, i // 3, i % 3)

    def _done(self, res) -> None:
        self.result = res
        self.btn_pause.setEnabled(False)
        self.btn_pause.setChecked(False)
        self.btn_stop.setEnabled(False)
        self.btn_ome.setEnabled(bool(res.frames))
        self.btn_ome.setToolTip("One OME-TIFF per beacon: T × Z × C × Y × X with the Z-stacks, else T × C × Y × X")
        n_stacks = len(res.stacks) + len(res.stack_folders)
        saved = f" saved to {res.output_folder}" if res.stack_folders else ""
        self.lbl_progress.setText(res.message + (f" · {res.late_timepoints} late" if res.late_timepoints else "")
                                  + (f" · {n_stacks} Z-stacks{saved}" if n_stacks else ""))
        self.model.add_log("info" if res.completed else "warn", f"time-lapse: {res.message}")
        self._fill_stacks(res)

    def _fill_stacks(self, res) -> None:
        self.stack_pick.clear()
        for (tp, beacon), by_slot in sorted(res.stacks.items(), key=lambda kv: kv[0]):
            for slot, st in by_slot.items():
                ch = self.profile.channel(slot)
                name = ch.display if ch else slot
                part = "" if st.complete else f" · stopped {len(st.images)}/{st.planned}"
                self.stack_pick.addItem(f"t{tp + 1} · {beacon} · {name} · {len(st.images)} slices{part}",
                                        ((tp, beacon), slot))
        for (tp, beacon), folder in sorted(res.stack_folders.items(), key=lambda kv: kv[0]):
            self.stack_pick.addItem(f"t{tp + 1} · {beacon} · saved in {Path(folder).name}", ("folder", folder))
        self.stack_row.setVisible(self.stack_pick.count() > 0)
        self.btn_open_stack.setEnabled(self.stack_pick.count() > 0)

    def _open_stack(self) -> None:
        if not self.result or self.stack_pick.currentData() is None:
            return
        from openevos.acquisition.zstack import zstack_set_of

        key, slot = self.stack_pick.currentData()
        if key == "folder":  # written to disk during the run and dropped from memory: read it back
            from openevos.imaging.zstack_io import load_zstack_set

            try:
                zs = load_zstack_set(Path(slot))
            except (OSError, ValueError, KeyError) as exc:
                self.model.add_log("warn", f"Z-stack {slot}: {exc}")
                return
            self.session.capture.buffers.set_zstack_set(zs)
            self.request_page.emit("zstack")
            return
        by_slot = self.result.stacks.get(key, {})
        if slot not in by_slot:
            return
        order = {slot: by_slot[slot], **{k: v for k, v in by_slot.items() if k != slot}}
        self.session.capture.buffers.set_zstack_set(zstack_set_of(order, self.result.routine.camera,
                                                                  f"time point {key[0] + 1}, {key[1]}"))
        self.request_page.emit("zstack")

    def _export_ome(self) -> None:
        from openevos.imaging.ome import timelapse_to_ome

        if not self.result or not self.result.frames:
            return
        d = QFileDialog.getExistingDirectory(self, "Export OME-TIFF to")
        if not d:
            return
        interval = self.result.routine.scenes[0].interval_s
        n = 0
        for b in dict.fromkeys(lab for _tp, lab, _i in self.result.frames):
            try:
                timelapse_to_ome(Path(d) / f"{self.result.routine.name}_{b}.ome.tif", self.result, b, interval)
                n += 1
            except ValueError as exc:
                self.model.add_log("warn", f"OME export {b}: {exc}")
        self.model.add_log("info", f"exported {n} OME-TIFF file(s) to {d}")


