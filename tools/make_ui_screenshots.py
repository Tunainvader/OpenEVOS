"""Render documentation screenshots of every workspace with representative (simulated) content.

    QT_QPA_PLATFORM=offscreen python tools/make_ui_screenshots.py docs/ui/screenshots

All images come from the optics simulator and are labelled SIMULATED in the UI. Produces dark-theme
captures of every workspace plus light-theme and narrow-window variants of Capture.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def main(out: Path) -> int:
    from PySide6.QtWidgets import QApplication

    from openevos.acquisition.session import build_sim_session
    from openevos.analysis.annotations import AnnKind
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import AppSettings
    from openevos.ui import theme
    from openevos.ui.screenshots import pump
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    theme.load_fonts(app)
    theme.apply_theme("dark", app)

    def idle(m, timeout=120.0):
        end = time.monotonic() + timeout
        pump(app, 0.3)
        while m.tasks.busy and time.monotonic() < end:
            pump(app, 0.1)
        pump(app, 0.4)

    s = build_sim_session(ThreadClock(), settings=AppSettings(), fps=12)
    m = SessionModel(s)
    w = MainWindow(m)
    w.resize(1680, 1000)
    w.show()
    pump(app, 2.0)
    out.mkdir(parents=True, exist_ok=True)
    callouts: dict[str, list] = {}

    def shot(name: str, marks: list | None = None) -> None:
        w.grab().save(str(out / f"{name}.png"))
        if marks:
            callouts[name] = [(label, geom(wd)) for label, wd in marks if wd is not None and wd.isVisible()]

    def geom(wd):
        from PySide6.QtCore import QPoint

        p = wd.mapTo(w, QPoint(0, 0))
        return [p.x(), p.y(), wd.width(), wd.height()]
    cap = w.pages["capture"]
    cap.obj_buttons[2].click()
    pump(app, 1.5)
    cap._select_channel("cube1")
    pump(app, 1.0)
    cap._autofocus(False)
    idle(m)
    for ch in ("cube1", "cube2", "cube3", "trans"):
        cap._select_channel(ch)
        pump(app, 1.0)
        cap.capture_current()
        idle(m)
    cap._select_channel("cube2")
    cap.go_live()
    pump(app, 2.0)
    shot("01_capture_live", [
        ("Menu strip: File · Edit · View · Acquire · Microscope · Analyze · Window · Help", w.menuBar()),
        ("Search commands (Ctrl+K) and backend badge", w.mode_badge),
        ("Device status pills: colour + text, shape differs for faults", w.pills[s.profile.stage_device]),
        ("Stop all (Esc): cancels every command and workflow", w.btn_stop),
        ("Navigation rail: icons + labels, labels drop below 1440 px", w.nav_frame),
        ("Camera, objective, vessel readouts + SIMULATED badge", cap.badge_frames),
        ("Vessel expert…", cap.btn_vessel),
        ("Virtual vessel: click a well to centre, drag crosshair, FOV box, reachable area", cap.map),
        ("Medium jog joystick + speed", cap.joy),
        ("Objective buttons (empty slot disabled)", cap.obj_buttons[3]),
        ("Viewer toolbar: Live, Overlay, Fit, 1:1, Crosshair, Scale bar, Clipping, Saturation, Loupe, Compare",
         cap.toolbar),
        ("Image viewport (always dark) with badge: channel • LIVE/CAPTURED • SIMULATED", cap.view),
        ("Levels histogram: drag black/white points, double-click = auto; clipped % as text", cap.hist),
        ("Scientific display (linear only) + gamma (disabled) + display log", cap.chk_sci),
        ("Channel buttons with buffer thumbnails; colour camera greys fluorescence", cap.ch_buttons["cube1"]),
        ("Light on/off and Light and camera… dialog", cap.btn_power),
        ("Light row: slider + number + ↺ reset (Shift ×10, Alt ×0.1, double-click resets)", cap.light),
        ("Actual mode: LED / exposure / gain rows", cap.chk_actual),
        ("Focus column: Quick (LAST marker), Coarse, Fine", cap.quick),
        ("Snap to last · Find sample · Auto fine", cap.btn_find),
        ("Capture (primary) + four hot buttons", cap.btn_capture),
        ("Footer: session · command bus · task · XY/Z/objective · raw-data note", w.f_bus),
    ])
    cap.show_overlay()
    pump(app, 0.5)
    shot("02_capture_overlay")
    cap.btn_clip.setChecked(True)
    cap.show_captured(s.capture.buffers.get("cube2"))
    pump(app, 0.4)
    shot("03_capture_clipping")
    cap.btn_clip.setChecked(False)

    zs = w.pages["zstack"]
    w.navigate("zstack")
    zs._acquire()
    idle(m)
    zs.slice.setValue(5)
    pump(app, 0.3)
    shot("06_zstack")

    an = w.pages["analysis"]
    w.navigate("analysis")
    pump(app, 0.3)
    an.source.setCurrentIndex(an.source.findData("cube1"))
    an.tabs.setCurrentIndex(1)
    an.tool_buttons[AnnKind.ELLIPSE].setChecked(True)
    an._drag(220, 150, 330, 240)
    an.tool_buttons[AnnKind.LINE].setChecked(True)
    an._drag(80, 300, 520, 330)
    an.ann_table.selectRow(1)
    pump(app, 0.4)
    shot("08_analysis_measure", [
        ("Image source + Count from file…", an.source),
        ("Annotations drawn on the image with live measurement", an.view),
        ("Tool tabs: Overlay · Measure · Count · Auto count", an.tabs.tabBar()),
        ("Annotation tools: text, arrow, line, rectangle, ellipse, histogram ROI (≤ 12 ROIs)",
         an.tool_buttons[AnnKind.TEXT]),
        ("Annotation list: name, type, measurement", an.ann_table),
        ("Show annotations · units · Export all · Delete", an.show_ann),
        ("Intensity profile / ROI histogram plot (series also dashed: never colour alone)", an.plot),
        ("Per-channel statistics for the selected annotation", an.stats_table),
    ])
    an.tabs.setCurrentIndex(3)
    an._auto()
    pump(app, 0.4)
    shot("09_analysis_autocount")

    sp = w.pages["scan"]
    sp.map.selected = {"D7"}
    sp.rect_on.setChecked(True)
    for box, v in ((sp.rx, 2.0), (sp.ry, 2.4), (sp.rw, 2.4), (sp.rh, 1.6)):
        box.setValue(v)
    sp.coverage.setCurrentIndex(sp.coverage.findData("full"))
    sp.fine.setChecked(False)
    sp.replan()
    w.navigate("scan")
    sp._run()
    idle(m, 240)
    shot("04_scan", [
        ("Wizard order on one page: 1 name/vessel/optics", sp.name),
        ("2 Area: rectangle per well or two corner beacons", sp.m_rect),
        ("3 Focus: Find sample / Auto fine", sp.find),
        ("4 Coverage: six options from manual p.24 + overlap", sp.coverage),
        ("Stored routines: save / recall / delete", sp.stored),
        ("Selectable vessel map with tile overlay", sp.map),
        ("Plan summary + warnings (nominal geometry, limits)", sp.lbl_plan),
        ("Start / Pause / Stop + progress", sp.btn_run),
        ("Result mosaic, channel, export quality, Export mosaic… (OME-TIFF / PNG)", sp.mosaic),
    ])
    tl = w.pages["timelapse"]
    tl._populate()
    w.navigate("timelapse")
    pump(app, 0.5)
    shot("05_timelapse")

    cal = w.pages["calibration"]
    w.navigate("calibration")
    cap._select_channel("cube1")
    pump(app, 0.8)
    for key in ("pixel_size", "hot_pixels"):
        cal.run(key)
        idle(m)
    shot("15_calibration", [
        ("Every calibration: manual page, last run, result vs nominal", cal.table),
        ("Run buttons — simulator only; locked on hardware until gates pass", cal.buttons["pixel_size"]),
        ("Hot pixel correction toggle · Clear all calibrations", cal.chk_hot),
        ("Capture focus nominal · Auto fine range · vessel alignment state", cal.rng),
    ])
    import json as _json

    cmds = [{"key": c.key, "text": c.text.replace("&", ""), "menu": c.menu_path, "shortcut": c.shortcut,
             "toggle": c.checked is not None} for c in w.commands.values()]
    (out / "commands.json").write_text(_json.dumps(cmds, indent=2), "utf-8")

    for name, key in (("10_save", "save"), ("11_review", "review"), ("12_hardware", "hardware"),
                      ("13_environment", "environment"), ("16_settings", "settings"), ("17_service", "service"),
                      ("18_events", "events"), ("07_movies", "movies")):
        w.navigate(key)
        pump(app, 0.8)
        shot(name)

    w.navigate("capture")
    cap.go_live()
    w.set_theme("light")
    pump(app, 1.5)
    shot("20_capture_light")
    w.navigate("analysis")
    an.tabs.setCurrentIndex(1)
    pump(app, 0.6)
    shot("21_analysis_light")
    w.set_theme("dark")
    w.navigate("capture")
    for _ in range(3):
        w.resize(1200, 820)
        pump(app, 0.5)
    pump(app, 1.0)
    shot("22_capture_narrow", [
        ("Rail: icons only, tooltips keep the names", w.nav_frame),
        ("Viewer toolbar: icons only (labels drop first)", cap.toolbar),
        ("Panels narrow to their minimum; nothing is hidden", cap.right),
    ])
    import json as _json2

    (out / "callouts.json").write_text(_json2.dumps(callouts, indent=2), "utf-8")
    w.close()
    s.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ui/screenshots")))
