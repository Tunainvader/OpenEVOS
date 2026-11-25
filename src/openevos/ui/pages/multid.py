"""Multi-D Acquisition (ADR-0065; OpenEVOS Multi-Dimensional Acquisition Kit v1.0): Z x C x T x P in one plan.

Three steps along the top, the way the scientist works:

* **Plan.** One card per dimension, each with its own switch: Channels (always on), Z, Time, Positions; then the
  order and where the data goes. The right side answers "what will this do?" before anything moves: exact image
  counts, data size and duration (estimates that say what they come from), the pre-flight checks grouped into what
  must be fixed, what needs confirming and plain notes, and the first events in the order they will run. Plans save
  and load as JSON.
* **Acquire.** Where the run is in every dimension, the latest image, a timeline of timepoints (done, late, skipped),
  an event log and the writer's state. Pause takes effect at a safe boundary with the light off; Stop & keep data
  stops after the current move or exposure and keeps everything written.
* **Review.** Opens any experiment folder: step through T, P, C and Z, project a Z subrange, read the metadata;
  volumes not acquired are listed, never hidden.

Positions on the instrument: the XY stage is not yet qualified (kit gate G-XY), so Acquire asks the operator to confirm
the moves first (ADR-0065 addendum 2026-11-25); the simulator runs them without asking. A failing pre-flight check
that has a fix offers it as a button that says what it changes (it edits the plan, never moves anything).
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.mda import (
    PHYSICAL_POSITIONS_QUALIFIED,
    STORAGE_RESERVE_MB,
    ChannelPlan,
    FocusPolicy,
    MDAFix,
    Missed,
    MultiDimPlan,
    PositionPlan,
    Schedule,
    TimeAxisPlan,
    Traversal,
    ZAxisPlan,
    preflight,
    summarize,
)
from openevos.acquisition.mda_runner import MDAProgress, MDAResult, MDAState, MultiDimRunner
from openevos.acquisition.zstack import fmt_seconds
from openevos.imaging.mda_store import ExperimentReader, ExperimentWriter
from openevos.imaging.zproject import METHODS, project
from openevos.ui import theme
from openevos.ui.common import hbox, label, panel, render_frame
from openevos.ui.pages.zstack import ChannelChips, _Elided, _NoHeightForWidth
from openevos.ui.widgets.image_view import ImageView
from openevos.ui.widgets.mda_widgets import (
    AxisBar,
    DimChip,
    KeyValues,
    PositionMap,
    ZPlanRuler,
    checklist_html,
    dim_color,
)
from openevos.ui.widgets.segmented import SegmentedControl

TASK = "Multi-D acquisition"
PROJECTIONS = ("max", "mean", "min", "std")
STEPS = [("plan", "Plan"), ("acquire", "Acquire"), ("review", "Review")]
PRESETS: list[tuple[str, str, tuple[bool, bool, bool]]] = [  # key, text, (Z, T, P)
    ("zc", "Z-stack in each channel", (True, False, False)),
    ("zct", "Time-lapse of Z-stacks", (True, True, False)),
    ("zcp", "Z-stacks at several positions", (True, False, True)),
    ("zctp", "Time-lapse of Z-stacks at several positions", (True, True, True)),
    ("ct", "Time-lapse, one plane", (False, True, False)),
]
UNITS = [("s", 1.0), ("min", 60.0), ("h", 3600.0)]


def default_experiment_root() -> Path:
    return Path.home() / "Documents" / "OpenEVOS" / "Experiments"


def _size(mb: float) -> str:
    return f"{mb / 1000:,.2f} GB" if mb >= 1000 else f"{mb:,.0f} MB" if mb >= 10 else f"{mb:,.1f} MB"


def _gate_locked(session) -> bool:
    """Positions with stage moves need the operator's confirmation on the instrument until XY is qualified (G-XY)."""
    return not PHYSICAL_POSITIONS_QUALIFIED and ((not session.simulated) or bool(session.physical_stage))


# ====================================================================================== small widgets
class DimCard(QFrame):
    """A dimension (kit mockup 08): its switch, a lettered badge in its colour, the title, a one-line summary on the
    right, then the body. Channels have no switch (always on)."""

    def __init__(self, letter: str, title: str, key: str, *, switchable: bool = True, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("subpanel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.switch: QCheckBox | None = None
        if switchable:
            self.switch = QCheckBox()
            self.switch.setAccessibleName(f"Use {title}")
            self.switch.setToolTip(f"Use {title} in this experiment")
            self.switch.toggled.connect(self._toggled)
            head.addWidget(self.switch)
        self.badge = QLabel(letter)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(22, 22)
        head.addWidget(self.badge)
        self.title = label(f"{title}", "section")
        head.addWidget(self.title)
        head.addStretch(1)
        self.summary = _Elided("", "caption")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.summary, 1)
        lay.addLayout(head)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 2, 0, 0)
        self.body_lay.setSpacing(6)
        lay.addWidget(self.body)
        self.retint()

    def retint(self) -> None:
        on = self.enabled()
        c = dim_color(self.key)
        self.badge.setStyleSheet(f"background: {c if on else theme.BORDER_STRONG}; color: "
                                 f"{theme.on(c) if on else theme.TEXT_2}; border-radius: 5px; font-weight: 700;")
        self.title.setEnabled(on)

    def _toggled(self, on: bool) -> None:
        self.body.setVisible(on)
        self.retint()

    def enabled(self) -> bool:
        return self.switch is None or self.switch.isChecked()

    def set_enabled(self, on: bool) -> None:
        if self.switch is not None:
            self.switch.setChecked(on)
            self.body.setVisible(on)
            self.retint()


class TimelineStrip(QWidget):
    """Timepoints as boxes: done, late, skipped, stopped, running, to go. The words are in the tooltip and in the
    caption beside it (never colour alone)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.n = 0
        self.status: dict[int, str] = {}
        self.setMinimumHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Timepoints")

    def sizeHint(self) -> QSize:
        return QSize(240, 22)

    def set_state(self, n: int, status: dict[int, str]) -> str:
        self.n, self.status = n, dict(status)
        counts: dict[str, int] = {}
        for s in self.status.values():
            counts[s] = counts.get(s, 0) + 1
        done = counts.get("done", 0) + counts.get("late", 0)
        parts = [f"{done} of {n} done"]
        for key, word in (("late", "late"), ("skipped", "skipped"), ("stopped", "stopped")):
            if counts.get(key):
                parts.append(f"{counts[key]} {word}")
        text = ", ".join(parts)
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self.update()
        return text

    def paintEvent(self, _e):
        if self.n <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        gap = 3.0 if self.n <= 60 else 0.0
        bw = max(1.0, (w - gap * (self.n - 1)) / self.n)
        colours = {"done": theme.OK, "late": theme.WARN, "skipped": theme.MUTED, "stopped": theme.FAULT,
                   "running": theme.ACCENT}
        for i in range(self.n):
            x = i * (bw + gap)
            r = QRectF(x + 0.5, 2.5, max(1.0, bw - 1), h - 5)
            s = self.status.get(i, "pending")
            if s == "pending":
                p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(colours.get(s, theme.MUTED)))
                if s == "skipped":
                    p.setBrush(QColor(theme.MUTED))
                    p.setBrush(Qt.BrushStyle.BDiagPattern)
                    p.setPen(QPen(QColor(theme.MUTED), 1))
            p.drawRoundedRect(r, 2, 2)
        p.end()


def _num(v) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return "–" if v is None else str(v)


def _xy(pos: dict) -> str:
    if pos.get("x") is None:
        return "no move (where the stage was)"
    return f"X {_num(pos.get('x'))}, Y {_num(pos.get('y'))} {pos.get('unit', '')} ({pos.get('frame', '')})"


def _tp_status(recs: list[dict]) -> dict[int, str]:
    out: dict[int, str] = {}
    for r in recs:
        st = r.get("status", "run")
        if st == "skipped":
            out[r["t_index"]] = "skipped"
        elif st == "stopped":
            out[r["t_index"]] = "stopped"
        elif "finished_at" in r:
            out[r["t_index"]] = "late" if st == "late" else "done"
        else:
            out[r["t_index"]] = "running"
    return out


def _tile(caption: str) -> tuple[QWidget, QLabel]:
    w = QFrame()
    w.setObjectName("subpanel")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(10, 6, 10, 6)
    lay.setSpacing(0)
    v = label("–", "value")
    lay.addWidget(v)
    lay.addWidget(label(caption, "caption"))
    return w, v


# ====================================================================================== page
class MultiDPage(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.session = model.session
        self.md = self.session.settings.multid
        self.anchor_um: float | None = None
        self.positions: list[PositionPlan] = []
        self._runner: MultiDimRunner | None = None
        self._run_plan: MultiDimPlan | None = None
        self._run_summary = None
        self._last_msg = ""
        self._latest: tuple | None = None
        self._img_pending = False
        self._reader: ExperimentReader | None = None
        self._run_started: float | None = None
        self._loading = True
        self.last_preflight = None
        self.last_summary = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("Multi-D", "title"))
        head.addSpacing(8)
        self.steps = SegmentedControl(STEPS, accessible_name="Multi-D step")
        self.steps.activated.connect(self._go_step)
        head.addWidget(self.steps)
        head.addSpacing(14)
        self.chip = {k: DimChip(k, n) for k, n in (("z", "Depth"), ("c", "Channels"), ("t", "Time"),
                                                    ("p", "Positions"))}
        for c in self.chip.values():
            head.addWidget(c)
        head.addStretch(1)
        self.lbl_mode = _Elided("", "caption")
        self.lbl_mode.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.lbl_mode, 1)
        root.addLayout(head)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_plan())
        self.stack.addWidget(self._build_monitor())
        self.stack.addWidget(self._build_review())
        root.addWidget(self.stack, 1)
        self.steps.set_current("plan")

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh)
        model.tasks.busy_changed.connect(self._on_busy)
        model.register_stopper(self.stop)
        model.settings_changed.connect(self._changed)
        model.vessel_changed.connect(self._changed)
        self._load_channels()
        self._restore()
        self._loading = False
        self.refresh()

    # ------------------------------------------------------------------ build: plan (kit mockup 08, Plan)
    def _column(self, *, width: int | None = None, scroll: bool = True) -> tuple[QWidget, QVBoxLayout, QWidget]:
        """A page column: (outer widget to add, its layout, inner widget). Scrolls vertically when `scroll`."""
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 8 if scroll else 0, 0)
        lay.setSpacing(10)
        if not scroll:
            outer = inner
        else:
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setWidget(inner)
            outer = sc
        if width:
            k = min(theme.TEXT_SCALE, 1.4)
            outer.setMinimumWidth(round(width * 0.86 * k))
            outer.setMaximumWidth(round(width * k))
        return outer, lay, inner

    def _build_plan(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        # ---- left: the experiment (name, preset, instrument, XY positions, data)
        left, ll, _li = self._column(width=380)
        exp, el = panel("Experiment", spacing=6)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Experiment name (optional)")
        self.name.setAccessibleName("Experiment name")
        self.name.textChanged.connect(self._changed)
        el.addWidget(label("Experiment name", "rowlabel"))
        el.addWidget(self.name)
        self.preset = QComboBox()
        self.preset.setAccessibleName("Experiment preset")
        for key, text, _dims in PRESETS:
            self.preset.addItem(text, key)
        self.preset.addItem("Custom", "custom")
        self.preset.activated.connect(self._apply_preset)
        self.preset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset.setMinimumContentsLength(14)
        el.addWidget(label("Start from", "rowlabel"))
        el.addWidget(self.preset)
        self.kv_inst = KeyValues([("vessel", "Vessel / sample"), ("objective", "Objective"), ("camera", "Camera")])
        el.addWidget(self.kv_inst)
        ll.addWidget(exp)

        pos, pl = panel("XY positions", spacing=6)
        self.pmap = PositionMap()
        self.pmap.clicked.connect(self._map_clicked)
        self.pmap.setToolTip("Click a well to add it as a position; click again to remove it")
        pl.addWidget(self.pmap)
        self.lbl_pmap = label("", "caption", wrap=True)
        pl.addWidget(self.lbl_pmap)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Label", "X (µm)", "Y (µm)", "Focus (µm)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAccessibleName("Positions")
        self.table.setMinimumHeight(130)
        self.table.itemChanged.connect(self._table_edited)
        pl.addWidget(self.table)
        b_add = QPushButton("Add here")
        b_add.setToolTip("Add the stage's position now, with the focus now as its own reference")
        b_add.clicked.connect(self.add_current_position)
        b_wells = QPushButton("All wells")
        b_wells.setToolTip("One position at the centre of every well or region of the current vessel")
        b_wells.clicked.connect(self.add_vessel_regions)
        b_clear = QPushButton("Clear")
        b_clear.setToolTip("Remove every position")
        b_clear.clicked.connect(self._clear_positions)
        pgrid = QGridLayout()
        pgrid.setSpacing(6)
        for i, b in enumerate((b_add, b_wells, b_clear)):
            pgrid.addWidget(b, 0, i)
        b_focus = QPushButton("Focus here")
        b_focus.setToolTip("Store the current focus as the selected position's own focus reference")
        b_focus.clicked.connect(self._set_row_focus)
        b_up, b_down, b_rm = QPushButton("↑"), QPushButton("↓"), QPushButton("Remove")
        b_up.setAccessibleName("Move the selected position up")
        b_down.setAccessibleName("Move the selected position down")
        b_up.clicked.connect(self._row_up)
        b_down.clicked.connect(self._row_down)
        b_rm.clicked.connect(self._row_remove)
        for b in (b_add, b_wells, b_clear, b_focus, b_rm):
            b.setMinimumWidth(1)
            b.setStyleSheet("padding-left: 6px; padding-right: 6px;")
        pgrid.addWidget(b_focus, 1, 0)
        pgrid.addWidget(hbox(b_up, b_down, spacing=4), 1, 1)
        pgrid.addWidget(b_rm, 1, 2)
        pl.addLayout(pgrid)
        ll.addWidget(pos)

        data, dl = panel("Data", spacing=6)
        self.proj_checks: dict[str, QCheckBox] = {}
        dl.addWidget(label("Also save these projections of each volume", "rowlabel", wrap=True))
        prow = QGridLayout()
        prow.setSpacing(6)
        for i, m in enumerate(PROJECTIONS):
            chk = QCheckBox(METHODS[m])
            chk.setChecked(m == "max")
            chk.toggled.connect(self._changed)
            self.proj_checks[m] = chk
            prow.addWidget(chk, i, 0)
        dl.addLayout(prow)
        dl.addWidget(label("Raw planes are always saved unchanged; projections are extra files.", "caption",
                           wrap=True))
        ll.addWidget(data)
        ll.addStretch(1)
        row.addWidget(left)
        self._sides = [left]

        # ---- centre: the dimension plan (Z, C, T, P), then the first events
        centre, cl, _ci = self._column()
        dims, dl2 = panel("Dimension plan", spacing=8)

        self.card_z = DimCard("Z", "Depth (3D)", "z")
        zt = QHBoxLayout()
        self.z_mode = SegmentedControl([("centre", "Centre"), ("first_last", "First / last")],
                                       accessible_name="Z range mode")
        self.z_mode.activated.connect(self._z_mode_changed)
        self.z_keep = SegmentedControl([("interval", "Keep interval"), ("slices", "Keep slices")],
                                       accessible_name="What a range change keeps")
        self.z_keep.activated.connect(self._z_keep_changed)
        zt.addWidget(self.z_mode)
        zt.addSpacing(8)
        zt.addWidget(self.z_keep)
        zt.addStretch(1)
        self.card_z.body_lay.addLayout(zt)
        zg = QGridLayout()
        zg.setHorizontalSpacing(10)
        zg.setVerticalSpacing(4)
        self.z_a = self._spin(-2000, 2000, 10.0, " µm", "Below the focus")
        self.z_b = self._spin(-2000, 2000, 10.0, " µm", "Above the focus")
        self.lbl_za, self.lbl_zb = label("Below", "caption"), label("Above", "caption")
        self.z_step = self._spin(0.1, 500, 1.0, " µm", "Z interval")
        self.z_slices = QSpinBox()
        self.z_slices.setRange(2, 200)
        self.z_slices.setValue(11)
        self.z_slices.setAccessibleName("Number of slices")
        self.z_slices.valueChanged.connect(self._changed)
        self.lbl_zstep = label("Interval", "caption")
        for col, (lab, w) in enumerate(((self.lbl_za, self.z_a), (self.lbl_zb, self.z_b),
                                        (self.lbl_zstep, hbox(self.z_step, self.z_slices, spacing=0)))):
            zg.addWidget(lab, 0, col)
            zg.addWidget(w, 1, col)

        self.card_z.body_lay.addLayout(zg)
        self.zruler = ZPlanRuler()
        self.card_z.body_lay.addWidget(self.zruler)
        self.lbl_anchor = label("", "caption", wrap=True)
        self.btn_anchor = QPushButton("Use focus now")
        self.btn_anchor.setToolTip("Make the current focus position the reference the planes are placed around")
        self.btn_anchor.clicked.connect(self._use_focus_now)
        self.card_z.body_lay.addWidget(hbox(self.lbl_anchor, self.btn_anchor, spacing=8))
        self.lbl_anchor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        dl2.addWidget(self.card_z)

        self.card_c = DimCard("C", "Channels", "c", switchable=False)
        self.chips = ChannelChips()
        self.chips.setAccessibleName("Multi-D channels")
        self.chips.changed.connect(self._changed)
        self.card_c.body_lay.addWidget(self.chips)
        self.lbl_exposure = label("", "caption", wrap=True)
        self.card_c.body_lay.addWidget(self.lbl_exposure)
        self.card_c.body_lay.addWidget(label("Channel order", "rowlabel"))
        self.order_group = QButtonGroup(self)
        self.rb_cz = QRadioButton("Channel by channel")
        self.rb_zc = QRadioButton("Plane by plane")
        self.rb_cz.setToolTip("The whole Z range in one channel, then the next")
        self.rb_zc.setToolTip("Every channel at each plane before moving on")
        orow = QHBoxLayout()
        orow.setSpacing(18)
        for i, rb in enumerate((self.rb_cz, self.rb_zc)):
            self.order_group.addButton(rb, i)
            orow.addWidget(rb)
        orow.addStretch(1)
        self.card_c.body_lay.addLayout(orow)
        self.rb_cz.setChecked(True)
        self.order_group.idToggled.connect(self._order_toggled)
        self.lbl_order = label("", "caption", wrap=True)
        self.card_c.body_lay.addWidget(self.lbl_order)
        dl2.addWidget(self.card_c)

        self.card_t = DimCard("T", "Time (time-lapse)", "t")
        tg = QGridLayout()
        tg.setHorizontalSpacing(10)
        tg.setVerticalSpacing(4)
        self.t_count = QSpinBox()
        self.t_count.setRange(2, 10000)
        self.t_count.setValue(10)
        self.t_count.setAccessibleName("Number of timepoints")
        self.t_count.valueChanged.connect(self._changed)
        self.t_every = self._spin(1, 100000, 5.0, "", "Interval between timepoints")
        self.t_unit = QComboBox()
        for text, _f in UNITS:
            self.t_unit.addItem(text)
        self.t_unit.setCurrentIndex(1)
        self.t_unit.setAccessibleName("Interval unit")
        self.t_unit.currentIndexChanged.connect(self._changed)
        self.t_sched = QComboBox()
        self.t_sched.addItem("At least the interval apart", Schedule.MIN_INTERVAL.value)
        self.t_sched.addItem("Fixed schedule from the start", Schedule.FIXED.value)
        self.t_sched.setItemData(0, "Start to start; a long cycle makes the next start late, never early",
                                 Qt.ItemDataRole.ToolTipRole)
        self.t_sched.setAccessibleName("Timing")
        self.t_sched.currentIndexChanged.connect(self._changed)
        self.t_missed = QComboBox()
        self.t_missed.addItem("Skip it", Missed.SKIP.value)
        self.t_missed.addItem("Run it once, late", Missed.RUN_LATE_ONCE.value)
        self.t_missed.setAccessibleName("When a timepoint is missed")
        self.t_missed.currentIndexChanged.connect(self._changed)
        self.lbl_missed = label("If a timepoint is missed", "caption")
        tg.addWidget(label("Timepoints", "caption"), 0, 0)
        tg.addWidget(self.t_count, 1, 0)
        tg.addWidget(label("Interval", "caption"), 0, 1)
        tg.addWidget(hbox(self.t_every, self.t_unit, spacing=4), 1, 1)
        tg.addWidget(label("Timing", "caption"), 0, 2)
        tg.addWidget(self.t_sched, 1, 2)
        tg.addWidget(self.lbl_missed, 2, 0, 1, 2)
        tg.addWidget(self.t_missed, 2, 2)
        self.card_t.body_lay.addLayout(tg)
        self.t_preview = TimelineStrip()
        self.t_preview.setAccessibleName("Timepoints planned")
        self.card_t.body_lay.addWidget(self.t_preview)
        self.lbl_tpreview = label("", "caption", wrap=True)
        self.card_t.body_lay.addWidget(self.lbl_tpreview)
        dl2.addWidget(self.card_t)

        self.card_p = DimCard("P", "Positions (XY stage)", "p")
        self.focus_policy = QComboBox()
        self.focus_policy.addItem("One focus reference for every position", FocusPolicy.GLOBAL.value)
        self.focus_policy.addItem("Each position's own focus", FocusPolicy.PER_POSITION.value)
        self.focus_policy.setAccessibleName("Focus policy")
        self.focus_policy.currentIndexChanged.connect(self._changed)
        self.card_p.body_lay.addWidget(hbox(label("Focus", "rowlabel"), self.focus_policy, stretch_last=True))
        self.lbl_porder = label("", "caption", wrap=True)
        self.card_p.body_lay.addWidget(self.lbl_porder)
        self.lbl_gate = label("", "warn", wrap=True)
        self.card_p.body_lay.addWidget(self.lbl_gate)
        dl2.addWidget(self.card_p)
        cl.addWidget(dims)

        ev, evl = panel("First events, in the order they run", spacing=6)
        self.events = QListWidget()
        self.events.setAccessibleName("First events")
        self.events.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.events.setMinimumHeight(150)
        self.events.setObjectName("mono")
        evl.addWidget(self.events)
        cl.addWidget(ev)
        cl.addStretch(1)
        for card in (self.card_z, self.card_t, self.card_p):
            assert card.switch is not None
            card.switch.toggled.connect(self._dims_toggled)
        row.addWidget(centre, 1)

        # ---- right: the plan summary, pre-flight, where the data goes, Acquire
        right, rl, _ri = self._column(width=360)
        summ, sl = panel("Plan summary", spacing=6)
        sl.addWidget(label("Planned acquisition", "section"))
        self.kv_sum = KeyValues([("z", "Z (slices)"), ("c", "C (channels)"), ("t", "T (timepoints)"),
                                 ("p", "P (positions)")], mono_values=True)
        for key, text in (("images", "Total images"), ("time", "Estimated duration"), ("cycle", "Per timepoint"),
                          ("size", "Estimated storage"), ("ztravel", "Z travel range"), ("light", "Light on")):
            self.kv_sum.add_row(key, text, mono_values=True, separator=key == "images")
        sl.addWidget(self.kv_sum)
        self.lbl_dims = label("", "caption", wrap=True)
        sl.addWidget(self.lbl_dims)
        rl.addWidget(summ)

        pf, pfl = panel("Pre-flight validation", spacing=6)
        self.lbl_errors = label("", "fault", wrap=True)
        self.lbl_confirm = label("", "warn", wrap=True)
        self.lbl_notes = label("", "muted", wrap=True)
        self.lbl_checks = label("", "", wrap=True)
        self.lbl_checks.setTextFormat(Qt.TextFormat.RichText)
        # one-click fixes for failing checks, each saying what it changes; rebuilt on every pre-flight
        self.fix_box = QWidget()
        self.fix_lay = QVBoxLayout(self.fix_box)
        self.fix_lay.setContentsMargins(0, 2, 0, 2)
        self.fix_lay.setSpacing(4)
        self.fix_box.setVisible(False)
        self.fix_buttons: list[QPushButton] = []
        for w in (self.lbl_checks, self.fix_box, self.lbl_errors, self.lbl_confirm, self.lbl_notes):
            pfl.addWidget(w)
        rl.addWidget(pf)

        dest, dsl = panel("Destination folder", spacing=6)
        self.lbl_folder = _Elided("", "mono")
        self.lbl_folder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        b_folder = QPushButton("Choose…")
        b_folder.setAccessibleName("Choose the destination folder")
        b_folder.clicked.connect(self._choose_folder)
        dsl.addWidget(hbox(self.lbl_folder, b_folder, spacing=6))
        self.lbl_folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        rl.addWidget(dest)
        rl.addStretch(1)
        side = QWidget()
        sdl = QVBoxLayout(side)
        sdl.setContentsMargins(0, 0, 0, 0)
        sdl.setSpacing(8)
        sdl.addWidget(right, 1)
        side.setMinimumWidth(right.minimumWidth())
        side.setMaximumWidth(right.maximumWidth())
        rl = sdl  # the buttons below stay in view

        b_save = QPushButton("Save plan…")
        b_save.clicked.connect(self._save_plan)
        b_load = QPushButton("Load plan…")
        b_load.clicked.connect(self._load_plan)
        b_open = QPushButton("Open experiment…")
        b_open.clicked.connect(self._open_dialog)
        g = QGridLayout()
        g.setSpacing(6)
        g.addWidget(b_save, 0, 0)
        g.addWidget(b_load, 0, 1)
        g.addWidget(b_open, 1, 0, 1, 2)
        rl.addLayout(g)
        self.btn_acquire = QPushButton("Acquire experiment")
        self.btn_acquire.setObjectName("primary")
        self.btn_acquire.setMinimumHeight(42)
        self.btn_acquire.clicked.connect(lambda _c=False: self.acquire())
        rl.addWidget(self.btn_acquire)
        row.addWidget(side)
        self._sides.append(side)
        self._plan_scroll = page
        return page

    def _spin(self, lo: float, hi: float, val: float, suffix: str, name: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        s.setValue(val)
        s.setSuffix(suffix)
        s.setAccessibleName(name)
        s.setKeyboardTracking(False)
        s.valueChanged.connect(self._changed)
        return s

    # ------------------------------------------------------------------ build: monitor (kit mockup 08, Acquire)
    def _build_monitor(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        we, self.m_exp = _tile("experiment")
        wp, self.m_p = _tile("position")
        wt, self.m_t = _tile("timepoint")
        wc, self.m_c = _tile("channel")
        wz, self.m_z = _tile("Z slice")
        tiles.addWidget(we, 2)
        for w in (wp, wt, wc, wz):
            tiles.addWidget(w, 1)
        lay.addLayout(tiles)

        body = QHBoxLayout()
        body.setSpacing(12)
        # left: the newest image over the per-axis progress and the queue
        lc = _NoHeightForWidth()
        lcl = QVBoxLayout(lc)
        lcl.setContentsMargins(0, 0, 0, 0)
        lcl.setSpacing(10)
        self.m_view = ImageView()
        self.m_view.setAccessibleName("Latest image")
        self.m_view.setMinimumSize(320, 220)
        lcl.addWidget(self.m_view, 1)
        low = QHBoxLayout()
        low.setSpacing(10)
        dp, dpl = panel("Dimension progress", spacing=4)
        self.ax = {k: AxisBar(k, n) for k, n in (("z", "Z slices"), ("c", "Channels"), ("t", "Timepoints"),
                                                 ("p", "Positions"))}
        for b in self.ax.values():
            dpl.addWidget(b)
        dpl.addStretch(1)
        low.addWidget(dp, 2)
        qp, qpl = panel("Acquisition queue", spacing=4)
        self.m_queue = QTableWidget(0, 5)
        self.m_queue.setHorizontalHeaderLabels(["Position", "T", "Channels", "Progress", "Status"])
        self.m_queue.verticalHeader().setVisible(False)
        self.m_queue.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.m_queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.m_queue.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.m_queue.setAccessibleName("Acquisition queue")
        self.m_queue.setMinimumHeight(120)
        qpl.addWidget(self.m_queue)
        low.addWidget(qp, 3)
        lcl.addLayout(low)
        body.addWidget(lc, 1)

        # right: status, controls, timepoints, where, log, writer
        right, rl, _ri = self._column(width=380)
        st, stl = panel("Acquisition status", spacing=6)
        self.kv_run = KeyValues([("position", "Position"), ("channel", "Channel"), ("z", "Z slice"),
                                 ("t", "Timepoint"), ("images", "Images"), ("left", "Remaining time"),
                                 ("written", "Written"), ("rate", "Throughput")], mono_values=True)
        stl.addWidget(self.kv_run)
        self.m_progress = QProgressBar()
        self.m_progress.setAccessibleName("Images taken")
        self.m_progress.setFixedHeight(18)
        self.m_progress.setFormat("%p %")
        stl.addWidget(self.m_progress)
        self.m_status = label("Nothing running. Plan an experiment and press Acquire experiment.", "", wrap=True)
        self.m_status.setMinimumWidth(1)
        self.m_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        stl.addWidget(self.m_status)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setToolTip("Pause at the next safe point; the light goes off while paused")
        self.btn_pause.clicked.connect(self._pause_resume)
        self.btn_stop = QPushButton("Stop and keep data")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setToolTip("Stop after the current move or exposure. Everything taken so far is kept; there is "
                                 "no return move.")
        self.btn_stop.clicked.connect(self.stop)
        for btn in (self.btn_pause, self.btn_stop):
            btn.setMinimumHeight(36)
        stl.addWidget(hbox(self.btn_pause, self.btn_stop, spacing=8))
        stl.addWidget(label("Stop all, in the top bar, stops every instrument workflow.", "caption", wrap=True))
        self.btn_review = QPushButton("Review the data")
        self.btn_review.setObjectName("primary")
        self.btn_review.clicked.connect(lambda _c=False: self._go_step("review"))
        stl.addWidget(self.btn_review)
        rl.addWidget(st)
        tp, tpl = panel("Timepoint timeline", spacing=6)
        self.m_timeline = TimelineStrip()
        tpl.addWidget(self.m_timeline)
        self.m_tl_text = label("", "caption", wrap=True)
        tpl.addWidget(self.m_tl_text)
        rl.addWidget(tp)
        mp, mpl = panel("Position map", spacing=6)
        self.m_map = PositionMap()
        self.m_map.interactive = False
        self.m_map.setMinimumHeight(120)
        mpl.addWidget(self.m_map)
        rl.addWidget(mp)
        lg, lgl = panel("Event log", spacing=6)
        self.m_log = QListWidget()
        self.m_log.setAccessibleName("Event log")
        self.m_log.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.m_log.setMinimumHeight(140)
        lgl.addWidget(self.m_log)
        lgl.addWidget(label("Writer", "section"))
        self.m_writer = label("", "caption", wrap=True)
        self.m_writer.setMinimumWidth(1)
        self.m_writer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lgl.addWidget(self.m_writer)
        rl.addWidget(lg, 1)
        body.addWidget(right)
        lay.addLayout(body, 1)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_review.setVisible(False)
        return page

    # ------------------------------------------------------------------ build: review (kit mockup 08, Review)
    def _build_review(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # left: the dataset (positions, timepoints)
        left, ll, _li = self._column(width=300, scroll=False)
        ds, dsl = panel("Dataset", spacing=6)
        b_open = QPushButton("Open experiment…")
        b_open.clicked.connect(self._open_dialog)
        dsl.addWidget(b_open)
        self.r_folder = _Elided("No experiment open", "caption")
        self.r_folder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        dsl.addWidget(self.r_folder)
        self.r_status = label("", "", wrap=True)
        dsl.addWidget(self.r_status)
        self.r_tree = QTreeWidget()
        self.r_tree.setHeaderLabels(["Volume", "Images", "Status"])
        self.r_tree.setAccessibleName("Experiment volumes")
        self.r_tree.setRootIsDecorated(True)
        self.r_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.r_tree.itemClicked.connect(self._tree_clicked)
        dsl.addWidget(self.r_tree, 1)
        ll.addWidget(ds, 1)
        lay.addWidget(left)

        # centre: the viewer
        centre = _NoHeightForWidth()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        vw, vl = panel(None, spacing=8)
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.r_t = QSpinBox()
        self.r_t.setPrefix("T ")
        self.r_t.setAccessibleName("Timepoint")
        self.r_p = QComboBox()
        self.r_p.setAccessibleName("Position")
        self.r_c = QComboBox()
        self.r_c.setAccessibleName("Channel")
        self.r_mode = QComboBox()
        self.r_mode.setAccessibleName("Show")
        self.r_mode.addItem("Slice", "slice")
        for m in ("max", "mean", "min", "std"):
            self.r_mode.addItem(f"{METHODS[m]} projection", m)
        for combo, n in ((self.r_p, 8), (self.r_c, 6), (self.r_mode, 16)):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(n)
        for w in (self.r_p, self.r_t, self.r_c):
            nav.addWidget(w)
        nav.addStretch(1)
        vl.addLayout(nav)
        nav = QHBoxLayout()  # second row: what is shown, and the projection's Z range
        nav.setSpacing(8)
        nav.addWidget(self.r_mode)
        self.r_from = QSpinBox()
        self.r_from.setPrefix("Z from ")
        self.r_from.setAccessibleName("Projection from slice")
        self.r_to = QSpinBox()
        self.r_to.setPrefix("to ")
        self.r_to.setAccessibleName("Projection to slice")
        nav.addWidget(self.r_from)
        nav.addWidget(self.r_to)
        nav.addStretch(1)
        vl.addLayout(nav)
        self.r_view = ImageView()
        self.r_view.setAccessibleName("Experiment image")
        self.r_view.setMinimumSize(320, 240)
        vl.addWidget(self.r_view, 1)
        zrow = QHBoxLayout()
        self.r_z = QSlider(Qt.Orientation.Horizontal)
        self.r_z.setAccessibleName("Slice")
        self.r_zlab = label("", "zslice")
        self.r_zlab.setMinimumWidth(190)
        zrow.addWidget(label("Z", "rowlabel"))
        zrow.addWidget(self.r_z, 1)
        zrow.addWidget(self.r_zlab)
        vl.addLayout(zrow)
        cl.addWidget(vw, 1)
        lay.addWidget(centre, 1)

        # right: metadata, not acquired, files
        right, rl, _ri = self._column(width=360, scroll=False)
        md, mdl = panel("Metadata", spacing=6)
        self.r_meta = QTableWidget(0, 2)
        self.r_meta.setHorizontalHeaderLabels(["", ""])
        self.r_meta.horizontalHeader().setVisible(False)
        self.r_meta.verticalHeader().setVisible(False)
        self.r_meta.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.r_meta.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.r_meta.setShowGrid(False)
        self.r_meta.setWordWrap(True)
        self.r_meta.setAccessibleName("Metadata")
        mdl.addWidget(self.r_meta, 1)
        rl.addWidget(md, 3)
        na, nal = panel("Not acquired", spacing=6)
        self.r_missing = QListWidget()
        self.r_missing.setAccessibleName("Volumes not acquired")
        self.r_missing.itemActivated.connect(self._missing_activated)
        nal.addWidget(self.r_missing)
        rl.addWidget(na, 1)
        self.btn_rfolder = QPushButton("Open raw data")
        self.btn_rfolder.setToolTip("Open the experiment folder")
        self.btn_rfolder.clicked.connect(lambda _c=False: self._reveal(""))
        self.btn_rmanifest = QPushButton("Experiment manifest")
        self.btn_rmanifest.setToolTip("Open manifest.json: every volume, its status and the files written")
        self.btn_rmanifest.clicked.connect(lambda _c=False: self._reveal("manifest.json"))
        for b in (self.btn_rfolder, self.btn_rmanifest):
            b.setEnabled(False)
        for b in (self.btn_rfolder, self.btn_rmanifest):
            rl.addWidget(b)
        lay.addWidget(right)

        for sig in (self.r_t.valueChanged, self.r_p.currentIndexChanged, self.r_c.currentIndexChanged,
                    self.r_mode.currentIndexChanged, self.r_from.valueChanged, self.r_to.valueChanged,
                    self.r_z.valueChanged):
            sig.connect(self._review_show)
        return page

    # ------------------------------------------------------------------ steps
    def resizeEvent(self, e):
        """On a narrow window the Plan's side columns give way to the dimension plan (kit doc 06 §6)."""
        super().resizeEvent(e)
        narrow = self.width() < 1400
        for w in getattr(self, "_sides", []):
            full = w.property("full_w")
            if full is None:
                w.setProperty("full_w", w.maximumWidth())
                full = w.maximumWidth()
            w.setMaximumWidth(w.minimumWidth() if narrow else int(full))

    def _go_step(self, key: str) -> None:
        self.steps.set_current(key)
        self.stack.setCurrentIndex([k for k, _t in STEPS].index(key))

    # ------------------------------------------------------------------ channels, restore, presets
    def _load_channels(self) -> None:
        role = self.session.settings.active_camera
        want = [(c.display, c.slot, c.pseudocolor) for c in self.session.profile.channels_for(role) if c.installed]
        keep = set(self.chips.checked())
        if want != self.chips.items():
            self.chips.blockSignals(True)
            self.chips.set_channels(want, keep)
            if not self.chips.checked() and want:
                self.chips.set_checked(want[0][1])
            self.chips.blockSignals(False)

    def _restore(self) -> None:
        """The last plan's settings, without its positions and focus reference (those belong to one sample)."""
        d = self.md.last_plan
        if d:
            try:
                plan = MultiDimPlan.from_dict(d)
            except (ValueError, KeyError, TypeError):
                plan = None
            if plan is not None:
                self.apply_plan(plan, with_positions=False)
        self._apply_preset_dims(self.card_z.enabled(), self.card_t.enabled(), self.card_p.enabled())
        self._z_mode_changed(self.z_mode.current() or "centre")
        self._z_keep_changed(self.z_keep.current() or "interval")

    def _apply_preset(self, _i: int = 0) -> None:
        key = self.preset.currentData()
        for k, _text, dims in PRESETS:
            if k == key:
                self._apply_preset_dims(*dims)
        self._changed()

    def _apply_preset_dims(self, z: bool, t: bool, p: bool) -> None:
        for card, on in ((self.card_z, z), (self.card_t, t), (self.card_p, p)):
            assert card.switch is not None
            card.switch.blockSignals(True)
            card.set_enabled(on)
            card.switch.blockSignals(False)
        self._sync_preset()

    def _sync_preset(self) -> None:
        dims = (self.card_z.enabled(), self.card_t.enabled(), self.card_p.enabled())
        key = next((k for k, _t, d in PRESETS if d == dims), "custom")
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(max(0, self.preset.findData(key)))
        self.preset.blockSignals(False)

    def _dims_toggled(self, _on: bool) -> None:
        self._sync_preset()
        self._changed()

    def _z_mode_changed(self, mode: str) -> None:
        self.z_mode.set_current(mode)
        centre = mode == "centre"
        self.lbl_za.setText("Below" if centre else "First")
        self.lbl_zb.setText("Above" if centre else "Last")
        self.z_a.setToolTip("How far below the focus reference the stack starts" if centre else
                            "First plane, relative to the focus reference (negative is below)")
        self.z_b.setToolTip("How far above the focus reference the stack ends" if centre else
                            "Last plane, relative to the focus reference")
        self.z_a.setMinimum(0 if centre else -2000)
        self.z_b.setMinimum(0 if centre else -2000)
        self._changed()

    def _z_keep_changed(self, keep: str) -> None:
        self.z_keep.set_current(keep)
        self.z_step.setVisible(keep == "interval")
        self.z_slices.setVisible(keep == "slices")
        if keep == "slices":
            self.z_slices.blockSignals(True)
            self.z_slices.setValue(max(2, self._range_planes()))
            self.z_slices.blockSignals(False)
        self._changed()

    def _range_um(self) -> float:
        if (self.z_mode.current() or "centre") == "centre":
            return self.z_a.value() + self.z_b.value()
        return abs(self.z_b.value() - self.z_a.value())

    def _range_planes(self) -> int:
        step = self.z_step.value()
        return int(self._range_um() / step + 1e-9) + 1 if step > 0 else 1

    def _order_toggled(self, _id: int, on: bool) -> None:
        if on:
            self._changed()

    # ------------------------------------------------------------------ positions
    def _table_edited(self, item: QTableWidgetItem) -> None:
        r, c = item.row(), item.column()
        if self._loading or r >= len(self.positions):
            return
        pos = self.positions[r]
        text = item.text().strip()
        try:
            if c == 0:
                pos = replace(pos, label=text or pos.label)
            elif c in (1, 2):
                v = float(text)
                pos = replace(pos, x=v) if c == 1 else replace(pos, y=v)
            else:
                pos = replace(pos, z_anchor_um=float(text) if text else None)
        except ValueError:
            self._fill_table()
            return
        self.positions[r] = pos
        self._changed()

    def _fill_table(self) -> None:
        was = self._loading
        self._loading = True
        self.table.setRowCount(len(self.positions))
        for r, p in enumerate(self.positions):
            vals = [p.label, "" if p.x is None else f"{p.x:.1f}", "" if p.y is None else f"{p.y:.1f}",
                    "" if p.z_anchor_um is None else f"{p.z_anchor_um:.1f}"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
        self._loading = was

    def _renumber(self) -> None:
        self.positions = [replace(p, position_id=f"P{i + 1:04d}") for i, p in enumerate(self.positions)]

    def add_current_position(self, _c: bool = False) -> None:
        ctl = self.session.controller
        xy = ctl.stage_xy()
        if xy is None:
            self.model.add_log("warn", "Multi-D: no stage position read back; cannot add the current position")
            return
        unit, frame = ctl.stage_frame()
        n = len(self.positions) + 1
        self.positions.append(PositionPlan(f"P{n:04d}", f"Position {n}", float(xy[0]), float(xy[1]), unit, frame,
                                           z_anchor_um=ctl.focus_z(), source="current"))
        self._fill_table()
        self._changed()

    def add_vessel_regions(self, _c: bool = False) -> None:
        ctl = self.session.controller
        unit, frame = ctl.stage_frame()
        sm = self.session.stage_map
        added = 0
        for reg in self.session.vessel.regions:
            x, y = sm.to_stage(reg.cx, reg.cy)
            n = len(self.positions) + 1
            if n > 384:
                break
            self.positions.append(PositionPlan(f"P{n:04d}", reg.region_id, float(x), float(y), unit, frame,
                                               source="vessel"))
            added += 1
        self.model.add_log("info", f"Multi-D: added {added} positions from {self.session.vessel.name}")
        self._fill_table()
        self._changed()

    def _map_clicked(self, region_id: str) -> None:
        """A well on the map: add a position at its centre, or remove the one already there."""
        i = next((k for k, q in enumerate(self.positions) if q.label == region_id), -1)
        if i >= 0:
            del self.positions[i]
        else:
            reg = self.session.vessel.region(region_id)
            if reg is None:
                return
            unit, frame = self.session.controller.stage_frame()
            x, y = self.session.stage_map.to_stage(reg.cx, reg.cy)
            self.positions.append(PositionPlan(f"P{len(self.positions) + 1:04d}", region_id, float(x), float(y),
                                               unit, frame, source="vessel"))
            if not self.card_p.enabled():
                self.card_p.set_enabled(True)
                self._sync_preset()
        self._renumber()
        self._fill_table()
        self._changed()

    def _clear_positions(self, _c: bool = False) -> None:
        self.positions = []
        self._fill_table()
        self._changed()

    def _selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _set_row_focus(self, _c: bool = False) -> None:
        r = self._selected_row()
        z = self.session.controller.focus_z()
        if r < 0 or z is None:
            return
        self.positions[r] = replace(self.positions[r], z_anchor_um=float(z))
        self._fill_table()
        self._changed()

    def _move_row(self, d: int) -> None:
        r = self._selected_row()
        j = r + d
        if r < 0 or not 0 <= j < len(self.positions):
            return
        self.positions[r], self.positions[j] = self.positions[j], self.positions[r]
        self._renumber()
        self._fill_table()
        self.table.selectRow(j)
        self._changed()

    def _row_up(self, _c: bool = False) -> None:
        self._move_row(-1)

    def _row_down(self, _c: bool = False) -> None:
        self._move_row(1)

    def _row_remove(self, _c: bool = False) -> None:
        r = self._selected_row()
        if r < 0:
            return
        del self.positions[r]
        self._renumber()
        self._fill_table()
        self._changed()

    def _use_focus_now(self, _c: bool = False) -> None:
        z = self.session.controller.focus_z()
        if z is not None:
            self.anchor_um = float(z)
        self._changed()

    def _choose_folder(self, _c: bool = False) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save experiments in", str(self.save_root()))
        if d:
            self.md.save_folder = d
            self._changed()

    def save_root(self) -> Path:
        return Path(self.md.save_folder) if self.md.save_folder else default_experiment_root()

    # ------------------------------------------------------------------ plan <-> widgets
    def plan(self) -> MultiDimPlan:
        role = self.session.settings.active_camera
        prof = self.session.profile
        chans = []
        for slot in self.chips.checked():
            cs = self.session.settings.channel(role, slot)
            ch = prof.channel(slot)
            chans.append(ChannelPlan(slot, ch.display if ch else slot, cs.exposure_ms, cs.gain, cs.led_percent,
                                     getattr(ch, "emission_nm", None),
                                     bool(ch is not None and ch.kind.value == "fluorescence")))
        keep = self.z_keep.current() or "interval"
        mode = self.z_mode.current() or "centre"
        a, b = self.z_a.value(), self.z_b.value()
        step = self.z_step.value()
        target = None
        if keep == "slices":
            target = self.z_slices.value()
            step = max(0.1, round(self._range_um() / max(1, target - 1), 3))
        z = ZAxisPlan(enabled=self.card_z.enabled(), mode=mode, below_um=a if mode == "centre" else 10.0,
                      above_um=b if mode == "centre" else 10.0, first_um=a if mode != "centre" else -10.0,
                      last_um=b if mode != "centre" else 10.0, interval_um=step, keep=keep, target_slices=target,
                      approach_um=self.session.settings.zstack.approach_um,
                      settle_ms=self.session.settings.zstack.settle_ms,
                      light_between_slices=self.session.settings.zstack.light_between_slices)
        unit = UNITS[self.t_unit.currentIndex()][1]
        t = TimeAxisPlan(enabled=self.card_t.enabled(), timepoints=self.t_count.value(),
                         interval_s=self.t_every.value() * unit, schedule=Schedule(self.t_sched.currentData()),
                         missed=Missed(self.t_missed.currentData()))
        if self.anchor_um is None:
            fz = self.session.controller.focus_z()
            self.anchor_um = float(fz) if fz is not None else None
        kw: dict = {}
        if self.card_p.enabled():
            kw["positions"] = tuple(self.positions)
        return MultiDimPlan(
            channels=tuple(chans), camera=role, z=z, time=t,
            traversal=Traversal.T_P_C_Z if self.rb_cz.isChecked() else Traversal.T_P_Z_C,
            focus_policy=FocusPolicy(self.focus_policy.currentData()) if self.card_p.enabled() else FocusPolicy.GLOBAL,
            global_anchor_z_um=self.anchor_um,
            projections=tuple(m for m, c in self.proj_checks.items() if c.isChecked()),
            name=self.name.text().strip(), instrument=prof.name, simulated=bool(self.session.simulated), **kw)

    def apply_plan(self, plan: MultiDimPlan, *, with_positions: bool = True) -> None:
        was, self._loading = self._loading, True
        try:
            slots = self.chips.slots()
            for s in slots:
                self.chips.buttons[s].blockSignals(True)
                self.chips.set_checked(s, s in {c.slot for c in plan.channels})
                self.chips.buttons[s].blockSignals(False)
            z = plan.z
            self.z_mode.set_current(z.mode)
            self._z_mode_changed(z.mode)
            self.z_a.setValue(z.below_um if z.mode == "centre" else z.first_um)
            self.z_b.setValue(z.above_um if z.mode == "centre" else z.last_um)
            self.z_step.setValue(z.interval_um)
            if z.target_slices:
                self.z_slices.setValue(z.target_slices)
            self._z_keep_changed(z.keep)
            t = plan.time
            self.t_count.setValue(max(2, t.timepoints))
            unit_i = 2 if t.interval_s >= 3600 and t.interval_s % 3600 == 0 else \
                1 if t.interval_s >= 60 and t.interval_s % 60 == 0 else 0
            self.t_unit.setCurrentIndex(unit_i)
            self.t_every.setValue(t.interval_s / UNITS[unit_i][1])
            self.t_sched.setCurrentIndex(max(0, self.t_sched.findData(t.schedule.value)))
            self.t_missed.setCurrentIndex(max(0, self.t_missed.findData(t.missed.value)))
            (self.rb_cz if plan.traversal is Traversal.T_P_C_Z else self.rb_zc).setChecked(True)
            self.focus_policy.setCurrentIndex(max(0, self.focus_policy.findData(plan.focus_policy.value)))
            for m, c in self.proj_checks.items():
                c.setChecked(m in plan.projections)
            self.name.setText(plan.name)
            use_p = self.card_p.enabled()
            if with_positions:
                self.positions = [p for p in plan.positions if p.moves]
                self.anchor_um = plan.global_anchor_z_um
                self._fill_table()
                use_p = bool(self.positions)
            self._apply_preset_dims(z.enabled, t.enabled, use_p)
        finally:
            self._loading = was
        self._changed()

    # ------------------------------------------------------------------ the answer side
    def _changed(self, *_a) -> None:
        if not self._loading:
            self._refresh_timer.start()

    def refresh(self) -> None:
        """Plan → summary → pre-flight → first events. Reads the instrument's state; never moves anything."""
        self._load_channels()
        sim = bool(self.session.simulated)
        self.lbl_mode.setText("Simulator: images are synthetic" if sim else "Instrument")
        self.lbl_folder.setText(str(self.save_root()))
        self.lbl_folder.setToolTip(str(self.save_root()))
        self.lbl_missed.setVisible(self.t_sched.currentData() == Schedule.FIXED.value)
        self.t_missed.setVisible(self.t_sched.currentData() == Schedule.FIXED.value)
        locked = _gate_locked(self.session)
        self.lbl_gate.setVisible(locked and self.card_p.enabled())
        self.lbl_gate.setText("The XY stage is not yet qualified on this instrument: Acquire asks you to confirm "
                              "the moves between positions before the run starts.")
        try:
            plan = self.plan()
        except (CaptureRejected, ValueError) as exc:
            self._show_problem(str(exc))
            return
        role = self.session.settings.active_camera
        exp = []
        for c in plan.channels:
            exp.append(f"{c.label} {c.exposure_ms:g} ms")
        self.lbl_exposure.setText(("Exposure from the Capture page: " + ", ".join(exp)) if exp else
                                  f"Pick at least one channel on the {role.value} camera.")
        a = self.anchor_um
        self.lbl_anchor.setText(f"Planes are placed around the focus reference, {a:,.1f} µm" if a is not None else
                                "No focus reference: focus read-back unavailable")
        summ = summarize(plan, self.session.profile)
        pre = preflight(plan, self.session, summ, save_root=self.save_root())
        self.last_summary, self.last_preflight = summ, pre
        kv = self.kv_sum
        kv.set("z", f"{summ.n_z:,}")
        kv.set("c", f"{summ.n_c:,}")
        kv.set("t", f"{summ.n_t:,}")
        kv.set("p", f"{summ.n_p:,}")
        kv.set("images", f"{summ.images:,}")
        kv.set("time", fmt_seconds(summ.total_s), "Estimate: moves, channel changes, exposures and settling")
        kv.set("cycle", fmt_seconds(summ.cycle_s) if plan.time.enabled else "–")
        kv.set("size", _size(summ.megabytes), f"{summ.bytes_per_image / 1e6:,.2f} MB per image, frame from the "
               f"{summ.frame_source}")
        zoffs = plan.z_offsets() if plan.z.enabled and summ.n_z else []
        kv.set("ztravel", f"{zoffs[-1] - zoffs[0]:,.1f} µm" if len(zoffs) > 1 else "–")
        light = sum(summ.light_s.values())
        kv.set("light", fmt_seconds(light) if light else "–",
               "\n".join(f"{k}: {fmt_seconds(v)}" for k, v in summ.light_s.items()))
        self.chip["z"].set_state(plan.z.enabled, summ.n_z)
        self.chip["c"].set_state(True, summ.n_c)
        self.chip["t"].set_state(plan.time.enabled, summ.n_t)
        self.chip["p"].set_state(self.card_p.enabled(), summ.n_p)
        anchor = self.anchor_um
        self.zruler.set_planes([(anchor or 0.0) + o for o in zoffs] if zoffs else [], anchor)
        if plan.time.enabled:
            self.t_preview.set_state(plan.n_t, {})
            self.lbl_tpreview.setText(f"{plan.n_t} timepoints over {fmt_seconds(plan.time.interval_s * (plan.n_t - 1))}"
                                      f"; one cycle takes about {fmt_seconds(summ.cycle_s)}")
        self.lbl_porder.setText(f"{len(self.positions)} listed, visited top to bottom in the table at every "
                                "timepoint" if self.positions else "No positions yet: click wells on the map, or "
                                "Add here / All wells on the left")
        self.lbl_dims.setText(
            f"{summ.n_t} timepoint{'s' if summ.n_t != 1 else ''} × {summ.n_p} position{'s' if summ.n_p != 1 else ''}"
            f" × {summ.n_c} channel{'s' if summ.n_c != 1 else ''} × {summ.n_z} plane{'s' if summ.n_z != 1 else ''}. "
            f"Frame {summ.frame_shape[1]}×{summ.frame_shape[0]} from the {summ.frame_source}; times include moves, "
            f"channel changes and settling.")
        self.card_c.summary.setText(f"{summ.n_c} selected")
        self._show_context()
        zr = plan.z_offsets() if plan.z.enabled and summ.n_z else []
        self.card_z.summary.setText(f"{summ.n_z} planes, {zr[0]:+.1f} to {zr[-1]:+.1f} µm" if zr else "one plane")
        self.card_t.summary.setText(f"{plan.n_t} every {fmt_seconds(plan.time.interval_s)}" if plan.time.enabled
                                    else "once")
        self.card_p.summary.setText(f"{plan.n_p} listed" if self.card_p.enabled() else "where the stage is")
        o = Traversal.T_P_C_Z if self.rb_cz.isChecked() else Traversal.T_P_Z_C
        self.lbl_order.setText("The whole Z range in one channel, then the next: fewest filter changes; the channels "
                               "of one plane are seconds to minutes apart." if o is Traversal.T_P_C_Z else
                               "Every channel at each plane before moving on: the channels of a plane are taken "
                               "together (best for moving samples); a filter change at every plane makes it slower.")
        self._show_preflight(pre)
        self.events.clear()
        labels = {p.position_id: p.label for p in plan.positions}
        zoff = plan.z_offsets() if summ.n_z else []
        for e in summ.preview:
            cname = plan.channels[e.c_index].label if e.c_index < len(plan.channels) else "?"
            zs = f", Z {e.z_index + 1} ({zoff[e.z_index]:+.1f} µm)" if plan.z.enabled and e.z_index < len(zoff) else ""
            where = labels.get(e.position_id, e.position_id)
            self.events.addItem(f"{e.event_id}   T{e.t_index + 1}, {where}, {cname}{zs}")
        if summ.images > len(summ.preview):
            self.events.addItem(f"… then {summ.images - len(summ.preview):,} more")
        self._update_acquire()

    def _show_context(self) -> None:
        """The instrument the plan runs on, and the map's wells."""
        ses = self.session
        ctl = ses.controller
        slot = ctl.objective_slot()
        obj = ses.profile.objective(slot) if slot else None
        role = ses.settings.active_camera
        cam = ses.profile.camera(role) if hasattr(ses.profile, "camera") else None
        self.kv_inst.set("vessel", ses.vessel.display_name)
        na = getattr(obj, "numerical_aperture", None)
        self.kv_inst.set("objective", (f"{obj.short} (NA {na:g})" if na else obj.short) if obj else "unknown")
        model = getattr(cam, "model", "") if cam is not None else ""
        self.kv_inst.set("camera", role.value.capitalize() + (f" ({model})" if model else ""))
        self.pmap.set_vessel(ses.vessel)
        self.m_map.set_vessel(ses.vessel)
        on_map = {q.label for q in self.positions if ses.vessel.region(q.label) is not None}
        self.pmap.set_marks(on_map)
        n = len(self.positions)
        self.lbl_pmap.setText(f"Selected {n} of {len(ses.vessel.regions)} " +
                              ("wells" if ses.vessel.is_plate else "regions") +
                              (f" ({n - len(on_map)} not on a well)" if n - len(on_map) else ""))

    def _show_problem(self, text: str) -> None:
        self.lbl_errors.setText("Fix before acquiring: " + text)
        self.lbl_errors.setVisible(True)
        for w in (self.lbl_confirm, self.lbl_notes, self.lbl_checks, self.fix_box):
            w.setVisible(False)
        self.last_preflight = None
        self._update_acquire()

    def _show_preflight(self, pre) -> None:
        self.lbl_errors.setVisible(bool(pre.errors))
        self.lbl_errors.setText("Fix before acquiring:\n" + "\n".join(f"• {e}" for e in pre.errors))
        self.lbl_confirm.setVisible(bool(pre.confirm))
        self.lbl_confirm.setText(f"Needs confirming when you acquire: {pre.confirm}" if pre.confirm else "")
        self.lbl_notes.setVisible(bool(pre.notes))
        self.lbl_notes.setText("\n".join(f"• {n}" for n in pre.notes))
        self.lbl_checks.setVisible(bool(pre.checks))
        self.lbl_checks.setText(checklist_html(list(pre.checks)))
        self._show_fixes(pre.fixes, pre.fix_hint)

    def _show_fixes(self, fixes: tuple[MDAFix, ...], hint: str | None) -> None:
        while self.fix_lay.count():
            item = self.fix_lay.takeAt(0)
            old = item.widget() if item is not None else None
            if old is not None:
                old.deleteLater()
        self.fix_buttons = []
        for fx in fixes:
            b = QPushButton(f"Fix {fx.check.lower()}: {fx.label}")
            b.setToolTip(fx.explain)
            b.setAccessibleName(f"{fx.label}. {fx.explain}")
            b.clicked.connect(lambda _c=False, f=fx: self.apply_fix(f))
            self.fix_buttons.append(b)
            self.fix_lay.addWidget(b)
            self.fix_lay.addWidget(label(fx.explain, "caption", wrap=True))
        if hint:
            self.fix_lay.addWidget(label(hint, "warn", wrap=True))
        self.fix_box.setVisible(bool(hint or fixes))

    def apply_fix(self, fx: MDAFix) -> None:
        """A pre-flight Fix button: edits the plan (the focus reference), never moves anything."""
        if fx.global_anchor_um is not None:
            self.anchor_um = fx.global_anchor_um
        if fx.anchors:
            new = dict(fx.anchors)
            self.positions = [replace(q, z_anchor_um=new[q.position_id]) if q.position_id in new else q
                              for q in self.positions]
            self._fill_table()
        self.model.add_log("info", f"Multi-D: {fx.label}. {fx.explain}")
        self.refresh()  # at once, so the check turns green under the operator's click

    def _running(self) -> bool:
        return self.model.tasks.busy and self.model.tasks.current == TASK

    def _update_acquire(self) -> None:
        pre = self.last_preflight
        busy = self.model.tasks.busy
        ok = pre is not None and pre.ok and not busy
        self.btn_acquire.setEnabled(ok)
        self.btn_acquire.setToolTip("" if ok else "Another workflow is running" if busy else
                                    "Fix the items marked ✗ under Pre-flight validation (its Fix buttons can do it)")

    def _on_busy(self, _busy: bool) -> None:
        running = self._running()
        self._plan_scroll.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        if not running:
            self.btn_pause.setText("Pause")
        self._update_acquire()

    # ------------------------------------------------------------------ plan files
    def _save_plan(self, _c: bool = False) -> None:
        try:
            plan = self.plan()
        except (CaptureRejected, ValueError) as exc:
            self.model.add_log("warn", f"Multi-D: {exc}")
            return
        root = self.save_root()
        f, _ = QFileDialog.getSaveFileName(self, "Save plan", str(root / "plan.json"), "Multi-D plan (*.json)")
        if f:
            Path(f).write_text(plan.to_json(), "utf-8")
            self.model.add_log("info", f"Multi-D: plan saved to {f}")

    def _load_plan(self, _c: bool = False) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Load plan", str(self.save_root()), "Multi-D plan (*.json)")
        if f:
            self.load_plan_file(Path(f))

    def load_plan_file(self, path: Path) -> bool:
        try:
            text = path.read_text("utf-8")
            plan = MultiDimPlan.from_json(text)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.model.add_log("warn", f"Multi-D: cannot load {path.name}: {exc}")
            return False
        self.apply_plan(plan, with_positions=True)
        self.model.add_log("info", f"Multi-D: loaded {path.name}; check positions and the focus reference "
                                   "against this sample before acquiring")
        return True

    # ------------------------------------------------------------------ acquire
    def _confirm(self, text: str, title: str = "Acquire above the safe height?", run_text: str = "Acquire") -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, parent=self)
        run = box.addButton(run_text, QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is run

    def acquire(self, *, ask=None) -> bool:
        """Start the experiment; `ask(question) -> bool` replaces the confirmation box (tests)."""
        if self.model.tasks.busy:
            return False
        try:
            plan = self.plan().fresh()
        except (CaptureRejected, ValueError) as exc:
            self.model.add_log("warn", f"Multi-D: {exc}")
            return False
        summ = summarize(plan, self.session.profile)
        pre = preflight(plan, self.session, summ, save_root=self.save_root())
        if not pre.ok:
            self._show_preflight(pre)
            self.model.add_log("warn", "Multi-D: " + "; ".join(pre.errors))
            return False
        confirmed = confirmed_positions = False
        if pre.confirm_positions:
            q = pre.confirm_positions
            if not (ask(q) if ask else self._confirm(q, "Move between positions on the instrument?",
                                                      "Move and acquire")):
                return False
            confirmed_positions = True
            n = sum(1 for p in plan.positions if p.moves)
            self.model.add_log("warn", f"Multi-D: operator confirmed moves between {n} positions on the instrument "
                                       "(XY stage not yet qualified)")
        if pre.confirm:
            if not (ask(pre.confirm) if ask else self._confirm(pre.confirm)):
                return False
            confirmed = True
        self.md.last_plan = plan.to_dict()
        writer = ExperimentWriter(self.save_root(), reserve_mb=STORAGE_RESERVE_MB)
        runner = MultiDimRunner(self.session, writer)
        self._runner, self._run_plan, self._run_summary = runner, plan, summ
        self._run_started = time.monotonic()
        self.m_log.clear()
        self._last_msg = ""
        self.m_exp.setText(plan.name or "Unnamed experiment")
        self.m_queue.setRowCount(0)
        for k, n in (("z", summ.n_z), ("c", summ.n_c), ("t", summ.n_t), ("p", summ.n_p)):
            self.ax[k].set_value(0, n)
        self.m_progress.setRange(0, max(1, summ.images))
        self.m_progress.setValue(0)
        self.m_view.set_image(None)
        self.btn_review.setVisible(False)
        self.m_timeline.set_state(plan.n_t, {})
        self._log(f"Started: {summ.images:,} images, about {fmt_seconds(summ.total_s)}")
        self._go_step("acquire")
        disp = self.model.dispatcher

        def on_progress(p: MDAProgress) -> None:  # worker thread
            snap = replace(p, timepoints=[dict(r) for r in p.timepoints])
            disp(lambda: self._on_progress(snap))

        def on_image(job, slot, zi, done, img) -> None:  # worker thread; only the newest image is drawn
            self._latest = (job, slot, zi, img)
            if not self._img_pending:
                self._img_pending = True
                disp(self._show_latest)

        def work():
            return runner.run(plan, summ, pre, confirmed_above_safe=confirmed,
                              confirmed_positions=confirmed_positions, progress=on_progress, on_image=on_image)

        return bool(self.model.submit_when_idle(TASK, work, on_done=self._done, on_error=self._error))

    def _pause_resume(self, _c: bool = False) -> None:
        r = self._runner
        if r is None or not self._running():
            return
        if r.control.paused:
            r.resume()
            self.btn_pause.setText("Pause")
            self._log("Resumed")
        else:
            r.pause()
            self.btn_pause.setText("Resume")
            self._log("Pause requested: pausing at the next safe point")

    def stop(self, _c: bool = False) -> None:
        r = self._runner
        if r is not None and self._running():
            r.stop()
            self._log("Stop requested: stopping after the current move or exposure; data taken so far is kept")
            self.m_status.setText("Stopping…")

    def _log(self, text: str) -> None:
        self.m_log.addItem(f"{time.strftime('%H:%M:%S')}  {text}")
        while self.m_log.count() > 400:
            self.m_log.takeItem(0)
        self.m_log.scrollToBottom()

    def _on_progress(self, p: MDAProgress) -> None:
        plan, summ = self._run_plan, self._run_summary
        if plan is None or summ is None:
            return
        self.m_t.setText(f"T{p.t_index + 1} ({p.t_index + 1}/{plan.n_t})")
        self.m_p.setText(f"{p.position_label or '–'} ({p.p_index + 1}/{plan.n_p})")
        self.m_p.setToolTip(p.position_label)
        labels = [c.label for c in plan.channels]
        ci = labels.index(p.channel_label) if p.channel_label in labels else 0
        self.m_c.setText(f"{p.channel_label or '–'} ({ci + 1}/{summ.n_c})")
        self.m_z.setText(f"{p.z_index + 1} / {summ.n_z}")
        self._show_run(p, plan, summ, ci)
        self.m_progress.setValue(min(p.images_done, summ.images))
        left = ""
        if p.images_done and p.state is not MDAState.PAUSED:
            rate = p.elapsed_s / p.images_done
            left = f", about {fmt_seconds(rate * (summ.images - p.images_done))} left"
        state = "Paused. " if p.state is MDAState.PAUSED else ""
        self.m_status.setText(f"{state}{p.images_done:,} of {summ.images:,} images, "
                              f"{fmt_seconds(p.elapsed_s)} elapsed{left}. {p.message}")
        noisy = (p.message.startswith(("Slice ", "Z ", "Moving", "Settling")) or "next starts in" in p.message
                 or p.message == "Resumed" or p.state in (MDAState.COMPLETED, MDAState.PARTIAL, MDAState.FAILED))
        if p.message and p.message != self._last_msg and not noisy:
            self._last_msg = p.message
            self._log(p.message)
        self.m_tl_text.setText(self.m_timeline.set_state(plan.n_t, _tp_status(p.timepoints)))
        r = self._runner
        if r is not None and r.writer.folder is not None:
            m = r.writer.manifest
            self.m_writer.setText(f"{r.writer.folder.name}\n{m.get('images_written', 0):,} images written; last "
                                  f"{m.get('last_event_id') or '–'}")

    def _show_run(self, p: MDAProgress, plan: MultiDimPlan, summ, ci: int) -> None:
        """Per-axis progress, the queue of this timepoint's volumes, the status card and the map (kit doc 06 §3: never
        one percentage that hides a stuck dimension)."""
        per_vol = max(1, summ.n_c * summ.n_z)
        vol_done = p.images_done - (p.t_index * summ.n_p + p.p_index) * per_vol
        vol_done = max(0, min(per_vol, vol_done))
        cz = plan.traversal is Traversal.T_P_C_Z
        self.ax["z"].set_value(min(summ.n_z, p.z_index + (1 if vol_done else 0)), summ.n_z)
        self.ax["c"].set_value(min(summ.n_c, (vol_done // max(1, summ.n_z)) if cz else ci + (1 if vol_done else 0)),
                               summ.n_c)
        self.ax["t"].set_value(sum(1 for r in p.timepoints if "finished_at" in r), summ.n_t)
        self.ax["p"].set_value(p.p_index + (1 if vol_done >= per_vol else 0), summ.n_p)
        q = self.m_queue
        q.setRowCount(summ.n_p)
        chans = ", ".join(c.label for c in plan.channels)
        for i, pos in enumerate(plan.positions[:summ.n_p] or [None] * summ.n_p):
            name = pos.label if pos is not None else "Here"
            if i < p.p_index:
                frac, status = 1.0, "Done"
            elif i == p.p_index:
                frac, status = vol_done / per_vol, ("Paused" if p.state is MDAState.PAUSED else "Acquiring")
            else:
                frac, status = 0.0, "Queued"
            vals = [name, f"T{p.t_index + 1}", chans, f"{frac * 100:.0f} %", status]
            for c, v in enumerate(vals):
                it = q.item(i, c)
                if it is None:
                    it = QTableWidgetItem()
                    q.setItem(i, c, it)
                it.setText(v)
                if c == 4:
                    it.setForeground(QColor(theme.OK if status == "Done" else theme.ACCENT if status == "Acquiring"
                                            else theme.WARN if status == "Paused" else theme.TEXT_2))
        kv = self.kv_run
        kv.set("position", f"{p.position_label or 'Here'} ({p.p_index + 1}/{summ.n_p})")
        kv.set("channel", f"{p.channel_label or '–'} ({ci + 1}/{summ.n_c})")
        kv.set("z", f"{p.z_index + 1} / {summ.n_z}" + (f" · {p.z_um:,.2f} µm" if p.z_um is not None else ""))
        kv.set("t", f"T{p.t_index + 1} / {summ.n_t}")
        pct = p.images_done / max(1, summ.images) * 100
        kv.set("images", f"{p.images_done:,} / {summ.images:,} ({pct:.1f} %)")
        left = "–"
        if p.images_done and p.state is not MDAState.PAUSED:
            left = fmt_seconds(p.elapsed_s / p.images_done * (summ.images - p.images_done))
        kv.set("left", left)
        kv.set("written", _size(p.images_done * summ.bytes_per_image / 1e6))
        kv.set("rate", f"{p.images_done * summ.bytes_per_image / 1e6 / p.elapsed_s:,.1f} MB/s" if p.elapsed_s > 0
               else "–")
        on_map = {q.label for q in plan.positions if self.session.vessel.region(q.label) is not None}
        done = {q.label for k, q in enumerate(plan.positions) if k < p.p_index} & on_map
        self.m_map.set_marks(on_map, p.position_label if p.position_label in on_map else None, done)

    def _show_latest(self) -> None:
        self._img_pending = False
        latest = self._latest
        if latest is None:
            return
        _job, slot, _zi, img = latest
        self.m_view.set_image(render_frame(img.data, slot, self.session.profile, self.session.settings))

    def _done(self, res: MDAResult) -> None:
        self._runner = None
        words = {MDAState.COMPLETED: "Completed", MDAState.PARTIAL: "Partial", MDAState.FAILED: "Failed"}
        text = f"{words.get(res.state, res.state.value)}: {res.message}"
        self.m_status.setText(text + (f". Saved in {Path(res.folder).name}" if res.folder else ""))
        self.m_status.setToolTip(str(res.folder or ""))
        self._log(text)
        self.model.add_log("info" if res.state is MDAState.COMPLETED else "warn", f"Multi-D: {text}")
        if res.state is MDAState.COMPLETED:
            for r in range(self.m_queue.rowCount()):
                for c, v in ((3, "100 %"), (4, "Done")):
                    it = self.m_queue.item(r, c)
                    if it is not None:
                        it.setText(v)
                        if c == 4:
                            it.setForeground(QColor(theme.OK))
        if self._run_plan is not None:
            self.m_tl_text.setText(self.m_timeline.set_state(self._run_plan.n_t, _tp_status(res.timepoints)))
        if res.folder and res.images_written:
            self.btn_review.setVisible(True)
            self.open_experiment(Path(res.folder))
        self._on_busy(False)

    def _error(self, msg: str) -> None:
        self._runner = None
        self.m_status.setText(f"Could not run: {msg}")
        self._log(f"Could not run: {msg}")
        self._on_busy(False)

    # ------------------------------------------------------------------ review
    def _open_dialog(self, _c: bool = False) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open experiment", str(self.save_root()))
        if d:
            self.open_experiment(Path(d))
            self._go_step("review")

    def open_experiment(self, folder: Path) -> bool:
        try:
            rd = ExperimentReader(folder)
        except (OSError, ValueError, KeyError) as exc:
            self.model.add_log("warn", f"Multi-D: {folder.name} is not a readable experiment: {exc}")
            return False
        self._reader = rd
        exp, man = rd.experiment, rd.manifest
        for w in (self.r_t, self.r_p, self.r_c, self.r_z, self.r_from, self.r_to):
            w.blockSignals(True)
        self.r_t.setRange(1, max(1, rd.n_t))
        self.r_t.setValue(1)
        self.r_t.setEnabled(rd.n_t > 1)
        self.r_p.clear()
        for p in range(rd.n_p):
            v = rd.volume_info(0, p)
            self.r_p.addItem(f"{v['position_id']} {v['label']}", p)
        self.r_p.setEnabled(rd.n_p > 1)
        self.r_c.clear()
        for slot, lab in rd.channels():
            self.r_c.addItem(lab, slot)
        for w in (self.r_t, self.r_p, self.r_c, self.r_z, self.r_from, self.r_to):
            w.blockSignals(False)
        self.r_folder.setText(str(folder))
        self.r_folder.setToolTip(str(folder))
        sim = " Images are SIMULATED (synthetic)." if exp.get("simulated") else ""
        status = man.get("status", "?")
        msg = str(man.get("message", "")).rstrip(".")
        why = f". {msg[:1].upper()}{msg[1:]}" if msg else ""
        self.r_status.setText(f"{status.capitalize()}: {man.get('images_written', 0):,} of "
                              f"{man.get('images_planned', 0):,} images{why}.{sim}")
        self.r_tree.clear()
        groups: dict[int, QTreeWidgetItem] = {}
        for v in man["volumes"]:
            top = groups.get(v["p_index"])
            if top is None:
                top = QTreeWidgetItem([f"{v['position_id']} {v['label']}", "", ""])
                top.setData(0, Qt.ItemDataRole.UserRole, (0, v["p_index"]))
                self.r_tree.addTopLevelItem(top)
                groups[v["p_index"]] = top
            st = v["status"]
            child = QTreeWidgetItem([f"T{v['t_index'] + 1:03d}", str(v.get("images", "")), st])
            child.setData(0, Qt.ItemDataRole.UserRole, (v["t_index"], v["p_index"]))
            child.setForeground(2, QColor(theme.OK if st == "completed" else theme.WARN if st == "partial"
                                          else theme.MUTED))
            top.addChild(child)
        for k, top in enumerate(groups.values()):
            kids = [top.child(i) for i in range(top.childCount())]
            n_ok = sum(1 for kid in kids if kid is not None and kid.text(2) == "completed")
            top.setText(2, f"{n_ok}/{top.childCount()}")
            top.setExpanded(k < 3)
        for b in (self.btn_rfolder, self.btn_rmanifest):
            b.setEnabled(True)
        self.r_missing.clear()
        for v in man["volumes"]:
            if v["status"] != "completed":
                it = f"T{v['t_index'] + 1}, {v['position_id']} {v['label']}: {v['status']}"
                self.r_missing.addItem(it)
                self.r_missing.item(self.r_missing.count() - 1).setData(Qt.ItemDataRole.UserRole,
                                                                        (v["t_index"], v["p_index"]))
        if not self.r_missing.count():
            self.r_missing.addItem("Every planned volume was acquired.")
        self._review_show()
        return True

    def _tree_clicked(self, item, _col: int = 0) -> None:
        tp = item.data(0, Qt.ItemDataRole.UserRole)
        if tp:
            self.r_p.setCurrentIndex(tp[1])
            self.r_t.setValue(tp[0] + 1)

    def _reveal(self, name: str) -> None:
        rd = self._reader
        if rd is None:
            return
        target = rd.folder / name if name else rd.folder
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _set_meta(self, rows: list[tuple[str, str]]) -> None:
        t = self.r_meta
        t.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            a = QTableWidgetItem(k)
            a.setForeground(QColor(theme.TEXT_2))
            t.setItem(r, 0, a)
            t.setItem(r, 1, QTableWidgetItem(v))
        t.resizeColumnToContents(0)
        t.resizeRowsToContents()

    def _missing_activated(self, item) -> None:
        tp = item.data(Qt.ItemDataRole.UserRole)
        if tp:
            self.r_t.setValue(tp[0] + 1)
            self.r_p.setCurrentIndex(tp[1])

    def _review_show(self, *_a) -> None:
        rd = self._reader
        if rd is None:
            return
        t, p = self.r_t.value() - 1, max(0, self.r_p.currentIndex())
        slot = self.r_c.currentData()
        vol = rd.load(t, p)
        info = rd.volume_info(t, p)
        if vol is None or slot not in vol["stacks"]:
            self.r_view.set_image(None)
            self.r_zlab.setText("")
            what = "not acquired" if vol is None else "no images in this channel"
            self._set_meta([("Volume", f"T{t + 1}, {info['position_id']} {info['label']}"),
                            ("Status", f"{what} ({info['status']})")])
            return
        arr, zs = vol["stacks"][slot]
        n = arr.shape[0]
        for w in (self.r_z, self.r_from, self.r_to):
            w.blockSignals(True)
        self.r_z.setRange(0, n - 1)
        self.r_from.setRange(1, n)
        self.r_to.setRange(1, n)
        if self.r_to.value() < self.r_from.value() or self.r_to.property("n") != n:
            self.r_from.setValue(1)
            self.r_to.setValue(n)
            self.r_to.setProperty("n", n)
        for w in (self.r_z, self.r_from, self.r_to):
            w.blockSignals(False)
        mode = self.r_mode.currentData()
        proj = mode != "slice"
        self.r_from.setVisible(proj)
        self.r_to.setVisible(proj)
        self.r_z.setEnabled(not proj and n > 1)
        zi = self.r_z.value()
        if proj:
            a, b = self.r_from.value() - 1, self.r_to.value() - 1
            a, b = min(a, b), max(a, b)
            raw = project(list(arr[a:b + 1]), mode)
            if raw.dtype != arr.dtype and mode in ("mean", "std"):
                raw = np.clip(raw, 0, np.iinfo(arr.dtype).max if arr.dtype.kind in "ui" else raw.max())
            self.r_zlab.setText(f"{METHODS[mode]} of slices {a + 1}–{b + 1}")
        else:
            raw = arr[zi]
            self.r_zlab.setText(f"Slice {zi + 1} of {n}, {zs[zi]:,.2f} µm" if zi < len(zs) and zs[zi] is not None
                                else f"Slice {zi + 1} of {n}")
        self.r_view.set_image(render_frame(np.asarray(raw), slot, self.session.profile, self.session.settings))
        rec = vol["record"]
        md = rec.get("mda", {})
        pos = md.get("position", {})
        ch: dict = next((c for c in rec.get("channels", []) if c["slot"] == slot), {})
        sl = ch.get("slices", [])
        s0 = sl[zi] if zi < len(sl) else {}
        rows = [
            ("Dataset", str(rd.experiment.get("name") or rd.folder.name)),
            ("Status", f"{rd.manifest.get('status')}"),
            ("Plan", str(rd.experiment.get("plan_id"))),
            ("Provenance", str(rec.get("provenance"))),
            ("Timepoint", f"{t + 1} of {rd.n_t}"),
            ("Position", f"{pos.get('id')} {pos.get('label')}"),
            ("Stage", _xy(pos)),
            ("Focus reference", f"{_num(md.get('anchor_um'))} µm"),
            ("Volume", f"{info['status']}, {info['images']} images"),
            ("Started", str(info.get("started_at", "–"))),
            ("Channel", f"{ch.get('label', slot)}, {n} slices" + ("" if ch.get("complete") else " (incomplete)")),
        ]
        for key, word, unit in (("z_um", "Focus (observed)", " µm"), ("exposure_ms", "Exposure", " ms"),
                                ("gain", "Gain", ""), ("led_percent", "Light", " %"),
                                ("pixel_size_um", "Pixel size", " µm"), ("bit_depth", "Bit depth", ""),
                                ("captured_at", "Captured", "")):
            if key in s0:
                rows.append((word, f"{_num(s0[key])}{unit}"))
        foc = rec.get("focus", {}).get(slot)
        if foc:
            rows.append(("Sharpest slice", f"{foc.get('sharpest_slice')} ({foc.get('metric')})"))
        self._set_meta(rows)
