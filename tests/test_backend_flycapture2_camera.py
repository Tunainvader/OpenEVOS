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


def test_exposure_and_gain_commands_read_back():
    dev, cam, _ = make()
    dev.connect()
    ctx = Ctx()
    dev.start(SetCameraExposure(device_id="cam-mono", origin=CommandOrigin.UI, exposure_ms=5.0, timeout_s=5.0), ctx)
    assert ctx.done and cam.props["shutter"]["abs"] == pytest.approx(5.0)
    ctx = Ctx()
    dev.start(SetCameraGain(device_id="cam-mono", origin=CommandOrigin.UI, gain_db=6.0, timeout_s=5.0), ctx)
    assert ctx.done and dev.verify_result(SetCameraGain(device_id="cam-mono", origin=CommandOrigin.UI, gain_db=6.0, timeout_s=5.0))[0]


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
