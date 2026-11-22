"""Light and camera controls in the UI: every control the light/camera backends support is reachable, goes
through the command router, and reflects the device state (simulated devices only)."""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings
from openevos.safety.capabilities import AxisLimits, ParameterRange


@pytest.fixture
def session():
    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    yield s
    s.shutdown()


def limit_colour_gain_to_unity(session) -> None:
    """Give the colour camera the capability record the uEye backend declares (gain 1x only), so the
    router itself rejects any other gain - the UI must never send one."""
    did = session.profile.camera(CameraRole.COLOR).device_id
    recs = session.core.devices._records
    rec = recs[did]
    ranges = tuple(r if r.name != "gain_db" else ParameterRange(
        name="gain_db", limits=AxisLimits(minimum=0.0, maximum=0.001, unit="dB")) for r in rec.capabilities.ranges)
    recs[did] = dataclasses.replace(rec, capabilities=rec.capabilities.model_copy(update={"ranges": ranges}))


# ---------------------------------------------------------------------------------------- Qt-free
def test_device_state_limits_and_readback(session):
    from openevos.ui import device_state as ds

    lim = ds.camera_limits(session, CameraRole.MONO)
    assert lim.exposure_ms == (0.01, 10_000.0)  # simulator capability record
    assert lim.gain == (1.0, 64.0) and not lim.gain_fixed
    assert ds.light_limits(session, "trans") == (0.0, 100.0)
    assert "cube1" in ds.light_channels(session)
    ctl = session.controller
    ctl.run(ctl.set_exposure(CameraRole.MONO, 123.0))
    ctl.run(ctl.set_gain(CameraRole.MONO, 2.0))
    ctl.run(ctl.set_light("cube1", 55.0, True))
    cam = ds.read_camera(session, CameraRole.MONO)
    assert cam.exposure_ms == 123.0 and cam.gain == pytest.approx(2.0) and cam.synthetic
    light = ds.read_light(session)
    assert (light.channel, light.enabled, light.intensity_percent) == ("cube1", True, 55.0)
    assert "ON 55 %" in light.summary()


def test_device_state_unity_gain_camera(session):
    from openevos.ui import device_state as ds

    limit_colour_gain_to_unity(session)
    lim = ds.camera_limits(session, CameraRole.COLOR)
    assert lim.gain_fixed and lim.gain == (1.0, 1.0)
    assert lim.clamp_gain(8.0) == 1.0


def test_apply_camera_resends_after_direct_setting(session):
    """A direct exposure change (Capture page / Actual mode) must not leave apply_camera trusting a stale
    cache: the next capture would otherwise run at the wrong exposure."""
    ctl = session.controller
    cam = ctl.camera_backend(CameraRole.MONO)
    ctl.apply_camera(CameraRole.MONO, 40.0, 1.0)
    ctl.run(ctl.set_exposure(CameraRole.MONO, 100.0))
    ctl.run(ctl.set_gain(CameraRole.MONO, 3.0))
    ctl.apply_camera(CameraRole.MONO, 40.0, 1.0)
    assert cam.exposure_ms == 40.0
    assert cam.gain == pytest.approx(1.0)


def test_stop_switches_the_led_off(session):
    """Operator STOP halts the LED board: the light goes off at once; intensity and channel are kept."""
    ctl, router, light = session.controller, session.core.router, session.rig.light
    ctl.run(ctl.set_light("cube1", 60.0, True))
    assert light.emitting
    halted, errors = router.stop_all("test STOP")
    assert light.device_id in halted and errors == {}
    assert not light.enabled and not light.emitting
    assert (light.channel, light.intensity_percent) == ("cube1", 60.0)
    # the controller's light state follows the halted device (autofocus / calibration check it)
    assert ctl.light.enabled is False and ctl.light.lit is None
    assert (ctl.light.channel, ctl.light.intensity_percent) == ("cube1", 60.0)
    snap = session.core.store.get(light.device_id)
    assert snap.attr("led_enabled") is False and snap.attr("channel") == "cube1"
    router.clear_stop()
    ctl.run(ctl.set_light("cube1", 60.0, True))  # after Resume, on again only through a command
    assert ctl.light.enabled and session.core.store.get(light.device_id).attr("led_enabled") is True


def test_stop_during_carriage_move_cancels_it_and_leaves_led_off(session):
    from openevos.core.models import CommandStatus

    ctl, router, light = session.controller, session.core.router, session.rig.light
    ctl.run(ctl.set_light("cube1", 50.0, True))
    ticket = ctl.set_light("cube3", 50.0, True)  # carriage travel (simulated latency)
    for _ in range(200):
        if light.busy:
            break
        time.sleep(0.005)
    assert light.busy
    router.stop_all("test STOP")
    assert ticket.wait(5.0) is CommandStatus.CANCELLED
    assert not light.busy and not light.enabled
    assert light.channel == "cube1"  # the move was abandoned, not carried out
    router.clear_stop()


def test_halt_alone_cancels_a_pending_move(session):
    from openevos.core.models import CommandStatus

    ctl, light = session.controller, session.rig.light
    ticket = ctl.set_light("cube2", 40.0, True)
    for _ in range(200):
        if light.busy:
            break
        time.sleep(0.005)
    light.halt()  # called directly, without the router cancelling first: the move fails as HALTED
    assert ticket.wait(5.0) is CommandStatus.FAILED
    assert "halted" in ticket.record.message
    assert light.channel != "cube2"  # not carried out
    assert not light.enabled and not light.busy
    light.halt()  # idempotent when idle


# ---------------------------------------------------------------------------------------- offscreen UI
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from openevos.ui.theme import QSS

    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(QSS)
    return a


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(tmp_path / "docs"))
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow


    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    limit_colour_gain_to_unity(s)
    m = SessionModel(s)
    w = MainWindow(m)
    w.resize(1600, 960)
    w.show()
    pump(app, 0.5)
    yield w
    w.close()
    s.shutdown()
    w.deleteLater()
    pump(app, 0.1)


def _settled(app, cap):

    return wait_for(app, lambda: not cap.coalesce.pending() and not cap.model.tasks.busy)


def _errors(window):
    return [e for e in window.model.log if e[1] == "error"]


def test_no_gamma_and_the_histogram_toggles(app, window):
    """Gamma was removed (the live display is always linear); the Histogram toolbar button shows and hides the
    levels histogram and Auto range, and remembers the choice (operator, 2026-11-18)."""
    cap = window.pages["capture"]
    assert not hasattr(cap, "gamma")
    assert not cap.chk_sci.isVisible()
    assert cap.btn_hist.isChecked() and cap.hist.isVisible() and cap.btn_auto.isVisible()
    cap.btn_hist.click()
    pump(app, 0.05)
    assert not cap.hist.isVisible() and not cap.btn_auto.isVisible()
    assert window.model.session.settings.basic.show_histogram is False
    cap.btn_hist.click()
    pump(app, 0.05)
    assert cap.hist.isVisible() and window.model.session.settings.basic.show_histogram is True
    # the colour camera's live frames switch the histogram to R, G, B curves; Auto range has nothing to reset
    from openevos.instrument.profile import CameraRole as _Role

    cap.switch_camera(_Role.COLOR)
    assert _settled(app, cap)
    assert wait_for(app, lambda: cap.hist.rgb is not None, timeout=5.0)
    assert len(cap.hist.rgb) == 3 and not cap.btn_auto.isEnabled()
    cap.switch_camera(_Role.MONO)
    assert _settled(app, cap)
    assert wait_for(app, lambda: cap.hist.rgb is None, timeout=5.0)
    assert cap.btn_auto.isEnabled()


def test_camera_switch_turns_the_light_off_unless_the_setting_is_off(app, window):
    cap = window.pages["capture"]
    s = window.session
    assert s.settings.basic.light_off_on_camera_switch is True  # default
    cap.btn_power.setChecked(True)
    assert wait_for(app, lambda: s.controller.light.enabled)
    cap.switch_camera(CameraRole.COLOR)
    assert _settled(app, cap)
    assert not cap.btn_power.isChecked() and wait_for(app, lambda: not s.controller.light.enabled)
    s.settings.basic.light_off_on_camera_switch = False
    cap.btn_power.setChecked(True)
    assert wait_for(app, lambda: s.controller.light.enabled)
    assert _settled(app, cap)
    pump(app, 0.5)
    assert cap.btn_power.isChecked() and s.controller.light.enabled, list(window.model.log)[-8:]
    cap.switch_camera(CameraRole.MONO)
    assert _settled(app, cap)
    ok = wait_for(app, lambda: cap.btn_power.isChecked() and s.controller.light.enabled, 5.0)
    assert ok, (cap.channel, s.controller.light, cap.btn_power.isChecked(), list(window.model.log)[-8:])
    settings_page = next(p for p in window.pages.values() if hasattr(p, "chk_light_off_switch"))
    settings_page.chk_light_off_switch.setChecked(False)
    settings_page.chk_light_off_switch.setChecked(True)
    assert s.settings.basic.light_off_on_camera_switch is True


def test_actual_mode_controls_reach_devices_and_read_back(app, window):

    cap = window.pages["capture"]
    s = window.session
    cam = s.controller.camera_backend(CameraRole.MONO)
    assert (cap.exp.minimum, cap.exp.maximum) == (0.01, 10_000.0)  # from the capability record
    assert (cap.gain_row.minimum, cap.gain_row.maximum) == (1.0, 64.0)
    cap._select_channel("cube1")
    cap._set_actual(True)
    cap.led.set_value(65, emit=True)
    cap.exp.set_value(250, emit=True)
    cap.gain_row.set_value(4, emit=True)
    assert wait_for(app, lambda: cam.exposure_ms == 250 and abs(cam.gain - 4.0) < 1e-9
                    and s.rig.light.intensity_percent == 65 and s.rig.light.channel == "cube1")
    assert _settled(app, cap)
    assert wait_for(app, lambda: "250 ms" in cap.lbl_device.text() and "4.00×" in cap.lbl_device.text())
    assert "ON 65 %" in cap.lbl_device.text()
    hw = window.pages["hardware"]
    hw.refresh()
    rows = {r[0]: r for r in hw.lightcam_rows()}
    assert "250 ms" in rows[cam.device_id][4]
    assert "ON 65 %" in rows[s.profile.illumination_device][4]
    assert hw.lightcam.rowCount() == len(rows) == 1 + len(s.profile.cameras)
    assert _errors(window) == []


def test_power_button_drives_and_follows_the_led(app, window):

    cap = window.pages["capture"]
    s = window.session
    assert _settled(app, cap)
    assert not s.rig.light.enabled and not cap.btn_power.isChecked()  # dark at launch
    assert cap.btn_power.text() == "Light off" and s.capture.light_power is False
    cap.btn_power.click()  # on through the router
    assert wait_for(app, lambda: s.rig.light.enabled)
    assert _settled(app, cap)
    cap.btn_power.click()  # off through the router
    assert wait_for(app, lambda: not s.rig.light.enabled)
    assert _settled(app, cap)
    cap.btn_power.click()
    assert wait_for(app, lambda: s.rig.light.enabled)
    assert _settled(app, cap)
    # the LED is switched off by something else (another workflow, a script): the toggle follows the device
    ctl = s.controller
    ctl.run(ctl.set_light(cap.channel, s.rig.light.intensity_percent, False))
    assert wait_for(app, lambda: not cap.btn_power.isChecked())
    assert cap.btn_power.text() == "Light off" and s.capture.light_power is False
    assert "OFF" in cap.lbl_device.text()


def test_live_toggle_starts_and_stops_the_stream(app, window):

    cap = window.pages["capture"]
    ctl = window.session.controller
    assert wait_for(app, lambda: ctl.is_live(CameraRole.MONO))
    assert cap.btn_live.isChecked()
    cap.btn_live.click()  # stop
    assert wait_for(app, lambda: not ctl.is_live(CameraRole.MONO))
    assert not cap.btn_live.isChecked() and cap.view_mode == "stopped"
    assert "STOPPED" in cap.lbl_device.text()
    pump(app, 0.2)  # a frame already in the preview cache may still be shown once
    shown = window.model.frames_shown
    pump(app, 0.3)
    assert window.model.frames_shown == shown  # no frames while stopped
    window.navigate("hardware")
    window.navigate("capture")  # coming back does not silently restart the stream
    pump(app, 0.1)
    assert not ctl.is_live(CameraRole.MONO)
    cap.btn_live.click()  # start
    assert wait_for(app, lambda: ctl.is_live(CameraRole.MONO))
    assert cap.btn_live.isChecked() and cap.view_mode == "live"
    assert wait_for(app, lambda: window.model.frames_shown > shown)


def test_unity_gain_camera_limits_controls_and_settings(app, window):

    cap = window.pages["capture"]
    s = window.session
    cap.switch_camera(CameraRole.COLOR)
    assert _settled(app, cap)
    assert (cap.gain_row.minimum, cap.gain_row.maximum) == (1.0, 1.0)
    assert cap.gain_row.box.isReadOnly()
    assert "1× only" in cap.gain_row.toolTip()
    # the Light slider's top end asks for 8x gain; the colour camera gets 1x and the router rejects nothing
    cap.light.set_value(100, emit=True)
    assert _settled(app, cap)
    cs = cap.current_channel_settings()
    assert cs.gain == 1.0
    cam = s.controller.camera_backend(CameraRole.COLOR)
    assert wait_for(app, lambda: cam.exposure_ms == cs.exposure_ms)
    assert abs(cam.gain - 1.0) < 1e-9
    assert _errors(window) == []
    from openevos.ui.dialogs import LightCameraDialog

    dlg = LightCameraDialog(cap, cap)
    assert dlg.gain.maximum() == 1.0 and not dlg.gain.isEnabled()
    assert dlg.exp.maximum() == 10_000.0
    dlg.deleteLater()
    cap.switch_camera(CameraRole.MONO)
    assert cap.gain_row.maximum == 64.0 and not cap.gain_row.box.isReadOnly()



def test_stop_turns_led_off_and_power_toggle_follows(app, window):
    cap = window.pages["capture"]
    s = window.session
    assert _settled(app, cap)
    cap.btn_power.click()
    assert wait_for(app, lambda: s.rig.light.enabled)
    assert _settled(app, cap)
    assert cap.btn_power.isChecked()
    s.core.router.stop_all("test STOP")
    window.model.device_changed.emit(s.rig.light.device_id, None)
    assert wait_for(app, lambda: not cap.btn_power.isChecked())
    assert not s.rig.light.enabled and "OFF" in cap.lbl_device.text()
    assert not s.controller.light.enabled
    s.core.router.clear_stop()


def test_channel_click_lights_it_and_a_second_click_turns_it_off(app, window):
    cap = window.pages["capture"]
    s = window.session
    assert _settled(app, cap)
    other = next(slot for slot in cap.ch_buttons if slot != cap.channel)
    cap.ch_buttons[other].clicked.emit(other)  # a new channel: selected and lit
    assert wait_for(app, lambda: s.rig.light.enabled and s.rig.light.channel == other)
    assert _settled(app, cap)
    name = s.profile.channel(other).display
    assert cap.channel == other and cap.btn_power.isChecked() and cap.btn_power.text() == f"{name} on"
    # the lit channel shows it (ON, its LED colour); every other channel reads off (operator 2026-11-23)
    assert [slot for slot, b in cap.ch_buttons.items() if b.lit] == [other]
    cap.ch_buttons[other].clicked.emit(other)  # the lit channel again: off, still selected
    assert wait_for(app, lambda: not s.rig.light.enabled)
    assert _settled(app, cap)
    assert cap.channel == other and not cap.btn_power.isChecked() and cap.btn_power.text() == "Light off"
    assert not any(b.lit for b in cap.ch_buttons.values()) and cap.ch_buttons[other].selected
    cap.ch_buttons[other].clicked.emit(other)  # and on again
    assert wait_for(app, lambda: s.rig.light.enabled)


def test_camera_and_live_segmented_controls(app, window):
    cap = window.pages["capture"]
    ctl = window.session.controller
    assert cap.seg_camera.current() == "mono"
    assert wait_for(app, lambda: cap.seg_live.current() == "live")
    cap.seg_camera.buttons["color"].click()
    assert wait_for(app, lambda: cap.role is CameraRole.COLOR and ctl.is_live(CameraRole.COLOR))
    assert cap.seg_camera.current() == "color" and "Color" in cap.lbl_camera.text()
    assert not cap.ch_buttons["cube1"].isEnabled()  # colour camera: brightfield/phase only
    cap.seg_live.buttons["stopped"].click()
    assert wait_for(app, lambda: not ctl.is_live(CameraRole.COLOR))
    assert cap.seg_live.current() == "stopped" and not cap.btn_live.isChecked()
    cap.seg_live.buttons["live"].click()
    assert wait_for(app, lambda: ctl.is_live(CameraRole.COLOR))
    assert cap.seg_live.current() == "live" and cap.btn_live.isChecked()
    cap.switch_camera(CameraRole.MONO)  # from code: the segment follows without emitting
    assert cap.seg_camera.current() == "mono"
    cap._on_busy(True)
    assert not cap.seg_camera.isEnabled() and not cap.seg_live.isEnabled()
    cap._on_busy(False)
    assert cap.seg_camera.isEnabled() and cap.seg_live.isEnabled()


def test_number_boxes_fit_their_widest_value(app, window):
    """Percent/number readouts never clip: each box is at least as wide as its widest value + padding."""
    from PySide6.QtGui import QFont, QFontMetrics

    from openevos.ui import tokens as tk
    from openevos.ui.widgets.param import ParamRow

    f = QFont(tk.FONT_MONO)
    f.setPixelSize(12)
    fm = QFontMetrics(f)
    rows = window.findChildren(ParamRow)
    assert len(rows) >= 4
    for r in rows:
        for v in (r.minimum, r.maximum):
            text = f"{v:.{r.decimals}f}{r.box.suffix()}"
            assert r.box.width() >= fm.horizontalAdvance(text) + 16, (r.label.text(), text, r.box.width())
    wide = ParamRow("Exposure", 0.0, 99_999.9, 1.0, decimals=1, unit="ms")
    assert wide.box.width() >= fm.horizontalAdvance("99999.9 ms") + 16
    small = wide.box.width()
    wide.set_range(0.0, 10.0)  # a narrower device range shrinks the box again
    assert wide.box.width() < small
