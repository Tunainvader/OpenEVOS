"""Physical mono camera backend (ADR-0037) against a fake FlyCapture2 DLL: never touches hardware."""

import threading
import time

import numpy as np
import pytest
from test_maintenance_camera_gate2_mono import FakeCam, o

from openevos.backends.flycapture2 import camera as fcam
from openevos.core.models import CommandOrigin, Connection, SetCameraExposure, SetCameraGain
from openevos.maintenance import camera_gate2_mono as g2


class FakeChameleon(FakeCam):
    def _fc2GetPropertyInfo(self, ctx, p):
        x = o(p)
        x.present, x.readOutSupported = 1, 1
        if x.type == 12:  # shutter
            x.absMin, x.absMax = 0.005, 66.64
        elif x.type == 13:  # gain
            x.absMin, x.absMax = -5.63, 24.0
        return 0


REAL_SDK = g2.FlyCapture2SettingsBackend


def sdk(cam):
    return REAL_SDK(dll_path="fake.dll", loader=lambda p: cam)


class Transport:
    def __init__(self, accept=True):
        self.frames, self.accept = [], accept
        self.got = threading.Event()

    def push(self, px, md, owned):
        self.frames.append((px, md))
        self.got.set()
        return self.accept


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


def make(cam=None, transport=None):
    cam = cam or FakeChameleon()
    reports = []
    dev = fcam.FlyCapture2MonoCamera("cam-mono", transport or Transport(), sdk=sdk(cam))
    dev.attach(lambda **kw: reports.append(kw) or True)
    return dev, cam, reports


def test_connect_applies_operating_settings():
    dev, cam, reports = make()
    dev.connect()
    assert reports[-1]["connection"] is Connection.CONNECTED
    assert cam.mode == 23 and cam.packet == 2928  # Format7, 75 % packet
    assert not cam.props["auto_exposure"]["on"] and not cam.props["gain"]["auto"] and not cam.props["shutter"]["auto"]
    assert cam.props["shutter"]["abs"] == pytest.approx(fcam.DEFAULT_EXPOSURE_MS)
    assert cam.props["brightness"]["abs"] == pytest.approx(2.0)
    caps = dev.capabilities()
    r = {p.name: (p.limits.minimum, p.limits.maximum) for p in caps.ranges}
    assert r["exposure_ms"] == pytest.approx((0.01, 66.64)) and r["gain_db"] == pytest.approx((0.0, 24.0))
    assert caps.backend_kind.value == "physical"


def test_stream_delivers_real_12bit_frames():
    t = Transport()
    dev, cam, _ = make(transport=t)
    dev.connect()
    dev.start_stream(acquisition_id="acq-1", n_frames=3)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (len(t.frames) < 3 or dev.streaming):
        time.sleep(0.05)
    assert len(t.frames) == 3 and not dev.streaming
    px, md = t.frames[0]
    assert px.shape == (964, 1296) and px.dtype == np.uint16 and 150 < px.mean() < 250 and px.max() <= 4095
    assert md.synthetic is False and md.acquisition_id == "acq-1" and dict(md.extra)["bit_shift"] == "4"
    assert cam.calls.count("fc2StartCapture") == 1 and cam.calls.count("fc2StopCapture") == 1


def test_live_stream_stops_on_request():
    t = Transport()
    dev, _, _ = make(transport=t)
    dev.connect()
    dev.start_stream()
    assert t.got.wait(2.0)
    dev.stop_stream("live view stopped")
    assert not dev.streaming


def test_incomplete_frames_skipped_and_counted():
    t = Transport()
    cam = FakeChameleon()
    dev, _, _ = make(cam, t)
    dev.connect()
    cam.consistency_errors = 2
    dev.start_stream(n_frames=1)
    assert t.got.wait(2.0)
    assert dev.incomplete_frames == 2


def test_retrieval_error_restarts_capture_in_place_and_recovers(monkeypatch):
    """More than MAX_CONSECUTIVE_INCOMPLETE bad frames used to end the stream for good (live view froze with a
    fault). Now capture restarts in place (stop, start) and the stream carries on."""
    monkeypatch.setattr(fcam, "RESTART_PAUSE_S", 0.0)
    t = Transport()
    cam = FakeChameleon()
    dev, _, reports = make(cam, t)
    dev.connect()
    cam.consistency_errors = fcam.MAX_CONSECUTIVE_INCOMPLETE + 3
    dev.start_stream(n_frames=1)
    assert t.got.wait(5.0)
    assert dev.stream_restarts == 1 and cam.calls.count("fc2StartCapture") == 2
    healths = [r.get("health") for r in reports if r.get("health") is not None]
    assert healths[-2:] == [fcam.Health.WARNING, fcam.Health.NORMAL]
    assert "restarted" in next(r["reason"] for r in reports if r.get("health") is fcam.Health.WARNING)


class QuickRestartChameleon(FakeChameleon):
    """The instrument, 2026-11-24 (every capture): capture started again within moments of a stop sends a burst of
    incomplete frames, longer than the loop skips, so live view resumed right after a snap restarted with a camera
    warning each time. A pause after the stop (the restart path's RESTART_PAUSE_S) brings clean frames."""

    QUICK_S = 0.1

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stopped_at = None
        self.start_gaps = []

    def _fc2StopCapture(self, ctx):
        self.stopped_at = time.monotonic()
        return 0

    def _fc2StartCapture(self, ctx):
        if self.stopped_at is not None:
            gap = time.monotonic() - self.stopped_at
            self.start_gaps.append(gap)
            if gap < self.QUICK_S:
                self.consistency_errors = fcam.MAX_CONSECUTIVE_INCOMPLETE + 3
        return 0


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.005)
    return cond()


def test_live_view_resumed_after_a_snap_starts_clean():
    """InstrumentController.acquire: the one-frame snap stream stops, then live view starts again at once. The
    camera settles first, so live view resumes without the burst, the restart and the warning."""
    t = Transport()
    cam = QuickRestartChameleon()
    dev, _, reports = make(cam, t)
    dev.connect()
    dev.start_stream(acquisition_id="acq-1", n_frames=1)  # the snap
    assert _wait(lambda: len(t.frames) == 1 and not dev.streaming)
    t0 = time.monotonic()
    dev.start_stream()  # live view resumes
    assert time.monotonic() - t0 < 0.05  # the settle is on the stream thread: the capture task is not held up
    assert _wait(lambda: len(t.frames) >= 3)
    dev.stop_stream()
    assert dev.stream_restarts == 0 and not any(r.get("health") is fcam.Health.WARNING for r in reports)
    assert cam.start_gaps[-1] >= fcam.LIVE_RESTART_SETTLE_S - 0.01


def test_snap_starts_at_once_after_live_view_stops():
    """A snap is not delayed: the instrument delivers it complete right after live view stops (14:04 and 14:12
    captures, 2026-11-24); only live view, which restarts with nothing waiting for it, settles first."""
    t = Transport()
    dev, _cam, _ = make(transport=t)
    dev.connect()
    dev.start_stream()
    assert t.got.wait(2.0)
    dev.stop_stream("paused for acquisition")
    t0 = time.monotonic()
    dev.start_stream(acquisition_id="acq-1", n_frames=1)
    assert _wait(lambda: not dev.streaming)
    assert time.monotonic() - t0 < fcam.LIVE_RESTART_SETTLE_S


def test_live_view_stopped_while_settling_never_starts_capture():
    t = Transport()
    dev, cam, _ = make(transport=t)
    dev.connect()
    dev.start_stream(acquisition_id="acq-1", n_frames=1)
    assert _wait(lambda: len(t.frames) == 1 and not dev.streaming)
    starts = cam.calls.count("fc2StartCapture")
    dev.start_stream()  # live view: settling
    dev.stop_stream("paused for acquisition")  # the next snap, before live view started capture
    time.sleep(fcam.LIVE_RESTART_SETTLE_S + 0.1)
    assert cam.calls.count("fc2StartCapture") == starts and not dev.streaming
    assert not dev._sdk._capturing  # the camera is not left capturing with no one reading it


def test_persistent_retrieval_error_faults_after_the_restart_budget(monkeypatch):
    monkeypatch.setattr(fcam, "RESTART_PAUSE_S", 0.0)

    class Broken(FakeChameleon):
        def _fc2RetrieveBuffer(self, ctx, p):
            return 33  # not an image-consistency error: e.g. a timeout

    t = Transport()
    dev, _cam, reports = make(Broken(), t)
    dev.connect()
    dev.start_stream()
    deadline = time.monotonic() + 5
    while dev.streaming and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not dev.streaming and dev.stream_restarts == fcam.MAX_STREAM_RESTARTS
    fault = [r for r in reports if r.get("health") is fcam.Health.FAULT]
    assert fault and "after 3 restart(s)" in fault[-1]["fault"]


class UnpluggedChameleon(FakeChameleon):
    """The instrument 2026-11-25 15:03:45: the microscope switched off and on with live view running. Off USB, the
    camera answers 32 ("Isochronous transfer has not been started") and enumeration counts no camera."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gone = False

    def _fc2RetrieveBuffer(self, ctx, p):
        return 32 if self.gone else super()._fc2RetrieveBuffer(ctx, p)

    def _fc2StartCapture(self, ctx):
        return 32 if self.gone else 0

    def _fc2GetNumOfCameras(self, ctx, p):
        o(p).value = 0 if self.gone else 1
        return 0

    def _fc2Connect(self, ctx, guid):
        return 32 if self.gone else 0


def test_camera_reopens_after_the_microscope_is_switched_off_and_on(monkeypatch):
    monkeypatch.setattr(fcam, "RESTART_PAUSE_S", 0.0)
    monkeypatch.setattr(fcam, "RECONNECT_POLL_S", 0.02)
    t = Transport()
    cam = UnpluggedChameleon()
    dev, _, reports = make(cam, t)
    dev.connect()
    first = dev._snapshot
    dev.start_stream()  # live view
    assert t.got.wait(2.0)
    cam.gone = True
    assert _wait(lambda: any(r.get("health") is fcam.Health.FAULT for r in reports) and not dev.streaming)
    time.sleep(0.2)
    assert dev.reconnects == 0  # off USB: nothing opened, only counted
    cam.gone = False
    n = len(t.frames)
    assert _wait(lambda: dev.reconnects == 1 and dev.streaming and len(t.frames) > n)  # live view is back
    assert dev._snapshot is first  # exit still restores the state found at the first connect
    assert any("reopened" in (r.get("reason") or "") for r in reports)
    assert [r.get("health") for r in reports if r.get("health") is not None][-1] is fcam.Health.NORMAL
    dev.disconnect()


def test_recover_reopens_the_camera_or_says_it_is_not_on_usb():
    cam = UnpluggedChameleon()
    dev, _, reports = make(cam)
    dev.connect()
    cam.gone = True
    dev.recover()
    assert reports[-1]["health"] is fcam.Health.FAULT and "not on USB" in reports[-1]["reason"]
    cam.gone = False
    dev.recover()
    assert dev.reconnects == 1 and dev._connected and "reopened" in reports[-1]["reason"]
    dev.disconnect()


def test_exposure_and_gain_commands_read_back():
    dev, cam, _ = make()
    dev.connect()
    ctx = Ctx()
    dev.start(SetCameraExposure(device_id="cam-mono", origin=CommandOrigin.UI, exposure_ms=5.0, timeout_s=5.0), ctx)
    assert ctx.done and cam.props["shutter"]["abs"] == pytest.approx(5.0)
    ctx = Ctx()
    dev.start(SetCameraGain(device_id="cam-mono", origin=CommandOrigin.UI, gain_db=6.0, timeout_s=5.0), ctx)
    assert ctx.done and dev.verify_result(SetCameraGain(device_id="cam-mono", origin=CommandOrigin.UI, gain_db=6.0, timeout_s=5.0))[0]  # noqa: E501


def test_settings_accepted_while_streaming():
    t = Transport()
    dev, cam, _ = make(transport=t)
    dev.connect()
    dev.start_stream()
    ctx = Ctx()
    dev.start(SetCameraExposure(device_id="cam-mono", origin=CommandOrigin.UI, exposure_ms=5.0, timeout_s=5.0), ctx)
    dev.stop_stream()
    assert ctx.done and not ctx.failed and cam.props["shutter"]["abs"] == pytest.approx(5.0)


class LightLaggingChameleon(FakeChameleon):
    """The mono lag check (2026-11-16): the frame retrieved next was exposed under the light as it was at the
    previous retrieval. bright = the light now; the frame shows the light it was really taken under."""

    def _fc2RetrieveBuffer(self, ctx, p):
        now = self.bright
        self.bright = getattr(self, "in_flight", False)
        rc = super()._fc2RetrieveBuffer(ctx, p)
        self.bright, self.in_flight = now, now
        return rc


def _snap(dev, t):
    n = len(t.frames)
    dev.start_stream(n_frames=1)
    deadline = time.monotonic() + 5
    while (len(t.frames) <= n or dev.streaming) and time.monotonic() < deadline:
        time.sleep(0.01)
    return int(t.frames[-1][0].max())


@pytest.mark.parametrize("invalidate", [False, True])
def test_snap_after_light_change(invalidate):
    """Without invalidate_frames the first snap after the light comes on is dark (the bug, seen on the
    instrument); with it the snap is lit. Steady light drops nothing."""
    t = Transport()
    dev, cam, _ = make(LightLaggingChameleon(), t)
    dev.connect()
    assert _snap(dev, t) < 1000  # dark
    cam.bright = True
    if invalidate:
        dev.invalidate_frames("light changed")
    lit = 30000 >> fcam.BIT_SHIFT
    assert (_snap(dev, t) == lit) is invalidate and dev.stale_dropped == int(invalidate)
    assert _snap(dev, t) == lit and dev.stale_dropped == int(invalidate)


def test_disconnect_restores_power_up_state():
    dev, cam, reports = make()
    dev.connect()
    dev.disconnect()
    assert dev.restore_diffs == [] and cam.mode == 17 and cam.rate == 3
    assert cam.props["gain"]["auto"] and cam.props["shutter"]["auto"] and cam.props["brightness"]["value"] == 0
    assert "restored" in reports[-1]["reason"]
    assert not any("MemoryChannel" in c and c != "fc2GetMemoryChannel" and c != "fc2GetMemoryChannelInfo"
                   for c in cam.calls)


def test_wrong_model_faults_without_settings():
    cam = FakeChameleon()
    orig = cam._fc2GetCameraInfo

    def info(ctx, p):
        orig(ctx, p)
        o(p).modelName = b"Flea3"
        return 0
    cam._fc2GetCameraInfo = info
    dev, _, reports = make(cam)
    dev.connect()
    assert reports[-1]["connection"] is Connection.DISCONNECTED and "Flea3" in reports[-1]["fault"]
    assert not any(c.startswith("fc2Set") for c in cam.calls)


def test_session_uses_real_mono_camera(monkeypatch):
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.instrument.profile import CameraRole

    cam = FakeChameleon()
    monkeypatch.setattr(fcam.fc, "FlyCapture2SettingsBackend", lambda: sdk(cam))
    s = build_sim_session(ThreadClock(), fast=True, cameras={"mono": "flycapture2"})
    try:
        mono = s.controller.camera_backend(CameraRole.MONO)
        assert isinstance(mono, fcam.FlyCapture2MonoCamera) and mono.identity["model"] == "Chameleon CMLN-13S2M"
        color = s.controller.camera_backend(CameraRole.COLOR)
        assert not isinstance(color, fcam.FlyCapture2MonoCamera)
        frames = s.controller.acquire(CameraRole.MONO, n_frames=2, timeout=10)
        assert len(frames) == 2 and not frames[0].metadata.synthetic
        img = s.capture.capture("trans", CameraRole.MONO)  # the Capture button's path (sets exposure first)
        assert not img.synthetic and img.data.shape == (964, 1296)
        assert cam.props["shutter"]["abs"] == pytest.approx(s.settings.channel(CameraRole.MONO, "trans").exposure_ms)
        s.controller.start_live(CameraRole.MONO)  # the real UI snaps with live view running
        s.settings.channel(CameraRole.MONO, "trans").exposure_ms = 7.0
        img = s.capture.capture("trans", CameraRole.MONO)
        assert not img.synthetic and cam.props["shutter"]["abs"] == pytest.approx(7.0)
        assert s.controller.is_live(CameraRole.MONO)  # live view resumes after the snap
        ctl = s.controller
        ctl.run(ctl.set_light("trans", 33.0, True))  # a real light change: the controller invalidates
        deadline = time.monotonic() + 5
        while mono.stale_dropped == 0 and time.monotonic() < deadline:  # live view consumes the drop
            time.sleep(0.01)
        assert mono.stale_dropped >= 1
    finally:
        s.shutdown()
    assert cam.mode == 17  # restored on shutdown


def test_app_mono_camera_resolution():
    from openevos.ui.app import _resolve_mono_camera as r

    assert r("sim") is None and r("flycapture2") == "flycapture2"
    assert r("auto", probe=lambda: True) == "flycapture2" and r("auto", probe=lambda: False) is None
    assert r("auto", screenshots=True, probe=lambda: True) is None


def test_unknown_camera_backend_refused():
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock

    with pytest.raises(ValueError, match="no real camera backend"):
        build_sim_session(ThreadClock(), fast=True, cameras={"color": "flycapture2"})
