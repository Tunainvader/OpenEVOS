"""Image > Edit (manual p.11-17): Overlay, Measure, Count, Auto Count — Craftimon inspector conventions.

Overlay   per-channel ON/OFF, brightness and contrast (display only, logged), Reset all, Clear channel.
Measure   text box, arrow, line (intensity profile), rectangle, ellipse, histogram ROI — up to 12 ROIs;
          per-channel statistics, plot, units, Show annotations, Delete selected/all, Export all (CSV).
Count     up to six labels with colour, running tally + %, total, grid, Delete all tags, right-click removes.
Auto count Brightness / Area / Circularity, Separate touching cells, Aggressive boundaries, Show cells,
          Count / Count from file, results table + CSV.
Annotations and tags are kept per image and burnt into saved images when the Save page asks for it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openevos.analysis.annotations import (
    MAX_ROIS,
    AnnKind,
    Annotation,
    AnnotationSet,
    line_profile,
    roi_histogram,
    roi_stats,
    to_csv,
)
from openevos.analysis.count import (
    DEFAULT_LABEL_COLORS,
    MAX_LABELS,
    AutoCountParams,
    ManualCount,
    auto_count,
)
from openevos.imaging.composite import Layer, auto_range, composite
from openevos.ui import theme
from openevos.ui.common import channel_color, hbox, label, panel, render_frame
from openevos.ui.icons import icon
from openevos.ui.widgets.image_view import ImageView, Marker
from openevos.ui.widgets.param import ParamRow
from openevos.ui.widgets.plot import MiniPlot

TOOLS = [(AnnKind.TEXT, "text"), (AnnKind.ARROW, "arrow"), (AnnKind.LINE, "line"), (AnnKind.RECT, "rect"),
         (AnnKind.ELLIPSE, "ellipse"), (AnnKind.HISTOGRAM, "histogram")]


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    return t


class AnalysisPage(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        self.current = None
        self.current_key: str | None = None
        self.auto = None
        self.channel_display: dict[str, tuple[float, float]] = {}  # slot -> (brightness, contrast)
        # per-image state shared with the Save page
        model.annotations = getattr(model, "annotations", {})
        model.counts = getattr(model, "counts", {})
        model.count_grid = getattr(model, "count_grid", None)
        model.analysis_page = self
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        g, gl = panel(None, margins=10)
        top = QHBoxLayout()
        top.addWidget(label("Analysis", "title"))
        top.addStretch(1)
        self.source = QComboBox()
        self.source.setAccessibleName("Image to analyse")
        top.addWidget(label("Image", "rowlabel"))
        top.addWidget(self.source)
        b_file = QPushButton("Count from file…")
        b_file.clicked.connect(self.open_file_dialog)
        top.addWidget(b_file)
        gl.addLayout(top)
        self.view = ImageView()
        self.view.placeholder = "Capture images first (Capture workspace) or open a file"
        self.view.set_click_mode(True)
        gl.addWidget(self.view, 1)
        self.lbl_hint = label("", "caption")
        gl.addWidget(self.lbl_hint)
        root.addWidget(g, 1)

        f, lay = panel("Tools")
        f.setFixedWidth(400)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._overlay_tab(), "Overlay")
        self.tabs.addTab(self._measure_tab(), "Measure")
        self.tabs.addTab(self._count_tab(), "Count")
        self.tabs.addTab(self._auto_tab(), "Auto count")
        root.addWidget(f)

        self.source.currentIndexChanged.connect(self._load)
        self.view.clicked.connect(self._click)
        self.view.dragged.connect(self._drag)
        self.view.hovered.connect(lambda x, y, v: self.lbl_hint.setText(f"x {x}  y {y}  ·  {v}") if v else None)
        self.tabs.currentChanged.connect(lambda _i: self._render())
        model.buffers_changed.connect(self._sources)

    # ================================================================== tabs
    def _overlay_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Channels", "section"))
        self.ov_box = QVBoxLayout()
        lay.addLayout(self.ov_box)
        self.ov_checks: dict[str, QCheckBox] = {}
        self.ov_pick = QComboBox()
        self.ov_pick.setAccessibleName("Channel to adjust")
        lay.addWidget(hbox(label("Adjust", "rowlabel"), self.ov_pick, None))
        self.ov_b = ParamRow("Brightness", -100, 100, 0, step=1)
        self.ov_c = ParamRow("Contrast", -100, 100, 0, step=1)
        lay.addWidget(self.ov_b)
        lay.addWidget(self.ov_c)
        b_reset, b_clear = QPushButton("Reset all channels"), QPushButton("Clear channel")
        b_clear.setObjectName("danger")
        lay.addWidget(hbox(b_reset, b_clear, None))
        lay.addWidget(label("Display only: raw data are not changed. Adjustments are written to the display log.",
                            "caption", wrap=True))
        lay.addStretch(1)
        self.ov_pick.currentIndexChanged.connect(self._ov_pick)
        self.ov_b.valueChanged.connect(self._ov_adjust)
        self.ov_c.valueChanged.connect(self._ov_adjust)
        b_reset.clicked.connect(self._ov_reset)
        b_clear.clicked.connect(self._ov_clear)
        return w

    def _measure_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Annotation tools", "section"))
        row = QHBoxLayout()
        row.setSpacing(3)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons: dict[AnnKind, QToolButton] = {}
        for kind, ic in TOOLS:
            b = QToolButton()
            b.setIcon(icon(ic))
            b.setText(kind.label)
            b.setToolTip(f"{kind.label}: drag on the image" + (" (click for text)" if kind is AnnKind.TEXT else ""))
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.setAccessibleName(kind.label)
            self.tool_group.addButton(b)
            self.tool_buttons[kind] = b
            row.addWidget(b)
        self.tool_buttons[AnnKind.LINE].setChecked(True)
        self.color_btn = QPushButton("Colour")
        self.ann_color = "#FFD24D"
        self.color_btn.setStyleSheet(f"border-left: 12px solid {self.ann_color};")
        row.addStretch(1)
        row.addWidget(self.color_btn)
        lay.addLayout(row)
        self.lbl_tool = label("Line — drag on the image", "caption")
        lay.addWidget(self.lbl_tool)
        self.ann_table = _table(["Name", "Type", "Measurement"])
        self.ann_table.setMinimumHeight(140)
        lay.addWidget(self.ann_table)
        self.show_ann = QCheckBox("Show annotations")
        self.show_ann.setChecked(True)
        self.units = QComboBox()
        self.units.addItem("µm", "µm")
        self.units.addItem("pixels", "px")
        lay.addWidget(hbox(self.show_ann, None, label("Units", "rowlabel"), self.units))
        b_del, b_all, b_csv = QPushButton("Delete selected"), QPushButton("Delete all"), QPushButton("Export all…")
        b_all.setObjectName("danger")
        lay.addWidget(hbox(b_csv, b_del, b_all, None))
        self.plot = MiniPlot()
        lay.addWidget(self.plot)
        self.stats_table = _table(["Channel", "Mean", "SD", "Min", "Max", "Area"])
        self.stats_table.setMinimumHeight(110)
        lay.addWidget(self.stats_table)
        self.color_btn.clicked.connect(self._pick_color)
        self.tool_group.buttonClicked.connect(lambda b: self.lbl_tool.setText(
            f"{b.text()} — " + ("click on the image to place text" if b is self.tool_buttons[AnnKind.TEXT]
                                 else "drag on the image")))
        self.ann_table.itemSelectionChanged.connect(self._show_stats)
        self.show_ann.toggled.connect(self._toggle_ann)
        self.units.currentIndexChanged.connect(lambda _i: (self._ann_table(), self._show_stats()))
        b_del.clicked.connect(self._del_ann)
        b_all.clicked.connect(lambda: (self.anns().clear(), self._ann_table(), self._render()))
        b_csv.clicked.connect(self._export_ann)
        return w

    def _count_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Labels", "section"))
        self.label_combo = QComboBox()
        self.new_label = QLineEdit()
        self.new_label.setPlaceholderText("Label name")
        b_add = QPushButton("Add label")
        lay.addWidget(hbox(label("Tag with", "rowlabel"), self.label_combo, None))
        lay.addWidget(hbox(self.new_label, b_add))
        lay.addWidget(label(f"Left-click adds a tag · right-click removes the nearest · up to {MAX_LABELS} labels",
                            "caption", wrap=True))
        self.count_table = _table(["Label", "Count", "%"])
        self.count_table.setMinimumHeight(170)
        lay.addWidget(self.count_table)
        self.show_tags = QCheckBox("Show tags")
        self.show_tags.setChecked(True)
        self.show_grid = QCheckBox("Show grid")
        self.grid_size = QComboBox()
        for n in (2, 3, 4, 5, 6, 8, 10):
            self.grid_size.addItem(f"{n} × {n}", n)
        self.grid_size.setCurrentIndex(2)
        lay.addWidget(hbox(self.show_tags, self.show_grid, self.grid_size, None))
        self.screenshot = QCheckBox("Save image as screenshot (labels, counts and %)")
        self.screenshot.setChecked(True)
        lay.addWidget(self.screenshot)
        b_clear = QPushButton("Delete all tags")
        b_clear.setObjectName("danger")
        lay.addWidget(b_clear, 0, Qt.AlignLeft)
        lay.addStretch(1)
        b_add.clicked.connect(self._add_label)
        b_clear.clicked.connect(lambda: (self.count().clear(), self._render()))
        for c in (self.show_tags, self.show_grid, self.screenshot):
            c.toggled.connect(lambda _on: self._render())
        self.grid_size.currentIndexChanged.connect(lambda _i: self._render())
        return w

    def _auto_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Sensitivity", "section"))
        self.s_bright = ParamRow("Brightness", 0, 100, 50, unit="%")
        self.s_area = ParamRow("Area", 0, 100, 50, unit="%")
        self.s_circ = ParamRow("Circularity", 0, 100, 50, unit="%")
        for r in (self.s_bright, self.s_area, self.s_circ):
            lay.addWidget(r)
        lay.addWidget(label("Toward 100 % = more sensitive (lower threshold), as in the original.", "caption", wrap=True))  # noqa: E501
        self.invert = QCheckBox("Dark objects on bright background")
        self.split = QCheckBox("Separate touching cells")
        self.split.setChecked(True)
        self.aggr = QCheckBox("Aggressive boundaries")
        self.show_cells = QCheckBox("Show cells")
        self.show_cells.setChecked(True)
        for c in (self.invert, self.split, self.aggr, self.show_cells):
            lay.addWidget(c)
        b_run = QPushButton("Count")
        b_run.setObjectName("primary")
        b_csv = QPushButton("Export CSV…")
        lay.addWidget(hbox(b_run, b_csv, None))
        self.lbl_auto = label("", "value", wrap=True)
        lay.addWidget(self.lbl_auto)
        self.obj_table = _table(["#", "Area", "Circ.", "Mean"])
        lay.addWidget(self.obj_table, 1)
        b_run.clicked.connect(self._auto)
        b_csv.clicked.connect(self._auto_csv)
        self.show_cells.toggled.connect(lambda _on: self._render())
        return w

    # ================================================================== image selection
    def showEvent(self, e):
        super().showEvent(e)
        self._sources()

    def _sources(self) -> None:
        cur = self.source.currentData()
        self.source.blockSignals(True)
        self.source.clear()
        bufs = self.session.capture.buffers.all()
        for slot, img in bufs.items():
            self.source.addItem(img.channel_label, slot)
        if self.current_key and self.current_key.startswith("file:"):
            self.source.addItem(Path(self.current_key[5:]).name, self.current_key)
        self.source.blockSignals(False)
        i = self.source.findData(cur)
        self.source.setCurrentIndex(max(i, 0))
        while self.ov_box.count():
            wd = self.ov_box.takeAt(0).widget()
            if wd:
                wd.deleteLater()
        self.ov_checks = {}
        self.ov_pick.blockSignals(True)
        self.ov_pick.clear()
        for slot, img in bufs.items():
            cb = QCheckBox(img.channel_label)
            cb.setChecked(not img.is_color)
            cb.toggled.connect(lambda _on: self._render())
            self.ov_checks[slot] = cb
            self.ov_box.addWidget(cb)
            if not img.is_color:
                self.ov_pick.addItem(img.channel_label, slot)
        self.ov_pick.blockSignals(False)
        self._load()

    def _load(self) -> None:
        key = self.source.currentData() if self.source.count() else None
        if key is not None and not str(key).startswith("file:"):
            new = self.session.capture.buffers.get(key)
        else:
            new = self.current if key and key == self.current_key else None
        if new is not self.current:
            self.current, self.current_key, self.auto = new, key, None
        self._ann_table()
        self._labels()
        self._render()

    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open image", "", "Images (*.tif *.tiff *.png *.jpg *.bmp)")
        if path:
            self.open_file(Path(path))

    def open_file(self, path: Path) -> None:
        from openevos.ui.pages.workspaces import load_image_file

        try:
            self.current = load_image_file(path)
        except Exception as exc:  # noqa: BLE001
            self.model.add_log("error", f"open {path}: {exc}")
            return
        self.current_key = f"file:{path}"
        self.auto = None
        self._sources()
        self.source.setCurrentIndex(self.source.findData(self.current_key))

    # per-image state
    def anns(self) -> AnnotationSet:
        return self.model.annotations.setdefault(self.current_key or "_", AnnotationSet())

    def count(self) -> ManualCount:
        return self.model.counts.setdefault(self.current_key or "_", ManualCount())

    # ================================================================== interaction
    def _tool(self) -> AnnKind:
        return next(k for k, b in self.tool_buttons.items() if b.isChecked())

    def _click(self, x: float, y: float, button) -> None:
        tab = self.tabs.currentIndex()
        if tab == 1 and self._tool() is AnnKind.TEXT and button == Qt.LeftButton:
            text, ok = QInputDialog.getText(self, "Text box", "Text")
            if ok and text:
                self.anns().add(Annotation(AnnKind.TEXT, (x, y), (x, y), color=self.ann_color, text=text))
                self._ann_table()
        elif tab == 2:
            mc = self.count()
            if button == Qt.RightButton:
                mc.remove_near(x, y)
            else:
                mc.add(x, y, self.label_combo.currentData() or 0)
        self._render()

    def _drag(self, x0: float, y0: float, x1: float, y1: float) -> None:
        if self.tabs.currentIndex() != 1 or self.current is None:
            return
        kind = self._tool()
        if kind is AnnKind.TEXT:
            return
        try:
            self.anns().add(Annotation(kind, (x0, y0), (x1, y1), color=self.ann_color))
        except ValueError as exc:
            self.model.add_log("warn", str(exc))
            return
        self._ann_table()
        self.ann_table.selectRow(len(self.anns().items) - 1)
        self._render()

    def _pick_color(self) -> None:
        from PySide6.QtGui import QColor

        c = QColorDialog.getColor(QColor(self.ann_color), self)
        if c.isValid():
            self.ann_color = c.name()
            self.color_btn.setStyleSheet(f"border-left: 12px solid {self.ann_color};")

    def _toggle_ann(self, on: bool) -> None:
        self.anns().visible = on
        self._render()

    def _del_ann(self) -> None:
        rows = sorted({i.row() for i in self.ann_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.anns().remove(r)
        self._ann_table()
        self._render()

    def _channels(self) -> dict[str, np.ndarray]:
        """Underlying channels for statistics: every buffered channel of the same shape, else the image."""
        if self.current is None:
            return {}
        bufs = self.session.capture.buffers.all()
        same = {v.channel_label: v.data for v in bufs.values() if v.data.shape[:2] == self.current.data.shape[:2]}
        return same if self.current_key in bufs and same else {self.current.channel_label: self.current.data}

    def _px(self) -> float | None:
        return self.current.pixel_size_um if self.current is not None and self.units.currentData() == "µm" else None

    def _measure_text(self, a: Annotation) -> str:
        px = self._px()
        if a.kind in (AnnKind.LINE, AnnKind.ARROW):
            return f"{a.length_px() * px:.2f} µm" if px else f"{a.length_px():.1f} px"
        if a.kind.is_roi:
            area = int(a.mask(self.current.data.shape[:2]).sum()) if self.current is not None else 0
            return f"{area * px * px:.1f} µm²" if px else f"{area} px"
        return a.text

    def _ann_table(self) -> None:
        items = self.anns().items if self.current is not None else []
        self.ann_table.setRowCount(len(items))
        for r, a in enumerate(items):
            for c, v in enumerate((a.name, a.kind.label, self._measure_text(a))):
                self.ann_table.setItem(r, c, QTableWidgetItem(v))
        self.lbl_tool.setToolTip(f"{self.anns().roi_count} of {MAX_ROIS} regions of interest used")

    def _show_stats(self) -> None:
        rows = sorted({i.row() for i in self.ann_table.selectedIndexes()})
        if not rows or self.current is None:
            self.plot.set_data([])
            self.stats_table.setRowCount(0)
            return
        a = self.anns().items[rows[0]]
        chans = self._channels()
        colors = [channel_color(s, self.session.profile, self.session.settings)
                  for s in (self.session.capture.buffers.all() if self.current_key in self.session.capture.buffers.all()
                            else {})] or [theme.ACCENT]
        stats = roi_stats(a, chans, self._px())
        self.stats_table.setRowCount(len(stats))
        for r, st in enumerate(stats):
            area = st.get("area_um2") or st.get("area_px") or st.get("length_um") or st.get("length_px")
            for c, v in enumerate((st["channel"], st.get("mean"), st.get("sd", ""), st.get("min"), st.get("max"), area)):  # noqa: E501
                self.stats_table.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
        if a.kind is AnnKind.LINE:
            series = [(n, line_profile(d.mean(axis=2) if d.ndim == 3 else d, a.p0, a.p1), colors[i % len(colors)])
                      for i, (n, d) in enumerate(chans.items())]
            self.plot.set_data(series, "line", "distance (px)", f"Intensity profile — {a.name}")
        elif a.kind.is_roi:
            n, d = next(iter(chans.items()))
            counts, _ = roi_histogram(a, d, 64, self.current.max_value)
            self.plot.set_data([(n, counts, colors[0])], "bar", "intensity", f"Histogram — {a.name}")

    def _export_ann(self) -> None:
        if self.current is None:
            return
        rows = [r for a in self.anns().items for r in roi_stats(a, self._channels(), self._px())]
        path, _ = QFileDialog.getSaveFileName(self, "Export measurements", "measurements.csv")
        if path:
            Path(path).write_text(to_csv(rows), "utf-8")
            self.model.add_log("info", f"exported {len(rows)} measurement rows to {path}")

    # overlay -----------------------------------------------------------------------------------------
    def _ov_pick(self) -> None:
        b, c = self.channel_display.get(self.ov_pick.currentData(), (0.0, 0.0))
        self.ov_b.set_value(b)
        self.ov_c.set_value(c)

    def _ov_adjust(self, _v: float = 0.0) -> None:
        slot = self.ov_pick.currentData()
        if slot:
            self.channel_display[slot] = (self.ov_b.value(), self.ov_c.value())
            self._render()

    def _ov_reset(self) -> None:
        self.channel_display.clear()
        self._ov_pick()
        self._render()

    def _ov_clear(self) -> None:
        slot = self.ov_pick.currentData()
        if slot:
            self.session.capture.buffers.remove(slot)

    def overlay_layers(self, images) -> list[Layer]:
        out = []
        for img in images:
            ch = self.session.profile.channel(img.channel)
            fluo = ch is not None and ch.kind.value == "fluorescence"
            lo, hi = auto_range(img.data)
            b, c = self.channel_display.get(img.channel, (0.0, 0.0))
            span = (hi - lo) * max(0.05, 1 - 0.9 * c / 100)
            mid = (lo + hi) / 2 - b / 100 * (hi - lo) / 2
            out.append(Layer(img.data, channel_color(img.channel, self.session.profile, self.session.settings)
                             if fluo else "#FFFFFF", mid - span / 2, mid + span / 2, opacity=1.0 if fluo else 0.4))
        return out

    def display_log(self) -> list[str]:
        if not self.channel_display:
            return []
        return ["Overlay display adjustments (linear): " + ", ".join(
            f"{s}: brightness {b:+.0f}, contrast {c:+.0f}" for s, (b, c) in self.channel_display.items())]

    # count -------------------------------------------------------------------------------------------
    def _labels(self) -> None:
        mc = self.count()
        self.label_combo.clear()
        for i, n in enumerate(mc.labels):
            self.label_combo.addItem(icon("count", DEFAULT_LABEL_COLORS[i]), n, i)

    def _add_label(self) -> None:
        try:
            self.count().add_label(self.new_label.text().strip() or f"Label {len(self.count().labels) + 1}")
        except ValueError as exc:
            self.model.add_log("warn", str(exc))
        self.new_label.clear()
        self._labels()
        self._render()

    def count_burn(self) -> dict:
        """What the Save page burns in for this image (tags, totals, grid)."""
        mc = self.count()
        if not self.screenshot.isChecked() or not mc.markers:
            return {}
        pct = mc.percentages()
        return {"count_markers": [(x, y, DEFAULT_LABEL_COLORS[k], mc.labels[k]) for x, y, k in mc.markers],
                "count_summary": [f"{n}: {v} ({pct[n]:.1f}%)" for n, v in mc.counts().items()]
                + [f"Total: {sum(mc.counts().values())}"],
                "grid": self.grid_size.currentData() if self.show_grid.isChecked() else None}

    # auto count --------------------------------------------------------------------------------------
    def _auto(self) -> None:
        if self.current is None:
            return
        p = AutoCountParams(self.s_bright.value(), self.s_area.value(), self.s_circ.value(), self.invert.isChecked(),
                            self.split.isChecked(), self.aggr.isChecked())
        self.auto = auto_count(self.current.data, p, pixel_size_um=self.current.pixel_size_um)
        s = self.auto.summary(self.current.pixel_size_um)
        self.lbl_auto.setText(f"{s['count']} objects · mean area {s.get('mean_area_um2', s['mean_area_px'])} "
                              f"{'µm²' if 'mean_area_um2' in s else 'px'} · mean circularity {s['mean_circularity']}")
        self.obj_table.setRowCount(0)
        for o in self.auto.objects[:500]:
            r = self.obj_table.rowCount()
            self.obj_table.insertRow(r)
            area = f"{o.area_um2:.1f} µm²" if o.area_um2 else f"{o.area_px} px"
            for c, v in enumerate((str(o.label), area, f"{o.circularity:.2f}", f"{o.mean_intensity:.0f}")):
                self.obj_table.setItem(r, c, QTableWidgetItem(v))
        self._render()

    def _auto_csv(self) -> None:
        if not self.auto:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export counts", "counts.csv")
        if path:
            lines = ["label,x_px,y_px,area_px,area_um2,circularity,mean_intensity"]
            lines += [f"{o.label},{o.centroid[0]:.1f},{o.centroid[1]:.1f},{o.area_px},"
                      f"{'' if o.area_um2 is None else f'{o.area_um2:.2f}'},{o.circularity:.3f},{o.mean_intensity:.1f}"
                      for o in self.auto.objects]
            Path(path).write_text("\n".join(lines) + "\n", "utf-8")

    # ================================================================== rendering
    def _render(self) -> None:
        img = self.current
        prof, st = self.session.profile, self.session.settings
        self.view.markers, self.view.shapes = [], []
        if img is None:
            self.view.set_image(None)
            return
        tab = self.tabs.currentIndex()
        if tab == 0 and self.current_key in self.session.capture.buffers.all():
            bufs = self.session.capture.buffers.all()
            imgs = [v for k, v in bufs.items() if k in self.ov_checks and self.ov_checks[k].isChecked()
                    and not v.is_color and v.data.shape == img.data.shape]
            rgb = composite(self.overlay_layers(imgs)) if imgs else render_frame(img.data, img.channel, prof, st)
        else:
            rgb = render_frame(img.data, img.channel, prof, st, max_value=img.max_value)
        self.view.set_image(rgb, img.data)
        self.view.pixel_um = img.pixel_size_um
        self.view.pixel_calibrated = img.pixel_size_calibrated
        anns = self.anns()
        if anns.visible:
            for a in anns.items:
                txt = a.text if a.kind is AnnKind.TEXT else f"{a.name}  {self._measure_text(a)}"
                self.view.shapes.append((a.kind.value, a.p0, a.p1, a.color, txt))
        if tab == 2 and self.show_tags.isChecked():
            mc = self.count()
            self.view.markers = [Marker(x, y, DEFAULT_LABEL_COLORS[k], 7, mc.labels[k][:1]) for x, y, k in mc.markers]
            c, pct = mc.counts(), mc.percentages()
            self.count_table.setRowCount(0)
            for n, v in [*c.items(), ("Total", sum(c.values()))]:
                r = self.count_table.rowCount()
                self.count_table.insertRow(r)
                self.count_table.setItem(r, 0, QTableWidgetItem(n))
                self.count_table.setItem(r, 1, QTableWidgetItem(str(v)))
                self.count_table.setItem(r, 2, QTableWidgetItem(f"{pct.get(n, 100.0 if v else 0.0):.1f}"))
            if self.show_grid.isChecked():
                n = self.grid_size.currentData()
                h, w = img.data.shape[:2]
                for i in range(1, n):
                    self.view.shapes.append(("line", (w * i / n, 0), (w * i / n, h), "#FFFFFF", ""))
                    self.view.shapes.append(("line", (0, h * i / n), (w, h * i / n), "#FFFFFF", ""))
        elif tab == 3 and self.auto and self.show_cells.isChecked():
            self.view.markers = [Marker(o.centroid[0], o.centroid[1], theme.OK, max(3.0, np.sqrt(o.area_px / np.pi)))
                                 for o in self.auto.objects]
        hints = ["Toggle channels; adjust brightness/contrast per channel (display only).",
                 "Drag to draw · select a row for statistics and plot.",
                 "Left-click tags with the selected label · right-click removes the nearest tag.",
                 "Adjust sensitivity, then Count."]
        self.lbl_hint.setText(hints[tab] + ("   ·   SIMULATED image" if img.synthetic else ""))
        self.view.viewport().update()

    def retint(self) -> None:
        for kind, ic in TOOLS:
            self.tool_buttons[kind].setIcon(icon(ic))
        self._render()
