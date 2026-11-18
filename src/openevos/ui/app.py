"""`openevos-ui` entry point: ThreadClock core + optics simulator + PySide6 shell.

    openevos-ui                 # simulation backend (the only backend available today)
    openevos-ui --screenshot-dir out/   # render each workspace offscreen and exit (docs/CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openevos-ui", description="OpenEVOS desktop UI (simulation backend)")
    ap.add_argument("--settings", type=Path, default=None, help="settings JSON (default: user config dir)")
    ap.add_argument("--no-settings", action="store_true", help="do not load or save settings")
    ap.add_argument("--screenshot-dir", type=Path, default=None, help="save a PNG of each workspace and exit")
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--theme", choices=("dark", "light", "system"), default=None, help="override the saved theme")
    ap.add_argument("--motor-telemetry", default=None, metavar="PORT",
                    help="add the real motor drives, READ-ONLY: COM3, 'auto' (the single FTDI 0403:6001 port) or "
                         "'sim' (simulated drive chain). Optics stay simulated (ADR-0023).")
    ap.add_argument("--motor-control", action="store_true",
                    help="with --motor-telemetry: the Capture joystick, vessel map and Stop act on the REAL X/Y stage, "
                         "inside the drives' power-up box and through the safety checks (ADR-0024)")
    ap.add_argument("--scans-dir", type=Path, default=Path.cwd() / "openevos-scans",
                    help="Inspector scan folder listed on the Diagnostics page (default ./openevos-scans)")
    ap.add_argument("--calibration", type=Path, default=None,
                    help="parsed calibration JSON for the physical stage's travel box (ADR-0029; default: newest "
                         "led-application-stepd-*-parsed.json in --scans-dir). Without it the box is 0-22 mm.")
    ap.add_argument("--lock-axis", action="append", choices=("X", "Y", "x", "y"), default=[],
                    help="refuse every move on this stage axis for the session (repeatable), e.g. --lock-axis Y "
                         "while Y's hardware fault is investigated (ADR-0030)")
    ap.add_argument("--stage-log", action="store_true",
                    help="with --motor-telemetry on a real port: also record every X/Y encoder sample (~20 Hz) to "
                         "<scans-dir>/stage-monitor/stage-log-<time>.csv, in the y_drift_correlate.py format "
                         "(ADR-0040). The drift monitor's latch and its evidence are always kept there.")
    ap.add_argument("--speed-control", action="store_true",
                    help="with --motor-control: the Capture Speed box sets the real X/Y speed (Slow 1, Medium 4, Fast "
                         "8.3 mm/s; RAM only, power-up speed restored on exit). Only after Motor Gate 10 (ADR-0032)")
    ap.add_argument("--mono-camera", choices=("auto", "flycapture2", "sim"), default="auto",
                    help="mono camera: 'auto' (default) = the REAL Chameleon when exactly one 1E10:2005 with a working "
                         "driver and the FlyCapture2 SDK are present, else the simulator; 'flycapture2' = real only; "
                         "'sim' = simulator. Real: 16-bit full sensor, manual exposure/gain, RAM-only settings "
                         "restored on exit (ADR-0037)")
    ap.add_argument("--colour-camera", "--color-camera", dest="colour_camera", choices=("auto", "ueye", "sim"),
                    default="auto",
                    help="colour camera: 'auto' (default) = the REAL uEye UI-146xLE when exactly one 1409:1465 with a "
                         "working driver and the uEye API are present, else the simulator; 'ueye' = real only; "
                         "'sim' = simulator. Real: 2048x1536 colour, manual exposure, gain 1x, session-only "
                         "settings (ADR-0039)")
    ap.add_argument("--illumination", choices=("auto", "real", "sim"), default="auto",
                    help="LEDs: 'auto' (default) = the REAL LED board when exactly one 0DAD:0051 (application) and no "
                         "bootloader interface are present, else the simulator; 'real' = real only; 'sim' = simulator. "
                         "Real: the light buttons switch brightfield/epi ON/OFF at the board's default level (no "
                         "intensity control); epi only lights the cube in the light path; all off on STOP and exit; "
                         "epi auto-off after 300 s. Never look into the light path (ADR-0048)")
    ap.add_argument("--mcp", nargs="?", type=int, const=8765, default=None, metavar="PORT",
                    help="serve the READ-ONLY MCP link for AI assistants on http://127.0.0.1:PORT/mcp (default "
                         "8765) with a bearer token; prints the `claude mcp add` command. The assistant can read "
                         "state, events, telemetry and images and can command nothing (ADR-0003, ADR-0054)")
    ap.add_argument("--no-led-autostart", action="store_true",
                    help="do not start the LED board application at launch. By default, a board found in its "
                         "bootloader (0DAD:0057, its power-up state) is started with the reviewed version query + "
                         "StartApp steps, as the vendor software does every session (ADR-0055)")
    ap.add_argument("--no-limits-autoload", action="store_true",
                    help="with --motor-control: do not load the calibrated X/Y limits at launch. By default, drives "
                         "still on their power-up limits (0-22 mm) get Motor Gate 7's reviewed limit writes, no motion "
                         "(ADR-0056)")
    args = ap.parse_args(argv)
    # one app per instrument: a second copy would fight the first for COM3 and the LED board (2026-11-18: two copies
    # started 6 s apart after a power cycle). Checked before anything touches the hardware.
    instance_lock = None
    real_hw = (args.motor_telemetry and str(args.motor_telemetry).lower() != "sim") or args.illumination == "real"
    if real_hw and args.screenshot_dir is None:
        from PySide6.QtCore import QLockFile

        from openevos.instrument.settings import default_settings_path

        lock_path = (args.settings or default_settings_path()).with_name("openevos-instrument.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(lock_path))
        if not instance_lock.tryLock(300):
            from PySide6.QtWidgets import QApplication, QMessageBox

            _app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.warning(None, "OpenEVOS", "OpenEVOS is already running on this instrument.\n\nUse the open "
                                "window, or close it before starting another (only one program can own the motor port "
                                "and the LED board).")
            return 1
    launch_notes: list[tuple[str, str]] = []  # also written to the session fault log once it is open

    def note(step: str, text: str) -> None:
        launch_notes.append((step, text))
        print(text, file=sys.stderr, flush=True)

    if (args.illumination != "sim" and not args.no_led_autostart and args.screenshot_dir is None
            and sys.platform == "win32"):
        from openevos.maintenance.led_autostart import autostart

        try:
            r = autostart(evidence_dir=args.scans_dir)
            note("led_autostart", f"LED board: {r.message}"
                 + (f" (evidence: {', '.join(r.evidence)})" if r.evidence else ""))
        except Exception as exc:  # noqa: BLE001 - the app still starts; the precheck reports the light
            note("led_autostart", f"LED board auto-start failed: {exc}")
    if (args.motor_control and args.motor_telemetry and str(args.motor_telemetry).lower() != "sim"
            and not args.no_limits_autoload and args.screenshot_dir is None and sys.platform == "win32"):
        from openevos.maintenance.motor_autolimits import autoload

        try:  # before the session opens the motor port
            r = autoload(scans=args.scans_dir)
            note("limits_autoload", f"Stage limits: {r.message}" + (f" (evidence: {r.evidence})" if r.evidence else ""))
        except Exception as exc:  # noqa: BLE001 - the app still starts; the precheck reports power-up limits
            note("limits_autoload", f"Stage limits auto-load failed: {exc}")
    illumination = _resolve_illumination(args.illumination, screenshots=args.screenshot_dir is not None)
    mono_camera =_resolve_mono_camera(args.mono_camera, screenshots=args.screenshot_dir is not None)
    colour_camera = _resolve_colour_camera(args.colour_camera, screenshots=args.screenshot_dir is not None)
    cameras = {k: v for k, v in (("mono", mono_camera), ("color", colour_camera)) if v}
    if args.motor_control and not args.motor_telemetry:
        ap.error("--motor-control needs --motor-telemetry PORT")
    calibration = args.calibration
    if calibration is None and args.scans_dir.is_dir():  # newest parsed Step-D calibration (git-ignored scans)
        found = sorted(args.scans_dir.glob("led-application-stepd-*-parsed.json"))
        calibration = found[-1] if found else None
    telemetry = {"technosoft": {"port": args.motor_telemetry, "motion": bool(args.motor_control),
                                "calibration": str(calibration) if calibration else None,
                                "sim_realtime": True, "locked_axes": [a.upper() for a in args.lock_axis],
                                "speed_control": bool(args.speed_control),
                                # the drift monitor's latch survives restarts on the real drives only: a simulated
                                # stage must never lock (or unlock) the real one
                                "monitor_dir": None if str(args.motor_telemetry).lower() == "sim"
                                else str(args.scans_dir / "stage-monitor"),
                                "stage_log": bool(args.stage_log)}} \
        if args.motor_telemetry else None

    import os

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and sys.platform == "win32":
        # the offscreen platform has no system font lookup on Windows (screenshots/CI)
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if fonts.is_dir():
            os.environ.setdefault("QT_QPA_FONTDIR", str(fonts))
    from PySide6.QtWidgets import QApplication

    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.settings import default_settings_path
    from openevos.ui import theme
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("OpenEVOS")
    theme.load_fonts(app)
    settings_path = None if args.no_settings else (args.settings or default_settings_path())
    cal_path = None if args.no_settings else settings_path.with_name("calibration.json")
    audit_path = None if args.no_settings or telemetry is None else settings_path.with_name("physical_audit.jsonl")
    session = build_sim_session(ThreadClock(), settings_path=settings_path, fps=args.fps, calibration_path=cal_path,
                                telemetry=telemetry, audit_path=audit_path,
                                cameras=cameras or None, illumination=illumination)
    if args.theme:
        session.settings.basic.theme = args.theme
    name = session.settings.basic.theme
    theme.apply_theme(("dark" if theme.system_prefers_dark(app) else "light") if name == "system" else name, app)
    fault_log = None
    if settings_path is not None and not args.screenshot_dir:
        from openevos.acquisition.session_log import SessionLog

        try:
            fault_log = SessionLog(session.core.bus, settings_path.with_name("Logs"))
            for what, text in launch_notes:
                fault_log.write("launch", step=what, message=text)
        except OSError as exc:
            print(f"session fault log unavailable: {exc}", file=sys.stderr, flush=True)
    mcp_link = None
    if args.mcp is not None and not args.screenshot_dir:
        from openevos.mcp_link.cli import start_for_session

        mcp_link = start_for_session(session, port=args.mcp)
        print(mcp_link[2], file=sys.stderr, flush=True)
    model = SessionModel(session)
    model.scans_dir = args.scans_dir  # read by the Diagnostics page
    win = MainWindow(model)
    if args.screenshot_dir:
        from openevos.ui.screenshots import capture_all_pages

        capture_all_pages(app, win, args.screenshot_dir)
        win.close()
        session.shutdown()
        return 0
    win.show()
    win.start_precheck()  # read-only; opens the precheck only when something needs attention
    win.pages["capture"].route_light_path_when_ready()  # F to the mirror if the app opened on the colour camera
    try:
        return app.exec()
    finally:
        if mcp_link:
            mcp_link[0].stop()
            mcp_link[1].close()
        session.shutdown()
        if fault_log is not None:
            fault_log.close()
        if instance_lock is not None:
            instance_lock.unlock()


def _resolve_mono_camera(choice: str, *, screenshots: bool = False, probe=None) -> str | None:
    """'flycapture2' (real) or None (simulator). 'auto' probes read-only: PnP + SDK file, no camera opened."""
    if choice == "sim" or (choice == "auto" and screenshots):
        return None
    if choice == "flycapture2":
        return "flycapture2"
    present = probe() if probe else _real_mono_present()
    print(f"mono camera: {'real Chameleon (FlyCapture2)' if present else 'simulator (no usable Chameleon found)'}",
          file=sys.stderr)
    return "flycapture2" if present else None


def _resolve_colour_camera(choice: str, *, screenshots: bool = False, probe=None) -> str | None:
    """'ueye' (real) or None (simulator). 'auto' probes read-only: PnP + API file, no camera opened."""
    if choice == "sim" or (choice == "auto" and screenshots):
        return None
    if choice == "ueye":
        return "ueye"
    present = probe() if probe else _real_colour_present()
    print(f"colour camera: {'real uEye UI-146xLE' if present else 'simulator (no usable uEye found)'}",
          file=sys.stderr)
    return "ueye" if present else None


def _resolve_illumination(choice: str, *, screenshots: bool = False, probe=None) -> str | None:
    """'real' or None (simulator). 'auto' enumerates read-only (SetupAPI); nothing is opened or sent."""
    if choice == "sim" or (choice == "auto" and screenshots):
        return None
    if choice == "real":
        return "real"
    present = probe() if probe else _real_led_present()
    print(f"illumination: {'real LED board (0DAD:0051)' if present else 'simulator (no LED board application found)'}",
          file=sys.stderr)
    return "real" if present else None


def _real_led_present() -> bool:
    if sys.platform != "win32":
        return False
    from openevos.acquisition.session import real_led_board_present

    try:
        return real_led_board_present()
    except Exception:  # noqa: BLE001 - probing must never stop the app
        return False


def _real_colour_present() -> bool:
    from openevos.acquisition.session import real_colour_camera_present

    try:
        return real_colour_camera_present()
    except Exception:  # noqa: BLE001 - probing must never stop the app
        return False


def _real_mono_present() -> bool:
    from openevos.acquisition.session import real_mono_camera_present

    try:
        return real_mono_camera_present()
    except Exception:  # noqa: BLE001 - probing must never stop the app
        return False


if __name__ == "__main__":
    raise SystemExit(main())
