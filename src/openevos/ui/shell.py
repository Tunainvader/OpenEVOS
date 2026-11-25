"""Main window — Craftimon shell conventions applied to OpenEVOS.

Layout (top to bottom):
  menu strip   File · Edit · View · Acquire · Microscope · Analyze · Window · Help   [search] [SIMULATION]
  status bar   brand + profile | device pills (colour + text) | Stop all (Esc)
  body         navigation rail (icons + labels; labels drop below 1360 px) | workspace
  footer       session · command bus · task · XY/Z/objective · raw-data note

Menu rules: two levels maximum; commands that do not apply are greyed, never hidden, and their tooltip
says why; "…" opens a dialog, "▸" a submenu, checkmarks are toggles; destructive commands last.
Every command is also reachable from the command palette (Ctrl+K).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from openevos import __version__
from openevos.core.models import CommandStatus, Connection, Health
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import HotButtonAction
from openevos.ui import theme
from openevos.ui import theme_engine as te
from openevos.ui.common import label
from openevos.ui.icons import icon
from openevos.ui.instrument_mode import instrument_mode
from openevos.ui.session_model import SessionModel
from openevos.ui.widgets.status import Badge, HealthSummary, StatusPill

NAV = [
    ("Acquire", [("capture", "Capture", "capture"), ("scan", "Scan", "scan"), ("timelapse", "Time-lapse", "timelapse"),
                 ("zstack", "Z-stack", "zstack"), ("multid", "Multi-D", "multid"),
                 ("movies", "Movies", "movies")]),
    ("Inspect", [("save", "Save", "save"), ("review", "Review", "review")]),
    ("Analyze", [("analysis", "Analysis", "analysis")]),
    ("Instrument", [("health", "Health", "health"), ("environment", "Environment", "environment"),
                    ("calibration", "Calibration", "calibration")]),
    ("Manage", [("settings", "Settings", "settings"), ("service_area", "Service", "service")]),
]
#: engineering pages, one "Service" area with tabs (UI audit 2026-11-17: 15 rail entries were too many);
#: navigate("hardware") etc. still lands on the right tab
SERVICE_TABS = (("hardware", "Hardware"), ("service", "Functions"), ("diagnostics", "Diagnostics"),
                ("events", "Event log"))
RAIL_BELOW_PX = 1440
#: Re-home stage lowers the objective itself when Z is above this (the drive service refuses beyond 5 Z counts, 0.6 µm)
REHOME_Z_LOWERED_UM = 0.5
REHOME_Z_TIMEOUT_S = 40.0
#: any settings change reaches the settings file within this (operator 2026-11-22: "the plate selection is not saved
#: on quitting"): they were written only in closeEvent, and a run that ended without a clean close lost them all
SETTINGS_AUTOSAVE_MS = 2000


def describe_snapshot(snap) -> tuple[str, str]:
    if snap is None:
        return "unknown", "NOT REGISTERED"
    if snap.connection is not Connection.CONNECTED:
        return ("fault" if snap.health is Health.FAULT else "unknown"), snap.connection.value.upper()
    if snap.health is Health.FAULT or snap.recovery_required:
        return "fault", "RECOVERY REQUIRED" if snap.recovery_required else "FAULT"
    if snap.health is not Health.NORMAL:
        return "warn", snap.health.value.upper()
    sim = snap.backend_kind.value == "simulated"
    if snap.activity.value not in ("idle", "unknown"):
        return "busy", ("SIM · " if sim else "") + snap.activity.value.upper()
    return ("simulated" if sim else "ok"), "SIMULATED · IDLE" if sim else "IDLE"


@dataclass
class Cmd:
    """A command: menu entry + palette entry. `why_disabled` returns a reason or '' when available."""

    key: str
    text: str
    run: object
    shortcut: str | None = None
    why_disabled: object = None
    checked: object = None  # callable -> bool for toggles
    menu_path: str = ""
    action: QAction | None = field(default=None, repr=False)


class CommandPalette(QDialog):
    """Ctrl+K: type to find any command; disabled commands are listed greyed with the reason."""

    def __init__(self, win: MainWindow):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Search commands")
        self.setModal(True)
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search commands…  e.g. capture, objective 20x, theme")
        self.list = QListWidget()
        lay.addWidget(self.edit)
        lay.addWidget(self.list, 1)
        lay.addWidget(label("Enter runs · Esc closes · greyed commands show why they are unavailable", "caption"))
        self.edit.textChanged.connect(self.refresh)
        self.edit.returnPressed.connect(self.run_current)
        self.list.itemActivated.connect(lambda _it: self.run_current())
        self.refresh()

    def refresh(self) -> None:
        q = self.edit.text().lower().split()
        self.list.clear()
        for c in self.win.commands.values():
            hay = f"{c.menu_path} {c.text}".lower()
            if all(w in hay for w in q):
                why = c.why_disabled() if callable(c.why_disabled) else ""
                text = f"{c.text.replace('&', '')}    —  {c.menu_path}" + (f"    [{c.shortcut}]" if c.shortcut else "")
                it = QListWidgetItem(text + (f"   ({why})" if why else ""))
                it.setData(Qt.UserRole, c.key)
                if why:
                    it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
                self.list.addItem(it)
        for i in range(self.list.count()):
            if self.list.item(i).flags() & Qt.ItemIsEnabled:
                self.list.setCurrentRow(i)
                break

    def run_current(self) -> None:
        it = self.list.currentItem()
        if it and it.flags() & Qt.ItemIsEnabled:
            key = it.data(Qt.UserRole)
            self.accept()
            self.win.run_command(key)


class MainWindow(QMainWindow):
    def __init__(self, model: SessionModel):
        super().__init__()
        self.model = model
        self.session = model.session
        # what is real, from each device's backend: title, header, chip, footer and About all say the same
        self.mode = instrument_mode(self.session)
        self.setWindowTitle(f"OpenEVOS {__version__} — {self.mode.name}")
        self.resize(1600, 960)
        self.setMinimumSize(1100, 720)  # below this the three Capture panels overlap (Impeccable audit 2026-11-18)
        self.commands: dict[str, Cmd] = {}
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._status_bar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._nav())
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        v.addLayout(body, 1)
        v.addWidget(self._footer())
        self.pages: dict[str, QWidget] = {}
        self._build_pages()
        self._build_menus()
        model.device_changed.connect(self._on_device)
        model.command_event.connect(self._on_command_event)
        model.tasks.busy_changed.connect(lambda _b: self._tick())
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._settings_saved = self._settings_snapshot()
        self._autosave_failed = False
        self._autosave = QTimer(self)
        self._autosave.setInterval(SETTINGS_AUTOSAVE_MS)
        self._autosave.timeout.connect(self.autosave_settings)
        self._autosave.start()
        for did in self.session.core.devices.device_ids():
            self._on_device(did, self.session.core.store.get(did))
        self.navigate("capture")
        self._apply_nav_mode()

    # ================================================================== chrome
    def _status_bar(self) -> QWidget:
        f = QFrame()
        f.setObjectName("header")
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 6, 10, 6)
        lay.setSpacing(8)
        lay.addWidget(label("OpenEVOS", "brand"))
        self.lbl_profile = label(f"{self.session.profile.model} · {self.mode.name}", "muted")
        lay.addWidget(self.lbl_profile)
        lay.addStretch(1)
        p = self.session.profile
        self.pills: dict[str, StatusPill] = {}
        for did, title in ((p.stage_device, "Stage"), (p.focus_device, "Focus"), (p.turret_device, "Turret"),
                           (p.illumination_device, "Light"),
                           (p.camera(CameraRole.MONO).device_id if p.camera(CameraRole.MONO) else None, "Mono cam"),
                           (p.camera(CameraRole.COLOR).device_id if p.camera(CameraRole.COLOR) else None, "Color cam")):
            if did:
                pill = StatusPill(title)
                self.pills[did] = pill
        stage_pill = self.pills.get(p.stage_device)
        if stage_pill is not None:  # a locked stage: clicking its pill offers the release (ADR-0057)
            stage_pill.setCursor(Qt.PointingHandCursor)
            stage_pill.setFocusPolicy(Qt.StrongFocus)
            stage_pill.clicked.connect(self._stage_pill_clicked)
        env = StatusPill("Environment")
        env.set_state("unavailable", "UNAVAILABLE", "Onstage incubator: no control or telemetry path (bring-up record)")
        # one summary pill; the per-device pills open from it (UI audit 2026-11-17)
        self.health = HealthSummary([*self.pills.values(), env])
        lay.addWidget(self.health)
        self.btn_stop = QPushButton("Stop all")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setIcon(icon("stop", theme.FAULT))
        self.btn_stop.setToolTip("Cancel every running command and workflow (Esc)")
        self.btn_stop.clicked.connect(self.stop_all)
        lay.addSpacing(4)
        lay.addWidget(self.btn_stop)
        return f

    def _nav(self) -> QWidget:
        w = QFrame()
        w.setObjectName("rail")
        self.nav_frame = w
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 8)
        lay.setSpacing(1)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_titles: dict[str, str] = {}
        self.nav_headers = []
        self.nav_dividers: list[QFrame] = []  # stand in for the group headings while the rail is collapsed
        for gi, (group, items) in enumerate(NAV):
            if gi:
                d = QFrame()
                d.setObjectName("raildiv")
                d.setFixedHeight(1)
                d.setVisible(False)
                self.nav_dividers.append(d)
                lay.addSpacing(2)
                lay.addWidget(d)
                lay.addSpacing(2)
            h = label(group, "navgroup")
            self.nav_headers.append(h)
            lay.addWidget(h)
            for key, title, ic in items:
                b = QPushButton(title)
                b.setObjectName("nav")
                b.setCheckable(True)
                b.setIcon(icon(ic))
                b.setIconSize(QSize(18, 18))
                b.setToolTip(title)
                b.clicked.connect(lambda _=False, k=key: self.navigate(k))
                self.nav_group.addButton(b)
                self.nav_buttons[key] = b
                self.nav_titles[key] = title
                lay.addWidget(b)
        lay.addStretch(1)
        self.lbl_version = label(f"v{__version__}", "caption")  # in Help › About; not repeated in the rail
        self.lbl_version.setVisible(False)
        lay.addWidget(self.lbl_version)
        # collapse / expand the rail (operator 2026-11-23: "make this pane collapsable"); also View menu, Ctrl+B
        self.btn_rail = QPushButton()
        self.btn_rail.setObjectName("nav")
        self.btn_rail.setIconSize(QSize(18, 18))
        self.btn_rail.setAccessibleName("Collapse or expand the sidebar")
        self.btn_rail.clicked.connect(self.toggle_nav)
        lay.addWidget(self.btn_rail)
        return w

    def _footer(self) -> QWidget:
        f = QFrame()
        f.setObjectName("footer")
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 4, 14, 4)
        self.f_session = label("", "mono")
        self.f_bus = label("", "mono")
        self.f_task = label("", "mono")
        self.f_pos = label("", "mono")
        self.f_raw = label("Raw data preserved · display mapping is non-destructive", "caption")
        # UI audit 2026-11-17: the footer keeps the session and what is running; the position repeats the Capture
        # page (in another frame), and the slogan and an idle command bus are not operator information
        self.f_pos.setVisible(False)
        self.f_raw.setVisible(False)
        for x in (self.f_session, self.f_bus, self.f_task):
            lay.addWidget(x)
            lay.addSpacing(22)
        lay.addStretch(1)
        lay.addWidget(self.f_pos)
        lay.addWidget(self.f_raw)
        return f

    # ================================================================== commands & menus
    def _cmd(self, menu, key: str, text: str, run, *, shortcut: str | None = None, why=None, checked=None,
             path: str = "", group: QActionGroup | None = None) -> Cmd:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.WindowShortcut)
        if checked is not None:
            a.setCheckable(True)
        if group is not None:
            group.addAction(a)
        a.triggered.connect(lambda _c=False, k=key: self.run_command(k))
        menu.addAction(a)
        c = Cmd(key, text, run, shortcut, why, checked, path or menu.title().replace("&", ""), a)
        self.commands[key] = c
        return c

    def run_command(self, key: str) -> None:
        c = self.commands[key]
        why = c.why_disabled() if callable(c.why_disabled) else ""
        if why:
            self.model.add_log("warn", f"{c.text.replace('&', '')}: {why}")
            return
        c.run()
        self._refresh_commands()

    def _build_menus(self) -> None:
        mb = self.menuBar()
        cap = self.pages["capture"]
        busy = lambda: "the instrument is busy with " + self.model.tasks.current if self.model.tasks.busy else ""
        no_capture = lambda: "" if self.session.capture.buffers.all() else "capture an image first"

        m = mb.addMenu("&File")
        self._cmd(m, "file.open", "Open image…", lambda: self.pages["analysis"].open_file_dialog(), shortcut="Ctrl+O")
        m.addSeparator()
        self._cmd(m, "file.save", "Save image…", lambda: self.navigate("save"), shortcut="Ctrl+S", why=no_capture)
        self._cmd(m, "file.quicksave", "Quick save", lambda: self.pages["save"].quick_save(), shortcut="Ctrl+Shift+S",
                  why=no_capture)
        self._cmd(m, "file.diag", "Export diagnostics…", lambda: self.pages["service"].export_dialog())
        m.addSeparator()
        self._cmd(m, "file.settings", "Settings…", lambda: self.navigate("settings"), shortcut="Ctrl+,")
        m.addSeparator()
        self._cmd(m, "file.quit", "Quit", self.close, shortcut="Ctrl+Q")

        m = mb.addMenu("&Edit")
        self._cmd(m, "edit.copy", "Copy image", cap.copy_image, shortcut="Ctrl+C",
                  why=lambda: "" if cap.view.has_image() else "no image in the viewer")
        self._cmd(m, "edit.palette", "Search commands…", self.open_palette, shortcut="Ctrl+K")
        m.addSeparator()
        self._cmd(m, "edit.clear", "Clear all channels", lambda: self.session.capture.buffers.clear(), why=no_capture)
        self._cmd(m, "edit.undo_nominal", "Undo focus nominal", cap.undo_focus_nominal,
                  why=lambda: "" if getattr(cap, "_last_nominal", None) else "no focus nominal change to undo")

        m = mb.addMenu("&View")
        tm = m.addMenu("Theme")
        g = QActionGroup(self)
        for name, text in (("dark", "Dark"), ("light", "Light"), ("system", "Match system")):
            self._cmd(tm, f"view.theme.{name}", text, lambda n=name: self.set_theme(n), group=g,
                      checked=lambda n=name: self.session.settings.basic.theme == n, path="View › Theme")
        tm.addSeparator()  # colour theme (ADR-0063)
        gp = QActionGroup(self)
        for pid, spec in te.PRESETS.items():
            self._cmd(tm, f"view.theme.preset.{pid}", f"{spec.name} colours", lambda p=pid: self.set_theme_preset(p),
                      group=gp, checked=lambda p=pid: self.session.settings.basic.theme_preset == p,
                      path="View › Theme")
        self._cmd(tm, "view.theme.preset.custom", "Custom colours…", lambda: self.set_theme_preset("custom"),
                  group=gp, checked=lambda: self.session.settings.basic.theme_preset == "custom", path="View › Theme")
        tm.addSeparator()
        self._cmd(tm, "view.theme.high_contrast", "High contrast",
                  lambda: self.set_high_contrast(not self.session.settings.basic.high_contrast),
                  checked=lambda: self.session.settings.basic.high_contrast, path="View › Theme")
        nm = m.addMenu("Navigation")
        g2 = QActionGroup(self)
        for name, text in (("auto", "Automatic (icons below 1440 px)"), ("expanded", "Labels"), ("rail", "Icons only")):
            self._cmd(nm, f"view.nav.{name}", text, lambda n=name: self.set_nav(n), group=g2,
                      checked=lambda n=name: self.session.settings.basic.navigation == n, path="View › Navigation")
        self._cmd(m, "view.nav.toggle", "Collapse or expand sidebar", self.toggle_nav, shortcut="Ctrl+B")
        m.addSeparator()
        self._cmd(m, "view.fit", "Zoom to fit", cap.view.fit, shortcut="Ctrl+0")
        self._cmd(m, "view.actual", "Actual size", cap.view.one_to_one, shortcut="Ctrl+1")
        m.addSeparator()
        self._cmd(m, "view.cross", "Crosshair", lambda: cap.btn_cross.toggle(), checked=lambda: cap.btn_cross.isChecked())  # noqa: E501
        self._cmd(m, "view.scale", "Scale bar", lambda: cap.btn_scale.toggle(), checked=lambda: cap.btn_scale.isChecked())  # noqa: E501
        self._cmd(m, "view.clip", "Clipping overlay", lambda: cap.btn_clip.toggle(), shortcut="Ctrl+Shift+C",
                  checked=lambda: cap.btn_clip.isChecked())
        self._cmd(m, "view.sat", "Highlight saturated pixels", lambda: cap.btn_sat.toggle(),
                  checked=lambda: cap.btn_sat.isChecked())
        self._cmd(m, "view.loupe", "Loupe", lambda: cap.btn_loupe.toggle(), checked=lambda: cap.btn_loupe.isChecked())
        self._cmd(m, "view.sci", "Scientific display (linear only)", cap.toggle_scientific,
                  checked=lambda: self.session.settings.basic.scientific_display)
        m.addSeparator()
        self._cmd(m, "view.dev", "Developer tools", self._toggle_developer,
                  checked=lambda: self.session.settings.basic.developer_mode)
        self._cmd(m, "view.full", "Enter full screen", self._toggle_full, shortcut="F11")

        m = mb.addMenu("&Acquire")
        self._cmd(m, "acq.live", "Live", cap.go_live, shortcut="Ctrl+L", why=busy)
        self._cmd(m, "acq.capture", "Capture", cap.capture_current, shortcut="Ctrl+Return", why=busy)
        self._cmd(m, "acq.all", "Capture all", cap.capture_all, why=lambda: busy() or (
            "" if self.session.settings.active_camera is CameraRole.MONO else "Capture all uses the monochrome camera"))
        m.addSeparator()
        self._cmd(m, "acq.find", "Find sample", lambda: cap._autofocus(True), why=busy)
        self._cmd(m, "acq.fine", "Auto fine", lambda: cap._autofocus(False), shortcut="Ctrl+F", why=busy)
        self._cmd(m, "acq.snap", "Snap to last", cap._snap, why=lambda: busy() or (
            "" if self.session.capture.last_capture_z is not None else "no captured image yet"))
        self._cmd(m, "acq.nominal", "Capture focus nominal",
                  lambda: cap.run_action(HotButtonAction.CAPTURE_FOCUS_NOMINAL), why=busy)
        m.addSeparator()
        self._cmd(m, "acq.zstack", "Z-stack…", lambda: cap.run_action(HotButtonAction.ZSTACK), why=busy)
        self._cmd(m, "acq.transfection", "Transfection…", lambda: cap.run_action(HotButtonAction.TRANSFECTION), why=busy)  # noqa: E501
        m.addSeparator()
        self._cmd(m, "acq.scan", "Scan…", lambda: self.navigate("scan"))
        self._cmd(m, "acq.timelapse", "Time-lapse…", lambda: self.navigate("timelapse"))

        m = mb.addMenu("&Microscope")
        om = m.addMenu("Objective")
        go = QActionGroup(self)
        for o in self.session.profile.objectives:
            self._cmd(om, f"mic.obj.{o.slot}", f"{o.slot}  {o.label or 'Empty'}",
                      lambda s=o.slot: cap.obj_group.button(s).click(), shortcut=f"Alt+{o.slot}", group=go,
                      checked=lambda s=o.slot: self.session.controller.objective_slot() == s,
                      why=lambda o=o: busy() or ("" if o.installed else "no objective in this slot"),
                      path="Microscope › Objective")
        chm = m.addMenu("Channel")
        gc = QActionGroup(self)
        for c in self.session.profile.channels:
            if not c.installed:
                continue
            self._cmd(chm, f"mic.ch.{c.slot}", c.display, lambda s=c.slot: cap._select_channel(s), group=gc,
                      checked=lambda s=c.slot: cap.channel == s,
                      why=lambda c=c: busy() or self._channel_reason(c), path="Microscope › Channel")
        cm = m.addMenu("Camera")
        gcam = QActionGroup(self)
        for cam in self.session.profile.cameras:
            self._cmd(cm, f"mic.cam.{cam.role.value}", cam.display_name, lambda r=cam.role: cap.switch_camera(r),
                      group=gcam, checked=lambda r=cam.role: self.session.settings.active_camera is r, why=busy,
                      path="Microscope › Camera")
        self._cmd(m, "mic.light", "Light on/off", lambda: cap.btn_power.toggle(), shortcut="Ctrl+Shift+L",
                  checked=lambda: cap.btn_power.isChecked(), why=busy)
        m.addSeparator()
        self._cmd(m, "mic.vessel", "Vessel…", cap._vessel_expert, why=busy)
        self._cmd(m, "mic.lightcam", "Light and camera…", cap._light_camera, why=busy)
        m.addSeparator()
        self._cmd(m, "mic.release", "Release stage lock…", self.release_stage_lock, why=self._release_why)
        self._cmd(m, "mic.rehome", "Re-home stage…", lambda: self.rehome_stage(), why=self._rehome_why)
        self._cmd(m, "mic.precheck", "System precheck…", self.open_precheck)
        self._cmd(m, "mic.heights", "Objective safe heights…", self.open_safe_heights, why=busy)
        self._cmd(m, "mic.recover", "Recover device…", lambda: self.navigate("hardware"))
        self._cmd(m, "mic.stop", "Stop all", self.stop_all, shortcut="Esc")

        m = mb.addMenu("A&nalyze")
        an = self.pages["analysis"]
        for i, (k, t) in enumerate((("overlay", "Overlay"), ("measure", "Measure"), ("count", "Count"),
                                    ("auto", "Auto count"))):
            self._cmd(m, f"an.{k}", t, lambda i=i: (self.navigate("analysis"), an.tabs.setCurrentIndex(i)),
                      why=no_capture if k != "count" else None)
        m.addSeparator()
        self._cmd(m, "an.file", "Count from file…", an.open_file_dialog)

        m = mb.addMenu("&Window")
        n = 0
        for _g, items in NAV:
            for key, title, _ic in items:
                n += 1
                self._cmd(m, f"win.{key}", title, lambda k=key: self.navigate(k),
                          shortcut=f"Ctrl+Alt+{n}" if n <= 9 else None, path="Window")
            m.addSeparator()

        m = mb.addMenu("&Help")
        self._cmd(m, "help.start", "Getting started…", self.show_getting_started)
        self._cmd(m, "help.keys", "Keyboard shortcuts…", self.show_shortcuts)
        self._cmd(m, "help.palette", "Search commands…", self.open_palette, shortcut="Alt+Q")
        m.addSeparator()
        self._cmd(m, "help.about", "About OpenEVOS…", self.show_about)

        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 6, 0)
        cl.setSpacing(6)
        search = QPushButton("Search  Ctrl+K")
        search.setObjectName("menu")
        search.setIcon(icon("search"))
        search.clicked.connect(self.open_palette)
        cl.addWidget(search)
        self.mode_badge = Badge(self.mode.badge, self.mode.state)
        self.mode_badge.setToolTip(self.mode.tooltip)
        cl.addWidget(self.mode_badge)
        self._corner = corner  # PySide does not transfer ownership of corner widgets: keep a reference
        mb.setCornerWidget(corner, Qt.TopRightCorner)
        for menu in mb.findChildren(QMenu):
            menu.aboutToShow.connect(self._refresh_commands)
        self._refresh_commands()

    def _channel_reason(self, c) -> str:
        cam = self.session.profile.camera(self.session.settings.active_camera)
        return "" if cam and cam.supports(c) else "the colour camera is brightfield/phase only"

    def _refresh_commands(self) -> None:
        for c in self.commands.values():
            if c.action is None:
                continue
            why = c.why_disabled() if callable(c.why_disabled) else ""
            c.action.setEnabled(not why or c.key == "mic.stop")
            c.action.setToolTip(why or c.text.replace("&", ""))
            c.action.setStatusTip(why)
            if callable(c.checked):
                c.action.setChecked(bool(c.checked()))

    def open_palette(self) -> None:
        self._refresh_commands()
        CommandPalette(self).exec()

    # ================================================================== theme / navigation / chrome actions
    # ================================================================== system precheck
    def start_precheck(self, timeout_s: float = 10.0) -> None:
        """Start-up precheck (app.py): wait until every mapped device has reported (or `timeout_s`), give the live
        view a moment, then run it once. It opens the precheck dialog only when something needs attention; a clean
        pass is one log line."""
        from openevos.acquisition import precheck as pc

        self._pc_waited = 0.0
        self._pc_timer = QTimer(self)
        self._pc_timer.setInterval(250)

        def poll():
            self._pc_waited += 0.25
            if not pc.ready(self.session) and self._pc_waited < timeout_s:
                return
            self._pc_timer.stop()
            QTimer.singleShot(1500, self._precheck_once)  # live frames start after the devices

        self._pc_timer.timeout.connect(poll)
        self._pc_timer.start()

    def _precheck_once(self) -> None:
        from openevos.acquisition import precheck as pc

        checks = pc.run_precheck(self.session)
        w = pc.worst(checks)
        self.model.add_log("info" if w in ("ok", "info") else "warn", pc.summary(checks))
        for c in checks:
            if c.status in ("fault", "warn"):
                self.model.add_log("error" if c.status == "fault" else "warn", f"Precheck · {c.title}: {c.detail}")
        if w in ("fault", "warn"):
            self.open_precheck()

    # ================================================================== stage lock release (ADR-0057)
    def _locked_monitors(self):
        return [s for s in getattr(self.session, "telemetry", []) or []
                if getattr(s, "monitor", None) is not None and s.monitor.latched]

    def _release_why(self) -> str:
        locked = self._locked_monitors()
        if not locked:
            return "the stage is not locked"
        return locked[0].monitor._release_blocker()

    def release_stage_lock(self) -> None:
        """Operator release of a small drift lock without a power cycle: confirm, then fresh X/Y reads decide."""
        from PySide6.QtWidgets import QApplication, QMessageBox

        locked = self._locked_monitors()
        if not locked:
            self.model.add_log("info", "Release stage lock: the stage is not locked")
            return
        svc = locked[0]
        st, drift = svc.monitor.state(), svc.monitor.latch_drift()
        blocker = svc.monitor._release_blocker()
        if blocker:  # not a small drift: offer the software re-home (ADR-0058) instead of a dead end
            self.rehome_stage(reason=f"{st.get('message', '')} (it cannot be released: {blocker})")
            return
        landing = st.get("rule") == "landing_error"  # ADR-0061: a small landing miss is releasable too
        after = ("The next move again gets the settle time and one automatic re-approach before it can lock."
                 if landing else "The monitor keeps watching: if the stage creeps again it locks again.")
        text = (f"The drift monitor locked the stage: {st.get('message', '')}.\n\n"
                f"Recorded {'landing miss' if landing else 'drift'}: {drift:+d} counts "
                f"(about {abs(drift) * 0.44:.1f} µm; the commutation risk starts near 14 counts).\n\n"
                "OpenEVOS reads X and Y now and releases the lock only if both are homed, idle, free of drive faults, "
                f"powered and within 3 counts of their command. {after} Release the lock?"
                ) if drift is not None else st.get("message", "")
        if QMessageBox.question(self, "Release stage lock", text) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = svc.release_latch("operator (Microscope › Release stage lock)")
        finally:
            QApplication.restoreOverrideCursor()
        self.model.add_log("info" if ok else "warn", f"Release stage lock: {msg}")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Release stage lock", msg)
        dlg = getattr(self, "_precheck_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.refresh()

    # ================================================================== software re-home (ADR-0058)
    def _rehome_service(self):
        return next((s for s in getattr(self.session, "telemetry", []) or [] if hasattr(s, "rehome_stage")), None)

    def _rehome_why(self) -> str:
        svc = self._rehome_service()
        return "no physical stage in this session" if svc is None else svc.rehome_blocker()

    def rehome_stage(self, reason: str = "") -> None:
        """Re-home X and Y from software during a stage lock, as a power cycle would, then return to the position
        before (ADR-0058; operator 2026-11-19: "re home (as in powercycle) during stage lock (with a notification
        about stage lock rehoming), and then reach to the old position"). Confirm with the lock reason, start the
        service's job, report its progress in the log and the result in a box; the UI is not blocked meanwhile."""
        from PySide6.QtWidgets import QMessageBox

        svc = self._rehome_service()
        why = self._rehome_why()
        if why:
            QMessageBox.information(self, "Re-home stage", f"The stage cannot be re-homed from the app now: {why}.")
            return
        if not reason:
            st = svc.monitor.state()
            reason = st.get("message", "") or "operator request"
        ctl = self.session.controller
        xy, z = ctl.stage_xy(), ctl.focus_z()
        back = f"X {xy[0] / 1000:.2f}, Y {xy[1] / 1000:.2f} mm" if xy else "where it was"
        lower = z is not None and abs(z) > REHOME_Z_LOWERED_UM
        first = (f"First the objective is lowered fully (Z {z:.0f} µm → 0), since the stage travels to its home "
                 f"corner; it goes back to Z {z:.0f} µm after the stage is back. " if lower else "")
        text = (f"Stage locked: {reason}.\n\nRe-home the stage now? {first}X, then Y, each drive runs its own "
                "power-up homing: it drives to its hard stop and finds its index, as after switching the microscope on "
                f"(about 10–20 s per axis). The lock clears when both are seen homed, and the stage then returns to "
                f"{back} (X, then Y).\n\nKeep hands clear of the stage. Stop cannot interrupt an axis while it homes; "
                "the microscope's power switch can. A drive fault that survives homing still needs a power cycle.")
        if QMessageBox.question(self, "Re-home stage", text) != QMessageBox.StandardButton.Yes:
            return
        if not lower:
            self._start_rehome(svc, reason, back, None)
            return
        # operator 2026-11-19 ("cant [you] do this auto"): Re-home was refused with Z up; lower it here, through the
        # router (the focus device's own checks apply), then start the re-home once Z is verified at 0
        self.model.add_log("info", f"Re-home stage: lowering the objective from Z {z:.0f} µm to 0 first")
        ticket = ctl.move_focus(0.0, timeout_s=REHOME_Z_TIMEOUT_S)
        timer = QTimer(self)
        timer.setInterval(100)

        def tick() -> None:
            status = ticket.status
            if not status.is_terminal:
                return
            timer.stop()
            timer.deleteLater()
            if status is not CommandStatus.SUCCEEDED:
                msg = (f"not started: lowering the objective to Z 0 ended {status.value}"
                       f"{': ' + ticket.record.message if ticket.record.message else ''}; nothing was sent to X or Y")
                self.model.add_log("warn", f"Re-home stage: {msg}")
                QMessageBox.warning(self, "Re-home stage", msg[0].upper() + msg[1:])
                return
            self._start_rehome(svc, reason, back, z)

        timer.timeout.connect(tick)
        timer.start()
        self._rehome_z_timer = timer  # held until Z has landed

    def _start_rehome(self, svc, reason: str, back: str, z_restore: float | None) -> None:
        from PySide6.QtWidgets import QMessageBox

        job = svc.rehome_stage("operator (Re-home stage)")
        if isinstance(job, str):
            self.model.add_log("warn", f"Re-home stage: not started: {job}")
            QMessageBox.warning(self, "Re-home stage", f"Not started: {job}")
            return
        self.model.add_log("warn", f"Re-home stage: started ({reason}); the stage returns to {back} after homing")
        self._watch_rehome(job, z_restore)

    def _watch_rehome(self, job, z_restore: float | None = None) -> None:
        from PySide6.QtWidgets import QMessageBox

        last: dict[str, str | None] = {"stage": None}
        timer = QTimer(self)
        timer.setInterval(250)

        def tick() -> None:
            stage = getattr(job, "stage", "")
            if stage != last["stage"]:
                last["stage"] = stage
                if stage:
                    self.model.add_log("info", f"Re-home stage: {stage}")
            if not job.done.is_set():
                return
            timer.stop()
            timer.deleteLater()
            ok, msg = bool(getattr(job, "ok", False)), getattr(job, "message", "") or "finished"
            self.model.add_log("info" if ok else "warn", f"Re-home stage: {msg}")
            if ok and z_restore is not None:
                # Z back to the height it had before Re-home lowered it; the focus device's safe-height check applies
                self.model.add_log("info", f"Re-home stage: raising the objective back to Z {z_restore:.0f} µm")
                self.session.controller.move_focus(z_restore)
                msg += f"\n\nThe objective goes back to Z {z_restore:.0f} µm."
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Re-home stage", msg)
            dlg = getattr(self, "_precheck_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.refresh()

        timer.timeout.connect(tick)
        timer.start()
        self._rehome_timer = timer  # held for the life of the job

    def open_precheck(self):
        from openevos.ui.precheck_dialog import PrecheckDialog

        dlg = getattr(self, "_precheck_dlg", None)
        if dlg is None:
            dlg = self._precheck_dlg = PrecheckDialog(self)
        else:
            dlg.refresh()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def open_safe_heights(self):
        from openevos.ui.precheck_dialog import SafeHeightsDialog

        dlg = getattr(self, "_heights_dlg", None)
        if dlg is None or not dlg.isVisible():
            dlg = self._heights_dlg = SafeHeightsDialog(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def set_theme(self, name: str) -> None:
        self.session.settings.basic.theme = name
        self._apply_theme_settings()

    def set_theme_preset(self, preset: str) -> None:
        """Colour theme: a preset id, or "custom" for the saved custom theme (ADR-0063)."""
        if preset == "custom" and not self.session.settings.basic.theme_custom:
            # no custom theme yet: open the colour picker rather than refuse in the log (operator 2026-11-24:
            # "custom theme not working")
            picker = getattr(self.pages.get("settings"), "theme_picker", None)
            if picker is not None:
                picker.pick_colour()
            return
        self.session.settings.basic.theme_preset = preset
        self._apply_theme_settings()

    def set_custom_theme(self, spec) -> None:
        """Save `spec` (theme_engine.ThemeSpec) as the custom theme and switch to it."""
        b = self.session.settings.basic
        b.theme_custom = spec.model_dump(exclude_none=True)
        b.theme_preset = "custom"
        self._apply_theme_settings()

    def set_high_contrast(self, on: bool) -> None:
        self.session.settings.basic.high_contrast = bool(on)
        self._apply_theme_settings()

    def _apply_theme_settings(self):
        built = theme.apply_settings(self.session.settings.basic, QApplication.instance())
        for w in built.warnings:
            self.model.add_log("warn", f"Theme: {w}")
        self._retint()
        self._refresh_commands()
        self.model.settings_changed.emit()
        return built

    def _retint(self) -> None:
        for key, b in self.nav_buttons.items():
            ic = next(i for _g, items in NAV for k, _t, i in items if k == key)
            b.setIcon(icon(ic))
        self._show_rail_button()
        self.btn_stop.setIcon(icon("stop", theme.FAULT))
        for p in self.pages.values():
            fn = getattr(p, "retint", None)
            if fn:
                fn()

    def set_nav(self, mode: str) -> None:
        self.session.settings.basic.navigation = mode
        self._apply_nav_mode()

    def nav_collapsed(self) -> bool:
        mode = self.session.settings.basic.navigation
        return mode == "rail" or (mode == "auto" and self.width() < RAIL_BELOW_PX)

    def toggle_nav(self) -> None:
        """The rail's own button and Ctrl+B: flip what is shown now, and keep it (Settings › navigation)."""
        self.set_nav("expanded" if self.nav_collapsed() else "rail")

    def _show_rail_button(self) -> None:
        rail = self.nav_collapsed()
        self.btn_rail.setIcon(icon("sidebar-expand" if rail else "sidebar-collapse"))
        self.btn_rail.setText("" if rail else "Collapse")
        self.btn_rail.setToolTip(("Expand" if rail else "Collapse") + " the sidebar (Ctrl+B)")

    def _apply_nav_mode(self) -> None:
        rail = self.nav_collapsed()
        for key, b in self.nav_buttons.items():
            b.setText("" if rail else self.nav_titles[key])
        for h in self.nav_headers:
            h.setVisible(not rail)
        for d in self.nav_dividers:
            d.setVisible(rail)
        self._show_rail_button()
        self.lbl_version.setVisible(False)
        self.nav_frame.setFixedWidth(52 if rail else 172)
        self.lbl_profile.setVisible(self.width() >= 1500)  # labels drop first, nothing hides
        for p in self.pages.values():
            fn = getattr(p, "set_compact", None)
            if fn:
                fn(self.width() < RAIL_BELOW_PX)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "nav_buttons"):
            self._apply_nav_mode()

    def _toggle_developer(self) -> None:
        """View › Developer tools: simulator fault injection and gate evidence (hidden from operators by default)."""
        on = not self.session.settings.basic.developer_mode
        self.session.settings.basic.developer_mode = on
        for p in self.pages.values():
            fn = getattr(p, "set_developer", None)
            if fn:
                fn(on)

    def _toggle_full(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def stop_all(self) -> None:
        n = self.model.stop_all()
        self.model.add_log("warn", f"Stop all: {n} command(s) asked to cancel; workflows stopped")

    def show_getting_started(self) -> None:
        from openevos.ui.dialogs import GettingStartedDialog

        GettingStartedDialog(self).exec()

    def show_shortcuts(self) -> None:
        from openevos.ui.dialogs import InfoDialog

        rows = [(c.menu_path, c.text.replace("&", ""), c.shortcut) for c in self.commands.values() if c.shortcut]
        text = "\n".join(f"{s:<16} {p} › {t}" for p, t, s in sorted(rows, key=lambda r: r[0]))
        text += "\n\nSliders and number boxes: arrows step · Shift ×10 · Alt ×0.1 · double-click label resets"
        text += "\nImage: wheel zooms · drag pans · hold B compares raw · Ctrl+0 fit · Ctrl+1 actual size"
        InfoDialog("Keyboard shortcuts", text, self, mono=True).exec()

    def show_about(self) -> None:
        from openevos.ui.dialogs import InfoDialog

        InfoDialog("About OpenEVOS", f"OpenEVOS {__version__}\nOpen-source diagnostics and control for the EVOS FL "
                   f"Auto (AMAFD1000).\n{self.mode.about}\n\nInterface design "
                   "system: Craftimon Lab tokens (Inter, JetBrains Mono — SIL OFL 1.1).", self).exec()

    # ================================================================== status
    def _tick(self) -> None:
        s, m = self.session, self.model
        self.f_session.setText(f"SESSION {self.mode.footer} · {s.vessel.display_name}")
        n = len(m.active_commands)
        self.f_bus.setText(f"COMMAND BUS: {'IDLE' if n == 0 else f'{n} ACTIVE'}")
        self.f_bus.setStyleSheet(f"color: {theme.OK if n == 0 else theme.ACCENT_TX};")
        self.f_bus.setVisible(n > 0)  # shown only while commands run
        self.f_task.setText(f"TASK: {m.tasks.current or 'none'}")
        self.f_task.setVisible(bool(m.tasks.current))
        xy, z, slot = s.controller.stage_xy(), s.controller.focus_z(), s.controller.objective_slot()
        obj = s.profile.objective(slot) if slot else None
        self.f_pos.setText((f"XY {xy[0] / 1000:+.3f}, {xy[1] / 1000:+.3f} mm" if xy else "XY —")
                           + (f"   Z {z:.1f} µm" if z is not None else "") + (f"   {obj.short}" if obj else ""))
        self._refresh_commands()
        self._maybe_auto_recover()

    # ================================================================== automatic stage recovery (ADR-0067)
    def _maybe_auto_recover(self) -> None:
        """A drive trip outside a run (a manual move, or found at rest): start the automatic stage recovery as a task
        when it is allowed and nothing else runs. A Multi-D or Scan run handles its own trip."""
        if self.model.tasks.busy:
            return
        rec = getattr(self, "_auto_recovery", None)
        if rec is None:
            from openevos.acquisition.stage_recovery import StageAutoRecovery

            rec = self._auto_recovery = StageAutoRecovery(self.session, log=self.model.add_log)
        if rec.blocker():
            return
        self.model.tasks.submit("Stage recovery", lambda: rec.run("a manual stage move or at rest"),
                                on_done=self._auto_recovery_done)

    def _auto_recovery_done(self, out) -> None:
        from PySide6.QtWidgets import QMessageBox

        if out is None or not getattr(out, "attempted", False):
            return
        if out.recovered:
            QMessageBox.information(self, "Stage recovered", out.message)
        else:
            QMessageBox.warning(self, "Stage locked", out.message)
        dlg = getattr(self, "_precheck_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.refresh()

    def _on_device(self, did: str, snap) -> None:
        pill = self.pills.get(did)
        if pill is not None:
            state, text = describe_snapshot(snap)
            tip = f"{did}: {snap.note or ''}" if snap else did
            if did == self.session.profile.stage_device and self._locked_monitors():
                tip += "\n\nStage locked by the drift monitor: click to release or re-home it"
            pill.set_state(state, text, tip)
            self.health.refresh()

    def _stage_pill_clicked(self) -> None:
        if self._locked_monitors():
            self.health.popup.hide()
            self.release_stage_lock()

    def _on_command_event(self, e) -> None:
        """A stage move refused by the drift lock: offer the release once per lock (operator, 2026-11-18)."""
        msg = getattr(e, "message", "") or ""
        if getattr(e, "device_id", None) != self.session.profile.stage_device or "locked by the drift monitor" not in msg:  # noqa: E501
            return
        locked = self._locked_monitors()
        if not locked:
            return
        key = locked[0].monitor.state().get("since_utc")
        if key and key != getattr(self, "_lock_prompted", None):
            self._lock_prompted = key
            QTimer.singleShot(0, self.release_stage_lock)

    # ================================================================== pages
    def _build_pages(self) -> None:
        from openevos.ui.pages import capture, save
        from openevos.ui.pages.others import build_other_pages

        cap = capture.CapturePage(self.model)
        self._add("capture", cap)
        sv = save.SavePage(self.model, cap)
        self._add("save", sv)
        cap.request_page.connect(self._page_request)
        for key, page in build_other_pages(self.model, cap).items():
            self._add(key, page)
            if hasattr(page, "request_page"):  # e.g. Time-lapse opens one of its Z-stacks on the Z-stack page
                page.request_page.connect(self._page_request)
        from openevos.ui.pages.diagnostics import DiagnosticsPage

        self._add("diagnostics", DiagnosticsPage(self.model))  # Inspector scans (ported from the legacy UI)
        from PySide6.QtWidgets import QTabWidget

        self.service_tabs = QTabWidget()
        self.service_tabs.setDocumentMode(True)
        for key, title in SERVICE_TABS:
            self.service_tabs.addTab(self._service_holders[key], title)
        self.stack.addWidget(self.service_tabs)
        self.pages["service_area"] = self.service_tabs

    def _add(self, key: str, page: QWidget) -> None:
        if key in dict(SERVICE_TABS):  # goes into the Service area's tabs (built after all pages)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(page)
            self._service_holders = getattr(self, "_service_holders", {})
            self._service_holders[key] = sc
            self.pages[key] = page
            return
        if key != "capture":
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(page)
            self.stack.addWidget(sc)
        else:
            self.stack.addWidget(page)
        self.pages[key] = page

    def _page_request(self, what: str) -> None:
        if what == "quick_save":
            self.pages["save"].quick_save()
        else:
            self.navigate(what)

    def navigate(self, key: str) -> None:
        if key in dict(SERVICE_TABS):
            self.service_tabs.setCurrentIndex([k for k, _t in SERVICE_TABS].index(key))
            key = "service_area"
        page = self.pages.get(key)
        if page is None:
            return
        idx = next(i for i in range(self.stack.count())
                   if self.stack.widget(i) is page or getattr(self.stack.widget(i), "widget", lambda: None)() is page)
        self.stack.setCurrentIndex(idx)
        self.nav_buttons[key].setChecked(True)

    # ================================================================== settings persistence
    def _settings_snapshot(self) -> str:
        return self.session.settings.model_dump_json()

    def autosave_settings(self) -> bool:
        """Write the settings file when the settings changed since the last write: every SETTINGS_AUTOSAVE_MS, and
        first thing on close. True when the file holds the current settings (or there is no settings file)."""
        now = self._settings_snapshot()
        if now == self._settings_saved:
            return True
        try:
            self.session.save_settings()
        except OSError as exc:
            if not self._autosave_failed:  # once per failure run; the timer keeps retrying
                self.model.add_log("warn", f"Settings not saved yet ({exc}); retrying")
            self._autosave_failed = True
            return False
        self._autosave_failed = False
        self._settings_saved = now
        return True

    def closeEvent(self, e):
        self.autosave_settings()  # before the hardware shutdown: a hang or crash there must not lose them
        self.model.shutdown()
        self.session.save_settings()
        super().closeEvent(e)
