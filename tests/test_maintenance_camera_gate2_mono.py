import ctypes as C
import json

import numpy as np
import pytest

from openevos.maintenance import camera_gate1 as g1
from openevos.maintenance import camera_gate2_mono as g2
from openevos.maintenance import flycapture2_gate as fc

SERIAL = 42
POWER_UP = {  # as read by Gate 1 on the instrument (raw values)
    "brightness": {"on": True, "auto": False, "value": 0, "abs": 0.0},
    "auto_exposure": {"on": True, "auto": True, "value": 1, "abs": -7.58},
    "gamma": {"on": False, "auto": False, "value": 1024, "abs": 1.0},
    "shutter": {"on": True, "auto": True, "value": 256, "abs": 12.0},
    "gain": {"on": True, "auto": True, "value": 160, "abs": 0.0},
    "frame_rate": {"on": True, "auto": True, "value": 480, "abs": 15.0},
    "temperature": {"on": False, "auto": False, "value": 3164, "abs": 0.0},
}
TYPE_NAME = {v: k for k, v in fc.PROPERTIES.items()}


def o(a):
    return a._obj


class FakeCam:
    """Stateful fake of FlyCapture2_C_v140.dll for Gate 2."""

    def __init__(self, bias=200.0, fail=None, restore_breaks=False, sticky_gain=False, bright=False):
        self.calls = []
        self.fail, self.restore_breaks, self.sticky_gain, self.bright = fail, restore_breaks, sticky_gain, bright
        self.bias = bias
        self.mode, self.rate = 17, 3
        self.props = {k: dict(v) for k, v in POWER_UP.items()}
        self.rng = np.random.default_rng(0)
        self._buf = None
        self.fc2ErrorToDescription = lambda rc: b"fake"
        for name in g2.SETTER_BOUND:
            if name != "fc2ErrorToDescription":
                setattr(self, name, self._wrap(name))

    def _wrap(self, name):
        def fn(*a):
            self.calls.append(name)
            if self.fail == name:
                return 1
            return getattr(self, "_" + name, lambda *x: 0)(*a)
        return fn

    def _fc2CreateContext(self, p):
        o(p).value = 7
        return 0

    def _fc2GetNumOfCameras(self, ctx, p):
        o(p).value = 1
        return 0

    def _fc2GetCameraInfo(self, ctx, p):
        i = o(p)
        i.serialNumber, i.modelName = SERIAL, b"Chameleon CMLN-13S2M"
        return 0

    def _fc2GetVideoModeAndFrameRate(self, ctx, vm, fr):
        o(vm).value, o(fr).value = self.mode, self.rate
        return 0

    def _fc2SetVideoModeAndFrameRate(self, ctx, vm, fr):
        self.mode, self.rate = vm.value, fr.value
        if self.restore_breaks:
            self.props["gamma"]["value"] = 999
        return 0

    def _fc2GetMemoryChannelInfo(self, ctx, p):
        o(p).value = 2
        return 0

    def _fc2GetProperty(self, ctx, p):
        x = o(p)
        s = self.props[TYPE_NAME[x.type]]
        x.present, x.onOff, x.autoManualMode, x.valueA, x.absValue = 1, s["on"], s["auto"], s["value"], s["abs"]
        return 0

    def _fc2SetProperty(self, ctx, p):
        x = o(p)
        name = TYPE_NAME[x.type]
        s = self.props[name]
        s["on"], s["auto"] = bool(x.onOff), bool(x.autoManualMode)
        if name == "gain" and self.sticky_gain:
            s["auto"] = True
        if x.absControl:
            s["abs"] = x.absValue
        else:
            s["value"] = x.valueA
        return 0

    def _fc2ValidateFormat7Settings(self, ctx, s, ok, pkt):
        o(ok).value = 1
        o(pkt).recommendedBytesPerPacket = 3904
        o(pkt).maxBytesPerPacket = 3904
        o(pkt).unitBytesPerPacket = 4
        return 0
    def _fc2SetFormat7ConfigurationPacket(self, ctx, s, pkt):
        st = o(s)
        assert (st.width, st.height, st.pixelFormat) == (1296, 964, 0x04000000)
        self.packet = pkt.value
        self.mode = fc.VIDEOMODE_FORMAT7
        return 0

    def _fc2RetrieveBuffer(self, ctx, p):
        if getattr(self, "consistency_errors", 0) > 0:
            self.consistency_errors -= 1
            return g2.FC2_IMAGE_CONSISTENCY_ERROR
        img = o(p)
        level = 30000 if self.bright else self.bias
        px = (np.clip(self.rng.normal(level, 4, (964, 1296)), 0, 4095).astype(np.uint16) * 16
              if not self.bright else np.full((964, 1296), level, np.uint16))
        self._buf = np.ascontiguousarray(px.astype(">u2"))  # the camera sends big-endian Y16
        img.rows, img.cols, img.stride, img.format = 964, 1296, 1296 * 2, 0x04000000
        img.pData = self._buf.ctypes.data_as(C.POINTER(C.c_ubyte))
        return 0


class Pnp:
    def present(self, vid, pid):
        return [g1.PnpDevice("x", vid, pid, 0)]


def gate1_record(tmp_path, serial=SERIAL):
    props = {k: {"present": True, "value": v["value"], "auto": v["auto"], "on": v["on"]} for k, v in POWER_UP.items()}
    rec = {"identity": {"serial": serial}, "steps_done": ["connect", "identity", "modes", "grab_one", "close"],
           "modes": {"video_mode": 17, "frame_rate_enum": 3, "properties": props}}
    (tmp_path / "camera-gate1-mono-20261113T180418-stopped.json").write_text(json.dumps(rec), encoding="utf-8")


def be(cam):
    return g2.FlyCapture2SettingsBackend(dll_path="fake.dll", loader=lambda p: cam)


def run(tmp_path, cam, **kw):
    return g2.run_gate2(Pnp(), be(cam), evidence_dir=tmp_path, send=True, confirm=g2.CONFIRM_PHRASE, **kw)


def test_setter_set_is_exactly_reviewed():
    extra = set(g2.SETTER_BOUND) - set(fc.BOUND)
    assert extra == {"fc2GetMemoryChannel", "fc2GetMemoryChannelInfo", "fc2ValidateFormat7Settings",
                     "fc2SetFormat7ConfigurationPacket", "fc2SetProperty", "fc2SetVideoModeAndFrameRate"}
    for banned in ("fc2SaveToMemoryChannel", "fc2RestoreFromMemoryChannel", "fc2WriteRegister",
                   "fc2SetConfiguration", "fc2FireBusReset"):
        with pytest.raises(fc.Fc2Error):
            be(FakeCam())._call(banned)


def test_gamma_not_settable():
    b = be(FakeCam())
    with pytest.raises(g2.Gate2Error):
        b.set_prop("gamma", on=True, auto=False, raw_value=1)


def test_dry_run_and_phrase(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    r = g2.run_gate2(Pnp(), be(cam), evidence_dir=tmp_path)
    assert r.outcome == "dry_run" and "fc2Connect" not in cam.calls
    r = g2.run_gate2(Pnp(), be(cam), evidence_dir=tmp_path, send=True, confirm="SET CAMERA")
    assert r.outcome == "refused" and "fc2Connect" not in cam.calls


def test_refuses_without_gate1_record(tmp_path):
    r = run(tmp_path, FakeCam())
    assert r.outcome == "refused" and "Gate 1" in r.message


def test_complete_run_restores(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    r = run(tmp_path, cam)
    assert r.passed, r.message
    assert set(r.exposures) == {"1ms", "10ms", "50ms"}
    s = r.exposures["10ms"]
    assert s["shape"] == [964, 1296] and s["histogram_step"] == 16 and 3000 < s["mean"] < 3400
    assert 40 < s["temporal_noise"] < 90
    assert cam.calls.count("fc2RetrieveBuffer") == 12  # 3 exposures x (1 discarded + 3 kept)
    assert r.restore_verified and cam.mode == 17 and cam.props["gain"]["auto"] and cam.props["shutter"]["auto"]
    assert cam.props["brightness"]["value"] == 0
    assert len(r.frame_files) == 3 and all((tmp_path / f).exists() for f in r.frame_files)
    assert r.snapshot["memory_channels"] == 2


def test_packet_size_is_three_quarters_of_max(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    r = run(tmp_path, cam)
    assert cam.packet == 2928 and cam.packet % 4 == 0 and r.settings[0]["format7"]["packet_used"] == 2928


def test_incomplete_frames_are_skipped_within_limit(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    cam.consistency_errors = 3
    r = run(tmp_path, cam)
    assert r.passed and r.exposures["1ms"]["incomplete_frames_skipped"] == 3
    assert r.exposures["10ms"]["incomplete_frames_skipped"] == 0


def test_too_many_incomplete_frames_stop(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    cam.consistency_errors = 4
    r = run(tmp_path, cam)
    assert r.outcome == "stopped" and "returned 41" in r.message and r.restore_verified


def test_clipped_is_reported_not_passed(tmp_path):
    gate1_record(tmp_path)
    r = run(tmp_path, FakeCam(bias=0.0))
    assert r.outcome == "clipped" and not r.passed and r.restore_verified


def test_readback_mismatch_stops_and_restores(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam(sticky_gain=True)
    r = run(tmp_path, cam)
    assert r.outcome == "stopped" and "gain.auto" in r.message
    assert "fc2RetrieveBuffer" not in cam.calls and cam.mode == 17


def test_state_differs_from_gate1_touches_nothing(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam()
    cam.props["gamma"]["value"] = 2048
    r = run(tmp_path, cam)
    assert r.outcome == "stopped" and "differs" in r.message
    assert not any(c.startswith("fc2Set") for c in cam.calls)


def test_bright_frame_stops(tmp_path):
    gate1_record(tmp_path)
    r = run(tmp_path, FakeCam(bright=True))
    assert r.outcome == "stopped" and "light leak" in r.message and r.restore_verified


def test_unverified_restore_tells_operator_to_power_cycle(tmp_path):
    gate1_record(tmp_path)
    r = run(tmp_path, FakeCam(restore_breaks=True))
    assert r.outcome == "stopped" and not r.restore_verified and "power-cycle" in r.message


def test_format7_failure_restores(tmp_path):
    gate1_record(tmp_path)
    cam = FakeCam(fail="fc2SetFormat7ConfigurationPacket")
    r = run(tmp_path, cam)
    assert r.outcome == "stopped" and "format7" in r.message
    assert "fc2SetVideoModeAndFrameRate" in cam.calls and r.restore_verified
    assert cam.calls[-2:] == ["fc2Disconnect", "fc2DestroyContext"]
