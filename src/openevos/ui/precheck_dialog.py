"""System precheck dialog (start-up and Microscope › System precheck) and the guided safe-height table.

Both are non-modal: the operator keeps using the Capture page (focus pad, joystick) while they are open.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from openevos.acquisition import precheck as pc
from openevos.ui import theme
from openevos.ui.common import label
from openevos.ui.widgets.focus_pad import fmt_um

STATUS_TEXT = {"ok": "OK", "info": "INFO", "warn": "WARNING", "fault": "FAULT"}
STATUS_MARK = {"ok": "✓", "info": "i", "warn": "▲", "fault": "✕"}  # shape as well as colour


def status_color(status: str) -> str:
    return {"ok": theme.OK, "info": theme.TEXT_2, "warn": theme.WARN, "fault": theme.FAULT}[status]


class PrecheckDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.session = window.session
        self.setWindowTitle("System precheck")
        self.setModal(False)
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        self.lbl_summary = label("", "title")
        lay.addWidget(self.lbl_summary)
        sub = label("Reads every mapped device, the drift monitor, the live view and the objective safe heights. "
                    "Nothing is sent to the instrument.", "caption", wrap=True)
        lay.addWidget(sub)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(self.scroll, 1)
        row = QHBoxLayout()
        self.btn_release = QPushButton("Release stage lock…")
        self.btn_release.setToolTip("Shown when the drift monitor holds a small lock that fresh reads can release")
        self.btn_release.clicked.connect(lambda: self.win.release_stage_lock())
        row.addWidget(self.btn_release)
        self.btn_heights = QPushButton("Set safe heights…")
        self.btn_recheck = QPushButton("Check again")
        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("primary")
        row.addWidget(self.btn_heights)
        row.addStretch(1)
        row.addWidget(self.btn_recheck)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.btn_close.clicked.connect(self.close)
        self.btn_recheck.clicked.connect(self.refresh)
        self.btn_heights.clicked.connect(self.win.open_safe_heights)
        self.checks: list[pc.Check] = []
        self.refresh()

    def refresh(self) -> list[pc.Check]:
        self.checks = pc.run_precheck(self.session)
        body = QWidget()
        g = QGridLayout(body)
        g.setContentsMargins(0, 0, 8, 0)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        g.setColumnStretch(1, 1)
        order = sorted(self.checks, key=lambda c: pc.ORDER.index(c.status))  # problems first
        for i, c in enumerate(order):
            col = status_color(c.status)
            badge = QLabel(f"{STATUS_MARK[c.status]}  {STATUS_TEXT[c.status]}")
            badge.setStyleSheet(f"color: {col}; font-weight: 700; border: 1px solid {col}; border-radius: 11px; "
                                "padding: 2px 9px;")
            badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            g.addWidget(badge, i, 0, Qt.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(2)
            t = label(c.title, "")
            t.setStyleSheet("font-weight: 600;")
            d = label(c.detail, "caption", wrap=True)
            text.addWidget(t)
            text.addWidget(d)
            if c.fix and c.status != "ok":
                f = label(f"→ {c.fix}", "caption", wrap=True)
                f.setTextInteractionFlags(Qt.TextSelectableByMouse)
                text.addWidget(f)
            box = QWidget()
            box.setLayout(text)
            box.setAccessibleName(f"{c.title}: {STATUS_TEXT[c.status]}. {c.detail}. {c.fix}")
            g.addWidget(box, i, 1)
        g.setRowStretch(len(order), 1)
        self.scroll.setWidget(body)
        w = pc.worst(self.checks)
        self.btn_release.setVisible(self.win._release_why() == "")
        self.lbl_summary.setText(pc.summary(self.checks))
        self.lbl_summary.setStyleSheet(f"color: {status_color(w) if w in ('fault', 'warn') else theme.TEXT};")
        return self.checks


class SafeHeightsDialog(QDialog):
    """Guided table: one row per installed objective. Go to it, raise Z with the focus pad until the objective is
    close to the vessel, then Set here. Values go to ui_settings.json (Settings › focus_limit_um)."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.session = window.session
        self.cap = window.pages["capture"]
        self.setWindowTitle("Objective safe heights")
        self.setModal(False)
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Objective safe heights", "title"))
        lay.addWidget(label(
            "For each objective: Go to it (the stage must be centred; Z retracts to 0 before the turret turns). Then "
            "raise Z with the focus pad, watching the objective, until it is close to the vessel you image with. Stop "
            "with a margin, then Set here. Above this height the focus readout turns amber and a held pad stops.",
            "caption", wrap=True))
        self.lbl_now = label("", "mono")
        lay.addWidget(self.lbl_now)
        self.lbl_msg = label("", "warn", wrap=True)  # the last refusal, until the next action
        lay.addWidget(self.lbl_msg)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for c, h in enumerate(("Objective", "Safe height", "", "", "")):
            if h:
                grid.addWidget(label(h, "section"), 0, c)
        self.rows: dict[int, tuple[QLabel, QPushButton, QPushButton, QPushButton]] = {}
        objs = [o for o in self.session.profile.objectives if o.installed]
        for r, o in enumerate(objs, start=1):
            name = label(f"{o.slot}  {o.short}", "")
            val = label("", "mono")
            go = QPushButton("Go to")
            go.setToolTip(f"Turn the turret to {o.short} (Z retracts to 0 first; the stage must be centred)")
            setb = QPushButton("Set here")
            setb.setObjectName("primary")
            clr = QPushButton("Clear")
            go.clicked.connect(lambda _=False, s=o.slot: self._go(s))
            setb.clicked.connect(lambda _=False: self.cap._set_focus_limit())
            clr.clicked.connect(lambda _=False, s=o.slot: self.cap._clear_focus_limit(s))
            for c, w in enumerate((name, val, go, setb, clr)):
                grid.addWidget(w, r, c)
            self.rows[o.slot] = (val, go, setb, clr)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Done")
        done.clicked.connect(self.close)
        row.addWidget(done)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _go(self, slot: int) -> None:
        self.lbl_msg.setText("")
        box = pc.stage_box(self.session)
        if box is not None and box.status != "ok":
            text = f"Objective change refused: {box.detail}. {box.fix}"
            self.win.model.add_log("warn", text)
            self.lbl_msg.setText(text)
            return
        self.cap._objective(slot)

    def refresh(self) -> None:
        ctl = self.session.controller
        slot, z = ctl.objective_slot(), ctl.focus_z()
        limits = self.session.settings.basic.focus_limit_um
        tur = ctl.snapshot(self.session.profile.turret_device) if self.session.profile.turret_device else None
        moving = (tur is not None and tur.activity.value == "moving") or self.cap.coalesce.busy("turret") \
            or self.cap.coalesce.busy("focus")
        obj = self.session.profile.objective(slot) if slot else None
        self.lbl_now.setText(f"In the light path: {obj.short if obj else '—'} · Z "
                             f"{fmt_um(z) if z is not None else '—'} µm" + (" · moving" if moving else ""))
        for s, (val, go, setb, clr) in self.rows.items():
            v = limits.get(str(s))
            val.setText(f"{fmt_um(v)} µm" if v is not None else "not set")
            val.setStyleSheet(f"color: {theme.TEXT if v is not None else theme.WARN};")
            here = s == slot
            go.setEnabled(not here and not moving)
            setb.setEnabled(here and z is not None and not moving)
            setb.setToolTip(f"Set {obj.short if obj else s} safe height to {fmt_um(z)} µm" if here and z is not None
                            else "Go to this objective first")
            clr.setEnabled(v is not None)

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)
