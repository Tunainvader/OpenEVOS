"""Physical colour camera backend (ADR-0039) against a fake uEye DLL: never touches hardware."""

import ctypes as C
import threading
import time

import numpy as np
import pytest
from test_backend_ueye_gate import o
from test_maintenance_camera_gate2_colour import StatefulUeye

from openevos.backends.ueye import camera as ucam
from openevos.backends.ueye import sdk as u
from openevos.core.models import CommandOrigin, Connection, SetCameraExposure, SetCameraGain


class FakeColour(StatefulUeye):
    """BGR8 frames: B=10, G=20, R=30 everywhere."""

    def is_AllocImageMem(self, h, w, hh, bpp, pmem, pid):
        self.pitch = w * bpp // 8
        buf = (C.c_ubyte * (self.pitch * hh))()
        if bpp == 24:
            a = np.frombuffer(buf, dtype=np.uint8).reshape(hh, w, 3)
            a[:] = (10, 20, 30)
        self.bufs[7] = buf
        o(pmem).value, o(pid).value = C.addressof(buf), 7
        return 0


REAL_SDK = ucam.UeyeAppSdk


def sdk(fake):
    return REAL_SDK(dll_path="fake.dll", loader=lambda p: fake)


class Transport:
    def __init__(self):
        self.frames, self.got = [], threading.Event()

    def push(self, px, md, owned):
        self.frames.append((px, md))
        self.got.set()
        return True


class Ctx:
    command_id = "c1"

    def __init__(self):
        self.done = self.failed = None

    def mark_started(self):
        pass

    def complete(self, result=None, payload=None):
        self.done = True

    def fail(self, message, error_code=None):
        self.failed = (message, error_code)


def make(fake=None, transport=None):
    fake = fake or FakeColour()
    reports = []
    dev = ucam.UeyeColourCamera("cam-color", transport or Transport(), sdk=sdk(fake))
    dev.attach(lambda **kw: reports.append(kw) or True)
    return dev, fake, reports


def exp(ms):
    return SetCameraExposure(device_id="cam-color", origin=CommandOrigin.UI, exposure_ms=ms, timeout_s=5.0)


def test_connect_sets_only_gain_and_exposure():
    dev, fake, reports = make()
    dev.connect()
    assert reports[-1]["connection"] is Connection.CONNECTED and reports[-1]["firmware_mode"].value == "application"
    assert fake.mode == 1 and fake.gains[0] == 0 and fake.bl_mode == 1 and fake.bl_offset == 0
    assert abs(fake.exposure - ucam.DEFAULT_EXPOSURE_MS) <= ucam.EXPOSURE_TOL_MS
    assert not any(n == "is_SetColorMode" and a != (u.IS_GET_COLOR_MODE,) for n, a in fake.calls)
    r = {p.name: (p.limits.minimum, p.limits.maximum) for p in dev.capabilities().ranges}
    assert r["exposure_ms"] == pytest.approx((0.101625, 199.693125)) and r["gain_db"] == (0.0, 0.001)


def test_stream_delivers_rgb_frames():
    t = Transport()
    dev, _, _ = make(transport=t)
    dev.connect()
    dev.start_stream(acquisition_id="a1", n_frames=2)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (len(t.frames) < 2 or dev.streaming):
        time.sleep(0.02)
    px, md = t.frames[0]
    assert len(t.frames) == 2 and px.shape == (1536, 2048, 3) and px.dtype == np.uint8
    assert tuple(px[0, 0]) == (30, 20, 10)  # BGR in memory -> RGB
    assert md.synthetic is False and md.acquisition_id == "a1"


def test_timeouts_skipped_while_streaming():
    t = Transport()
    fake = FakeColour()
    dev, _, _ = make(fake, t)
    dev.connect()
    fake.timeouts = 3
    dev.start_stream(n_frames=1)
    assert t.got.wait(5.0) and dev.skipped_grabs == 3


def test_exposure_command_and_guard():
    dev, fake, _ = make()
    dev.connect()
    ctx = Ctx()
    dev.start(exp(33.0), ctx)
    assert ctx.done and abs(fake.exposure - 33.0) <= 0.11 and dev.verify_result(exp(33.0))[0]
    with pytest.raises(u.UeyeError, match="refused"):  # outside the range read from the camera
        dev._sdk._call("is_Exposure", dev._sdk._h, u.EXPOSURE_SET, C.byref(C.c_double(500.0)), 8)


def test_exposure_accepted_while_live():
    t = Transport()
    dev, fake, _ = make(transport=t)
    dev.connect()
    dev.start_stream()
    assert t.got.wait(3.0)
    ctx = Ctx()
    dev.start(exp(5.0), ctx)
    dev.stop_stream()
    assert ctx.done and abs(fake.exposure - 5.0) <= 0.11


def test_first_frame_after_exposure_change_dropped():
    """The UI-146xLE pipelines one frame (brightfield sweep 2026-11-15): the first freeze after an exposure
    change still carries the old exposure, so it never reaches the transport."""
    t = Transport()
    dev, fake, _ = make(transport=t)
    dev.connect()  # sets the default exposure
    freezes = lambda: sum(n == "is_FreezeVideo" for n, _ in fake.calls)
    dev.start_stream(n_frames=1)
    assert t.got.wait(5.0)
    deadline = time.monotonic() + 5
    while dev.streaming and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(t.frames) == 1 and dev.stale_dropped == 1 and freezes() == 2
    dev.start_stream(n_frames=1)  # no exposure change: nothing dropped
    deadline = time.monotonic() + 5
    while (len(t.frames) < 2 or dev.streaming) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(t.frames) == 2 and dev.stale_dropped == 1 and freezes() == 3
    dev.start(exp(40.0), Ctx())
    dev.start_stream(n_frames=1)
    deadline = time.monotonic() + 5
    while (len(t.frames) < 3 or dev.streaming) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(t.frames) == 3 and dev.stale_dropped == 2 and freezes() == 5
    assert abs(t.frames[-1][1].exposure_ms - 40.0) <= 0.11


class LaggingColour(FakeColour):
    """Models the freerun pipelining seen on 2026-11-15: each freeze returns the frame that was already
    exposing, i.e. one taken at the exposure in force at the previous freeze (or at init: the 199.7 ms
    power-up exposure). Pixel value = exposure in ms, rounded, so a frame shows which exposure it really had."""

    def is_InitCamera(self, ph, hwnd):
        rc = super().is_InitCamera(ph, hwnd)
        self.in_flight = self.exposure  # the frame the sensor is exposing now
        return rc

    def is_FreezeVideo(self, h, wait):
        rc = super().is_FreezeVideo(h, wait)
        if rc == 0:
            taken, self.in_flight = self.in_flight, self.exposure
            np.frombuffer(self.bufs[7], dtype=np.uint8)[:] = min(255, round(taken))
        return rc


def _snap(dev, t):
    n = len(t.frames)
    dev.start_stream(n_frames=1)
    deadline = time.monotonic() + 5
    while (len(t.frames) <= n or dev.streaming) and time.monotonic() < deadline:
        time.sleep(0.01)
    px, md = t.frames[-1]
    return int(px[0, 0, 0]), md.exposure_ms


def _sweep(dev, t):
    out = []
    for ms in (199.0, 0.5, 2.0, 5.0, 50.0):  # one snap straight after each change, as the sweep script did
        dev.start(exp(ms), Ctx())
        out.append(_snap(dev, t))
    return out


def test_exposure_lag_reproduced_without_the_drop(monkeypatch):
    """With no frame dropped, the snap after each change carries the previous exposure (the bug)."""
    monkeypatch.setattr(ucam, "STALE_FRAMES_AFTER_EXPOSURE", 0)
    t = Transport()
    dev, _, _ = make(LaggingColour(), t)
    dev.connect()
    vals = [v for v, _ in _sweep(dev, t)]
    assert vals == [200, 199, 1, 2, 5]  # each is the exposure before it; first = power-up 199.7 ms, as seen


def test_snap_after_exposure_change_has_the_new_exposure():
    t = Transport()
    dev, _, _ = make(LaggingColour(), t)
    dev.connect()
    got = _sweep(dev, t)
    assert [v for v, _ in got] == [199, 1, 2, 5, 50]  # 0.5 ms reads back 0.508, rounds to 1
    assert all(abs(md - want) <= 0.11 for (_, md), want in zip(got, (199.0, 0.5, 2.0, 5.0, 50.0), strict=True))
    assert dev.stale_dropped == 5  # one per change (connect's pending drop is not stacked on the first)
    assert _snap(dev, t)[0] == 50 and dev.stale_dropped == 5  # no change: nothing dropped


def test_frame_keeps_the_exposure_it_was_taken_at():
    """An exposure command landing between the freeze and delivery must not relabel the frame."""
    t = Transport()
    dev, _, _ = make(LaggingColour(), t)
    dev.connect()
    real = dev._deliver

    def deliver_late(px, exposure_ms):
        dev.start(exp(80.0), Ctx())  # arrives after the freeze released the SDK lock
        real(px, exposure_ms)
    dev._deliver = deliver_late
    dev.start_stream(n_frames=1)
    assert t.got.wait(5.0)
    px, md = t.frames[0]
    assert int(px[0, 0, 0]) == 20 and abs(md.exposure_ms - 20.0) <= 0.11 and abs(dev.exposure_ms - 80.0) <= 0.11


class LightLagging(FakeColour):
    """The LED-change lag seen on 2026-11-15: each freeze returns the frame already exposing, lit by the light
    as it was at the previous freeze. Pixel value = light level."""

    def is_InitCamera(self, ph, hwnd):
        rc = super().is_InitCamera(ph, hwnd)
        self.light = self.in_flight = 0
        return rc

    def is_FreezeVideo(self, h, wait):
        rc = super().is_FreezeVideo(h, wait)
        if rc == 0:
            taken, self.in_flight = self.in_flight, self.light
            np.frombuffer(self.bufs[7], dtype=np.uint8)[:] = taken
        return rc


@pytest.mark.parametrize("invalidate", [False, True])
def test_snap_after_light_change(invalidate):
    """Without invalidate_frames the first snap after the light comes on is the dark frame (the bug)."""
    t = Transport()
    fake = LightLagging()
    dev, _, _ = make(fake, t)
    dev.connect()
    assert _snap(dev, t)[0] == 0  # dark; takes connect's exposure drop
    dropped = dev.stale_dropped
    fake.light = 118
    if invalidate:
        dev.invalidate_frames("light changed")
    assert _snap(dev, t)[0] == (118 if invalidate else 0)
    assert dev.stale_dropped == dropped + invalidate
    assert _snap(dev, t)[0] == 118 and dev.stale_dropped == dropped + invalidate  # steady light: no drop


def test_gain_other_than_1x_refused():
    dev, fake, _ = make()
    dev.connect()
    ctx = Ctx()
    dev.start(SetCameraGain(device_id="cam-color", origin=CommandOrigin.UI, gain_db=6.0, timeout_s=5.0), ctx)
    assert ctx.failed and ctx.failed[1] == "UNSUPPORTED" and fake.gains[0] == 0
    ctx = Ctx()
    dev.start(SetCameraGain(device_id="cam-color", origin=CommandOrigin.UI, gain_db=0.0, timeout_s=5.0), ctx)
    assert ctx.done


def test_non_default_mode_refused_without_changes():
    fake = FakeColour()
    orig = fake._defaults

    def raw8_defaults():
        orig()
        fake.mode = 11
    fake._defaults = raw8_defaults
    dev, _, reports = make(fake)
    dev.connect()
    assert reports[-1]["connection"] is Connection.DISCONNECTED and "driver defaults" in reports[-1]["fault"]
    assert fake.gains[0] == 0 and fake.exited


def test_disconnect_exits_camera():
    dev, fake, reports = make()
    dev.connect()
    dev.disconnect()
    assert fake.exited and fake.freed == [7] and "dropped" in reports[-1]["reason"]


def test_session_uses_real_colour_camera(monkeypatch):
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.profile import CameraRole

    fake = FakeColour()
    monkeypatch.setattr(ucam, "UeyeAppSdk", lambda: sdk(fake))
    s = build_sim_session(ThreadClock(), fast=True, cameras={"color": "ueye"})
    try:
        cam = s.controller.camera_backend(CameraRole.COLOR)
        assert isinstance(cam, ucam.UeyeColourCamera)
        s.controller.start_live(CameraRole.COLOR)
        img = s.capture.capture("trans", CameraRole.COLOR)  # the Capture button's path, with live running
        assert not img.synthetic and img.data.shape == (1536, 2048, 3) and img.is_color
        want = s.settings.channel(CameraRole.COLOR, "trans").exposure_ms
        assert abs(fake.exposure - want) <= 0.11
    finally:
        s.shutdown()
    assert fake.exited


def test_controller_drops_a_frame_only_when_the_light_really_changes(monkeypatch):
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock

    fake = FakeColour()
    monkeypatch.setattr(ucam, "UeyeAppSdk", lambda: sdk(fake))
    s = build_sim_session(ThreadClock(), fast=True, cameras={"color": "ueye"})
    try:
        from openevos.instrument.profile import CameraRole

        cam = s.controller.camera_backend(CameraRole.COLOR)
        ctl = s.controller
        cam._stale = 0
        ctl.run(ctl.set_light("trans", 50.0, True))
        assert cam._stale == 1  # dark -> lit
        cam._stale = 0
        ctl.run(ctl.set_light("trans", 50.0, True))
        assert cam._stale == 0  # same light again: nothing to drop
        ctl.run(ctl.set_light("trans", 80.0, True))
        assert cam._stale == 1  # intensity change
        cam._stale = 0
        ctl.run(ctl.set_light("trans", 80.0, False))
        assert cam._stale == 1  # lit -> dark
        cam._stale = 0
        ctl.run(ctl.set_light("trans", 0.0, True))
        assert cam._stale == 0  # on at 0 % is still dark
    finally:
        s.shutdown()


def test_app_colour_camera_resolution():
    from openevos.ui.app import _resolve_colour_camera as r

    assert r("sim") is None and r("ueye") == "ueye"
    assert r("auto", probe=lambda: True) == "ueye" and r("auto", probe=lambda: False) is None
    assert r("auto", screenshots=True, probe=lambda: True) is None
