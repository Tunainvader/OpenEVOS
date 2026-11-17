import ctypes as C

import numpy as np
import pytest

from openevos.maintenance import camera_gate1 as g
from openevos.maintenance import flycapture2_gate as f


def obj(arg):
    return arg._obj  # the object behind ctypes.byref


class FakeLib:
    """Stands in for FlyCapture2_C_v140.dll. Records calls; fills output structs."""

    def __init__(self, n=1, fmt=0x04000000, hang=False, fail=None):
        self.calls, self.n, self.fmt, self.hang, self.fail = [], n, fmt, hang, fail
        self._buf = None
        self.fc2ErrorToDescription = lambda rc: b"fake error"
        for name in f.BOUND:
            if name != "fc2ErrorToDescription":
                setattr(self, name, self._wrap(name))

    def _wrap(self, name):
        def fn(*args):
            self.calls.append(name)
            if self.fail == name:
                return 1
            return getattr(self, "_" + name, lambda *a: 0)(*args)
        return fn

    def _fc2CreateContext(self, p):
        obj(p).value = 1234
        return 0

    def _fc2GetNumOfCameras(self, ctx, p):
        obj(p).value = self.n
        return 0

    def _fc2GetCameraInfo(self, ctx, p):
        i = obj(p)
        i.serialNumber, i.modelName, i.sensorInfo, i.fw_ver = 42, b"Chameleon CMLN-13S2M", b"ICX445", b"1.17.3.0"
        return 0

    def _fc2GetVideoModeAndFrameRate(self, ctx, vm, fr):
        obj(vm).value, obj(fr).value = f.VIDEOMODE_FORMAT7, 8
        return 0

    def _fc2GetFormat7Configuration(self, ctx, s, pkt, pct):
        o = obj(s)
        o.width, o.height, o.pixelFormat = 1296, 964, self.fmt
        return 0

    def _fc2GetFormat7Info(self, ctx, p, ok):
        i = obj(p)
        if i.mode == 0:
            i.maxWidth, i.maxHeight, i.pixelFormatBitField = 1296, 964, 0x84000000
            obj(ok).value = 1
        return 0

    def _fc2GetPropertyInfo(self, ctx, p):
        i = obj(p)
        i.present, i.readOutSupported, i.max = 1, 1, 1000
        return 0

    def _fc2RetrieveBuffer(self, ctx, p):
        if self.hang:
            import time
            time.sleep(1.0)
            return 1
        img = obj(p)
        depth16 = self.fmt in (0x04000000, 0x00200000)
        stride = 1296 * (2 if depth16 else 1) + 8
        arr = np.zeros((964, stride), dtype=np.uint8)
        if depth16:
            arr[:, : 1296 * 2] = np.full((964, 1296), 300, ">u2").view(np.uint8)
        else:
            arr[:, :1296] = 30
        self._buf = arr
        img.rows, img.cols, img.stride, img.format = 964, 1296, stride, self.fmt
        img.pData = arr.ctypes.data_as(C.POINTER(C.c_ubyte))
        return 0


def backend(lib):
    return f.FlyCapture2GateBackend(dll_path="fake.dll", loader=lambda p: lib)


MONO = g.ROLES["mono"]


class Pnp:
    def present(self, vid, pid):
        return [g.PnpDevice("x", vid, pid, 0)]


def test_only_read_functions_bound():
    for name in f.BOUND:
        assert not any(w in name for w in ("Set", "Write", "Save", "Restore", "Firmware", "Register"))
    b = backend(FakeLib())
    with pytest.raises(f.Fc2Error):
        b._call("fc2SetProperty")


def test_count_uses_temporary_context():
    lib = FakeLib(n=1)
    assert backend(lib).count_cameras() == 1
    assert lib.calls == ["fc2CreateContext", "fc2GetNumOfCameras", "fc2DestroyContext"]


def test_full_gate_with_fake_dll(tmp_path):
    lib = FakeLib()
    r = g.run_gate(MONO, Pnp(), backend(lib), send=True, confirm=MONO.phrase, evidence_dir=tmp_path)
    assert r.passed, r.message
    assert r.identity["model"] == "Chameleon CMLN-13S2M" and r.identity["fw_version"] == "1.17.3.0"
    assert r.modes["format7_current"]["pixel_format"] == "MONO16"
    assert r.modes["format7_modes"][0]["pixel_formats"] == ["MONO8", "MONO16"]
    assert r.frame_stats["mean"] == 300 and r.frame_stats["bit_depth"] == 16
    assert lib.calls.count("fc2RetrieveBuffer") == 1 and lib.calls.count("fc2StartCapture") == 1
    assert lib.calls.count("fc2StopCapture") == 1
    assert lib.calls[-2:] == ["fc2Disconnect", "fc2DestroyContext"]


def test_mono8_frame_strided():
    lib = FakeLib(fmt=0x80000000)
    b = backend(lib)
    b.connect()
    px, depth = b.grab_one(2.0)
    assert px.shape == (964, 1296) and px.dtype == np.uint8 and int(px.max()) == 30 and depth == 8
    b.close()


def test_grab_timeout_stops_capture():
    lib = FakeLib(hang=True)
    b = backend(lib)
    b.connect()
    with pytest.raises(f.Fc2Error, match="no frame"):
        b.grab_one(0.2)
    assert lib.calls.count("fc2StopCapture") == 1 and lib.calls.count("fc2RetrieveBuffer") == 1
    b.close()


def test_connect_failure_stops_and_cleans_up():
    lib = FakeLib(fail="fc2Connect")
    r = g.run_gate(MONO, Pnp(), backend(lib), send=True, confirm=MONO.phrase)
    assert r.outcome == "stopped" and "fc2Connect" in r.message
    assert "fc2Disconnect" not in lib.calls and lib.calls[-1] == "fc2DestroyContext"


def test_mono16_is_decoded_big_endian():
    data = np.array([[0x0C80, 0x0010]], dtype=">u2")
    img = f.Image()
    img.rows, img.cols, img.stride, img.format = 1, 2, 4, 0x04000000
    img.pData = data.ctypes.data_as(C.POINTER(C.c_ubyte))
    px, _name, _depth = f.image_to_array(img)
    assert px.tolist() == [[3200, 16]] and px.dtype == np.uint16 and px.dtype.isnative


def test_missing_dll_is_not_available(tmp_path):
    assert not f.FlyCapture2GateBackend(dll_path=tmp_path / "nope.dll").sdk_available()
